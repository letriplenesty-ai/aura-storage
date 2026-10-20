"""Capacity guard: uploads reserve their declared size and are refused (507)
once the store would pass its high watermark. Off when no capacity is set."""
import os

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


@pytest.fixture
def capacity(monkeypatch):
    main.reset_login_attempts()
    authz.reset_m2m_attempts()
    monkeypatch.setattr(main.settings, "max_pending_intents", 10_000)

    def set_free(free_bytes: int):
        # Capacity chosen so that exactly `free_bytes` remain under a 90% mark.
        used = sum(main.repo.storage_commitment().values())
        limit = used + free_bytes
        monkeypatch.setattr(main.settings, "storage_high_watermark_pct", 90)
        monkeypatch.setattr(main.settings, "storage_capacity_bytes", -(-limit * 100 // 90))
    return set_free


def _admin():
    c = TestClient(main.app)
    r = c.post("/api/v1/login", json={
        "username": "admin", "password": os.environ["AURA_API_ADMIN_PASSWORD"],
    })
    return c, {"X-CSRF-Token": r.json()["csrf_token"]}


def _intent(c, headers, size, idem=None):
    hdrs = dict(headers)
    if idem:
        hdrs["Idempotency-Key"] = idem
    return c.post("/api/v1/objects/upload-intents",
                  json={"display_name": "big.bin", "size_bytes": size}, headers=hdrs)


def test_off_by_default():
    assert main.settings.storage_capacity_bytes == 0
    c, headers = _admin()
    assert _intent(c, headers, 5).status_code == 200
    assert c.get("/api/v1/admin/usage", headers=headers).json()["capacity"] is None


def test_each_intent_reserves_its_size_until_the_mark_is_reached(capacity):
    c, headers = _admin()
    capacity(100)
    assert _intent(c, headers, 60).status_code == 200
    # 60 reserved: 40 left, so 50 is refused but 40 still fits.
    r = _intent(c, headers, 50)
    assert r.status_code == 507 and r.json()["detail"] == "storage capacity reached"
    assert _intent(c, headers, 40).status_code == 200
    assert _intent(c, headers, 1).status_code == 507


def test_aborting_an_upload_frees_its_reservation(capacity, monkeypatch):
    class S3:
        def delete_object(self, Bucket, Key):
            pass
    monkeypatch.setattr(main, "s3ops", S3())
    c, headers = _admin()
    capacity(100)
    oid = _intent(c, headers, 100).json()["id"]
    assert _intent(c, headers, 1).status_code == 507
    assert c.post(f"/api/v1/objects/{oid}/abort", headers=headers).status_code == 200
    assert _intent(c, headers, 100).status_code == 200


def test_a_replayed_intent_is_not_refused_or_counted_twice(capacity):
    c, headers = _admin()
    capacity(100)
    first = _intent(c, headers, 100, idem="same-key")
    assert first.status_code == 200
    again = _intent(c, headers, 100, idem="same-key")
    assert again.status_code == 200 and again.json()["id"] == first.json()["id"]


def test_usage_reports_the_fill_level(capacity):
    c, headers = _admin()
    capacity(100)
    _intent(c, headers, 30)
    cap = c.get("/api/v1/admin/usage", headers=headers).json()["capacity"]
    assert cap["high_watermark_pct"] == 90
    assert cap["free_for_uploads_bytes"] == 70
    assert cap["reserved_bytes"] >= 30
