"""Dialect-specific SQL lives here and nowhere else, so switching to MySQL/Postgres later is
a new DSN plus migrations (spec §2.1)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import Connection, Table, cast, func
from sqlalchemy.sql.elements import ColumnElement

CHUNK = 500


def upsert(
    conn: Connection,
    table: Table,
    rows: Sequence[Mapping[str, Any]],
    key_cols: Sequence[str],
    update_cols: Sequence[str] | None = None,
) -> int:
    """Idempotent INSERT ... ON CONFLICT DO UPDATE. Returns the number of rows submitted.

    `update_cols=None` updates every non-key column present in the rows; `[]` means DO NOTHING.
    """
    if not rows:
        return 0
    if update_cols is None:
        update_cols = [c for c in rows[0] if c not in key_cols]
    name = conn.dialect.name

    stmt: Any
    if name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        stmt = sqlite_insert(table)
    elif name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        stmt = pg_insert(table)
    if name in ("sqlite", "postgresql"):
        if update_cols:
            stmt = stmt.on_conflict_do_update(
                index_elements=list(key_cols),
                set_={c: stmt.excluded[c] for c in update_cols},
            )
        else:
            stmt = stmt.on_conflict_do_nothing(index_elements=list(key_cols))
    elif name in ("mysql", "mariadb"):
        from sqlalchemy.dialects.mysql import insert as my_insert

        my_stmt = my_insert(table)
        cols = update_cols or list(key_cols)[:1]  # no-op update emulates DO NOTHING
        stmt = my_stmt.on_duplicate_key_update(
            {c: (my_stmt.inserted[c] if update_cols else table.c[c]) for c in cols}
        )
    else:
        raise NotImplementedError(f"upsert not implemented for dialect {name!r}")

    for i in range(0, len(rows), CHUNK):
        conn.execute(stmt, [dict(r) for r in rows[i : i + CHUNK]])
    return len(rows)


def json_extract(col: ColumnElement[Any], path: str, dialect_name: str) -> ColumnElement[Any]:
    """Extract a scalar from a JSON TEXT column. `path` is a JSONPath like `$.a.b`."""
    if dialect_name in ("sqlite", "mysql", "mariadb"):
        return func.json_extract(col, path)
    if dialect_name == "postgresql":
        from sqlalchemy.dialects.postgresql import JSONB

        parts = [p for p in path.removeprefix("$").split(".") if p]
        return func.jsonb_extract_path_text(cast(col, JSONB), *parts)
    raise NotImplementedError(f"json_extract not implemented for dialect {dialect_name!r}")
