"""M15 (spec §10): one `llm_calls` row per outcome, the cache-marker rule and the expense export."""

from __future__ import annotations

import csv
import io
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from sqlalchemy import Engine, insert, select, text

from aether.db import migrate
from aether.db.engine import ensure_db_file, make_ro_engine, make_rw_engine, write_tx
from aether.db.models import llm_calls
from aether.llm.client import BatchResult, BudgetExceeded, LlmError, web_search_tool
from aether.ops.llm_log import (
    COMPONENTS,
    RangeError,
    append_dev_log,
    daily_summary,
    export,
    parse_range,
    rows,
)
from aether.runs import JobResult, run_job
from tests.conftest import CONFIG_DIR, TEST_SESSION_SECRET
from tests.conftest import make_client as web_client
from tests.llm_fakes import (
    TEST_API_KEY,
    FakeApi,
    llm_config,
    llm_settings,
    make_client,
    message,
    research_message,
    search_result,
    usage,
)

NOW = datetime(2026, 10, 5, 4, 0, tzinfo=UTC)  # 12:00 SGT


def _clock() -> datetime:
    return NOW


def _rows(engine: Engine) -> list[Any]:
    with engine.connect() as conn:
        return list(conn.execute(select(llm_calls).order_by(llm_calls.c.id)).all())


def _complete(client: Any, **kw: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "purpose": "test_call",
        "model": "claude-opus-5-5",
        "system": "You are a test.",
        "messages": [{"role": "user", "content": "ACME?"}],
        "max_tokens": 500,
    }
    args.update(kw)
    return client.complete(**args)  # type: ignore[no-any-return]


# --------------------------------------------------------------------------- one row per outcome


def test_ok_row_has_all_m15_fields(rw_engine: Engine, migrated_db: Path) -> None:
    api = FakeApi(messages=[message([{"type": "text", "text": "hi"}])])
    client = make_client(rw_engine, llm_settings(migrated_db), api, clock=_clock)
    _complete(client, effort="low", context={"symbol": "ACME"})
    (row,) = _rows(rw_engine)
    assert row.status == "ok" and row.stop_reason == "end_turn"
    assert row.attempts == 1 and row.latency_ms is not None and row.latency_ms >= 0
    assert row.effort == "low" and row.price_version == llm_config().price_version
    assert json.loads(row.context) == {"symbol": "ACME"}
    assert row.request_id == "msg_test_0001" and row.cost_micros > 0


def test_retried_error_logs_one_row_with_attempts(rw_engine: Engine, migrated_db: Path) -> None:
    api = FakeApi(status_code=529)  # overloaded: retried, then given up
    client = make_client(rw_engine, llm_settings(migrated_db), api, clock=_clock)
    with pytest.raises(LlmError):
        _complete(client)
    assert len(api.requests) == 3
    (row,) = _rows(rw_engine)
    assert row.status == "error" and row.attempts == 3 and row.cost_micros == Decimal(0)
    assert TEST_API_KEY not in (row.error or "")


def test_transient_error_then_ok_is_one_ok_row(rw_engine: Engine, migrated_db: Path) -> None:
    api = FakeApi(messages=[message([{"type": "text", "text": "hi"}])])
    calls = {"n": 0}

    def flaky(_req: Any) -> None:
        calls["n"] += 1
        api.status_code = 429 if calls["n"] == 1 else 200

    api.on_request = flaky
    client = make_client(rw_engine, llm_settings(migrated_db), api, clock=_clock)
    _complete(client)
    (row,) = _rows(rw_engine)
    assert row.status == "ok" and row.attempts == 2


def test_refusal_is_logged_and_returned(rw_engine: Engine, migrated_db: Path) -> None:
    m = message([], use=usage(input_tokens=50, output_tokens=0))
    m["stop_reason"] = "refusal"
    api = FakeApi(messages=[m])
    client = make_client(rw_engine, llm_settings(migrated_db), api, clock=_clock)
    out = _complete(client)
    assert out["stop_reason"] == "refusal"
    (row,) = _rows(rw_engine)
    assert row.status == "refusal" and row.stop_reason == "refusal" and row.cost_micros > 0


def test_budget_refusal_row_is_free_with_context(rw_engine: Engine, migrated_db: Path) -> None:
    api = FakeApi()
    client = make_client(
        rw_engine, llm_settings(migrated_db, daily_llm_budget_usd="0.0001"), api, clock=_clock
    )
    with pytest.raises(BudgetExceeded):
        _complete(client, context={"symbol": "ACME"})
    assert api.requests == []
    (row,) = _rows(rw_engine)
    assert row.status == "refused_budget" and row.cost_micros == Decimal(0)
    assert json.loads(row.context)["symbol"] == "ACME"


def test_context_rejects_unknown_keys_and_non_scalars(rw_engine: Engine, migrated_db: Path) -> None:
    api = FakeApi()
    client = make_client(rw_engine, llm_settings(migrated_db), api, clock=_clock)
    with pytest.raises(ValueError, match="unknown"):
        _complete(client, context={"holdings": 1})
    with pytest.raises(ValueError, match="scalar"):
        _complete(client, context={"symbol": ["ACME"]})
    assert api.requests == [] and _rows(rw_engine) == []


def test_job_context_comes_from_run_job(rw_engine: Engine, migrated_db: Path) -> None:
    api = FakeApi(messages=[message([{"type": "text", "text": "hi"}])])
    client = make_client(rw_engine, llm_settings(migrated_db), api, clock=_clock)

    def job() -> JobResult:
        _complete(client, context={"symbol": "ACME"})
        return JobResult()

    run_job(rw_engine, "research_sweep", job)
    (row,) = _rows(rw_engine)
    ctx = json.loads(row.context)
    assert ctx["job"] == "research_sweep" and isinstance(ctx["job_run_id"], int)
    assert ctx["symbol"] == "ACME"


# --------------------------------------------------------------------------- cache markers


def _research(client: Any, **kw: Any) -> None:
    _complete(
        client,
        purpose="research_sweep",
        tools=[web_search_tool(3, ["example.test"])],
        **kw,
    )


def _markers(body: Any) -> list[str]:
    """JSON paths of every cache_control in a request body."""
    found: list[str] = []

    def walk(v: Any, path: str) -> None:
        if isinstance(v, dict):
            for k, x in v.items():
                if k == "cache_control":
                    found.append(path)
                walk(x, f"{path}.{k}")
        elif isinstance(v, list):
            for i, x in enumerate(v):
                walk(x, f"{path}[{i}]")

    walk(body, "")
    return found


def test_cache_marker_only_on_static_system_prefix(rw_engine: Engine, migrated_db: Path) -> None:
    results = [search_result("https://example.test/a", "Synthetic", "October 4, 2026")]
    api = FakeApi(messages=[research_message(results), research_message(results)])
    client = make_client(rw_engine, llm_settings(migrated_db), api, clock=_clock)
    # A follow-up turn carrying search results in `messages` must still get no marker there.
    history = [
        {"role": "user", "content": "ACME?"},
        {"role": "assistant", "content": research_message(results)["content"]},
        {"role": "user", "content": "More?"},
    ]
    _research(client, cache_prefix=True)
    _research(client, messages=history, cache_prefix=True)
    for body in api.bodies():
        assert _markers(body) == [".system[0]"]
        assert "cache_control" not in body  # no top-level automatic caching


def test_web_search_calls_uncached_by_default(rw_engine: Engine, migrated_db: Path) -> None:
    """`research.cache_prefix: false` (the M15 A/B): with a marker the API adds its own writes
    after each search result, which cost more than they saved."""
    assert llm_config().research.cache_prefix is False
    results = [search_result("https://example.test/a", "Synthetic", "October 4, 2026")]
    api = FakeApi(messages=[research_message(results), message([{"type": "text", "text": "x"}])])
    client = make_client(rw_engine, llm_settings(migrated_db), api, clock=_clock)
    _research(client)
    _complete(client)  # non-research calls keep the system marker
    research_body, plain_body = api.bodies()
    assert _markers(research_body) == []
    assert _markers(plain_body) == [".system[0]"]


def test_second_identical_sweep_reads_prefix_from_cache(
    rw_engine: Engine, migrated_db: Path
) -> None:
    """Recorded-shape fixture: the first call writes the prefix, an identical second call reads
    it. The rows keep both components so the Ops page can show cache-write spend."""
    first = message([], use=usage(input_tokens=100, cache_write=1500, output_tokens=50))
    second = message([], use=usage(input_tokens=100, cache_read=1500, output_tokens=50))
    api = FakeApi(messages=[first, second])
    client = make_client(rw_engine, llm_settings(migrated_db), api, clock=_clock)
    _research(client, cache_prefix=True)
    _research(client, cache_prefix=True)
    a, b = api.bodies()
    assert a == b  # byte-identical prefix: nothing per-request before the marker
    r1, r2 = _rows(rw_engine)
    assert (r1.cache_write_tokens, r1.cache_read_tokens) == (1500, 0)
    assert (r2.cache_write_tokens, r2.cache_read_tokens) == (0, 1500)
    assert r2.cost_micros < r1.cost_micros


# --------------------------------------------------------------------------- batches


def _batch_item(cid: str) -> dict[str, Any]:
    return {
        "custom_id": cid,
        "result": {
            "type": "succeeded",
            "message": message(
                [{"type": "text", "text": "ok"}], model="claude-sonnet-5-5", msg_id=f"msg_{cid}"
            ),
        },
    }


def test_batch_submission_and_results(rw_engine: Engine, migrated_db: Path) -> None:
    cfg = llm_config()
    api = FakeApi(batch_status="ended", batch_results=[_batch_item(f"c{i}") for i in range(3)])
    client = make_client(rw_engine, llm_settings(migrated_db), api, clock=_clock)
    params = {
        "model": "claude-sonnet-5-5",
        "max_tokens": 100,
        "system": [{"type": "text", "text": "x"}],
        "messages": [{"role": "user", "content": "ACME"}],
        "output_config": {"effort": "low"},
    }
    batch_id = client.submit_batch([(f"c{i}", params) for i in range(3)], purpose="classify_batch")
    for res in client.batch_results(batch_id, purpose="classify_batch"):
        assert res.message is not None
        client.record_batch_result(
            purpose="classify_batch",
            model="claude-sonnet-5-5",
            message=res.message,
            custom_id=res.custom_id,
        )
    sub, *items = _rows(rw_engine)
    assert sub.status == "batch_submitted" and sub.cost_micros == Decimal(0) and sub.batch == 1
    assert json.loads(sub.context) == {"batch_id": batch_id, "items": 3}
    assert sub.effort == "low"
    assert [r.status for r in items] == ["batch_result"] * 3
    one = message([], model="claude-sonnet-5-5")["usage"]
    p = cfg.prices["claude-sonnet-5-5"]
    expected = (
        (Decimal(one["input_tokens"]) * p.input + Decimal(one["output_tokens"]) * p.output)
        / Decimal(1_000_000)
        * cfg.batch_discount
    ).quantize(Decimal("0.000001"))
    assert all(r.cost_micros == expected and r.batch == 1 for r in items)
    assert [json.loads(r.context)["custom_id"] for r in items] == ["c0", "c1", "c2"]


def test_batch_failure_item_logs_error_row(rw_engine: Engine, migrated_db: Path) -> None:
    api = FakeApi()
    client = make_client(rw_engine, llm_settings(migrated_db), api, clock=_clock)
    client.record_batch_failure(
        purpose="classify_batch",
        model="claude-sonnet-5-5",
        result=BatchResult("c9", "expired", None, None),
    )
    (row,) = _rows(rw_engine)
    assert row.status == "error" and row.batch == 1 and row.cost_micros == Decimal(0)
    assert row.error == "expired" and json.loads(row.context)["custom_id"] == "c9"


def test_batch_submit_error_logs_row(rw_engine: Engine, migrated_db: Path) -> None:
    api = FakeApi(status_code=400)
    client = make_client(rw_engine, llm_settings(migrated_db), api, clock=_clock)
    params = {"model": "claude-sonnet-5-5", "max_tokens": 10, "messages": []}
    with pytest.raises(LlmError):
        client.submit_batch([("c0", params)], purpose="classify_batch")
    (row,) = _rows(rw_engine)
    assert row.status == "error" and row.batch == 1


# --------------------------------------------------------------------------- export


def _seed(engine: Engine) -> None:
    base = {
        "model": "claude-opus-5-5",
        "price_version": "2026-10-05",
        "input_tokens": 1000,
        "output_tokens": 200,
        "cache_write_tokens": 3000,
        "cache_read_tokens": 4000,
        "web_searches": 2,
    }
    with write_tx(engine) as conn:
        for row in [
                {**base, "purpose": "research_sweep", "status": "ok", "cost_micros":
                    Decimal("0.042800"), "created_at": "2026-10-04T23:00:00Z",
                    "request_id": "msg_a", "context": '{"symbol": "ACME"}'},
                {**base, "purpose": "classify_batch", "status": "batch_result", "batch": 1,
                    "cost_micros": Decimal("0.011400"), "created_at": "2026-10-05T02:00:00Z",
                    "error": "SECRET PROMPT FRAGMENT"},
                {**base, "purpose": "classify", "status": "refused_budget", "input_tokens": 0,
                    "output_tokens": 0, "cache_write_tokens": 0, "cache_read_tokens": 0,
                    "web_searches": 0, "cost_micros": Decimal(0),
                    "created_at": "2026-10-05T03:00:00Z"},
                # Outside the range (2026-10-06 SGT).
                {**base, "purpose": "classify", "status": "ok", "cost_micros": Decimal("1"),
                    "created_at": "2026-10-05T17:00:00Z"},
        ]:  # fmt: skip
            conn.execute(insert(llm_calls).values(**row))


def test_export_rows_and_summary_add_up(rw_engine: Engine) -> None:
    _seed(rw_engine)
    cfg = llm_config()
    data = rows(rw_engine, date(2026, 10, 5), date(2026, 10, 5))
    assert [r["purpose"] for r in data] == ["research_sweep", "classify_batch", "classify"]
    summary = daily_summary(data, cfg)
    total = next(s for s in summary if s.day == "ALL")
    assert total.cost == sum((Decimal(r["cost_usd"]) for r in data), Decimal(0))
    for s in summary:
        assert sum((s.parts[k] for k in COMPONENTS), Decimal(0)) == s.cost
    day_total = next(s for s in summary if s.day == "2026-10-05" and s.purpose == "TOTAL")
    assert day_total.calls == 3 and day_total.parts["batch_discount"] < 0
    assert day_total.parts["cache_write"] > 0


def test_export_files_are_metadata_only(rw_engine: Engine) -> None:
    _seed(rw_engine)
    cfg = llm_config()
    for fmt in ("csv", "json"):
        out = export(rw_engine, cfg, date(2026, 10, 5), date(2026, 10, 5), fmt)
        assert "SECRET PROMPT FRAGMENT" not in out  # the error text is never exported
        assert TEST_API_KEY not in out and "shares" not in out.lower()
        assert "2026-10-05" in out and cfg.price_version in out
    body = export(rw_engine, cfg, date(2026, 10, 5), date(2026, 10, 5), "csv")
    head = next(csv.reader(io.StringIO(body)))
    assert "error" not in head and "request_id" in head


def test_parse_range() -> None:
    today = date(2026, 10, 13)
    assert parse_range(None, None, today) == (date(2026, 9, 14), today)
    with pytest.raises(RangeError):
        parse_range("2026-10-10", "2026-10-01", today)
    with pytest.raises(RangeError):
        parse_range("bad", None, today)
    with pytest.raises(RangeError):
        parse_range("2020-01-01", "2026-10-01", today)


def test_dev_log_appends_throwaway_rows(rw_engine: Engine, tmp_path: Path) -> None:
    _seed(rw_engine)
    out = tmp_path / "exports" / "llm-dev-log.csv"
    assert append_dev_log(rw_engine, out) == 4
    assert append_dev_log(rw_engine, out) == 4
    lines = out.read_text().splitlines()
    assert lines[0].startswith("id,") and len(lines) == 9  # one header, then 8 rows


def test_export_routes(rw_engine: Engine, settings: Any) -> None:
    _seed(rw_engine)
    with web_client(settings, logged_in=False) as c:
        r = c.get("/ops/llm-log.csv?from=2026-10-05&to=2026-10-05", follow_redirects=False)
        assert r.status_code == 303
    before = _rows(rw_engine)
    with web_client(settings) as c:
        r = c.get("/ops/llm-log.csv?from=2026-10-05&to=2026-10-05")
        assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
        assert r.headers["content-type"].startswith("text/csv")
        assert "attachment" in r.headers["content-disposition"]
        assert "research_sweep" in r.text and "SECRET PROMPT FRAGMENT" not in r.text
        j = c.get("/ops/llm-log.json?from=2026-10-05&to=2026-10-05")
        assert j.status_code == 200 and len(j.json()["rows"]) == 3
        assert c.get("/ops/llm-log.csv?from=nope").status_code == 400
        page = c.get("/ops?from=2026-10-05&to=2026-10-05&purpose=classify")
        assert page.status_code == 200 and "LLM expense log" in page.text
    assert _rows(rw_engine) == before  # the export never writes


# --------------------------------------------------------------------------- migration


def test_migration_converts_old_statuses(tmp_path: Path) -> None:
    db = tmp_path / "m.db"
    ensure_db_file(db)
    migrate.upgrade(db, "0016_adjacent_thesis")
    eng = make_rw_engine(db)
    with write_tx(eng) as conn:
        for status, batch in (("budget_refused", 0), ("ok", 1), ("ok", 0), ("error", 0)):
            conn.execute(
                text(
                    "INSERT INTO llm_calls (purpose, model, created_at, status, batch) "
                    "VALUES ('classify', 'claude-sonnet-5-5', '2026-10-01T00:00:00Z', :s, :b)"
                ),
                {"s": status, "b": batch},
            )
    eng.dispose()
    migrate.upgrade(db)
    eng = make_ro_engine(db)
    got = [(r.status, r.attempts, r.context) for r in _rows(eng)]
    eng.dispose()
    assert got == [
        ("refused_budget", 1, "{}"),
        ("batch_result", 1, "{}"),
        ("ok", 1, "{}"),
        ("error", 1, "{}"),
    ]
    command.downgrade(migrate.alembic_config(db), "0016_adjacent_thesis")
    eng = make_ro_engine(db)
    with eng.connect() as conn:
        statuses = [s for (s,) in conn.execute(text("SELECT status FROM llm_calls ORDER BY id"))]
    eng.dispose()
    assert statuses == ["budget_refused", "ok", "ok", "error"]


_ = (CONFIG_DIR, TEST_SESSION_SECRET)
