from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from sqlalchemy import Engine, select

from aether.config import Settings, Watchlist, load_sources, load_watchlist
from aether.db.engine import write_tx
from aether.db.models import facts as facts_table
from aether.facts import load_facts, render_for_prompt, render_markdown, sync_facts
from tests.conftest import CONFIG_DIR, REPO


def test_watchlist_matches_spec() -> None:
    wl = load_watchlist(CONFIG_DIR)
    assert [t.symbol for t in wl.by_type("etf")] == ["QTUM"]
    assert [t.symbol for t in wl.by_type("pure_play")] == ["IONQ", "QNT", "RGTI", "QBTS", "INFQ"]
    assert [t.symbol for t in wl.by_type("adjacent")] == ["KEYS", "FEIM", "PANW"]
    assert [t.symbol for t in wl.by_type("benchmark")] == ["QQQ", "SOXX"]
    assert [t.symbol for t in wl.by_type("context")] == ["IBM", "GOOGL", "MSFT", "AMZN", "NVDA"]


def test_watchlist_rejects_commentary() -> None:
    with pytest.raises(ValidationError):
        Watchlist.model_validate(
            {"tickers": [{"symbol": "ACME", "type": "pure_play", "note": "strongest tech"}]}
        )


def test_watchlist_rejects_duplicates() -> None:
    with pytest.raises(ValidationError):
        Watchlist.model_validate(
            {"tickers": [{"symbol": "ACME", "type": "etf"}, {"symbol": "ACME", "type": "etf"}]}
        )


def test_sources_tiers() -> None:
    src = load_sources(CONFIG_DIR)
    assert src.tier_for("www.sec.gov") == "T1"
    assert src.tier_for("hpcwire.com") == "T2"
    assert src.tier_for("notsec.gov") == "T3"
    assert src.tier_for("random-blog.example") == "T3"


def test_settings_empty_env_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("SEC_USER_AGENT", "")
    s = Settings()
    assert s.anthropic_api_key is None and s.sec_user_agent is None


def test_secrets_not_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-synthetic-not-a-real-key")
    assert "synthetic-not-a-real" not in repr(Settings())


def test_facts_verified_and_partly_signed_off() -> None:
    facts = load_facts(CONFIG_DIR)
    assert (
        len(facts) == 17
    )  # M8 added darpa_qbi_stage_b_duration; M14 the 8 modality/evidence facts
    # M2 verified every seed against a primary source; the owner signed off five (2026-10-05).
    assert {f.status for f in facts} == {"verified_by_claude", "signed_off"}
    assert sum(f.status == "signed_off" for f in facts) == 5
    assert all(f.sources and f.retrieved_at for f in facts)
    lockup = next(f for f in facts if f.id == "qnt_lockup_expiry")
    assert "2026-11-30" in lockup.claim
    assert any("sec.gov" in u and "424b4" in u for u in lockup.sources)


def test_unconfirmed_facts_are_labelled_in_prompts() -> None:
    facts = load_facts(CONFIG_DIR)
    block = render_for_prompt(facts)
    unsigned = [f for f in facts if f.status != "signed_off"]
    assert block.count("[UNCONFIRMED") == len(unsigned) > 0
    assert block.count("[FACT ") == len(facts) - len(unsigned)
    signed = facts[0].model_copy(update={"status": "signed_off"})
    assert render_for_prompt([signed]).startswith("[FACT ")
    pending = facts[0].model_copy(update={"status": "verified_by_claude"})
    assert render_for_prompt([pending]).startswith("[UNCONFIRMED")


def test_facts_md_in_sync() -> None:
    expected = render_markdown(load_facts(CONFIG_DIR))
    assert (REPO / "FACTS.md").read_text(encoding="utf-8") == expected, "run `make facts`"


def test_sync_facts_idempotent(rw_engine: Engine) -> None:
    facts = load_facts(CONFIG_DIR)
    for _ in range(2):
        with write_tx(rw_engine) as conn:
            sync_facts(conn, facts)
    with rw_engine.connect() as conn:
        rows = conn.execute(select(facts_table)).all()
    assert len(rows) == len(facts)
    assert all(isinstance(json.loads(r.source_urls), list) for r in rows)


def test_fact_sources_must_be_http(tmp_path: Path) -> None:
    bad = {
        "facts": [
            {
                "id": "abc",
                "claim": "c",
                "sources": ["ftp://x"],
                "retrieved_at": None,
                "status": "unverified",
            }
        ]
    }
    (tmp_path / "facts.yaml").write_text(yaml.safe_dump(bad))
    with pytest.raises(ValidationError):
        load_facts(tmp_path)


def test_ignore_files_cover_secrets() -> None:
    for name in (".gitignore", ".dockerignore"):
        lines = (REPO / name).read_text().splitlines()
        for entry in (".env", "config/positions.yaml", "data/"):
            assert entry in lines, f"{entry} missing from {name}"


# --------------------------------------------------------------------------- M14


def test_m14_watchlist_tags_and_ciks() -> None:
    wl = load_watchlist(CONFIG_DIR)
    by = {t.symbol: t for t in wl.tickers}
    assert {s: by[s].modality for s in ("IONQ", "QNT", "RGTI", "QBTS", "INFQ")} == {
        "IONQ": "trapped_ion",
        "QNT": "trapped_ion",
        "RGTI": "superconducting",
        "QBTS": "annealing",
        "INFQ": "neutral_atom",
    }
    assert {s: (by[s].cik, by[s].sector) for s in ("KEYS", "FEIM", "PANW")} == {
        "KEYS": ("0001601046", "test_measurement"),
        "FEIM": ("0000039020", "sensing_timing"),
        "PANW": ("0001327567", "pqc_cyber"),
    }
    facts = {f.id: f for f in load_facts(CONFIG_DIR)}
    for t in wl.by_type("pure_play"):
        assert t.modality_fact in facts
        assert all(u.startswith("https://www.sec.gov/") for u in facts[t.modality_fact].sources)
    assert len(wl.sleeve()) == 8


def _wl(n_pure: int, extra: dict | None = None) -> dict:
    tickers = [{"symbol": "QTUM", "type": "etf"}]
    for i in range(n_pure):
        tickers.append(
            {
                "symbol": f"ACM{chr(65 + i)}",
                "type": "pure_play",
                "cik": f"{i + 1:010d}",
                "modality": "trapped_ion",
                "modality_fact": "acme_modality",
            }
        )
    if extra:
        tickers.append(extra)
    return {"tickers": tickers}


def test_tenth_active_name_fails_the_loader(tmp_path: Path) -> None:
    import shutil

    shutil.copy(CONFIG_DIR / "universe.yaml", tmp_path / "universe.yaml")
    (tmp_path / "watchlist.yaml").write_text(yaml.safe_dump(_wl(9)))
    assert len(load_watchlist(tmp_path).sleeve()) == 9
    tenth = {"symbol": "ADJA", "type": "adjacent", "cik": "0000000099", "sector": "pqc_cyber"}
    (tmp_path / "watchlist.yaml").write_text(yaml.safe_dump(_wl(9, tenth)))
    with pytest.raises(ValueError, match=r"10 active pure_play \+ adjacent names .* cap is 9"):
        load_watchlist(tmp_path)
    # An inactive 10th name doesn't count.
    (tmp_path / "watchlist.yaml").write_text(yaml.safe_dump(_wl(9, {**tenth, "active": False})))
    assert len(load_watchlist(tmp_path).sleeve()) == 9


def test_pure_play_without_modality_fails_the_loader() -> None:
    with pytest.raises(ValidationError, match="needs a modality"):
        Watchlist.model_validate(
            {"tickers": [{"symbol": "ACME", "type": "pure_play", "cik": "0000000001"}]}
        )
    with pytest.raises(ValidationError, match="needs a sector"):
        Watchlist.model_validate(
            {"tickers": [{"symbol": "ACME", "type": "adjacent", "cik": "0000000001"}]}
        )
    with pytest.raises(ValidationError, match="only a pure_play has a modality"):
        Watchlist.model_validate(
            {"tickers": [{"symbol": "ACME", "type": "etf", "modality": "photonic"}]}
        )
    with pytest.raises(ValidationError):
        Watchlist.model_validate(
            {
                "tickers": [
                    {
                        "symbol": "ACME",
                        "type": "pure_play",
                        "cik": "0000000001",
                        "modality": "magic",
                        "modality_fact": "acme_modality",
                    }
                ]
            }
        )


def test_m14_configs_load() -> None:
    from aether.config import load_strategies, load_thesis_config, load_universe_config

    t = load_thesis_config(CONFIG_DIR)
    assert (t.max_modality_share, t.max_name_share, t.min_modalities) == (0.5, 0.25, 3)
    assert (t.runway_min_months, t.max_etf_hyperscaler_weight) == (24, 0.10)
    st = load_strategies(CONFIG_DIR)
    assert st.sleeve_types == ("pure_play", "adjacent")
    assert {p: v.min_per_name for p, v in st.profiles.items()} == {
        "safe": 0.015,
        "medium": 0.03,
        "aggressive": 0.04,
    }
    u = load_universe_config(CONFIG_DIR)
    assert u.max_names_ex_qtum == 9 and u.adjacent is not None
    assert u.adjacent.excluded_symbols == ("AMZN", "MSFT", "GOOGL", "ORCL", "BABA")
    assert u.adjacent.excluded_sics == ("3674",)
    assert u.strong_candidate.cooldown_reviews == 3 and u.removal.min_materiality == 4
    assert u.adjacent.sector_of_seed("KEYS") == "test_measurement"
    with pytest.raises(ValidationError, match=r"unknown|Extra"):
        type(t).model_validate({"opinion": "quantum is great"})
