from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import Engine, column, func, insert, select
from sqlalchemy.dialects import mysql, postgresql

from aether.db.dialect import json_extract, upsert
from aether.db.engine import write_tx
from aether.db.models import llm_calls, tickers
from aether.db.types import (
    decimal_to_micros,
    i64_to_u64,
    micros_to_decimal,
    u64_to_i64,
    utcnow_iso,
)


def test_upsert_is_idempotent(rw_engine: Engine) -> None:
    rows = [{"symbol": "ACME", "type": "context", "active": 1}]
    for _ in range(3):
        with write_tx(rw_engine) as conn:
            upsert(conn, tickers, rows, key_cols=["symbol"])
    with write_tx(rw_engine) as conn:
        upsert(conn, tickers, [{"symbol": "ACME", "type": "etf", "active": 0}], ["symbol"])
    with rw_engine.connect() as conn:
        got = conn.execute(select(tickers)).all()
    assert len(got) == 1
    assert (got[0].type, got[0].active) == ("etf", 0)


def test_upsert_do_nothing(rw_engine: Engine) -> None:
    with write_tx(rw_engine) as conn:
        upsert(conn, tickers, [{"symbol": "ACME", "type": "etf"}], ["symbol"])
        upsert(conn, tickers, [{"symbol": "ACME", "type": "context"}], ["symbol"], update_cols=[])
    with rw_engine.connect() as conn:
        assert conn.execute(select(tickers.c.type)).scalar_one() == "etf"


def test_upsert_compiles_for_other_dialects() -> None:
    from sqlalchemy.dialects.mysql import insert as my_insert
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    pg = pg_insert(tickers).on_conflict_do_update(
        index_elements=["symbol"], set_={"type": pg_insert(tickers).excluded.type}
    )
    assert "ON CONFLICT" in str(pg.compile(dialect=postgresql.dialect()))
    my = my_insert(tickers)
    assert "ON DUPLICATE KEY UPDATE" in str(
        my.on_duplicate_key_update(type=my.inserted.type).compile(dialect=mysql.dialect())
    )


def test_json_extract(rw_engine: Engine) -> None:
    with rw_engine.connect() as conn:
        v = conn.execute(select(json_extract(func.json('{"a": {"b": 7}}'), "$.a.b", "sqlite")))
        assert v.scalar_one() == 7
    pg = json_extract(column("payload"), "$.a.b", "postgresql")
    assert "jsonb_extract_path_text" in str(pg.compile(dialect=postgresql.dialect()))


def test_micros_roundtrip(rw_engine: Engine) -> None:
    cost = Decimal("0.012345")
    with write_tx(rw_engine) as conn:
        conn.execute(
            insert(llm_calls).values(
                purpose="test", model="synthetic", cost_micros=cost, created_at=utcnow_iso()
            )
        )
    with rw_engine.connect() as conn:
        assert conn.execute(select(llm_calls.c.cost_micros)).scalar_one() == cost
        raw = conn.exec_driver_sql("SELECT typeof(cost_micros), cost_micros FROM llm_calls").one()
    assert tuple(raw) == ("integer", 12345)


def test_money_rejects_float() -> None:
    with pytest.raises(TypeError):
        decimal_to_micros(0.1)  # type: ignore[arg-type]
    assert micros_to_decimal(decimal_to_micros(Decimal("1.5"))) == Decimal("1.5")


@pytest.mark.parametrize("u", [0, 1, (1 << 63) - 1, 1 << 63, (1 << 64) - 1])
def test_simhash_signed_roundtrip(u: int) -> None:
    s = u64_to_i64(u)
    assert -(1 << 63) <= s < (1 << 63)
    assert i64_to_u64(s) == u
