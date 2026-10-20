"""M03 — AURA Vault Transfer Engine: offline backend scenarios.

The S3 client is replaced with an in-process stub, so these tests exercise the
real route handlers (intent → direct PUT → commit/abort) without a live endpoint
and without PostgreSQL/SeaweedFS. The scenarios mirror the mission's required
coverage: success, empty file (when allowed), too large, interrupted storage,
expired intent, repeated commit, validation error and forbidden access, plus the
server-side content-type/size policy, idempotent double-submit suppression and
the explicit abort (cancellation) endpoint.

TESTS THAT NEED A REAL BACKEND (not run here, see the M03 report):
- PostgreSQL parity of the catalogue layer: ``tests/test_pg_parity.py``
  (``AURA_TEST_PG_DSN``).
- Live SeaweedFS round trip, presigned-URL expiry and multipart/checksum
  compatibility: ``tests/test_api.py::test_live_round_trip_with_integrity``
  (``AURA_LIVE_S3=1``) and ``scripts/seaweedfs-compat-probe.py``.
"""
import hashlib
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
from urllib.parse import parse_qs, urlparse  # noqa: E402

import api.main as main  # noqa: E402
from api import authz  # noqa: E402
from api.auth import hash_password  # noqa: E402

client = TestClient(main.app)


# ---------------------------------------------------------------------------
# Offline storage stub
# ---------------------------------------------------------------------------
class FakeBody:
    def __init__(self, data: bytes):
        self.data = data
        self.chunks: list[int] = []

    def iter_chunks(self, chunk_size=1024 * 1024):
        for i in range(0, len(self.data), chunk_size):
            chunk = self.data[i:i + chunk_size]
            self.chunks.append(len(chunk))
            yield chunk


class DummyS3:
    """Minimal S3 ops stub: head/get/delete, with injectable failures."""

    def __init__(
        self, data: bytes | None = None, *, declared_size=None,
        head_error: Exception | None = None, get_error: Exception | None = None,
    ):
        self.data = b"" if data is None else data
        self.declared_size = len(self.data) if declared_size is None else declared_size
        self.head_error = head_error
        self.get_error = get_error
        self.deleted: list[tuple[str, str]] = []

    def head_object(self, Bucket, Key):
        if self.head_error is not None:
            raise self.head_error
        return {"ContentLength": self.declared_size, "ContentType": "application/octet-stream",
                "ServerSideEncryption": "AES256"}

    def get_object(self, Bucket, Key):
        if self.get_error is not None:
            raise self.get_error
        return {"Body": FakeBody(self.data)}

    def delete_object(self, Bucket, Key):
        self.deleted.append((Bucket, Key))


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


def _intent(c, headers, *, name="file.bin", size_bytes=1, content_type=None,
            folder_id=None, idem=None):
    body = {"display_name": name, "size_bytes": size_bytes}
    if content_type is not None:
        body["content_type"] = content_type
    if folder_id is not None:
        body["folder_id"] = folder_id
    hdrs = dict(headers)
    if idem is not None:
        hdrs["Idempotency-Key"] = idem
    return c.post("/api/v1/objects/upload-intents", json=body, headers=hdrs)


def _backdate_intent(oid, seconds):
    expired = int(time.time()) - seconds
    if main.repo.dialect == "sqlite":
        conn = main.repo.conn
        conn.execute("UPDATE objects SET intent_expires_at = ? WHERE id = ?", (expired, oid))
        conn.commit()
    else:  # pragma: no cover - offline tests run on SQLite
        main.repo._run("UPDATE objects SET intent_expires_at = %s WHERE id = %s", (expired, oid))


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------
def test_upload_success_verifies_sha256_and_returns_expected_headers(monkeypatch):
    data = b"M03 transfer engine payload"
    digest = hashlib.sha256(data).hexdigest()
    monkeypatch.setattr(main, "s3ops", DummyS3(data))
    c, headers = _admin()

    r = _intent(c, headers, name="report.pdf", size_bytes=len(data),
                content_type="application/pdf")
    assert r.status_code == 200, r.text
    intent = r.json()
    assert intent["status"] == "pending"
    assert intent["method"] == "PUT"
    assert intent["headers"]["Content-Type"] == "application/pdf"
    assert intent["content_type"] == "application/pdf"
    assert intent["idempotent_replay"] is False

    committed = c.post(f"/api/v1/objects/{intent['id']}/commit", headers=headers)
    assert committed.status_code == 200, committed.text
    body = committed.json()
    assert body["status"] == "available"
    assert body["size_bytes"] == len(data)
    assert body["checksum_sha256"] == digest
    assert body["checksum_algorithm"] == "sha256"


def test_presigned_url_binds_content_length_and_type(monkeypatch):
    monkeypatch.setattr(main, "s3ops", DummyS3(b"x"))
    c, headers = _admin()
    r = _intent(c, headers, size_bytes=4096, content_type="text/plain")
    signed = parse_qs(urlparse(r.json()["upload_url"]).query)["X-Amz-SignedHeaders"][0]
    # The declared byte count and media type are part of the signed request, so
    # the gateway refuses a different size/type than the API authorized (#40).
    assert "content-length" in signed
    assert "content-type" in signed


def test_presigned_put_signs_sse_s3_and_get_does_not():
    # SSE-S3 (AES256) is bound into the upload signature, exactly like the
    # size/type headers above; botocore records it in X-Amz-SignedHeaders (it is
    # a *header*, not a query parameter). The GET path must not carry it: a
    # download is never re-encrypted and must stay byte-identical.
    from api.s3 import make_client, presigned_get, presigned_put

    client = make_client("https://example.invalid", "ak-1234567890", "sk-1234567890", "us-east-1")
    put = presigned_put(client, "bucket", "key", "text/plain", 900, 10)
    signed = parse_qs(urlparse(put).query)["X-Amz-SignedHeaders"][0]
    assert "x-amz-server-side-encryption" in signed
    assert "x-amz-server-side-encryption" in put

    get = presigned_get(client, "bucket", "key", 900)
    assert "x-amz-server-side-encryption" not in get


def test_intent_tells_the_client_to_send_the_sse_header(monkeypatch):
    monkeypatch.setattr(main, "s3ops", DummyS3(b"x"))
    c, headers = _admin()
    r = _intent(c, headers, size_bytes=4096, content_type="text/plain")
    assert r.status_code == 200, r.text
    body = r.json()
    signed = parse_qs(urlparse(body["upload_url"]).query)["X-Amz-SignedHeaders"][0]
    assert "x-amz-server-side-encryption" in signed
    # The header the API signed and the header the client is told to send must
    # match, or the gateway rejects the PUT (signed-header mismatch).
    assert body["headers"]["x-amz-server-side-encryption"] == "AES256"


# ---------------------------------------------------------------------------
# Empty file (allowed only when the deployment enables it)
# ---------------------------------------------------------------------------
def test_empty_file_is_rejected_by_default():
    c, headers = _admin()
    r = _intent(c, headers, name="empty.bin", size_bytes=0)
    assert r.status_code == 422, r.text
    assert "empty" in r.json()["detail"]


def test_empty_file_is_accepted_and_committed_when_enabled(monkeypatch):
    monkeypatch.setattr(main.settings, "allow_empty_objects", True)
    monkeypatch.setattr(main, "s3ops", DummyS3(b""))
    c, headers = _admin()
    r = _intent(c, headers, name="empty.bin", size_bytes=0)
    assert r.status_code == 200, r.text
    committed = c.post(f"/api/v1/objects/{r.json()['id']}/commit", headers=headers)
    assert committed.status_code == 200, committed.text
    body = committed.json()
    assert body["size_bytes"] == 0
    assert body["checksum_sha256"] == hashlib.sha256(b"").hexdigest()


# ---------------------------------------------------------------------------
# Too large (server-side, both at intent and at commit)
# ---------------------------------------------------------------------------
def test_size_over_cap_is_refused_at_intent(monkeypatch):
    monkeypatch.setattr(main.settings, "max_object_bytes", 1024)
    c, headers = _admin()
    r = _intent(c, headers, size_bytes=4096)
    assert r.status_code == 422, r.text


def test_object_larger_than_declared_is_refused_at_commit(monkeypatch):
    monkeypatch.setattr(main.settings, "max_object_bytes", 1024)
    monkeypatch.setattr(main, "s3ops", DummyS3(os.urandom(4096)))
    c, headers = _admin()
    oid = _intent(c, headers, size_bytes=1).json()["id"]
    r = c.post(f"/api/v1/objects/{oid}/commit", headers=headers)
    assert r.status_code == 413, r.text
    assert c.get(f"/api/v1/objects/{oid}").json()["status"] == "failed"


# ---------------------------------------------------------------------------
# Network/storage interruption: retryable, never a false success
# ---------------------------------------------------------------------------
def test_storage_unreachable_keeps_intent_pending_for_retry(monkeypatch):
    monkeypatch.setattr(main, "s3ops", DummyS3(b"x", head_error=RuntimeError("timeout")))
    c, headers = _admin()
    oid = _intent(c, headers).json()["id"]
    r = c.post(f"/api/v1/objects/{oid}/commit", headers=headers)
    assert r.status_code == 422, r.text
    # A transient storage error is not a content failure: the object stays
    # pending so a later reconcile/retry can still validate it.
    assert c.get(f"/api/v1/objects/{oid}").json()["status"] == "pending"


def test_unreadable_storage_does_not_mark_available(monkeypatch):
    monkeypatch.setattr(main, "s3ops", DummyS3(b"x", get_error=RuntimeError("reset")))
    c, headers = _admin()
    oid = _intent(c, headers).json()["id"]
    r = c.post(f"/api/v1/objects/{oid}/commit", headers=headers)
    assert r.status_code == 422, r.text
    assert c.post(f"/api/v1/objects/{oid}/download-intent", headers=headers).status_code == 409


def test_truncated_upload_is_failed_and_not_downloadable(monkeypatch):
    monkeypatch.setattr(main, "s3ops", DummyS3(b"short", declared_size=200))
    c, headers = _admin()
    oid = _intent(c, headers).json()["id"]
    r = c.post(f"/api/v1/objects/{oid}/commit", headers=headers)
    assert r.status_code == 422, r.text
    assert c.get(f"/api/v1/objects/{oid}").json()["status"] == "failed"
    assert c.post(f"/api/v1/objects/{oid}/download-intent", headers=headers).status_code == 409


# ---------------------------------------------------------------------------
# Expired intent / URL
# ---------------------------------------------------------------------------
def test_expired_intent_is_refused_and_terminal(monkeypatch):
    monkeypatch.setattr(main, "s3ops", DummyS3(b"x"))
    c, headers = _admin()
    oid = _intent(c, headers).json()["id"]
    _backdate_intent(oid, main.settings.intent_ttl + main.settings.intent_grace_s + 60)
    r = c.post(f"/api/v1/objects/{oid}/commit", headers=headers)
    assert r.status_code == 410, r.text
    assert c.get(f"/api/v1/objects/{oid}").json()["status"] == "expired"
    # A second commit cannot resurrect an expired intent.
    assert c.post(f"/api/v1/objects/{oid}/commit", headers=headers).status_code == 409


# ---------------------------------------------------------------------------
# Repeated commit is idempotent (no duplicate verification side effects)
# ---------------------------------------------------------------------------
def test_repeated_commit_is_idempotent(monkeypatch):
    data = b"commit-once"
    s3 = DummyS3(data)
    monkeypatch.setattr(main, "s3ops", s3)
    c, headers = _admin()
    oid = _intent(c, headers, size_bytes=len(data)).json()["id"]
    first = c.post(f"/api/v1/objects/{oid}/commit", headers=headers)
    second = c.post(f"/api/v1/objects/{oid}/commit", headers=headers)
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["checksum_sha256"] == second.json()["checksum_sha256"]
    assert first.json()["status"] == "available"


def test_concurrent_commits_converge_to_one_available_object(monkeypatch):
    # #83: simultaneous commits of the same pending intent must converge to a
    # single available object with one digest — no duplicate rows, no failure
    # state, no divergent checksum. SQLite serializes via the repo lock; the
    # PostgreSQL equivalent needs the live parity suite (AURA_TEST_PG_DSN).
    import hashlib
    import threading

    data = b"concurrent-commit"
    monkeypatch.setattr(main, "s3ops", DummyS3(data))
    c, headers = _admin()
    oid = _intent(c, headers, size_bytes=len(data)).json()["id"]
    expected = hashlib.sha256(data).hexdigest()

    results: list = []
    barrier = threading.Barrier(8)

    def commit_once():
        worker = TestClient(main.app)
        worker.cookies = c.cookies
        barrier.wait()
        results.append(worker.post(
            f"/api/v1/objects/{oid}/commit", headers=headers))

    threads = [threading.Thread(target=commit_once) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 8
    assert all(r.status_code == 200 for r in results), [
        (r.status_code, r.text) for r in results if r.status_code != 200
    ]
    assert {r.json()["checksum_sha256"] for r in results} == {expected}
    assert {r.json()["status"] for r in results} == {"available"}


# ---------------------------------------------------------------------------
# Abort == the only cancellation the UI is allowed to use
# ---------------------------------------------------------------------------
def test_abort_pending_intent_deletes_bytes_and_is_idempotent(monkeypatch):
    s3 = DummyS3(b"partial")
    monkeypatch.setattr(main, "s3ops", s3)
    c, headers = _admin()
    oid = _intent(c, headers).json()["id"]
    r = c.post(f"/api/v1/objects/{oid}/abort", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "aborted"
    assert len(s3.deleted) == 1
    # Repeating the abort returns the same terminal state without another delete.
    again = c.post(f"/api/v1/objects/{oid}/abort", headers=headers)
    assert again.status_code == 200 and again.json()["status"] == "aborted"
    assert len(s3.deleted) == 1
    # An aborted intent can no longer be committed.
    assert c.post(f"/api/v1/objects/{oid}/commit", headers=headers).status_code == 409


def test_abort_refused_for_available_object(monkeypatch):
    data = b"kept"
    monkeypatch.setattr(main, "s3ops", DummyS3(data))
    c, headers = _admin()
    oid = _intent(c, headers, size_bytes=len(data)).json()["id"]
    assert c.post(f"/api/v1/objects/{oid}/commit", headers=headers).status_code == 200
    # Cancelling a committed object is a delete, not a transfer abort.
    assert c.post(f"/api/v1/objects/{oid}/abort", headers=headers).status_code == 409


def test_abort_tolerates_storage_failure(monkeypatch):
    class FailingDelete(DummyS3):
        def delete_object(self, Bucket, Key):
            raise RuntimeError("storage down")

    monkeypatch.setattr(main, "s3ops", FailingDelete(b"partial"))
    c, headers = _admin()
    oid = _intent(c, headers).json()["id"]
    r = c.post(f"/api/v1/objects/{oid}/abort", headers=headers)
    assert r.status_code == 200 and r.json()["status"] == "aborted"


# ---------------------------------------------------------------------------
# Idempotency: accidental double submit does not create a second object
# ---------------------------------------------------------------------------
def test_same_idempotency_key_replays_the_same_intent(monkeypatch):
    monkeypatch.setattr(main, "s3ops", DummyS3(b"x"))
    c, headers = _admin()
    key = f"dbl-{uuid.uuid4().hex}"
    first = _intent(c, headers, name="once.bin", idem=key)
    second = _intent(c, headers, name="once.bin", idem=key)
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert first.json()["idempotent_replay"] is False
    assert second.json()["idempotent_replay"] is True
    # Exactly one catalogue row exists for the key.
    rows = [o for o in c.get("/api/v1/objects").json()["items"]
            if o["id"] == first.json()["id"]]
    assert len(rows) == 1


def test_idempotency_key_is_scoped_to_the_actor(monkeypatch):
    monkeypatch.setattr(main, "s3ops", DummyS3(b"x"))
    c, headers = _admin()
    key = f"shared-{uuid.uuid4().hex}"
    a_ids = {_intent(c, headers, idem=key).json()["id"] for _ in range(2)}

    other = f"other-{uuid.uuid4().hex[:8]}"
    main.repo.create_user(other, hash_password("other-pass"), role="user",
                          space_id=main.repo.default_scope()[1])
    oc = TestClient(main.app)
    login = oc.post("/api/v1/login", json={"username": other, "password": "other-pass"})
    oheaders = {"X-CSRF-Token": login.json()["csrf_token"]}
    b = _intent(oc, oheaders, idem=key).json()["id"]
    assert b not in a_ids


def test_invalid_idempotency_key_is_refused(monkeypatch):
    monkeypatch.setattr(main, "s3ops", DummyS3(b"x"))
    c, headers = _admin()
    assert _intent(c, headers, idem="short").status_code == 200  # >=1 char is fine
    assert _intent(c, headers, idem="x" * 500).status_code == 422
    assert _intent(c, headers, idem="bad\nkey").status_code == 422


# ---------------------------------------------------------------------------
# Server-side content-type policy
# ---------------------------------------------------------------------------
def test_content_type_allow_list_is_enforced_server_side(monkeypatch):
    monkeypatch.setattr(main, "s3ops", DummyS3(b"x"))
    monkeypatch.setattr(main.settings, "allowed_content_types", ("image/png", "text/*"))
    c, headers = _admin()
    assert _intent(c, headers, content_type="image/png").status_code == 200
    assert _intent(c, headers, content_type="text/csv").status_code == 200
    blocked = _intent(c, headers, content_type="application/zip")
    assert blocked.status_code == 422, blocked.text
    assert blocked.json()["detail"] == "content type not allowed"


def test_malformed_content_type_is_refused(monkeypatch):
    monkeypatch.setattr(main, "s3ops", DummyS3(b"x"))
    c, headers = _admin()
    for bad in ("not-a-type", "text/", "/plain", "a/b/c", "text/plain\r\nX: y"):
        r = _intent(c, headers, content_type=bad)
        assert r.status_code == 422, (bad, r.text)


def test_content_type_is_normalized(monkeypatch):
    monkeypatch.setattr(main, "s3ops", DummyS3(b"x"))
    c, headers = _admin()
    r = _intent(c, headers, content_type="TEXT/PLAIN")
    assert r.status_code == 200, r.text
    assert r.json()["content_type"] == "text/plain"
    assert r.json()["headers"]["Content-Type"] == "text/plain"


# ---------------------------------------------------------------------------
# Forbidden access / isolation
# ---------------------------------------------------------------------------
def _make_identity(c, headers, scopes):
    suffix = uuid.uuid4().hex[:8]
    app = c.post("/api/v1/admin/applications", json={"name": f"app-{suffix}"},
                 headers=headers).json()["id"]
    space = c.post("/api/v1/admin/spaces", json={
        "application_id": app, "name": f"space-{suffix}", "environment": "test",
    }, headers=headers).json()
    ident = c.post("/api/v1/admin/identities", json={
        "space_id": space["id"], "name": "svc", "scopes": scopes,
    }, headers=headers).json()
    return space["id"], {"Authorization": f"Bearer {ident['token']}"}


def test_abort_and_commit_deny_cross_space(monkeypatch):
    monkeypatch.setattr(main, "s3ops", DummyS3(b"x"))
    c, headers = _admin()
    _, token_a = _make_identity(c, headers, ["objects:read", "objects:write"])
    _, token_b = _make_identity(c, headers, ["objects:read", "objects:write"])
    oid = c.post("/api/v1/objects/upload-intents",
                 json={"display_name": "a.bin", "size_bytes": 1},
                 headers=token_a).json()["id"]
    assert c.post(f"/api/v1/objects/{oid}/commit", headers=token_b).status_code == 404
    assert c.post(f"/api/v1/objects/{oid}/abort", headers=token_b).status_code == 404


def test_read_only_identity_cannot_create_or_abort(monkeypatch):
    monkeypatch.setattr(main, "s3ops", DummyS3(b"x"))
    c, headers = _admin()
    _, token = _make_identity(c, headers, ["objects:read"])
    assert c.post("/api/v1/objects/upload-intents",
                  json={"display_name": "x", "size_bytes": 1},
                  headers=token).status_code == 403
    oid = _intent(c, headers).json()["id"]
    assert c.post(f"/api/v1/objects/{oid}/abort", headers=token).status_code == 403


def test_anonymous_abort_is_rejected():
    assert client.post("/api/v1/objects/some-id/abort", json={}).status_code == 401


# ---------------------------------------------------------------------------
# Download intent carries the display metadata the UI needs
# ---------------------------------------------------------------------------
def test_download_intent_reports_size_type_and_checksum(monkeypatch):
    data = b"download-me"
    digest = hashlib.sha256(data).hexdigest()
    monkeypatch.setattr(main, "s3ops", DummyS3(data))
    c, headers = _admin()
    oid = _intent(c, headers, name="dl.txt", size_bytes=len(data),
                  content_type="text/plain").json()["id"]
    c.post(f"/api/v1/objects/{oid}/commit", headers=headers)
    r = c.post(f"/api/v1/objects/{oid}/download-intent", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["method"] == "GET"
    assert body["display_name"] == "dl.txt"
    assert body["content_type"] == "text/plain"
    assert body["size_bytes"] == len(data)
    assert body["checksum_sha256"] == digest


# ---------------------------------------------------------------------------
# Preview intent: same scope/status rules as download, but the presigned GET
# is bound to Content-Disposition: inline for in-browser rendering.
# ---------------------------------------------------------------------------
def test_preview_intent_renders_inline_with_object_type(monkeypatch):
    data = b"%PDF-1.4 preview-bytes"
    monkeypatch.setattr(main, "s3ops", DummyS3(data))
    c, headers = _admin()
    oid = _intent(c, headers, name="preview.pdf", size_bytes=len(data),
                  content_type="application/pdf").json()["id"]
    c.post(f"/api/v1/objects/{oid}/commit", headers=headers)
    r = c.post(f"/api/v1/objects/{oid}/preview-intent", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["method"] == "GET"
    assert body["display_name"] == "preview.pdf"
    assert body["content_type"] == "application/pdf"
    assert body["size_bytes"] == len(data)
    # The presigned GET forces an in-browser render of the object's own type.
    assert "X-Amz-Signature=" in body["preview_url"]
    assert "response-content-disposition=inline" in body["preview_url"]
    assert "response-content-type=application%2Fpdf" in body["preview_url"]


def test_preview_intent_refuses_types_that_can_run_script(monkeypatch):
    # A user-uploaded HTML/SVG/XML file must never be served inline from the
    # storage origin: preview-intent answers 415 and the UI offers a download.
    monkeypatch.setattr(main, "s3ops", DummyS3(b"<script>1</script>"))
    c, headers = _admin()
    for name, ctype in (("page.html", "text/html"), ("logo.svg", "image/svg+xml"),
                        ("data.xml", "application/xml"), ("doc.bin", "application/octet-stream")):
        oid = _intent(c, headers, name=name, size_bytes=18, content_type=ctype).json()["id"]
        c.post(f"/api/v1/objects/{oid}/commit", headers=headers)
        r = c.post(f"/api/v1/objects/{oid}/preview-intent", headers=headers)
        assert r.status_code == 415, (ctype, r.status_code, r.text)
        # download-intent is unaffected: the file stays retrievable.
        assert c.post(f"/api/v1/objects/{oid}/download-intent", headers=headers).status_code == 200


def test_is_previewable_ignores_parameters_and_case():
    assert main.is_previewable("Application/PDF")
    assert main.is_previewable("image/png; charset=binary")
    assert main.is_previewable("audio/mpeg")
    assert not main.is_previewable("image/svg+xml")
    assert not main.is_previewable("text/html")
    assert not main.is_previewable("")


def test_preview_intent_matches_download_scoping(monkeypatch):
    monkeypatch.setattr(main, "s3ops", DummyS3(b"x"))
    c, headers = _admin()
    _, token_b = _make_identity(c, headers, ["objects:read"])
    oid = _intent(c, headers, name="scoped.bin").json()["id"]

    # Pending -> 409 (only an available object is previewable).
    assert c.post(f"/api/v1/objects/{oid}/preview-intent",
                  headers=headers).status_code == 409
    # Cross-space -> 404, indistinguishable from a missing object.
    assert c.post(f"/api/v1/objects/{oid}/preview-intent",
                  headers=token_b).status_code == 404
    # Anonymous -> 401.
    assert client.post(f"/api/v1/objects/{oid}/preview-intent",
                       json={}).status_code == 401


# ---------------------------------------------------------------------------
# Secret hygiene: presigned URLs and keys never reach the audit log
# ---------------------------------------------------------------------------
def test_audit_log_never_records_presigned_urls(monkeypatch, tmp_path):
    import json
    log = tmp_path / "audit.jsonl"
    monkeypatch.setenv("AURA_AUDIT_LOG", str(log))
    monkeypatch.setattr(main, "s3ops", DummyS3(b"x"))
    c, headers = _admin()
    key = f"audit-{uuid.uuid4().hex}"
    oid = _intent(c, headers, idem=key).json()["id"]
    _intent(c, headers, idem=key)  # replay path
    c.post(f"/api/v1/objects/{oid}/abort", headers=headers)
    blob = log.read_text(encoding="utf-8")
    assert "X-Amz-Signature" not in blob
    assert "upload_url" not in blob and "download_url" not in blob
    assert key not in blob
    records = [json.loads(line) for line in blob.strip().splitlines()]
    assert any(r["action"] == "upload-intent" and r["result"] == "replay" for r in records)
    assert any(r["action"] == "abort" and r["result"] == "success" for r in records)


# ---------------------------------------------------------------------------
# #83: a catalogue failure at commit must not fake success (#87 invariant)
# ---------------------------------------------------------------------------
def test_commit_catalogue_failure_keeps_intent_pending(monkeypatch):
    from fastapi.testclient import TestClient as _TestClient
    data = b"no-false-success"
    monkeypatch.setattr(main, "s3ops", DummyS3(data))
    c = _TestClient(main.app, raise_server_exceptions=False)
    r = c.post("/api/v1/login", json={
        "username": "admin", "password": os.environ["AURA_API_ADMIN_PASSWORD"],
    })
    assert r.status_code == 200, r.text
    headers = {"X-CSRF-Token": r.json()["csrf_token"]}
    oid = _intent(c, headers, size_bytes=len(data)).json()["id"]

    def _boom(oid_, size_, digest_):
        raise RuntimeError("catalogue down")

    monkeypatch.setattr(main.repo, "mark_available", _boom)
    r = c.post(f"/api/v1/objects/{oid}/commit", headers=headers)
    assert r.status_code == 500
    row = main.repo.get_object(oid, space_id=main.repo.default_scope()[1])
    assert dict(row)["status"] == "pending"
