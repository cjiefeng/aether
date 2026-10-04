"""Deterministic rules (rubric.yaml) and the EDGAR client's guard rails."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx
from pydantic import ValidationError

from aether.classify.rules import classify_filing
from aether.config import load_rubric
from aether.edgar.form4 import InsiderTxn
from aether.edgar.submissions import FilingMeta
from aether.providers.edgar import EdgarClient, cik10, document_url, raw_doc_name
from aether.providers.prices import ProviderError
from tests.conftest import CONFIG_DIR

RUBRIC = load_rubric(CONFIG_DIR)
UA = "Aether test suite tests@example.test"


def meta(form: str, items: tuple[str, ...] = (), desc: str | None = None) -> FilingMeta:
    return FilingMeta(
        accession="0000000001-26-000001",
        cik="0000000001",
        form=form,
        filed_at="2026-01-02",
        accepted_at="2026-01-02T21:05:00Z",
        report_date=None,
        items=items,
        primary_doc="acme.htm",
        primary_doc_description=desc,
        is_xbrl=False,
    )


def sale(insider: str = "Doe Jane", plan: bool = False, code: str = "S") -> InsiderTxn:
    return InsiderTxn(
        0, "9", insider, None, "Common", "2026-01-02", code, "D", 1000, 1.0, plan, False
    )


@pytest.mark.parametrize(
    ("form", "rule", "materiality"),
    [
        ("S-3ASR", "edgar_shelf_registration", 3),
        ("S-1", "edgar_registration_statement", 3),
        ("424B5", "edgar_primary_prospectus", 4),
        ("424B4", "edgar_primary_prospectus", 4),
        ("424B3", "edgar_resale_prospectus", 2),
        ("NT 10-Q", "edgar_late_filing_notice", 3),
    ],
)
def test_form_rules(form: str, rule: str, materiality: int) -> None:
    hit = classify_filing("ACME", meta(form), RUBRIC)
    assert hit is not None
    assert (hit.rule_id, hit.materiality, hit.cls, hit.direction) == (rule, materiality, "RISK", -1)
    assert hit.title.startswith(f"ACME {form}")


def test_amendments_and_unruled_forms_are_left_for_the_classifier() -> None:
    for form in ("S-1/A", "10-Q", "S-4", "SCHEDULE 13G", "3"):
        assert classify_filing("ACME", meta(form), RUBRIC) is None
    assert classify_filing("ACME", meta("8-K", ("1.01", "9.01")), RUBRIC) is None


def test_8k_items() -> None:
    e = classify_filing("ACME", meta("8-K", ("2.02", "9.01")), RUBRIC)
    assert e is not None and (e.cls, e.category, e.direction) == ("SIGNAL", "earnings_release", 0)
    equity = classify_filing("ACME", meta("8-K", ("1.01", "3.02")), RUBRIC)
    assert equity is not None and (equity.category, equity.rule_id) == ("dilution", "edgar_8k_3_02")
    titled = classify_filing("ACME", meta("424B5", desc="424B5"), RUBRIC)
    assert titled is not None and titled.title == "ACME 424B5"
    both = classify_filing("ACME", meta("8-K", ("3.01", "5.02")), RUBRIC)
    assert both is not None and both.category == "delisting_or_compliance"
    assert "edgar_8k_5_02" in both.rationale  # the weaker match is recorded


def test_form4_rules() -> None:
    hit = classify_filing("ACME", meta("4"), RUBRIC, txns=[sale(), sale(plan=True)])
    assert hit is not None and hit.category == "insider_selling" and hit.materiality == 2
    plan_only = classify_filing("ACME", meta("4"), RUBRIC, txns=[sale(plan=True)])
    assert plan_only is not None and plan_only.materiality == 1 and "(10b5-1)" in plan_only.title
    assert classify_filing("ACME", meta("4"), RUBRIC, txns=[sale(code="F")]) is None
    assert classify_filing("ACME", meta("4"), RUBRIC, txns=[]) is None


def test_going_concern_rule() -> None:
    hit = classify_filing("ACME", meta("10-K"), RUBRIC, going_concern_excerpt="…substantial doubt…")
    assert hit is not None and (hit.category, hit.materiality) == ("going_concern", 5)
    assert hit.evidence_quote == "…substantial doubt…"


def test_rubric_forbids_unknown_keys_and_duplicate_forms(tmp_path: Path) -> None:
    text = (CONFIG_DIR / "rubric.yaml").read_text()
    (tmp_path / "rubric.yaml").write_text(text + "\nopinion: speculative\n")
    with pytest.raises(ValidationError):
        load_rubric(tmp_path)
    dup = text.replace("forms: [S-1, S-1MEF, F-1]", "forms: [S-1, S-3]")
    (tmp_path / "rubric.yaml").write_text(dup)
    with pytest.raises(ValidationError):
        load_rubric(tmp_path)


# --------------------------------------------------------------------------- client


def test_client_requires_contact_user_agent() -> None:
    for bad in ("", "aether", "python-httpx/0.27"):
        with pytest.raises(ValueError):
            EdgarClient(bad)


def test_client_sends_ua_only_to_sec_and_refuses_other_hosts() -> None:
    c = EdgarClient(UA, min_interval_s=0, sleep=lambda _s: None)
    with respx.mock(assert_all_called=False) as router:
        route = router.get("https://data.sec.gov/submissions/CIK0000000001.json").mock(
            return_value=httpx.Response(200, json={"filings": {}})
        )
        other = router.get("https://example.test/x").mock(return_value=httpx.Response(200))
        c.submissions("1")
        assert route.calls.last.request.headers["User-Agent"] == UA
        with pytest.raises(ProviderError):
            c.get("https://example.test/x")
        with pytest.raises(ProviderError):
            c.get("http://www.sec.gov/insecure")
        assert not other.called


def test_client_retries_429_then_succeeds_and_throttles() -> None:
    sleeps: list[float] = []
    now = [0.0]
    c = EdgarClient(UA, min_interval_s=0.2, sleep=sleeps.append, clock=lambda: now[0])
    with respx.mock() as router:
        router.get("https://data.sec.gov/submissions/CIK0000000001.json").mock(
            side_effect=[httpx.Response(429), httpx.Response(200, json={"ok": 1})]
        )
        assert c.submissions("1") == {"ok": 1}
    assert c.requests_made == 2
    assert 2.0 in sleeps  # backoff after the 429
    assert any(0 < s <= 0.2 for s in sleeps)  # throttle between the two requests


def test_client_gives_up_on_permanent_errors() -> None:
    c = EdgarClient(UA, min_interval_s=0, sleep=lambda _s: None)
    with respx.mock() as router:
        router.get("https://data.sec.gov/submissions/CIK0000000001.json").mock(
            return_value=httpx.Response(404)
        )
        with pytest.raises(ProviderError):
            c.submissions("1")
    assert c.requests_made == 1


def test_url_builders_validate_inputs() -> None:
    assert cik10("1824920") == "0001824920"
    assert raw_doc_name("xslF345X06/form4.xml") == "form4.xml"
    assert document_url("1", "0000000001-26-000001", "xslF345X06/f.xml", raw=True).endswith(
        "/1/000000000126000001/f.xml"
    )
    for bad in ("../x.htm", "a/b/c.htm", "/etc/passwd"):
        with pytest.raises(ValueError):
            raw_doc_name(bad)
    with pytest.raises(ValueError):
        cik10("12a")
    with pytest.raises(ValueError):
        document_url("1", "1-2-3", "x.htm")
