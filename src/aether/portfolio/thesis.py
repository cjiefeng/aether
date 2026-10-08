"""Thesis checks (spec §6.10, M14): the owner's Quantum Thesis applied as deterministic checks on
stored data. No LLM, no writes. Results are shown (Holdings, Strategies, ticker pages, review
pack) and fed to the monthly review as computed metrics. **They never change a weight or write a
trade**: the research overlay (§6.6.1) stays the only automatic weight adjustment.

1. Categories: every pure-play has a `modality`, every adjacent name a `sector` (watchlist.yaml).
2. Weights and concentration, by modality and by supplier sector, as % of the non-QTUM sleeve and
   of the whole sleeve; flags: one modality over `max_modality_share` of the non-QTUM sleeve, one
   company over `max_name_share` of it, fewer than `min_modalities` modalities held.
3. Red flags per pure-play / adjacent name, each with its evidence:
   - repeated issuance: >= `financings_min` dilutive financings (424B1/2/4/5 supplements and 8-K
     Item 3.02) in `financing_lookback_days`, or fully diluted shares up > `fd_yoy_max` YoY;
   - runway < `runway_min_months` (liquidity / quarterly operating burn, as §6.1);
   - TTM revenue growth <= `revenue_flat_max` while TTM operating expenses grow > `opex_growth_min`;
   - TTM cash paid for acquisitions (XBRL) > `acquisitions_liquidity_max` of liquidity.
   An input that isn't tagged makes the check `unknown` (shown with the reason), never a pass.
4. Winner signals (pure-plays): a resolved roadmap catalyst tagged `error_correction`; TTM revenue
   up `winner_revenue_quarters` consecutive quarters; no financing in the lookback and FD shares
   up < `winner_fd_yoy_max`. "Winner signals present" needs all three.
5. ETF check: QTUM's combined weight in the excluded hyperscalers (universe.yaml), flagged above
   `max_etf_hyperscaler_weight`.
6. Gaps: modalities and supplier sectors with no active name (feeds the review's `fills_gap`).
7. Stepwise building: QTUM's drawdown from its 52-week high and the red-flag count, next to the
   rebalance planner's new-cash-only mode. Information only.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Connection, Engine, func, select

from aether.config import (
    MODALITY_IDS,
    SECTOR_IDS,
    SLEEVE_TYPES,
    ThesisConfig,
    UniverseConfig,
)
from aether.db.models import catalysts, filings, prices_daily, qtum_holdings, tickers
from aether.score import fundamentals as fnd

CORE = "QTUM"
FINANCING_FORMS = ("424B1", "424B2", "424B4", "424B5")
EIGHT_K = ("8-K", "8-K/A")
FINANCING_ITEM = "3.02"
HIGH_SESSIONS = 252
# Modalities a gap can be reported for ("other" is a catch-all, never a gap).
GAP_MODALITIES = tuple(m for m in MODALITY_IDS if m != "other")
RED_FLAG_LABELS = {
    "issuance": "Repeated share issuance",
    "runway": "Cash runway under ~2 years",
    "revenue_vs_spend": "Revenue flat while spending rises",
    "acquisitions": "Large cash-draining acquisitions",
}
WINNER_LABELS = {
    "error_correction": "Error-correction milestone delivered",
    "recurring_revenue": "TTM revenue up in consecutive quarters",
    "dilution_ended": "Heavy issuance ended",
}


@dataclass(frozen=True)
class Name:
    symbol: str
    type: str  # pure_play | adjacent
    modality: str | None
    sector: str | None

    @property
    def category(self) -> str:
        """The thesis category: the modality for a pure-play, the sector for an adjacent name."""
        return (self.modality if self.type == "pure_play" else self.sector) or "untagged"


def load_names(conn: Connection) -> list[Name]:
    return [
        Name(r.symbol, r.type, r.modality, r.sector)
        for r in conn.execute(
            select(tickers.c.symbol, tickers.c.type, tickers.c.modality, tickers.c.sector)
            .where(tickers.c.type.in_(SLEEVE_TYPES), tickers.c.active == 1)
            .order_by(tickers.c.symbol)
        )
    ]


# --------------------------------------------------------------------------- 2. concentration


def breakdown(
    weights: Mapping[str, float], names: Sequence[Name], cfg: ThesisConfig
) -> dict[str, Any]:
    """Weights by modality and by supplier sector, with the concentration flags. `weights` are
    fractions of the whole sleeve (QTUM included); names outside `names` other than QTUM are
    ignored."""
    by_symbol = {n.symbol: n for n in names}
    whole = sum(max(0.0, float(w)) for s, w in weights.items() if s == CORE or s in by_symbol)
    held = {s: float(w) for s, w in weights.items() if s in by_symbol and float(w) > 0}
    ex_qtum = sum(held.values())

    def share(x: float, of: float) -> float | None:
        return x / of if of > 0 else None

    groups: dict[tuple[str, str], float] = {}
    for s, w in held.items():
        n = by_symbol[s]
        kind = "modality" if n.type == "pure_play" else "sector"
        groups[(kind, n.category)] = groups.get((kind, n.category), 0.0) + w
    rows: list[dict[str, Any]] = [
        {
            "kind": kind,
            "category": cat,
            "weight": w,
            "pct_ex_qtum": share(w, ex_qtum),
            "pct_whole": share(w, whole),
            "symbols": sorted(s for s in held if by_symbol[s].category == cat),
        }
        for (kind, cat), w in sorted(
            groups.items(), key=lambda kv: (kv[0][0] != "modality", -kv[1], kv[0][1])
        )
    ]
    flags: list[dict[str, Any]] = []
    for r in rows:
        if r["kind"] == "modality" and (r["pct_ex_qtum"] or 0) > cfg.max_modality_share:
            flags.append(
                {
                    "flag": "modality_concentration",
                    "text": f"{r['category'].replace('_', ' ')} is {r['pct_ex_qtum']:.0%} of the "
                    f"non-QTUM sleeve (limit {cfg.max_modality_share:.0%})",
                }
            )
    for s, w in sorted(held.items()):
        sh = share(w, ex_qtum)
        if sh is not None and sh > cfg.max_name_share:
            flags.append(
                {
                    "flag": "name_concentration",
                    "text": f"{s} is {sh:.0%} of the non-QTUM sleeve "
                    f"(limit {cfg.max_name_share:.0%})",
                }
            )
    modalities = sorted({m for s in held if (m := by_symbol[s].modality)})
    if held and len(modalities) < cfg.min_modalities:
        flags.append(
            {
                "flag": "few_modalities",
                "text": f"{len(modalities)} modalit{'y' if len(modalities) == 1 else 'ies'} held "
                f"({', '.join(m.replace('_', ' ') for m in modalities) or 'none'}); "
                f"the minimum is {cfg.min_modalities}",
            }
        )
    return {
        "rows": rows,
        "flags": flags,
        "ex_qtum": ex_qtum,
        "whole": whole,
        "qtum": float(weights.get(CORE, 0.0)),
        "modalities_held": modalities,
    }


# --------------------------------------------------------------------------- 3/4. per name


@dataclass
class NameInputs:
    """Everything the red flags and winner signals read for one name (pure data, for tests)."""

    symbol: str
    financings: list[dict[str, Any]] = field(default_factory=list)  # within the lookback
    fd_yoy: float | None = None
    fd_yoy_reason: str | None = None
    runway_months: float | None = None
    not_burning: bool = False
    runway_reason: str | None = None
    revenue_growth: float | None = None
    opex_growth: float | None = None
    growth_reason: str | None = None
    acquisitions_ttm: Decimal | None = None
    liquidity: Decimal | None = None
    acquisitions_reason: str | None = None
    acquisition_8ks: list[dict[str, Any]] = field(default_factory=list)
    revenue_ttms: list[tuple[str, float]] = field(default_factory=list)  # newest first
    error_correction: list[dict[str, Any]] = field(default_factory=list)
    refs: dict[str, Any] = field(default_factory=dict)


def _flag(key: str, fired: bool | None, text: str, evidence: Any = None) -> dict[str, Any]:
    return {
        "key": key,
        "label": RED_FLAG_LABELS.get(key) or WINNER_LABELS.get(key, key),
        "status": "unknown" if fired is None else ("flag" if fired else "ok"),
        "text": text,
        "evidence": evidence or [],
    }


def red_flags(x: NameInputs, cfg: ThesisConfig) -> list[dict[str, Any]]:
    out = []
    # Repeated issuance.
    n = len(x.financings)
    parts = [f"{n} dilutive financing{'s' if n != 1 else ''} in {cfg.financing_lookback_days} days"]
    fired: bool | None = n >= cfg.financings_min
    if x.fd_yoy is not None:
        parts.append(f"fully diluted shares {x.fd_yoy:+.1%} YoY")
        fired = fired or x.fd_yoy > cfg.fd_yoy_max
    elif not fired:
        parts.append(f"FD YoY unknown ({x.fd_yoy_reason or 'not computable'})")
        fired = None  # under the financing bar, but the FD half can't be checked
    out.append(
        _flag(
            "issuance",
            fired,
            "; ".join(parts) + f" (flag at >= {cfg.financings_min} or > {cfg.fd_yoy_max:+.0%})",
            x.financings,
        )
    )
    # Runway.
    if x.not_burning:
        out.append(_flag("runway", False, "not burning cash (TTM operating cash flow >= 0)"))
    elif x.runway_months is None:
        out.append(_flag("runway", None, f"runway unknown ({x.runway_reason or 'not computable'})"))
    else:
        out.append(
            _flag(
                "runway",
                x.runway_months < cfg.runway_min_months,
                f"{x.runway_months:.0f} months of runway (flag under {cfg.runway_min_months:g})",
                x.refs.get("runway"),
            )
        )
    # Revenue flat while spending rises.
    if x.revenue_growth is None or x.opex_growth is None:
        out.append(
            _flag(
                "revenue_vs_spend",
                None,
                f"growth unknown ({x.growth_reason or 'TTM revenue or opex not computable'})",
            )
        )
    else:
        out.append(
            _flag(
                "revenue_vs_spend",
                x.revenue_growth <= cfg.revenue_flat_max and x.opex_growth > cfg.opex_growth_min,
                f"TTM revenue {x.revenue_growth:+.0%}, TTM operating expenses "
                f"{x.opex_growth:+.0%} (flag at revenue <= {cfg.revenue_flat_max:+.0%} with "
                f"opex > {cfg.opex_growth_min:+.0%})",
                x.refs.get("growth"),
            )
        )
    # Acquisitions.
    if x.acquisitions_ttm is None or x.liquidity is None or x.liquidity <= 0:
        extra = f"; 8-K Item 2.01 filings: {len(x.acquisition_8ks)}" if x.acquisition_8ks else ""
        out.append(
            _flag(
                "acquisitions",
                None,
                f"unknown ({x.acquisitions_reason or 'acquisition cash or liquidity not tagged'})"
                + extra,
                x.acquisition_8ks,
            )
        )
    else:
        ratio = float(x.acquisitions_ttm / x.liquidity)
        out.append(
            _flag(
                "acquisitions",
                ratio > cfg.acquisitions_liquidity_max,
                f"TTM cash paid for acquisitions is {ratio:.0%} of liquidity "
                f"(flag above {cfg.acquisitions_liquidity_max:.0%})",
                [*(x.refs.get("acquisitions") or []), *x.acquisition_8ks],
            )
        )
    return out


def winner_signals(x: NameInputs, cfg: ThesisConfig) -> dict[str, Any]:
    signals = []
    signals.append(
        _flag(
            "error_correction",
            bool(x.error_correction),
            "; ".join(c["title"] for c in x.error_correction)
            or "no resolved roadmap catalyst tagged error_correction",
            x.error_correction,
        )
    )
    q = cfg.winner_revenue_quarters
    ttms = x.revenue_ttms[: q + 1]
    if len(ttms) < q + 1:
        signals.append(
            _flag(
                "recurring_revenue",
                False,
                f"only {len(ttms)} consecutive quarterly TTM revenue figures (needs {q + 1})",
            )
        )
    else:
        ups = all(ttms[i][1] > ttms[i + 1][1] for i in range(q))
        signals.append(
            _flag(
                "recurring_revenue",
                ups,
                f"TTM revenue {'up' if ups else 'not up'} in each of the last {q} quarters "
                f"(quarters ending {ttms[0][0]} back to {ttms[q][0]})",
            )
        )
    no_fin = not x.financings
    low_fd = x.fd_yoy is not None and x.fd_yoy < cfg.winner_fd_yoy_max
    signals.append(
        _flag(
            "dilution_ended",
            no_fin and low_fd,
            f"{len(x.financings)} financings in {cfg.financing_lookback_days} days; "
            + (
                f"FD shares {x.fd_yoy:+.1%} YoY"
                if x.fd_yoy is not None
                else f"FD YoY unknown ({x.fd_yoy_reason or 'not computable'})"
            )
            + f" (needs none and < {cfg.winner_fd_yoy_max:+.0%})",
        )
    )
    present = all(s["status"] == "flag" for s in signals)
    return {"signals": signals, "present": present}


def _growth(cur: fnd.Ttm | None, facts: Sequence[fnd.Fact]) -> tuple[float | None, str | None]:
    if cur is None:
        return None, "no TTM figure"
    prev = fnd.ttm_year_ago(facts, cur)
    if prev is None or prev.value <= 0:
        return None, "no TTM figure a year earlier"
    return float(cur.value / prev.value) - 1.0, None


def financings_for(
    conn: Connection, symbol: str, as_of: date, lookback_days: int
) -> list[dict[str, Any]]:
    since = (as_of - timedelta(days=lookback_days)).isoformat()
    out = []
    for r in conn.execute(
        select(
            filings.c.accession,
            filings.c.form,
            filings.c.filed_at,
            filings.c["items"],
            filings.c.url,
        )
        .where(
            filings.c.symbol == symbol,
            filings.c.form.in_((*FINANCING_FORMS, *EIGHT_K)),
            filings.c.filed_at >= since,
            filings.c.filed_at <= as_of.isoformat(),
        )
        .order_by(filings.c.filed_at, filings.c.accession)
    ):
        if r.form in EIGHT_K and FINANCING_ITEM not in (json.loads(r._mapping["items"]) or []):
            continue
        out.append(
            {
                "accession": r.accession,
                "form": r.form if r.form not in EIGHT_K else f"{r.form} Item {FINANCING_ITEM}",
                "filed_at": r.filed_at,
                "url": r.url,
            }
        )
    return out


def _acquisition_8ks(conn: Connection, symbol: str, as_of: date) -> list[dict[str, Any]]:
    since = (as_of - timedelta(days=365)).isoformat()
    out = []
    for r in conn.execute(
        select(
            filings.c.accession,
            filings.c.form,
            filings.c.filed_at,
            filings.c["items"],
            filings.c.url,
        )
        .where(
            filings.c.symbol == symbol,
            filings.c.form.in_(EIGHT_K),
            filings.c.filed_at >= since,
            filings.c.filed_at <= as_of.isoformat(),
        )
        .order_by(filings.c.filed_at)
    ):
        if "2.01" in (json.loads(r._mapping["items"]) or []):
            out.append(
                {
                    "accession": r.accession,
                    "form": f"{r.form} Item 2.01",
                    "filed_at": r.filed_at,
                    "url": r.url,
                }
            )
    return out


def name_inputs(conn: Connection, symbol: str, as_of: date, cfg: ThesisConfig) -> NameInputs:
    facts = fnd.load_facts(conn, symbol, as_of)
    snap = fnd.compute(
        facts,
        symbol,
        as_of,
        first_session=fnd.first_session_of(conn, symbol),
        price=None,
    )
    x = NameInputs(symbol)
    x.financings = financings_for(conn, symbol, as_of, cfg.financing_lookback_days)
    x.fd_yoy = snap.fd_yoy.get("value")
    x.fd_yoy_reason = snap.fd_yoy.get("reason")
    x.runway_months = snap.runway_months
    x.not_burning = snap.not_burning
    x.runway_reason = snap.reasons.get("runway")
    if snap.liquidity is not None:
        x.refs["runway"] = snap.liquidity_refs
    x.revenue_growth = snap.revenue_growth
    opex = fnd.fresh(fnd.latest_ttm(facts, fnd.OPEX), as_of, snap, "opex")
    x.opex_growth, opex_reason = _growth(opex, facts)
    reasons = []
    if snap.revenue_growth is None:
        reasons.append(
            "revenue: "
            + (
                snap.reasons.get("revenue_growth")
                or snap.reasons.get("revenue")
                or "not computable"
            )
        )
    if x.opex_growth is None:
        reasons.append("operating expenses: " + (opex_reason or "not tagged"))
    x.growth_reason = "; ".join(reasons) or None
    if snap.revenue_ttm is not None and opex is not None:
        x.refs["growth"] = [*snap.revenue_ttm.refs, *opex.refs]
    acq = fnd.fresh(fnd.latest_ttm(facts, fnd.ACQUISITIONS), as_of, snap, "acquisitions")
    x.liquidity = snap.liquidity
    if acq is None:
        x.acquisitions_reason = "no TTM cash paid for acquisitions tagged"
    else:
        x.acquisitions_ttm = acq.value
        x.refs["acquisitions"] = list(acq.refs)
        if snap.liquidity is None:
            x.acquisitions_reason = snap.reasons.get("liquidity") or "liquidity not tagged"
    x.acquisition_8ks = _acquisition_8ks(conn, symbol, as_of)
    x.revenue_ttms = [
        (t.end.isoformat(), float(t.value))
        for t in fnd.ttm_history(facts, fnd.REVENUE, cfg.winner_revenue_quarters + 1)
    ]
    for r in conn.execute(
        select(catalysts.c.id, catalysts.c.title, catalysts.c.tags, catalysts.c.resolved_at)
        .where(
            catalysts.c.symbol == symbol,
            catalysts.c.kind == "roadmap",
            catalysts.c.status == "hit",
        )
        .order_by(catalysts.c.id)
    ):
        if "error_correction" in json.loads(r.tags or "[]"):
            x.error_correction.append(
                {"catalyst_id": r.id, "title": r.title, "resolved_at": r.resolved_at}
            )
    return x


# --------------------------------------------------------------------------- 5/6/7


def hyperscaler_check(
    qtum: Mapping[str, float], snapshot: str | None, excluded: Iterable[str], cfg: ThesisConfig
) -> dict[str, Any]:
    """`qtum`: holding symbol -> weight in percent of the fund (as `qtum_holdings` stores it)."""
    parts = {s: float(qtum[s]) for s in sorted(set(excluded)) if s in qtum}
    total = sum(parts.values()) / 100.0
    return {
        "snapshot": snapshot,
        "parts": parts,
        "weight": total if snapshot else None,
        "limit": cfg.max_etf_hyperscaler_weight,
        "flag": bool(snapshot) and total > cfg.max_etf_hyperscaler_weight,
    }


def gaps(names: Sequence[Name]) -> dict[str, list[str]]:
    mods = {n.modality for n in names if n.type == "pure_play" and n.modality}
    secs = {n.sector for n in names if n.type == "adjacent" and n.sector}
    return {
        "modalities": [m for m in GAP_MODALITIES if m not in mods],
        "sectors": [s for s in SECTOR_IDS if s not in secs],
    }


def fills_gap(category: str | None, names: Sequence[Name]) -> bool:
    """Whether a candidate's modality (pure-play track) or sector (adjacent track) is uncovered."""
    if not category:
        return False
    g = gaps(names)
    return category in g["modalities"] or category in g["sectors"]


def qtum_drawdown(conn: Connection, as_of: date) -> dict[str, Any] | None:
    rows = conn.execute(
        select(prices_daily.c.d, prices_daily.c.c)
        .where(prices_daily.c.symbol == CORE, prices_daily.c.d <= as_of.isoformat())
        .order_by(prices_daily.c.d.desc())
        .limit(HIGH_SESSIONS)
    ).all()
    if not rows:
        return None
    high = max(rows, key=lambda r: r.c)
    last = rows[0]
    return {
        "as_of": last.d,
        "close": float(last.c),
        "high": float(high.c),
        "high_d": high.d,
        "drawdown": float(last.c) / float(high.c) - 1.0 if high.c > 0 else None,
    }


def latest_qtum(conn: Connection) -> tuple[str | None, dict[str, float]]:
    d = conn.execute(select(func.max(qtum_holdings.c.snapshot_date))).scalar()
    if d is None:
        return None, {}
    rows = conn.execute(
        select(qtum_holdings.c.holding_symbol, qtum_holdings.c.weight).where(
            qtum_holdings.c.snapshot_date == d
        )
    ).all()
    return d, {str(s).upper(): float(w) for s, w in rows}


# --------------------------------------------------------------------------- report


def name_checks(
    conn: Connection, names: Sequence[Name], as_of: date, cfg: ThesisConfig
) -> dict[str, Any]:
    out = {}
    for n in names:
        x = name_inputs(conn, n.symbol, as_of, cfg)
        flags = red_flags(x, cfg)
        out[n.symbol] = {
            "symbol": n.symbol,
            "type": n.type,
            "category": n.category,
            "red_flags": flags,
            "n_flags": sum(f["status"] == "flag" for f in flags),
            "winner": winner_signals(x, cfg) if n.type == "pure_play" else None,
        }
    return out


def holdings_weights(conn: Connection, positions: Mapping[str, Decimal]) -> dict[str, float]:
    """Value weights (fractions of the invested sleeve, cash excluded) at the last close."""
    from aether.portfolio.publish import last_prices

    px = last_prices(conn, sorted(positions))
    vals = {s: positions[s] * px[s][1] for s in positions if s in px}
    total = sum(vals.values(), Decimal(0))
    return {s: float(v / total) for s, v in sorted(vals.items())} if total > 0 else {}


def thesis_report(
    engine: Engine,
    as_of: date,
    cfg: ThesisConfig,
    uni: UniverseConfig,
    *,
    holdings: Mapping[str, Decimal] | None = None,
    targets: Mapping[str, Mapping[str, float]] | None = None,
    with_names: bool = True,
) -> dict[str, Any]:
    """Everything the pages and the review pack show. Read-only; never writes."""
    with engine.connect() as conn:
        names = load_names(conn)
        snap, qtum = latest_qtum(conn)
        excluded = uni.adjacent.excluded_symbols if uni.adjacent else ()
        per_name = name_checks(conn, names, as_of, cfg) if with_names else {}
        held = holdings_weights(conn, holdings) if holdings else {}
        dd = qtum_drawdown(conn, as_of)
    report: dict[str, Any] = {
        "as_of": as_of.isoformat(),
        "names": [
            {"symbol": n.symbol, "type": n.type, "modality": n.modality, "sector": n.sector}
            for n in names
        ],
        "cap": {"used": len(names), "max": uni.max_names_ex_qtum},
        "per_name": per_name,
        "red_flag_total": sum(v["n_flags"] for v in per_name.values()),
        "winners": sorted(s for s, v in per_name.items() if v["winner"] and v["winner"]["present"]),
        "holdings": breakdown(held, names, cfg) if held else None,
        "targets": {p: breakdown(w, names, cfg) for p, w in (targets or {}).items()},
        "hyperscaler": hyperscaler_check(qtum, snap, excluded, cfg),
        "gaps": gaps(names),
        "qtum_drawdown": dd,
    }
    return report


def summary_lines(report: Mapping[str, Any], profile: str | None = None) -> list[str]:
    """Plain-text lines for Telegram and the review pack (no holdings values)."""
    cap = report["cap"]
    lines = [f"Names: {cap['used']} of {cap['max']} used (QTUM excluded)."]
    t = (report.get("targets") or {}).get(profile) if profile else None
    for label, b in (
        ("Holdings", report.get("holdings")),
        (f"{profile} targets" if profile else "", t),
    ):
        if not b:
            continue
        mods = ", ".join(
            f"{r['category'].replace('_', ' ')} {r['pct_ex_qtum']:.0%}"
            for r in b["rows"]
            if r["pct_ex_qtum"] is not None
        )
        lines.append(f"{label} by modality/sector (ex-QTUM): {mods or 'none'}.")
        for f in b["flags"]:
            lines.append(f"Concentration: {f['text']}.")
    flagged = [
        f"{s} ({', '.join(f['label'].lower() for f in v['red_flags'] if f['status'] == 'flag')})"
        for s, v in sorted(report["per_name"].items())
        if v["n_flags"]
    ]
    lines.append("Red flags: " + ("; ".join(flagged) if flagged else "none") + ".")
    if report["winners"]:
        lines.append("Winner signals present: " + ", ".join(report["winners"]) + ".")
    g = report["gaps"]
    if g["modalities"] or g["sectors"]:
        lines.append(
            "Gaps: "
            + ", ".join(m.replace("_", " ") for m in [*g["modalities"], *g["sectors"]])
            + "."
        )
    h = report["hyperscaler"]
    if h["weight"] is not None:
        lines.append(
            f"QTUM hyperscaler weight {h['weight']:.1%} (limit {h['limit']:.0%})"
            + (" FLAGGED." if h["flag"] else ".")
        )
    return lines


# --------------------------------------------------------------------------- pages


def page_report(
    engine: Engine, as_of: date, config_dir: Any, *, with_holdings: bool = True
) -> dict[str, Any]:
    """The thesis panel for Holdings / Strategies / the review pack: actual holdings (if any) and
    every profile's published targets. Read-only."""
    from aether.config import PROFILES, load_thesis_config, load_universe_config
    from aether.portfolio.holdings import load_holdings
    from aether.portfolio.publish import latest_published

    targets: dict[str, dict[str, float]] = {}
    with engine.connect() as conn:
        for p in PROFILES:
            pub = latest_published(conn, p)
            if pub is not None:
                targets[p] = {s: float(w) for s, w in pub.weights.items()}
    positions = None
    if with_holdings:
        h = load_holdings(engine)
        positions = {s: p.shares for s, p in h.positions.items()} or None
    return thesis_report(
        engine,
        as_of,
        load_thesis_config(config_dir),
        load_universe_config(config_dir),
        holdings=positions,
        targets=targets,
    )


def ticker_checks(
    engine: Engine, symbol: str, as_of: date, config_dir: Any
) -> dict[str, Any] | None:
    """Red flags and winner signals for one pure-play / adjacent name (ticker page)."""
    from aether.config import load_thesis_config

    with engine.connect() as conn:
        names = [n for n in load_names(conn) if n.symbol == symbol]
        if not names:
            return None
        out: dict[str, Any] = name_checks(conn, names, as_of, load_thesis_config(config_dir))[
            symbol
        ]
        return out
