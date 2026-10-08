"""M14 thesis checks (spec §6.10). Synthetic names, filings and figures only (ACME,
example.test)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy import Engine, func, insert, select

from aether.config import ThesisConfig, load_thesis_config, load_universe_config
from aether.db.dialect import upsert
from aether.db.engine import make_ro_engine, write_tx
from aether.db.models import metadata, qtum_holdings, tickers
from aether.portfolio import thesis
from aether.portfolio.thesis import Name, NameInputs
from tests.conftest import CONFIG_DIR
from tests.fundamentals_data import REV, add_facts, fact
from tests.holdings_data import add_filing

CFG = load_thesis_config(CONFIG_DIR)
UNI = load_universe_config(CONFIG_DIR)
AS_OF = date(2026, 10, 9)

NAMES = [
    Name("ACMA", "pure_play", "trapped_ion", None),
    Name("ACMB", "pure_play", "trapped_ion", None),
    Name("ACMC", "pure_play", "superconducting", None),
    Name("ACMD", "pure_play", "neutral_atom", None),
    Name("ADJA", "adjacent", None, "pqc_cyber"),
]


def _status(flags: list[dict], key: str) -> str:
    return next(f["status"] for f in flags if f["key"] == key)


def _inputs(**kw: object) -> NameInputs:
    base: dict[str, object] = {
        "fd_yoy": 0.02,
        "runway_months": 60.0,
        "revenue_growth": 0.30,
        "opex_growth": 0.10,
        "acquisitions_ttm": Decimal(0),
        "liquidity": Decimal(1_000_000_000),
    }
    base.update(kw)
    return NameInputs("ACME", **base)  # type: ignore[arg-type]


def _fin(n: int) -> list[dict]:
    return [
        {
            "accession": f"x-{i}",
            "form": "424B5",
            "filed_at": "2026-05-01",
            "url": "https://example.test/f",
        }
        for i in range(n)
    ]


# --------------------------------------------------------------------------- 2. concentration


def test_one_modality_over_half_raises_the_concentration_flag() -> None:
    # Non-QTUM sleeve 0.55: trapped ion 0.30 (54.5%) > 50%.
    w = {"QTUM": 0.45, "ACMA": 0.15, "ACMB": 0.15, "ACMC": 0.13, "ADJA": 0.12}
    b = thesis.breakdown(w, NAMES, CFG)
    kinds = {f["flag"] for f in b["flags"]}
    assert "modality_concentration" in kinds
    top = b["rows"][0]
    assert top["category"] == "trapped_ion" and abs(top["pct_ex_qtum"] - 0.30 / 0.55) < 1e-9
    assert abs(top["pct_whole"] - 0.30) < 1e-9
    # Two modalities held (trapped ion, superconducting) < 3.
    assert "few_modalities" in kinds
    # Name share: 0.15 / 0.55 = 27% > 25%.
    assert any(f["flag"] == "name_concentration" and "ACMA" in f["text"] for f in b["flags"])
    sector = next(r for r in b["rows"] if r["kind"] == "sector")
    assert sector["category"] == "pqc_cyber" and sector["symbols"] == ["ADJA"]


def test_balanced_sleeve_raises_no_flag() -> None:
    w = {"QTUM": 0.45, "ACMA": 0.11, "ACMC": 0.11, "ACMD": 0.11, "ADJA": 0.11, "ACMB": 0.11}
    b = thesis.breakdown(w, NAMES, CFG)
    # trapped ion 0.22 / 0.55 = 40% (two names); every name 20%; three modalities.
    assert b["flags"] == []
    assert b["modalities_held"] == ["neutral_atom", "superconducting", "trapped_ion"]


# --------------------------------------------------------------------------- 3. red flags


def test_issuance_fires_at_two_financings_not_one() -> None:
    assert _status(thesis.red_flags(_inputs(financings=_fin(2)), CFG), "issuance") == "flag"
    assert _status(thesis.red_flags(_inputs(financings=_fin(1)), CFG), "issuance") == "ok"
    # Or FD shares up more than 20% YoY.
    assert _status(thesis.red_flags(_inputs(fd_yoy=0.21), CFG), "issuance") == "flag"
    assert _status(thesis.red_flags(_inputs(fd_yoy=0.19), CFG), "issuance") == "ok"
    # Under the financing bar with no FD figure: unknown, not a pass.
    x = _inputs(financings=_fin(1), fd_yoy=None, fd_yoy_reason="listed < 1 year")
    assert _status(thesis.red_flags(x, CFG), "issuance") == "unknown"


def test_runway_fires_at_23_not_25_months() -> None:
    assert _status(thesis.red_flags(_inputs(runway_months=23.0), CFG), "runway") == "flag"
    assert _status(thesis.red_flags(_inputs(runway_months=25.0), CFG), "runway") == "ok"
    x = _inputs(runway_months=None, not_burning=True)
    assert _status(thesis.red_flags(x, CFG), "runway") == "ok"
    x = _inputs(runway_months=None, runway_reason="cash not tagged")
    assert _status(thesis.red_flags(x, CFG), "runway") == "unknown"


def test_flat_revenue_with_rising_spend() -> None:
    x = _inputs(revenue_growth=0.04, opex_growth=0.30)
    assert _status(thesis.red_flags(x, CFG), "revenue_vs_spend") == "flag"
    x = _inputs(revenue_growth=0.06, opex_growth=0.30)
    assert _status(thesis.red_flags(x, CFG), "revenue_vs_spend") == "ok"
    x = _inputs(revenue_growth=0.04, opex_growth=0.20)
    assert _status(thesis.red_flags(x, CFG), "revenue_vs_spend") == "ok"
    x = _inputs(opex_growth=None, growth_reason="operating expenses: not tagged")
    assert _status(thesis.red_flags(x, CFG), "revenue_vs_spend") == "unknown"


def test_acquisitions_fire_at_26_not_24_percent_of_liquidity() -> None:
    liq = Decimal(100_000_000)
    x = _inputs(acquisitions_ttm=Decimal(26_000_000), liquidity=liq)
    assert _status(thesis.red_flags(x, CFG), "acquisitions") == "flag"
    x = _inputs(acquisitions_ttm=Decimal(24_000_000), liquidity=liq)
    assert _status(thesis.red_flags(x, CFG), "acquisitions") == "ok"
    x = _inputs(acquisitions_ttm=None, acquisitions_reason="not tagged")
    assert _status(thesis.red_flags(x, CFG), "acquisitions") == "unknown"


# --------------------------------------------------------------------------- 4. winner signals


def _winner_inputs(**kw: object) -> NameInputs:
    ttms = [("2026-06-30", 150.0), ("2026-03-31", 140.0), ("2025-12-31", 130.0)]
    ttms += [("2025-09-30", 120.0), ("2025-06-30", 110.0)]
    base: dict[str, object] = {
        "error_correction": [
            {"catalyst_id": 1, "title": "ACME logical qubits", "resolved_at": "x"}
        ],
        "revenue_ttms": ttms,
        "financings": [],
        "fd_yoy": 0.03,
    }
    base.update(kw)
    return _inputs(**base)


def test_winner_signals_need_all_three() -> None:
    assert thesis.winner_signals(_winner_inputs(), CFG)["present"] is True
    assert thesis.winner_signals(_winner_inputs(error_correction=[]), CFG)["present"] is False
    flat = [("2026-06-30", 150.0), ("2026-03-31", 150.0), ("2025-12-31", 130.0)]
    flat += [("2025-09-30", 120.0), ("2025-06-30", 110.0)]
    assert thesis.winner_signals(_winner_inputs(revenue_ttms=flat), CFG)["present"] is False
    short = _winner_inputs(revenue_ttms=flat[:4])
    assert thesis.winner_signals(short, CFG)["present"] is False
    assert thesis.winner_signals(_winner_inputs(financings=_fin(1)), CFG)["present"] is False
    assert thesis.winner_signals(_winner_inputs(fd_yoy=0.06), CFG)["present"] is False


# --------------------------------------------------------------------------- 5/6. ETF and gaps


def test_qtum_hyperscaler_weight_over_ten_percent_is_flagged() -> None:
    q = {"MSFT": 3.0, "AMZN": 3.0, "GOOGL": 3.0, "ORCL": 2.0, "ACMA": 5.0}  # 11%
    h = thesis.hyperscaler_check(q, "2026-10-06", UNI.adjacent.excluded_symbols, CFG)  # type: ignore[union-attr]
    assert h["flag"] is True and abs(h["weight"] - 0.11) < 1e-9
    q = {"MSFT": 1.48, "AMZN": 1.18, "BABA": 1.07, "GOOGL": 1.09, "ORCL": 0.79}  # ~5.6%
    h = thesis.hyperscaler_check(q, "2026-10-06", UNI.adjacent.excluded_symbols, CFG)  # type: ignore[union-attr]
    assert h["flag"] is False and abs(h["weight"] - 0.0561) < 1e-9
    assert thesis.hyperscaler_check({}, None, ("MSFT",), CFG)["weight"] is None


def test_gaps_and_fills_gap() -> None:
    g = thesis.gaps(NAMES)
    assert g["modalities"] == ["photonic", "annealing", "spin_silicon"]
    assert "pqc_cyber" not in g["sectors"] and "test_measurement" in g["sectors"]
    assert thesis.fills_gap("photonic", NAMES) and thesis.fills_gap("cryogenics_gases", NAMES)
    assert not thesis.fills_gap("trapped_ion", NAMES) and not thesis.fills_gap(None, NAMES)


# --------------------------------------------------------------------------- DB


def _seed(engine: Engine) -> None:
    with write_tx(engine) as conn:
        upsert(
            conn,
            tickers,
            [
                {"symbol": "QTUM", "type": "etf", "active": 1},
                {"symbol": "ACMA", "type": "pure_play", "active": 1, "modality": "trapped_ion"},
                {"symbol": "ADJA", "type": "adjacent", "active": 1, "sector": "pqc_cyber"},
            ],
            key_cols=["symbol"],
        )
        conn.execute(
            insert(qtum_holdings),
            [
                {
                    "snapshot_date": "2026-10-06",
                    "holding_symbol": s,
                    "name": s,
                    "cusip": "000000000",
                    "weight": w,
                    "shares": 1,
                    "fetched_at": "2026-10-06T00:00:00Z",
                }
                for s, w in (("MSFT", 6.0), ("AMZN", 5.0), ("ACMA", 2.0))
            ],
        )


def test_financings_count_primary_supplements_and_item_302_only(rw_engine: Engine) -> None:
    _seed(rw_engine)
    add_filing(rw_engine, "ACMA", "424B5", "2026-03-01")
    add_filing(rw_engine, "ACMA", "8-K", "2026-04-01", items=["3.02"])
    add_filing(rw_engine, "ACMA", "424B3", "2026-05-01")  # resale prospectus: not a financing
    add_filing(rw_engine, "ACMA", "8-K", "2026-06-01", items=["7.01"])
    add_filing(rw_engine, "ACMA", "424B5", "2025-09-01")  # outside the 12 months
    with rw_engine.connect() as conn:
        fin = thesis.financings_for(conn, "ACMA", AS_OF, CFG.financing_lookback_days)
    assert [f["form"] for f in fin] == ["424B5", "8-K Item 3.02"]


def test_name_inputs_read_xbrl_growth(rw_engine: Engine) -> None:
    _seed(rw_engine)
    opex = "us-gaap:OperatingExpenses"
    add_facts(
        rw_engine,
        [
            fact("ACMA", REV, "2024-12-31", 100, days=365, form="10-K"),
            fact("ACMA", REV, "2025-12-31", 104, days=365, form="10-K"),
            fact("ACMA", opex, "2024-12-31", 100, days=365, form="10-K"),
            fact("ACMA", opex, "2025-12-31", 130, days=365, form="10-K"),
        ],
    )
    with rw_engine.connect() as conn:
        x = thesis.name_inputs(conn, "ACMA", AS_OF, CFG)
    assert abs((x.revenue_growth or 0) - 0.04) < 1e-9 and abs((x.opex_growth or 0) - 0.30) < 1e-9
    assert _status(thesis.red_flags(x, CFG), "revenue_vs_spend") == "flag"


def _counts(engine: Engine) -> dict[str, int]:
    with engine.connect() as conn:
        return {
            t: int(conn.execute(select(func.count()).select_from(metadata.tables[t])).scalar_one())
            for t in sorted(metadata.tables)
        }


def test_thesis_report_is_read_only(rw_engine: Engine, migrated_db: Path) -> None:
    """No thesis check changes a weight or writes a trade: the report runs on a read-only
    connection and no table changes."""
    _seed(rw_engine)
    add_filing(rw_engine, "ACMA", "424B5", "2026-03-01")
    before = _counts(rw_engine)
    ro = make_ro_engine(migrated_db)
    try:
        rep = thesis.thesis_report(
            ro,
            AS_OF,
            CFG,
            UNI,
            targets={"medium": {"QTUM": 0.45, "ACMA": 0.40, "ADJA": 0.15}},
        )
    finally:
        ro.dispose()
    assert _counts(rw_engine) == before
    assert rep["cap"] == {"used": 2, "max": 9}
    assert rep["hyperscaler"]["flag"] is True  # 11%
    assert "few_modalities" in {f["flag"] for f in rep["targets"]["medium"]["flags"]}
    assert set(rep["per_name"]) == {"ACMA", "ADJA"}
    assert rep["per_name"]["ADJA"]["winner"] is None
    lines = thesis.summary_lines(rep, "medium")
    assert lines[0] == "Names: 2 of 9 used (QTUM excluded)."
    assert any(line.startswith("QTUM hyperscaler weight 11.0%") for line in lines)
    # Nothing in the module issues SQL writes.
    src = Path(thesis.__file__).read_text(encoding="utf-8")
    assert "write_tx" not in src and "insert(" not in src and "update(" not in src


def test_thesis_config_defaults_match_the_spec() -> None:
    d = ThesisConfig()
    assert (d.financings_min, d.fd_yoy_max, d.runway_min_months) == (2, 0.20, 24.0)
    assert (d.revenue_flat_max, d.opex_growth_min, d.acquisitions_liquidity_max) == (
        0.05,
        0.25,
        0.25,
    )
    assert (d.winner_revenue_quarters, d.winner_fd_yoy_max) == (4, 0.05)
