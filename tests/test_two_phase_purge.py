"""Two-phase purge (api/lifecycle.py): a crash never leaves the catalogue lying.

The row is marked 'purging' before the bytes are deleted; a failed delete puts
it back; a row left 'purging' by a dead process is finished by resume_purges.
"""
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
from api.lifecycle import purge_object, resume_purges  # noqa: E402

DIGEST = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


class Store:
    def __init__(self):
        self.deleted: list[str] = []
        self.down = False
        self.crash_after_delete = False

    def delete_object(self, Bucket, Key):
        if self.down:
            raise ConnectionError("storage down")
        self.deleted.append(Key)
        if self.crash_after_delete:
            raise SystemExit("process killed between the bytes and the row")


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


def _available(c, headers):
    oid = c.post("/api/v1/objects/upload-intents",
                 json={"display_name": "p.bin", "size_bytes": 1}, headers=headers).json()["id"]
    main.repo.mark_available(oid, 1, DIGEST)
    row = _sql("SELECT space_id FROM objects WHERE id = ?", (oid,))
    return oid, row["space_id"]


def _admin():
    c = TestClient(main.app)
    r = c.post("/api/v1/login", json={
        "username": "admin", "password": os.environ["AURA_API_ADMIN_PASSWORD"],
    })
    return c, {"X-CSRF-Token": r.json()["csrf_token"]}


def _status(oid):
    row = _sql("SELECT status FROM objects WHERE id = ?", (oid,))
    return row["status"] if row else None


def test_a_successful_purge_removes_bytes_then_row(store):
    c, headers = _admin()
    oid, _ = _available(c, headers)
    assert c.post(f"/api/v1/objects/{oid}/purge", headers=headers).status_code == 200
    assert _status(oid) is None and len(store.deleted) == 1


def test_a_failed_storage_delete_puts_the_row_back(store):
    c, headers = _admin()
    oid, _ = _available(c, headers)
    store.down = True
    assert c.post(f"/api/v1/objects/{oid}/purge", headers=headers).status_code == 502
    assert _status(oid) == "available"


def test_a_crash_between_bytes_and_row_is_finished_by_the_sweep(store):
    c, headers = _admin()
    oid, space = _available(c, headers)
    store.crash_after_delete = True
    with pytest.raises(SystemExit):
        purge_object(main.repo, store, oid, space_id=space)
    # The row no longer claims the object is available...
    assert _status(oid) == "purging"
    # ...it is hidden from the listing and cannot be downloaded.
    ids = [o["id"] for o in c.get("/api/v1/objects", headers=headers).json()["items"]]
    assert oid not in ids
    assert c.post(f"/api/v1/objects/{oid}/download-intent", headers=headers).status_code == 409

    store.crash_after_delete = False
    # Too recent for the sweep: a live request may still own it.
    assert resume_purges(main.repo, store)["purges_completed"] == 0
    later = time.time() + 3600
    out = resume_purges(main.repo, store, now=lambda: later)
    assert out["purges_completed"] >= 1
    assert _status(oid) is None


def test_the_sweep_keeps_the_row_while_storage_is_down(store):
    c, headers = _admin()
    oid, _ = _available(c, headers)
    _sql("UPDATE objects SET status = 'purging', updated_at = ? WHERE id = ?", (1, oid))
    store.down = True
    out = resume_purges(main.repo, store)
    assert out["purge_failures"] >= 1 and _status(oid) == "purging"
    store.down = False
    assert resume_purges(main.repo, store)["purges_completed"] >= 1
    assert _status(oid) is None


def test_reconcile_also_finishes_interrupted_purges(store):
    c, headers = _admin()
    oid, _ = _available(c, headers)
    _sql("UPDATE objects SET status = 'purging', updated_at = ? WHERE id = ?", (1, oid))
    out = c.post("/api/v1/admin/reconcile", headers=headers).json()
    assert out["purges_completed"] >= 1 and _status(oid) is None
