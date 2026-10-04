"""Alembic environment. Always render_as_batch (SQLite's ALTER TABLE is limited).

Batch mode recreates tables; check that STRICT survives (tests/test_strict_schema.py) and
pass `sqlite_strict=True` via `table_kwargs` in batch_alter_table when needed.
"""

from __future__ import annotations

from pathlib import Path

from alembic import context

from aether.config import get_settings
from aether.db.engine import make_rw_engine
from aether.db.models import metadata

config = context.config


def run_migrations_online() -> None:
    db_path = config.attributes.get("db_path") or get_settings().db_path
    engine = make_rw_engine(Path(db_path))
    try:
        with engine.connect() as conn:
            context.configure(
                connection=conn,
                target_metadata=metadata,
                render_as_batch=True,
                transaction_per_migration=True,
                compare_type=True,
            )
            with context.begin_transaction():
                context.run_migrations()
            if conn.in_transaction():
                conn.commit()
    finally:
        engine.dispose()


if context.is_offline_mode():
    raise SystemExit("offline migrations are not supported")
run_migrations_online()
