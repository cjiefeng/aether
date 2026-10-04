"""SEC XBRL `companyfacts` -> quarterly fundamentals and XBRL-tagged capital structure.

Only non-dimensional facts appear in companyfacts. New filers (QNT, INFQ) lack many concepts;
anything missing is simply absent, never filled in.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

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
)
SHARE_CONCEPTS = (
    "dei:EntityCommonStockSharesOutstanding",
    "us-gaap:CommonStockSharesOutstanding",
    "us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding",
    "us-gaap:ClassOfWarrantOrRightOutstanding",
)
PER_SHARE_CONCEPTS = ("us-gaap:ClassOfWarrantOrRightExercisePriceOfWarrantsOrRights1",)
CONVERTIBLE_CONCEPTS = (  # priority order; the first present for a period end wins
    "us-gaap:ConvertibleNotesPayable",
    "us-gaap:ConvertibleDebtNoncurrent",
    "us-gaap:ConvertibleSeniorNotesNoncurrent",
    "us-gaap:ConvertibleNotesPayableCurrent",
)
FORMS = {"10-K", "10-Q", "10-K/A", "10-Q/A", "S-1", "S-1/A", "S-4", "S-4/A", "8-K"}


def _keep_duration(days: int) -> bool:
    """Instants, quarters and fiscal years; drop 6- and 9-month year-to-date durations."""
    return days == 0 or 80 <= days <= 100 or 350 <= days <= 380


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
        if not _keep_duration(days):
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
