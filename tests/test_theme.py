"""M9 acceptance: the QTUM theme decomposition recovers known betas from a synthetic factor series
within ±0.05 (spec §6.1)."""

from __future__ import annotations

import json
from datetime import date

import numpy as np
import pytest
from sqlalchemy import Engine, select

from aether import nyse
from aether.db.models import theme_decomposition
from aether.score.theme import basket_returns, decompose, run_theme
from tests.test_reactions import closes_from, seed

TRUE = {"semis": 0.40, "tech": 0.30, "quantum": 0.20}


def factors(n: int = 300, seed_: int = 7) -> dict[str, np.ndarray]:
    rng = np.random.default_rng(seed_)
    semis = rng.normal(0.0006, 0.015, n)
    tech = 0.5 * semis + rng.normal(0.0004, 0.01, n)  # correlated, as SOXX and QQQ are
    members = {s: rng.normal(0.001, 0.04, n) for s in ("ACME", "DEMO", "EXMP", "FAKE")}
    members["FAKE"][:200] = np.nan  # lists late: joins the basket after 60 sessions
    basket, _ = basket_returns(members, 60)
    qtum = (
        0.0001
        + TRUE["semis"] * semis
        + TRUE["tech"] * tech
        + TRUE["quantum"] * np.nan_to_num(basket)
        + rng.normal(0, 0.002, n)
    )
    return {"semis": semis, "tech": tech, "qtum": qtum, **members}


def test_recovers_known_betas_within_tolerance() -> None:
    f = factors()
    days = [f"d{i:04d}" for i in range(len(f["qtum"]))]
    members = {s: f[s] for s in ("ACME", "DEMO", "EXMP", "FAKE")}
    d = decompose(days, f["qtum"], f["semis"], f["tech"], members)
    assert d is not None
    for k, v in TRUE.items():
        assert d.betas[k]["value"] == pytest.approx(v, abs=0.05)
    assert d.partial_r2 is not None and 0 < d.partial_r2 <= 1
    assert d.r2 is not None and d.r2 > 0.9
    # FAKE has 99 returns by the last day: it's in the basket; attribution parts add up.
    assert d.members == ["ACME", "DEMO", "EXMP", "FAKE"]
    a = d.attribution["90"]
    total = a["semis"] + a["tech"] + a["quantum"] + a["alpha"] + a["residual"]
    assert total == pytest.approx(a["actual"], abs=1e-9)
    assert a["sessions"] == 90


def test_basket_membership_waits_for_min_sessions() -> None:
    r = {
        "ACME": np.full(100, 0.01),
        "DEMO": np.concatenate([np.full(50, np.nan), np.full(50, 0.03)]),
    }
    b, who = basket_returns(r, 60)
    assert who[99] == ["ACME"] and b[99] == pytest.approx(0.01)
    assert np.isnan(basket_returns({"ACME": np.full(10, 0.01)}, 60)[0]).all()


def test_too_little_overlap_reports_nothing() -> None:
    f = factors(n=50)
    days = [f"d{i:04d}" for i in range(50)]
    assert decompose(days, f["qtum"], f["semis"], f["tech"], {"ACME": f["ACME"]}) is None


def test_job_writes_one_row_on_qtum_calendar(rw_engine: Engine) -> None:
    f = factors()
    days = [d.isoformat() for d in nyse.sessions(date(2025, 1, 1), date(2026, 6, 30))][-300:]
    closes = {
        "QTUM": closes_from(f["qtum"]),
        "SOXX": closes_from(f["semis"]),
        "QQQ": closes_from(f["tech"]),
        **{s: closes_from(np.nan_to_num(f[s])) for s in ("ACME", "DEMO", "EXMP")},
    }
    seed(rw_engine, days, closes)
    assert run_theme(rw_engine).rows_written == 1
    with rw_engine.connect() as conn:
        row = conn.execute(select(theme_decomposition)).one()
    assert row.as_of == days[-1] and row.n_sessions == 120
    assert json.loads(row.basket_members) == ["ACME", "DEMO", "EXMP"]
    assert set(json.loads(row.attribution)) == {"30", "90"}
