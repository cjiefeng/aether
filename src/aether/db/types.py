"""Column types and value conventions shared by all tables (spec §2.1).

- Timestamps: ISO-8601 UTC TEXT, second precision, `Z` suffix (lexicographically sortable).
- Money: INTEGER micros (x1e6) <-> `Decimal`. Floats are rejected.
- simhash: 64-bit unsigned in Python <-> signed INTEGER in SQLite.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from sqlalchemy import Integer, func, type_coerce
from sqlalchemy.engine import Dialect
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.types import TypeDecorator

MICROS = Decimal(1_000_000)
_U64 = 1 << 64
_I64_MAX = (1 << 63) - 1


def utcnow_iso() -> str:
    return to_iso(datetime.now(UTC))


def to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("naive datetime; use timezone-aware UTC")
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def decimal_to_micros(value: Decimal) -> int:
    if not isinstance(value, Decimal):
        raise TypeError(f"money must be Decimal, got {type(value).__name__}")
    return int((value * MICROS).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


def micros_to_decimal(value: int) -> Decimal:
    return Decimal(value) / MICROS


class Micros(TypeDecorator[Decimal]):
    """INTEGER micros in the DB, `Decimal` in Python. Never do money arithmetic in floats."""

    impl = Integer
    cache_ok = True

    def process_bind_param(self, value: Decimal | None, dialect: Dialect) -> int | None:
        return None if value is None else decimal_to_micros(value)

    def process_result_value(self, value: Any, dialect: Dialect) -> Decimal | None:
        return None if value is None else micros_to_decimal(int(value))


def u64_to_i64(value: int) -> int:
    if not 0 <= value < _U64:
        raise ValueError("simhash must be an unsigned 64-bit value")
    return value - _U64 if value > _I64_MAX else value


def i64_to_u64(value: int) -> int:
    if not -(1 << 63) <= value <= _I64_MAX:
        raise ValueError("value out of signed 64-bit range")
    return value + _U64 if value < 0 else value


def micros_sum(col: ColumnElement[Any]) -> ColumnElement[int]:
    """SUM over a `Micros` column as raw INTEGER micros (0 when empty). Without the coercion the
    result would be processed as `Decimal` dollars, like the column."""
    return func.coalesce(func.sum(type_coerce(col, Integer)), 0)
