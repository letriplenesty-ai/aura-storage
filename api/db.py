"""SQLite catalogue behind a small Repository interface (#18; PostgreSQL in #19)."""
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

from . import ids
from .auth import new_token, session_key
from .dblock import synchronized
from .migrations import (
    DEFAULT_APPLICATION_NAME,
    DEFAULT_ENVIRONMENT,
    DEFAULT_SPACE_NAME,
    apply_migrations,
)
from .object_query import build_object_where, order_clause

SCHEMA = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")


def _as_dict(row):
    return dict(row) if row is not None else None


@synchronized
class Repository:
    dialect = "sqlite"

    def __init__(self, path: str):
        self.path = path
        # One connection is shared by every request thread (FastAPI runs sync
        # endpoints in a threadpool). `_lock` serializes access; see api/dblock.py.
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        apply_migrations(self.conn, "sqlite")

    def now(self) -> int:
        return int(time.time())

    def new_id(self) -> str:
        return ids.new_id()

    # -- users ------------------------------------------------------------
    def get_user_by_name(self, username: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()

    def get_user(self, uid: str) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()

    def create_user(
        self, username: str, pass_hash: str, role: str = "user",
        space_id: Optional[str] = None, application_id: Optional[str] = None,
    ) -> str:
        uid = self.new_id()
        application_id, space_id = self.default_scope(application_id, space_id)
        self.conn.execute(
            "INSERT INTO users (id, username, pass_hash, created_at, role, "
            "application_id, space_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (uid, username, pass_hash, self.now(), role, application_id, space_id),
        )
        self.conn.commit()
        return uid

    def ensure_admin(self, uid: str) -> None:
        application_id, space_id = self.default_scope()
        self.conn.execute("UPDATE users SET role = 'admin' WHERE id = ?", (uid,))
        self.conn.execute(
            "UPDATE users SET application_id = ? WHERE id = ? AND application_id IS NULL",
            (application_id, uid),
        )
        self.conn.execute(
            "UPDATE users SET space_id = ? WHERE id = ? AND space_id IS NULL",
            (space_id, uid),
        )
        self.conn.commit()

    # -- default tenant ---------------------------------------------------
    def get_default_space(self, application_id: str) -> Optional[dict]:
        """The seeded default space of ``application_id`` (name + environment)."""
        return _as_dict(self.conn.execute(
            "SELECT * FROM spaces WHERE application_id = ? AND name = ? "
            "AND environment = ? ORDER BY created_at LIMIT 1",
            (application_id, DEFAULT_SPACE_NAME, DEFAULT_ENVIRONMENT),
        ).fetchone())

    def default_scope(
        self,
        application_id: Optional[str] = None,
        space_id: Optional[str] = None,
    ) -> tuple[Optional[str], Optional[str]]:
        """Resolve the seeded default application/space ids for a new row.

        Ids are generated UUID v4 values, not constants, so they are looked up by
        the seed names (``aura`` / ``default`` / ``prod``) and only used to fill a
        missing argument. Either element stays ``None`` if the seed row is absent.
        """
        if not application_id:
            application = self.get_application_by_name(DEFAULT_APPLICATION_NAME)
            application_id = application["id"] if application else None
        if not space_id and application_id:
            space = self.get_default_space(application_id)
            space_id = space["id"] if space else None
        return application_id, space_id

    # -- sessions ---------------------------------------------------------
    def create_session(self, user_id: str, csrf: str, expires_at: int) -> str:
        """Create a session and return its opaque cookie token.

        Only ``session_key(token)`` is stored (row ``id``); the clear token
        exists solely in the returned value and the client cookie. Expired rows
        are pruned here so the table cannot grow without bound.
        """
        token = new_token(32)
        now = self.now()
        self.conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
        self.conn.execute(
            "INSERT INTO sessions (id, user_id, csrf, expires_at) VALUES (?, ?, ?, ?)",
            (session_key(token), user_id, csrf, expires_at),
        )
        self.conn.commit()
        return token

    def get_session(self, token: str) -> Optional[sqlite3.Row]:
        """Look a session up by its cookie token (hashed before the query)."""
        return self.conn.execute(
            "SELECT * FROM sessions WHERE id = ? AND expires_at > ?",
            (session_key(token), self.now()),
        ).fetchone()

    def delete_session(self, sid: str) -> None:
        """Delete by stored row id (``session["id"]``, i.e. the digest)."""
        self.conn.execute("DELETE FROM sessions WHERE id = ?", (sid,))
        self.conn.commit()

    # -- applications / spaces / buckets (#35) ----------------------------
    def create_application(self, name: str) -> str:
        aid = self.new_id()
        self.conn.execute(
            "INSERT INTO applications (id, name, status, created_at) VALUES (?, ?, 'active', ?)",
            (aid, name, self.now()),
        )
        self.conn.commit()
        return aid

    def get_application(self, aid: str) -> Optional[dict]:
        return _as_dict(self.conn.execute(
            "SELECT * FROM applications WHERE id = ?", (aid,)
        ).fetchone())

    def get_application_by_name(self, name: str) -> Optional[dict]:
        return _as_dict(self.conn.execute(
            "SELECT * FROM applications WHERE name = ?", (name,)
        ).fetchone())

    def list_applications(self) -> list:
        return [_as_dict(r) for r in self.conn.execute(
            "SELECT * FROM applications ORDER BY created_at"
        ).fetchall()]

    def application_name_exists(self, name: str, exclude_id: Optional[str] = None) -> bool:
        sql = "SELECT 1 FROM applications WHERE name = ?"
        params: list = [name]
        if exclude_id is not None:
            sql += " AND id != ?"
            params.append(exclude_id)
        return self.conn.execute(sql, params).fetchone() is not None

    def update_application(
        self, aid: str, *, name: Optional[str] = None, status: Optional[str] = None
    ) -> None:
        sets: list[str] = []
        params: list = []
        if name is not None:
            sets.append("name = ?")
            params.append(name)
        if status is not None:
            sets.append("status = ?")
            params.append(status)
        if not sets:
            return
        params.append(aid)
        self.conn.execute(
            f"UPDATE applications SET {', '.join(sets)} WHERE id = ?", params
        )
        self.conn.commit()

    def count_spaces_for_application(self, aid: str) -> int:
        return int(self.conn.execute(
            "SELECT COUNT(*) AS n FROM spaces WHERE application_id = ?", (aid,)
        ).fetchone()["n"])

    def create_space(self, application_id: str, name: str, environment: str) -> str:
        sid = self.new_id()
        self.conn.execute(
            "INSERT INTO spaces (id, application_id, name, environment, status, "
            "created_at) VALUES (?, ?, ?, ?, 'active', ?)",
            (sid, application_id, name, environment, self.now()),
        )
        self.conn.commit()
        return sid

    def get_space(self, sid: str) -> Optional[dict]:
        return _as_dict(self.conn.execute(
            "SELECT * FROM spaces WHERE id = ?", (sid,)
        ).fetchone())

    def list_spaces(self, application_id: Optional[str] = None) -> list:
        if application_id:
            rows = self.conn.execute(
                "SELECT * FROM spaces WHERE application_id = ? ORDER BY created_at",
                (application_id,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM spaces ORDER BY created_at"
            ).fetchall()
        return [_as_dict(r) for r in rows]

    def space_exists(
        self, application_id: str, name: str, environment: str,
        exclude_id: Optional[str] = None,
    ) -> bool:
        sql = (
            "SELECT 1 FROM spaces WHERE application_id = ? AND name = ? "
            "AND environment = ?"
        )
        params: list = [application_id, name, environment]
        if exclude_id is not None:
            sql += " AND id != ?"
            params.append(exclude_id)
        return self.conn.execute(sql, params).fetchone() is not None

    def update_space(
        self, sid: str, *, name: Optional[str] = None,
        environment: Optional[str] = None, status: Optional[str] = None,
    ) -> None:
        sets: list[str] = []
        params: list = []
        if name is not None:
            sets.append("name = ?")
            params.append(name)
        if environment is not None:
            sets.append("environment = ?")
            params.append(environment)
        if status is not None:
            sets.append("status = ?")
            params.append(status)
        if not sets:
            return
        params.append(sid)
        self.conn.execute(f"UPDATE spaces SET {', '.join(sets)} WHERE id = ?", params)
        self.conn.commit()

    def space_counts(self, sid: str) -> dict:
        def count(sql: str) -> int:
            return int(self.conn.execute(sql, (sid,)).fetchone()["n"])

        return {
            "buckets": count("SELECT COUNT(*) AS n FROM buckets WHERE space_id = ?"),
            "identities": count(
                "SELECT COUNT(*) AS n FROM service_identities WHERE space_id = ?"
            ),
            "objects": count("SELECT COUNT(*) AS n FROM objects WHERE space_id = ?"),
        }

    def create_bucket(self, space_id: str, name: str, is_default: bool = False) -> str:
        bid = self.new_id()
        self.conn.execute(
            "INSERT INTO buckets (id, space_id, name, is_default, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (bid, space_id, name, 1 if is_default else 0, self.now()),
        )
        self.conn.commit()
        return bid

    def get_bucket_for_space(self, space_id: str, name: str) -> Optional[dict]:
        return _as_dict(self.conn.execute(
            "SELECT * FROM buckets WHERE space_id = ? AND name = ?", (space_id, name)
        ).fetchone())

    def get_default_bucket(self, space_id: str) -> Optional[dict]:
        # Strict default only (M06): a space with no default bucket yields None
        # so upload-intents without an explicit registered bucket fail with 409
        # instead of silently falling back to a non-default bucket.
        return _as_dict(self.conn.execute(
            "SELECT * FROM buckets WHERE space_id = ? AND is_default = 1 "
            "ORDER BY created_at ASC LIMIT 1",
            (space_id,),
        ).fetchone())

    def get_bucket_by_name(self, name: str) -> Optional[dict]:
        return _as_dict(self.conn.execute(
            "SELECT * FROM buckets WHERE name = ?", (name,)
        ).fetchone())

    def get_bucket(self, bid: str) -> Optional[dict]:
        return _as_dict(self.conn.execute(
            "SELECT * FROM buckets WHERE id = ?", (bid,)
        ).fetchone())

    def list_buckets(self, space_id: str) -> list:
        return [_as_dict(r) for r in self.conn.execute(
            "SELECT * FROM buckets WHERE space_id = ? ORDER BY created_at", (space_id,)
        ).fetchall()]

    def list_all_buckets(self) -> list:
        # Every registered bucket, oldest first; the API joins space/application
        # names so a bucket is never shown without its attachment (#M04).
        return [_as_dict(r) for r in self.conn.execute(
            "SELECT * FROM buckets ORDER BY created_at, name"
        ).fetchall()]

    def set_default_bucket(self, space_id: str, bid: str, is_default: bool = True) -> None:
        # Exactly one default per space: clear the flag on the space's other
        # buckets, then set it on the target (scoped to the same space).
        if is_default:
            self.conn.execute(
                "UPDATE buckets SET is_default = 0 WHERE space_id = ?", (space_id,)
            )
            self.conn.execute(
                "UPDATE buckets SET is_default = 1 WHERE id = ? AND space_id = ?",
                (bid, space_id),
            )
        else:
            self.conn.execute(
                "UPDATE buckets SET is_default = 0 WHERE id = ? AND space_id = ?",
                (bid, space_id),
            )
        self.conn.commit()

    # -- service identities (#38) -----------------------------------------
    def create_identity(
        self, iid: str, application_id: str, space_id: str, name: str,
        secret_hash: str, scopes: str, expires_at: Optional[int] = None,
    ) -> str:
        self.conn.execute(
            "INSERT INTO service_identities (id, application_id, space_id, name, "
            "secret_hash, scopes, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (iid, application_id, space_id, name, secret_hash, scopes, self.now(), expires_at),
        )
        self.conn.commit()
        return iid

    def get_identity(self, iid: str) -> Optional[dict]:
        return _as_dict(self.conn.execute(
            "SELECT * FROM service_identities WHERE id = ?", (iid,)
        ).fetchone())

    def list_identities(self, space_id: Optional[str] = None) -> list:
        if space_id:
            rows = self.conn.execute(
                "SELECT * FROM service_identities WHERE space_id = ? ORDER BY created_at",
                (space_id,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM service_identities ORDER BY created_at"
            ).fetchall()
        return [_as_dict(r) for r in rows]

    def update_identity_secret(self, iid: str, secret_hash: str) -> None:
        self.conn.execute(
            "UPDATE service_identities SET secret_hash = ?, rotated_at = ? WHERE id = ?",
            (secret_hash, self.now(), iid),
        )
        self.conn.commit()

    def revoke_identity(self, iid: str) -> None:
        self.conn.execute(
            "UPDATE service_identities SET revoked_at = ? WHERE id = ?",
            (self.now(), iid),
        )
        self.conn.commit()

    def touch_identity(self, iid: str, ts: int) -> None:
        self.conn.execute(
            "UPDATE service_identities SET last_used_at = ? WHERE id = ?", (ts, iid)
        )
        self.conn.commit()

    # -- objects ----------------------------------------------------------
    def create_pending_object(
        self, *, space_id: str, application_id: Optional[str], environment: Optional[str],
        bucket: str, object_key: str, display_name: str, content_type: str,
        owner_id: Optional[str], actor_id: str, actor_kind: str,
        intent_expires_at: Optional[int], folder_id: Optional[str] = None,
        expected_size_bytes: Optional[int] = None,
        idempotency_key: Optional[str] = None,
    ) -> str:
        oid = self.new_id()
        now = self.now()
        self.conn.execute(
            """INSERT INTO objects (id, bucket, object_key, display_name, content_type,
               size_bytes, checksum_sha256, folder_id, owner_id, visibility, status,
               created_at, updated_at, actor_kind, actor_id, application_id, space_id,
               environment, intent_expires_at, expected_size_bytes, idempotency_key)
               VALUES (?, ?, ?, ?, ?, -1, '', ?, ?, 'private', 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (oid, bucket, object_key, display_name, content_type, folder_id, owner_id,
             now, now, actor_kind, actor_id, application_id, space_id, environment,
             intent_expires_at, expected_size_bytes, idempotency_key),
        )
        self.conn.commit()
        return oid

    def get_object(self, oid: str, *, space_id: str, owner_id: Optional[str] = None):
        sql = "SELECT * FROM objects WHERE id = ? AND space_id = ?"
        params = [oid, space_id]
        if owner_id is not None:
            sql += " AND owner_id = ?"
            params.append(owner_id)
        return self.conn.execute(sql, params).fetchone()

    def list_objects(
        self, *, space_id: str, owner_id: Optional[str] = None,
        folder_id: Optional[str] = None, q: Optional[str] = None,
        status: Optional[str] = None, hide_deleted: bool = False,
        content_type: Optional[str] = None,
        min_size: Optional[int] = None, max_size: Optional[int] = None,
        created_after: Optional[int] = None, created_before: Optional[int] = None,
        sort: str = "created_at", order: str = "desc",
        limit: int = 50, offset: int = 0,
    ):
        # Filters and sort are built by api/object_query.py so this stays in
        # lockstep with the PostgreSQL repository (M02). Every argument is
        # optional: omitting them keeps the historical "every visible object,
        # newest first" query.
        where, params = build_object_where(
            space_id=space_id, owner_id=owner_id, folder_id=folder_id, q=q,
            status=status, hide_deleted=hide_deleted,
            content_type=content_type, min_size=min_size,
            max_size=max_size, created_after=created_after,
            created_before=created_before,
        )
        sql = (
            f"SELECT * FROM objects WHERE {where} "
            f"{order_clause(sort=sort, order=order)} LIMIT ? OFFSET ?"
        )
        params += [limit, offset]
        return self.conn.execute(sql, params).fetchall()

    def count_pending(self, *, space_id: str, owner_id: Optional[str] = None) -> int:
        sql = "SELECT COUNT(*) AS n FROM objects WHERE status = 'pending' AND space_id = ?"
        params = [space_id]
        if owner_id is not None:
            sql += " AND owner_id = ?"
            params.append(owner_id)
        return int(self.conn.execute(sql, params).fetchone()["n"])

    def get_pending_by_idempotency(
        self, *, actor_id: str, idempotency_key: str, space_id: str,
    ):
        """The live pending intent for an ``Idempotency-Key`` + actor, if any.

        Scoped by actor and space so one caller can never replay or observe
        another caller's (or another space's) intent. Returns the newest match.
        """
        return self.conn.execute(
            "SELECT * FROM objects WHERE status = 'pending' AND actor_id = ? "
            "AND idempotency_key = ? AND space_id = ? ORDER BY created_at DESC LIMIT 1",
            (actor_id, idempotency_key, space_id),
        ).fetchone()

    # -- asynchronous verification (N5) ------------------------------------
    def mark_verifying(self, oid: str, etag: Optional[str]) -> None:
        """Hand an uploaded object to the verification worker."""
        self.conn.execute(
            "UPDATE objects SET status = 'verifying', verify_etag = ?, "
            "verify_lease_until = NULL, verify_attempts = 0, updated_at = ? "
            "WHERE id = ? AND status = 'pending'",
            (etag, self.now(), oid),
        )
        self.conn.commit()

    def claim_verification(self, now: int, lease_until: int):
        """Take the oldest verifying object nobody holds, under a lease.

        Compare-and-set on the lease, so two workers (threads or processes)
        never hash the same object at once; a crashed worker's lease simply
        runs out and the object is taken again.
        """
        row = self.conn.execute(
            "SELECT id FROM objects WHERE status = 'verifying' "
            "AND (verify_lease_until IS NULL OR verify_lease_until < ?) "
            "ORDER BY updated_at ASC LIMIT 1",
            (now,),
        ).fetchone()
        if row is None:
            return None
        cur = self.conn.execute(
            "UPDATE objects SET verify_lease_until = ?, "
            "verify_attempts = COALESCE(verify_attempts, 0) + 1 "
            "WHERE id = ? AND status = 'verifying' "
            "AND (verify_lease_until IS NULL OR verify_lease_until < ?)",
            (lease_until, row["id"], now),
        )
        self.conn.commit()
        if cur.rowcount != 1:
            return None
        return self.conn.execute("SELECT * FROM objects WHERE id = ?", (row["id"],)).fetchone()

    def extend_verification(self, oid: str, lease_until: int) -> None:
        """Push the lease (heartbeat while hashing, or a retry delay)."""
        self.conn.execute(
            "UPDATE objects SET verify_lease_until = ? WHERE id = ? AND status = 'verifying'",
            (lease_until, oid),
        )
        self.conn.commit()

    def get_by_idempotency(
        self, *, actor_id: str, idempotency_key: str, space_id: str,
    ):
        """Newest object of any status for an ``Idempotency-Key`` + actor."""
        return self.conn.execute(
            "SELECT * FROM objects WHERE actor_id = ? AND idempotency_key = ? "
            "AND space_id = ? ORDER BY created_at DESC LIMIT 1",
            (actor_id, idempotency_key, space_id),
        ).fetchone()

    def storage_commitment(self) -> dict:
        """Bytes the object store holds or has promised, across every space.

        ``stored``: verified bytes still on disk (available, trash, purge in
        progress). ``reserved``: declared sizes of uploads not yet verified
        (pending, verifying) — counted from the moment the intent is issued.
        """
        row = self.conn.execute(
            "SELECT "
            "COALESCE(SUM(CASE WHEN status IN ('available', 'deleted', 'purging') "
            "AND size_bytes > 0 THEN size_bytes ELSE 0 END), 0) AS stored, "
            "COALESCE(SUM(CASE WHEN status IN ('pending', 'verifying') "
            "AND expected_size_bytes > 0 THEN expected_size_bytes ELSE 0 END), 0) AS reserved "
            "FROM objects"
        ).fetchone()
        return {"stored": int(row["stored"]), "reserved": int(row["reserved"])}
    # -- periodic integrity scrub ------------------------------------------
    def claim_scrub(self, now: int, lease_until: int, checked_before: int):
        """Take the available object checked longest ago (never-checked first),
        under a lease, if it is due (checked before ``checked_before``)."""
        row = self.conn.execute(
            "SELECT id FROM objects WHERE status = 'available' "
            "AND (scrubbed_at IS NULL OR scrubbed_at < ?) "
            "AND (scrub_lease_until IS NULL OR scrub_lease_until < ?) "
            "ORDER BY COALESCE(scrubbed_at, 0) ASC, created_at ASC LIMIT 1",
            (checked_before, now),
        ).fetchone()
        if row is None:
            return None
        cur = self.conn.execute(
            "UPDATE objects SET scrub_lease_until = ? WHERE id = ? "
            "AND (scrub_lease_until IS NULL OR scrub_lease_until < ?)",
            (lease_until, row["id"], now),
        )
        self.conn.commit()
        if cur.rowcount != 1:
            return None
        return self.conn.execute("SELECT * FROM objects WHERE id = ?", (row["id"],)).fetchone()

    def record_scrub(self, oid: str, result: str, at: int) -> None:
        self.conn.execute(
            "UPDATE objects SET scrubbed_at = ?, scrub_result = ?, scrub_lease_until = NULL "
            "WHERE id = ?",
            (at, result, oid),
        )
        self.conn.commit()

    def release_scrub(self, oid: str) -> None:
        """Give the object back unchecked (storage outage): retried next run."""
        self.conn.execute("UPDATE objects SET scrub_lease_until = NULL WHERE id = ?", (oid,))
        self.conn.commit()

    def integrity_summary(self) -> dict:
        rows = self.conn.execute(
            "SELECT COALESCE(scrub_result, 'unchecked') AS result, COUNT(*) AS n, "
            "MIN(scrubbed_at) AS oldest FROM objects WHERE status = 'available' "
            "GROUP BY COALESCE(scrub_result, 'unchecked')"
        ).fetchall()
        return {r["result"]: {"count": int(r["n"]), "oldest": r["oldest"]} for r in rows}

    def list_integrity_problems(self, limit: int = 100):
        return self.conn.execute(
            "SELECT id, space_id, display_name, size_bytes, scrub_result, scrubbed_at "
            "FROM objects WHERE status = 'available' "
            "AND scrub_result IN ('mismatch', 'missing') "
            "ORDER BY scrubbed_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    def object_states_for_keys(self, bucket: str, keys: list) -> dict:
        """Catalogue state of storage keys: ``{key: {id, status, multipart_upload_id}}``.

        Keys without a row are absent from the result (storage orphans).
        """
        out: dict = {}
        for i in range(0, len(keys), 500):
            chunk = list(keys[i:i + 500])
            if not chunk:
                continue
            marks = ",".join("?" for _ in chunk)
            for r in self.conn.execute(
                f"SELECT id, object_key, status, multipart_upload_id FROM objects "
                f"WHERE bucket = ? AND object_key IN ({marks})",
                (bucket, *chunk),
            ).fetchall():
                out[r["object_key"]] = {
                    "id": r["id"], "status": r["status"],
                    "multipart_upload_id": r["multipart_upload_id"],
                }
        return out

    def mark_available(self, oid: str, size_bytes: int, checksum_sha256: str) -> None:
        self.conn.execute(
            "UPDATE objects SET status = 'available', size_bytes = ?, checksum_sha256 = ?, updated_at = ? WHERE id = ?",
            (size_bytes, checksum_sha256, self.now(), oid),
        )
        self.conn.commit()

    def set_multipart(self, oid: str, upload_id: Optional[str],
                      part_bytes: Optional[int]) -> None:
        """Record (or clear, with ``upload_id=None``) a multipart session (#72)."""
        self.conn.execute(
            "UPDATE objects SET multipart_upload_id = ?, multipart_part_bytes = ?, "
            "updated_at = ? WHERE id = ?",
            (upload_id, part_bytes, self.now(), oid),
        )
        self.conn.commit()

    def list_pending_before(self, cutoff: int, limit: int = 500,
                            multipart_expired_before: Optional[int] = None):
        # A multipart session lives longer than a single-PUT intent: it is only
        # stale once its own expiry has passed, whatever its creation time.
        mp_cut = multipart_expired_before if multipart_expired_before is not None else cutoff
        return self.conn.execute(
            "SELECT * FROM objects WHERE status = 'pending' AND created_at < ? "
            "AND (multipart_upload_id IS NULL OR intent_expires_at < ?) "
            "ORDER BY created_at ASC LIMIT ?",
            (cutoff, mp_cut, limit),
        ).fetchall()

    def list_available(self, limit: int = 500):
        """Available rows oldest-first, bounded (M07 orphan scan)."""
        return self.conn.execute(
            "SELECT * FROM objects WHERE status = 'available' "
            "ORDER BY updated_at ASC LIMIT ?",
            (limit,),
        ).fetchall()

    def set_status(self, oid: str, status: str) -> None:
        self.conn.execute(
            "UPDATE objects SET status = ?, updated_at = ? WHERE id = ?",
            (status, self.now(), oid),
        )
        self.conn.commit()

    def list_status_before(self, status: str, cutoff: int, limit: int = 500):
        """Rows in ``status`` not updated since ``cutoff``, oldest first."""
        return self.conn.execute(
            "SELECT * FROM objects WHERE status = ? AND updated_at < ? "
            "ORDER BY updated_at ASC LIMIT ?",
            (status, cutoff, limit),
        ).fetchall()

    def delete_object_row(self, oid: str) -> None:
        self.conn.execute("DELETE FROM objects WHERE id = ?", (oid,))
        self.conn.commit()

    # -- folders (#61) ----------------------------------------------------
    # Folders are logical catalogue entities owned by a human user. They are
    # never real host directories; parent/child links live in `parent_id`.
    def create_folder(self, owner_id: str, name: str, parent_id: Optional[str] = None) -> str:
        fid = self.new_id()
        self.conn.execute(
            "INSERT INTO folders (id, owner_id, parent_id, name, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (fid, owner_id, parent_id, name, self.now()),
        )
        self.conn.commit()
        return fid

    def get_folder(self, fid: str, *, owner_id: str) -> Optional[dict]:
        return _as_dict(self.conn.execute(
            "SELECT * FROM folders WHERE id = ? AND owner_id = ?", (fid, owner_id)
        ).fetchone())

    def list_folders(self, *, owner_id: str, parent_id: Optional[str] = None) -> list:
        # parent_id=None lists the root level (parent_id IS NULL).
        if parent_id is None:
            rows = self.conn.execute(
                "SELECT * FROM folders WHERE owner_id = ? AND parent_id IS NULL "
                "ORDER BY name",
                (owner_id,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM folders WHERE owner_id = ? AND parent_id = ? ORDER BY name",
                (owner_id, parent_id),
            ).fetchall()
        return [_as_dict(r) for r in rows]

    def list_all_folders(self, *, owner_id: str) -> list:
        # Flat listing of the owner's whole tree in one query (#62 follow-up).
        # Ordered by name for a deterministic payload; the client rebuilds the
        # hierarchy from parent_id. Owner-scoped, so no cross-user leak.
        rows = self.conn.execute(
            "SELECT * FROM folders WHERE owner_id = ? ORDER BY name, id", (owner_id,)
        ).fetchall()
        return [_as_dict(r) for r in rows]

    def folder_name_exists(
        self, *, owner_id: str, parent_id: Optional[str], name: str,
        exclude_id: Optional[str] = None,
    ) -> bool:
        if parent_id is None:
            sql = "SELECT 1 FROM folders WHERE owner_id = ? AND parent_id IS NULL AND name = ?"
            params: list = [owner_id, name]
        else:
            sql = "SELECT 1 FROM folders WHERE owner_id = ? AND parent_id = ? AND name = ?"
            params = [owner_id, parent_id, name]
        if exclude_id is not None:
            sql += " AND id != ?"
            params.append(exclude_id)
        return self.conn.execute(sql, params).fetchone() is not None

    def rename_folder(self, fid: str, *, owner_id: str, name: str) -> None:
        self.conn.execute(
            "UPDATE folders SET name = ? WHERE id = ? AND owner_id = ?", (name, fid, owner_id)
        )
        self.conn.commit()

    def set_folder_parent(
        self, fid: str, *, owner_id: str, parent_id: Optional[str]
    ) -> None:
        self.conn.execute(
            "UPDATE folders SET parent_id = ? WHERE id = ? AND owner_id = ?",
            (parent_id, fid, owner_id),
        )
        self.conn.commit()

    def set_object_folder(self, oid: str, *, folder_id: Optional[str]) -> None:
        # File an object into a folder, or unfiled (folder_id IS NULL). Scope is
        # enforced by the caller first (#61/#62): the route reads the object with
        # the caller's space/owner filter before issuing this update.
        self.conn.execute(
            "UPDATE objects SET folder_id = ?, updated_at = ? WHERE id = ?",
            (folder_id, self.now(), oid),
        )
        self.conn.commit()

    def folder_child_count(self, fid: str, *, owner_id: str) -> int:
        return int(self.conn.execute(
            "SELECT COUNT(*) AS n FROM folders WHERE parent_id = ? AND owner_id = ?",
            (fid, owner_id),
        ).fetchone()["n"])

    def folder_object_count(self, fid: str, *, owner_id: str) -> int:
        return int(self.conn.execute(
            "SELECT COUNT(*) AS n FROM objects WHERE folder_id = ? AND owner_id = ?",
            (fid, owner_id),
        ).fetchone()["n"])

    def delete_folder(self, fid: str, *, owner_id: str) -> None:
        self.conn.execute(
            "DELETE FROM folders WHERE id = ? AND owner_id = ?", (fid, owner_id)
        )
        self.conn.commit()

    # -- observability (#40, M07) ----------------------------------------
    def ping(self) -> None:
        """Liveness probe for supervision: raises on any backend failure."""
        self.conn.execute("SELECT 1").fetchone()

    def stored_bytes(self) -> int:
        """Verified bytes currently held: available objects only (M07).

        Pending intents still carry ``size_bytes = -1`` and deleted/expired
        rows hold no retrievable bytes, so only ``available`` rows count.
        """
        row = self.conn.execute(
            "SELECT COALESCE(SUM(size_bytes), 0) AS n FROM objects "
            "WHERE status = 'available'"
        ).fetchone()
        return int(row["n"])

    def space_usage(self, sid: str) -> dict:
        """Real per-space consumption for the M07 usage endpoint."""
        by_status = {
            row["status"]: row["n"]
            for row in self.conn.execute(
                "SELECT status, COUNT(*) AS n FROM objects "
                "WHERE space_id = ? GROUP BY status", (sid,)
            ).fetchall()
        }
        row = self.conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(size_bytes), 0) AS bytes "
            "FROM objects WHERE space_id = ? AND status = 'available'",
            (sid,),
        ).fetchone()
        pending = int(self.conn.execute(
            "SELECT COUNT(*) AS n FROM objects "
            "WHERE space_id = ? AND status = 'pending'", (sid,)
        ).fetchone()["n"])
        return {
            "objects_by_status": by_status,
            "available_count": int(row["n"]),
            "stored_bytes": int(row["bytes"]),
            "pending_count": pending,
        }

    def stats(self) -> dict:
        objects = {
            row["status"]: row["n"]
            for row in self.conn.execute(
                "SELECT status, COUNT(*) AS n FROM objects GROUP BY status"
            ).fetchall()
        }
        return {
            "objects_by_status": objects,
            "applications": self.conn.execute(
                "SELECT COUNT(*) AS n FROM applications"
            ).fetchone()["n"],
            "spaces": self.conn.execute("SELECT COUNT(*) AS n FROM spaces").fetchone()["n"],
            "buckets": self.conn.execute("SELECT COUNT(*) AS n FROM buckets").fetchone()["n"],
            "service_identities": self.conn.execute(
                "SELECT COUNT(*) AS n FROM service_identities"
            ).fetchone()["n"],
            "active_service_identities": self.conn.execute(
                "SELECT COUNT(*) AS n FROM service_identities WHERE revoked_at IS NULL"
            ).fetchone()["n"],
        }
