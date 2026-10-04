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


def test_facts_all_unverified_and_sourced() -> None:
    facts = load_facts(CONFIG_DIR)
    assert len(facts) == 8
    assert {f.status for f in facts} == {"unverified"}
    lockup = next(f for f in facts if f.id == "qnt_lockup_expiry")
    assert lockup.sources == () and "UNKNOWN" in lockup.claim


def test_unconfirmed_facts_are_labelled_in_prompts() -> None:
    facts = load_facts(CONFIG_DIR)
    block = render_for_prompt(facts)
    assert block.count("[UNCONFIRMED") == len(facts)
    assert "[FACT " not in block
    signed = facts[0].model_copy(update={"status": "signed_off"})
    assert render_for_prompt([signed]).startswith("[FACT ")


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
