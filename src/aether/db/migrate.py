"""Programmatic Alembic entry points (`python -m aether.db.migrate`)."""

from __future__ import annotations

import sys
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine

from aether.config import get_settings


def alembic_config(db_path: Path | None = None) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", "aether.db:migrations")
    if db_path is not None:
        cfg.attributes["db_path"] = db_path
    return cfg


def upgrade(db_path: Path, revision: str = "head") -> None:
    command.upgrade(alembic_config(db_path), revision)


def head_revision() -> str | None:
    return ScriptDirectory.from_config(alembic_config()).get_current_head()


def current_revision(engine: Engine) -> str | None:
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def main() -> int:
    upgrade(get_settings().db_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
