"""Open risk flags (spec §6.1 "risk load"): lock-up within N days, insider-selling cluster,
active ATM / shelf, going-concern language. Read-only; thresholds from `config/rubric.yaml`.

M3 alerts and M9 scorecards consume these. Nothing here writes.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import Engine, func, select

from aether.config import RiskFlagParams
from aether.db.models import capital_structure, filings, insider_txns, lockups

PERIODIC = ("10-K", "10-Q", "10-K/A", "10-Q/A")


@dataclass(frozen=True)
class Flag:
    symbol: str
    kind: str  # lockup_expiry | insider_cluster | active_atm | active_shelf | going_concern
    detail: str
    as_of: str  # the date the flag is anchored to (expiry, window end, filing date)
    url: str | None = None


@dataclass(frozen=True)
class Sale:
    insider_key: str  # reporting-owner CIK when present, else the name
    insider: str
    txn_date: date
    shares: int
    is_10b5_1: bool


@dataclass(frozen=True)
class Cluster:
    start: date
    end: date
    insiders: tuple[str, ...]  # display names
    n_insiders: int
    shares: int
    plan_sales: int  # sales under a 10b5-1 plan
    sales: int


def cluster_in_window(
    sales: Iterable[Sale], end: date, window_days: int, min_insiders: int
) -> Cluster | None:
    """Distinct insiders with an open-market sale in (end - window_days, end]."""
    start = end - timedelta(days=window_days)
    hits = [s for s in sales if start < s.txn_date <= end]
    if len({s.insider_key for s in hits}) < min_insiders:
        return None
    return Cluster(
        start=min(s.txn_date for s in hits),
        end=max(s.txn_date for s in hits),
        insiders=tuple(sorted({s.insider for s in hits})),
        n_insiders=len({s.insider_key for s in hits}),
        shares=sum(s.shares for s in hits),
        plan_sales=sum(1 for s in hits if s.is_10b5_1),
        sales=len(hits),
    )


def load_sales(engine: Engine, symbol: str, since: date) -> list[Sale]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                func.coalesce(insider_txns.c.insider_cik, insider_txns.c.insider),
                insider_txns.c.insider,
                insider_txns.c.txn_date,
                insider_txns.c.shares,
                insider_txns.c.is_10b5_1,
            ).where(
                insider_txns.c.symbol == symbol,
                insider_txns.c.code == "S",
                insider_txns.c.is_derivative == 0,
                insider_txns.c.txn_date > since.isoformat(),
            )
        ).all()
    # Key on the insider CIK when present (names vary between filings); display one name.
    names = {key: name for key, name, *_ in rows}
    return [
        Sale(key, names[key], date.fromisoformat(d), shares or 0, bool(plan))
        for key, _name, d, shares, plan in rows
    ]


def open_flags(
    engine: Engine, params: RiskFlagParams, today: date, symbols: Sequence[str]
) -> list[Flag]:
    out: list[Flag] = []
    syms = list(symbols)
    horizon = (today + timedelta(days=params.lockup_window_days)).isoformat()
    with engine.connect() as conn:
        for sym, expiry, days, url in conn.execute(
            select(lockups.c.symbol, lockups.c.expiry_date, lockups.c.lockup_days, filings.c.url)
            .join(filings, filings.c.accession == lockups.c.accession)
            .where(
                lockups.c.symbol.in_(syms),
                lockups.c.expiry_date >= today.isoformat(),
                lockups.c.expiry_date <= horizon,
            )
        ).all():
            left = (date.fromisoformat(expiry) - today).days
            out.append(
                Flag(
                    sym, "lockup_expiry", f"{days}-day IPO lock-up ends in {left} days", expiry, url
                )
            )

        for kind, instrument, days in (
            ("active_atm", "atm", params.atm_active_days),
            ("active_shelf", "shelf", params.shelf_active_days),
        ):
            cutoff = (today - timedelta(days=days)).isoformat()
            for sym, as_of, url in conn.execute(
                select(
                    capital_structure.c.symbol,
                    func.max(capital_structure.c.as_of),
                    func.max(filings.c.url),
                )
                .join(filings, filings.c.accession == capital_structure.c.source_accession)
                .where(
                    capital_structure.c.symbol.in_(syms),
                    capital_structure.c.instrument == instrument,
                    capital_structure.c.as_of >= cutoff,
                )
                .group_by(capital_structure.c.symbol)
            ).all():
                label = "at-the-market program" if instrument == "atm" else "shelf registration"
                out.append(Flag(sym, kind, f"{label} filed {as_of}", as_of, url))

        latest = (
            select(filings.c.symbol, func.max(filings.c.filed_at).label("d"))
            .where(filings.c.form.in_(PERIODIC), filings.c.parsed.is_not(None))
            .group_by(filings.c.symbol)
            .subquery()
        )
        for sym, form, filed_at, parsed, url in conn.execute(
            select(
                filings.c.symbol,
                filings.c.form,
                filings.c.filed_at,
                filings.c.parsed,
                filings.c.url,
            )
            .join(
                latest,
                (filings.c.symbol == latest.c.symbol) & (filings.c.filed_at == latest.c.d),
            )
            .where(filings.c.symbol.in_(syms), filings.c.form.in_(PERIODIC))
        ).all():
            if json.loads(parsed).get("going_concern"):
                out.append(
                    Flag(sym, "going_concern", f"going-concern language in {form}", filed_at, url)
                )

    window = params.insider_cluster_window_days
    for sym in syms:
        c = cluster_in_window(
            load_sales(engine, sym, today - timedelta(days=window)),
            today,
            window,
            params.insider_cluster_min_insiders,
        )
        if c is not None:
            out.append(
                Flag(
                    sym,
                    "insider_cluster",
                    f"{c.n_insiders} insiders sold {c.shares:,} shares in {window} days "
                    f"({c.plan_sales} of {c.sales} sales under 10b5-1 plans)",
                    c.end.isoformat(),
                )
            )
    return sorted(out, key=lambda f: (f.symbol, f.kind))
