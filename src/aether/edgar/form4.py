"""Form 3/4/5 ownership XML -> insider transactions (spec §4: transaction code + 10b5-1 flag).

Stdlib `xml.etree` on a document that must not declare a DTD: ownership XML never needs one,
and refusing it rules out entity-expansion tricks in untrusted input.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation


class Form4Error(ValueError):
    """The document is not parseable ownership XML."""


@dataclass(frozen=True)
class InsiderTxn:
    seq: int
    insider_cik: str | None
    insider: str
    role: str | None
    security: str | None
    txn_date: str
    code: str
    acquired_disposed: str | None
    shares: int | None
    price: float | None
    is_10b5_1: bool
    is_derivative: bool

    @property
    def is_open_market_sale(self) -> bool:
        return self.code == "S" and self.acquired_disposed in (None, "D")


@dataclass(frozen=True)
class Form4:
    issuer_symbol: str | None
    issuer_cik: str | None
    aff_10b5_1: bool  # the filing-level "10b5-1(c)" checkbox (filings since April 2023)
    txns: tuple[InsiderTxn, ...]


_TRUE = {"1", "true", "y", "yes"}
_10B51 = re.compile(r"10b5-?1", re.IGNORECASE)
_NOT_10B51 = re.compile(r"\bnot\b[^.]{0,60}10b5-?1", re.IGNORECASE)


def _text(el: ET.Element | None, path: str) -> str | None:
    if el is None:
        return None
    v = el.findtext(path)
    v = v.strip() if v else None
    return v or None


def _flag(v: str | None) -> bool:
    return (v or "").strip().lower() in _TRUE


def _shares(v: str | None) -> int | None:
    if v is None:
        return None
    try:
        return int(Decimal(v).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))
    except InvalidOperation:
        return None


def _price(v: str | None) -> float | None:
    if v is None:
        return None
    try:
        return float(Decimal(v))
    except InvalidOperation:
        return None


def _role(owner: ET.Element) -> str | None:
    rel = owner.find("reportingOwnerRelationship")
    if rel is None:
        return None
    parts = []
    if _flag(_text(rel, "isDirector")):
        parts.append("Director")
    if _flag(_text(rel, "isOfficer")):
        title = _text(rel, "officerTitle")
        parts.append(f"Officer: {title}" if title else "Officer")
    if _flag(_text(rel, "isTenPercentOwner")):
        parts.append("10% owner")
    if _flag(_text(rel, "isOther")):
        other = _text(rel, "otherText")
        parts.append(f"Other: {other}" if other else "Other")
    return "; ".join(parts) or None


def parse_form4(xml_text: str) -> Form4:
    if re.search(r"<!DOCTYPE|<!ENTITY", xml_text[:4000], re.IGNORECASE):
        raise Form4Error("ownership XML must not declare a DTD")
    try:
        root = ET.fromstring(xml_text.lstrip("﻿").strip())  # noqa: S314 (DTD refused above)
    except ET.ParseError as exc:
        raise Form4Error(f"not XML: {exc}") from exc
    if root.tag != "ownershipDocument":
        raise Form4Error(f"unexpected root element {root.tag!r}")

    owners = root.findall("reportingOwner")
    if not owners:
        raise Form4Error("no reportingOwner")
    first = owners[0]
    insider = _text(first, "reportingOwnerId/rptOwnerName") or "unknown"
    insider_cik = _text(first, "reportingOwnerId/rptOwnerCik")
    roles = [r for r in (_role(o) for o in owners) if r]
    role = " | ".join(dict.fromkeys(roles)) or None
    if len(owners) > 1:
        insider = f"{insider} (+{len(owners) - 1} joint filer{'s' if len(owners) > 2 else ''})"

    footnotes = {
        fn.get("id", ""): " ".join("".join(fn.itertext()).split())
        for fn in root.iterfind("footnotes/footnote")
    }
    aff = _flag(_text(root, "aff10b5One"))

    def fn_says_10b5_1(t: ET.Element) -> bool:
        ids = {f.get("id", "") for f in t.iter("footnoteId")}
        texts = [footnotes.get(i, "") for i in ids]
        return any(_10B51.search(x) and not _NOT_10B51.search(x) for x in texts)

    txns: list[InsiderTxn] = []
    for is_deriv, path in (
        (False, "nonDerivativeTable/nonDerivativeTransaction"),
        (True, "derivativeTable/derivativeTransaction"),
    ):
        for t in root.iterfind(path):
            code = _text(t, "transactionCoding/transactionCode")
            d = _text(t, "transactionDate/value")
            if not code or len(code) != 1 or not d:
                continue
            try:
                txn_date = date.fromisoformat(d[:10]).isoformat()
            except ValueError:
                continue
            ad = _text(t, "transactionAmounts/transactionAcquiredDisposedCode/value")
            txns.append(
                InsiderTxn(
                    seq=len(txns),
                    insider_cik=insider_cik,
                    insider=insider,
                    role=role,
                    security=_text(t, "securityTitle/value"),
                    txn_date=txn_date,
                    code=code.upper(),
                    acquired_disposed=ad if ad in ("A", "D") else None,
                    shares=_shares(_text(t, "transactionAmounts/transactionShares/value")),
                    price=_price(_text(t, "transactionAmounts/transactionPricePerShare/value")),
                    is_10b5_1=aff or fn_says_10b5_1(t),
                    is_derivative=is_deriv,
                )
            )
    return Form4(
        issuer_symbol=_text(root, "issuer/issuerTradingSymbol"),
        issuer_cik=_text(root, "issuer/issuerCik"),
        aff_10b5_1=aff,
        txns=tuple(txns),
    )
