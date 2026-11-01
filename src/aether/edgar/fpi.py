"""Foreign private issuers' XBRL beyond companyfacts (spec §6.7.4, M18). Shared by the EDGAR ingest
(active names) and the universe review's candidate red-flag checks.

An FPI files 20-F / 40-F annual reports and furnishes interim results on 6-K. companyfacts can
miss those interim figures (XNDU's Q2 2026 statements were only in a 6-K's inline XBRL, checked
2026-11-01), so the latest XBRL-bearing 6-K, annual report and F-1 are read directly: the filing
folder's `index.json` names the instance SEC extracts from inline XBRL (`*_htm.xml`). A filing
whose facts companyfacts already has is skipped (no request).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from aether.edgar import xbrl
from aether.edgar.submissions import FilingMeta
from aether.providers.edgar import EdgarClient
from aether.providers.prices import ProviderError

log = logging.getLogger(__name__)

ANNUAL_FPI = frozenset({"20-F", "20-F/A", "40-F", "40-F/A"})
ANNUAL_US = frozenset({"10-K", "10-K/A"})
# One instance per group: the latest XBRL filing of each (newest first within a group).
INSTANCE_GROUPS: tuple[frozenset[str], ...] = (
    frozenset({"6-K", "6-K/A"}),
    ANNUAL_FPI,
    frozenset({"F-1", "F-1/A"}),
)
MAX_INSTANCES = 3
_LINKBASE = re.compile(r"_(cal|def|lab|pre)\.xml$", re.IGNORECASE)
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,199}\.xml$")


def is_fpi(metas: Sequence[FilingMeta]) -> bool:
    """The latest annual report is a 20-F / 40-F; with none, 6-Ks and no 10-K."""
    annual = [m for m in metas if m.form in ANNUAL_FPI | ANNUAL_US]
    if annual:
        return max(annual, key=lambda m: (m.filed_at, m.accession)).form in ANNUAL_FPI
    return any(m.form.startswith("6-K") for m in metas)


def instance_name(index: Mapping[str, Any]) -> str | None:
    """The XBRL instance in a filing folder listing: `*_htm.xml` (extracted from inline XBRL),
    else the only `.xml` that isn't a linkbase or SEC's FilingSummary."""
    items = (index.get("directory") or {}).get("item") or []
    names = [str(i.get("name")) for i in items if isinstance(i, Mapping) and i.get("name")]
    names = [n for n in names if _NAME.fullmatch(n)]
    inline = sorted(n for n in names if n.lower().endswith("_htm.xml"))
    if inline:
        return inline[0]
    plain = [n for n in names if not _LINKBASE.search(n) and n != "FilingSummary.xml"]
    return plain[0] if len(plain) == 1 else None


@dataclass
class Instances:
    facts: list[dict[str, Any]] = field(default_factory=list)  # companyfacts-shaped, one each
    used: list[dict[str, str]] = field(default_factory=list)  # accession, form, filed, name
    errors: list[str] = field(default_factory=list)


def pick(metas: Sequence[FilingMeta], have: set[str]) -> list[FilingMeta]:
    """The filings whose instance is worth reading: the latest XBRL filing per group, unless
    companyfacts already holds its facts."""
    out = []
    for group in INSTANCE_GROUPS:
        xs = [m for m in metas if m.form in group and m.is_xbrl]
        if not xs:
            continue
        latest = max(xs, key=lambda m: (m.filed_at, m.accession))
        if latest.accession not in have:
            out.append(latest)
    return out[:MAX_INSTANCES]


def fetch_instances(
    client: EdgarClient, cik: str, metas: Sequence[FilingMeta], companyfacts: Mapping[str, Any]
) -> Instances:
    """Network: 2 requests per instance (folder index + instance). Failures are kept as
    reasons, never raised: companyfacts alone is still usable."""
    res = Instances()
    for m in pick(metas, xbrl.accessions_in(companyfacts)):
        try:
            name = instance_name(client.filing_index(cik, m.accession))
            if name is None:
                res.errors.append(f"{m.form} {m.accession}: no XBRL instance in the filing index")
                continue
            body = client.document(cik, m.accession, name)
            res.facts.append(xbrl.instance_facts(body, m.accession, m.form, m.filed_at))
            res.used.append(
                {"accession": m.accession, "form": m.form, "filed": m.filed_at, "name": name}
            )
        except (ProviderError, ValueError) as exc:
            log.warning("FPI instance %s %s failed: %s", cik, m.accession, exc)
            res.errors.append(f"{m.form} {m.accession}: {exc}"[:200])
    return res
