"""Asynchronous integrity verification (N5).

``commit`` checks that the bytes exist and have the expected size, then hands a
large object to this worker instead of hashing it inside the HTTP request. The
worker takes one ``verifying`` object at a time under a lease, streams it to
compute the SHA-256 and marks it ``available`` (or ``failed``). Nothing is ever
``available`` without a verified SHA-256.

Robustness:
- **Lease, not lock.** A claim sets ``verify_lease_until``; a crashed worker
  leaves an expired lease and the object is simply taken again.
- **Heartbeat.** While hashing, the lease is pushed forward, so a slow 100 GB
  hash is not stolen by a second worker.
- **Replaced bytes.** The ETag seen at commit must still match before and
  after hashing; otherwise the object is ``failed`` (a still-valid presigned
  URL could have overwritten it).
- **Transient errors** (storage unreachable) release the lease with an
  exponential delay; after ``max_attempts`` the object is ``failed``.

It runs in-process (a daemon thread started with the API, the default) or as
its own process: ``python -m api.verifier``.
"""
from __future__ import annotations

import hashlib
import threading
import time

from .s3 import ObjectTooLarge


class _Heartbeat:
    """Wrap a body iterator and renew the lease every ``every`` bytes."""

    def __init__(self, body, renew, every: int):
        self.body = body
        self.renew = renew
        self.every = max(1, every)

    def iter_chunks(self, chunk_size):
        since = 0
        for chunk in self.body.iter_chunks(chunk_size):
            since += len(chunk)
            if since >= self.every:
                self.renew()
                since = 0
            yield chunk


def _stream_sha256(body, max_bytes: int, chunk_size: int) -> tuple[int, str]:
    digest = hashlib.sha256()
    total = 0
    for chunk in body.iter_chunks(chunk_size):
        total += len(chunk)
        if total > max_bytes:
            raise ObjectTooLarge(total)
        digest.update(chunk)
    return total, digest.hexdigest()


def verify_one(repo, s3ops, settings, *, now=time.time, audit=None) -> str | None:
    """Claim and verify one object. Returns its outcome, or None if idle.

    Outcomes: ``available``, ``failed`` (final) or ``retry`` (lease released
    for a later attempt).
    """
    lease = settings.verify_lease_s
    t = int(now())
    row = repo.claim_verification(t, t + lease)
    if row is None:
        return None
    row = dict(row) if not isinstance(row, dict) else row
    oid = row["id"]

    def fail(reason: str) -> str:
        repo.set_status(oid, "failed")
        if audit:
            audit("verify", "system", reason, {"object_id": oid})
        return "failed"

    def retry() -> str:
        attempts = int(row.get("verify_attempts") or 1)
        if attempts >= settings.verify_max_attempts:
            return fail("gave_up")
        delay = min(3600, 30 * 2 ** (attempts - 1))
        repo.extend_verification(oid, int(now()) + delay)
        return "retry"

    expected = row.get("expected_size_bytes")
    max_bytes = settings.max_object_bytes
    if row.get("multipart_part_bytes"):
        max_bytes = max(max_bytes, settings.multipart_max_object_bytes)
    try:
        head = s3ops.head_object(Bucket=row["bucket"], Key=row["object_key"])
        if row.get("verify_etag") and head.get("ETag") != row["verify_etag"]:
            return fail("changed")
        body = s3ops.get_object(Bucket=row["bucket"], Key=row["object_key"])["Body"]
        beat = _Heartbeat(
            body,
            lambda: repo.extend_verification(oid, int(now()) + lease),
            settings.verify_heartbeat_bytes,
        )
        total, digest = _stream_sha256(beat, max_bytes, settings.commit_chunk_bytes)
        after = s3ops.head_object(Bucket=row["bucket"], Key=row["object_key"])
    except ObjectTooLarge:
        return fail("too_large")
    except Exception:
        return retry()

    if row.get("verify_etag") and after.get("ETag") != row["verify_etag"]:
        return fail("changed")
    if total != int(head.get("ContentLength", -1)) or (
        expected is not None and expected >= 0 and total != int(expected)
    ):
        return fail("size_mismatch")
    repo.mark_available(oid, total, digest)
    if audit:
        audit("verify", "system", "success", {"object_id": oid, "size_bytes": total})
    return "available"


def run_forever(repo, s3ops, settings, *, stop: threading.Event | None = None,
                audit=None) -> None:
    """Verify objects as they arrive; sleep briefly when there is nothing to do."""
    from .lifecycle import resume_purges

    stop = stop or threading.Event()
    last_purge_sweep = 0.0
    while not stop.is_set():
        try:
            outcome = verify_one(repo, s3ops, settings, audit=audit)
        except Exception:
            # Never let one bad row kill the worker; its lease expires and
            # the object is retried.
            outcome = None
        if outcome is None:
            # Idle: once a minute, finish purges a crash left half-done.
            if time.time() - last_purge_sweep >= 60:
                last_purge_sweep = time.time()
                try:
                    resume_purges(repo, s3ops)
                except Exception:
                    pass
            stop.wait(settings.verify_poll_s)


def start_background(repo, s3ops, settings, audit=None) -> threading.Event:
    """Start the in-process worker thread; set the returned event to stop it."""
    stop = threading.Event()
    thread = threading.Thread(
        target=run_forever, args=(repo, s3ops, settings),
        kwargs={"stop": stop, "audit": audit}, name="aura-verifier", daemon=True,
    )
    thread.start()
    return stop


if __name__ == "__main__":  # pragma: no cover - process entry point
    from .audit import audit_event
    from .config import settings as _settings
    from .s3 import make_clients
    from .store import repo as _repo

    _, _ops = make_clients(
        _settings.s3_endpoint, _settings.s3_internal_endpoint,
        _settings.s3_access_key, _settings.s3_secret_key, _settings.s3_region,
    )
    run_forever(_repo, _ops, _settings, audit=audit_event)
