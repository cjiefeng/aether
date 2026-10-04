"""Ticker page SEC sections, dilution JSON, Overview risk cards and the refresh-edgar command,
rendered from recorded SEC data via the ro engine."""

from __future__ import annotations

import re

import httpx
import respx
from sqlalchemy import Engine
from starlette.testclient import TestClient

from aether.config import load_rubric
from aether.ingest.edgar import ingest_edgar
from aether.security.csrf import COOKIE_NAME
from tests.cassettes import mount_cassette
from tests.conftest import CONFIG_DIR
from tests.test_ingest_edgar import (
    CASSETTES,
    seeded,  # noqa: F401 (fixture)
)
from tests.test_ingest_edgar import client as edgar_client


def _ingest(engine: Engine) -> None:
    with respx.mock(assert_all_called=False) as router:
        for name in CASSETTES:
            mount_cassette(router, name)
        router.route(host__regex=r"^(www|data)\.sec\.gov$").mock(return_value=httpx.Response(404))
        ingest_edgar(engine, edgar_client(), load_rubric(CONFIG_DIR))


def test_ticker_page_sec_sections(seeded: Engine, client: TestClient) -> None:  # noqa: F811
    _ingest(seeded)
    r = client.get("/t/QNT")
    assert r.status_code == 200
    for heading in (
        "Open risk flags",
        "Lock-ups",
        "Earnings dates",
        "Shares outstanding",
        "Capital structure",
        "Insider transactions",
        "Filings",
    ):
        assert f"<h2>{heading}" in r.text
    assert "2026-11-30" in r.text and "180 days" in r.text
    assert 'rel="noopener noreferrer nofollow"' in r.text  # EDGAR links via extlink
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", r.text)
    assert "style=" not in r.text
    rgti = client.get("/t/RGTI").text
    assert "10b5-1" in rgti and "Sale" in rgti and "16.89" in rgti


def test_non_pure_play_has_no_sec_sections(seeded: Engine, client: TestClient) -> None:  # noqa: F811
    from tests.conftest import seed_tickers

    seed_tickers(seeded, [("QQQ", "benchmark")])
    r = client.get("/t/QQQ")
    assert r.status_code == 200 and "Insider transactions" not in r.text


def test_dilution_api(seeded: Engine, client: TestClient) -> None:  # noqa: F811
    _ingest(seeded)
    body = client.get("/api/dilution/QNT").json()
    assert body["symbol"] == "QNT"
    for s in body["series"]:
        assert all(isinstance(v, int) for _d, v in s["data"])
    assert client.get("/api/dilution/NOPE").status_code == 404


def test_overview_risk_cards(seeded: Engine, client: TestClient) -> None:  # noqa: F811
    _ingest(seeded)
    r = client.get("/")
    assert "<h2>Open risk flags</h2>" in r.text
    assert "<h2>RISK filings, last 30 days</h2>" in r.text


def test_refresh_edgar_requires_csrf(seeded: Engine, client: TestClient) -> None:  # noqa: F811
    assert client.post("/commands/refresh-edgar").status_code == 403
    client.get("/")
    token = client.cookies.get(COOKIE_NAME)
    r = client.post("/commands/refresh-edgar", headers={"X-CSRF-Token": token or ""})
    assert r.status_code == 202
