"""LLM wrapper (M6): soft budget guard, tool gate, cost accounting, logging, redaction."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine, insert, select

from aether.alerts.candidates import llm_budget
from aether.db.engine import write_tx
from aether.db.models import llm_calls
from aether.llm.client import BudgetExceeded, LlmDisabled, LlmError, web_search_tool
from aether.llm.pricing import (
    TokenCounts,
    UnknownModel,
    Usage,
    cost_usd,
    estimate_usd,
    usage_from_dict,
)
from tests.conftest import make_settings
from tests.llm_fakes import (
    TEST_API_KEY,
    FakeApi,
    llm_config,
    llm_settings,
    make_client,
    message,
    usage,
)

NOW = datetime(2026, 10, 5, 4, 0, tzinfo=UTC)  # 12:00 SGT


def _clock() -> datetime:
    return NOW


def _spend(engine: Engine, usd: str, *, batch: int = 0, at: str = "2026-10-05T01:00:00Z") -> None:
    with write_tx(engine) as conn:
        conn.execute(
            insert(llm_calls).values(
                purpose="research_sweep",
                model="claude-opus-5-5",
                cost_micros=Decimal(usd),
                created_at=at,
                batch=batch,
            )
        )


def _rows(engine: Engine) -> list:  # type: ignore[type-arg]
    with engine.connect() as conn:
        return list(conn.execute(select(llm_calls).order_by(llm_calls.c.id)).all())


def _complete(client, **kw):  # type: ignore[no-untyped-def]
    args = {
        "purpose": "test_call",
        "model": "claude-opus-5-5",
        "system": "You are a test.",
        "messages": [{"role": "user", "content": "ACME?"}],
        "max_tokens": 500,
    }
    args.update(kw)
    return client.complete(**args)


# --------------------------------------------------------------------------- budget guard


def test_budget_breach_stops_calls(rw_engine: Engine, migrated_db: Path) -> None:
    """Spec M6 acceptance: a budget breach stops calls (no HTTP request is sent)."""
    _spend(rw_engine, "4.99")  # today, SGT
    api = FakeApi(messages=[message([{"type": "text", "text": "hi"}])])
    client = make_client(rw_engine, llm_settings(migrated_db), api, clock=_clock)
    with pytest.raises(BudgetExceeded):
        _complete(client)
    assert api.requests == []
    refused = _rows(rw_engine)[-1]
    assert refused.status == "budget_refused"
    assert refused.cost_micros == Decimal(0)
    assert "budget" in refused.error


def test_budget_ignores_yesterday_and_batches(rw_engine: Engine, migrated_db: Path) -> None:
    _spend(rw_engine, "50", at="2026-10-04T15:59:59Z")  # 23:59:59 SGT yesterday
    _spend(rw_engine, "50", batch=1)  # backfill batch: outside the soft budget
    api = FakeApi(messages=[message([{"type": "text", "text": "hi"}])])
    client = make_client(rw_engine, llm_settings(migrated_db), api, clock=_clock)
    assert client.spent_today() == Decimal(0)
    _complete(client)
    assert len(api.requests) == 1


def test_budget_counts_worst_case_estimate(rw_engine: Engine, migrated_db: Path) -> None:
    """Spent alone fits, but spent + the call's worst case doesn't → refused."""
    cfg = llm_config()
    est = estimate_usd(
        cfg,
        "claude-opus-5-5",
        system="You are a test.",
        messages=[{"role": "user", "content": "ACME?"}],
        max_tokens=500,
    )
    _spend(rw_engine, str(Decimal("5.00") - est + Decimal("0.000001")))
    api = FakeApi(messages=[message([])])
    client = make_client(rw_engine, llm_settings(migrated_db), api, clock=_clock)
    with pytest.raises(BudgetExceeded):
        _complete(client)
    assert api.requests == []


def test_llm_budget_alert_once_per_day_at_80pct(rw_engine: Engine) -> None:
    with rw_engine.connect() as conn:
        assert llm_budget(conn, Decimal("3"), Decimal("0.8"), NOW) == []
    _spend(rw_engine, "2.40")
    with rw_engine.connect() as conn:
        (c,) = llm_budget(conn, Decimal("3"), Decimal("0.8"), NOW)
    assert c.kind == "llm_budget"
    assert c.dedupe_key == "llm_budget:2026-10-05"  # one per SGT day (outbox dedupe)
    assert "80%" in c.text and "$2.40" in c.text


# --------------------------------------------------------------------------- calls & cost


def test_call_logs_row_with_exact_cost(rw_engine: Engine, migrated_db: Path) -> None:
    u = usage(input_tokens=10_000, output_tokens=2_000, searches=3, cache_read=50_000)
    api = FakeApi(messages=[message([{"type": "text", "text": "ok"}], use=u)])
    client = make_client(rw_engine, llm_settings(migrated_db), api, clock=_clock)
    out = _complete(client)
    assert out["content"][0]["text"] == "ok"
    (row,) = _rows(rw_engine)
    # 10k x $4 + 2k x $20 + 50k x $0.20 per MTok + 3 searches x $0.01
    assert row.cost_micros == Decimal("0.04") + Decimal("0.04") + Decimal("0.01") + Decimal("0.03")
    assert (row.input_tokens, row.output_tokens, row.cache_read_tokens, row.web_searches) == (
        10_000,
        2_000,
        50_000,
        3,
    )
    assert row.status == "ok" and row.batch == 0 and row.request_id == "msg_test_0001"
    body = api.bodies()[0]
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}  # cached system prompt
    assert body["fallbacks"] == "default"
    assert "tools" not in body


def test_fallback_iterations_priced_per_model() -> None:
    cfg = llm_config()
    u = usage_from_dict(
        {
            "input_tokens": 0,
            "output_tokens": 0,
            "iterations": [
                {"type": "message", "model": "claude-opus-5-5", "input_tokens": 1_000_000},
                {"type": "fallback_message", "model": "claude-opus-4-8", "input_tokens": 1_000_000},
                {
                    "type": "fallback_message",
                    "model": "claude-unlisted-9",
                    "output_tokens": 1_000_000,
                },
            ],
        }
    )
    # $4 + $5 + the dearest listed output price ($25)
    assert cost_usd(cfg, "claude-opus-5-5", u) == Decimal("34")


def test_batch_discount_applies_to_tokens_not_searches() -> None:
    cfg = llm_config()
    u = Usage(TokenCounts(input=1_000_000), web_searches=10)
    assert cost_usd(cfg, "claude-opus-5-5", u) == Decimal("4.1")
    assert cost_usd(cfg, "claude-opus-5-5", u, batch=True) == Decimal("2.1")


def test_unknown_model_refused(rw_engine: Engine, migrated_db: Path) -> None:
    api = FakeApi()
    client = make_client(rw_engine, llm_settings(migrated_db), api, clock=_clock)
    with pytest.raises(UnknownModel):
        _complete(client, model="claude-mystery-1")
    assert api.requests == [] and _rows(rw_engine) == []


@pytest.mark.parametrize("purpose", ["classify", "synthesis", "brief"])
def test_tools_refused_outside_research(rw_engine: Engine, migrated_db: Path, purpose: str) -> None:
    """S1: classification and synthesis calls get no tools."""
    api = FakeApi()
    client = make_client(rw_engine, llm_settings(migrated_db), api, clock=_clock)
    with pytest.raises(ValueError, match="not allowed"):
        _complete(client, purpose=purpose, tools=[web_search_tool(3, ["example.test"])])
    assert api.requests == []


def test_research_may_use_only_web_search(rw_engine: Engine, migrated_db: Path) -> None:
    api = FakeApi()
    client = make_client(rw_engine, llm_settings(migrated_db), api, clock=_clock)
    with pytest.raises(ValueError, match="only the web-search"):
        _complete(
            client,
            purpose="research_sweep",
            tools=[{"type": "code_execution_20260521", "name": "code_execution"}],
        )
    assert api.requests == []


def test_research_tool_request_shape(rw_engine: Engine, migrated_db: Path) -> None:
    api = FakeApi(messages=[message([{"type": "text", "text": "none"}])])
    client = make_client(rw_engine, llm_settings(migrated_db), api, clock=_clock)
    _complete(
        client,
        purpose="research_sweep",
        tools=[web_search_tool(5, ["sec.gov", "example.test"])],
        effort="low",
    )
    body = api.bodies()[0]
    assert body["tools"] == [
        {
            "type": "web_search_20260209",
            "name": "web_search",
            "max_uses": 5,
            "allowed_domains": ["sec.gov", "example.test"],
            "allowed_callers": ["direct"],
        }
    ]
    assert body["output_config"] == {"effort": "low"}


# --------------------------------------------------------------------------- failures & secrets


def test_api_error_logged_and_key_never_leaks(
    rw_engine: Engine, migrated_db: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    api = FakeApi(status_code=400)  # 4xx: not retried
    client = make_client(rw_engine, llm_settings(migrated_db), api, clock=_clock)
    with pytest.raises(LlmError) as exc:
        _complete(client)
    assert exc.value.__cause__ is None
    (row,) = _rows(rw_engine)
    assert row.status == "error" and row.cost_micros == Decimal(0)
    # The key went out only in the auth header, and appears nowhere we keep.
    assert api.requests[0].headers["x-api-key"] == TEST_API_KEY
    assert TEST_API_KEY not in str(exc.value)
    assert TEST_API_KEY not in (row.error or "")
    assert TEST_API_KEY not in caplog.text


def test_no_key_disables(rw_engine: Engine, migrated_db: Path) -> None:
    from aether.llm.client import LlmClient

    with pytest.raises(LlmDisabled):
        LlmClient(rw_engine, make_settings(migrated_db), llm_config())


def test_row_written_after_the_response(rw_engine: Engine, migrated_db: Path) -> None:
    """No write transaction is open while the request is in flight."""
    seen: list[int] = []

    def during(_req) -> None:  # type: ignore[no-untyped-def]
        # A write lock held by the caller would make this BEGIN IMMEDIATE wait and fail.
        with write_tx(rw_engine, attempts=1) as conn:
            seen.append(len(conn.execute(select(llm_calls.c.id)).all()))

    api = FakeApi(messages=[message([])], on_request=during)
    client = make_client(rw_engine, llm_settings(migrated_db), api, clock=_clock)
    _complete(client)
    assert seen == [0]
    assert len(_rows(rw_engine)) == 1


def test_lint_rejects_anthropic_import_outside_wrapper(tmp_path: Path) -> None:
    from scripts import check_llm_imports

    bad = tmp_path / "aether" / "classify" / "llm.py"
    bad.parent.mkdir(parents=True)
    bad.write_text("import anthropic\nfrom anthropic import Anthropic\n")
    assert len(check_llm_imports.check_file(bad)) == 2
    ok = tmp_path / "aether" / "llm" / "client.py"
    ok.parent.mkdir(parents=True)
    ok.write_text("import anthropic\n")
    assert check_llm_imports.check_file(ok) == []
    assert check_llm_imports.main([str(tmp_path)]) == 1
    assert check_llm_imports.main(["src"]) == 0  # the real tree is clean
