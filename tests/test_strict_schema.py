from __future__ import annotations

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Engine, insert, text
from sqlalchemy.exc import IntegrityError

from aether.db import migrate
from aether.db.models import alerts, metadata


def test_all_tables_strict(ro_engine: Engine) -> None:
    with ro_engine.connect() as conn:
        rows = conn.execute(
            text("SELECT name, strict FROM pragma_table_list WHERE schema='main'")
        ).all()
    tables = {name: strict for name, strict in rows if not name.startswith("sqlite_")}
    tables.pop("alembic_version")  # Alembic's own bookkeeping table; the only exemption.
    assert set(tables) == set(metadata.tables)
    assert all(tables.values()), tables


def test_without_rowid_tables(ro_engine: Engine) -> None:
    with ro_engine.connect() as conn:
        rows = conn.execute(
            text("SELECT name, wr FROM pragma_table_list WHERE schema='main'")
        ).all()
    without_rowid = {name for name, wr in rows if wr}
    assert {"prices_daily", "qtum_holdings"} <= without_rowid
    expected = {
        t.name
        for t in metadata.tables.values()
        if t.dialect_options["sqlite"]["with_rowid"] is False
    }
    assert without_rowid == expected


def test_migrations_match_models(rw_engine: Engine) -> None:
    with rw_engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), metadata)
    assert diff == []


def test_at_head(ro_engine: Engine) -> None:
    assert migrate.current_revision(ro_engine) == migrate.head_revision()


def test_json_columns_must_be_valid(rw_engine: Engine) -> None:
    with pytest.raises(IntegrityError), rw_engine.begin() as conn:
        conn.execute(
            insert(alerts).values(
                kind="test",
                channel="dashboard",
                status="dashboard_only",
                text="t",
                created_at="2026-01-01T00:00:00Z",
                payload="{not json",
                dedupe_key="k",
            )
        )


def test_strict_rejects_wrong_type(rw_engine: Engine) -> None:
    with pytest.raises(IntegrityError), rw_engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO job_runs(job, started_at, status, rows_written) "
            "VALUES ('x', '2026-01-01T00:00:00Z', 'ok', 'lots')"
        )


def test_check_constraint_enum(rw_engine: Engine) -> None:
    with pytest.raises(IntegrityError), rw_engine.begin() as conn:
        conn.exec_driver_sql("INSERT INTO tickers(symbol, type) VALUES ('ACME', 'crypto')")


def test_m5_alert_rebuild_keeps_rows_and_strict(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """0006 rebuilds `alerts` (new kinds) without losing rows or STRICT."""
    from aether.db.engine import ensure_db_file, make_rw_engine

    path = tmp_path / "a.db"
    ensure_db_file(path)
    migrate.upgrade(path, "0005_portfolio")
    eng = make_rw_engine(path)
    with eng.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO alerts (kind, channel, status, text, created_at, dedupe_key) "
                "VALUES ('test','dashboard','dashboard_only','t','2026-01-01T00:00:00Z','k1')"
            )
        )
    migrate.upgrade(path)
    with eng.begin() as conn:
        assert conn.execute(text("SELECT dedupe_key FROM alerts")).scalars().all() == ["k1"]
        assert (
            conn.execute(text("SELECT strict FROM pragma_table_list WHERE name='alerts'")).scalar()
            == 1
        )
        conn.execute(
            text(
                "INSERT INTO alerts (kind, channel, status, text, created_at, dedupe_key) "
                "VALUES ('review_pack','dashboard','dashboard_only','t','x','k2')"
            )
        )
    eng.dispose()
