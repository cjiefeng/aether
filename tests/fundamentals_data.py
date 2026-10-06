"""Synthetic XBRL fundamentals for the M9 tests. Symbols and accessions are made up; every number
is chosen by the test."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import Engine

from aether.db.dialect import upsert
from aether.db.engine import write_tx
from aether.db.models import fundamentals_q

COMMON = "dei:EntityCommonStockSharesOutstanding"
CASH = "us-gaap:CashAndCashEquivalentsAtCarryingValue"
STI = "us-gaap:ShortTermInvestments"
OCF = "us-gaap:NetCashProvidedByUsedInOperatingActivities"
REV = "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"
WARRANTS = "us-gaap:ClassOfWarrantOrRightOutstanding"
OPTIONS = (
    "us-gaap:ShareBasedCompensationArrangementByShareBasedPaymentAwardOptionsOutstandingNumber"
)
DEBT = "us-gaap:LongTermDebtNoncurrent"

_seq = [0]


def fact(
    symbol: str,
    concept: str,
    end: str,
    value: float | int,
    *,
    days: int = 0,
    filed: str | None = None,
    form: str = "10-Q",
) -> dict:
    """One `fundamentals_q` row. Money concepts are USD; share concepts are integers."""
    _seq[0] += 1
    shares = concept in (COMMON, WARRANTS, OPTIONS) or concept.endswith(("Number", "Outstanding"))
    filed = filed or (date.fromisoformat(end) + timedelta(days=40)).isoformat()
    return {
        "symbol": symbol,
        "period_end": end,
        "concept": concept,
        "period_days": days,
        "value_micros": None if shares else Decimal(str(value)),
        "value_int": int(value) if shares else None,
        "unit": "shares" if shares else "USD",
        "fy": None,
        "fp": None,
        "form": form,
        "accession": f"0009999998-26-{_seq[0]:06d}",
        "filed": filed,
    }


def add_facts(engine: Engine, rows: list[dict]) -> None:
    with write_tx(engine) as conn:
        upsert(
            conn, fundamentals_q, rows, key_cols=["symbol", "period_end", "concept", "period_days"]
        )


def cash_flow_history(symbol: str, quarterly_ocf: float) -> list[dict]:
    """FY2025 + 2025/2026 YTD operating cash flow, as 10-Qs report it (Q1, H1, 9M, FY)."""
    q = quarterly_ocf
    return [
        fact(symbol, OCF, "2025-03-31", q, days=90),
        fact(symbol, OCF, "2025-06-30", 2 * q, days=181),
        fact(symbol, OCF, "2025-09-30", 3 * q, days=273),
        fact(symbol, OCF, "2025-12-31", 4 * q, days=365, form="10-K"),
        fact(symbol, OCF, "2026-03-31", q, days=90),
        fact(symbol, OCF, "2026-06-30", 2 * q, days=181),
    ]
