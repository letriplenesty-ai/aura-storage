"""Storage ↔ catalogue reconciliation: bytes the catalogue does not account for.

``GET /admin/orphans`` already finds catalogue rows whose bytes are gone. This
module covers the other direction, which costs disk space silently:

- ``no_row``: an object in a registered bucket that no catalogue row names;
- ``terminal``: bytes kept for a row that is ``aborted``, ``expired`` or
  ``failed`` (its cleanup failed and nothing retries it);
- ``multipart``: an unfinished multipart upload no pending row owns any more.

Soft-deleted objects (the trash) are **not** orphans: their bytes are kept on
purpose until a purge. Anything younger than ``AURA_ORPHAN_GRACE_SECONDS`` is
ignored so an upload in flight is never reported.

The scan is read-only, bounded and resumable (``start_after``). Removal is a
separate, explicit admin action that re-checks every key just before acting.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

TERMINAL = frozenset({"aborted", "expired", "failed"})
KEY_PREFIX = "aura/"


def _epoch(value) -> int:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return int(value.timestamp())
    return int(value or 0)


def _reason(state: dict | None) -> str | None:
    if state is None:
        return "no_row"
    if state["status"] in TERMINAL:
        return "terminal"
    return None


def scan_bucket(repo, s3ops, bucket: str, *, grace_s: int, max_keys: int,
                start_after: str | None = None, now=time.time) -> dict:
    """Scan up to ``max_keys`` keys after ``start_after``; report orphans."""
    cutoff = int(now()) - grace_s
    scanned = 0
    orphans: list[dict] = []
    last_key = start_after
    truncated = False
    while scanned < max_keys:
        kwargs = {"Bucket": bucket, "Prefix": KEY_PREFIX,
                  "MaxKeys": min(1000, max_keys - scanned)}
        if last_key:
            kwargs["StartAfter"] = last_key
        page = s3ops.list_objects_v2(**kwargs)
        contents = page.get("Contents", [])
        if not contents:
            truncated = False
            break
        states = repo.object_states_for_keys(bucket, [o["Key"] for o in contents])
        for obj in contents:
            scanned += 1
            last_key = obj["Key"]
            modified = _epoch(obj.get("LastModified"))
            if modified > cutoff:
                continue
            state = states.get(obj["Key"])
            reason = _reason(state)
            if reason:
                orphans.append({
                    "key": obj["Key"], "size_bytes": int(obj.get("Size", 0)),
                    "last_modified": modified, "reason": reason,
                    "object_id": state["id"] if state else None,
                })
        truncated = bool(page.get("IsTruncated"))
        if not truncated:
            break
    return {
        "bucket": bucket, "scanned": scanned, "orphans": orphans,
        "orphan_bytes": sum(o["size_bytes"] for o in orphans),
        "next_start_after": last_key if truncated or scanned >= max_keys else None,
    }


def scan_multipart(repo, s3ops, bucket: str, *, grace_s: int, now=time.time,
                   max_uploads: int = 1000) -> list[dict]:
    """Unfinished multipart uploads no pending catalogue row still owns."""
    cutoff = int(now()) - grace_s
    page = s3ops.list_multipart_uploads(Bucket=bucket, Prefix=KEY_PREFIX,
                                        MaxUploads=max_uploads)
    uploads = page.get("Uploads", [])
    states = repo.object_states_for_keys(bucket, [u["Key"] for u in uploads])
    out = []
    for upload in uploads:
        initiated = _epoch(upload.get("Initiated"))
        if initiated > cutoff:
            continue
        state = states.get(upload["Key"])
        owned = (
            state is not None and state["status"] == "pending"
            and state.get("multipart_upload_id") == upload["UploadId"]
        )
        if not owned:
            out.append({"key": upload["Key"], "upload_id": upload["UploadId"],
                        "initiated": initiated,
                        "object_id": state["id"] if state else None})
    return out


def remove_orphans(repo, s3ops, bucket: str, keys: list, uploads: list, *,
                   grace_s: int, now=time.time) -> dict:
    """Delete the given orphan keys / abort the given uploads, re-checking each.

    Every key is re-validated just before acting: it must still exist, be older
    than the grace period and still be unaccounted for (no row, or a terminal
    row). Anything else is skipped with a reason; nothing else is touched.
    """
    cutoff = int(now()) - grace_s
    removed: list[str] = []
    aborted: list[str] = []
    skipped: list[dict] = []
    states = repo.object_states_for_keys(bucket, list(keys) + [u["key"] for u in uploads])
    for key in keys:
        if not key.startswith(KEY_PREFIX):
            skipped.append({"key": key, "reason": "outside the AURA prefix"})
            continue
        if _reason(states.get(key)) is None:
            skipped.append({"key": key, "reason": "now accounted for"})
            continue
        try:
            head = s3ops.head_object(Bucket=bucket, Key=key)
        except Exception:
            skipped.append({"key": key, "reason": "not found or storage unavailable"})
            continue
        if _epoch(head.get("LastModified")) > cutoff:
            skipped.append({"key": key, "reason": "too recent"})
            continue
        try:
            s3ops.delete_object(Bucket=bucket, Key=key)
        except Exception:
            skipped.append({"key": key, "reason": "storage delete failed"})
            continue
        removed.append(key)
    for upload in uploads:
        state = states.get(upload["key"])
        if (state is not None and state["status"] == "pending"
                and state.get("multipart_upload_id") == upload["upload_id"]):
            skipped.append({"key": upload["key"], "reason": "upload still active"})
            continue
        try:
            s3ops.abort_multipart_upload(Bucket=bucket, Key=upload["key"],
                                         UploadId=upload["upload_id"])
        except Exception:
            skipped.append({"key": upload["key"], "reason": "abort failed or already gone"})
            continue
        aborted.append(upload["upload_id"])
    return {"removed": removed, "aborted_uploads": aborted, "skipped": skipped}
