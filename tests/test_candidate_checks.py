"""M18 acceptance (spec §6.7.4): the four §6.10 red flags for add-qualified candidates, from SEC
submissions + companyfacts (+ 424B covers and, for foreign private issuers, the XBRL instance of
the latest 6-K / 20-F / F-1). Synthetic ACME-style data on a fake SEC client; no network."""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, func, insert, select

from aether.config import load_rubric, load_thesis_config, load_universe_config
from aether.db.engine import write_tx
from aether.db.models import fundamentals_q
from aether.edgar import fpi, xbrl
from aether.edgar.submissions import FilingMeta
from aether.ingest.edgar import ingest_edgar
from aether.universe import candidate_checks as cc
from aether.universe import gates
from tests.conftest import CONFIG_DIR, seed_tickers

TODAY = date(2026, 11, 1)
THESIS = load_thesis_config(CONFIG_DIR)
UNI = load_universe_config(CONFIG_DIR)
CIK = "0000000301"
OLD_LISTING = date(2020, 1, 2)
SRC = Path(__file__).resolve().parents[1] / "src" / "aether"

COVER = "<html><body><p>We are offering {n} shares of our common stock.</p></body></html>"
NO_COVER = "<html><body><p>Prospectus supplement.</p></body></html>"


# --------------------------------------------------------------------------- builders


def fact(
    val: int,
    end: str,
    *,
    start: str | None = None,
    filed: str = "2026-08-10",
    form: str = "10-Q",
    accn: str = "0000000301-26-000050",
) -> dict[str, Any]:
    row: dict[str, Any] = {"val": val, "end": end, "filed": filed, "form": form, "accn": accn}
    if start:
        row["start"] = start
    return row


def cfacts(**concepts: list[dict[str, Any]]) -> dict[str, Any]:
    """`us_gaap__Concept=[...]` / `dei__Concept=[...]` / `ifrs_full__Concept=[...]`."""
    out: dict[str, Any] = {}
    for key, rows in concepts.items():
        tax, name = key.split("__")
        tax = tax.replace("_", "-")
        unit = "shares" if "Shares" in name else "USD"
        out.setdefault(tax, {})[name] = {"units": {unit: rows}}
    return {"facts": out}


def fy(val: int, end: str, start: str, **kw: Any) -> dict[str, Any]:
    return fact(val, end, start=start, form="10-K", **kw)


def healthy(**over: Any) -> dict[str, Any]:
    """A cash-generative US filer, fresh data: every money check passes."""
    base: dict[str, list[dict[str, Any]]] = {
        "us_gaap__CashAndCashEquivalentsAtCarryingValue": [fact(500_000_000, "2026-06-30")],
        "us_gaap__NetCashProvidedByUsedInOperatingActivities": [
            fy(50_000_000, "2026-06-30", "2025-07-01"),
            fy(40_000_000, "2025-06-30", "2024-07-01", filed="2025-08-10"),
        ],
        "us_gaap__Revenues": [
            fy(300_000_000, "2026-06-30", "2025-07-01"),
            fy(250_000_000, "2025-06-30", "2024-07-01", filed="2025-08-10"),
        ],
        "us_gaap__OperatingExpenses": [
            fy(200_000_000, "2026-06-30", "2025-07-01"),
            fy(190_000_000, "2025-06-30", "2024-07-01", filed="2025-08-10"),
        ],
        "us_gaap__PaymentsToAcquireBusinessesNetOfCashAcquired": [
            fy(0, "2026-06-30", "2025-07-01"),
            fy(0, "2025-06-30", "2024-07-01", filed="2025-08-10"),
        ],
        "dei__EntityCommonStockSharesOutstanding": [
            fact(100_000_000, "2026-08-01"),
            fact(98_000_000, "2025-08-01", filed="2025-08-10"),
        ],
    }
    base.update(over)
    return cfacts(**base)


@dataclass
class Filing:
    form: str
    filed: str
    accession: str
    items: str = ""
    doc: str = "doc.htm"
    xbrl: int = 0


@dataclass
class FakeSec:
    filings: list[Filing] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=lambda: healthy())
    docs: dict[str, str] = field(default_factory=dict)  # accession -> body
    index: dict[str, list[str]] = field(default_factory=dict)  # accession -> file names
    calls: list[str] = field(default_factory=list)
    requests_made: int = 0
    in_tx: list[bool] = field(default_factory=list)

    def _hit(self, what: str) -> None:
        self.calls.append(what)
        self.requests_made += 1

    def submissions(self, cik: str) -> dict[str, Any]:
        self._hit("submissions")
        cols: dict[str, list[Any]] = {
            k: []
            for k in ("form", "filingDate", "accessionNumber", "items", "primaryDocument", "isXBRL")
        }
        for f in self.filings:
            cols["form"].append(f.form)
            cols["filingDate"].append(f.filed)
            cols["accessionNumber"].append(f.accession)
            cols["items"].append(f.items)
            cols["primaryDocument"].append(f.doc)
            cols["isXBRL"].append(f.xbrl)
        return {"filings": {"recent": cols, "files": []}}

    def submissions_page(self, name: str) -> dict[str, Any]:
        raise AssertionError("no older pages in these fixtures")

    def companyfacts(self, cik: str) -> dict[str, Any]:
        self._hit("companyfacts")
        return self.facts

    def filing_index(self, cik: str, accession: str) -> dict[str, Any]:
        self._hit(f"index {accession}")
        names = self.index.get(accession, [])
        return {"directory": {"item": [{"name": n} for n in names]}}

    def document(self, cik: str, accession: str, primary_doc: str, *, raw: bool = False) -> str:
        self._hit(f"document {accession} {primary_doc}")
        if accession not in self.docs:
            from aether.providers.prices import ProviderError

            raise ProviderError("SEC 404")
        return self.docs[accession]


def acc(n: int) -> str:
    return f"0000000301-26-{n:06d}"


def run(
    sec: FakeSec, *, first: date | None = OLD_LISTING, cfg: Any = UNI, today: date = TODAY
) -> dict[str, Any]:
    return cc.run(
        sec,  # type: ignore[arg-type]
        "ACME",
        CIK,
        as_of=today,
        first_session=first,
        cfg=cfg,
        thesis_cfg=THESIS,
    )


def check(res: dict[str, Any], key: str) -> dict[str, Any]:
    return next(c for c in res["checks"] if c["key"] == key)


# --------------------------------------------------------------------------- issuance


def test_two_8k_3_02s_in_12_months_flag_issuance() -> None:
    sec = FakeSec(
        filings=[
            Filing("8-K", "2026-02-10", acc(1), "3.02,9.01"),
            Filing("8-K", "2026-07-15", acc(2), "3.02"),
            Filing("8-K", "2025-09-01", acc(3), "3.02"),  # outside 365 days
        ]
    )
    res = run(sec)
    iss = check(res, "issuance")
    assert iss["status"] == "flag"
    assert [e["accession"] for e in iss["evidence"] if "form" in e and "3.02" in e["form"]] == [
        acc(1),
        acc(2),
    ]
    assert res["n_flags"] == 1 and res["n_unknown"] == 0
    assert set(iss["accessions"]) >= {acc(1), acc(2)}


def test_no_financing_but_shares_up_40pct_flags_and_cites_both_counts() -> None:
    sec = FakeSec(
        facts=healthy(
            dei__EntityCommonStockSharesOutstanding=[
                fact(140_000_000, "2026-08-01", accn=acc(60)),
                fact(100_000_000, "2025-08-01", filed="2025-08-10", accn=acc(40)),
            ]
        )
    )
    iss = check(run(sec), "issuance")
    assert iss["status"] == "flag" and "+40.0% YoY" in iss["text"]
    counts = {e["kind"]: e for e in iss["evidence"] if "kind" in e}
    assert counts["shares_from"]["common"] == 100_000_000
    assert counts["shares_to"]["common"] == 140_000_000
    assert counts["shares_from"]["accession"] == acc(40) and counts["shares_to"][
        "accession"
    ] == acc(60)
    assert "proxy" in iss["text"]  # no warrants/options tagged: shares outstanding is the proxy


def test_listed_under_a_year_share_yoy_is_unknown() -> None:
    res = run(FakeSec(), first=date(2026, 3, 27))
    iss = check(res, "issuance")
    assert iss["status"] == "unknown" and "listed < 1 year" in iss["text"]


def test_424b3_is_resale_overhang_not_issuance() -> None:
    sec = FakeSec(
        filings=[
            Filing("424B3", "2026-05-15", acc(1)),
            Filing("424B3", "2026-08-21", acc(2)),
        ]
    )
    res = run(sec)
    iss = check(res, "issuance")
    assert iss["status"] == "ok" and "0 dilutive financings" in iss["text"]
    assert "resale overhang: 2 424B3" in iss["text"]
    assert [r["accession"] for r in res["resale_overhang"]] == [acc(1), acc(2)]
    assert not [c for c in sec.calls if c.startswith("document")]  # 424B3 covers aren't read


@pytest.mark.parametrize(
    ("second", "body2", "n"),
    [
        (("424B2", "2026-06-01"), COVER.format(n="5,000,000"), 1),  # same day, same size
        (("424B2", "2026-06-01"), COVER.format(n="6,000,000"), 2),  # different size
        (("424B5", "2026-06-02"), COVER.format(n="5,000,000"), 2),  # different day
        (("424B2", "2026-06-01"), NO_COVER, 2),  # size not parsed: never merged on a guess
    ],
)
def test_issue_51_same_offering_counts_once(second: tuple[str, str], body2: str, n: int) -> None:
    sec = FakeSec(
        filings=[Filing("424B5", "2026-06-01", acc(1)), Filing(second[0], second[1], acc(2))],
        docs={acc(1): COVER.format(n="5,000,000"), acc(2): body2},
    )
    iss = check(run(sec), "issuance")
    fin = [e for e in iss["evidence"] if "kind" not in e]
    assert len(fin) == n
    if n == 1:
        assert fin[0]["also"] == [acc(2)] and fin[0]["size"] == 5_000_000
        assert iss["status"] == "ok"
    else:
        assert iss["status"] == "flag"


def test_424b_covers_beyond_the_cap_are_counted_without_a_size() -> None:
    sec = FakeSec(
        filings=[Filing("424B5", "2026-06-01", acc(1)), Filing("424B2", "2026-06-01", acc(2))],
        docs={acc(1): COVER.format(n="5,000,000"), acc(2): COVER.format(n="5,000,000")},
    )
    res = run(sec, cfg=UNI.model_copy(update={"candidate_max_documents": 1}))
    fin = [e for e in check(res, "issuance")["evidence"] if "kind" not in e]
    assert len(fin) == 2 and {f["size"] for f in fin} == {5_000_000, "not parsed"}
    assert res["requests"] == 3  # submissions, companyfacts, one cover


# --------------------------------------------------------------------------- #49 / #50


def test_issue_49_own_closing_8k_isnt_an_acquisition_but_a_later_2_01_is() -> None:
    first = date(2026, 3, 27)
    sec = FakeSec(
        filings=[
            Filing("8-K", "2026-03-26", acc(1), "2.01,5.01,5.06"),  # own de-SPAC closing (5.06)
            Filing("8-K", "2026-03-27", acc(2), "2.01,5.01"),  # filed on the first session
            Filing("8-K", "2026-09-15", acc(3), "2.01,9.01"),  # a real acquisition later
        ]
    )
    acq = check(run(sec, first=first), "acquisitions")
    eights = [e["accession"] for e in acq["evidence"] if "Item 2.01" in (e.get("form") or "")]
    assert eights == [acc(3)]


def test_issue_50_retrieval_dated_events_never_change_the_result(rw_engine: Engine) -> None:
    """The checks read only EDGAR-dated SEC data: a retrieval-dated research "offering" event
    about the candidate changes nothing (the module never reads `events`)."""
    from aether.db.models import event_tickers, events

    before = run(FakeSec())
    seed_tickers(rw_engine, [("ACME", "pure_play")])
    with write_tx(rw_engine) as conn:
        eid = conn.execute(
            insert(events)
            .values(
                origin="web_search",
                source_domain="example.test",
                trust_tier="T2",
                url="https://example.test/acme-offering",
                url_hash=b"x" * 32,
                title="ACME prices a large offering",
                published_at="2026-10-30T00:00:00Z",
                created_at="2026-10-30T00:00:00Z",
                raw=json.dumps({"date_source": "retrieved"}),
            )
            .returning(events.c.id)
        ).scalar_one()
        conn.execute(insert(event_tickers).values(event_id=eid, symbol="ACME"))
    assert run(FakeSec()) == before
    tree = ast.parse((SRC / "universe" / "candidate_checks.py").read_text(encoding="utf-8"))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert "events" not in names and "event_filters" not in str(ast.dump(tree))


# --------------------------------------------------------------------------- runway / growth


def test_runway_cash_plus_short_term_over_ttm_burn_matches_hand_computation() -> None:
    sec = FakeSec(
        facts=healthy(
            us_gaap__CashAndCashEquivalentsAtCarryingValue=[fact(30_000_000, "2026-06-30")],
            us_gaap__ShortTermInvestments=[fact(6_000_000, "2026-06-30")],
            us_gaap__NetCashProvidedByUsedInOperatingActivities=[
                fy(-24_000_000, "2026-06-30", "2025-07-01"),
            ],
        )
    )
    rw = check(run(sec), "runway")
    # (30 + 6) / (24 / 4) x 3 = 18 months -> flag (under 24).
    assert rw["status"] == "flag" and rw["text"].startswith("18 months")
    assert rw["basis"] == "TTM" and rw["period_end"] == "2026-06-30"


def test_positive_operating_cash_flow_is_runway_ok() -> None:
    rw = check(run(FakeSec()), "runway")
    assert rw["status"] == "ok" and "not burning" in rw["text"]
    assert rw["accessions"] and rw["data_as_of"] == "2026-08-10"  # cites the cash and OCF facts


def test_stale_data_is_unknown_never_ok() -> None:
    old = healthy(
        us_gaap__CashAndCashEquivalentsAtCarryingValue=[fact(500_000_000, "2026-03-31")],
        us_gaap__NetCashProvidedByUsedInOperatingActivities=[
            fy(50_000_000, "2025-12-31", "2025-01-01", filed="2026-02-20")
        ],
    )
    res = run(FakeSec(facts=old))
    rw = check(res, "runway")
    # 2025-12-31 is 305 days before the review (> 200): stale, not "not burning".
    assert rw["status"] == "unknown" and "stale" in rw["text"]


def test_revenue_falls_back_to_period_not_ttm() -> None:
    def q(val: int, end: str, start: str) -> dict[str, Any]:
        return fact(val, end, start=start)

    facts = healthy(
        us_gaap__Revenues=[
            q(10_000_000, "2026-06-30", "2026-04-01"),
            q(10_200_000, "2025-06-30", "2025-04-01"),
        ],
        us_gaap__OperatingExpenses=[
            q(40_000_000, "2026-06-30", "2026-04-01"),
            q(28_000_000, "2025-06-30", "2025-04-01"),
        ],
    )
    g = check(run(FakeSec(facts=facts)), "revenue_vs_spend")
    # Revenue -2% while opex +43% (same quarter a year earlier) -> flag, labelled.
    assert g["status"] == "flag" and g["basis"] == "period, not TTM"
    assert "period, not TTM" in g["text"] and g["period_end"] == "2026-06-30"
    assert (
        g["text"].startswith("revenue -2%, operating expenses +43%") and "TTM" not in g["text"][:40]
    )


# --------------------------------------------------------------------------- FPI


def instance_xml(ns: str, facts: list[tuple[str, str, str, int]], extra: str = "") -> str:
    """facts: (element, context id, unit id, value). Contexts: d = 2026-01-01..06-30,
    dp = 2025-01-01..06-30, i = 2026-06-30, ip = 2025-06-30, seg = dimensional."""
    body = "".join(
        f'<{el} contextRef="{c}" unitRef="{u}" decimals="-3">{v}</{el}>' for el, c, u, v in facts
    )

    def ctx(cid: str, period: str, seg: str = "") -> str:
        return (
            f'<context id="{cid}"><entity><identifier scheme="http://www.sec.gov/CIK">{CIK}'
            f"</identifier>{seg}</entity><period>{period}</period></context>"
        )

    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<xbrl xmlns="http://www.xbrl.org/2003/instance" {ns} '
        'xmlns:iso4217="http://www.xbrl.org/2003/iso4217" '
        'xmlns:xbrldi="http://xbrl.org/2006/xbrldi" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        + ctx("d", "<startDate>2026-01-01</startDate><endDate>2026-06-30</endDate>")
        + ctx("dp", "<startDate>2025-01-01</startDate><endDate>2025-06-30</endDate>")
        + ctx("i", "<instant>2026-06-30</instant>")
        + ctx("ip", "<instant>2025-06-30</instant>")
        + ctx(
            "seg",
            "<instant>2026-06-30</instant>",
            '<segment><xbrldi:explicitMember dimension="x:Axis">x:M</xbrldi:explicitMember>'
            "</segment>",
        )
        + '<unit id="usd"><measure>iso4217:USD</measure></unit>'
        '<unit id="sh"><measure>xbrli:shares</measure></unit>' + body + extra + "</xbrl>"
    )


US_NS = 'xmlns:us-gaap="http://fasb.org/us-gaap/2026" xmlns:dei="http://xbrl.sec.gov/dei/2026"'
IFRS_NS = 'xmlns:ifrs-full="https://xbrl.ifrs.org/taxonomy/2025-03-27/ifrs-full"'

SIX_K = instance_xml(
    US_NS,
    [
        ("us-gaap:CashAndCashEquivalentsAtCarryingValue", "i", "usd", 312_780_000),
        ("us-gaap:CashAndCashEquivalentsAtCarryingValue", "seg", "usd", 1),  # dimensional: skip
        ("us-gaap:NetCashProvidedByUsedInOperatingActivities", "d", "usd", -30_630_000),
        ("us-gaap:NetCashProvidedByUsedInOperatingActivities", "dp", "usd", -27_374_000),
        ("us-gaap:CommonStockSharesOutstanding", "i", "sh", 304_224_024),
    ],
    '<us-gaap:Revenues contextRef="d" unitRef="usd" xsi:nil="true"/>',
)


def fpi_sec() -> FakeSec:
    """A 20-F filer whose companyfacts stops at Q1 (from the F-1); Q2 is only in a 6-K."""
    q1 = cfacts(
        us_gaap__CashAndCashEquivalentsAtCarryingValue=[
            fact(290_000_000, "2026-03-31", form="F-1", accn=acc(10), filed="2026-05-22")
        ],
        us_gaap__NetCashProvidedByUsedInOperatingActivities=[
            fact(
                -14_000_000,
                "2026-03-31",
                start="2026-01-01",
                form="F-1",
                accn=acc(10),
                filed="2026-05-22",
            )
        ],
    )
    return FakeSec(
        filings=[
            Filing("20-F", "2026-04-09", acc(5), xbrl=1),
            Filing("F-1", "2026-05-22", acc(10), doc="f1.htm", xbrl=1),
            Filing("6-K", "2026-08-05", acc(20), doc="acmf-20260630.htm", xbrl=1),
            Filing("6-K", "2026-08-28", acc(21), doc="pr.htm"),  # press release, no XBRL
            Filing("424B3", "2026-08-21", acc(22)),
        ],
        facts=q1,
        index={
            acc(20): [
                "acmf-20260630.htm",
                "acmf-20260630_cal.xml",
                "acmf-20260630_htm.xml",
                "FilingSummary.xml",
            ],
            acc(5): ["x.htm", "x_htm.xml"],
        },
        docs={acc(20): SIX_K, acc(10): NO_COVER, acc(5): instance_xml(US_NS, [])},
    )


def test_fpi_uses_the_6k_instance_period_not_older_companyfacts() -> None:
    sec = fpi_sec()
    res = run(sec)
    assert res["fpi"] is True
    assert [i["accession"] for i in res["instances"]] == [acc(20), acc(5)]
    rw = check(res, "runway")
    # Liquidity at 2026-06-30 from the 6-K; burn from the 181-day H1 scaled to a quarter:
    # 30.63M / (181 / 91.3125) = 15.45M; 312.78 / 15.45 x 3 = 60.7 months.
    assert rw["period_end"] == "2026-06-30" and rw["basis"] == "period, not TTM"
    assert rw["status"] == "ok" and rw["text"].startswith("61 months")
    assert acc(20) in rw["accessions"]
    iss = check(res, "issuance")
    assert "partial" in iss["text"] and "resale overhang: 1 424B3" in iss["text"]
    # F-1 already in companyfacts: its instance isn't fetched (skip, no request).
    assert f"index {acc(10)}" not in sec.calls


def test_ifrs_instance_maps_all_seven_concepts() -> None:
    xml = instance_xml(
        IFRS_NS,
        [
            ("ifrs-full:CashAndCashEquivalents", "i", "usd", 100),
            ("ifrs-full:CurrentInvestments", "i", "usd", 20),
            ("ifrs-full:CashFlowsFromUsedInOperatingActivities", "d", "usd", -10),
            ("ifrs-full:Revenue", "d", "usd", 50),
            ("ifrs-full:OperatingExpense", "d", "usd", 40),
            (
                "ifrs-full:CashFlowsUsedInObtainingControlOfSubsidiariesOrOtherBusinesses"
                "ClassifiedAsInvestingActivities",
                "d",
                "usd",
                5,
            ),
            ("ifrs-full:NumberOfSharesOutstanding", "i", "sh", 1_000_000),
        ],
    )
    cf = xbrl.instance_facts(xml, acc(30), "6-K", "2026-08-05")
    got = {f.concept for f in xbrl.fundamentals(cf)}
    from aether.score import fundamentals as fnd

    seven = {
        fnd.CASH[1],
        "ifrs-full:CurrentInvestments",
        fnd.OCF[1],
        "ifrs-full:Revenue",
        fnd.OPEX[2],
        fnd.ACQUISITIONS[1],
        fnd.COMMON[2],
    }
    assert seven <= got and all(c.startswith("ifrs-full:") for c in seven)
    facts = cc.to_facts(cf, TODAY)
    snap = fnd.compute(facts, "ACMF", TODAY, first_session=None, price=None)
    assert snap.liquidity == 120  # cash + current investments


def test_instance_parser_skips_dimensions_nil_and_unknown_units() -> None:
    cf = xbrl.instance_facts(SIX_K, acc(20), "6-K", "2026-08-05")
    cash = cf["facts"]["us-gaap"]["CashAndCashEquivalentsAtCarryingValue"]["units"]["USD"]
    assert [r["val"] for r in cash] == [312_780_000]
    assert "Revenues" not in cf["facts"]["us-gaap"]
    assert cash[0]["accn"] == acc(20) and cash[0]["form"] == "6-K"
    with pytest.raises(xbrl.InstanceError):
        xbrl.instance_facts("<html/>", acc(20), "6-K", "2026-08-05")
    with pytest.raises(xbrl.InstanceError, match="DTD"):
        xbrl.instance_facts('<!DOCTYPE x [<!ENTITY a "b">]><xbrl/>', acc(20), "6-K", "2026-08-05")


def test_fpi_detection_and_instance_name() -> None:
    def m(form: str, d: str) -> FilingMeta:
        return FilingMeta(acc(1), CIK, form, d, None, None, (), "a.htm", None, True)

    assert fpi.is_fpi([m("20-F", "2026-04-01"), m("6-K", "2026-08-01")])
    assert not fpi.is_fpi([m("10-K", "2026-03-01"), m("8-K", "2026-08-01")])
    assert fpi.is_fpi([m("6-K", "2026-08-01")])
    assert (
        fpi.instance_name({"directory": {"item": [{"name": "a_cal.xml"}, {"name": "a.xml"}]}})
        == "a.xml"
    )
    assert fpi.instance_name({"directory": {"item": [{"name": "../x_htm.xml"}]}}) is None


# --------------------------------------------------------------------------- failures / gate


def test_sec_failure_makes_every_check_unknown() -> None:
    class Down(FakeSec):
        def submissions(self, cik: str) -> dict[str, Any]:
            from aether.providers.prices import ProviderError

            raise ProviderError("SEC 503")

    res = run(Down())
    assert res["n_unknown"] == 4 and res["n_flags"] == 0
    assert res["errors"] and "submissions" in res["errors"][0]


def test_add_max_red_flags_gate() -> None:
    two = {
        "n_flags": 2,
        "checks": [
            {"label": "Repeated share issuance", "status": "flag"},
            {"label": "Cash runway under ~2 years", "status": "flag"},
        ],
    }
    action, note = gates.red_flag_gate("add", None, two, 1)
    assert (
        action == "watch"
        and "2 computed red flag(s)" in str(note)
        and "add_max_red_flags 1" in str(note)
    )
    assert gates.red_flag_gate("add", None, two, None) == ("add", None)  # null: no gate
    assert gates.red_flag_gate("add", None, two, 2) == ("add", None)
    assert gates.red_flag_gate("watch", "x", two, 0) == ("watch", "x")


def test_checks_make_no_llm_call() -> None:
    tree = ast.parse((SRC / "universe" / "candidate_checks.py").read_text(encoding="utf-8"))
    mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert not [m for m in mods if m and m.startswith("aether.llm")]


def test_line_text() -> None:
    res = run(
        FakeSec(
            filings=[
                Filing("8-K", "2026-02-10", acc(1), "3.02"),
                Filing("8-K", "2026-07-15", acc(2), "3.02"),
            ]
        )
    )
    assert cc.line(res) == "red flags: 1 (repeated share issuance), unknown: 0"
    assert cc.short(res) == "red flags: 1, unknown: 0"


# --------------------------------------------------------------------------- ingest (active FPI)


def test_active_fpi_gets_fundamentals_rows_from_a_6k_instance(rw_engine: Engine) -> None:
    seed_tickers(rw_engine, [("ACMF", "pure_play")])
    from sqlalchemy import update

    from aether.db.models import tickers

    with write_tx(rw_engine) as conn:
        conn.execute(update(tickers).where(tickers.c.symbol == "ACMF").values(cik=CIK))
    sec = fpi_sec()
    ingest_edgar(rw_engine, sec, load_rubric(CONFIG_DIR))  # type: ignore[arg-type]
    with rw_engine.connect() as conn:
        rows = conn.execute(
            select(
                fundamentals_q.c.concept, fundamentals_q.c.period_end, fundamentals_q.c.form
            ).where(fundamentals_q.c.symbol == "ACMF", fundamentals_q.c.accession == acc(20))
        ).all()
        n_all = conn.execute(
            select(func.count())
            .select_from(fundamentals_q)
            .where(fundamentals_q.c.symbol == "ACMF")
        ).scalar_one()
    got = {(r.concept, r.period_end) for r in rows}
    assert ("us-gaap:CashAndCashEquivalentsAtCarryingValue", "2026-06-30") in got
    assert ("us-gaap:NetCashProvidedByUsedInOperatingActivities", "2026-06-30") in got
    assert {r.form for r in rows} == {"6-K"} and n_all > len(rows)  # plus the F-1 facts
