"""M02: object list search, filters, sort and exposed dates (offline, SQLite).

The files workspace needs server-side search/filter/sort so it never loads the
whole catalogue to answer a query. ``GET /api/v1/objects`` now accepts ``q``
(name substring), ``status``, ``content_type``, ``min_size``/``max_size``,
``created_after``/``created_before`` and ``sort``/``order``; the object payload
also carries ``created_at``/``updated_at``. No S3 endpoint is contacted: upload
intents only create a pending row and presign a URL, and ``mark_available`` is a
pure catalogue update used here to give a row a real size without storage.
"""
import os
import time
import uuid

import pytest

os.environ.setdefault("AURA_API_ADMIN_PASSWORD", "test-admin-password")
os.environ.setdefault("AURA_S3_ACCESS_KEY", "testaccess12345678")
os.environ.setdefault("AURA_S3_SECRET_KEY", "testsecret45678901234567890123456789")
os.environ.setdefault("AURA_S3_ENDPOINT", "https://127.0.0.1:9")
os.environ.setdefault("AURA_API_DB", ":memory:")
os.environ.setdefault("AURA_COOKIE_SECURE", "0")
os.environ.setdefault("AURA_AUDIT_LOG", "/tmp/opencode/aura-test-audit.jsonl")
os.environ.setdefault("AURA_M2M_RATE_LIMIT", "100000")

from fastapi.testclient import TestClient  # noqa: E402

import api.main as main  # noqa: E402
from tests import catalogue_sql  # noqa: E402
from api import authz  # noqa: E402
from api.auth import hash_password  # noqa: E402
from api.object_query import OBJECT_STATUSES, SORT_COLUMNS  # noqa: E402

client = TestClient(main.app)


@pytest.fixture(autouse=True)
def _reset_limiters():
    main.reset_login_attempts()
    authz.reset_m2m_attempts()
    yield
    main.reset_login_attempts()
    authz.reset_m2m_attempts()


def _admin():
    c = TestClient(main.app)
    r = c.post("/api/v1/login", json={
        "username": "admin", "password": os.environ["AURA_API_ADMIN_PASSWORD"],
    })
    assert r.status_code == 200, r.text
    return c, {"X-CSRF-Token": r.json()["csrf_token"]}


def _upload(c, headers, name, *, content_type=None, folder_id=None):
    body = {"display_name": name, "size_bytes": 1}
    if content_type is not None:
        body["content_type"] = content_type
    if folder_id is not None:
        body["folder_id"] = folder_id
    r = c.post("/api/v1/objects/upload-intents", json=body, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _names(payload):
    return [i["display_name"] for i in payload["items"]]


def _ids(payload):
    return {i["id"] for i in payload["items"]}


def _make_available(oid, size, name="digest"):
    # Catalogue-only transition: gives the row a real size/checksum so the size
    # filter and the "available" status are exercised without touching storage.
    main.repo.mark_available(oid, size, uuid.uuid4().hex)


def _set_created_at(oid, value):
    catalogue_sql.execute(
        "UPDATE objects SET created_at = ? WHERE id = ?", (value, oid)
    )


# ---------------------------------------------------------------------------
# Exposed metadata
# ---------------------------------------------------------------------------
def test_list_exposes_created_and_updated_dates():
    c, headers = _admin()
    oid = _upload(c, headers, f"dated-{uuid.uuid4().hex[:8]}.txt")
    item = next(
        i for i in c.get("/api/v1/objects", headers=headers).json()["items"]
        if i["id"] == oid
    )
    assert isinstance(item["created_at"], int)
    assert isinstance(item["updated_at"], int)
    # The internal storage coordinates are never part of the payload.
    assert "object_key" not in item and "bucket" not in item


def test_get_by_id_also_exposes_dates():
    c, headers = _admin()
    oid = _upload(c, headers, f"one-{uuid.uuid4().hex[:8]}.txt")
    got = c.get(f"/api/v1/objects/{oid}", headers=headers)
    assert got.status_code == 200
    assert set(got.json()) >= {"created_at", "updated_at"}


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------
def test_search_is_case_insensitive_substring():
    c, headers = _admin()
    tag = uuid.uuid4().hex[:8]
    target = _upload(c, headers, f"Rapport-{tag}.pdf")
    _upload(c, headers, f"photo-{tag}.jpg")

    hit = c.get("/api/v1/objects", params={"q": "rapport"}, headers=headers)
    assert hit.status_code == 200
    assert _ids(hit.json()) == {target}

    upper = c.get("/api/v1/objects", params={"q": "RAPPORT"}, headers=headers)
    assert _ids(upper.json()) == {target}


def test_search_without_match_is_empty_not_error():
    c, headers = _admin()
    _upload(c, headers, f"note-{uuid.uuid4().hex[:8]}.txt")
    r = c.get("/api/v1/objects", params={"q": "zz-no-such-name"}, headers=headers)
    assert r.status_code == 200
    assert r.json()["items"] == []


def test_search_treats_wildcards_literally():
    c, headers = _admin()
    tag = uuid.uuid4().hex[:8]
    percent = _upload(c, headers, f"rate-100%-{tag}.txt")
    plain = _upload(c, headers, f"rate-100-{tag}.txt")

    # "%" must be a literal character, not a LIKE wildcard matching everything.
    r = c.get("/api/v1/objects", params={"q": "100%"}, headers=headers)
    assert _ids(r.json()) == {percent}
    assert plain not in _ids(r.json())

    # A bare "%" only matches names that literally contain "%".
    only_percent = c.get("/api/v1/objects", params={"q": "%"}, headers=headers)
    assert _ids(only_percent.json()) == {percent}


def test_search_never_matches_the_internal_object_key():
    c, headers = _admin()
    oid = _upload(c, headers, f"opaque-{uuid.uuid4().hex[:8]}.txt")
    row = catalogue_sql.fetch_one(
        "SELECT object_key FROM objects WHERE id = ?", (oid,)
    )
    key_fragment = row["object_key"].split("/")[-1]
    r = c.get("/api/v1/objects", params={"q": key_fragment}, headers=headers)
    assert r.status_code == 200
    assert r.json()["items"] == []


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------
def test_filter_by_status():
    c, headers = _admin()
    tag = uuid.uuid4().hex[:8]
    pending = _upload(c, headers, f"pending-{tag}.txt")
    deleted = _upload(c, headers, f"deleted-{tag}.txt")
    assert c.delete(f"/api/v1/objects/{deleted}", headers=headers).status_code == 200

    pend = c.get("/api/v1/objects", params={"status": "pending"}, headers=headers).json()
    assert pending in _ids(pend) and deleted not in _ids(pend)

    gone = c.get("/api/v1/objects", params={"status": "deleted"}, headers=headers).json()
    assert deleted in _ids(gone) and pending not in _ids(gone)

    avail = c.get("/api/v1/objects", params={"status": "available"}, headers=headers)
    assert avail.status_code == 200


def test_filter_by_content_type_is_case_insensitive():
    c, headers = _admin()
    tag = uuid.uuid4().hex[:8]
    pdf = _upload(c, headers, f"doc-{tag}", content_type="application/pdf")
    _upload(c, headers, f"txt-{tag}", content_type="text/plain")

    r = c.get(
        "/api/v1/objects", params={"content_type": "APPLICATION/PDF"}, headers=headers
    )
    assert _ids(r.json()) == {pdf}


def test_filter_by_size_uses_committed_size():
    c, headers = _admin()
    tag = uuid.uuid4().hex[:8]
    small = _upload(c, headers, f"small-{tag}.bin")
    big = _upload(c, headers, f"big-{tag}.bin")
    _make_available(small, 10)
    _make_available(big, 10_000)

    ge = c.get("/api/v1/objects", params={"min_size": 1000}, headers=headers).json()
    assert big in _ids(ge) and small not in _ids(ge)

    le = c.get("/api/v1/objects", params={"max_size": 100}, headers=headers).json()
    assert small in _ids(le) and big not in _ids(le)


def test_filter_by_creation_period():
    c, headers = _admin()
    tag = uuid.uuid4().hex[:8]
    old = _upload(c, headers, f"old-{tag}.txt")
    new = _upload(c, headers, f"new-{tag}.txt")
    base = int(time.time())
    _set_created_at(old, base - 10_000)
    _set_created_at(new, base)

    after = c.get(
        "/api/v1/objects", params={"created_after": base - 60}, headers=headers
    ).json()
    assert new in _ids(after) and old not in _ids(after)

    before = c.get(
        "/api/v1/objects", params={"created_before": base - 60}, headers=headers
    ).json()
    assert old in _ids(before) and new not in _ids(before)


# ---------------------------------------------------------------------------
# Sort
# ---------------------------------------------------------------------------
def test_sort_by_name_ascending_and_descending():
    c, headers = _admin()
    tag = uuid.uuid4().hex[:8]
    # Names chosen so alphabetical order differs from insertion order.
    _upload(c, headers, f"bbb-{tag}.txt")
    _upload(c, headers, f"aaa-{tag}.txt")
    _upload(c, headers, f"ccc-{tag}.txt")

    asc = c.get(
        "/api/v1/objects",
        params={"q": tag, "sort": "display_name", "order": "asc"},
        headers=headers,
    ).json()
    assert _names(asc) == [f"aaa-{tag}.txt", f"bbb-{tag}.txt", f"ccc-{tag}.txt"]

    desc = c.get(
        "/api/v1/objects",
        params={"q": tag, "sort": "display_name", "order": "desc"},
        headers=headers,
    ).json()
    assert _names(desc) == [f"ccc-{tag}.txt", f"bbb-{tag}.txt", f"aaa-{tag}.txt"]


def test_sort_by_size_descending():
    c, headers = _admin()
    tag = uuid.uuid4().hex[:8]
    a = _upload(c, headers, f"a-{tag}.bin")
    b = _upload(c, headers, f"b-{tag}.bin")
    c2 = _upload(c, headers, f"c-{tag}.bin")
    _make_available(a, 100)
    _make_available(b, 3_000)
    _make_available(c2, 20)

    r = c.get(
        "/api/v1/objects",
        params={"q": tag, "sort": "size_bytes", "order": "desc"},
        headers=headers,
    ).json()
    assert _names(r) == [f"b-{tag}.bin", f"a-{tag}.bin", f"c-{tag}.bin"]


def test_default_sort_is_newest_first():
    c, headers = _admin()
    tag = uuid.uuid4().hex[:8]
    older = _upload(c, headers, f"older-{tag}.txt")
    newer = _upload(c, headers, f"newer-{tag}.txt")
    base = int(time.time())
    _set_created_at(older, base - 100)
    _set_created_at(newer, base)

    r = c.get("/api/v1/objects", params={"q": tag}, headers=headers).json()
    assert _names(r) == [f"newer-{tag}.txt", f"older-{tag}.txt"]


# ---------------------------------------------------------------------------
# Pagination stability + validation
# ---------------------------------------------------------------------------
def test_pagination_is_stable_across_pages():
    c, headers = _admin()
    tag = uuid.uuid4().hex[:8]
    created = [
        _upload(c, headers, f"page-{tag}-{i:02d}.txt") for i in range(5)
    ]
    page1 = c.get(
        "/api/v1/objects", params={"q": tag, "limit": 2, "offset": 0}, headers=headers
    ).json()
    page2 = c.get(
        "/api/v1/objects", params={"q": tag, "limit": 2, "offset": 2}, headers=headers
    ).json()
    page3 = c.get(
        "/api/v1/objects", params={"q": tag, "limit": 2, "offset": 4}, headers=headers
    ).json()
    seen = _ids(page1) | _ids(page2) | _ids(page3)
    assert seen == set(created)
    assert len(page1["items"]) == 2 and len(page3["items"]) == 1


@pytest.mark.parametrize(
    "params",
    [
        {"status": "nonsense"},
        {"sort": "pass_hash"},
        {"sort": "bucket"},
        {"order": "sideways"},
        {"min_size": -1},
        {"created_after": -5},
    ],
)
def test_invalid_query_values_are_refused(params):
    c, headers = _admin()
    r = c.get("/api/v1/objects", params=params, headers=headers)
    assert r.status_code == 422


def test_query_vocabulary_matches_the_repository():
    # The route's ``ObjectStatus`` Literal and the SQL builder's vocabulary must not
    # drift. ``aborted`` is written by the abort endpoint (api/main.py) and defined by
    # api/transfer.py, so both must carry it. Comparing against the route Literal
    # itself (not a hard-coded set) makes a route↔builder divergence actually fail.
    assert set(OBJECT_STATUSES) == {
        "pending", "verifying", "available", "deleted", "expired", "failed", "aborted",
        "purging",
    }
    assert set(OBJECT_STATUSES) == set(main.ObjectStatus.__args__)
    assert set(SORT_COLUMNS) == {"created_at", "display_name", "size_bytes"}


def test_status_aborted_is_an_accepted_filter_value():
    # Regression: ``aborted`` is a real terminal status (api/transfer.py, the
    # abort endpoint). The query vocabulary must accept it (200), unlike a value
    # outside the vocabulary which is refused with 422 (see the parameterized
    # test above). A 422 here would make aborted rows unfilterable.
    c, headers = _admin()
    oid = _upload(c, headers, f"aborted-{uuid.uuid4().hex[:8]}.bin")
    main.repo.set_status(oid, "aborted")

    r = c.get("/api/v1/objects", params={"status": "aborted"}, headers=headers)
    assert r.status_code == 200, r.text
    assert oid in _ids(r.json())


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------
def test_search_respects_owner_scoping():
    c, headers = _admin()
    tag = uuid.uuid4().hex[:8]
    admin_obj = _upload(c, headers, f"private-{tag}.txt")

    username = f"user-{uuid.uuid4().hex[:8]}"
    password = "user-password-123"
    main.repo.create_user(
        username, hash_password(password), role="user",
        space_id=main.repo.default_scope()[1],
    )
    u = TestClient(main.app)
    r = u.post("/api/v1/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    uh = {"X-CSRF-Token": r.json()["csrf_token"]}

    # The other user's search must not surface (or count) the admin's object.
    r = u.get("/api/v1/objects", params={"q": tag}, headers=uh)
    assert r.status_code == 200
    assert admin_obj not in _ids(r.json())
    assert r.json()["items"] == []
