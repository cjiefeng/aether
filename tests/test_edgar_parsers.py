"""EDGAR parsers on recorded SEC responses (real filings) and synthetic ACME inputs."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest

from aether.edgar import xbrl
from aether.edgar.form4 import Form4Error, parse_form4
from aether.edgar.submissions import FilingMeta, iter_filings
from aether.edgar.text import (
    EXCERPT_MAX,
    extract_atm,
    extract_going_concern,
    extract_lockup,
    extract_offering,
    html_to_text,
)
from tests.cassettes import cassette_text

# --------------------------------------------------------------------------- submissions


def _filings(name: str, cik: str) -> dict[str, FilingMeta]:
    subs = json.loads(cassette_text(name))
    return {f.accession: f for f in iter_filings(subs["filings"]["recent"], cik)}


def test_submissions_parse_recorded_qbts() -> None:
    fs = _filings("sec_submissions_qbts", "0001907982")
    s3 = fs["0001628280-26-002629"]
    assert s3.form == "S-3ASR"
    assert s3.filed_at == "2026-01-20"
    assert s3.url == (
        "https://www.sec.gov/Archives/edgar/data/1907982/000162828026002629/"
        "0001628280-26-002629-index.htm"
    )
    eightk = [f for f in fs.values() if f.form == "8-K" and f.items]
    assert eightk and all("," not in i for f in eightk for i in f.items)
    assert all(f.accepted_at is None or f.accepted_at.endswith("Z") for f in fs.values())


def test_submissions_skip_malformed_rows() -> None:
    block = {
        "accessionNumber": ["0000000001-26-000001", "bad", "0000000001-26-000002"],
        "filingDate": ["2026-01-02", "2026-01-02", "not-a-date"],
        "form": ["8-K", "8-K", "4"],
        "primaryDocument": ["../etc/passwd", "x.htm", "y.xml"],
        "items": ["2.02,9.01", "", ""],
    }
    rows = list(iter_filings(block, "1"))
    assert [r.accession for r in rows] == ["0000000001-26-000001"]
    assert rows[0].primary_doc is None  # path traversal rejected
    assert rows[0].items == ("2.02", "9.01")


# --------------------------------------------------------------------------- Form 4


def test_form4_recorded_rgti_sale_not_under_plan() -> None:
    f4 = parse_form4(cassette_text("sec_form4_rgti_sale"))
    assert f4.issuer_symbol == "RGTI" and not f4.aff_10b5_1
    (t,) = f4.txns
    assert (t.code, t.acquired_disposed, t.shares, t.price) == ("S", "D", 3860, 16.8877)
    assert t.is_open_market_sale and not t.is_10b5_1 and not t.is_derivative
    assert t.txn_date == "2026-08-20"


def test_form4_recorded_rgti_sale_under_plan() -> None:
    f4 = parse_form4(cassette_text("sec_form4_rgti_sale_10b5_1"))
    assert f4.aff_10b5_1
    sales = [t for t in f4.txns if t.code == "S"]
    assert len(sales) == 1 and sales[0].shares == 120000 and sales[0].is_10b5_1
    assert any(t.code == "M" for t in f4.txns)  # option exercise, not a sale


def test_form4_recorded_ionq_true_flag_and_tax_withholding() -> None:
    sale = parse_form4(cassette_text("sec_form4_ionq_sale_10b5_1"))
    assert sale.issuer_symbol == "IONQ" and sale.aff_10b5_1  # flag spelled "true"
    assert [t.code for t in sale.txns] == ["S"]
    tax = parse_form4(cassette_text("sec_form4_ionq_tax_withholding"))
    assert [t.code for t in tax.txns] == ["F"]
    assert not any(t.is_open_market_sale for t in tax.txns)


ACME_F4 = """<?xml version="1.0"?>
<ownershipDocument>
  <issuer><issuerCik>0000000001</issuerCik><issuerTradingSymbol>ACME</issuerTradingSymbol></issuer>
  <reportingOwner>
    <reportingOwnerId>
      <rptOwnerCik>0000000009</rptOwnerCik><rptOwnerName>Doe Jane</rptOwnerName>
    </reportingOwnerId>
    <reportingOwnerRelationship>
      <isOfficer>1</isOfficer><officerTitle>CFO</officerTitle>
    </reportingOwnerRelationship>
  </reportingOwner>
  <reportingOwner>
    <reportingOwnerId>
      <rptOwnerCik>0000000010</rptOwnerCik><rptOwnerName>Doe Fund LP</rptOwnerName>
    </reportingOwnerId>
    <reportingOwnerRelationship>
      <isTenPercentOwner>true</isTenPercentOwner>
    </reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2026-01-05</value></transactionDate>
      <transactionCoding>
        <transactionCode>S</transactionCode><footnoteId id="F1"/>
      </transactionCoding>
      <transactionAmounts>
        <transactionShares><value>100.5</value></transactionShares>
        <transactionPricePerShare><value>1.25</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <transactionDate><value>2026-01-06</value></transactionDate>
      <transactionCoding>
        <transactionCode>S</transactionCode><footnoteId id="F2"/>
      </transactionCoding>
      <transactionAmounts>
        <transactionShares><value>10</value></transactionShares>
        <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
  <footnotes>
    <footnote id="F1">Sold pursuant to a Rule 10b5-1 trading plan adopted on 2025-06-01.</footnote>
    <footnote id="F2">This sale was not made pursuant to a Rule 10b5-1 plan.</footnote>
  </footnotes>
</ownershipDocument>"""


def test_form4_synthetic_footnotes_rounding_and_joint_filers() -> None:
    f4 = parse_form4(ACME_F4)
    a, b = f4.txns
    assert a.shares == 100  # 100.5 rounds half-even to whole shares
    assert a.is_10b5_1 and not b.is_10b5_1  # negated footnote is not a plan sale
    assert a.insider == "Doe Jane (+1 joint filer)"
    assert a.role == "Officer: CFO | 10% owner"
    assert a.insider_cik == "0000000009"


@pytest.mark.parametrize(
    "doc",
    [
        '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><ownershipDocument/>',
        "not xml at all",
        "<html><body>nope</body></html>",
        "<ownershipDocument></ownershipDocument>",
    ],
)
def test_form4_rejects_bad_documents(doc: str) -> None:
    with pytest.raises(Form4Error):
        parse_form4(doc)


# --------------------------------------------------------------------------- text extractors


def test_qnt_lockup_from_recorded_424b4() -> None:
    """M2 acceptance: QNT lock-up date extracted from the recorded final prospectus."""
    text = html_to_text(cassette_text("sec_424b4_qnt"))
    lk = extract_lockup(text)
    assert lk is not None
    assert lk.prospectus_date == date(2026, 6, 3)
    assert lk.days == 180
    assert lk.expiry == date(2026, 11, 30)
    assert lk.early_release_possible
    assert "180 days after the date of this prospectus" in lk.excerpt
    assert len(lk.excerpt) <= EXCERPT_MAX
    assert extract_going_concern(text) is None


def test_lockup_needs_prospectus_date_and_context() -> None:
    no_date = "lock-up agreements for a period of 180 days after the date of this prospectus."
    assert extract_lockup(no_date) is None
    no_context = (
        "Prospectus dated March 2, 2026. Offer valid 90 days after the date of this prospectus."
    )
    assert extract_lockup(no_context) is None
    two = (
        "Prospectus dated March 2, 2026. Our lock-up agreements: directors for a period of 90 days "
        "after the date of this prospectus; holders for a period of 180 days after the date of "
        "this prospectus; officers for a period of 180 days after the date of this prospectus."
    )
    lk = extract_lockup(two)
    assert lk is not None and lk.days == 180 and lk.expiry == date(2026, 8, 29)


@pytest.mark.parametrize(
    ("text", "flag"),
    [
        (
            "These conditions raise substantial doubt about the Company's ability to continue as a "
            "going concern.",
            True,
        ),
        (
            "If we cannot raise capital, there could be substantial doubt about our ability to "
            "continue as a going concern.",
            False,
        ),
        (
            "Management concluded that substantial doubt about our ability to continue as a going "
            "concern has been alleviated.",
            False,
        ),
        ("We have no going-concern issues.", False),
        (
            "In our prior annual report we disclosed that there was substantial doubt about our "
            "ability to continue as a going concern.",
            False,
        ),
        (
            "There is substantial doubt about ACME's ability to continue as a going concern. "
            "Following the financing, management concluded this doubt has been alleviated.",
            False,
        ),
    ],
)
def test_going_concern_only_unhedged(text: str, flag: bool) -> None:
    assert (extract_going_concern(text) is not None) is flag


def test_atm_amounts() -> None:
    a = extract_atm(
        "ACME entered into an at-the-market sales agreement for shares having an aggregate "
        "offering price of up to $150,000,000 through the agents."
    )
    assert a is not None and a.amount == Decimal(150_000_000)
    b = extract_atm("At The Market program with an aggregate sales price of up to $1.5 billion.")
    assert b is not None and b.amount == Decimal("1500000000.0")
    # Base-prospectus boilerplate: ATM sales allowed, no program size nearby.
    assert extract_atm("We may sell shares in at-the-market offerings.") is None
    assert extract_atm("An underwritten public offering of 1,000,000 shares.") is None


def test_html_to_text_skips_hidden_xbrl_header_and_scripts() -> None:
    html = (
        "<html><head><title>t</title></head><body><ix:header>HIDDEN</ix:header>"
        "<script>evil()</script><p>Prospectus&nbsp;dated</p><p>March&#160;2, 2026</p></body></html>"
    )
    assert html_to_text(html) == "Prospectus dated March 2, 2026"


# --------------------------------------------------------------------------- XBRL


def test_companyfacts_recorded_qnt() -> None:
    cf = json.loads(cassette_text("sec_companyfacts_qnt"))
    facts = xbrl.fundamentals(cf)
    by = {(f.concept, f.period_days) for f in facts}
    assert any(c == "us-gaap:Revenues" and 80 <= d <= 100 for c, d in by)
    # YTD (6/9-month) durations only for the flow concepts TTM needs (M9).
    assert all(d == 0 or 80 <= d <= 100 or 350 <= d <= 380 or c in xbrl.YTD_CONCEPTS for c, d in by)
    keys = [(f.concept, f.period_end, f.period_days) for f in facts]
    assert len(keys) == len(set(keys))
    # New filer: concepts it doesn't tag are simply absent.
    assert not any(f.concept == "dei:EntityCommonStockSharesOutstanding" for f in facts)


def _cf(facts: dict[str, object]) -> dict[str, object]:
    return {"facts": {"us-gaap": facts}}


def test_capital_structure_from_synthetic_xbrl() -> None:
    def fact(
        val: object, end: str, filed: str, accn: str = "0000000001-26-000001"
    ) -> dict[str, object]:
        return {"end": end, "val": val, "accn": accn, "form": "10-Q", "filed": filed, "fy": 2026}

    cf = _cf(
        {
            "ClassOfWarrantOrRightOutstanding": {
                "units": {"shares": [fact(5_000_000, "2026-03-31", "2026-05-01")]}
            },
            "ClassOfWarrantOrRightExercisePriceOfWarrantsOrRights1": {
                "units": {"USD/shares": [fact(11.5, "2026-03-31", "2026-05-01")]}
            },
            "ConvertibleNotesPayable": {
                "units": {
                    "USD": [
                        fact(1000, "2026-03-31", "2026-05-01"),
                        fact(2000, "2026-03-31", "2026-08-01", "0000000001-26-000002"),
                    ]
                }
            },
        }
    )
    items = {i.instrument: i for i in xbrl.capital_structure(cf)}
    w, c = items["warrant"], items["convertible"]
    assert (w.shares_underlying, w.strike) == (5_000_000, Decimal("11.5"))
    assert c.amount == Decimal(2000) and c.accession == "0000000001-26-000002"  # latest filed


# --------------------------------------------------------------------------- offering size (M13)


def test_extract_offering_cover_shares_and_prefunded() -> None:
    o = extract_offering(
        "PROSPECTUS SUPPLEMENT Acme Quantum, Inc. We are offering 12,500,000 shares of our "
        "Class A common stock and, in lieu of common stock to certain investors, pre-funded "
        "warrants to purchase up to 2,500,000 shares of Class A common stock. The underwriters "
        "have an option to purchase up to an additional 2,250,000 shares."
    )
    assert o is not None and (o.shares, o.prefunded) == (15_000_000, 2_500_000)
    assert "12,500,000 shares" in o.excerpt and len(o.excerpt) <= EXCERPT_MAX
    plain = extract_offering("We are offering 4000000 shares of common stock at $2.50 per share.")
    assert plain is not None and (plain.shares, plain.prefunded) == (4_000_000, 0)


def test_extract_offering_returns_none_without_a_cover_size() -> None:
    # An ATM states a dollar amount, not shares; resale and boilerplate text don't count.
    assert (
        extract_offering("Sales agreement for an aggregate offering price of up to $100M.") is None
    )
    assert extract_offering("The selling stockholders are offering 1,000,000 shares.") is None
    # Beyond the cover area: not read.
    far = "x " * 15_000 + "We are offering 1,000,000 shares of common stock."
    assert extract_offering(far) is None


def test_parse_document_stores_the_offering_for_424b() -> None:
    from aether.ingest.edgar import parse_document

    meta = FilingMeta(
        accession="0000000001-26-000099",
        cik="0000000001",
        form="424B5",
        filed_at="2026-03-09",
        accepted_at=None,
        report_date=None,
        items=(),
        primary_doc="acme.htm",
        primary_doc_description=None,
        is_xbrl=False,
    )
    body = "<p>We are offering 3,000,000 shares of our common stock.</p>"
    summary = parse_document("ACME", meta, body).summary
    assert summary["offering"] == {"shares": 3_000_000, "prefunded": 0}
    assert summary["atm"] is None
