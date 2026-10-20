"""Environment-driven settings. No secrets are hard-coded; all secrets come from env."""
import os


def _req(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise RuntimeError(f"Environment variable {name} must be an integer")


class Settings:
    # Admin bootstrap credential (initial password; hashed into the DB on boot).
    admin_user: str = os.environ.get("AURA_API_ADMIN_USER", "admin")
    admin_password: str = _req("AURA_API_ADMIN_PASSWORD")
    # S3 data plane (server-side only; never sent to browsers).
    # Public base embedded in presigned URLs (browser-reachable, no call made).
    s3_endpoint: str = os.environ.get("AURA_S3_ENDPOINT", "https://aura-storage.eurinhash.com")
    # Internal endpoint for server-side S3 calls (no egress needed).
    s3_internal_endpoint: str = os.environ.get(
        "AURA_S3_INTERNAL_ENDPOINT", "http://seaweed-filer:8333"
    )
    s3_access_key: str = _req("AURA_S3_ACCESS_KEY")
    s3_secret_key: str = _req("AURA_S3_SECRET_KEY")
    s3_region: str = os.environ.get("AURA_S3_REGION", "us-east-1")
    # Catalogue database path (SQLite for #18; PostgreSQL replaces it in #19).
    db_path: str = os.environ.get("AURA_API_DB", "aura-catalogue.db")
    # Backend: 'sqlite' (default, dev/test) or 'postgres' (pilot, #19).
    db_backend: str = os.environ.get("AURA_API_BACKEND", "sqlite")
    # PostgreSQL DSN, e.g. postgresql://aura:PASSWORD@localhost:5432/aura.
    # Only used when backend is 'postgres'. Never logged.
    db_dsn: str = os.environ.get("AURA_API_DSN", "")
    # Lifetimes (seconds).
    # Human session cookie lifetime: 90 days by default. A long session widens the
    # window during which a stolen cookie stays valid; that is compensated by the
    # HttpOnly + Secure + SameSite=Strict cookie and the CSRF double-submit enforced
    # in api/main.py. The expiry is fixed at login (no rolling renewal), so logout
    # or TTL expiry truly ends the session. Shorten via AURA_SESSION_TTL if needed.
    session_ttl: int = _int("AURA_SESSION_TTL", 7776000)
    intent_ttl: int = _int("AURA_INTENT_TTL", 900)
    # Grace period after intent_ttl during which a commit is still accepted
    # (upload may finish just before expiry; #37).
    intent_grace_s: int = _int("AURA_INTENT_GRACE_SECONDS", 300)
    # Audit log (JSON-lines, IDs and statuses only — never secrets or URLs).
    audit_log: str = os.environ.get("AURA_AUDIT_LOG", "aura-audit.jsonl")
    # Login rate limit (failed attempts per window, per client IP). A successful
    # login clears the counter for that client.
    login_rate_limit: int = _int("AURA_LOGIN_RATE_LIMIT", 10)
    login_rate_window: int = _int("AURA_LOGIN_RATE_WINDOW", 60)
    # Machine-to-machine authentication failure rate limit (per client IP; #38).
    m2m_rate_limit: int = _int("AURA_M2M_RATE_LIMIT", 60)
    m2m_rate_window: int = _int("AURA_M2M_RATE_WINDOW", 60)
    # Reverse proxies trusted to set X-Forwarded-For when resolving the client
    # IP for the rate limiters (#35/#38). Comma-separated IPs or CIDRs; defaults
    # to the loopback/RFC1918 ranges Docker uses, since the API is only reachable
    # on the internal network. See api/netinfo.py.
    trusted_proxies: tuple[str, ...] = tuple(
        part.strip()
        for part in os.environ.get(
            "AURA_TRUSTED_PROXIES",
            "127.0.0.0/8,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16",
        ).split(",")
        if part.strip()
    )
    # Upload/commit technical limits (#37, #40). Defaults are guard rails, not billing.
    #
    # ``max_object_bytes`` is the single-request upload ceiling. It must stay at
    # or below the S3 backend's single-PUT limit (SeaweedFS/AWS: 5 GiB) until the
    # multipart workflow is implemented and compatibility-tested; see
    # docs/TRANSFER-ENGINE.md. The default equals that limit on purpose.
    max_object_bytes: int = _int("AURA_MAX_OBJECT_BYTES", 5 * 1024 * 1024 * 1024)
    # Multipart upload (#72). Off by default until the compatibility gate in
    # docs/MULTIPART-UPLOAD-PLAN.md is recorded on the pinned SeaweedFS image.
    # When on, files at or above the threshold are sent as independently
    # retried parts and may exceed ``max_object_bytes`` up to the multipart cap.
    multipart_enabled: bool = os.environ.get("AURA_MULTIPART_ENABLED", "0") == "1"
    multipart_threshold_bytes: int = _int("AURA_MULTIPART_THRESHOLD_BYTES", 100 * 1024 * 1024)
    # Each part is one request through the edge proxy: keep part / slowest
    # supported rate well under Traefik's readTimeout (see the plan).
    multipart_part_bytes: int = _int("AURA_MULTIPART_PART_BYTES", 16 * 1024 * 1024)
    multipart_max_object_bytes: int = _int(
        "AURA_MULTIPART_MAX_OBJECT_BYTES", 1024 * 1024 * 1024 * 1024
    )
    # A multipart session outlives a single-PUT intent: a large upload may be
    # paused (reload, sleep, network loss) and resumed within this window.
    multipart_session_ttl: int = _int("AURA_MULTIPART_SESSION_TTL", 7 * 24 * 3600)
    # Asynchronous integrity check (N5). At or above this size, commit answers
    # 202 'verifying' and a worker hashes the object outside the request.
    verify_async_threshold_bytes: int = _int(
        "AURA_VERIFY_ASYNC_THRESHOLD_BYTES", 256 * 1024 * 1024
    )
    # "inline": a worker thread inside the API process (default). "off": run
    # `python -m api.verifier` as its own process instead.
    verify_worker: str = os.environ.get("AURA_VERIFY_WORKER", "inline")
    verify_lease_s: int = _int("AURA_VERIFY_LEASE_SECONDS", 300)
    verify_heartbeat_bytes: int = _int("AURA_VERIFY_HEARTBEAT_BYTES", 256 * 1024 * 1024)
    verify_max_attempts: int = _int("AURA_VERIFY_MAX_ATTEMPTS", 8)
    verify_poll_s: int = _int("AURA_VERIFY_POLL_SECONDS", 2)
    # Capacity guard: the usable object-store capacity in bytes (0 = off) and
    # the fill level, in percent, above which new uploads are refused (507).
    # Every upload reserves its declared size from the moment its intent is
    # issued, so concurrent imports cannot overfill the single disk.
    storage_capacity_bytes: int = _int("AURA_STORAGE_CAPACITY_BYTES", 0)
    storage_high_watermark_pct: int = _int("AURA_STORAGE_HIGH_WATERMARK", 90)
    # Periodic integrity scrub (api/scrubber.py): every available object is
    # re-hashed at least once per interval, oldest check first, within a byte
    # budget per run so the disk is never saturated. Read-only: it reports,
    # it never deletes or rewrites.
    scrub_enabled: bool = os.environ.get("AURA_SCRUB_ENABLED", "1") == "1"
    scrub_interval_days: int = _int("AURA_SCRUB_INTERVAL_DAYS", 30)
    scrub_bytes_per_run: int = _int("AURA_SCRUB_BYTES_PER_RUN", 10 * 1024 ** 3)
    scrub_period_s: int = _int("AURA_SCRUB_PERIOD_SECONDS", 3600)
    # Storage orphans (api/storage_reconcile.py): bytes younger than this are
    # never reported or removed, so an upload in flight is never mistaken for one.
    orphan_grace_s: int = _int("AURA_ORPHAN_GRACE_SECONDS", 86400)
    commit_chunk_bytes: int = _int("AURA_COMMIT_CHUNK_BYTES", 1024 * 1024)
    max_pending_intents: int = _int("AURA_MAX_PENDING_INTENTS", 100)
    # Zero-byte objects are refused by default: an empty upload is usually a
    # client bug rather than intent. Enable only when a caller legitimately needs
    # empty placeholders (the commit path verifies the empty SHA-256 either way).
    allow_empty_objects: bool = os.environ.get("AURA_ALLOW_EMPTY_OBJECTS", "0") == "1"
    # Optional server-side content-type allow-list, comma-separated and
    # case-insensitive (``image/png,application/pdf,text/*``). Empty means every
    # syntactically valid media type is accepted. This is a server-enforced
    # control: a frontend ``accept`` attribute is never trusted as a policy.
    allowed_content_types: tuple[str, ...] = tuple(
        part.strip().lower()
        for part in os.environ.get("AURA_ALLOWED_CONTENT_TYPES", "").split(",")
        if part.strip()
    )
    # Audit observability bounds (M07). Reads scan a bounded tail so a large
    # history cannot exhaust the API; retention pruning drops only lines older
    # than the window and never lines without a valid timestamp.
    audit_retention_days: int = _int("AURA_AUDIT_RETENTION_DAYS", 90)
    audit_max_scan_lines: int = _int("AURA_AUDIT_MAX_SCAN_LINES", 20000)
    audit_error_scan_lines: int = _int("AURA_AUDIT_ERROR_SCAN_LINES", 5000)
    audit_error_window_s: int = _int("AURA_AUDIT_ERROR_WINDOW_S", 3600)
    # Upper bound (characters) for a client-supplied ``Idempotency-Key`` header.
    idempotency_key_max: int = _int("AURA_IDEMPOTENCY_KEY_MAX", 200)
    # Refuse to commit an object the storage does not report as encrypted at
    # rest (SSE-S3 ``AES256``). Disable only against a backend without SSE-S3.
    require_sse: bool = os.environ.get("AURA_REQUIRE_SSE", "1") == "1"
    # Cookie security.
    cookie_secure: bool = os.environ.get("AURA_COOKIE_SECURE", "1") == "1"


settings = Settings()

# Fail fast on misconfigured audit observability bounds (M07). A zero/negative
# scan window would silently disable reads and a negative retention would make
# pruning unbounded, so refuse the process at import instead of degrading at
# runtime. The readers in api/audit.py are also hardened defensively.
if settings.audit_retention_days < 1:
    raise RuntimeError("AURA_AUDIT_RETENTION_DAYS must be >= 1")
if settings.audit_max_scan_lines < 1:
    raise RuntimeError("AURA_AUDIT_MAX_SCAN_LINES must be >= 1")
if settings.audit_error_scan_lines < 1:
    raise RuntimeError("AURA_AUDIT_ERROR_SCAN_LINES must be >= 1")
if settings.audit_error_window_s < 0:
    raise RuntimeError("AURA_AUDIT_ERROR_WINDOW_S must be >= 0")
