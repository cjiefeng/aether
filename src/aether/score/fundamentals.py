"""Point-in-time fundamentals from XBRL `fundamentals_q` (spec §6.1, §6.6.1). Pure reads; no LLM.

Only facts filed on or before `as_of` are used, so a past publish date sees exactly what was public
then. Nothing is estimated: a missing concept is reported as "not tagged" with a reason.

- **TTM flows** (revenue, operating cash flow): the fiscal-year value if one ends at the latest
  period; otherwise prior FY + year-to-date - the prior year's same YTD (10-Qs report Q2/Q3 only as
  YTD).
- **Fully diluted shares** = common shares outstanding (cover-page count, else balance sheet) +
  warrants + options + unvested RSUs + shares underlying convertibles, each the latest instant
  within `COMPONENT_MAX_AGE` days before the common-share date. Untagged components are listed.
- **FD YoY**: the same component set at the latest common-share date and about a year earlier
  (+/-`YOY_TOLERANCE` days). The base must be dated on or after the stock's first trading session:
  pre-listing counts belong to a SPAC or a private company (owner decision 2026-10-06).
- **Liquidity** = cash and equivalents + current and non-current marketable debt securities (one
  concept per bucket, by priority). TTM flows and balances older than `MAX_AGE` days are stale.
- **Runway** = liquidity / quarterly burn x 3 months, burn = -TTM operating cash flow / 4;
  "not burning" if TTM operating cash flow >= 0.
- **EV** = FD shares x last close + debt + convertibles - liquidity;
  **EV/Sales** = EV / TTM revenue.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Connection, func, select

from aether.db.models import fundamentals_q, prices_daily

REVENUE = (
    "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
    "us-gaap:RevenueFromContractWithCustomerIncludingAssessedTax",
    "us-gaap:Revenues",
)
OCF = ("us-gaap:NetCashProvidedByUsedInOperatingActivities",)
# M14 thesis red flags (spec §6.10). One concept per name, by priority, never summed.
OPEX = ("us-gaap:OperatingExpenses", "us-gaap:CostsAndExpenses")
ACQUISITIONS = ("us-gaap:PaymentsToAcquireBusinessesNetOfCashAcquired",)
CASH = "us-gaap:CashAndCashEquivalentsAtCarryingValue"
# Priority order within each bucket; one concept per bucket is used, never summed (filers tag the
# same holding under several of them). `LongTermInvestments` is left out: it can hold strategic
# equity stakes, not just marketable debt.
SHORT_TERM = (
    "us-gaap:ShortTermInvestments",
    "us-gaap:MarketableSecuritiesCurrent",
    "us-gaap:AvailableForSaleSecuritiesDebtSecuritiesCurrent",
    "us-gaap:DebtSecuritiesAvailableForSaleExcludingAccruedInterestCurrent",
)
LONG_TERM = (
    "us-gaap:AvailableForSaleSecuritiesDebtSecuritiesNoncurrent",
    "us-gaap:DebtSecuritiesAvailableForSaleExcludingAccruedInterestNoncurrent",
    "us-gaap:MarketableSecuritiesNoncurrent",
)
DEBT = ("us-gaap:LongTermDebt", "us-gaap:LongTermDebtNoncurrent")
CONVERTIBLE = (
    "us-gaap:ConvertibleNotesPayable",
    "us-gaap:ConvertibleDebtNoncurrent",
    "us-gaap:ConvertibleSeniorNotesNoncurrent",
    "us-gaap:ConvertibleNotesPayableCurrent",
)
COMMON = ("dei:EntityCommonStockSharesOutstanding", "us-gaap:CommonStockSharesOutstanding")
FD_COMPONENTS = {
    "warrants": "us-gaap:ClassOfWarrantOrRightOutstanding",
    "options": "us-gaap:ShareBasedCompensationArrangementByShareBasedPaymentAward"
    "OptionsOutstandingNumber",
    "rsus": "us-gaap:ShareBasedCompensationArrangementByShareBasedPaymentAward"
    "EquityInstrumentsOtherThanOptionsNonvestedNumber",
    "convertible_shares": "us-gaap:DebtInstrumentConvertibleNumberOfEquityInstruments",
}
COMPONENT_MAX_AGE = 400  # days before the common-share date
MAX_AGE = 400  # a TTM flow or balance older than this (vs as_of) is stale, not used
BALANCE_MAX_GAP = 120  # debt/investments must be this close to the cash date
YOY_TOLERANCE = 45
DUR_TOL = 12


@dataclass(frozen=True)
class Fact:
    concept: str
    period_end: date
    days: int
    value: Decimal
    accession: str | None
    form: str | None
    filed: str | None

    def ref(self) -> dict[str, Any]:
        return {
            "concept": self.concept,
            "period_end": self.period_end.isoformat(),
            "accession": self.accession,
            "form": self.form,
        }


def load_facts(conn: Connection, symbol: str, as_of: date) -> list[Fact]:
    rows = conn.execute(
        select(fundamentals_q).where(
            fundamentals_q.c.symbol == symbol,
            fundamentals_q.c.filed.is_not(None),
            fundamentals_q.c.filed <= as_of.isoformat(),
        )
    ).all()
    out = []
    for r in rows:
        v = r.value_micros if r.value_micros is not None else Decimal(r.value_int)
        out.append(
            Fact(
                r.concept,
                date.fromisoformat(r.period_end),
                r.period_days,
                Decimal(v),
                r.accession,
                r.form,
                r.filed,
            )
        )
    return sorted(out, key=lambda f: (f.concept, f.period_end, f.days))


# --------------------------------------------------------------------------- pure helpers


def _of(facts: Iterable[Fact], concept: str) -> list[Fact]:
    return [f for f in facts if f.concept == concept]


def latest_instant(
    facts: Iterable[Fact], concept: str, *, on_or_before: date | None = None
) -> Fact | None:
    xs = [
        f
        for f in _of(facts, concept)
        if f.days == 0 and (on_or_before is None or f.period_end <= on_or_before)
    ]
    return max(xs, key=lambda f: f.period_end) if xs else None


def instant_near(facts: Iterable[Fact], concept: str, target: date, tol: int) -> Fact | None:
    xs = [
        f for f in _of(facts, concept) if f.days == 0 and abs((f.period_end - target).days) <= tol
    ]
    return min(xs, key=lambda f: (abs((f.period_end - target).days), f.period_end)) if xs else None


def _duration(facts: Sequence[Fact], end: date, days: int, tol: int = DUR_TOL) -> Fact | None:
    xs = [
        f
        for f in facts
        if f.days > 0 and abs((f.period_end - end).days) <= 3 and abs(f.days - days) <= tol
    ]
    return min(xs, key=lambda f: abs(f.days - days)) if xs else None


@dataclass(frozen=True)
class Ttm:
    concept: str
    end: date
    value: Decimal
    method: str  # 'fy' | 'ytd'
    refs: tuple[dict[str, Any], ...]


def ttm_at(facts: Sequence[Fact], end: date) -> Ttm | None:
    """TTM for one concept's facts ending at `end` (see module docstring)."""
    fy = _duration(facts, end, 365)
    if fy is not None:
        return Ttm(fy.concept, end, fy.value, "fy", (fy.ref(),))
    prior_fys = [f for f in facts if 350 <= f.days <= 380 and f.period_end < end]
    if not prior_fys:
        return None
    pfy = max(prior_fys, key=lambda f: f.period_end)
    ytd_days = (end - pfy.period_end).days
    if not 80 <= ytd_days <= 290:
        return None
    ytd = _duration(facts, end, ytd_days)
    prev = _duration(facts, end - timedelta(days=365), ytd_days, tol=DUR_TOL + 3) or _duration(
        facts, end - timedelta(days=364), ytd_days, tol=DUR_TOL + 3
    )
    if ytd is None or prev is None:
        return None
    return Ttm(
        ytd.concept,
        end,
        pfy.value + ytd.value - prev.value,
        "ytd",
        (pfy.ref(), ytd.ref(), prev.ref()),
    )


def latest_ttm(facts: Sequence[Fact], concepts: Sequence[str]) -> Ttm | None:
    """The most recent computable TTM across `concepts` (earlier concepts win ties)."""
    best: Ttm | None = None
    for c in concepts:
        fs = _of(facts, c)
        for end in sorted({f.period_end for f in fs if f.days > 0}, reverse=True):
            t = ttm_at(fs, end)
            if t is not None:
                if best is None or t.end > best.end:
                    best = t
                break
    return best


def ttm_year_ago(facts: Sequence[Fact], cur: Ttm) -> Ttm | None:
    fs = _of(facts, cur.concept)
    for delta in (365, 364, 366, 371, 357):
        target = cur.end - timedelta(days=delta)
        ends = sorted({f.period_end for f in fs if abs((f.period_end - target).days) <= 3})
        for e in ends:
            t = ttm_at(fs, e)
            if t is not None:
                return t
    return None


def ttm_history(facts: Sequence[Fact], concepts: Sequence[str], n: int) -> list[Ttm]:
    """Up to `n` consecutive quarterly TTMs of the first concept with a computable latest TTM,
    newest first (each about 91 days before the previous). Stops at the first gap."""
    cur = latest_ttm(facts, concepts)
    if cur is None:
        return []
    fs = _of(facts, cur.concept)
    out = [cur]
    while len(out) < n:
        prev_end = out[-1].end
        ends = sorted(
            {
                f.period_end
                for f in fs
                if f.days > 0 and 80 <= (prev_end - f.period_end).days <= 100
            },
            reverse=True,
        )
        nxt = next((t for e in ends if (t := ttm_at(fs, e)) is not None), None)
        if nxt is None:
            break
        out.append(nxt)
    return out


# --------------------------------------------------------------------------- fully diluted


@dataclass(frozen=True)
class FullyDiluted:
    total: int
    common: Fact
    components: dict[str, Fact]
    missing: tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "common": {**self.common.ref(), "value": int(self.common.value)},
            "components": {
                k: {**f.ref(), "value": int(f.value)} for k, f in sorted(self.components.items())
            },
            "not_tagged": list(self.missing),
        }


def fully_diluted_at(
    facts: Sequence[Fact], common: Fact, only: Iterable[str] | None = None
) -> FullyDiluted:
    names = list(FD_COMPONENTS) if only is None else [n for n in FD_COMPONENTS if n in set(only)]
    comps: dict[str, Fact] = {}
    missing = []
    for name in names:
        f = latest_instant(facts, FD_COMPONENTS[name], on_or_before=common.period_end)
        if f is not None and (common.period_end - f.period_end).days <= COMPONENT_MAX_AGE:
            comps[name] = f
        else:
            missing.append(name)
    total = int(common.value) + sum(int(f.value) for f in comps.values())
    return FullyDiluted(total, common, comps, tuple(missing))


def common_shares(facts: Sequence[Fact]) -> Fact | None:
    for c in COMMON:
        f = latest_instant(facts, c)
        if f is not None:
            return f
    return None


def fd_yoy(facts: Sequence[Fact], first_session: date | None) -> dict[str, Any]:
    """FD shares now vs about a year earlier, same concept and same component set."""
    for c in COMMON:
        cur = latest_instant(facts, c)
        if cur is None:
            continue
        target = cur.period_end - timedelta(days=365)
        base = instant_near(facts, c, target, YOY_TOLERANCE)
        if base is None:
            continue
        if first_session is not None and base.period_end < first_session:
            return {
                "value": None,
                "reason": f"listed < 1 year (first session {first_session.isoformat()})",
            }
        now_fd = fully_diluted_at(facts, cur)
        then_fd = fully_diluted_at(facts, base)
        shared = sorted(set(now_fd.components) & set(then_fd.components))
        now_fd = fully_diluted_at(facts, cur, shared)
        then_fd = fully_diluted_at(facts, base, shared)
        if then_fd.total <= 0:
            continue
        return {
            "value": now_fd.total / then_fd.total - 1.0,
            "from": then_fd.to_json(),
            "to": now_fd.to_json(),
            "components_compared": ["common", *shared],
            "reason": None,
        }
    if first_session is not None and common_shares(facts) is not None:
        cur = common_shares(facts)
        assert cur is not None
        if cur.period_end - timedelta(days=365 - YOY_TOLERANCE) < first_session:
            return {
                "value": None,
                "reason": f"listed < 1 year (first session {first_session.isoformat()})",
            }
    if common_shares(facts) is None:
        return {"value": None, "reason": "no company-wide common share count tagged"}
    return {"value": None, "reason": "no share count tagged about a year earlier"}


# --------------------------------------------------------------------------- snapshot


@dataclass
class Snapshot:
    """Every fundamentals metric for one symbol at one date, with sources and reasons."""

    symbol: str
    as_of: date
    revenue_ttm: Ttm | None = None
    revenue_growth: float | None = None
    ocf_ttm: Ttm | None = None
    liquidity: Decimal | None = None
    liquidity_refs: list[dict[str, Any]] = field(default_factory=list)
    quarterly_burn: Decimal | None = None
    not_burning: bool = False
    runway_months: float | None = None
    fd: FullyDiluted | None = None
    fd_yoy: dict[str, Any] = field(default_factory=dict)
    debt: Decimal = Decimal(0)
    convertibles: Decimal = Decimal(0)
    balance_refs: list[dict[str, Any]] = field(default_factory=list)
    price: float | None = None
    price_date: str | None = None
    market_cap_fd: Decimal | None = None
    ev: Decimal | None = None
    ev_sales: float | None = None
    instruments_pct: float | None = None
    reasons: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        def money(v: Decimal | None) -> float | None:
            return None if v is None else float(v)

        def ttm(t: Ttm | None) -> dict[str, Any] | None:
            if t is None:
                return None
            return {
                "value": float(t.value),
                "end": t.end.isoformat(),
                "method": t.method,
                "refs": list(t.refs),
            }

        return {
            "as_of": self.as_of.isoformat(),
            "revenue_ttm": ttm(self.revenue_ttm),
            "revenue_growth": self.revenue_growth,
            "ocf_ttm": ttm(self.ocf_ttm),
            "liquidity": money(self.liquidity),
            "liquidity_refs": self.liquidity_refs,
            "quarterly_burn": money(self.quarterly_burn),
            "not_burning": self.not_burning,
            "runway_months": self.runway_months,
            "fd_shares": self.fd.to_json() if self.fd else None,
            "fd_yoy": self.fd_yoy,
            "debt": money(self.debt),
            "convertibles": money(self.convertibles),
            "balance_refs": self.balance_refs,
            "price": self.price,
            "price_date": self.price_date,
            "market_cap_fd": money(self.market_cap_fd),
            "ev": money(self.ev),
            "ev_sales": self.ev_sales,
            "instruments_pct": self.instruments_pct,
            "reasons": dict(sorted(self.reasons.items())),
        }


def _liquidity(facts: Sequence[Fact], snap: Snapshot) -> date | None:
    cash = latest_instant(facts, CASH)
    if cash is None:
        snap.reasons["liquidity"] = "cash not tagged"
        return None
    if (snap.as_of - cash.period_end).days > MAX_AGE:
        snap.reasons["liquidity"] = f"latest cash figure is stale ({cash.period_end.isoformat()})"
        return None
    total = cash.value
    snap.liquidity_refs.append({**cash.ref(), "value": float(cash.value)})
    for bucket in (SHORT_TERM, LONG_TERM):
        for c in bucket:
            f = instant_near(facts, c, cash.period_end, 0)
            if f is not None:
                total += f.value
                snap.liquidity_refs.append({**f.ref(), "value": float(f.value)})
                break
    snap.liquidity = total
    return cash.period_end


def _balance(facts: Sequence[Fact], cash_date: date, concepts: Sequence[str]) -> Fact | None:
    for c in concepts:
        f = latest_instant(facts, c, on_or_before=cash_date)
        if f is not None and (cash_date - f.period_end).days <= BALANCE_MAX_GAP:
            return f
    return None


def fresh(t: Ttm | None, as_of: date, snap: Snapshot, key: str) -> Ttm | None:
    """`t` unless it ends more than `MAX_AGE` days before `as_of` (then a reason, no value)."""
    if t is not None and (as_of - t.end).days > MAX_AGE:
        snap.reasons[key] = f"latest TTM ends {t.end.isoformat()} (stale)"
        return None
    return t


def compute(
    facts: Sequence[Fact],
    symbol: str,
    as_of: date,
    *,
    first_session: date | None,
    price: tuple[str, float] | None,
) -> Snapshot:
    snap = Snapshot(symbol, as_of)

    snap.revenue_ttm = fresh(latest_ttm(facts, REVENUE), as_of, snap, "revenue")
    if snap.revenue_ttm is None:
        snap.reasons.setdefault("revenue", "no TTM revenue computable from tagged facts")
    else:
        prev = ttm_year_ago(facts, snap.revenue_ttm)
        if prev is not None and prev.value > 0:
            snap.revenue_growth = float(snap.revenue_ttm.value / prev.value) - 1.0
        else:
            snap.reasons["revenue_growth"] = "no TTM revenue a year earlier"

    cash_date = _liquidity(facts, snap)
    snap.ocf_ttm = fresh(latest_ttm(facts, OCF), as_of, snap, "runway")
    if snap.ocf_ttm is None:
        snap.reasons.setdefault("runway", "no TTM operating cash flow computable")
    elif snap.ocf_ttm.value >= 0:
        snap.not_burning = True
    else:
        snap.quarterly_burn = -snap.ocf_ttm.value / 4
        if snap.liquidity is not None:
            snap.runway_months = float(snap.liquidity / snap.quarterly_burn * 3)
        else:
            snap.reasons["runway"] = "cash not tagged"

    common = common_shares(facts)
    if common is None:
        snap.reasons["fd_shares"] = "no company-wide common share count tagged"
    else:
        snap.fd = fully_diluted_at(facts, common)
        inst = sum(
            int(f.value)
            for k, f in snap.fd.components.items()
            if k in ("warrants", "convertible_shares")
        )
        if common.value > 0:
            snap.instruments_pct = inst / float(common.value)
    snap.fd_yoy = fd_yoy(facts, first_session)

    if cash_date is not None:
        d = _balance(facts, cash_date, DEBT)
        if d is not None:
            snap.debt = d.value
            snap.balance_refs.append({**d.ref(), "value": float(d.value)})
        cv = _balance(facts, cash_date, CONVERTIBLE)
        if cv is not None:
            snap.convertibles = cv.value
            snap.balance_refs.append({**cv.ref(), "value": float(cv.value)})

    if price is not None:
        snap.price_date, snap.price = price
    if snap.fd is None or snap.price is None:
        snap.reasons["ev"] = snap.reasons.get("fd_shares") or "no price"
    elif snap.liquidity is None:
        snap.reasons["ev"] = "cash not tagged"
    else:
        snap.market_cap_fd = Decimal(snap.fd.total) * Decimal(repr(snap.price))
        snap.ev = snap.market_cap_fd + snap.debt + snap.convertibles - snap.liquidity
        if snap.revenue_ttm is not None and snap.revenue_ttm.value > 0:
            snap.ev_sales = float(snap.ev / snap.revenue_ttm.value)
        else:
            snap.reasons["ev_sales"] = "no positive TTM revenue"
    return snap


def first_session_of(conn: Connection, symbol: str) -> date | None:
    d = conn.execute(
        select(func.min(prices_daily.c.d)).where(prices_daily.c.symbol == symbol)
    ).scalar()
    return date.fromisoformat(d) if d else None


def last_close(conn: Connection, symbol: str, as_of: date) -> tuple[str, float] | None:
    r = conn.execute(
        select(prices_daily.c.d, prices_daily.c.c)
        .where(prices_daily.c.symbol == symbol, prices_daily.c.d <= as_of.isoformat())
        .order_by(prices_daily.c.d.desc())
        .limit(1)
    ).first()
    return (r.d, float(r.c)) if r else None


def snapshot(conn: Connection, symbol: str, as_of: date) -> Snapshot:
    return compute(
        load_facts(conn, symbol, as_of),
        symbol,
        as_of,
        first_session=first_session_of(conn, symbol),
        price=last_close(conn, symbol, as_of),
    )
