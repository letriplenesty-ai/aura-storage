"""Issue #18 tests. Offline by default; live S3 round-trip behind AURA_LIVE_S3=1."""
import hashlib
import json
import os
from urllib.parse import parse_qs, urlparse

import pytest

# Test credentials (offline-safe dummies unless live mode reuses the real .env).
os.environ.setdefault("AURA_API_ADMIN_PASSWORD", "test-admin-password")
os.environ.setdefault("AURA_S3_ACCESS_KEY", "testaccess12345678")
os.environ.setdefault("AURA_S3_SECRET_KEY", "testsecret45678901234567890123456789")
os.environ.setdefault("AURA_S3_ENDPOINT", "https://127.0.0.1:9")
os.environ.setdefault("AURA_API_DB", ":memory:")
os.environ.setdefault("AURA_COOKIE_SECURE", "0")
os.environ.setdefault("AURA_AUDIT_LOG", "/tmp/opencode/aura-test-audit.jsonl")

from fastapi.testclient import TestClient  # noqa: E402

import api.main as main  # noqa: E402

# NOTE: the SigV4 access key *ID* legitimately appears in presigned URLs
# (X-Amz-Credential); it is an identifier, not a secret. Only the secret
# key and the admin password must never appear.
# A marker shorter than MIN_MARKER_LEN cannot be asserted meaningfully: a short
# dummy (e.g. a 2-char password) also occurs by coincidence inside unrelated
# data, such as the access-key ID in a presigned credential.
MIN_MARKER_LEN = 8
SECRET_MARKERS = [
    marker
    for marker in (
        os.environ["AURA_S3_SECRET_KEY"],
        os.environ["AURA_API_ADMIN_PASSWORD"],
    )
    if len(marker) >= MIN_MARKER_LEN
]

client = TestClient(main.app)


def make_client():
    return TestClient(main.app)


def login(c, username="admin", password=None):
    password = password or os.environ["AURA_API_ADMIN_PASSWORD"]
    r = c.post("/api/v1/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["csrf_token"]


def authed_client(username="admin", password=None):
    password = password or os.environ["AURA_API_ADMIN_PASSWORD"]
    c = make_client()
    csrf = login(c, username, password)
    return c, {"X-CSRF-Token": csrf}


client = make_client()


@pytest.fixture(autouse=True)
def _reset_login_rate_limit():
    main.reset_login_attempts()
    yield
    main.reset_login_attempts()


def test_security_headers_present():
    r = client.get("/health/live")
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    assert r.headers.get("X-Frame-Options") == "DENY"


def test_login_rate_limit_429_after_window_exhausted():
    c = make_client()
    for _ in range(10):
        r = c.post("/api/v1/login", json={"username": "admin", "password": "wrong"})
        assert r.status_code == 401
    r = c.post("/api/v1/login", json={"username": "admin", "password": "wrong"})
    assert r.status_code == 429
    # #86: a rate-limited client is told when it may retry.
    assert r.headers.get("Retry-After") == str(main.settings.login_rate_window)


def test_successful_logins_do_not_consume_the_rate_limit():
    # Only failures count, so a burst of legitimate logins must not lock out the
    # client (regression for the review finding).
    c = make_client()
    password = os.environ["AURA_API_ADMIN_PASSWORD"]
    for _ in range(15):  # well above the limit (default 10)
        r = c.post("/api/v1/login", json={"username": "admin", "password": password})
        assert r.status_code == 200, r.text
    # The failure budget is still intact.
    assert c.post("/api/v1/login",
                  json={"username": "admin", "password": "wrong"}).status_code == 401


def test_successful_login_resets_failure_budget():
    c = make_client()
    password = os.environ["AURA_API_ADMIN_PASSWORD"]
    for _ in range(9):
        assert c.post("/api/v1/login",
                      json={"username": "admin", "password": "wrong"}).status_code == 401
    assert c.post("/api/v1/login",
                  json={"username": "admin", "password": password}).status_code == 200
    # 9 fresh failures are still allowed (the counter was cleared on success).
    for _ in range(9):
        assert c.post("/api/v1/login",
                      json={"username": "admin", "password": "wrong"}).status_code == 401


def test_audit_log_records_ids_and_no_secrets(tmp_path, monkeypatch):
    log = tmp_path / "audit.jsonl"
    monkeypatch.setenv("AURA_AUDIT_LOG", str(log))
    c = make_client()
    secret_pw = os.environ["AURA_API_ADMIN_PASSWORD"]
    c.post("/api/v1/login", json={"username": "admin", "password": "wrong"})
    csrf = login(c)
    c.post(
        "/api/v1/objects/upload-intents",
        json={"bucket": "aura", "display_name": "audit.txt", "size_bytes": 1},
        headers={"X-CSRF-Token": csrf},
    )
    lines = log.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) >= 3
    records = [json.loads(line) for line in lines]
    for rec in records:
        assert {"ts", "actor", "action", "result"} <= set(rec)
    actions = {(rec["action"], rec["result"]) for rec in records}
    assert ("login", "failure") in actions
    assert ("login", "success") in actions
    assert ("upload-intent", "success") in actions
    blob = log.read_text(encoding="utf-8")
    for marker in SECRET_MARKERS + [secret_pw]:
        assert marker not in blob, "secret material leaked in audit log"
    assert "X-Amz-Signature" not in blob
    assert "upload_url" not in blob
    assert "download_url" not in blob


def test_live_and_open_health():
    assert client.get("/health/live").json() == {"status": "live"}


def test_login_rejects_bad_password():
    r = client.post("/api/v1/login", json={"username": "admin", "password": "wrong"})
    assert r.status_code == 401


def test_verify_password_treats_corrupt_hash_as_no_match():
    # A malformed stored hash must yield False, never a 500.
    from api.auth import hash_password, verify_password

    good = hash_password("correct horse battery staple")
    assert verify_password(good, "correct horse battery staple") is True
    assert verify_password(good, "wrong password") is False
    for broken in ("", "not-a-hash", "$argon2id$broken", "$argon2id$v=19$m=65536,t=3,p=4$YWJj$YWJj"):
        assert verify_password(broken, "whatever") is False


def test_login_with_corrupt_stored_hash_is_401_not_500():
    import uuid as _uuid

    c = make_client()
    name = f"corrupt-{_uuid.uuid4().hex[:8]}"
    main.repo.create_user(name, "not-a-valid-hash")
    r = c.post("/api/v1/login", json={"username": name, "password": "anything-long"})
    assert r.status_code == 401, r.text


def test_objects_require_auth():
    assert client.get("/api/v1/objects").status_code == 401


def test_me_endpoint():
    from fastapi.testclient import TestClient as TC

    assert client.get("/api/v1/me").status_code == 401
    c2 = TC(main.app)
    login(c2)
    assert c2.get("/api/v1/me").json()["id"]


def test_me_exposes_the_signed_in_display_name():
    c = make_client()
    login(c)
    assert c.get("/api/v1/me").json()["name"] == "admin"


def test_me_exposes_csrf_token_recoverable_without_relogin():
    # Regression for "session expires all the time": the CSRF token was returned
    # only by POST /login and kept in the frontend's memory, so a refresh/new tab
    # lost it while the aura_session cookie stayed valid, breaking every mutation.
    # GET /me must hand the token back for a human session.
    c = make_client()
    csrf = login(c)
    me = c.get("/api/v1/me")
    assert me.status_code == 200
    token = me.json()["csrf_token"]
    assert token and token == csrf
    # The recovered token authorizes a real mutation (double-submit passes).
    name = f"csrf-recovered-{os.urandom(4).hex()}"
    ok = c.post("/api/v1/folders", json={"name": name},
                headers={"X-CSRF-Token": token})
    assert ok.status_code == 200, ok.text
    # And a bogus token is still refused, proving the check was not bypassed.
    bad = c.post("/api/v1/folders", json={"name": name + "-x"},
                 headers={"X-CSRF-Token": "bogus"})
    assert bad.status_code == 403


def test_me_csrf_token_is_null_for_service_principal():
    # Additive field, human-only: a service (Bearer) principal has no session,
    # so the token must be null rather than leaking another identity's value.
    from api.authz import SERVICE_SCOPES
    tag = os.urandom(4).hex()
    c, headers = authed_client()
    aid = c.post("/api/v1/admin/applications", json={"name": f"csrf-app-{tag}"},
                 headers=headers).json()["id"]
    sid = c.post("/api/v1/admin/spaces", json={"application_id": aid,
                 "name": f"csrf-space-{tag}", "environment": "test"},
                 headers=headers).json()["id"]
    token = c.post("/api/v1/admin/identities", json={"space_id": sid,
                  "name": f"csrf-svc-{tag}", "scopes": sorted(SERVICE_SCOPES)},
                  headers=headers).json()["token"]
    svc = make_client()
    me = svc.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["kind"] == "service"
    assert me.json()["csrf_token"] is None


def test_default_session_ttl_is_90_days():
    # The shipped default (no override) is 7776000s = 90 days. Skip if the
    # running environment deliberately overrides the TTL.
    if os.environ.get("AURA_SESSION_TTL"):
        pytest.skip("AURA_SESSION_TTL is overridden in this environment")
    assert main.settings.session_ttl == 7776000


def test_mutation_requires_csrf():
    c, _ = authed_client()
    r = c.post(
        "/api/v1/objects/upload-intents",
        json={"bucket": "aura", "display_name": "x.txt", "size_bytes": 1},
        headers={"X-CSRF-Token": ""},
    )
    assert r.status_code == 403
    r = c.post(
        "/api/v1/objects/upload-intents",
        json={"bucket": "aura", "display_name": "x.txt", "size_bytes": 1},
        headers={"X-CSRF-Token": "bogus"},
    )
    assert r.status_code == 403


def test_upload_intent_returns_scoped_url_and_leaks_nothing():
    c, headers = authed_client()
    r = c.post(
        "/api/v1/objects/upload-intents",
        json={"bucket": "aura", "display_name": "hello.txt",
              "content_type": "text/plain", "size_bytes": 4096},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"]
    assert body["upload_url"].startswith("https://")
    assert "X-Amz-Expires=" in body["upload_url"]
    blob = r.text
    for marker in SECRET_MARKERS:
        assert marker not in blob, "secret material leaked in response"
    # object is pending and invisible without auth
    assert make_client().get(f"/api/v1/objects/{body['id']}").status_code == 401
    got = c.get(f"/api/v1/objects/{body['id']}").json()
    assert got["status"] == "pending"


def test_upload_intent_binds_declared_size_as_signed_content_length():
    c, headers = authed_client()
    r = c.post(
        "/api/v1/objects/upload-intents",
        json={"bucket": "aura", "display_name": "sized.txt", "size_bytes": 1234},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["size_bytes"] == 1234
    # The declared size is part of the signed request, so the uploader cannot
    # send a different (larger) Content-Length than the API authorised (#40).
    signed = parse_qs(urlparse(body["upload_url"]).query)["X-Amz-SignedHeaders"][0]
    assert "content-length" in signed


def test_upload_intent_requires_a_declared_size():
    c, headers = authed_client()
    r = c.post(
        "/api/v1/objects/upload-intents",
        json={"bucket": "aura", "display_name": "nosize.txt"},
        headers=headers,
    )
    assert r.status_code == 422, r.text


def test_upload_intent_rejects_size_above_the_cap(monkeypatch):
    monkeypatch.setattr(main.settings, "max_object_bytes", 1024)
    c, headers = authed_client()
    r = c.post(
        "/api/v1/objects/upload-intents",
        json={"bucket": "aura", "display_name": "too-big.bin", "size_bytes": 4096},
        headers=headers,
    )
    assert r.status_code == 422, r.text


def test_soft_delete_and_purge(monkeypatch):
    # Purge deletes storage bytes first (M06): stub the S3 data plane offline.
    # Presigning is local crypto, but delete_object is a real call, so the
    # offline suite fakes a healthy backend; the failure path is covered in
    # tests/test_m06_isolation_matrix.py (502 + reference kept).
    class HealthyS3:
        def delete_object(self, Bucket, Key):
            return {}
    monkeypatch.setattr(main, "s3ops", HealthyS3())
    c, headers = authed_client()
    oid = c.post(
        "/api/v1/objects/upload-intents",
        json={"bucket": "aura", "display_name": "todelete.txt", "size_bytes": 1},
        headers=headers,
    ).json()["id"]
    r = c.delete(f"/api/v1/objects/{oid}", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "deleted"
    # deleted object is no longer downloadable
    r = c.post(f"/api/v1/objects/{oid}/download-intent", headers=headers)
    assert r.status_code == 409
    # purge removes it permanently
    r = c.post(f"/api/v1/objects/{oid}/purge", headers=headers)
    assert r.status_code == 200, r.text
    assert c.get(f"/api/v1/objects/{oid}").status_code == 404
    # purging twice is a clean 404
    assert c.post(f"/api/v1/objects/{oid}/purge", headers=headers).status_code == 404


def test_restore_brings_a_soft_deleted_object_back(monkeypatch):
    c, headers = authed_client()
    oid = c.post(
        "/api/v1/objects/upload-intents",
        json={"bucket": "aura", "display_name": "trash-me.txt", "size_bytes": 1},
        headers=headers,
    ).json()["id"]
    # An object that is not in the trash cannot be "restored".
    assert c.post(f"/api/v1/objects/{oid}/restore", headers=headers).status_code == 409

    # Simulate a committed object, then soft-delete and restore it.
    main.repo.mark_available(oid, 1, "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")
    assert c.delete(f"/api/v1/objects/{oid}", headers=headers).status_code == 200
    trash = c.get("/api/v1/objects?status=deleted", headers=headers).json()["items"]
    assert [i["id"] for i in trash] == [oid]

    r = c.post(f"/api/v1/objects/{oid}/restore", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "available"
    assert c.get("/api/v1/objects?status=deleted", headers=headers).json()["items"] == []
    # Restoring twice is refused: it is no longer in the trash.
    assert c.post(f"/api/v1/objects/{oid}/restore", headers=headers).status_code == 409


def test_restore_refuses_an_intent_deleted_before_its_upload():
    c, headers = authed_client()
    oid = c.post(
        "/api/v1/objects/upload-intents",
        json={"bucket": "aura", "display_name": "never-uploaded.txt", "size_bytes": 1},
        headers=headers,
    ).json()["id"]
    assert c.delete(f"/api/v1/objects/{oid}", headers=headers).status_code == 200
    r = c.post(f"/api/v1/objects/{oid}/restore", headers=headers)
    assert r.status_code == 409
    assert "never uploaded" in r.json()["detail"]


def test_reconcile_expires_stale_pending():
    from api.lifecycle import reconcile_pending

    c, headers = authed_client()
    oid = c.post(
        "/api/v1/objects/upload-intents",
        json={"bucket": "aura", "display_name": "stale.txt", "size_bytes": 1},
        headers=headers,
    ).json()["id"]
    # backdate the pending row past the grace period
    old = 7200
    if main.repo.dialect == "sqlite":
        main.repo.conn.execute(
            "UPDATE objects SET created_at = ? WHERE id = ?",
            (main.repo.now() - old, oid),
        )
        main.repo.conn.commit()
    else:  # psycopg connection: go through the repository helper
        main.repo._run(
            "UPDATE objects SET created_at = %s WHERE id = %s",
            (main.repo.now() - old, oid),
        )
    # fresh pending objects are untouched; only the stale one expires
    fresh = c.post(
        "/api/v1/objects/upload-intents",
        json={"bucket": "aura", "display_name": "fresh.txt", "size_bytes": 1},
        headers=headers,
    ).json()["id"]
    # stub storage (offline): reconcile must not require real S3
    class DummyS3:
        def __init__(self):
            self.deleted = []

        def delete_object(self, Bucket, Key):
            self.deleted.append((Bucket, Key))

    out = reconcile_pending(main.repo, DummyS3(), expire_after_s=3600)
    assert out == {"expired": 1, "storage_deletes_attempted": 1, "storage_failures": 0}
    assert c.get(f"/api/v1/objects/{oid}").json()["status"] == "expired"
    assert c.get(f"/api/v1/objects/{fresh}").json()["status"] == "pending"


def test_cross_owner_isolation():
    c, headers = authed_client()
    oid = c.post(
        "/api/v1/objects/upload-intents",
        json={"bucket": "aura", "display_name": "private.txt", "size_bytes": 1},
        headers=headers,
    ).json()["id"]
    # second user, created directly (unique per run: PG persists across runs)
    import uuid as _uuid

    from api.auth import hash_password
    bob_name = f"bob-{_uuid.uuid4().hex[:8]}"
    main.repo.create_user(bob_name, hash_password("bob-pass"))
    bob, _ = authed_client(bob_name, "bob-pass")
    assert bob.get(f"/api/v1/objects/{oid}").status_code == 404
    assert bob.get("/api/v1/objects").json()["items"] == []


LIVE = os.environ.get("AURA_LIVE_S3") == "1"


@pytest.mark.skipif(not LIVE, reason="needs real SeaweedFS (AURA_LIVE_S3=1)")
def test_live_round_trip_with_integrity():
    import httpx

    c, headers = authed_client()
    bucket = os.environ.get("AURA_TEST_BUCKET", "aura-api-test")
    try:
        main.s3.create_bucket(Bucket=bucket)
    except Exception:
        pass
    payload = b"AURA live round-trip %s" % os.urandom(16)
    want = hashlib.sha256(payload).hexdigest()
    intent = c.post(
        "/api/v1/objects/upload-intents",
        json={"bucket": bucket, "display_name": "live.bin",
              "content_type": "application/octet-stream", "size_bytes": len(payload)},
        headers=headers,
    ).json()
    put = httpx.put(
        intent["upload_url"], content=payload,
        headers={"Content-Type": "application/octet-stream"}, timeout=30,
    )
    assert put.status_code == 200, put.text
    committed = c.post(
        f"/api/v1/objects/{intent['id']}/commit",
        headers=headers,
    ).json()
    assert committed["status"] == "available"
    assert committed["checksum_sha256"] == want
    dl = c.post(
        f"/api/v1/objects/{intent['id']}/download-intent",
        headers=headers,
    ).json()
    assert "X-Amz-Expires=" in dl["download_url"]
    get = httpx.get(dl["download_url"], timeout=30)
    assert get.status_code == 200
    assert hashlib.sha256(get.content).hexdigest() == want
    # expired/tampered session is rejected
    bad = make_client().get(
        "/api/v1/objects", headers={"Cookie": "aura_session=bogus"}
    )
    assert bad.status_code == 401
    # a presigned URL past its TTL is rejected by storage (real expiry proof)
    import time as _time

    from api.s3 import presigned_put as _presign
    short_url = _presign(main.s3, bucket, "expiry-probe", "text/plain", 2, 1)
    _time.sleep(4)
    expired = httpx.put(short_url, content=b"x", headers={"Content-Type": "text/plain"}, timeout=30)
    assert expired.status_code == 403, expired.text


def test_default_list_hides_soft_deleted_objects():
    c, headers = authed_client()
    oid = c.post(
        "/api/v1/objects/upload-intents",
        json={"bucket": "aura", "display_name": "vanish.txt", "size_bytes": 1},
        headers=headers,
    ).json()["id"]
    assert oid in {o["id"] for o in c.get("/api/v1/objects").json()["items"]}
    assert c.delete(f"/api/v1/objects/{oid}", headers=headers).status_code == 200
    # The file visibly disappears from the default listing...
    assert oid not in {o["id"] for o in c.get("/api/v1/objects").json()["items"]}
    # ...but stays reachable explicitly (future trash view) until purged.
    assert c.get(f"/api/v1/objects/{oid}").json()["status"] == "deleted"
    deleted = c.get("/api/v1/objects", params={"status": "deleted"}).json()["items"]
    assert oid in {o["id"] for o in deleted}
