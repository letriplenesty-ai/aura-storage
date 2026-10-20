"""Storage -> catalogue reconciliation (api/storage_reconcile.py), offline.

The fake storage lists keys and multipart uploads like S3 (sorted, StartAfter,
truncation); catalogue rows come from the real API.
"""
import os
import time
from datetime import datetime, timezone

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
from api.storage_reconcile import scan_bucket  # noqa: E402

OLD = datetime(2020, 1, 1, tzinfo=timezone.utc)


class Store:
    def __init__(self):
        self.objects: dict[str, tuple[bytes, datetime]] = {}
        self.uploads: list[dict] = []
        self.aborted: list[str] = []

    def put(self, key, data=b"x", when=OLD):
        self.objects[key] = (data, when)

    def list_objects_v2(self, Bucket, Prefix, MaxKeys, StartAfter=None):
        keys = sorted(k for k in self.objects if k.startswith(Prefix)
                      and (StartAfter is None or k > StartAfter))
        page = keys[:MaxKeys]
        return {
            "Contents": [{"Key": k, "Size": len(self.objects[k][0]),
                          "LastModified": self.objects[k][1]} for k in page],
            "IsTruncated": len(keys) > len(page),
        }

    def list_multipart_uploads(self, Bucket, Prefix, MaxUploads):
        return {"Uploads": list(self.uploads)}

    def head_object(self, Bucket, Key):
        data, when = self.objects[Key]
        return {"ContentLength": len(data), "LastModified": when,
                "ServerSideEncryption": "AES256"}

    def get_object(self, Bucket, Key):
        raise AssertionError("never read")

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)

    def abort_multipart_upload(self, Bucket, Key, UploadId):
        self.aborted.append(UploadId)


def _sql(sql, params=()):
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
    return s


def _admin():
    c = TestClient(main.app)
    r = c.post("/api/v1/login", json={
        "username": "admin", "password": os.environ["AURA_API_ADMIN_PASSWORD"],
    })
    return c, {"X-CSRF-Token": r.json()["csrf_token"]}


def _row(c, headers, name="f.bin"):
    oid = c.post("/api/v1/objects/upload-intents",
                 json={"display_name": name, "size_bytes": 1}, headers=headers).json()["id"]
    row = _sql("SELECT bucket, object_key FROM objects WHERE id = ?", (oid,))
    return oid, row["bucket"], row["object_key"]


def _report(c, headers, bucket, **params):
    r = c.get("/api/v1/admin/storage-orphans", params={"bucket": bucket, **params},
              headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["buckets"][0]


def test_keys_without_a_row_are_reported_and_known_ones_are_not(store):
    c, headers = _admin()
    oid, bucket, key = _row(c, headers)
    store.put(key)                                   # accounted for (pending)
    store.put("aura/space/actor/stray-1", b"12345")  # no row
    report = _report(c, headers, bucket)
    assert [(o["key"], o["reason"]) for o in report["orphans"]] == [
        ("aura/space/actor/stray-1", "no_row"),
    ]
    assert report["orphan_bytes"] == 5


def test_bytes_of_aborted_rows_are_orphans_but_the_trash_is_not(store):
    c, headers = _admin()
    aborted, bucket, k1 = _row(c, headers, "aborted.bin")
    trashed, _, k2 = _row(c, headers, "trashed.bin")
    store.put(k1)
    store.put(k2)
    _sql("UPDATE objects SET status = 'aborted' WHERE id = ?", (aborted,))
    _sql("UPDATE objects SET status = 'deleted' WHERE id = ?", (trashed,))
    report = _report(c, headers, bucket)
    found = {o["key"]: o for o in report["orphans"]}
    assert found[k1]["reason"] == "terminal" and found[k1]["object_id"] == aborted
    assert k2 not in found


def test_recent_bytes_are_never_reported(store):
    c, headers = _admin()
    _, bucket, _ = _row(c, headers)
    store.put("aura/x/y/in-flight", when=datetime.now(timezone.utc))
    assert _report(c, headers, bucket)["orphans"] == []


def test_the_scan_is_bounded_and_resumable(store):
    c, headers = _admin()
    _, bucket, _ = _row(c, headers)
    for i in range(5):
        store.put(f"aura/s/a/stray-{i}")
    first = _report(c, headers, bucket, max_keys=3)
    assert first["scanned"] == 3 and first["next_start_after"]
    second = _report(c, headers, bucket, max_keys=3, start_after=first["next_start_after"])
    keys = [o["key"] for o in first["orphans"] + second["orphans"]]
    assert sorted(keys) == [f"aura/s/a/stray-{i}" for i in range(5)]


def test_stale_multipart_uploads_without_an_owner_are_reported(store):
    c, headers = _admin()
    oid, bucket, key = _row(c, headers)
    _sql("UPDATE objects SET multipart_upload_id = 'live' WHERE id = ?", (oid,))
    store.uploads = [
        {"Key": key, "UploadId": "live", "Initiated": OLD},
        {"Key": key, "UploadId": "stale", "Initiated": OLD},
        {"Key": "aura/z/z/gone", "UploadId": "orphan", "Initiated": OLD},
    ]
    report = _report(c, headers, bucket)
    assert sorted(u["upload_id"] for u in report["stale_uploads"]) == ["orphan", "stale"]


def test_cleanup_rechecks_every_key_before_deleting(store):
    c, headers = _admin()
    oid, bucket, key = _row(c, headers)
    store.put(key)
    store.put("aura/s/a/stray")
    store.put("aura/s/a/young", when=datetime.now(timezone.utc))
    store.put("other/not-ours")
    r = c.post("/api/v1/admin/storage-orphans/cleanup", headers=headers, json={
        "bucket": bucket,
        "keys": ["aura/s/a/stray", key, "aura/s/a/young", "other/not-ours", "aura/s/a/absent"],
    })
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["removed"] == ["aura/s/a/stray"]
    reasons = {s["key"]: s["reason"] for s in out["skipped"]}
    assert reasons[key] == "now accounted for"
    assert reasons["aura/s/a/young"] == "too recent"
    assert reasons["other/not-ours"] == "outside the AURA prefix"
    assert "aura/s/a/absent" in reasons
    assert key in store.objects and "aura/s/a/young" in store.objects


def test_cleanup_never_aborts_an_active_upload(store):
    c, headers = _admin()
    oid, bucket, key = _row(c, headers)
    _sql("UPDATE objects SET multipart_upload_id = 'live' WHERE id = ?", (oid,))
    r = c.post("/api/v1/admin/storage-orphans/cleanup", headers=headers, json={
        "bucket": bucket,
        "uploads": [{"key": key, "upload_id": "live"}, {"key": key, "upload_id": "stale"}],
    })
    assert store.aborted == ["stale"]
    assert r.json()["skipped"][0]["reason"] == "upload still active"


def test_unregistered_buckets_and_non_admins_are_refused(store):
    c, headers = _admin()
    assert c.get("/api/v1/admin/storage-orphans", params={"bucket": "nope"},
                 headers=headers).status_code == 404
    assert c.post("/api/v1/admin/storage-orphans/cleanup", headers=headers,
                  json={"bucket": "nope", "keys": ["aura/x"]}).status_code == 404
    anon = TestClient(main.app)
    assert anon.get("/api/v1/admin/storage-orphans").status_code in (401, 403)


def test_a_naive_last_modified_is_read_as_utc(store):
    store.put("aura/n/n/naive", when=datetime(2020, 1, 1))
    names = [b["name"] for b in main.repo.list_all_buckets()]
    report = scan_bucket(main.repo, store, names[0], grace_s=60, max_keys=10)
    assert any(o["key"] == "aura/n/n/naive" for o in report["orphans"])
