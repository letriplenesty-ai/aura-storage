"""AURA Management API (#18; authz #35/#36, M2M #38, streaming commit #37).

Catalogue metadata lives here; object bytes travel directly between the client
and S3 through short-lived presigned URLs. The API never stores a browser session
token in the response body and never puts S3 credentials in the frontend.
"""
import shutil
import time
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator

from . import authz, ids, netinfo
from .audit import (
    audit_event,
    audit_path,
    bind_request_context,
    prune_audit_log,
    query_audit_log,
    recent_error_events,
    reset_request_context,
)
from .auth import hash_password, new_token, verify_password
from .authz import (
    ADMIN_READ,
    ADMIN_WRITE,
    ALL_SCOPES,
    OBJECTS_DELETE,
    OBJECTS_PURGE,
    OBJECTS_READ,
    OBJECTS_WRITE,
    SERVICE_SCOPES,
    authorize,
    effective_scopes,
)
from .config import settings
from .identity import fingerprint, format_token, new_secret
from .lifecycle import purge_object, reconcile_pending
from .s3 import (
    SSE_ALGORITHM,
    SSE_HEADER,
    ObjectTooLarge,
    is_valid_bucket_name,
    make_clients,
    presigned_get,
    presigned_put,
    stream_sha256,
)
from .store import repo
from .transfer import (
    ABORTED,
    AVAILABLE,
    DELETED,
    EXPIRED,
    PENDING,
    validate_upload_policy,
    valid_idempotency_key,
)

app = FastAPI(title="AURA Management API", version="0.2.0")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    return response


# Process boot time for the M07 supervision endpoint (uptime is informational).
_BOOT_TIME = int(time.time())


def _incoming_request_id(request: Request) -> str:
    """Correlation id: echo a safe client value, otherwise mint a UUID v4.

    A client value is echoed only when it is short printable ASCII with no
    whitespace or controls, so it can be logged and indexed safely; anything
    else is replaced by a server-minted id (never 400: correlation must not
    break callers).
    """
    incoming = request.headers.get("x-request-id", "")
    if (incoming and len(incoming) <= 128
            and all(33 <= ord(ch) <= 126 for ch in incoming)):
        return incoming
    return ids.new_id()


@app.middleware("http")
async def request_context(request: Request, call_next):
    """Bind the M07 audit context for exactly one request.

    A fresh mutable holder carries the request id; the authorization layer
    fills the tenant in once the caller authenticates. The binding is reset
    after the response so sequential requests sharing a worker thread can
    never inherit each other's attribution.
    """
    request_id = _incoming_request_id(request)
    _, token = bind_request_context(request_id)
    try:
        response = await call_next(request)
        response.headers["X-Request-Id"] = request_id
        return response
    finally:
        # Always reset, including when call_next raises: the original
        # exception then reaches ServerErrorMiddleware unmasked.
        reset_request_context(token)


# In-memory login rate limiter: {client_ip: [failure_epoch, ...]}.
# Only *failed* attempts are counted, so a burst of legitimate logins cannot
# lock out everyone sharing a proxy/NAT address.
_LOGIN_ATTEMPTS: dict[str, list[float]] = {}


def _client_ip(request: Request) -> str:
    # Trusted-proxy aware: browsers arrive through Traefik and nginx, so the
    # socket peer is a proxy; see api/netinfo.py (#35/#38).
    return netinfo.client_ip(request)


def _login_blocked(client_ip: str, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    window = settings.login_rate_window
    attempts = [t for t in _LOGIN_ATTEMPTS.get(client_ip, []) if now - t < window]
    if attempts:
        _LOGIN_ATTEMPTS[client_ip] = attempts
    else:
        _LOGIN_ATTEMPTS.pop(client_ip, None)
    return len(attempts) >= settings.login_rate_limit


def _login_record_failure(client_ip: str) -> None:
    _LOGIN_ATTEMPTS.setdefault(client_ip, []).append(time.time())


def _login_clear(client_ip: str) -> None:
    # A successful login resets the failure budget for that client.
    _LOGIN_ATTEMPTS.pop(client_ip, None)


def reset_login_attempts() -> None:
    _LOGIN_ATTEMPTS.clear()

s3, s3ops = make_clients(
    settings.s3_endpoint, settings.s3_internal_endpoint,
    settings.s3_access_key, settings.s3_secret_key, settings.s3_region,
)


def _ensure_bootstrap_admin() -> None:
    """Create, or promote, the single bootstrap administrator (hashed password)."""
    row = repo.get_user_by_name(settings.admin_user)
    if row is None:
        repo.create_user(
            settings.admin_user, hash_password(settings.admin_password), role="admin"
        )
    else:
        repo.ensure_admin(row["id"])


_ensure_bootstrap_admin()

# Verified against when the username is unknown (timing equalisation in login).
_DUMMY_PASS_HASH = hash_password(new_token(32))


def _as_dict(row) -> dict:
    return dict(row) if not isinstance(row, dict) else row


def _canonical_id(value: str, field: str) -> str:
    """Return ``value`` as a canonical UUID v4, or raise 422.

    Identifiers AURA issues are always canonical UUID v4 (``api/ids.py``), so a
    request carrying anything else is a client error, not a lookup miss.
    """
    canonical = ids.canonical(value)
    if canonical is None:
        raise HTTPException(status_code=422, detail=f"invalid {field}")
    return canonical


def _canonical_uuid(value: str) -> str:
    """Pydantic validator helper: canonicalize or fail with 422."""
    canonical = ids.canonical(value)
    if canonical is None:
        raise ValueError("must be a canonical UUID v4")
    return canonical


# Canonical identifier type for request bodies. Accepts the alternate spellings
# ``uuid.UUID`` understands (case, braces, ``urn:``) and normalizes them, but
# rejects anything that is not a UUID v4.
Uuid4 = Annotated[str, AfterValidator(_canonical_uuid)]

# Object list query vocabulary (M02). Literals make FastAPI return 422 for any
# unsupported value and let the OpenAPI document describe the accepted set.
ObjectStatus = Literal[
    "pending", "available", "deleted", "expired", "failed", "aborted"
]
ObjectSort = Literal["created_at", "display_name", "size_bytes"]
SortOrder = Literal["asc", "desc"]


def _object_scope(principal: dict) -> dict:
    """Catalogue filter for the caller: space always, owner for human users."""
    return {
        "space_id": principal["space_id"],
        "owner_id": principal["id"] if principal["kind"] == "user" else None,
    }


# Folders (#61) are logical catalogue entities owned by a human user. Names are
# flat labels (no path semantics): a separator or control character is refused.
MAX_FOLDER_DEPTH = 32


def _validate_folder_name(value: str) -> str:
    name = value.strip()
    if not name or name in (".", ".."):
        raise ValueError("invalid folder name")
    if any(ch in name for ch in ("/", "\\")) or any(ord(ch) < 32 for ch in name):
        raise ValueError("invalid folder name")
    return name


def _folder_public(row) -> dict:
    row = _as_dict(row)
    return {
        "id": row["id"],
        "name": row["name"],
        "parent_id": row["parent_id"],
        "created_at": row["created_at"],
    }


# Lifecycle values accepted by the admin registry (#M04). Applications and spaces
# are soft-disabled, never deleted: a disable stops new provisioning/upload while
# keeping every catalogue row readable.
APPLICATION_STATUSES = frozenset({"active", "disabled"})
SPACE_STATUSES = frozenset({"active", "disabled"})


def _trim_name(value: str) -> str:
    name = value.strip()
    if not name:
        raise ValueError("name must not be blank")
    return name


def _application_public(row) -> dict:
    row = _as_dict(row)
    return {
        "id": row["id"], "name": row["name"], "status": row["status"],
        "created_at": row["created_at"],
    }


def _space_public(row) -> dict:
    row = _as_dict(row)
    return {
        "id": row["id"], "application_id": row["application_id"],
        "name": row["name"], "environment": row["environment"],
        "status": row["status"], "created_at": row["created_at"],
    }


def _bucket_public(row) -> dict:
    row = _as_dict(row)
    # A bucket row carries no S3 credential: the access key/secret live only in
    # the deployment environment and are never part of the catalogue (#M04).
    return {
        "id": row["id"], "space_id": row["space_id"], "name": row["name"],
        "is_default": bool(row["is_default"]), "created_at": row["created_at"],
    }


def _object_public(row) -> dict:
    row = _as_dict(row)
    # Only catalogue metadata that is safe to surface. ``bucket`` and
    # ``object_key`` are internal storage coordinates and are never returned.
    return {
        "id": row["id"], "display_name": row["display_name"],
        "content_type": row["content_type"], "size_bytes": row["size_bytes"],
        "expected_size_bytes": row.get("expected_size_bytes"),
        "checksum_sha256": row["checksum_sha256"],
        "checksum_algorithm": "sha256",
        "status": row["status"],
        "folder_id": row["folder_id"],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


class LoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str
    password: str


class UploadIntentIn(BaseModel):
    # `bucket` is optional and validated against the caller's space (#36). Object
    # keys are always generated server-side; no client field can choose them.
    model_config = ConfigDict(extra="forbid")
    bucket: str | None = Field(default=None, max_length=63)
    display_name: str = Field(min_length=1, max_length=255)
    content_type: str = Field(default="application/octet-stream", max_length=127)
    # Declared upload size. It is bound into the presigned URL as a signed
    # Content-Length so an uploader cannot exceed it (#40); the API refuses a
    # declared size above the configured object cap before presigning. Zero is
    # accepted only when empty objects are enabled for this deployment.
    size_bytes: int = Field(ge=0)
    folder_id: Uuid4 | None = None


class ObjectPatchIn(BaseModel):
    # Object reassignment into a logical folder (#62). `model_fields_set`
    # distinguishes "not sent" from an explicit null (which unfiles the object
    # back to the root level).
    model_config = ConfigDict(extra="forbid")
    folder_id: Uuid4 | None = None


class ApplicationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=63)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return _trim_name(value)


class SpaceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    application_id: Uuid4
    name: str = Field(min_length=1, max_length=63)
    environment: str = Field(min_length=1, max_length=32)
    default_bucket: str | None = Field(default=None, max_length=63)

    @field_validator("name", "environment")
    @classmethod
    def _label(cls, value: str) -> str:
        return _trim_name(value)


class ApplicationPatchIn(BaseModel):
    # Partial update of an application (#M04). `name` is the only mutable label;
    # `status` carries activation/deactivation. `model_fields_set` tells "not
    # sent" apart from an explicit null (which is refused for both fields).
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=63)
    status: str | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, value: str | None) -> str | None:
        return None if value is None else _trim_name(value)

    @field_validator("status")
    @classmethod
    def _status(cls, value: str | None) -> str | None:
        if value is not None and value not in APPLICATION_STATUSES:
            raise ValueError("invalid status")
        return value


class SpacePatchIn(BaseModel):
    # Controlled modification of a space (#M04). `application_id` is deliberately
    # absent: reassigning a space to another application would move every object,
    # bucket and identity to a different tenant, so it is not an editable field.
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=63)
    environment: str | None = Field(default=None, min_length=1, max_length=32)
    status: str | None = None

    @field_validator("name", "environment")
    @classmethod
    def _label(cls, value: str | None) -> str | None:
        return None if value is None else _trim_name(value)

    @field_validator("status")
    @classmethod
    def _status(cls, value: str | None) -> str | None:
        if value is not None and value not in SPACE_STATUSES:
            raise ValueError("invalid status")
        return value


class BucketIn(BaseModel):
    # Register an additional bucket for an existing space (#M04). The name is
    # validated against the S3 naming rules before it can be used by a client.
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=63)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return _trim_name(value)


class BucketPatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    is_default: bool


class IdentityIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    space_id: Uuid4
    name: str = Field(min_length=1, max_length=63)
    scopes: list[str] = Field(default_factory=list)
    ttl_seconds: int | None = Field(default=None, ge=1)


class FolderIn(BaseModel):
    # Logical folder creation (#61). `parent_id` is omitted/null for a root folder.
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=255)
    parent_id: Uuid4 | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return _validate_folder_name(value)


class FolderPatchIn(BaseModel):
    # Partial update. `model_fields_set` distinguishes "not sent" from an explicit
    # null (which moves the folder back to the root level).
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=255)
    parent_id: Uuid4 | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, value: str | None) -> str | None:
        return None if value is None else _validate_folder_name(value)


@app.get("/health/live")
def live():
    return {"status": "live"}


@app.get("/health/ready")
def ready():
    try:
        s3ops.list_buckets()
        return {"status": "ready"}
    except Exception:  # never leak backend details
        return JSONResponse(status_code=503, content={"status": "not ready"})


@app.post("/api/v1/login")
def login(body: LoginIn, request: Request, response: Response):
    client_ip = _client_ip(request)
    if _login_blocked(client_ip):
        audit_event("login", body.username, "rate_limited")
        # Honor-your-limits (#86): tell the client when it may retry.
        raise HTTPException(status_code=429, detail="too many login attempts",
                            headers={"Retry-After": str(settings.login_rate_window)})
    row = repo.get_user_by_name(body.username)
    # An unknown username still pays one argon2 verification, so response time
    # does not reveal which usernames exist.
    stored_hash = row["pass_hash"] if row is not None else _DUMMY_PASS_HASH
    if not verify_password(stored_hash, body.password) or row is None:
        _login_record_failure(client_ip)
        audit_event("login", body.username, "failure")
        raise HTTPException(status_code=401, detail="invalid credentials")
    _login_clear(client_ip)
    csrf = new_token(16)
    sid = repo.create_session(
        row["id"], csrf, int(time.time()) + settings.session_ttl
    )
    response.set_cookie(
        "aura_session", sid, httponly=True, secure=settings.cookie_secure,
        samesite="strict", max_age=settings.session_ttl, path="/",
    )
    audit_event("login", body.username, "success", {"user_id": row["id"]})
    return {"csrf_token": csrf}


@app.post("/api/v1/logout")
def logout(principal: dict = Depends(authorize(human=True))):
    repo.delete_session(principal["session_id"])
    audit_event("logout", principal["id"], "success")
    out = JSONResponse(content={"status": "logged out"})
    out.delete_cookie("aura_session", path="/")
    return out


@app.get("/api/v1/me")
def me(principal: dict = Depends(authorize())):
    # ``csrf_token`` is additive and human-only: the token is issued once at
    # login and kept in the frontend's memory, so a refresh or a new tab used to
    # lose it while the ``aura_session`` cookie stayed valid, making every
    # mutation fail CSRF. Re-exposing it here lets the client recover the token
    # from its still-valid session without a new login. A CSRF token is not a
    # secret in the double-submit model (it is only meaningful paired with the
    # HttpOnly cookie and unreadable cross-origin); service identities have no
    # session and therefore receive ``null``.
    return {
        "id": principal["id"],
        "kind": principal["kind"],
        # Display name (additive): username for a person, identity name for a
        # service. Lets the UI say who is signed in instead of a generic label.
        "name": principal.get("name"),
        "role": principal["role"],
        "space_id": principal["space_id"],
        "application_id": principal["application_id"],
        "scopes": sorted(effective_scopes(principal)),
        "csrf_token": principal.get("csrf") if principal["kind"] == "user" else None,
    }


def _upload_headers(content_type: str) -> dict:
    """Headers the client must send with the presigned PUT (both are signed)."""
    return {"Content-Type": content_type, SSE_HEADER: SSE_ALGORITHM}


@app.post("/api/v1/objects/upload-intents")
def upload_intent(
    body: UploadIntentIn, request: Request,
    principal: dict = Depends(authorize(OBJECTS_WRITE)),
):
    space_id = principal["space_id"]
    if not space_id:
        raise HTTPException(status_code=403, detail="forbidden")
    # A disabled space refuses new uploads (#M04): existing objects stay readable
    # and downloadable, but the space is closed to new writes.
    if principal.get("space_status") not in (None, "active"):
        raise HTTPException(status_code=403, detail="forbidden")

    # Authoritative server-side transfer policy: media type, size ceiling and
    # empty-file rule. A malformed/blocked type or an out-of-range size is a 422
    # before any intent row or presigned URL exists. The browser checks are UX.
    try:
        content_type = validate_upload_policy(
            body.content_type, body.size_bytes,
            max_bytes=settings.max_object_bytes,
            allow_empty=settings.allow_empty_objects,
            allowed_types=settings.allowed_content_types,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    idem_key = request.headers.get("idempotency-key")
    if idem_key is not None and not valid_idempotency_key(
        idem_key, max_length=settings.idempotency_key_max
    ):
        raise HTTPException(status_code=422, detail="invalid idempotency key")

    if body.bucket:
        if repo.get_bucket_for_space(space_id, body.bucket) is None:
            # A client-supplied bucket that is not registered for this space is
            # refused outright (#36): no arbitrary bucket selection.
            raise HTTPException(status_code=403, detail="forbidden")
        bucket = body.bucket
    else:
        default_bucket = repo.get_default_bucket(space_id)
        if default_bucket is None:
            raise HTTPException(status_code=409, detail="no storage bucket configured")
        bucket = default_bucket["name"]

    # Idempotent replay: an accidental double submit carrying the same
    # Idempotency-Key returns the same pending intent with a freshly signed URL
    # instead of creating a second object row. The key is opaque and never
    # logged; only the replay outcome is audited.
    if idem_key:
        existing = repo.get_pending_by_idempotency(
            actor_id=principal["id"], idempotency_key=idem_key, space_id=space_id
        )
        if existing is not None:
            existing = _as_dict(existing)
            expires_at = existing.get("intent_expires_at")
            if not expires_at or int(time.time()) <= int(expires_at) + settings.intent_grace_s:
                expected = existing.get("expected_size_bytes")
                if expected is None or expected < 0:
                    expected = body.size_bytes
                url = presigned_put(
                    s3, existing["bucket"], existing["object_key"],
                    existing["content_type"], settings.intent_ttl, expected,
                )
                audit_event("upload-intent", principal["id"], "replay", {
                    "object_id": existing["id"], "bucket": existing["bucket"],
                })
                return {
                    "id": existing["id"], "status": PENDING, "method": "PUT",
                    "upload_url": url,
                    "headers": _upload_headers(existing["content_type"]),
                    "content_type": existing["content_type"],
                    "size_bytes": expected, "expires_in": settings.intent_ttl,
                    "expires_at": expires_at, "idempotent_replay": True,
                }
            # The prior intent is past its grace window: retire it so the
            # catalogue never keeps two live intents for one key.
            repo.set_status(existing["id"], EXPIRED)

    if repo.count_pending(**_object_scope(principal)) >= settings.max_pending_intents:
        raise HTTPException(status_code=429, detail="too many pending uploads")

    if body.folder_id is not None:
        # A client-supplied folder must exist and be owned by the caller (#61).
        # Service identities own no folders, so any folder_id they send is refused.
        if principal["kind"] != "user" or repo.get_folder(
            body.folder_id, owner_id=principal["id"]
        ) is None:
            raise HTTPException(status_code=403, detail="forbidden")

    owner_id = principal["id"] if principal["kind"] == "user" else None
    key = f"aura/{space_id}/{principal['id']}/{ids.new_id()}"
    intent_expires_at = int(time.time()) + settings.intent_ttl
    oid = repo.create_pending_object(
        space_id=space_id, application_id=principal["application_id"],
        environment=principal["environment"], bucket=bucket, object_key=key,
        display_name=body.display_name, content_type=content_type,
        owner_id=owner_id, actor_id=principal["id"], actor_kind=principal["kind"],
        intent_expires_at=intent_expires_at, folder_id=body.folder_id,
        expected_size_bytes=body.size_bytes, idempotency_key=idem_key,
    )
    url = presigned_put(
        s3, bucket, key, content_type, settings.intent_ttl, body.size_bytes
    )
    audit_event("upload-intent", principal["id"], "success", {
        "object_id": oid, "bucket": bucket, "expires_in": settings.intent_ttl,
        "size_bytes": body.size_bytes,
    })
    return {
        "id": oid, "status": PENDING, "method": "PUT", "upload_url": url,
        "headers": _upload_headers(content_type),
        "content_type": content_type, "size_bytes": body.size_bytes,
        "expires_in": settings.intent_ttl, "expires_at": intent_expires_at,
        "idempotent_replay": False,
    }


@app.post("/api/v1/objects/{oid}/commit")
def commit_object(oid: str, principal: dict = Depends(authorize(OBJECTS_WRITE))):
    oid = _canonical_id(oid, "object id")
    row = repo.get_object(oid, **_object_scope(principal))
    if row is None:
        raise HTTPException(status_code=404, detail="object not found")
    row = _as_dict(row)

    if row["status"] == "available":
        # Idempotent: a repeated commit returns the already-verified object (#37).
        return {
            "id": oid, "status": "available", "size_bytes": row["size_bytes"],
            "checksum_sha256": row["checksum_sha256"],
            "checksum_algorithm": "sha256",
        }
    if row["status"] != "pending":
        raise HTTPException(status_code=409, detail="object not committable")

    expires_at = row.get("intent_expires_at")
    if expires_at and int(time.time()) > int(expires_at) + settings.intent_grace_s:
        repo.set_status(oid, "expired")
        audit_event("commit", principal["id"], "expired", {"object_id": oid})
        raise HTTPException(status_code=410, detail="upload intent expired")

    try:
        head = s3ops.head_object(Bucket=row["bucket"], Key=row["object_key"])
    except Exception:
        raise HTTPException(status_code=422, detail="upload not found at storage")

    size = int(head.get("ContentLength", -1))
    if size < 0:
        raise HTTPException(status_code=422, detail="upload size unknown")
    if size > settings.max_object_bytes:
        repo.set_status(oid, "failed")
        raise HTTPException(status_code=413, detail="object exceeds maximum allowed size")
    if settings.require_sse and head.get("ServerSideEncryption") != SSE_ALGORITHM:
        # Never mark plaintext-at-rest bytes available; drop them best-effort
        # (the reconciliation sweep retries a failed delete).
        repo.set_status(oid, "failed")
        try:
            s3ops.delete_object(Bucket=row["bucket"], Key=row["object_key"])
        except Exception:
            pass
        audit_event("commit", principal["id"], "unencrypted", {"object_id": oid})
        raise HTTPException(status_code=422, detail="object not encrypted at rest")

    try:
        body = s3ops.get_object(Bucket=row["bucket"], Key=row["object_key"])["Body"]
        total, digest = stream_sha256(
            body, settings.max_object_bytes, settings.commit_chunk_bytes
        )
    except ObjectTooLarge:
        repo.set_status(oid, "failed")
        raise HTTPException(status_code=413, detail="object exceeds maximum allowed size")
    except Exception:
        raise HTTPException(status_code=422, detail="upload not readable at storage")

    if total != size:
        # Truncated or mismatched upload is never marked available (#37).
        repo.set_status(oid, "failed")
        raise HTTPException(status_code=422, detail="uploaded size does not match storage")

    repo.mark_available(oid, size, digest)
    audit_event("commit", principal["id"], "success", {"object_id": oid, "size_bytes": size})
    return {
        "id": oid, "status": "available", "size_bytes": size,
        "checksum_sha256": digest, "checksum_algorithm": "sha256",
    }


@app.post("/api/v1/objects/{oid}/abort")
def abort_object(oid: str, principal: dict = Depends(authorize(OBJECTS_WRITE))):
    """Cancel a pending upload intent and best-effort remove its partial bytes.

    This is the explicit backend cancellation mechanism the UI is allowed to use
    (``OBJECTS_WRITE``). It is idempotent: aborting an already aborted/expired
    intent returns the same terminal state. A committed (available) object cannot
    be aborted — that would be a delete, not a transfer cancellation — so it is
    refused with 409. Only the caller's own space/scope is visible (404 else).
    """
    oid = _canonical_id(oid, "object id")
    row = repo.get_object(oid, **_object_scope(principal))
    if row is None:
        raise HTTPException(status_code=404, detail="object not found")
    row = _as_dict(row)

    if row["status"] in (AVAILABLE, DELETED):
        raise HTTPException(status_code=409, detail="object not abortable")
    if row["status"] in (ABORTED, EXPIRED):
        # Idempotent: a repeated abort returns the existing terminal state.
        return {"id": oid, "status": row["status"], "aborted": True}
    if row["status"] != PENDING:
        raise HTTPException(status_code=409, detail="object not abortable")

    try:
        s3ops.delete_object(Bucket=row["bucket"], Key=row["object_key"])
    except Exception:
        # A transient storage error must not block the catalogue transition, but
        # this cleanup is best-effort only: once the row is marked ``aborted`` the
        # reconciliation sweep no longer selects it (api/lifecycle.py::
        # reconcile_pending only scans status='pending'), so the partial bytes are
        # orphaned and are NOT retried automatically. They are detected read-only
        # by GET /admin/orphans and must be cleaned operationally.
        pass
    repo.set_status(oid, ABORTED)
    audit_event("abort", principal["id"], "success", {"object_id": oid})
    return {"id": oid, "status": ABORTED, "aborted": True}


@app.get("/api/v1/objects")
def list_objects(
    principal: dict = Depends(authorize(OBJECTS_READ)), limit: int = 50, offset: int = 0,
    folder_id: str | None = None,
    q: str | None = Query(default=None, max_length=200),
    status: ObjectStatus | None = None,
    content_type: str | None = Query(default=None, max_length=127),
    min_size: int | None = Query(default=None, ge=0),
    max_size: int | None = Query(default=None, ge=0),
    created_after: int | None = Query(default=None, ge=0),
    created_before: int | None = Query(default=None, ge=0),
    sort: ObjectSort = "created_at",
    order: SortOrder = "desc",
):
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    # ``folder_id`` is an optional catalogue filter (#62): the literal "root"
    # selects objects not filed in any folder; a canonical uuid selects that
    # folder. Unknown or foreign folder ids simply match nothing (no leak).
    folder_filter: str | None = None
    if folder_id is not None:
        folder_filter = "" if folder_id == "root" else _canonical_id(folder_id, "folder id")
    # ``q`` is a case-insensitive display-name substring. It never touches the
    # internal object key, and a blank value is treated as "no search".
    needle = (q or "").strip() or None
    rows = repo.list_objects(
        **_object_scope(principal), folder_id=folder_filter, q=needle,
        status=status, hide_deleted=status is None,
        content_type=(content_type or None),
        min_size=min_size, max_size=max_size,
        created_after=created_after, created_before=created_before,
        sort=sort, order=order, limit=limit, offset=offset,
    )
    return {"items": [_object_public(r) for r in rows]}


@app.get("/api/v1/objects/{oid}")
def get_object(oid: str, principal: dict = Depends(authorize(OBJECTS_READ))):
    oid = _canonical_id(oid, "object id")
    row = repo.get_object(oid, **_object_scope(principal))
    if row is None:
        raise HTTPException(status_code=404, detail="object not found")
    return _object_public(row)


@app.delete("/api/v1/objects/{oid}")
def soft_delete_object(oid: str, principal: dict = Depends(authorize(OBJECTS_DELETE))):
    oid = _canonical_id(oid, "object id")
    row = repo.get_object(oid, **_object_scope(principal))
    if row is None:
        raise HTTPException(status_code=404, detail="object not found")
    row = _as_dict(row)
    if row["status"] not in ("available", "pending"):
        raise HTTPException(status_code=409, detail="object not deletable")
    repo.set_status(oid, "deleted")
    audit_event("delete", principal["id"], "success", {"object_id": oid})
    return {"id": oid, "status": "deleted"}


@app.post("/api/v1/objects/{oid}/restore")
def restore_object(oid: str, principal: dict = Depends(authorize(OBJECTS_DELETE))):
    # Undo a soft delete (trash). Same scope as the delete it reverses. Only a
    # committed object can come back: a pending intent that was soft-deleted
    # never received its bytes (size_bytes stays -1), so it is not restorable.
    oid = _canonical_id(oid, "object id")
    row = repo.get_object(oid, **_object_scope(principal))
    if row is None:
        raise HTTPException(status_code=404, detail="object not found")
    row = _as_dict(row)
    if row["status"] != DELETED:
        raise HTTPException(status_code=409, detail="object is not in the trash")
    if row["size_bytes"] < 0:
        raise HTTPException(status_code=409, detail="object was never uploaded")
    repo.set_status(oid, AVAILABLE)
    audit_event("restore", principal["id"], "success", {"object_id": oid})
    return _object_public(repo.get_object(oid, **_object_scope(principal)))


@app.patch("/api/v1/objects/{oid}")
def update_object(
    oid: str, body: ObjectPatchIn,
    principal: dict = Depends(authorize(OBJECTS_WRITE, human=True)),
):
    # File an object into a logical folder (or unfile it). Folders are owned by a
    # human user (#61), so this metadata move is a human-only capability; a
    # service identity has no folder to target and is refused by `human=True`.
    oid = _canonical_id(oid, "object id")
    if "folder_id" not in body.model_fields_set:
        raise HTTPException(status_code=422, detail="no fields to update")
    if repo.get_object(oid, **_object_scope(principal)) is None:
        # 404 (not 403) so the caller cannot probe for objects outside its scope.
        raise HTTPException(status_code=404, detail="object not found")
    if body.folder_id is not None and repo.get_folder(
        body.folder_id, owner_id=principal["id"]
    ) is None:
        # The target folder is owner-scoped: a foreign/unknown id is a 404.
        raise HTTPException(status_code=404, detail="folder not found")
    repo.set_object_folder(oid, folder_id=body.folder_id)
    audit_event("object.move", principal["id"], "success", {
        "object_id": oid, "folder_id": body.folder_id,
    })
    return _object_public(repo.get_object(oid, **_object_scope(principal)))


@app.post("/api/v1/objects/{oid}/purge")
def purge(oid: str, principal: dict = Depends(authorize(OBJECTS_PURGE))):
    # Destructive: requires objects:purge (operator/admin), not merely a session.
    # A storage failure keeps the catalogue row and surfaces 502 (M06): the
    # reference is never dropped on a failed backend delete.
    from .lifecycle import StorageDeleteError
    oid = _canonical_id(oid, "object id")
    try:
        purged = purge_object(repo, s3ops, oid, **_object_scope(principal))
    except StorageDeleteError:
        audit_event("purge", principal["id"], "storage_failure", {"object_id": oid})
        raise HTTPException(status_code=502, detail="storage delete failed")
    if not purged:
        raise HTTPException(status_code=404, detail="object not found")
    audit_event("purge", principal["id"], "success", {"object_id": oid})
    return {"id": oid, "status": "purged"}


@app.post("/api/v1/objects/{oid}/download-intent")
def download_intent(oid: str, principal: dict = Depends(authorize(OBJECTS_READ))):
    oid = _canonical_id(oid, "object id")
    row = repo.get_object(oid, **_object_scope(principal))
    if row is None:
        raise HTTPException(status_code=404, detail="object not found")
    row = _as_dict(row)
    if row["status"] != "available":
        raise HTTPException(status_code=409, detail="object not available")
    url = presigned_get(s3, row["bucket"], row["object_key"], settings.intent_ttl)
    audit_event("download-intent", principal["id"], "success", {"object_id": oid})
    return {
        "id": oid, "download_url": url, "method": "GET",
        "expires_in": settings.intent_ttl,
        "expires_at": int(time.time()) + settings.intent_ttl,
        "display_name": row["display_name"], "content_type": row["content_type"],
        "size_bytes": row["size_bytes"], "checksum_sha256": row["checksum_sha256"],
        "checksum_algorithm": "sha256",
    }


# Media types that may be rendered in place. Anything that can carry script or
# markup (text/html, image/svg+xml, xml, ...) is deliberately absent: a
# user-uploaded file must never execute from the storage origin. Everything not
# listed here is offered as a download instead (415 from preview-intent).
PREVIEWABLE_CONTENT_TYPES = frozenset({
    "image/png", "image/jpeg", "image/gif", "image/webp", "image/avif",
    "application/pdf",
    "audio/mpeg", "audio/mp3", "audio/wav", "audio/x-wav", "audio/ogg",
    "audio/webm", "audio/mp4", "audio/aac", "audio/flac",
})


def is_previewable(content_type: str) -> bool:
    """True when `content_type` (parameters ignored) may be previewed inline."""
    return (content_type or "").split(";", 1)[0].strip().lower() in PREVIEWABLE_CONTENT_TYPES


@app.post("/api/v1/objects/{oid}/preview-intent")
def preview_intent(oid: str, principal: dict = Depends(authorize(OBJECTS_READ))):
    # Mirror of download-intent: same scope, same 404 (foreign/unknown id is not
    # an existence oracle) and same 409 (only available objects are previewable).
    # The difference is the presigned GET is bound to ``Content-Disposition:
    # inline`` and the object's own content type, so the browser renders PDFs,
    # images and audio in place. Only the allow-listed types above are served
    # inline (415 otherwise: the frontend offers a download). No storage
    # coordinates are echoed back.
    oid = _canonical_id(oid, "object id")
    row = repo.get_object(oid, **_object_scope(principal))
    if row is None:
        raise HTTPException(status_code=404, detail="object not found")
    row = _as_dict(row)
    if row["status"] != "available":
        raise HTTPException(status_code=409, detail="object not available")
    if not is_previewable(row["content_type"]):
        raise HTTPException(status_code=415, detail="preview not supported for this content type")
    url = presigned_get(
        s3, row["bucket"], row["object_key"], settings.intent_ttl,
        content_disposition="inline", content_type=row["content_type"],
    )
    audit_event("preview-intent", principal["id"], "success", {"object_id": oid})
    return {
        "id": oid, "preview_url": url, "method": "GET",
        "expires_in": settings.intent_ttl,
        "expires_at": int(time.time()) + settings.intent_ttl,
        "display_name": row["display_name"], "content_type": row["content_type"],
        "size_bytes": row["size_bytes"],
    }


# ---------------------------------------------------------------------------
# Logical folders (#61). Human-owned catalogue metadata: no bytes, no host
# directories. A folder is addressed by its id and scoped to its owner, so one
# user can never observe or mutate another user's tree.
# ---------------------------------------------------------------------------
def _folder_owner(principal: dict) -> str:
    # ``folders.owner_id`` references ``users.id`` (NOT NULL), so folder
    # management is a human-user capability. Service identities are refused by
    # ``authorize(..., human=True)`` on every folder route below.
    return principal["id"]


def _folder_depth(fid: str, owner_id: str) -> int:
    """Number of ancestors+self for a folder; bounded by MAX_FOLDER_DEPTH."""
    depth = 0
    current: str | None = fid
    while current is not None:
        depth += 1
        if depth > MAX_FOLDER_DEPTH:
            return depth
        row = repo.get_folder(current, owner_id=owner_id)
        current = row["parent_id"] if row else None
    return depth


def _folder_subtree_height(fid: str, owner_id: str) -> int:
    """Longest downward path (in nodes) starting at ``fid``, including it."""
    height = 1
    frontier = [fid]
    while frontier:
        nxt: list[str] = []
        for node in frontier:
            nxt.extend(
                c["id"] for c in repo.list_folders(owner_id=owner_id, parent_id=node)
            )
        if nxt:
            height += 1
        frontier = nxt
    return height


@app.get("/api/v1/folders")
def list_folders(
    parent_id: str | None = None,
    all: bool = False,
    principal: dict = Depends(authorize(OBJECTS_READ, human=True)),
):
    owner_id = _folder_owner(principal)
    if all and parent_id is not None:
        raise HTTPException(
            status_code=422, detail="all cannot be combined with parent_id"
        )
    if all:
        # Flat tree in a single request: the client rebuilds the hierarchy from
        # parent_id instead of walking the per-level endpoint (#62 follow-up).
        rows = repo.list_all_folders(owner_id=owner_id)
        return {"items": [_folder_public(r) for r in rows]}
    if parent_id is not None:
        parent_id = _canonical_id(parent_id, "parent id")
        if repo.get_folder(parent_id, owner_id=owner_id) is None:
            raise HTTPException(status_code=404, detail="folder not found")
    rows = repo.list_folders(owner_id=owner_id, parent_id=parent_id)
    return {"items": [_folder_public(r) for r in rows]}


@app.get("/api/v1/folders/{fid}")
def get_folder(fid: str, principal: dict = Depends(authorize(OBJECTS_READ, human=True))):
    fid = _canonical_id(fid, "folder id")
    row = repo.get_folder(fid, owner_id=_folder_owner(principal))
    if row is None:
        raise HTTPException(status_code=404, detail="folder not found")
    return _folder_public(row)


@app.post("/api/v1/folders")
def create_folder(body: FolderIn, principal: dict = Depends(authorize(OBJECTS_WRITE, human=True))):
    owner_id = _folder_owner(principal)
    if body.parent_id is not None:
        if repo.get_folder(body.parent_id, owner_id=owner_id) is None:
            raise HTTPException(status_code=404, detail="parent folder not found")
        if _folder_depth(body.parent_id, owner_id) >= MAX_FOLDER_DEPTH:
            raise HTTPException(status_code=409, detail="folder depth limit reached")
    if repo.folder_name_exists(owner_id=owner_id, parent_id=body.parent_id, name=body.name):
        raise HTTPException(status_code=409, detail="folder name already exists")
    fid = repo.create_folder(owner_id, body.name, body.parent_id)
    audit_event("folder.create", principal["id"], "success", {"folder_id": fid})
    return _folder_public(repo.get_folder(fid, owner_id=owner_id))


@app.patch("/api/v1/folders/{fid}")
def update_folder(
    fid: str, body: FolderPatchIn,
    principal: dict = Depends(authorize(OBJECTS_WRITE, human=True)),
):
    fid = _canonical_id(fid, "folder id")
    owner_id = _folder_owner(principal)
    row = repo.get_folder(fid, owner_id=owner_id)
    if row is None:
        raise HTTPException(status_code=404, detail="folder not found")
    fields = body.model_fields_set
    if not fields:
        raise HTTPException(status_code=422, detail="no fields to update")
    if "name" in fields and body.name is None:
        raise HTTPException(status_code=422, detail="name cannot be null")
    new_name = body.name if "name" in fields else row["name"]
    new_parent = body.parent_id if "parent_id" in fields else row["parent_id"]

    if "parent_id" in fields and new_parent is not None:
        if new_parent == fid:
            raise HTTPException(status_code=409, detail="folder cannot be its own parent")
        # Walk up from the prospective parent: reaching `fid` would create a cycle.
        current: str | None = new_parent
        depth = 0
        while current is not None:
            if current == fid:
                raise HTTPException(
                    status_code=409, detail="cannot move a folder into its own subtree"
                )
            depth += 1
            if depth > MAX_FOLDER_DEPTH:
                raise HTTPException(status_code=409, detail="folder depth limit reached")
            parent_row = repo.get_folder(current, owner_id=owner_id)
            if parent_row is None:
                raise HTTPException(status_code=404, detail="parent folder not found")
            current = parent_row["parent_id"]
        # Moving the whole subtree here must not push any descendant past the cap.
        if depth + _folder_subtree_height(fid, owner_id) > MAX_FOLDER_DEPTH:
            raise HTTPException(status_code=409, detail="folder depth limit reached")

    if (new_name != row["name"] or new_parent != row["parent_id"]) and (
        repo.folder_name_exists(
            owner_id=owner_id, parent_id=new_parent, name=new_name, exclude_id=fid
        )
    ):
        raise HTTPException(status_code=409, detail="folder name already exists")

    if "name" in fields and new_name != row["name"]:
        repo.rename_folder(fid, owner_id=owner_id, name=new_name)
    if "parent_id" in fields and new_parent != row["parent_id"]:
        repo.set_folder_parent(fid, owner_id=owner_id, parent_id=new_parent)
    audit_event("folder.update", principal["id"], "success", {"folder_id": fid})
    return _folder_public(repo.get_folder(fid, owner_id=owner_id))


@app.delete("/api/v1/folders/{fid}")
def delete_folder(fid: str, principal: dict = Depends(authorize(OBJECTS_DELETE, human=True))):
    fid = _canonical_id(fid, "folder id")
    owner_id = _folder_owner(principal)
    if repo.get_folder(fid, owner_id=owner_id) is None:
        raise HTTPException(status_code=404, detail="folder not found")
    if repo.folder_child_count(fid, owner_id=owner_id) > 0:
        raise HTTPException(status_code=409, detail="folder is not empty")
    if repo.folder_object_count(fid, owner_id=owner_id) > 0:
        raise HTTPException(status_code=409, detail="folder is not empty")
    repo.delete_folder(fid, owner_id=owner_id)
    audit_event("folder.delete", principal["id"], "success", {"folder_id": fid})
    return {"id": fid, "status": "deleted"}


# ---------------------------------------------------------------------------
# Administration (#35, #36, #38): require the admin:* capabilities.
# ---------------------------------------------------------------------------
@app.post("/api/v1/admin/reconcile")
def reconcile(limit: int = 500,
              principal: dict = Depends(authorize(ADMIN_WRITE))):
    # Bounded sweep (#87): at most `limit` stale intents per run so a huge
    # backlog cannot monopolize the API; repeat until expired is 0.
    limit = max(1, min(limit, 5000))
    # Derive the staleness window from the same settings that govern intent
    # death (commit is accepted until intent_ttl + intent_grace_s), instead of
    # relying on reconcile_pending's own default: an intent must never be swept
    # while its grace window could still accept a legitimate commit.
    expire_after_s = settings.intent_ttl + settings.intent_grace_s
    out = reconcile_pending(repo, s3ops, expire_after_s=expire_after_s, limit=limit)
    audit_event("admin.reconcile", principal["id"], "success", out)
    return out


@app.get("/api/v1/admin/orphans")
def list_orphan_candidates(limit: int = 100,
                           principal: dict = Depends(authorize(ADMIN_READ))):
    """Read-only orphan candidates (#87): `available` rows whose bytes are
    missing at storage. Candidates only — nothing is ever deleted on this
    signal alone; a transient outage also produces candidates, so an operator
    confirms before any destructive action. Bounded scan, oldest first.
    """
    limit = max(1, min(limit, 1000))
    checked = 0
    candidates = []
    for row in repo.list_available(limit=limit):
        row = _as_dict(row)
        checked += 1
        try:
            s3ops.head_object(Bucket=row["bucket"], Key=row["object_key"])
        except Exception:
            candidates.append({
                "id": row["id"], "display_name": row["display_name"],
                "space_id": row["space_id"],
                "application_id": row["application_id"],
                "size_bytes": row["size_bytes"],
                "updated_at": row["updated_at"],
            })
    return {"checked": checked, "candidates": candidates,
            "checked_at": int(time.time())}


@app.get("/api/v1/admin/stats")
def admin_stats(principal: dict = Depends(authorize(ADMIN_READ))):
    return repo.stats()


# ---------------------------------------------------------------------------
# Observability (M07): audit search, usage, supervision. Reads require
# admin:read; retention pruning requires admin:write. Every value returned is
# measured live from the catalogue, the audit file or the OS — see
# docs/OBSERVABILITY.md for what is (and is not) available.
# ---------------------------------------------------------------------------
@app.get("/api/v1/admin/audit")
def search_audit(
    since: int | None = Query(default=None, ge=0),
    until: int | None = Query(default=None, ge=0),
    actor: str | None = Query(default=None, max_length=200),
    action: str | None = Query(default=None, max_length=100),
    result: str | None = Query(default=None, max_length=50),
    space_id: str | None = None,
    application_id: str | None = None,
    request_id: str | None = Query(default=None, max_length=128),
    limit: int = 100,
    offset: int = 0,
    principal: dict = Depends(authorize(ADMIN_READ)),
):
    # Search the append-only audit log newest-first. `total` counts matches
    # inside the scanned tail window; `truncated` warns that older history
    # exists beyond it. A missing log file is an empty history, never an
    # error. Pagination: limit is clamped to 1..500, offset counts matches.
    if space_id is not None:
        space_id = _canonical_id(space_id, "space id")
    if application_id is not None:
        application_id = _canonical_id(application_id, "application id")
    if since is not None and until is not None and since > until:
        raise HTTPException(
            status_code=422, detail="since must not exceed until"
        )
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    out = query_audit_log(
        since=since, until=until, actor=actor or None, action=action or None,
        result=result or None, space_id=space_id,
        application_id=application_id, request_id=request_id or None,
        limit=limit, offset=offset,
        max_scan_lines=settings.audit_max_scan_lines,
    )
    return {
        **out, "retention_days": settings.audit_retention_days,
        "queried_at": int(time.time()),
    }


@app.post("/api/v1/admin/audit/prune")
def prune_audit(principal: dict = Depends(authorize(ADMIN_WRITE))):
    # Enforce the retention window: drop lines older than
    # AURA_AUDIT_RETENTION_DAYS. Lines without a valid timestamp are kept,
    # so audit data is never deleted silently.
    try:
        out = prune_audit_log(retention_days=settings.audit_retention_days)
    except OSError:
        raise HTTPException(status_code=503, detail="audit log unavailable")
    audit_event("admin.audit.prune", principal["id"], "success", out)
    return out


@app.get("/api/v1/admin/usage")
def admin_usage(
    space_id: str | None = None,
    principal: dict = Depends(authorize(ADMIN_READ)),
):
    # Real consumption from the catalogue (M07): verified bytes are
    # `available` objects only; pending intents carry no committed bytes.
    # `measured_at` is the live query time (freshness), `limits` the
    # configured technical ceilings. No bandwidth or capacity figures exist
    # offline — they are documented as unavailable, never invented.
    if space_id is not None:
        space_id = _canonical_id(space_id, "space id")
        space = repo.get_space(space_id)
        if space is None:
            raise HTTPException(status_code=404, detail="space not found")
        spaces = [space]
    else:
        spaces = repo.list_spaces()
    applications = {a["id"]: a for a in repo.list_applications()}
    items = []
    totals_by_status: dict[str, int] = {}
    total_bytes = 0
    total_available = 0
    total_pending = 0
    for space in spaces:
        space = _as_dict(space)
        usage = repo.space_usage(space["id"])
        for status, count in usage["objects_by_status"].items():
            totals_by_status[status] = totals_by_status.get(status, 0) + count
        total_bytes += usage["stored_bytes"]
        total_available += usage["available_count"]
        total_pending += usage["pending_count"]
        application = applications.get(space["application_id"])
        items.append({
            "space_id": space["id"], "space_name": space["name"],
            "environment": space["environment"],
            "application_id": space["application_id"],
            "application_name": application["name"] if application else None,
            **usage,
        })
    return {
        "measured_at": int(time.time()),
        "limits": {
            "max_object_bytes": settings.max_object_bytes,
            "max_pending_intents": settings.max_pending_intents,
            "intent_ttl": settings.intent_ttl,
            "allow_empty_objects": settings.allow_empty_objects,
        },
        "totals": {
            "objects_by_status": totals_by_status,
            "available_count": total_available,
            "pending_count": total_pending,
            "stored_bytes": total_bytes,
        },
        "spaces": items,
    }


@app.get("/api/v1/admin/health")
def admin_health(principal: dict = Depends(authorize(ADMIN_READ))):
    """Supervision snapshot (M07): process, database, storage, local disk.

    Always 200 with per-component statuses (`ok`/`down`); the top-level
    status is `degraded` when any dependency fails. The storage check proves
    S3 reachability, not a full upload/download round-trip — that proof is
    the live S3 smoke test, unavailable offline.
    """
    checked_at = int(time.time())
    components: dict[str, dict] = {
        "api": {
            "status": "ok", "uptime_s": checked_at - _BOOT_TIME,
            "version": app.version,
        },
    }
    start = time.monotonic()
    try:
        repo.ping()
        components["database"] = {
            "status": "ok",
            "latency_ms": int((time.monotonic() - start) * 1000),
        }
    except Exception:
        components["database"] = {"status": "down", "error": "unreachable"}
    start = time.monotonic()
    try:
        s3ops.list_buckets()
        components["storage"] = {
            "status": "ok",
            "latency_ms": int((time.monotonic() - start) * 1000),
            "note": "S3 reachable; not a full upload/download round-trip",
        }
    except Exception:
        components["storage"] = {"status": "down", "error": "unreachable"}
    try:
        disk = shutil.disk_usage(audit_path())
        components["disk"] = {
            "status": "ok", "total_bytes": disk.total,
            "used_bytes": disk.used, "free_bytes": disk.free,
            "note": "local process volume; not the object-store capacity",
        }
    except OSError:
        components["disk"] = {"status": "down", "error": "unreadable"}
    errors, scanned = recent_error_events(
        window_s=settings.audit_error_window_s, limit=5,
        max_scan_lines=settings.audit_error_scan_lines,
    )
    degraded = any(c.get("status") != "ok" for c in components.values())
    return {
        "status": "degraded" if degraded else "ok",
        "checked_at": checked_at,
        "components": components,
        "recent_errors": {
            "window_s": settings.audit_error_window_s,
            "events": errors, "scanned": scanned,
        },
        "note": (
            "Degraded storage means uploads/downloads will fail; full "
            "data-plane proof requires the live S3 smoke test."
        ),
    }


@app.post("/api/v1/admin/applications")
def create_application(body: ApplicationIn, principal: dict = Depends(authorize(ADMIN_WRITE))):
    if repo.get_application_by_name(body.name) is not None:
        raise HTTPException(status_code=409, detail="application already exists")
    aid = repo.create_application(body.name)
    audit_event("admin.application.create", principal["id"], "success", {"application_id": aid})
    return _application_public(repo.get_application(aid))


@app.get("/api/v1/admin/applications")
def list_applications(principal: dict = Depends(authorize(ADMIN_READ))):
    return {"items": [_application_public(a) for a in repo.list_applications()]}


@app.get("/api/v1/admin/applications/{aid}")
def get_application(aid: str, principal: dict = Depends(authorize(ADMIN_READ))):
    aid = _canonical_id(aid, "application id")
    row = repo.get_application(aid)
    if row is None:
        raise HTTPException(status_code=404, detail="application not found")
    # The stable identifier is returned verbatim; it is never derived from name.
    out = _application_public(row)
    out["space_count"] = repo.count_spaces_for_application(aid)
    return out


@app.patch("/api/v1/admin/applications/{aid}")
def update_application(
    aid: str, body: ApplicationPatchIn,
    principal: dict = Depends(authorize(ADMIN_WRITE)),
):
    aid = _canonical_id(aid, "application id")
    row = repo.get_application(aid)
    if row is None:
        raise HTTPException(status_code=404, detail="application not found")
    fields = body.model_fields_set
    if not fields:
        raise HTTPException(status_code=422, detail="no fields to update")
    if "name" in fields and body.name is None:
        raise HTTPException(status_code=422, detail="name cannot be null")
    if "status" in fields and body.status is None:
        raise HTTPException(status_code=422, detail="status cannot be null")
    if "name" in fields and body.name != row["name"]:
        if repo.application_name_exists(body.name, exclude_id=aid):
            raise HTTPException(status_code=409, detail="application name already exists")
    repo.update_application(
        aid,
        name=body.name if "name" in fields else None,
        status=body.status if "status" in fields else None,
    )
    audit_event("admin.application.update", principal["id"], "success",
                {"application_id": aid})
    out = _application_public(repo.get_application(aid))
    out["space_count"] = repo.count_spaces_for_application(aid)
    return out


def _space_detail(sid: str) -> dict:
    """Space detail with its application, buckets and attachment counts."""
    row = repo.get_space(sid)
    out = _space_public(row)
    application = repo.get_application(row["application_id"])
    out["application_name"] = application["name"] if application else None
    out["buckets"] = [_bucket_public(b) for b in repo.list_buckets(sid)]
    out["counts"] = repo.space_counts(sid)
    return out


@app.post("/api/v1/admin/spaces")
def create_space(body: SpaceIn, principal: dict = Depends(authorize(ADMIN_WRITE))):
    application = repo.get_application(body.application_id)
    if application is None:
        raise HTTPException(status_code=404, detail="application not found")
    if application["status"] != "active":
        # A space may only be attached to an allowed (active) application.
        raise HTTPException(status_code=409, detail="application is not active")
    if repo.space_exists(body.application_id, body.name, body.environment):
        # The unique (application_id, name, environment) is enforced up front so a
        # duplicate is a 409, never a database integrity error.
        raise HTTPException(status_code=409, detail="space already exists")
    bucket = body.default_bucket or f"aura-{body.name}-{body.environment}"
    if not is_valid_bucket_name(bucket):
        # Refuse up front rather than register a bucket name S3 will reject (#41).
        raise HTTPException(status_code=422, detail="invalid bucket name")
    if repo.get_bucket_by_name(bucket) is not None:
        raise HTTPException(status_code=409, detail="bucket name already in use")
    sid = repo.create_space(body.application_id, body.name, body.environment)
    repo.create_bucket(sid, bucket, is_default=True)
    audit_event("admin.space.create", principal["id"], "success", {"space_id": sid})
    return {**_space_detail(sid), "bucket": bucket}


@app.get("/api/v1/admin/spaces")
def list_spaces(principal: dict = Depends(authorize(ADMIN_READ))):
    return {"items": [_space_public(s) for s in repo.list_spaces()]}


@app.get("/api/v1/admin/spaces/{sid}")
def get_space(sid: str, principal: dict = Depends(authorize(ADMIN_READ))):
    sid = _canonical_id(sid, "space id")
    if repo.get_space(sid) is None:
        raise HTTPException(status_code=404, detail="space not found")
    return _space_detail(sid)


@app.patch("/api/v1/admin/spaces/{sid}")
def update_space(
    sid: str, body: SpacePatchIn,
    principal: dict = Depends(authorize(ADMIN_WRITE)),
):
    sid = _canonical_id(sid, "space id")
    row = repo.get_space(sid)
    if row is None:
        raise HTTPException(status_code=404, detail="space not found")
    fields = body.model_fields_set
    if not fields:
        raise HTTPException(status_code=422, detail="no fields to update")
    if "name" in fields and body.name is None:
        raise HTTPException(status_code=422, detail="name cannot be null")
    if "environment" in fields and body.environment is None:
        raise HTTPException(status_code=422, detail="environment cannot be null")
    if "status" in fields and body.status is None:
        raise HTTPException(status_code=422, detail="status cannot be null")
    new_name = body.name if "name" in fields else row["name"]
    new_environment = (
        body.environment if "environment" in fields else row["environment"]
    )
    # `application_id` is immutable (it is not an editable field), so uniqueness
    # is only ever re-checked inside the same application.
    if (new_name, new_environment) != (row["name"], row["environment"]):
        if repo.space_exists(
            row["application_id"], new_name, new_environment, exclude_id=sid
        ):
            raise HTTPException(status_code=409, detail="space already exists")
    repo.update_space(
        sid,
        name=new_name if "name" in fields else None,
        environment=new_environment if "environment" in fields else None,
        status=body.status if "status" in fields else None,
    )
    audit_event("admin.space.update", principal["id"], "success", {"space_id": sid})
    return _space_detail(sid)


@app.post("/api/v1/admin/spaces/{sid}/buckets")
def create_space_bucket(
    sid: str, body: BucketIn,
    principal: dict = Depends(authorize(ADMIN_WRITE)),
):
    # Register an extra bucket for a space. The bucket name is validated against
    # the S3 rules and must be globally unique; a bucket is never made the default
    # here (that is an explicit, separate action) and never deleted (#M04).
    sid = _canonical_id(sid, "space id")
    if repo.get_space(sid) is None:
        raise HTTPException(status_code=404, detail="space not found")
    if not is_valid_bucket_name(body.name):
        raise HTTPException(status_code=422, detail="invalid bucket name")
    if repo.get_bucket_by_name(body.name) is not None:
        raise HTTPException(status_code=409, detail="bucket name already in use")
    bid = repo.create_bucket(sid, body.name, is_default=False)
    audit_event("admin.bucket.create", principal["id"], "success",
                {"bucket_id": bid, "space_id": sid})
    return _bucket_public(repo.get_bucket(bid))


@app.get("/api/v1/admin/buckets")
def list_buckets(principal: dict = Depends(authorize(ADMIN_READ))):
    # Every registered bucket with its space/application attachment. The payload
    # has no S3 credential field: only the bucket name and its owner are exposed.
    spaces = {s["id"]: s for s in repo.list_spaces()}
    applications = {a["id"]: a for a in repo.list_applications()}
    items = []
    for bucket in repo.list_all_buckets():
        space = spaces.get(bucket["space_id"])
        application = applications.get(space["application_id"]) if space else None
        items.append({
            **_bucket_public(bucket),
            "space_name": space["name"] if space else None,
            "environment": space["environment"] if space else None,
            "application_id": space["application_id"] if space else None,
            "application_name": application["name"] if application else None,
        })
    return {"items": items}


@app.patch("/api/v1/admin/buckets/{bid}")
def update_bucket(
    bid: str, body: BucketPatchIn,
    principal: dict = Depends(authorize(ADMIN_WRITE)),
):
    # Toggle the default flag. Setting a bucket default clears the flag on the
    # other buckets of the same space, so exactly one default is possible; the
    # existing objects keep their own bucket locator, so the change is reversible.
    bid = _canonical_id(bid, "bucket id")
    bucket = repo.get_bucket(bid)
    if bucket is None:
        raise HTTPException(status_code=404, detail="bucket not found")
    repo.set_default_bucket(bucket["space_id"], bid, is_default=body.is_default)
    audit_event("admin.bucket.default", principal["id"], "success", {
        "bucket_id": bid, "space_id": bucket["space_id"],
        "is_default": body.is_default,
    })
    return _bucket_public(repo.get_bucket(bid))


@app.post("/api/v1/admin/identities")
def create_identity(body: IdentityIn, principal: dict = Depends(authorize(ADMIN_WRITE))):
    space = repo.get_space(body.space_id)
    if space is None:
        raise HTTPException(status_code=404, detail="space not found")
    if space.get("status") not in (None, "active"):
        # No new identity may be minted for a disabled space (#M04).
        raise HTTPException(status_code=409, detail="space is not active")
    scopes = set(body.scopes)
    unknown = scopes - ALL_SCOPES
    if unknown:
        raise HTTPException(status_code=422, detail="unknown scopes requested")
    forbidden = scopes - SERVICE_SCOPES
    if forbidden:
        # Machine identities are data-plane only: administration stays with a
        # human operator/admin session (see docs/AUTHORIZATION-MATRIX.md).
        raise HTTPException(
            status_code=422, detail="scopes not allowed for service identities"
        )
    identity_id = repo.new_id()
    secret = new_secret()
    expires_at = int(time.time()) + body.ttl_seconds if body.ttl_seconds else None
    repo.create_identity(
        identity_id, space["application_id"], body.space_id, body.name,
        fingerprint(secret), ",".join(sorted(scopes)), expires_at,
    )
    token = format_token(identity_id, secret)
    audit_event("admin.identity.create", principal["id"], "success",
                {"identity_id": identity_id, "space_id": body.space_id})
    # The token is returned exactly once; it is never logged or stored in clear.
    return {
        "id": identity_id, "name": body.name, "space_id": body.space_id,
        "application_id": space["application_id"], "scopes": sorted(scopes),
        "expires_at": expires_at, "token": token,
    }


@app.get("/api/v1/admin/identities")
def list_identities(space_id: str | None = None,
                    principal: dict = Depends(authorize(ADMIN_READ))):
    if space_id is not None:
        space_id = _canonical_id(space_id, "space id")
    items = []
    for i in repo.list_identities(space_id):
        items.append({
            "id": i["id"], "name": i["name"], "space_id": i["space_id"],
            "application_id": i["application_id"],
            "scopes": sorted(s for s in (i.get("scopes") or "").split(",") if s),
            "created_at": i["created_at"], "expires_at": i.get("expires_at"),
            "last_used_at": i.get("last_used_at"), "revoked_at": i.get("revoked_at"),
            "rotated_at": i.get("rotated_at"),
        })
    return {"items": items}


@app.post("/api/v1/admin/identities/{iid}/rotate")
def rotate_identity(iid: str, principal: dict = Depends(authorize(ADMIN_WRITE))):
    iid = _canonical_id(iid, "identity id")
    row = repo.get_identity(iid)
    if row is None or row.get("revoked_at"):
        raise HTTPException(status_code=404, detail="identity not found")
    secret = new_secret()
    repo.update_identity_secret(iid, fingerprint(secret))
    audit_event("admin.identity.rotate", principal["id"], "success", {"identity_id": iid})
    return {"id": iid, "token": format_token(iid, secret)}


@app.post("/api/v1/admin/identities/{iid}/revoke")
def revoke_identity(iid: str, principal: dict = Depends(authorize(ADMIN_WRITE))):
    iid = _canonical_id(iid, "identity id")
    row = repo.get_identity(iid)
    if row is None:
        raise HTTPException(status_code=404, detail="identity not found")
    repo.revoke_identity(iid)
    audit_event("admin.identity.revoke", principal["id"], "success", {"identity_id": iid})
    return {"id": iid, "revoked": True}
