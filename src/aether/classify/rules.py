"""Deterministic classification rules (spec §5.2 step 1). No LLM; `rule_id` is recorded.

EDGAR filings are T1, so trust-tier caps (M7) never lower these materialities. Parameters live in
`config/rubric.yaml`; titles and rationales are built from filing metadata only (no prose).

M7 adds `classify_news`: source-domain and headline-pattern rules for news (analyst ratings,
listicles). A company's own (T1) release always goes to the LLM classifier instead.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, replace

from aether.config import ClassifierRubric, Rubric
from aether.edgar.form4 import InsiderTxn
from aether.edgar.submissions import FilingMeta

SIGNAL_CATEGORIES = {"earnings_release"}


@dataclass(frozen=True)
class RuleHit:
    rule_id: str
    cls: str  # SIGNAL | NOISE | RISK
    category: str
    materiality: int
    direction: int
    confidence: float
    title: str
    rationale: str
    evidence_quote: str | None = None


def _hit(
    rule_id: str,
    category: str,
    materiality: int,
    title: str,
    rationale: str,
    evidence: str | None = None,
) -> RuleHit:
    signal = category in SIGNAL_CATEGORIES
    return RuleHit(
        rule_id=rule_id,
        cls="SIGNAL" if signal else "RISK",
        category=category,
        materiality=materiality,
        # Earnings direction depends on results vs guidance, which a form type can't tell.
        direction=0 if signal else -1,
        confidence=1.0,
        title=title,
        rationale=rationale,
        evidence_quote=evidence,
    )


def classify_filing(
    symbol: str,
    f: FilingMeta,
    rubric: Rubric,
    *,
    txns: Sequence[InsiderTxn] = (),
    going_concern_excerpt: str | None = None,
    lockup_excerpt: str | None = None,
) -> RuleHit | None:
    """The single strongest rule hit for a filing, or None (left for the M7 classifier)."""
    hits: list[RuleHit] = []
    d = (f.primary_doc_description or "").strip()
    desc = f" ({d})" if d and d.upper() not in (f.form.upper(), f"FORM {f.form}".upper()) else ""

    for r in rubric.edgar_form_rules:
        if f.form in r.forms:
            hits.append(
                _hit(
                    r.rule_id,
                    r.category,
                    r.materiality,
                    f"{symbol} {f.form}{desc}",
                    f"SEC form {f.form} filed {f.filed_at}; rule {r.rule_id}.",
                    lockup_excerpt,
                )
            )

    if f.form in ("8-K", "8-K/A"):
        for ir in rubric.edgar_8k_item_rules:
            if ir.item in f.items:
                hits.append(
                    _hit(
                        ir.rule_id,
                        ir.category,
                        ir.materiality,
                        f"{symbol} {f.form} Items {', '.join(f.items)}",
                        f"8-K Item {ir.item} filed {f.filed_at}; rule {ir.rule_id}.",
                    )
                )

    if f.form in ("4", "4/A"):
        sales = [t for t in txns if t.is_open_market_sale and not t.is_derivative]
        if sales:
            rule = rubric.insider_selling
            all_plan = all(t.is_10b5_1 for t in sales)
            shares = sum(t.shares or 0 for t in sales)
            plan = " (10b5-1)" if all_plan else ""
            who = sales[0].insider
            hits.append(
                _hit(
                    rule.rule_id,
                    "insider_selling",
                    rule.materiality_10b5_1_only if all_plan else rule.materiality,
                    f"{symbol} Form {f.form}: {who} sold {shares:,} shares{plan}",
                    f"{len(sales)} code-S sale(s), {shares:,} shares, "
                    f"{'all' if all_plan else 'not all'} under a 10b5-1 plan; rule {rule.rule_id}.",
                )
            )

    if going_concern_excerpt is not None:
        gc = rubric.going_concern
        hits.append(
            _hit(
                gc.rule_id,
                "going_concern",
                gc.materiality,
                f"{symbol} {f.form}: going-concern language",
                f"Unhedged 'substantial doubt … going concern' statement in {f.form} "
                f"filed {f.filed_at}; rule {gc.rule_id}.",
                going_concern_excerpt,
            )
        )

    if not hits:
        return None
    best = max(hits, key=lambda h: h.materiality)
    others = [h.rule_id for h in hits if h is not best]
    if others:
        best = replace(best, rationale=best.rationale + f" Also matched: {', '.join(others)}.")
    return best


# --------------------------------------------------------------------------- news (M7)


def _noise_hit(
    rule_id: str, category: str, materiality: int, confidence: float, why: str
) -> RuleHit:
    return RuleHit(
        rule_id=rule_id,
        cls="NOISE",
        category=category,
        materiality=materiality,
        direction=0,
        confidence=confidence,
        title="",
        rationale=why,
    )


def classify_news(title: str, domain: str, tier: str, rubric: ClassifierRubric) -> RuleHit | None:
    """A headline/domain rule hit for a news item, or None (left for the LLM)."""
    if tier == "T1":
        return None
    d = domain.lower().rstrip(".")
    for nd in rubric.noise_domains:
        if d == nd or d.endswith("." + nd):
            return _noise_hit(
                "news_noise_domain",
                "listicle_or_momentum",
                1,
                1.0,
                f"Source domain {nd} is configured as a noise domain; rule news_noise_domain.",
            )
    for rule in rubric.headline_rules:
        for pattern in rule.patterns:
            if re.search(pattern, title):
                return _noise_hit(
                    rule.rule_id,
                    rule.category,
                    rule.materiality,
                    rule.confidence,
                    f"Headline matches a {rule.category.replace('_', ' ')} pattern; "
                    f"rule {rule.rule_id}.",
                )
    return None
