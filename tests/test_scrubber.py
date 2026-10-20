"""Periodic integrity scrub (api/scrubber.py), offline.

Objects are created through the real API, the storage is an in-process fake
whose bytes the tests can corrupt or remove; the scrubber is driven directly.
"""
import hashlib
import os
import time

import pytest

os.environ.setdefault("AURA_API_ADMIN_PASSWORD", "test-admin-password")
os.environ.setdefault("AURA_S3_ACCESS_KEY", "testaccess12345678")
os.environ.setdefault("AURA_S3_SECRET_KEY", "testsecret45678901234567890123456789")
os.environ.setdefault("AURA_S3_ENDPOINT", "https://127.0.0.1:9")
os.environ.setdefault("AURA_API_DB", ":memory:")
os.environ.setdefault("AURA_COOKIE_SECURE", "0")

from fastapi.testclient import TestClient  # noqa: E402

import api.main as main  # noqa: E402
from api import authz  # noqa: E402
from api.scrubber import scrub_one, scrub_run  # noqa: E402


class NotFound(Exception):
    def __init__(self):
        super().__init__("NoSuchKey")
        self.response = {"Error": {"Code": "NoSuchKey"}}


class Body:
    def __init__(self, data):
        self.data = data

    def iter_chunks(self, chunk_size=1024 * 1024):
        for i in range(0, len(self.data), chunk_size):
            yield self.data[i:i + chunk_size]


class Store:
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.down = False
        self.reads = 0

    def head_object(self, Bucket, Key):
        return {"ContentLength": len(self.objects[Key]), "ServerSideEncryption": "AES256"}

    def get_object(self, Bucket, Key):
        if self.down:
            raise ConnectionError("storage down")
        if Key not in self.objects:
            raise NotFound()
        self.reads += 1
        return {"Body": Body(self.objects[Key])}

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)


def _sql(sql: str, params: tuple = ()):
    if main.repo.dialect == "sqlite":
        row = main.repo.conn.execute(sql, params).fetchone()
        main.repo.conn.commit()
        return row
    cur = main.repo.conn.execute(sql.replace("?", "%s"), params)
    return cur.fetchone() if cur.description else None


@pytest.fixture
def store(monkeypatch):
    main.reset_login_attempts()
    authz.reset_m2m_attempts()
    s = Store()
    monkeypatch.setattr(main, "s3ops", s)
    monkeypatch.setattr(main.settings, "verify_async_threshold_bytes", 10 ** 12)
    # Every object already in the catalogue counts as freshly checked, so each
    # test only sees the objects it creates.
    _sql("UPDATE objects SET scrubbed_at = ?, scrub_result = 'ok' WHERE status = 'available'",
         (int(time.time()),))
    return s


def _admin():
    c = TestClient(main.app)
    r = c.post("/api/v1/login", json={
        "username": "admin", "password": os.environ["AURA_API_ADMIN_PASSWORD"],
    })
    return c, {"X-CSRF-Token": r.json()["csrf_token"]}


def _stored(store, c, headers, data: bytes) -> tuple[str, str]:
    r = c.post("/api/v1/objects/upload-intents",
               json={"display_name": "orig.pdf", "size_bytes": len(data)}, headers=headers)
    oid = r.json()["id"]
    key = _sql("SELECT object_key FROM objects WHERE id = ?", (oid,))["object_key"]
    store.objects[key] = data
    assert c.post(f"/api/v1/objects/{oid}/commit", headers=headers).status_code == 200
    return oid, key


def _result(oid):
    return _sql("SELECT scrub_result FROM objects WHERE id = ?", (oid,))["scrub_result"]


def test_intact_objects_are_marked_ok_and_not_rechecked_before_the_interval(store):
    c, headers = _admin()
    oid, _ = _stored(store, c, headers, b"intact bytes")
    assert scrub_one(main.repo, store, main.settings) == ("ok", 12)
    assert _result(oid) == "ok"
    # Checked just now: not due again until the interval has passed.
    assert scrub_one(main.repo, store, main.settings) is None
    later = time.time() + main.settings.scrub_interval_days * 86400 + 60
    # Keep objects left by other test modules out of this later pass.
    _sql("UPDATE objects SET scrubbed_at = ? WHERE status = 'available' AND id <> ?",
         (int(later), oid))
    assert scrub_one(main.repo, store, main.settings, now=lambda: later)[0] == "ok"


def test_silently_corrupted_bytes_are_reported_not_changed(store):
    c, headers = _admin()
    oid, key = _stored(store, c, headers, b"original contents")
    store.objects[key] = b"original c0ntents"  # bit rot, same size
    assert scrub_one(main.repo, store, main.settings)[0] == "mismatch"
    # Read-only: the object stays available, its digest untouched.
    obj = c.get(f"/api/v1/objects/{oid}").json()
    assert obj["status"] == "available"
    assert obj["checksum_sha256"] == hashlib.sha256(b"original contents").hexdigest()

    report = c.get("/api/v1/admin/integrity", headers=headers).json()
    assert [p["id"] for p in report["problems"]] == [oid]
    assert report["results"]["mismatch"]["count"] == 1


def test_a_lost_object_is_reported_missing(store):
    c, headers = _admin()
    oid, key = _stored(store, c, headers, b"will vanish")
    del store.objects[key]
    assert scrub_one(main.repo, store, main.settings)[0] == "missing"
    assert _result(oid) == "missing"


def test_a_storage_outage_is_not_a_finding(store):
    c, headers = _admin()
    oid, _ = _stored(store, c, headers, b"still fine")
    store.down = True
    assert scrub_run(main.repo, store, main.settings)["error"] == 1
    assert _result(oid) is None  # unchecked, retried next run
    store.down = False
    assert scrub_run(main.repo, store, main.settings)["ok"] == 1
    assert _result(oid) == "ok"


def test_a_run_stops_at_its_byte_budget(store, monkeypatch):
    monkeypatch.setattr(main.settings, "scrub_bytes_per_run", 15)
    c, headers = _admin()
    for i in range(4):
        _stored(store, c, headers, b"0123456789" + bytes([i]))  # 11 bytes each
    out = scrub_run(main.repo, store, main.settings)
    assert out["ok"] == 2 and out["bytes"] == 22  # stops once 15 is passed
    assert scrub_run(main.repo, store, main.settings)["ok"] == 2


def test_never_checked_objects_go_first(store):
    c, headers = _admin()
    old, _ = _stored(store, c, headers, b"checked long ago")
    _sql("UPDATE objects SET scrubbed_at = ?, scrub_result = 'ok' WHERE id = ?", (1, old))
    new, _ = _stored(store, c, headers, b"never checked")
    scrub_one(main.repo, store, main.settings)
    assert _result(new) == "ok"
    assert _sql("SELECT scrubbed_at FROM objects WHERE id = ?", (old,))["scrubbed_at"] == 1


def test_two_scrubbers_never_take_the_same_object(store):
    c, headers = _admin()
    oid, _ = _stored(store, c, headers, b"only once")
    now = int(time.time())
    # checked_before=0: only never-checked rows qualify, so objects left by
    # other tests (marked checked a second ago) cannot become "due" when the
    # clock ticks over between the fixture and this call.
    assert main.repo.claim_scrub(now, now + 600, 0)["id"] == oid
    assert main.repo.claim_scrub(now, now + 600, 0) is None


def test_admin_routes_need_admin(store):
    anon = TestClient(main.app)
    assert anon.get("/api/v1/admin/integrity").status_code in (401, 403)
    c, headers = _admin()
    _stored(store, c, headers, b"abc")
    out = c.post("/api/v1/admin/integrity/scrub", headers=headers).json()
    assert out["ok"] == 1 and out["bytes"] == 3
