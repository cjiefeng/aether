"""Research runs (M6): events only from search-result blocks, sweep under the budget, backfill via
Message Batches. Synthetic responses only (example.test URLs); the real SDK talks to a fake
transport."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine, insert, select

from aether.config import SourceDomain, Sources, load_watchlist
from aether.db.engine import write_tx
from aether.db.models import event_tickers, events, llm_calls, research_runs
from aether.llm.pricing import estimate_usd
from aether.research.runner import (
    SYSTEM_PROMPT,
    Target,
    backfill_state,
    build_prompt,
    extract_items,
    month_windows,
    parse_page_age,
    poll_backfill,
    request_params,
    run_sweep,
    submit_backfill,
)
from aether.worker import sync_config
from tests.conftest import CONFIG_DIR, make_settings
from tests.llm_fakes import (
    MODEL,
    FakeApi,
    llm_config,
    llm_settings,
    make_client,
    research_message,
    search_result,
)

NOW = datetime(2026, 10, 5, 4, 0, tzinfo=UTC)
SOURCES = Sources(
    domains=(SourceDomain(domain="example.test", tier="T2"),),
)


def _clock() -> datetime:
    return NOW


@pytest.fixture
def engine(rw_engine: Engine, migrated_db: Path) -> Engine:
    sync_config(rw_engine, make_settings(migrated_db))
    return rw_engine


def _events(engine: Engine):  # type: ignore[no-untyped-def]
    with engine.connect() as conn:
        return conn.execute(select(events).order_by(events.c.id)).all()


def _runs(engine: Engine):  # type: ignore[no-untyped-def]
    with engine.connect() as conn:
        return conn.execute(select(research_runs).order_by(research_runs.c.id)).all()


# --------------------------------------------------------------------------- parsing


def test_events_only_from_search_results() -> None:
    msg = research_message(
        [
            search_result("https://example.test/a", "ACME synthetic contract", "October 3, 2026"),
            search_result(
                "https://example.test/b?utm_source=x", "ACME synthetic results", "2 days ago"
            ),
        ],
        citations=[("https://example.test/a", "ACME signed a synthetic contract on Oct 3.")],
        # A URL the model only *wrote* must never become an event.
        text="See also https://invented.example.test/not-in-results for more.",
    )
    items, seen = extract_items(
        msg, "IONQ", date(2026, 10, 1), date(2026, 10, 5), NOW, run_id=7, kind="sweep"
    )
    assert seen == 2
    assert [i.url for i in items] == [
        "https://example.test/a",
        "https://example.test/b?utm_source=x",
    ]
    a, b = items
    assert a.title == "ACME synthetic contract"
    assert a.excerpt == "ACME signed a synthetic contract on Oct 3."  # verbatim cited_text
    assert b.excerpt is None  # no citation: no excerpt, never model prose
    assert a.published_at == datetime(2026, 10, 3, 12, tzinfo=UTC)
    assert b.published_at == datetime(2026, 10, 3, 4, tzinfo=UTC)
    assert a.symbols == ("IONQ",) and a.origin == "web_search"
    assert a.raw["date_source"] == "page_age" and a.raw["research_run_id"] == 7


def test_out_of_window_dropped_and_undated_kept_as_retrieved() -> None:
    msg = research_message(
        [
            search_result("https://example.test/old", "ACME old item", "March 1, 2024"),
            search_result("https://example.test/nodate", "ACME undated item"),
            search_result("https://example.test/odd", "ACME odd date", "sometime"),
        ]
    )
    items, seen = extract_items(
        msg, "IONQ", date(2026, 10, 1), date(2026, 10, 5), NOW, run_id=1, kind="sweep"
    )
    assert seen == 3
    assert [i.url for i in items] == ["https://example.test/nodate", "https://example.test/odd"]
    assert all(i.raw["date_source"] == "retrieved" and i.published_at == NOW for i in items)


def test_search_error_block_ignored() -> None:
    msg = research_message([])
    msg["content"][1]["content"] = {
        "type": "web_search_tool_result_error",
        "error_code": "max_uses_exceeded",
    }
    assert extract_items(
        msg, "IONQ", date(2026, 10, 1), date(2026, 10, 5), NOW, run_id=1, kind="sweep"
    ) == ([], 0)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("April 30, 2025", datetime(2025, 4, 30, 12, tzinfo=UTC)),
        ("Apr 30, 2025", datetime(2025, 4, 30, 12, tzinfo=UTC)),
        ("2025-04-30", datetime(2025, 4, 30, 0, tzinfo=UTC)),
        ("2025-04-30T09:15:00Z", datetime(2025, 4, 30, 9, 15, tzinfo=UTC)),
        ("3 hours ago", datetime(2026, 10, 5, 1, 0, tzinfo=UTC)),
        ("1 week ago", datetime(2026, 9, 28, 4, 0, tzinfo=UTC)),
        ("", None),
        ("soon", None),
    ],
)
def test_parse_page_age(value: str, expected: datetime | None) -> None:
    assert parse_page_age(value, NOW) == expected


def test_prompt_holds_identifiers_only() -> None:
    p = build_prompt(Target("IONQ", "IonQ"), date(2026, 9, 1), date(2026, 9, 30))
    assert p == (
        "Company or fund: IonQ (ticker IONQ).\n"
        "Date window: 2026-09-01 to 2026-09-30 (inclusive).\n"
        "Find reports about it published in this window."
    )
    assert "untrusted" in SYSTEM_PROMPT  # S1 notice for search results


def test_month_windows() -> None:
    w = month_windows(date(2026, 10, 5), 12)
    assert len(w) == 12
    assert w[0] == (date(2025, 11, 1), date(2025, 11, 30))
    assert w[-2] == (date(2026, 9, 1), date(2026, 9, 30))
    assert w[-1] == (date(2026, 10, 1), date(2026, 10, 5))


# --------------------------------------------------------------------------- sweep


def _targets() -> list[str]:
    wl = load_watchlist(CONFIG_DIR)
    return [t.symbol for t in wl.tickers if t.type in ("etf", "pure_play", "adjacent")]


def test_sweep_writes_events_and_runs(engine: Engine, migrated_db: Path) -> None:
    names = _targets()
    api = FakeApi(
        messages=[
            research_message(
                [
                    search_result(
                        f"https://example.test/{s}", f"Synthetic item about {s}", "October 4, 2026"
                    )
                ]
            )
            for s in names
        ]
    )
    client = make_client(engine, llm_settings(migrated_db), api, clock=_clock)
    result = run_sweep(
        engine, client, llm_config(), SOURCES, load_watchlist(CONFIG_DIR), MODEL, NOW
    )
    assert result.rows_written == len(names) and result.warning is None
    runs = _runs(engine)
    assert [r.symbol for r in runs] == names
    assert all(r.status == "done" and r.items_found == 1 and r.events_new == 1 for r in runs)
    assert all(r.cost_micros > 0 for r in runs)
    assert all(r.window_start == "2026-10-03" and r.window_end == "2026-10-05" for r in runs)
    with engine.connect() as conn:
        calls = conn.execute(select(llm_calls.c.purpose, llm_calls.c.research_run_id)).all()
        tick = set(conn.execute(select(event_tickers.c.symbol)).scalars())
    assert all(p == "research_sweep" for p, _ in calls)
    assert sorted(rid for _, rid in calls) == [r.id for r in runs]
    assert tick == set(names)
    # The request carried the allow-list and identifiers only.
    body = api.bodies()[0]
    assert body["tools"][0]["allowed_domains"] == ["example.test"]
    assert body["tools"][0]["max_uses"] == llm_config().research.sweep_max_uses
    assert "Company or fund:" in body["messages"][0]["content"]


def test_sweep_stops_on_budget_and_keeps_partial(engine: Engine, migrated_db: Path) -> None:
    names = _targets()
    api = FakeApi(
        messages=[
            research_message(
                [search_result("https://example.test/first", "Synthetic first", "October 4, 2026")]
            )
        ]
    )
    # The budget fits one call's worst case plus less than that call's real cost.
    cfg = llm_config()
    params = request_params(
        cfg,
        MODEL,
        SOURCES,
        Target(names[0], "x" * 20),
        NOW.date(),
        NOW.date(),
        cfg.research.sweep_max_uses,
    )
    est_one = estimate_usd(
        cfg,
        MODEL,
        system=SYSTEM_PROMPT,
        messages=params["messages"],
        max_tokens=params["max_tokens"],
        max_searches=cfg.research.sweep_max_uses,
    ) + Decimal("0.005")
    client = make_client(
        engine, llm_settings(migrated_db, daily_llm_budget_usd=est_one), api, clock=_clock
    )
    result = run_sweep(
        engine, client, llm_config(), SOURCES, load_watchlist(CONFIG_DIR), MODEL, NOW
    )
    runs = _runs(engine)
    assert runs[0].status == "done"
    assert runs[1].status == "budget_refused"
    assert len(runs) == 2 < len(names)  # stopped, no further calls
    assert len(api.requests) == 1
    assert result.warning and result.warning.startswith(f"stopped at {names[1]}")
    assert len(_events(engine)) == 1


def test_sweep_failure_continues(engine: Engine, migrated_db: Path) -> None:
    api = FakeApi(status_code=400)
    client = make_client(engine, llm_settings(migrated_db), api, clock=_clock)
    with pytest.raises(RuntimeError, match="every research call failed"):
        run_sweep(engine, client, llm_config(), SOURCES, load_watchlist(CONFIG_DIR), MODEL, NOW)
    assert all(r.status == "failed" for r in _runs(engine))


# --------------------------------------------------------------------------- backfill


def test_backfill_submit_poll_ingest_once(engine: Engine, migrated_db: Path) -> None:
    names = _targets()
    api = FakeApi()
    client = make_client(engine, llm_settings(migrated_db), api, clock=_clock)
    cfg = llm_config()
    wl = load_watchlist(CONFIG_DIR)

    assert backfill_state(engine, NOW) == "none"
    res = submit_backfill(engine, client, cfg, SOURCES, wl, MODEL, NOW)
    assert res.rows_written == len(names) * 12
    runs = _runs(engine)
    assert all(r.status == "submitted" and r.batch_id == "msgbatch_test_0001" for r in runs)
    (batch_body,) = api.bodies()
    reqs = batch_body["requests"]
    assert len(reqs) == len(names) * 12
    assert reqs[0]["custom_id"] == f"bf-{names[0]}-2025-11"
    assert reqs[0]["params"]["tools"][0]["type"] == "web_search_20260209"
    assert "fallbacks" not in reqs[0]["params"]  # not accepted by the Batches API

    # Second start: nothing new is submitted.
    assert backfill_state(engine, NOW) == "open"
    again = submit_backfill(engine, client, cfg, SOURCES, wl, MODEL, NOW)
    assert again.rows_written == 0 and len(api.bodies()) == 1

    # Still processing: no ingest.
    polled = poll_backfill(engine, client, SOURCES, NOW)
    assert (
        polled is not None and polled.rows_written == 0 and "in_progress" in (polled.warning or "")
    )

    # Ended: one success, one error, the rest missing from the results.
    first, second = runs[0], runs[1]
    api.batch_status = "ended"
    api.batch_results = [
        {
            "custom_id": first.custom_id,
            "result": {
                "type": "succeeded",
                "message": research_message(
                    [
                        search_result(
                            "https://example.test/bf1",
                            "Synthetic backfill item",
                            "November 10, 2025",
                        )
                    ],
                    searches=4,
                ),
            },
        },
        {
            "custom_id": second.custom_id,
            "result": {
                "type": "errored",
                "error": {"type": "error", "error": {"type": "api_error", "message": "x"}},
            },
        },
    ]
    polled = poll_backfill(engine, client, SOURCES, NOW)
    assert polled is not None and polled.rows_written == 1
    after = {r.custom_id: r for r in _runs(engine)}
    assert after[first.custom_id].status == "done" and after[first.custom_id].events_new == 1
    assert after[second.custom_id].status == "failed"
    assert sum(r.status == "failed" for r in after.values()) == len(runs) - 1
    with engine.connect() as conn:
        (call,) = conn.execute(select(llm_calls)).all()
    assert call.batch == 1 and call.web_searches == 4 and call.purpose == "research_backfill"
    # Batch price: tokens at 50% (1000 in x $4, 200 out x $20 per MTok), searches at full price.
    assert call.cost_micros == (Decimal("0.004") + Decimal("0.004")) / 2 + Decimal("0.04")
    assert backfill_state(engine, NOW) == "done"
    assert poll_backfill(engine, client, SOURCES, NOW) is None
    (ev,) = _events(engine)
    assert ev.published_at == "2025-11-10T12:00:00Z"


def test_backfill_retries_after_failed_submit(engine: Engine, migrated_db: Path) -> None:
    from aether.llm.client import LlmError

    api = FakeApi(status_code=400)
    client = make_client(engine, llm_settings(migrated_db), api, clock=_clock)
    with pytest.raises(LlmError):
        submit_backfill(
            engine, client, llm_config(), SOURCES, load_watchlist(CONFIG_DIR), MODEL, NOW
        )
    assert all(r.status == "failed" and r.batch_id is None for r in _runs(engine))
    assert backfill_state(engine, NOW) == "none"
    api.status_code = 200
    res = submit_backfill(
        engine, client, llm_config(), SOURCES, load_watchlist(CONFIG_DIR), MODEL, NOW
    )
    assert res.rows_written == len(_targets()) * 12
    assert all(r.status == "submitted" for r in _runs(engine))


def test_backfill_stale_running_rows_dont_block(engine: Engine) -> None:
    with write_tx(engine) as conn:
        conn.execute(
            insert(research_runs).values(
                kind="backfill",
                symbol="IONQ",
                window_start="2026-09-01",
                window_end="2026-09-30",
                status="running",
                model=MODEL,
                custom_id="bf-IONQ-2026-09",
                created_at="2026-10-05T01:00:00Z",  # 3h old, never submitted
            )
        )
    assert backfill_state(engine, NOW) == "none"


def test_scheduler_registers_news_and_research_jobs(engine: Engine, migrated_db: Path) -> None:
    from aether.jobs import build_scheduler

    sched = build_scheduler(engine, make_settings(migrated_db))
    ids = {j.id for j in sched.get_jobs()}
    assert {
        "news_rss",
        "research_sweep",
        "research_backfill_submit",
        "research_backfill_poll",
    } <= ids
    sweep = sched.get_job("research_sweep")
    fields = {f.name: str(f) for f in sweep.trigger.fields}
    assert (fields["hour"], fields["minute"]) == ("8,20", "0")  # 08:00 / 20:00 SGT (spec §9)
    # No API key: research jobs are no-ops (no job_runs rows, no requests).
    sweep.func()
    sched.get_job("research_backfill_submit").func()
    with engine.connect() as conn:
        assert conn.execute(select(research_runs)).all() == []


def test_backfill_switch_off(engine: Engine, migrated_db: Path) -> None:
    """RESEARCH_BACKFILL=false (isolated test stacks) never submits."""
    from aether.jobs import build_scheduler

    api = FakeApi()
    settings = llm_settings(migrated_db, research_backfill=False)
    sched = build_scheduler(engine, settings)
    sched.get_job("research_backfill_submit").func()
    assert api.requests == [] and _runs(engine) == []
