"""Lifecycle: reconcile stale pending uploads, soft-delete, purge (#19 follow-up)."""
import time

from .s3 import is_no_such_upload
from .transfer import PURGING


class StorageDeleteError(RuntimeError):
    """Raised when the storage backend refuses a delete (M06).

    The catalogue row is left untouched so a transient S3 outage never causes
    reference loss: the caller must surface a 5xx and let the next sweep retry.
    """


def reconcile_pending(repo, s3ops, expire_after_s: int = 3600,
                      limit: int = 500,
                      multipart_expired_before: int | None = None) -> dict:
    """Expire pending objects older than the grace period.

    Administrative sweep across every space; the route that calls it requires
    the ``admin:write`` capability (#36). A catalogue row is marked ``expired``
    only after its storage bytes were actually removed: a storage failure keeps
    the intent ``pending`` for a later sweep and is reported in
    ``storage_failures`` instead of being counted as cleaned (M06). Returns
    counts for observability.
    """
    cutoff = int(time.time()) - expire_after_s
    expired = 0
    storage_deletes = 0
    storage_failures = 0
    if multipart_expired_before is None:
        multipart_expired_before = int(time.time())
    for r in repo.list_pending_before(
        cutoff, limit=max(1, limit), multipart_expired_before=multipart_expired_before,
    ):
        row = dict(r) if not isinstance(r, dict) else r
        try:
            if row.get("multipart_upload_id"):
                # Free the parts of an abandoned multipart upload (#72).
                try:
                    s3ops.abort_multipart_upload(
                        Bucket=row["bucket"], Key=row["object_key"],
                        UploadId=row["multipart_upload_id"],
                    )
                except Exception as exc:
                    if not is_no_such_upload(exc):
                        raise
            s3ops.delete_object(Bucket=row["bucket"], Key=row["object_key"])
        except Exception:
            # Keep the pending reference so the next sweep retries; never
            # declare a failed cleanup as expired.
            storage_failures += 1
            continue
        storage_deletes += 1
        repo.set_status(row["id"], "expired")
        expired += 1
    return {
        "expired": expired,
        "storage_deletes_attempted": storage_deletes,
        "storage_failures": storage_failures,
    }


def purge_object(repo, s3ops, oid: str, *, space_id: str,
                 owner_id: str | None = None) -> bool:
    """Permanently delete bytes then the catalogue row. Returns True if done.

    Scoped to the caller's space (#35); ``owner_id`` further restricts to a
    single human owner. The route requires the ``objects:purge`` capability.
    A storage failure raises :class:`StorageDeleteError` and the catalogue row
    is kept, so the reference is never lost (M06); the route maps this to 502.

    Two phases, so a crash never leaves the catalogue lying: the row is first
    marked ``purging`` (hidden, not downloadable), then the bytes are deleted,
    then the row. A failed delete puts the row back as it was; a process that
    dies between the steps leaves a ``purging`` row that
    :func:`resume_purges` finishes.
    """
    row = repo.get_object(oid, space_id=space_id, owner_id=owner_id)
    if row is None:
        return False
    row = dict(row) if not isinstance(row, dict) else row
    previous = row["status"]
    if previous != PURGING:
        repo.set_status(oid, PURGING)
    try:
        s3ops.delete_object(Bucket=row["bucket"], Key=row["object_key"])
    except Exception as exc:
        if previous != PURGING:
            repo.set_status(oid, previous)
        raise StorageDeleteError(f"storage delete failed for {oid}") from exc
    repo.delete_object_row(oid)
    return True


def resume_purges(repo, s3ops, *, older_than_s: int = 300, limit: int = 500,
                  now=time.time) -> dict:
    """Finish purges interrupted between their steps (process crash).

    A ``purging`` row older than ``older_than_s`` belongs to no live request:
    delete its bytes (deleting a key that is already gone succeeds on S3) and
    then the row. A storage failure keeps the row for the next sweep.
    """
    cutoff = int(now()) - older_than_s
    done = failures = 0
    for r in repo.list_status_before(PURGING, cutoff, limit=max(1, limit)):
        row = dict(r) if not isinstance(r, dict) else r
        try:
            s3ops.delete_object(Bucket=row["bucket"], Key=row["object_key"])
        except Exception:
            failures += 1
            continue
        repo.delete_object_row(row["id"])
        done += 1
    return {"purges_completed": done, "purge_failures": failures}
