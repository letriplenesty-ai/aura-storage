"""M06 — authorization and tenant-isolation matrix.

Negative tests for every sensitive operation across the caller matrix:
authorized owner / other application / other space / unauthenticated /
authenticated without permission / revoked identity / insufficient scope.

Offline (SQLite catalogue, stubbed S3 ops where a real storage call would be
made). Covers list, read, download, upload-intent, commit, soft-delete, purge
and the admin routes, plus the M06 lifecycle regressions: a storage delete
failure must not drop the catalogue reference (purge -> 502), and reconcile
must not report a failed cleanup as expired.
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
from api.config import settings  # noqa: E402
from api.lifecycle import reconcile_pending  # noqa: E402


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


def _login(username, password):
    c = TestClient(main.app)
    r = c.post("/api/v1/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return c, {"X-CSRF-Token": r.json()["csrf_token"]}


def _make_app_space(c, headers):
    suffix = uuid.uuid4().hex[:8]
    r = c.post("/api/v1/admin/applications", json={"name": f"m06-app-{suffix}"},
               headers=headers)
    assert r.status_code == 200, r.text
    app_id = r.json()["id"]
    r = c.post("/api/v1/admin/spaces", json={
        "application_id": app_id, "name": f"m06-space-{suffix}",
        "environment": "test",
    }, headers=headers)
    assert r.status_code == 200, r.text
    return app_id, r.json()["id"], r.json()["bucket"]


def _make_identity(c, headers, space_id, scopes, name="m06-svc"):
    r = c.post("/api/v1/admin/identities", json={
        "space_id": space_id, "name": f"{name}-{uuid.uuid4().hex[:6]}",
        "scopes": scopes,
    }, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _svc(token):
    return {"Authorization": f"Bearer {token}"}


def _user_with_role(role):
    """Human user bound to the default space (sees only its own objects)."""
    name = f"m06-{role}-{uuid.uuid4().hex[:8]}"
    password = "m06-password-123"
    app_id, space_id = main.repo.default_scope()
    main.repo.create_user(name, hash_password(password), role=role,
                         space_id=space_id, application_id=app_id)
    return name, password


def _make_available(c, headers, **intent):
    """Upload intent + synchronous catalogue commit (no S3 bytes offline)."""
    body = {"display_name": "m06.bin", "size_bytes": 1}
    body.update(intent)
    r = c.post("/api/v1/objects/upload-intents", json=body, headers=headers)
    assert r.status_code == 200, r.text
    oid = r.json()["id"]
    main.repo.mark_available(oid, 1, "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")
    return oid


FULL = ["objects:read", "objects:write", "objects:delete", "objects:purge"]


# ---------------------------------------------------------------------------
# Owner: the authorized path works (matrix baseline).
# ---------------------------------------------------------------------------
def test_m06_owner_allowed():
    c, headers = _admin()
    _, space, _ = _make_app_space(c, headers)
    token = _make_identity(c, headers, space, FULL)["token"]
    h = _svc(token)

    intent = c.post("/api/v1/objects/upload-intents",
                    json={"display_name": "ok.txt", "size_bytes": 1},
                    headers=h)
    assert intent.status_code == 200, intent.text
    body = intent.json()
    assert body["expires_in"] == settings.intent_ttl
    assert body["method"] == "PUT"
    assert body["upload_url"]
    # The server chooses storage coordinates: none are echoed to the client.
    assert "bucket" not in body and "object_key" not in body
    oid = body["id"]

    assert c.get(f"/api/v1/objects/{oid}", headers=h).status_code == 200
    assert oid in {o["id"] for o in
                   c.get("/api/v1/objects", headers=h).json()["items"]}
    assert c.delete(f"/api/v1/objects/{oid}", headers=h).status_code == 200


def test_m06_human_owner_folders_allowed():
    name, password = _user_with_role("user")
    c, h = _login(name, password)
    fid = c.post("/api/v1/folders", json={"name": "mine"},
                 headers=h).json()["id"]
    assert c.get(f"/api/v1/folders/{fid}", headers=h).status_code == 200
    oid = _make_available(c, h)
    assert c.patch(f"/api/v1/objects/{oid}", json={"folder_id": fid},
                   headers=h).status_code == 200


# ---------------------------------------------------------------------------
# Other application / other space: invisible with any verb, no leak.
# ---------------------------------------------------------------------------
def test_m06_other_space_and_application_are_isolated():
    c, headers = _admin()
    _, space_a, _ = _make_app_space(c, headers)
    _, space_b, _ = _make_app_space(c, headers)
    tok_a = _make_identity(c, headers, space_a, FULL)["token"]
    tok_b = _make_identity(c, headers, space_b, FULL)["token"]

    oid = _make_available(c, _svc(tok_a))
    hb = _svc(tok_b)

    assert c.get("/api/v1/objects", headers=hb).json()["items"] == []
    for method, path, kwargs in [
        ("get", f"/api/v1/objects/{oid}", {}),
        ("post", f"/api/v1/objects/{oid}/commit", {}),
        ("post", f"/api/v1/objects/{oid}/abort", {}),
        ("post", f"/api/v1/objects/{oid}/download-intent", {}),
        ("post", f"/api/v1/objects/{oid}/preview-intent", {}),
        ("post", f"/api/v1/objects/{oid}/purge", {}),
        ("post", f"/api/v1/objects/{oid}/restore", {}),
        ("delete", f"/api/v1/objects/{oid}", {}),
    ]:
        r = c.request(method, path, headers=hb, **kwargs)
        assert r.status_code == 404, (method, path, r.status_code)

    # Generic 404: no existence oracle, no storage coordinates.
    r = c.get(f"/api/v1/objects/{oid}", headers=hb)
    assert r.json() == {"detail": "object not found"}
    assert "bucket" not in r.text and "object_key" not in r.text


def test_m06_human_cannot_reach_other_owner_object():
    name_a, pwd_a = _user_with_role("user")
    name_b, pwd_b = _user_with_role("user")
    ca, ha = _login(name_a, pwd_a)
    cb, hb = _login(name_b, pwd_b)
    oid = _make_available(ca, ha)
    assert cb.get(f"/api/v1/objects/{oid}", headers=hb).status_code == 404
    assert all(o["id"] != oid
               for o in cb.get("/api/v1/objects", headers=hb).json()["items"])
    # ... nor their folders.
    fid = ca.post("/api/v1/folders", json={"name": "priv"},
                  headers=ha).json()["id"]
    assert cb.get(f"/api/v1/folders/{fid}", headers=hb).status_code == 404


def test_m06_forged_bucket_and_folder_are_refused():
    c, headers = _admin()
    _, space_a, _ = _make_app_space(c, headers)
    _, space_b, bucket_b = _make_app_space(c, headers)
    tok_a = _make_identity(c, headers, space_a, FULL)["token"]

    # A's caller cannot upload into B's bucket, even by naming it explicitly.
    r = c.post("/api/v1/objects/upload-intents",
               json={"bucket": bucket_b, "display_name": "x", "size_bytes": 1},
               headers=_svc(tok_a))
    assert r.status_code == 403

    # A service identity owns no folder: any folder_id is refused.
    name, password = _user_with_role("user")
    owner, ho = _login(name, password)
    fid = owner.post("/api/v1/folders", json={"name": "svc-target"},
                     headers=ho).json()["id"]
    r = c.post("/api/v1/objects/upload-intents",
               json={"display_name": "x", "size_bytes": 1, "folder_id": fid},
               headers=_svc(tok_a))
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Unauthenticated callers.
# ---------------------------------------------------------------------------
def test_m06_unauthenticated_is_denied():
    c, headers = _admin()
    _, space, _ = _make_app_space(c, headers)
    oid = _make_available(c, _svc(_make_identity(c, headers, space, FULL)["token"]))
    name, password = _user_with_role("user")
    owner, ho = _login(name, password)
    fid = owner.post("/api/v1/folders", json={"name": "u"},
                     headers=ho).json()["id"]

    # Fresh client: `c` still carries the admin session cookie, so anonymity
    # requires a client that never logged in.
    anon = TestClient(main.app)
    anonymous = [
        ("get", "/api/v1/me", {}),
        ("get", "/api/v1/objects", {}),
        ("get", f"/api/v1/objects/{oid}", {}),
        ("post", "/api/v1/objects/upload-intents",
         {"json": {"display_name": "x", "size_bytes": 1}}),
        ("post", f"/api/v1/objects/{oid}/commit", {}),
        ("post", f"/api/v1/objects/{oid}/abort", {}),
        ("post", f"/api/v1/objects/{oid}/download-intent", {}),
        ("post", f"/api/v1/objects/{oid}/preview-intent", {}),
        ("post", f"/api/v1/objects/{oid}/purge", {}),
        ("post", f"/api/v1/objects/{oid}/restore", {}),
        ("delete", f"/api/v1/objects/{oid}", {}),
        ("patch", f"/api/v1/objects/{oid}", {"json": {"folder_id": None}}),
        ("get", "/api/v1/folders", {}),
        ("get", f"/api/v1/folders/{fid}", {}),
        ("post", "/api/v1/folders", {"json": {"name": "x"}}),
        ("post", "/api/v1/logout", {}),
        ("get", "/api/v1/admin/stats", {}),
        ("get", "/api/v1/admin/applications", {}),
        ("post", "/api/v1/admin/reconcile", {}),
    ]
    for method, path, kwargs in anonymous:
        r = anon.request(method, path, **kwargs)
        assert r.status_code in (401, 403), (method, path, r.status_code)


# ---------------------------------------------------------------------------
# Authenticated without permission / with insufficient scope.
# ---------------------------------------------------------------------------
def test_m06_authenticated_without_permission_is_denied():
    c, headers = _admin()
    _, space, _ = _make_app_space(c, headers)
    oid = _make_available(c, _svc(_make_identity(c, headers, space, FULL)["token"]))

    # Plain human role: data plane ok, platform administration refused.
    name, password = _user_with_role("user")
    u, uh = _login(name, password)
    assert u.get("/api/v1/admin/stats", headers=uh).status_code == 403
    assert u.get("/api/v1/admin/applications", headers=uh).status_code == 403
    assert u.post("/api/v1/admin/reconcile", headers=uh).status_code == 403
    assert u.post("/api/v1/admin/applications", json={"name": "nope"},
                  headers=uh).status_code == 403
    assert u.post(f"/api/v1/objects/{oid}/purge", headers=uh).status_code == 403

    # Operator role: read-only administration, still no platform writes.
    oname, opwd = _user_with_role("operator")
    o, oh = _login(oname, opwd)
    assert o.get("/api/v1/admin/stats", headers=oh).status_code == 200
    assert o.post("/api/v1/admin/applications", json={"name": "nope"},
                  headers=oh).status_code == 403

    # Read-only service identity: reads pass, every write is refused.
    ro = _make_identity(c, headers, space, ["objects:read"])["token"]
    rh = _svc(ro)
    assert c.get("/api/v1/objects", headers=rh).status_code == 200
    assert c.get(f"/api/v1/objects/{oid}", headers=rh).status_code == 200
    assert c.post("/api/v1/objects/upload-intents",
                  json={"display_name": "x", "size_bytes": 1},
                  headers=rh).status_code == 403
    assert c.post(f"/api/v1/objects/{oid}/commit", headers=rh).status_code == 403
    assert c.delete(f"/api/v1/objects/{oid}", headers=rh).status_code == 403
    assert c.post(f"/api/v1/objects/{oid}/purge", headers=rh).status_code == 403
    assert c.get("/api/v1/admin/stats", headers=rh).status_code == 403
    assert c.post("/api/v1/admin/reconcile", headers=rh).status_code == 403


# ---------------------------------------------------------------------------
# Revoked / expired / unknown identities; service/human confusion.
# ---------------------------------------------------------------------------
def test_m06_revoked_expired_unknown_identities_rejected():
    import time as _time

    c, headers = _admin()
    app_id, space, _ = _make_app_space(c, headers)
    created = _make_identity(c, headers, space, FULL)
    token, iid = created["token"], created["id"]
    assert c.get("/api/v1/me", headers=_svc(token)).status_code == 200

    assert c.post(f"/api/v1/admin/identities/{iid}/revoke",
                  headers=headers).status_code == 200
    assert c.get("/api/v1/me", headers=_svc(token)).status_code == 401
    assert c.get("/api/v1/objects", headers=_svc(token)).status_code == 401

    from api import identity as _identity
    exp_id = main.repo.new_id()
    exp_secret = _identity.new_secret()
    main.repo.create_identity(
        exp_id, app_id, space, "m06-expired",
        _identity.fingerprint(exp_secret), "objects:read",
        int(_time.time()) - 60,
    )
    assert c.get("/api/v1/me", headers=_svc(
        _identity.format_token(exp_id, exp_secret))).status_code == 401
    assert c.get("/api/v1/me", headers=_svc(
        "aura_00000000-0000-4000-8000-000000000000_" + "0" * 64)).status_code == 401


def test_m06_service_human_confusion_refused():
    c, headers = _admin()
    _, space, _ = _make_app_space(c, headers)
    token = _make_identity(c, headers, space, FULL)["token"]
    h = _svc(token)

    # Service identities never manage human-owned folders or file objects.
    assert c.get("/api/v1/folders", headers=h).status_code == 403
    assert c.post("/api/v1/folders", json={"name": "svc"},
                  headers=h).status_code == 403
    oid = _make_available(c, h)
    assert c.patch(f"/api/v1/objects/{oid}", json={"folder_id": None},
                   headers=h).status_code == 403
    # ... and never hold a session: logout is human-only.
    assert c.post("/api/v1/logout", headers=h).status_code == 403
    # The undocumented X-AURA-Key alias is refused: Bearer is the only channel.
    # (Fresh client: the admin client still holds a session cookie.)
    assert TestClient(main.app).get("/api/v1/me",
                                    headers={"X-AURA-Key": token}).status_code == 401


# ---------------------------------------------------------------------------
# Error and payload hygiene: no cross-tenant oracle.
# ---------------------------------------------------------------------------
def test_m06_errors_and_payloads_do_not_leak():
    c, headers = _admin()
    _, space_a, _ = _make_app_space(c, headers)
    _, space_b, _ = _make_app_space(c, headers)
    tok_a = _make_identity(c, headers, space_a, FULL)["token"]
    tok_b = _make_identity(c, headers, space_b, FULL)["token"]
    oid = _make_available(c, _svc(tok_a))

    item = c.get(f"/api/v1/objects/{oid}",
                 headers=_svc(tok_a)).json()
    assert set(item) <= {"id", "display_name", "content_type", "size_bytes",
                         "expected_size_bytes", "checksum_sha256",
                         "checksum_algorithm", "status", "folder_id",
                         "created_at", "updated_at"}

    listed = c.get("/api/v1/admin/identities", headers=headers).text
    assert "secret_hash" not in listed and tok_a not in listed

    dl = c.post(f"/api/v1/objects/{oid}/download-intent",
                headers=_svc(tok_a)).json()
    assert dl["expires_in"] == settings.intent_ttl
    assert abs(dl["expires_at"] - (int(time.time()) + settings.intent_ttl)) <= 10


# ---------------------------------------------------------------------------
# M06 lifecycle regressions.
# ---------------------------------------------------------------------------
def test_m06_purge_storage_failure_keeps_reference(monkeypatch):
    c, headers = _admin()
    _, space, _ = _make_app_space(c, headers)
    token = _make_identity(c, headers, space, FULL)["token"]
    oid = _make_available(c, _svc(token))

    class BrokenS3:
        def delete_object(self, Bucket, Key):
            raise RuntimeError("storage outage")

    monkeypatch.setattr(main, "s3ops", BrokenS3())
    r = c.post(f"/api/v1/objects/{oid}/purge", headers=_svc(token))
    assert r.status_code == 502, r.text
    # The catalogue reference survives the failed backend delete.
    kept = c.get(f"/api/v1/objects/{oid}", headers=_svc(token))
    assert kept.status_code == 200
    assert kept.json()["status"] == "available"


def test_m06_reconcile_storage_failure_is_reported(monkeypatch):
    from api.lifecycle import StorageDeleteError  # noqa: F401 (contract)

    c, headers = _admin()
    _, space, _ = _make_app_space(c, headers)
    token = _make_identity(c, headers, space, FULL)["token"]
    oid = c.post("/api/v1/objects/upload-intents",
                 json={"display_name": "stale.bin", "size_bytes": 1},
                 headers=_svc(token)).json()["id"]
    catalogue_sql.execute(
        "UPDATE objects SET created_at = ? WHERE id = ?",
        (main.repo.now() - 7200, oid),
    )

    class BrokenS3:
        def delete_object(self, Bucket, Key):
            raise RuntimeError("storage outage")

    out = reconcile_pending(main.repo, BrokenS3(), expire_after_s=3600)
    assert out["expired"] == 0
    # >= 1: the shared offline catalogue may hold older stale intents from
    # other test files; what matters is OUR intent is untouched, not the
    # exact global count.
    assert out["storage_failures"] >= 1
    # Not declared cleaned: still pending for the next sweep.
    assert c.get(f"/api/v1/objects/{oid}",
                 headers=_svc(token)).json()["status"] == "pending"

    class HealthyS3:
        def delete_object(self, Bucket, Key):
            return {}

    before = c.get(f"/api/v1/objects/{oid}",
                   headers=_svc(token)).json()["status"]
    assert before == "pending"
    out = reconcile_pending(main.repo, HealthyS3(), expire_after_s=3600)
    assert out["expired"] >= 1 and out["storage_failures"] == 0
    assert c.get(f"/api/v1/objects/{oid}",
                 headers=_svc(token)).json()["status"] == "expired"


def test_m06_no_default_bucket_refuses_implicit_upload():
    c, headers = _admin()
    suffix = uuid.uuid4().hex[:8]
    app_id = c.post("/api/v1/admin/applications",
                    json={"name": f"m06-bucket-{suffix}"},
                    headers=headers).json()["id"]
    space = c.post("/api/v1/admin/spaces", json={
        "application_id": app_id, "name": f"m06-bucket-{suffix}",
        "environment": "test",
    }, headers=headers).json()
    sid = space["id"]
    # Register a second bucket, then clear every default flag.
    extra = c.post(f"/api/v1/admin/spaces/{sid}/buckets",
                   json={"name": f"m06-extra-{suffix}"}, headers=headers)
    assert extra.status_code == 200, extra.text
    for bucket in c.get(f"/api/v1/admin/spaces/{sid}",
                        headers=headers).json()["buckets"]:
        r = c.patch(f"/api/v1/admin/buckets/{bucket['id']}",
                    json={"is_default": False}, headers=headers)
        assert r.status_code == 200, r.text

    token = _make_identity(c, headers, sid, FULL)["token"]
    # No default: an implicit upload is refused instead of silently landing
    # on a non-default bucket...
    r = c.post("/api/v1/objects/upload-intents",
               json={"display_name": "x", "size_bytes": 1},
               headers=_svc(token))
    assert r.status_code == 409
    # ... while an explicitly registered bucket still works.
    r = c.post("/api/v1/objects/upload-intents",
               json={"bucket": f"m06-extra-{suffix}", "display_name": "x",
                     "size_bytes": 1},
               headers=_svc(token))
    assert r.status_code == 200, r.text
