"""USD/SGD (reporting only) and the options snapshot (research only), on recorded real data:
the ECB reference-rate XML and one yfinance IONQ chain, both recorded 2026-10-04/05."""

from __future__ import annotations

import gzip
import json
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import Engine, select

from aether.config import load_options_config
from aether.db.models import fx_rates, options_snapshots
from aether.ingest.fx import ingest_fx
from aether.options.job import snapshot_options
from aether.options.snapshot import choose_expiries, compute
from aether.providers.fx import ECB_URL, EcbFx, FallbackFx, FxRate, parse_ecb
from aether.providers.options import YFinanceOptions, parse_chain
from aether.providers.prices import ProviderError
from tests.conftest import CONFIG_DIR, REPO
from tests.holdings_data import seed_universe

FIX = REPO / "tests" / "fixtures"
ECB_XML = (FIX / "fx" / "ecb_eurofxref_hist_90d_2026-10-05.xml").read_text()
IONQ = json.loads(gzip.decompress((FIX / "options" / "IONQ_2026-10-04.json.gz").read_bytes()))
OPT = load_options_config(CONFIG_DIR)
D0 = date(2026, 10, 4)


# --------------------------------------------------------------------------- FX


def test_parse_ecb_cross_rate() -> None:
    rates = parse_ecb(ECB_XML)
    assert len(rates) == 10 and rates[-1].d == date(2026, 10, 2)
    assert all(1.0 < r.rate < 2.0 and r.provider == "ecb" for r in rates)  # SGD per USD


def test_ecb_rejects_dtd() -> None:
    with pytest.raises(ProviderError):
        parse_ecb('<!DOCTYPE x [<!ENTITY a "b">]><x/>')


class Broken:
    name = "yfinance"

    def fetch(self, start: date, end: date) -> list[FxRate]:
        raise ProviderError("down")


@respx.mock
def test_fx_falls_back_to_ecb_and_stores(rw_engine: Engine) -> None:
    respx.get(ECB_URL).mock(return_value=httpx.Response(200, text=ECB_XML))
    with httpx.Client() as c:
        res = ingest_fx(rw_engine, FallbackFx(Broken(), EcbFx(c)), today=date(2026, 10, 5))
    assert res.provider == "ecb" and res.rows_written == 10
    with rw_engine.connect() as conn:
        assert conn.execute(select(fx_rates.c.provider).distinct()).scalars().all() == ["ecb"]


# --------------------------------------------------------------------------- options


def test_choose_expiries_brackets_each_horizon() -> None:
    listed = [
        date(2026, 10, 9),
        date(2026, 10, 16),
        date(2026, 10, 30),
        date(2026, 11, 6),
        date(2026, 11, 20),
        date(2026, 12, 18),
        date(2027, 1, 15),
        date(2027, 6, 18),
    ]
    got = choose_expiries(listed, D0, OPT)
    assert got[0] == date(2026, 10, 9)
    assert date(2026, 10, 30) in got and date(2026, 11, 6) in got  # around 30 days
    assert date(2027, 6, 18) not in got  # beyond max_days


def test_metrics_from_recorded_chain() -> None:
    m, q = compute(parse_chain(IONQ), D0, OPT)
    assert not q["thin"] and q["reasons"] == {}
    for t in ("30", "60", "90"):
        assert 0.05 < m["term"][t] < 3.0
    assert m["atm_iv_30"] == m["term"]["30"]
    assert m["put_call_volume"] > 0 and m["total_open_interest"] > 0
    # Interpolation stays between the bracketing expiries' ATM IVs.
    pts = [(e["dte"], e["atm_iv"]) for e in m["expiries"] if e["atm_iv"]]
    lo = max(v for d, v in pts if d <= 30 and v)
    hi = next(v for d, v in pts if d >= 30)
    assert min(lo, hi) - 1e-9 <= m["term"]["30"] <= max(lo, hi) + 1e-9


def _thin(raw: dict[str, Any]) -> dict[str, Any]:
    out = json.loads(json.dumps(raw))
    for e in out["expiries"]:
        for side in ("calls", "puts"):
            for c in e[side]:
                c["openInterest"] = 0
    return out


def test_thin_chain_is_null_with_reason() -> None:
    m, q = compute(parse_chain(_thin(IONQ)), D0, OPT)
    assert q["thin"] is True
    assert m["term"] == {"30": None, "60": None, "90": None}
    assert "thin chain" in q["reasons"]["atm_iv_30"]


def test_no_extrapolation_beyond_listed_expiries() -> None:
    short = {**IONQ, "expiries": IONQ["expiries"][:2]}
    m, q = compute(parse_chain(short), D0, OPT)
    assert m["term"]["90"] is None and "not extrapolated" in q["reasons"]["atm_iv_90"]


def test_snapshot_job_stores_rows_and_nulls_thin(rw_engine: Engine) -> None:
    seed_universe(rw_engine)
    raw_by = {"QTUM": IONQ, "ACME": _thin(IONQ)}

    def fetch_raw(symbol: str, choose: Any) -> dict[str, Any]:
        if symbol not in raw_by:
            raise RuntimeError("no chain")
        return {**raw_by[symbol], "symbol": symbol}

    res = snapshot_options(rw_engine, YFinanceOptions(fetch_raw_fn=fetch_raw), OPT, d=D0)
    assert res.rows_written == 2 and res.warning and "DEMO" in res.warning
    with rw_engine.connect() as conn:
        rows = {r.symbol: r for r in conn.execute(select(options_snapshots))}
    assert json.loads(rows["QTUM"].metrics)["atm_iv_30"] is not None
    acme = json.loads(rows["ACME"].metrics), json.loads(rows["ACME"].quality)
    assert acme[0]["atm_iv_30"] is None and acme[1]["thin"] is True
    assert rows["QTUM"].provider == "yfinance"


def test_options_never_reach_targets() -> None:
    src = (REPO / "src" / "aether" / "portfolio").glob("*.py")
    for p in src:
        text = Path(p).read_text()
        assert "options_snapshots" not in text and "aether.options" not in text, p
