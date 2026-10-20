"""Additive schema migrations shared by the SQLite (dev/test) and PostgreSQL
(pilot) catalogue backends (#35, #38).

Design rules:
- **Additive only.** New tables live in ``schema.sql`` (CREATE TABLE IF NOT
  EXISTS). New columns are added here only when missing, so an existing
  catalogue keeps every row.
- **Idempotent.** Safe to run on every process start; a version row is recorded
  in ``schema_migrations``.
- **One documented exception.** ``_canonicalize_legacy_ids`` rewrites the three
  fixed ``0000…0001/2/3`` seed rows (application/space/bucket) to fresh UUID v4
  values and repoints their foreign keys. That is a deliberate one-time data
  migration, not an additive column change: see ``docs/DATA-MODEL.md`` and the
  identifier rule in ``api/ids.py``. It only ever touches those exact, known ids
  and is skipped once they are gone, so it stays idempotent.
- **Rollback.** Column additions add nothing the previous release reads, so the
  documented rollback for a bad release is "redeploy the previous image/volume".
  A restore-from-backup path is described in ``docs/BACKUP-RESTORE.md``.

Seeding creates the default application/space/bucket and backfills any legacy
user/object rows created before #35 so that current behaviour is preserved.
"""
import time

from . import ids

# Seed identity, resolved by name so the default rows carry a generated UUID v4
# instead of a fixed sentinel. Never used to derive an id.
DEFAULT_APPLICATION_NAME = "aura"
DEFAULT_SPACE_NAME = "default"
DEFAULT_ENVIRONMENT = "prod"
DEFAULT_BUCKET_NAME = "aura"

# Fixed ids written by releases before identifiers were canonicalized. Only ever
# read to detect and rewrite those rows; never inserted.
LEGACY_APPLICATION_ID = "00000000-0000-0000-0000-000000000001"
LEGACY_SPACE_ID = "00000000-0000-0000-0000-000000000002"
LEGACY_BUCKET_ID = "00000000-0000-0000-0000-000000000003"

SCHEMA_VERSION = "0003_space_status"

# (table, column, declaration) added only if absent. A constant DEFAULT is
# allowed by both engines on ADD COLUMN, so existing rows are filled with the
# default and new inserts keep working even before the backfill runs.
ADDED_COLUMNS = {
    "users": [
        ("role", "TEXT"),
        ("application_id", "TEXT"),
        ("space_id", "TEXT"),
    ],
    "objects": [
        ("actor_kind", "TEXT"),
        ("actor_id", "TEXT"),
        ("application_id", "TEXT"),
        ("space_id", "TEXT"),
        ("environment", "TEXT"),
        ("intent_expires_at", "INTEGER"),
        # Transfer engine (M03): declared size is kept distinct from the verified
        # ``size_bytes`` (which stays -1 until commit), and an optional opaque
        # client idempotency key dedupes accidental double submits.
        ("expected_size_bytes", "INTEGER"),
        ("idempotency_key", "TEXT"),
        # Multipart session (#72): backend upload id while parts are being
        # sent (cleared once completed) and the server-chosen part size.
        ("multipart_upload_id", "TEXT"),
        ("multipart_part_bytes", "INTEGER"),
        # Asynchronous integrity check (N5): the object waits in 'verifying'
        # until a worker holding the lease has hashed it. The ETag seen at
        # commit detects bytes replaced before or during hashing.
        ("verify_etag", "TEXT"),
        ("verify_lease_until", "INTEGER"),
        ("verify_attempts", "INTEGER DEFAULT 0"),
        # Periodic integrity scrub: when the stored bytes were last re-hashed,
        # what that found ('ok', 'mismatch', 'missing') and the scrub lease.
        ("scrubbed_at", "INTEGER"),
        ("scrub_result", "TEXT"),
        ("scrub_lease_until", "INTEGER"),
    ],
    # #M04: a space is soft-disabled, never deleted. Pre-existing spaces are
    # backfilled to 'active' in `_seed`.
    "spaces": [
        ("status", "TEXT DEFAULT 'active'"),
    ],
}


def _translate_placeholders(sql: str) -> str:
    """Translate ``?`` placeholders to PostgreSQL ``%s`` (finding #10).

    A plain ``str.replace('?', '%s')`` would also rewrite a ``?`` that sits inside
    a string literal or a quoted identifier, corrupting the statement. This
    scanner only rewrites ``?`` characters that are actual placeholders, leaving
    quoted regions and the jsonb existence operators (``?|``, ``?&``) intact. A
    bare jsonb ``?`` operator is not supported in migration SQL — use
    ``jsonb_exists()`` if it is ever needed.
    """
    out: list[str] = []
    i, n = 0, len(sql)
    quote = ""  # active quote character ("" when outside a quoted region)
    while i < n:
        ch = sql[i]
        if quote:
            out.append(ch)
            if ch == quote:
                if i + 1 < n and sql[i + 1] == quote:  # escaped '' or ""
                    out.append(quote)
                    i += 2
                    continue
                quote = ""
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "?":
            if i + 1 < n and sql[i + 1] in "|&":
                out.append(ch)  # jsonb operator (?| or ?&), not a placeholder
            else:
                out.append("%s")
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


class _Runner:
    """Thin dialect adapter: ``?`` placeholders are translated for PostgreSQL."""

    def __init__(self, conn, dialect: str):
        self.conn = conn
        self.dialect = dialect

    def exec(self, sql: str, params: tuple = ()):
        if self.dialect == "postgres":
            with self.conn.cursor() as cur:
                cur.execute(_translate_placeholders(sql), params)
                return cur
        return self.conn.execute(sql, params)

    def columns(self, table: str) -> set:
        if self.dialect == "postgres":
            with self.conn.cursor() as cur:
                cur.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = %s",
                    (table,),
                )
                return {
                    (row["column_name"] if isinstance(row, dict) else row[0])
                    for row in cur.fetchall()
                }
        rows = self.conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {row[1] for row in rows}

    def has_column(self, table: str, column: str) -> bool:
        return column in self.columns(table)

    def get(self, sql: str, params: tuple = ()):
        """Return one row as a column->value dict, or ``None``."""
        if self.dialect == "postgres":
            with self.conn.cursor() as cur:
                cur.execute(_translate_placeholders(sql), params)
                cols = [d[0] for d in cur.description]
                row = cur.fetchone()
        else:
            cur = self.conn.execute(sql, params)
            cols = [d[0] for d in cur.description]
            row = cur.fetchone()
        if row is None:
            return None
        if isinstance(row, dict):
            return row
        return dict(zip(cols, row))

    def scalar(self, sql: str, params: tuple = ()):
        row = self.get(sql, params)
        return None if row is None else next(iter(row.values()))

    def commit(self):
        if self.dialect != "postgres":
            self.conn.commit()


def resolve_default_ids(conn, dialect: str = "sqlite"):
    """Return ``(application_id, space_id)`` of the seeded default tenant.

    Either element is ``None`` when the corresponding row is absent (for example
    on a catalogue that has not finished seeding). The rows are looked up by
    name because their ids are generated UUID v4 values, not fixed sentinels.
    """
    runner = _Runner(conn, dialect)
    application_id = runner.scalar(
        "SELECT id FROM applications WHERE name = ?", (DEFAULT_APPLICATION_NAME,)
    )
    if application_id is None:
        return None, None
    space_id = runner.scalar(
        "SELECT id FROM spaces WHERE application_id = ? AND name = ? "
        "AND environment = ? ORDER BY created_at LIMIT 1",
        (application_id, DEFAULT_SPACE_NAME, DEFAULT_ENVIRONMENT),
    )
    return application_id, space_id


def apply_migrations(conn, dialect: str = "sqlite") -> None:
    """Add missing columns, canonicalize legacy ids, seed and record the version."""
    runner = _Runner(conn, dialect)
    for table, columns in ADDED_COLUMNS.items():
        existing = runner.columns(table)
        if not existing:
            continue  # schema.sql has not run yet; nothing to alter
        for name, declaration in columns:
            if name not in existing:
                runner.exec(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
    runner.commit()
    runner.exec("CREATE INDEX IF NOT EXISTS idx_objects_space ON objects(space_id, status)")
    runner.commit()
    _canonicalize_legacy_ids(runner)
    _seed(runner)
    runner.exec(
        "INSERT INTO schema_migrations (id, applied_at) VALUES (?, ?) "
        "ON CONFLICT DO NOTHING",
        (SCHEMA_VERSION, int(time.time())),
    )
    runner.commit()


def _canonicalize_legacy_ids(runner: _Runner) -> None:
    """Rewrite the fixed ``0000…0001/2/3`` seed rows to UUID v4, once.

    Earlier releases seeded the default application/space/bucket with constant
    ids. This step renames each of those three rows to a fresh canonical UUID v4
    and repoints every foreign key, so an upgraded catalogue matches one seeded
    from scratch. It is skipped when the legacy ids are not present, so it is
    safe to run on every start. ``applications.name`` and ``spaces`` are unique,
    so the old row is renamed to a temporary label before the new id is inserted
    and the old row deleted — the foreign keys never point at a missing row.
    """
    _canonicalize_application(runner)
    _canonicalize_space(runner)
    _canonicalize_bucket(runner)


def _canonicalize_application(runner: _Runner) -> None:
    row = runner.get(
        "SELECT name, status, created_at FROM applications WHERE id = ?",
        (LEGACY_APPLICATION_ID,),
    )
    if row is None:
        return
    new_id = ids.new_id()
    runner.exec(
        "UPDATE applications SET name = ? WHERE id = ?",
        (f"__legacy__{new_id}", LEGACY_APPLICATION_ID),
    )
    runner.exec(
        "INSERT INTO applications (id, name, status, created_at) VALUES (?, ?, ?, ?)",
        (new_id, row["name"], row["status"], row["created_at"]),
    )
    for table in ("spaces", "users", "objects", "service_identities"):
        if runner.has_column(table, "application_id"):
            runner.exec(
                f"UPDATE {table} SET application_id = ? WHERE application_id = ?",
                (new_id, LEGACY_APPLICATION_ID),
            )
    runner.exec("DELETE FROM applications WHERE id = ?", (LEGACY_APPLICATION_ID,))


def _canonicalize_space(runner: _Runner) -> None:
    row = runner.get(
        "SELECT application_id, name, environment, created_at FROM spaces WHERE id = ?",
        (LEGACY_SPACE_ID,),
    )
    if row is None:
        return
    new_id = ids.new_id()
    runner.exec(
        "UPDATE spaces SET name = ? WHERE id = ?",
        (f"__legacy__{new_id}", LEGACY_SPACE_ID),
    )
    runner.exec(
        "INSERT INTO spaces (id, application_id, name, environment, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (new_id, row["application_id"], row["name"], row["environment"], row["created_at"]),
    )
    for table in ("buckets", "users", "objects", "service_identities"):
        if runner.has_column(table, "space_id"):
            runner.exec(
                f"UPDATE {table} SET space_id = ? WHERE space_id = ?",
                (new_id, LEGACY_SPACE_ID),
            )
    runner.exec("DELETE FROM spaces WHERE id = ?", (LEGACY_SPACE_ID,))


def _canonicalize_bucket(runner: _Runner) -> None:
    # Nothing references ``buckets.id`` (objects locate a bucket by name), so the
    # primary key can be updated in place without a temporary rename.
    row = runner.get("SELECT 1 FROM buckets WHERE id = ?", (LEGACY_BUCKET_ID,))
    if row is None:
        return
    runner.exec(
        "UPDATE buckets SET id = ? WHERE id = ?", (ids.new_id(), LEGACY_BUCKET_ID)
    )


def _seed(runner: _Runner) -> None:
    """Ensure the default application/space/bucket and backfill legacy rows.

    Ids are generated UUID v4 values. Each INSERT is guarded by a name lookup and
    carries ``ON CONFLICT DO NOTHING`` so two processes racing on first start
    cannot create duplicates; the winning row is then re-read.
    """
    now = int(time.time())
    application_id = runner.scalar(
        "SELECT id FROM applications WHERE name = ?", (DEFAULT_APPLICATION_NAME,)
    )
    if application_id is None:
        runner.exec(
            "INSERT INTO applications (id, name, status, created_at) "
            "VALUES (?, ?, 'active', ?) ON CONFLICT DO NOTHING",
            (ids.new_id(), DEFAULT_APPLICATION_NAME, now),
        )
        application_id = runner.scalar(
            "SELECT id FROM applications WHERE name = ?", (DEFAULT_APPLICATION_NAME,)
        )
    space_id = runner.scalar(
        "SELECT id FROM spaces WHERE application_id = ? AND name = ? "
        "AND environment = ? ORDER BY created_at LIMIT 1",
        (application_id, DEFAULT_SPACE_NAME, DEFAULT_ENVIRONMENT),
    )
    if space_id is None:
        runner.exec(
            "INSERT INTO spaces (id, application_id, name, environment, created_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
            (ids.new_id(), application_id, DEFAULT_SPACE_NAME, DEFAULT_ENVIRONMENT, now),
        )
        space_id = runner.scalar(
            "SELECT id FROM spaces WHERE application_id = ? AND name = ? "
            "AND environment = ? ORDER BY created_at LIMIT 1",
            (application_id, DEFAULT_SPACE_NAME, DEFAULT_ENVIRONMENT),
        )
    bucket_id = runner.scalar(
        "SELECT id FROM buckets WHERE space_id = ? AND name = ?",
        (space_id, DEFAULT_BUCKET_NAME),
    )
    if bucket_id is None:
        runner.exec(
            "INSERT INTO buckets (id, space_id, name, is_default, created_at) "
            "VALUES (?, ?, ?, 1, ?) ON CONFLICT DO NOTHING",
            (ids.new_id(), space_id, DEFAULT_BUCKET_NAME, now),
        )
    # Backfill legacy users/objects so pre-#35 data keeps working.
    runner.exec("UPDATE users SET role = 'user' WHERE role IS NULL OR role = ''")
    # #M04: a space added before the status column is active by default.
    runner.exec("UPDATE spaces SET status = 'active' WHERE status IS NULL OR status = ''")
    runner.exec(
        "UPDATE users SET application_id = ? WHERE application_id IS NULL",
        (application_id,),
    )
    runner.exec("UPDATE users SET space_id = ? WHERE space_id IS NULL", (space_id,))
    runner.exec(
        "UPDATE objects SET space_id = "
        "(SELECT space_id FROM users WHERE users.id = objects.owner_id) "
        "WHERE space_id IS NULL"
    )
    runner.exec(
        "UPDATE objects SET application_id = "
        "(SELECT application_id FROM users WHERE users.id = objects.owner_id) "
        "WHERE application_id IS NULL"
    )
    runner.exec(
        "UPDATE objects SET environment = ? WHERE environment IS NULL",
        (DEFAULT_ENVIRONMENT,),
    )
