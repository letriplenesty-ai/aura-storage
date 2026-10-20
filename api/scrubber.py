"""Periodic integrity scrub: re-hash stored objects to catch silent corruption.

An object's SHA-256 is verified once, at import. A disk can still rot bytes
afterwards without any error. The scrubber re-reads every ``available`` object
at least once per ``AURA_SCRUB_INTERVAL_DAYS`` (never-checked and oldest-checked
first) and compares the digest with the catalogue:

- ``ok``: bytes match;
- ``mismatch``: bytes differ from the verified digest (restore from backup);
- ``missing``: the storage no longer has the object.

It is **read-only**: nothing is deleted, rewritten or hidden. Problems are
audited and listed by ``GET /api/v1/admin/integrity``. A storage outage is not a
finding: the object is released unchecked and retried on the next run.

Each run stops after ``AURA_SCRUB_BYTES_PER_RUN`` so the disk is never
saturated; runs repeat every ``AURA_SCRUB_PERIOD_SECONDS``. A lease on each
object keeps two API processes from scrubbing the same one.
"""
from __future__ import annotations

import threading
import time

from .s3 import ObjectTooLarge
from .verifier import _stream_sha256

# Long enough for one very large object at a slow disk; released on finish.
_LEASE_S = 6 * 3600


def _is_missing(exc: Exception) -> bool:
    response = getattr(exc, "response", None) or {}
    code = str(response.get("Error", {}).get("Code", ""))
    return code in ("404", "NoSuchKey", "NotFound")


def scrub_one(repo, s3ops, settings, *, now=time.time, audit=None) -> tuple[str, int] | None:
    """Check one due object. Returns ``(result, bytes_read)`` or None if none is due."""
    t = int(now())
    due_before = t - settings.scrub_interval_days * 86400
    row = repo.claim_scrub(t, t + _LEASE_S, due_before)
    if row is None:
        return None
    row = dict(row) if not isinstance(row, dict) else row
    oid = row["id"]
    limit = max(settings.max_object_bytes, settings.multipart_max_object_bytes)
    try:
        body = s3ops.get_object(Bucket=row["bucket"], Key=row["object_key"])["Body"]
        total, digest = _stream_sha256(body, limit, settings.commit_chunk_bytes)
    except ObjectTooLarge:
        total, digest = -1, ""
    except Exception as exc:
        if _is_missing(exc):
            repo.record_scrub(oid, "missing", int(now()))
            if audit:
                audit("scrub", "system", "missing", {"object_id": oid})
            return "missing", 0
        # Outage, not a finding: give it back for the next run.
        repo.release_scrub(oid)
        return "error", 0

    ok = total == int(row["size_bytes"]) and digest == row["checksum_sha256"]
    result = "ok" if ok else "mismatch"
    repo.record_scrub(oid, result, int(now()))
    if audit and not ok:
        audit("scrub", "system", "mismatch", {"object_id": oid, "size_bytes": total})
    return result, max(0, total)


def scrub_run(repo, s3ops, settings, *, now=time.time, audit=None,
              stop: threading.Event | None = None) -> dict:
    """One bounded pass: check due objects until the byte budget is spent."""
    counts = {"ok": 0, "mismatch": 0, "missing": 0, "error": 0, "bytes": 0}
    while counts["bytes"] < settings.scrub_bytes_per_run:
        if stop is not None and stop.is_set():
            break
        out = scrub_one(repo, s3ops, settings, now=now, audit=audit)
        if out is None:
            break
        result, read = out
        counts[result] += 1
        counts["bytes"] += read
        if result == "error":
            # The storage is failing: stop this pass rather than hammer it.
            break
    return counts


def start_background(repo, s3ops, settings, audit=None) -> threading.Event:
    """Run a bounded pass every ``scrub_period_s``; set the event to stop."""
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            try:
                scrub_run(repo, s3ops, settings, audit=audit, stop=stop)
            except Exception:
                pass
            stop.wait(settings.scrub_period_s)

    threading.Thread(target=loop, name="aura-scrubber", daemon=True).start()
    return stop
