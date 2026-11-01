"""SEC XBRL `companyfacts` -> quarterly fundamentals and XBRL-tagged capital structure.

Only non-dimensional facts appear in companyfacts. New filers (QNT, INFQ) lack many concepts;
anything missing is simply absent, never filled in.

M18 (spec §6.7.4): foreign private issuers. companyfacts can miss an FPI's interim figures (XNDU's
Q2 2026 statements were only in a 6-K's inline XBRL, checked 2026-11-01), so `instance_facts`
reads one filing's XBRL instance (the `*_htm.xml` SEC extracts from inline XBRL) into the same
shape, and `merge_companyfacts` adds it. `ifrs-full` concepts (names checked against Arqit's and
Spotify's companyfacts on 2026-11-01) sit next to their us-gaap equivalents.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

# M18: ifrs-full equivalents of the concepts below (flows keep their YTD durations too).
IFRS_FLOWS = (
    "ifrs-full:RevenueFromContractsWithCustomers",
    "ifrs-full:Revenue",
    "ifrs-full:CashFlowsFromUsedInOperatingActivities",
    "ifrs-full:OperatingExpense",
    "ifrs-full:CashFlowsUsedInObtainingControlOfSubsidiariesOrOtherBusinesses"
    "ClassifiedAsInvestingActivities",
)
IFRS_BALANCES = ("ifrs-full:CashAndCashEquivalents", "ifrs-full:CurrentInvestments")

# concept -> unit. USD amounts become Micros; share counts stay integers.
MONEY_CONCEPTS = (
    "us-gaap:Revenues",
    "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
    "us-gaap:CashAndCashEquivalentsAtCarryingValue",
    "us-gaap:NetCashProvidedByUsedInOperatingActivities",
    "us-gaap:LongTermDebt",
    "us-gaap:LongTermDebtNoncurrent",
    "us-gaap:ConvertibleNotesPayable",
    "us-gaap:ConvertibleNotesPayableCurrent",
    "us-gaap:ConvertibleDebtNoncurrent",
    "us-gaap:ConvertibleSeniorNotesNoncurrent",
    "us-gaap:WarrantsAndRightsOutstanding",
    # Liquidity beyond cash (M9: EV and cash runway): current and non-current marketable debt
    # securities, under the concepts the watchlist's filers use.
    "us-gaap:ShortTermInvestments",
    "us-gaap:MarketableSecuritiesCurrent",
    "us-gaap:AvailableForSaleSecuritiesDebtSecuritiesCurrent",
    "us-gaap:DebtSecuritiesAvailableForSaleExcludingAccruedInterestCurrent",
    "us-gaap:AvailableForSaleSecuritiesDebtSecuritiesNoncurrent",
    "us-gaap:DebtSecuritiesAvailableForSaleExcludingAccruedInterestNoncurrent",
    "us-gaap:MarketableSecuritiesNoncurrent",
    "us-gaap:RevenueFromContractWithCustomerIncludingAssessedTax",
    # M14 thesis red flags (spec §6.10): spending growth and cash paid for acquisitions.
    "us-gaap:OperatingExpenses",
    "us-gaap:CostsAndExpenses",
    "us-gaap:PaymentsToAcquireBusinessesNetOfCashAcquired",
    # M18: IFRS filers (foreign private issuers).
    *IFRS_FLOWS,
    *IFRS_BALANCES,
)
# Flow concepts whose 6- and 9-month year-to-date values are kept too: 10-Qs report Q2/Q3 cash
# flows only as YTD, so trailing-twelve-month figures need them (M9).
YTD_CONCEPTS = (
    "us-gaap:Revenues",
    "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
    "us-gaap:RevenueFromContractWithCustomerIncludingAssessedTax",
    "us-gaap:NetCashProvidedByUsedInOperatingActivities",
    "us-gaap:OperatingExpenses",
    "us-gaap:CostsAndExpenses",
    "us-gaap:PaymentsToAcquireBusinessesNetOfCashAcquired",
    *IFRS_FLOWS,
)
SHARE_CONCEPTS = (
    "dei:EntityCommonStockSharesOutstanding",
    "us-gaap:CommonStockSharesOutstanding",
    "us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding",
    "us-gaap:ClassOfWarrantOrRightOutstanding",
    # Dilutive instruments for the fully diluted share count (M9).
    "us-gaap:ShareBasedCompensationArrangementByShareBasedPaymentAwardOptionsOutstandingNumber",
    "us-gaap:ShareBasedCompensationArrangementByShareBasedPaymentAward"
    "EquityInstrumentsOtherThanOptionsNonvestedNumber",
    "us-gaap:DebtInstrumentConvertibleNumberOfEquityInstruments",
    "ifrs-full:NumberOfSharesOutstanding",
)
PER_SHARE_CONCEPTS = ("us-gaap:ClassOfWarrantOrRightExercisePriceOfWarrantsOrRights1",)
CONVERTIBLE_CONCEPTS = (  # priority order; the first present for a period end wins
    "us-gaap:ConvertibleNotesPayable",
    "us-gaap:ConvertibleDebtNoncurrent",
    "us-gaap:ConvertibleSeniorNotesNoncurrent",
    "us-gaap:ConvertibleNotesPayableCurrent",
)
FORMS = {"10-K", "10-Q", "10-K/A", "10-Q/A", "S-1", "S-1/A", "S-4", "S-4/A", "8-K"}
# M18: foreign private issuers' annual, interim and registration forms.
FPI_FORMS = {"20-F", "20-F/A", "40-F", "40-F/A", "6-K", "6-K/A", "F-1", "F-1/A", "F-4", "F-4/A"}
FORMS |= FPI_FORMS


# Bump when the stored concepts or durations change: every symbol's companyfacts is refetched once.
PARSER_VERSION = "m18.1"  # m14: opex + acquisitions concepts; m18: FPI forms, ifrs-full


def _keep_duration(days: int, ytd: bool = False) -> bool:
    """Instants, quarters and fiscal years; 6- and 9-month year-to-date durations only for
    `YTD_CONCEPTS`."""
    if days == 0 or 80 <= days <= 100 or 350 <= days <= 380:
        return True
    return ytd and (170 <= days <= 195 or 260 <= days <= 285)


@dataclass(frozen=True)
class XbrlFact:
    concept: str
    unit: str
    period_end: str
    period_days: int
    value: Decimal
    fy: int | None
    fp: str | None
    form: str | None
    accession: str | None
    filed: str | None


def _units(facts: Mapping[str, Any], concept: str) -> Mapping[str, Any]:
    ns, name = concept.split(":", 1)
    node = facts.get(ns, {}).get(name)
    return node.get("units", {}) if isinstance(node, Mapping) else {}


def iter_facts(companyfacts: Mapping[str, Any], concept: str, unit: str) -> Iterator[XbrlFact]:
    facts = companyfacts.get("facts", {})
    if not isinstance(facts, Mapping):
        return
    for f in _units(facts, concept).get(unit, []) or []:
        if not isinstance(f, Mapping) or f.get("form") not in FORMS:
            continue
        try:
            end = date.fromisoformat(str(f["end"]))
            start = date.fromisoformat(str(f["start"])) if f.get("start") else None
            value = Decimal(str(f["val"]))
        except (KeyError, ValueError, InvalidOperation):
            continue
        days = (end - start).days + 1 if start else 0
        if not _keep_duration(days, concept in YTD_CONCEPTS):
            continue
        fy = f.get("fy")
        yield XbrlFact(
            concept=concept,
            unit=unit,
            period_end=end.isoformat(),
            period_days=days,
            value=value,
            fy=fy if isinstance(fy, int) else None,
            fp=f.get("fp") if isinstance(f.get("fp"), str) else None,
            form=f.get("form"),
            accession=f.get("accn") if isinstance(f.get("accn"), str) else None,
            filed=f.get("filed") if isinstance(f.get("filed"), str) else None,
        )


def latest_by_period(facts: Iterator[XbrlFact]) -> list[XbrlFact]:
    """One value per (concept, period_end, period_days): the most recently filed."""
    best: dict[tuple[str, str, int], XbrlFact] = {}
    for f in facts:
        k = (f.concept, f.period_end, f.period_days)
        if k not in best or (f.filed or "") >= (best[k].filed or ""):
            best[k] = f
    return sorted(best.values(), key=lambda f: (f.concept, f.period_end, f.period_days))


def fundamentals(companyfacts: Mapping[str, Any]) -> list[XbrlFact]:
    out: list[XbrlFact] = []
    for c in MONEY_CONCEPTS:
        out += latest_by_period(iter_facts(companyfacts, c, "USD"))
    for c in SHARE_CONCEPTS:
        out += latest_by_period(iter_facts(companyfacts, c, "shares"))
    return out


@dataclass(frozen=True)
class XbrlInstrument:
    instrument: str  # 'warrant' | 'convertible'
    as_of: str
    accession: str
    concept: str
    amount: Decimal | None
    shares_underlying: int | None
    strike: Decimal | None


def capital_structure(companyfacts: Mapping[str, Any]) -> list[XbrlInstrument]:
    out: list[XbrlInstrument] = []
    strikes = {
        f.period_end: f.value
        for c in PER_SHARE_CONCEPTS
        for f in latest_by_period(iter_facts(companyfacts, c, "USD/shares"))
        if f.period_days == 0
    }
    for f in latest_by_period(
        iter_facts(companyfacts, "us-gaap:ClassOfWarrantOrRightOutstanding", "shares")
    ):
        if f.period_days == 0 and f.accession:
            out.append(
                XbrlInstrument(
                    "warrant",
                    f.period_end,
                    f.accession,
                    f.concept,
                    None,
                    int(f.value),
                    strikes.get(f.period_end),
                )
            )
    seen: set[str] = set()
    for c in CONVERTIBLE_CONCEPTS:
        for f in latest_by_period(iter_facts(companyfacts, c, "USD")):
            if f.period_days == 0 and f.accession and f.period_end not in seen and f.value > 0:
                seen.add(f.period_end)
                out.append(
                    XbrlInstrument(
                        "convertible", f.period_end, f.accession, f.concept, f.value, None, None
                    )
                )
    return out


# --------------------------------------------------------------------------- instances (M18)

XBRLI = "{http://www.xbrl.org/2003/instance}"
XSI_NIL = "{http://www.w3.org/2001/XMLSchema-instance}nil"
# Namespace URI -> companyfacts taxonomy key. Years/dates in the URIs change every release.
_TAXONOMIES = (
    (re.compile(r"^http://fasb\.org/us-gaap/\d{4}"), "us-gaap"),
    (re.compile(r"^https?://xbrl\.ifrs\.org/taxonomy/[\d-]+/ifrs-full"), "ifrs-full"),
    (re.compile(r"^http://xbrl\.sec\.gov/dei/\d{4}"), "dei"),
)
_MEASURES = {"iso4217:USD": "USD", "xbrli:shares": "shares", "shares": "shares"}


class InstanceError(ValueError):
    pass


def _taxonomy(uri: str) -> str | None:
    return next((key for pat, key in _TAXONOMIES if pat.match(uri)), None)


def instance_facts(xml: str, accession: str, form: str, filed: str) -> dict[str, Any]:
    """One XBRL instance document -> a companyfacts-shaped dict (`{"facts": {tax: {name:
    {"units": {unit: [...]}}}}}`) with this filing's accession, form and filing date on every
    fact. Only non-dimensional contexts (no `segment`/`scenario`), only us-gaap / ifrs-full / dei
    numeric facts in USD or shares. Nil and non-numeric values are skipped; within one instance
    the first value per (concept, unit, period) wins."""
    if re.search(r"<!DOCTYPE|<!ENTITY", xml[:4000], re.IGNORECASE):
        raise InstanceError("XBRL instance must not declare a DTD")
    try:
        root = ET.fromstring(xml.lstrip("\ufeff").strip())  # noqa: S314 (DTD refused above)
    except ET.ParseError as exc:
        raise InstanceError(f"not an XBRL instance: {exc}") from None
    if root.tag != f"{XBRLI}xbrl":
        raise InstanceError(f"not an XBRL instance (root {root.tag!r})")
    periods: dict[str, tuple[str | None, str]] = {}
    for ctx in root.iter(f"{XBRLI}context"):
        cid = ctx.get("id")
        if not cid or ctx.find(f".//{XBRLI}segment") is not None:
            continue
        if ctx.find(f".//{XBRLI}scenario") is not None:
            continue
        p = ctx.find(f"{XBRLI}period")
        if p is None:
            continue
        inst = p.findtext(f"{XBRLI}instant")
        start, end = p.findtext(f"{XBRLI}startDate"), p.findtext(f"{XBRLI}endDate")
        if inst:
            periods[cid] = (None, inst.strip()[:10])
        elif start and end:
            periods[cid] = (start.strip()[:10], end.strip()[:10])
    units: dict[str, str] = {}
    for u in root.iter(f"{XBRLI}unit"):
        measures = [m.text.strip() for m in u.iter(f"{XBRLI}measure") if m.text]
        if u.get("id") and len(measures) == 1 and measures[0] in _MEASURES:
            units[str(u.get("id"))] = _MEASURES[measures[0]]
    out: dict[str, dict[str, Any]] = {}
    seen: set[tuple[str, str, str, str | None, str]] = set()
    for el in root:
        if not el.tag.startswith("{") or el.get(XSI_NIL) == "true":
            continue
        uri, name = el.tag[1:].split("}", 1)
        tax = _taxonomy(uri)
        cref, unit = el.get("contextRef") or "", units.get(el.get("unitRef") or "")
        if tax is None or cref not in periods or unit is None or el.text is None:
            continue
        try:
            val = Decimal(el.text.strip())
        except InvalidOperation:
            continue
        start, end = periods[cref]
        key = (tax, name, unit, start, end)
        if key in seen:
            continue
        seen.add(key)
        row: dict[str, Any] = {
            "end": end,
            "val": int(val) if val == val.to_integral_value() else float(val),
            "accn": accession,
            "form": form,
            "filed": filed,
        }
        if start:
            row["start"] = start
        node = out.setdefault(tax, {}).setdefault(name, {"units": {}})
        node["units"].setdefault(unit, []).append(row)
    return {"facts": out}


def merge_companyfacts(base: Mapping[str, Any], *extra: Mapping[str, Any]) -> dict[str, Any]:
    """`base` with every fact of `extra` appended (a new dict; inputs are not modified).
    `latest_by_period` then keeps the latest-filed value per period."""
    facts: dict[str, dict[str, Any]] = {}
    for src in (base, *extra):
        for tax, names in (src.get("facts") or {}).items():
            if not isinstance(names, Mapping):
                continue
            for name, node in names.items():
                if not isinstance(node, Mapping):
                    continue
                dst = facts.setdefault(tax, {}).setdefault(name, {"units": {}})
                for unit, rows in (node.get("units") or {}).items():
                    dst["units"].setdefault(unit, []).extend(list(rows or []))
    return {**{k: v for k, v in base.items() if k != "facts"}, "facts": facts}


def accessions_in(companyfacts: Mapping[str, Any]) -> set[str]:
    """Every accession companyfacts has at least one fact from."""
    out: set[str] = set()
    for names in (companyfacts.get("facts") or {}).values():
        for node in (names or {}).values():
            for rows in ((node or {}).get("units") or {}).values():
                out |= {r["accn"] for r in rows or [] if isinstance(r, Mapping) and r.get("accn")}
    return out
