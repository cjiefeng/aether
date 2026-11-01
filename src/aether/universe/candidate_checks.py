"""Candidate red-flag checks (spec §6.7.4, M18; no LLM).

The four §6.10 red flags, with the same `config/thesis.yaml` thresholds, for every candidate that
passes either track's deterministic add criteria, in the monthly review and the full
re-evaluation. Candidates aren't on the watchlist, so nothing is in `filings` / `fundamentals_q`:
the data comes straight from SEC and nothing is written for them except the result in
`universe_candidates.criteria`.

    fetch (network, no transaction open)                                 2-12 SEC requests
      submissions (+ older pages in the window), companyfacts,
      up to `candidate_max_documents` primary 424B covers (newest first; FPIs: then F-1/F-3),
      FPIs: the XBRL instance of the latest 6-K / 20-F / F-1 not in companyfacts (`edgar/fpi.py`)
    checks (pure)
      the facts become `score/fundamentals.Fact`s and go through the same `fnd.compute`,
      `thesis.count_financings` and `thesis.red_flags` as watchlist names.

Rules beyond §6.10 (each labelled in the check's text):
- **Issuance:** 424B3 resale supplements aren't issuance ("resale overhang"); a 424B beyond the
  document cap is counted without a size ("size not parsed"), so the #51 merge never guesses.
  FPIs: an F-1 / F-3 whose cover reads "we are offering N shares" counts, unless a primary 424B
  follows it in the window (that 424B records the sale); 6-K private placements can't be read
  deterministically, so the count is "partial". Shares outstanding is the YoY proxy when warrants
  and options aren't tagged; the year-ago base must be on or after the first trading session.
- **Runway / revenue vs spend:** TTM as §6.10; when TTM isn't computable (a fresh FPI), the latest
  period (operating cash flow scaled to a quarter; revenue and opex vs the same period a year
  earlier), labelled "period, not TTM".
- **Acquisitions:** TTM cash paid vs liquidity, plus the 8-K Item 2.01 count in 12 months without
  the company's own business-combination or separation closing (#49: Item 5.06, or filed on or
  before the first trading session).
- **Freshness:** data whose period ended more than `candidate_stale_days` before the review is
  `unknown` ("stale"), never `ok`.
- **Dates (#50):** only EDGAR dates (submissions filing dates, XBRL period ends and filing dates).
  Research events are never read here.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from aether.config import ThesisConfig, UniverseConfig
from aether.edgar import fpi, xbrl
from aether.edgar.submissions import FilingMeta, iter_filings, older_pages
from aether.edgar.text import PRIMARY_PROSPECTUS_FORMS, extract_offering, html_to_text
from aether.portfolio import thesis
from aether.portfolio.thesis import FilingRow, NameInputs
from aether.providers.edgar import EdgarClient
from aether.providers.prices import ProviderError
from aether.score import fundamentals as fnd
from aether.universe.triggers import own_combination_closing

log = logging.getLogger(__name__)

RESALE_FORMS = frozenset({"424B3"})
FPI_REGISTRATIONS = frozenset({"F-1", "F-3"})
ACQUISITION_ITEM = "2.01"
ACQUISITION_DAYS = 365
QUARTER_DAYS = Decimal("91.3125")
PERIOD_NOT_TTM = "period, not TTM"
REF_KEYS = {"revenue_vs_spend": "growth"}  # check key -> NameInputs.refs key


# --------------------------------------------------------------------------- data


@dataclass
class CandidateData:
    """Everything the checks read for one candidate (fetched once; pure data for tests)."""

    symbol: str
    cik: str
    metas: list[FilingMeta] = field(default_factory=list)
    companyfacts: dict[str, Any] = field(default_factory=dict)
    covers: dict[str, dict[str, Any]] = field(default_factory=dict)  # accession -> parsed
    fpi: bool = False
    instances: list[dict[str, str]] = field(default_factory=list)
    first_session: date | None = None
    requests: int = 0
    errors: list[str] = field(default_factory=list)


def _cover(client: EdgarClient, m: FilingMeta) -> dict[str, Any]:
    """`filings.parsed`-shaped result for one prospectus / registration cover."""
    assert m.primary_doc is not None
    off = extract_offering(html_to_text(client.document(m.cik, m.accession, m.primary_doc)))
    return {"offering": None if off is None else {"shares": off.shares, "prefunded": off.prefunded}}


def fetch(
    client: EdgarClient,
    symbol: str,
    cik: str,
    *,
    as_of: date,
    first_session: date | None,
    cfg: UniverseConfig,
    lookback_days: int,
) -> CandidateData:
    """Network only; never raises for SEC failures (they become `errors`, and the affected
    checks are `unknown`)."""
    before = client.requests_made
    data = CandidateData(symbol, cik, first_session=first_session)
    since = as_of - timedelta(days=max(lookback_days, ACQUISITION_DAYS))
    try:
        subs = client.submissions(cik)
        metas = list(iter_filings(subs.get("filings", {}).get("recent", {}), cik))
        for page in older_pages(subs, since):
            metas += list(iter_filings(client.submissions_page(page), cik))
        data.metas = [m for m in metas if m.filed_at <= as_of.isoformat()]
    except (ProviderError, ValueError) as exc:
        data.errors.append(f"submissions: {exc}"[:200])
        data.requests = client.requests_made - before
        return data
    data.fpi = fpi.is_fpi(data.metas)
    try:
        data.companyfacts = client.companyfacts(cik)
    except (ProviderError, ValueError) as exc:
        data.errors.append(f"companyfacts: {exc}"[:200])

    window = [m for m in data.metas if m.filed_at >= since.isoformat() and m.primary_doc]
    newest = sorted(window, key=lambda m: (m.filed_at, m.accession), reverse=True)
    docs = [m for m in newest if m.form in PRIMARY_PROSPECTUS_FORMS]
    if data.fpi:
        docs += [m for m in newest if m.form in FPI_REGISTRATIONS]
    for m in docs[: cfg.candidate_max_documents]:
        try:
            data.covers[m.accession] = _cover(client, m)
        except (ProviderError, ValueError) as exc:
            data.errors.append(f"{m.form} {m.accession}: {exc}"[:200])

    if data.fpi:
        inst = fpi.fetch_instances(client, cik, data.metas, data.companyfacts)
        data.errors += inst.errors
        data.instances = inst.used
        data.companyfacts = xbrl.merge_companyfacts(data.companyfacts, *inst.facts)
    data.requests = client.requests_made - before
    return data


# --------------------------------------------------------------------------- pure helpers


def to_facts(companyfacts: Mapping[str, Any], as_of: date) -> list[fnd.Fact]:
    """companyfacts (plus merged instances) -> the facts `fnd` reads, filed on or before as_of."""
    out = [
        fnd.Fact(
            f.concept,
            date.fromisoformat(f.period_end),
            f.period_days,
            f.value,
            f.accession,
            f.form,
            f.filed,
        )
        for f in xbrl.fundamentals(companyfacts)
        if f.filed and f.filed <= as_of.isoformat()
    ]
    return sorted(out, key=lambda f: (f.concept, f.period_end, f.days))


def latest_period(facts: Sequence[fnd.Fact], concepts: Sequence[str]) -> fnd.Fact | None:
    """The latest-ending duration of the first concept that has one (longest at that end)."""
    for c in concepts:
        xs = [f for f in facts if f.concept == c and f.days > 0]
        if xs:
            end = max(f.period_end for f in xs)
            return max((f for f in xs if f.period_end == end), key=lambda f: f.days)
    return None


def period_pair(
    facts: Sequence[fnd.Fact], concepts: Sequence[str]
) -> tuple[fnd.Fact, fnd.Fact] | None:
    """(latest period, the same period a year earlier) for the first concept that has a pair:
    latest end first, then the longest duration."""
    for c in concepts:
        xs = [f for f in facts if f.concept == c and f.days > 0]
        for f in sorted(xs, key=lambda f: (f.period_end, f.days), reverse=True):
            prev = fnd._duration(xs, f.period_end - timedelta(days=365), f.days)
            if prev is not None and prev is not f:
                return f, prev
    return None


def _same_period(
    facts: Sequence[fnd.Fact], concepts: Sequence[str], end: date, days: int
) -> tuple[fnd.Fact, fnd.Fact] | None:
    for c in concepts:
        xs = [f for f in facts if f.concept == c]
        cur = fnd._duration(xs, end, days)
        prev = fnd._duration(xs, end - timedelta(days=365), days)
        if cur is not None and prev is not None:
            return cur, prev
    return None


def _ref(f: fnd.Fact) -> dict[str, Any]:
    return {**f.ref(), "value": float(f.value), "filed": f.filed}


def _row(m: FilingMeta, covers: Mapping[str, Any]) -> FilingRow:
    return FilingRow(m.accession, m.form, m.filed_at, m.items, m.url, covers.get(m.accession))


# --------------------------------------------------------------------------- checks


@dataclass
class _Ctx:
    as_of: date
    stale_days: int
    notes: dict[str, list[str]] = field(default_factory=dict)
    period: dict[str, str | None] = field(default_factory=dict)
    basis: dict[str, str] = field(default_factory=dict)

    def stale(self, end: date | None) -> bool:
        return end is not None and (self.as_of - end).days > self.stale_days


def _issuance(
    data: CandidateData, x: NameInputs, snap: fnd.Snapshot, cfg: ThesisConfig, c: _Ctx
) -> dict[str, Any]:
    since = (c.as_of - timedelta(days=cfg.financing_lookback_days)).isoformat()
    window = [m for m in data.metas if since <= m.filed_at <= c.as_of.isoformat()]
    x.financings = thesis.count_financings(
        data.symbol, [_row(m, data.covers) for m in window], c.as_of, cfg.financing_lookback_days
    )
    for item in x.financings:
        if item["form"] in PRIMARY_PROSPECTUS_FORMS:
            off = (data.covers.get(item["accession"]) or {}).get("offering")
            item["size"] = off["shares"] if off else "not parsed"
    if data.fpi:
        primaries = [m for m in window if m.form in PRIMARY_PROSPECTUS_FORMS]
        for m in sorted(window, key=lambda m: (m.filed_at, m.accession)):
            off = (data.covers.get(m.accession) or {}).get("offering")
            if m.form not in FPI_REGISTRATIONS or not off:
                continue
            if any(p.filed_at >= m.filed_at for p in primaries):
                continue  # the 424B that followed records the sale; counted above
            x.financings.append(
                {
                    "accession": m.accession,
                    "form": f"{m.form} (primary registration)",
                    "filed_at": m.filed_at,
                    "url": m.url,
                    "size": off["shares"],
                }
            )
        x.issuance_notes.append(
            "financing count partial (foreign private issuer: 6-K private placements aren't "
            "machine-readable)"
        )
    resale = [
        {"accession": m.accession, "form": m.form, "filed_at": m.filed_at, "url": m.url}
        for m in window
        if m.form in RESALE_FORMS
    ]
    if resale:
        x.issuance_notes.append(
            f"resale overhang: {len(resale)} 424B3 resale supplement(s), not issuance"
        )
    fd = dict(snap.fd_yoy)
    to = (fd.get("to") or {}).get("common") or {}
    end = date.fromisoformat(to["period_end"]) if to.get("period_end") else None
    if fd.get("value") is not None and c.stale(end):
        fd = {"value": None, "reason": f"stale (latest share count {to['period_end']})"}
    x.fd_yoy = fd.get("value")
    x.fd_yoy_reason = fd.get("reason")
    x.fd_refs = thesis.fd_refs(fd)
    if x.fd_yoy is not None and fd.get("components_compared") == ["common"]:
        x.issuance_notes.append(
            "share YoY uses shares outstanding (proxy: warrants/options untagged)"
        )
    c.period["issuance"] = to.get("period_end")
    return {"resale_overhang": resale}


def _runway(facts: Sequence[fnd.Fact], x: NameInputs, snap: fnd.Snapshot, c: _Ctx) -> None:
    liq_end = (
        date.fromisoformat(snap.liquidity_refs[0]["period_end"]) if snap.liquidity_refs else None
    )
    x.liquidity = snap.liquidity
    if snap.liquidity is not None and c.stale(liq_end):
        x.runway_reason = f"stale (cash at {liq_end})"
        x.liquidity = None
        return
    refs = list(snap.liquidity_refs)
    if snap.ocf_ttm is not None:
        if c.stale(snap.ocf_ttm.end):
            x.runway_reason = f"stale (operating cash flow to {snap.ocf_ttm.end})"
            return
        x.not_burning, x.runway_months = snap.not_burning, snap.runway_months
        x.runway_reason = snap.reasons.get("runway")
        c.period["runway"] = snap.ocf_ttm.end.isoformat()
        c.basis["runway"] = "TTM"
        x.refs["runway"] = [*refs, *snap.ocf_ttm.refs]
        return
    p = latest_period(facts, fnd.OCF)
    if p is None:
        x.runway_reason = snap.reasons.get("runway") or "no operating cash flow tagged"
        return
    if c.stale(p.period_end):
        x.runway_reason = f"stale (operating cash flow to {p.period_end})"
        return
    c.period["runway"] = p.period_end.isoformat()
    c.basis["runway"] = PERIOD_NOT_TTM
    c.notes.setdefault("runway", []).append(
        f"{PERIOD_NOT_TTM}: burn from the {p.days}-day period to {p.period_end}, "
        "scaled to a quarter"
    )
    x.refs["runway"] = [*refs, _ref(p)]
    if p.value >= 0:
        x.not_burning = True
        return
    if snap.liquidity is None:
        x.runway_reason = snap.reasons.get("liquidity") or "cash not tagged"
        return
    burn = -p.value / (Decimal(p.days) / QUARTER_DAYS)
    x.runway_months = float(snap.liquidity / burn * 3)


def _growth(facts: Sequence[fnd.Fact], x: NameInputs, snap: fnd.Snapshot, c: _Ctx) -> None:
    rev = snap.revenue_ttm
    opex = fnd.latest_ttm(facts, fnd.OPEX)
    if rev is not None and opex is not None and snap.revenue_growth is not None:
        og, _why = thesis.ttm_growth(opex, facts)
        if og is not None:
            end = min(rev.end, opex.end)
            if c.stale(end):
                x.growth_reason = f"stale (TTM to {end})"
                return
            x.revenue_growth, x.opex_growth = snap.revenue_growth, og
            x.refs["growth"] = [*rev.refs, *opex.refs]
            c.period["revenue_vs_spend"] = end.isoformat()
            c.basis["revenue_vs_spend"] = "TTM"
            return
    pair = period_pair(facts, fnd.REVENUE)
    if pair is None:
        x.growth_reason = "revenue: no TTM and no same period a year earlier"
        return
    cur, prev = pair
    o = _same_period(facts, fnd.OPEX, cur.period_end, cur.days)
    if o is None:
        x.growth_reason = (
            f"operating expenses: not tagged for the {cur.days}-day period to {cur.period_end} "
            "and a year earlier"
        )
        return
    if c.stale(cur.period_end):
        x.growth_reason = f"stale (latest period to {cur.period_end})"
        return
    if prev.value <= 0 or o[1].value <= 0:
        x.growth_reason = "no positive figure a year earlier"
        return
    x.revenue_growth = float(cur.value / prev.value) - 1.0
    x.opex_growth = float(o[0].value / o[1].value) - 1.0
    x.refs["growth"] = [_ref(cur), _ref(prev), _ref(o[0]), _ref(o[1])]
    c.period["revenue_vs_spend"] = cur.period_end.isoformat()
    c.basis["revenue_vs_spend"] = PERIOD_NOT_TTM
    c.notes.setdefault("revenue_vs_spend", []).append(
        f"{PERIOD_NOT_TTM}: the {cur.days}-day period to {cur.period_end} vs a year earlier"
    )


def _acquisitions(data: CandidateData, facts: Sequence[fnd.Fact], x: NameInputs, c: _Ctx) -> None:
    since = (c.as_of - timedelta(days=ACQUISITION_DAYS)).isoformat()
    first = data.first_session.isoformat() if data.first_session else None
    for m in sorted(data.metas, key=lambda m: (m.filed_at, m.accession)):
        if m.form not in ("8-K", "8-K/A") or ACQUISITION_ITEM not in m.items:
            continue
        if not since <= m.filed_at <= c.as_of.isoformat():
            continue
        if own_combination_closing(set(m.items), m.filed_at, first):  # #49
            continue
        x.acquisition_8ks.append(
            {
                "accession": m.accession,
                "form": f"{m.form} Item {ACQUISITION_ITEM}",
                "filed_at": m.filed_at,
                "url": m.url,
            }
        )
    acq = fnd.latest_ttm(facts, fnd.ACQUISITIONS)
    if acq is None:
        x.acquisitions_reason = "no TTM cash paid for acquisitions tagged"
        return
    if c.stale(acq.end):
        x.acquisitions_reason = f"stale (TTM to {acq.end})"
        return
    x.acquisitions_ttm = acq.value
    x.refs["acquisitions"] = list(acq.refs)
    c.period["acquisitions"] = acq.end.isoformat()
    if x.liquidity is None:
        x.acquisitions_reason = x.runway_reason or "liquidity not tagged"


def _accessions(evidence: Any) -> list[str]:
    out: list[str] = []
    for e in evidence or []:
        for a in (e.get("accession"), *(e.get("also") or [])):
            if a and a not in out:
                out.append(a)
    return out


def checks(data: CandidateData, as_of: date, cfg: ThesisConfig, stale_days: int) -> dict[str, Any]:
    """The four red flags for one candidate (pure). `flag` / `ok` / `unknown`, each with its text,
    evidence, period end, basis, data-as-of date and accessions."""
    c = _Ctx(as_of, stale_days)
    facts = to_facts(data.companyfacts, as_of)
    snap = fnd.compute(facts, data.symbol, as_of, first_session=data.first_session, price=None)
    x = NameInputs(data.symbol)
    extra: dict[str, Any] = {"resale_overhang": []}
    if data.metas or not data.errors:
        extra = _issuance(data, x, snap, cfg, c)
    else:
        x.fd_yoy_reason = "SEC submissions unavailable"
    _runway(facts, x, snap, c)
    _growth(facts, x, snap, c)
    _acquisitions(data, facts, x, c)
    flags = thesis.red_flags(x, cfg)
    if not data.metas and data.errors:
        # Without submissions the financing count is unknown, not zero.
        flags[0].update(status="unknown", text="unknown (SEC submissions unavailable)")
    filed = {f.accession: f.filed for f in facts if f.accession}
    for f in flags:
        k = f["key"]
        if not f["evidence"] and x.refs.get(REF_KEYS.get(k, k)):
            f["evidence"] = list(x.refs[REF_KEYS.get(k, k)])  # e.g. "not burning" cites its OCF
        if c.basis.get(k) == PERIOD_NOT_TTM:
            f["text"] = (
                f["text"]
                .replace("TTM revenue", "revenue")
                .replace("TTM operating expenses", "operating expenses")
            )
        if c.notes.get(k):
            f["text"] += "; " + "; ".join(c.notes[k])
        accs = _accessions(f["evidence"])
        dates = [d for a in accs if (d := filed.get(a) or _filed_of(data, a))]
        f.update(
            period_end=c.period.get(k),
            basis=c.basis.get(k),
            data_as_of=max(dates) if dates else None,
            accessions=accs,
        )
    return {
        "symbol": data.symbol,
        "as_of": as_of.isoformat(),
        "fpi": data.fpi,
        "checks": flags,
        "n_flags": sum(f["status"] == "flag" for f in flags),
        "n_unknown": sum(f["status"] == "unknown" for f in flags),
        "resale_overhang": extra["resale_overhang"],
        "instances": data.instances,
        "requests": data.requests,
        "errors": data.errors,
    }


def _filed_of(data: CandidateData, accession: str) -> str | None:
    return next((m.filed_at for m in data.metas if m.accession == accession), None)


def run(
    client: EdgarClient,
    symbol: str,
    cik: str,
    *,
    as_of: date,
    first_session: date | None,
    cfg: UniverseConfig,
    thesis_cfg: ThesisConfig,
) -> dict[str, Any]:
    data = fetch(
        client,
        symbol,
        cik,
        as_of=as_of,
        first_session=first_session,
        cfg=cfg,
        lookback_days=thesis_cfg.financing_lookback_days,
    )
    return checks(data, as_of, thesis_cfg, cfg.candidate_stale_days)


def line(result: Mapping[str, Any] | None) -> str | None:
    """Plain text for the proposal and Telegram: counts, with the flagged and unknown labels."""
    if not result:
        return None
    fl = [f["label"].lower() for f in result["checks"] if f["status"] == "flag"]
    un = [f["label"].lower() for f in result["checks"] if f["status"] == "unknown"]
    out = f"red flags: {len(fl)}" + (f" ({', '.join(fl)})" if fl else "")
    out += f", unknown: {len(un)}" + (f" ({', '.join(un)})" if un else "")
    return out + (" (foreign private issuer)" if result.get("fpi") else "")


def short(result: Mapping[str, Any] | None) -> str:
    if not result:
        return ""
    return f"red flags: {result['n_flags']}, unknown: {result['n_unknown']}"
