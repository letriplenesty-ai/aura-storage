"""Transfer policy: content-type allow-list, size/empty rules, status vocabulary.

Pure helpers (no catalogue, no S3, no network) so they are unit-testable offline
and shared by the control plane and the tests. Object bytes never pass through
this module: the API only authorizes transfers, SeaweedFS moves the bytes.
"""
import re

# Object lifecycle states used by the transfer engine. ``pending`` is the initial
# state an upload intent creates; the object only becomes ``available`` after the
# commit endpoint verifies size and SHA-256. See docs/TRANSFER-ENGINE.md.
PENDING = "pending"
# Uploaded and being hashed by the verification worker (N5, api/verifier.py).
VERIFYING = "verifying"
AVAILABLE = "available"
FAILED = "failed"
EXPIRED = "expired"
ABORTED = "aborted"
DELETED = "deleted"
# Purge in progress (two-phase delete, api/lifecycle.py): hidden and not
# downloadable; a row left in this state by a crash is finished by the sweep.
PURGING = "purging"

# Conservative RFC 6838 media-type shape. Parameters (everything after ``;``)
# are allowed but ignored for the allow-list comparison.
_MEDIA_TYPE_RE = re.compile(
    r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,126}/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}"
    r"(?:\s*;.*)?$"
)


def normalize_content_type(value: str) -> str:
    """Lowercase/collapse a media type, rejecting anything malformed.

    Raises :class:`ValueError` for a value that is not a plausible media type so
    a caller cannot smuggle control characters or a path-like string into the
    stored ``ContentType``.
    """
    if not value:
        raise ValueError("content_type is required")
    normalized = " ".join(value.split()).lower()
    if not normalized or len(normalized) > 127 or not _MEDIA_TYPE_RE.match(normalized):
        raise ValueError("invalid content type")
    return normalized


def bare_content_type(value: str) -> str:
    """The ``type/subtype`` part with parameters (``;charset=...``) stripped."""
    return value.split(";", 1)[0].strip()


def content_type_allowed(value: str, allowed) -> bool:
    """True when ``value`` is permitted by the ``allowed`` allow-list.

    An empty allow-list permits every valid media type. Each entry may be an
    exact type (``image/png``) or a ``type/*`` wildcard (``image/*``). The
    comparison ignores parameters and case.
    """
    if not allowed:
        return True
    bare = bare_content_type(value)
    for entry in allowed:
        candidate = entry.strip().lower()
        if not candidate:
            continue
        if candidate.endswith("/*"):
            if bare.startswith(candidate[:-1]):  # e.g. "image/" prefix
                return True
        elif candidate == bare:
            return True
    return False


def validate_upload_policy(
    content_type, size_bytes, *, max_bytes: int, allow_empty: bool, allowed_types
) -> str:
    """Apply the server-side upload policy and return the normalized media type.

    Raises :class:`ValueError` with a caller-safe message on any violation. This
    is the authoritative check; a frontend ``accept`` attribute or a size badge
    is UX only and is never treated as policy.
    """
    normalized = normalize_content_type(content_type)
    if not content_type_allowed(normalized, allowed_types):
        raise ValueError("content type not allowed")
    if size_bytes is None:
        raise ValueError("size_bytes is required")
    if size_bytes < 0:
        raise ValueError("size_bytes must not be negative")
    if size_bytes == 0 and not allow_empty:
        raise ValueError("empty objects are not allowed")
    if size_bytes > max_bytes:
        raise ValueError("size_bytes exceeds the maximum allowed object size")
    return normalized


def valid_idempotency_key(value: str, *, max_length: int) -> bool:
    """True when a client ``Idempotency-Key`` is usable as an opaque dedupe key.

    Rejects empty, over-long and control-character values so the key can be
    stored and compared without normalization surprises. The key is opaque: it
    is never logged (only a replay boolean is audited).
    """
    if not value or len(value) > max_length:
        return False
    return not any(ord(ch) < 32 or ord(ch) == 127 for ch in value)
