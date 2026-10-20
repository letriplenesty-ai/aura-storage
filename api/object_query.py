"""Shared filter/sort grammar for the object catalogue list (M02).

Kept in one place so the SQLite (``api/db.py``) and PostgreSQL (``api/db_pg.py``)
repositories cannot drift: both call :func:`build_object_where` with their own
placeholder style. Only whitelisted identifiers ever reach the statement, so a
caller-supplied sort key is never interpolated into SQL.
"""
from typing import Optional, Tuple

# Query-layer sort keys mapped to SQL expressions. ``display_name`` sorts
# case-insensitively so "Zebra" and "apple" order the way a reader expects.
SORT_COLUMNS = {
    "created_at": "created_at",
    "display_name": "LOWER(display_name)",
    "size_bytes": "size_bytes",
}

# Statuses the catalogue can hold, mirroring what the API writes.
OBJECT_STATUSES = (
    "pending", "verifying", "available", "deleted", "expired", "failed", "aborted",
    "purging",
)

# LIKE escape character (a single backslash) and the matching SQL clause.
LIKE_ESCAPE = "\\"
_ESCAPE_SQL = f"ESCAPE '{LIKE_ESCAPE}'"


def like_contains(value: str) -> str:
    """A ``%...%`` pattern that treats user input literally.

    ``%`` and ``_`` are LIKE wildcards and ``\\`` is the escape character; each is
    escaped so searching for ``100%`` only matches a literal ``100%``.
    """
    escaped = (
        value.replace(LIKE_ESCAPE, LIKE_ESCAPE * 2)
        .replace("%", LIKE_ESCAPE + "%")
        .replace("_", LIKE_ESCAPE + "_")
    )
    return f"%{escaped}%"


def build_object_where(
    *,
    ph: str = "?",
    space_id: str,
    owner_id: Optional[str] = None,
    folder_id: Optional[str] = None,
    q: Optional[str] = None,
    status: Optional[str] = None,
    hide_deleted: bool = False,
    content_type: Optional[str] = None,
    min_size: Optional[int] = None,
    max_size: Optional[int] = None,
    created_after: Optional[int] = None,
    created_before: Optional[int] = None,
) -> Tuple[str, list]:
    """Return ``(where_sql, params)`` for an object list query.

    ``folder_id`` keeps its #62 semantics: an empty string means "not filed in any
    folder" (``folder_id IS NULL``), any other value matches that folder. Every
    optional filter is skipped when ``None``/empty, so the default call is the
    historical "every visible object" query.
    """
    clauses = [f"space_id = {ph}"]
    params: list = [space_id]
    if owner_id is not None:
        clauses.append(f"owner_id = {ph}")
        params.append(owner_id)
    if folder_id is not None:
        if folder_id == "":
            clauses.append("folder_id IS NULL")
        else:
            clauses.append(f"folder_id = {ph}")
            params.append(folder_id)
    if q:
        clauses.append(
            f"LOWER(display_name) LIKE LOWER({ph}) {_ESCAPE_SQL}"
        )
        params.append(like_contains(q))
    if status is not None:
        clauses.append(f"status = {ph}")
        params.append(status)
    elif hide_deleted:
        # Default listing hides soft-deleted rows so a delete visibly removes
        # the file; pass status=deleted explicitly (future trash view) to see
        # them.
        # A purge in progress is hidden too: the object is on its way out.
        clauses.append(f"status NOT IN ({ph}, {ph})")
        params.extend(["deleted", "purging"])
    if content_type is not None:
        clauses.append(f"LOWER(content_type) = LOWER({ph})")
        params.append(content_type)
    if min_size is not None:
        clauses.append(f"size_bytes >= {ph}")
        params.append(min_size)
    if max_size is not None:
        clauses.append(f"size_bytes <= {ph}")
        params.append(max_size)
    if created_after is not None:
        clauses.append(f"created_at >= {ph}")
        params.append(created_after)
    if created_before is not None:
        clauses.append(f"created_at <= {ph}")
        params.append(created_before)
    return " AND ".join(clauses), params


def order_clause(*, sort: str = "created_at", order: str = "desc") -> str:
    """A whitelisted ORDER BY. ``id`` is a stable tiebreaker for OFFSET paging."""
    column = SORT_COLUMNS.get(sort, SORT_COLUMNS["created_at"])
    direction = "ASC" if order == "asc" else "DESC"
    return f"ORDER BY {column} {direction}, id ASC"
