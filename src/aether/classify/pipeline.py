"""The classification queue (spec §5.2, M7): rules → LLM (no tools) → trust-tier caps.

Every run:
1. reads unclassified news/research events (no `event_classifications` row and no `classify_state`
   other than `retry`);
2. applies the deterministic news rules (`classify.rules.classify_news`) in one short write;
3. sends the rest to the model: synchronously (one call per event, under the daily soft budget)
   when at most `batch_threshold` are waiting, else as one Message Batch (the backlog; outside the
   daily budget by owner decision 2026-10-05) that `poll_batches` ingests later.

Each answer is schema-validated (`classify.llm.parse_and_validate`); an invalid one is retried
until `max_attempts`, then the event is `failed` (shown on the Feed). An injection flag (model or
regex backstop) quarantines the event. Caps are applied after every write. No write transaction is
ever held across an LLM call.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Connection, Engine, select, update

from aether.classify.caps import apply_caps
from aether.classify.llm import Classification, InvalidOutput, parse_and_validate
from aether.classify.prompt import (
    EventInput,
    alias_map,
    batch_params,
    messages,
    output_format,
    prompt_version,
    system_prompt,
)
from aether.classify.rules import RuleHit, classify_news
from aether.config import (
    ClassifierRubric,
    ClassifyParams,
    Settings,
    load_llm_config,
    load_rubric,
    load_watchlist,
)
from aether.db.dialect import upsert
from aether.db.engine import write_tx
from aether.db.models import classify_state, event_classifications, event_tickers, events
from aether.db.types import to_iso
from aether.llm.client import BudgetExceeded, LlmClient, LlmError
from aether.runs import JobResult

log = logging.getLogger(__name__)

NEWS_ORIGINS = ("rss", "web_search", "manual")
STALE_SUBMIT = timedelta(hours=1)  # `batched` rows that never got a batch id (crash mid-submit)
MAX_CONSECUTIVE_ERRORS = 3  # API errors in a row → stop the run (likely an outage)


@dataclass(frozen=True)
class ClassifierContext:
    rubric: ClassifierRubric
    params: ClassifyParams
    model: str
    aliases: Mapping[str, str]  # symbol → company alias

    @property
    def version(self) -> str:
        return prompt_version(self.rubric)


def classifier_context(settings: Settings) -> ClassifierContext:
    watchlist = load_watchlist(settings.config_dir)
    return ClassifierContext(
        rubric=load_rubric(settings.config_dir).classifier,
        params=load_llm_config(settings.config_dir).classify,
        model=settings.classifier_model,
        aliases=alias_map([(t.symbol, t.aliases) for t in watchlist.tickers]),
    )


# --------------------------------------------------------------------------- reads


def _pending_ids(conn: Connection, limit: int | None = None) -> list[int]:
    classified = select(event_classifications.c.event_id)
    parked = select(classify_state.c.event_id).where(classify_state.c.status != "retry")
    rows = conn.execute(
        select(events.c.id)
        .where(
            events.c.origin.in_(NEWS_ORIGINS),
            events.c.id.not_in(classified),
            events.c.id.not_in(parked),
        )
        .order_by(events.c.published_at.desc(), events.c.id.desc())
        .limit(limit)
    ).scalars()
    return list(rows)


def has_classifier_work(engine: Engine, *, batches_only: bool = False) -> bool:
    """Anything to classify (or, with `batches_only`, any submitted batch to poll)?"""
    with engine.connect() as conn:
        if batches_only:
            q = select(classify_state.c.event_id).where(
                classify_state.c.status == "batched", classify_state.c.batch_id.is_not(None)
            )
            return conn.execute(q.limit(1)).first() is not None
        return bool(_pending_ids(conn, limit=1))


def load_inputs(
    conn: Connection, event_ids: Sequence[int], aliases: Mapping[str, str]
) -> dict[int, EventInput]:
    if not event_ids:
        return {}
    tick: dict[int, list[str]] = defaultdict(list)
    for eid, sym in conn.execute(
        select(event_tickers.c.event_id, event_tickers.c.symbol)
        .where(event_tickers.c.event_id.in_(event_ids))
        .order_by(event_tickers.c.symbol)
    ):
        tick[eid].append(sym)
    out = {}
    for r in conn.execute(
        select(
            events.c.id,
            events.c.title,
            events.c.excerpt,
            events.c.source_domain,
            events.c.trust_tier,
            events.c.published_at,
        ).where(events.c.id.in_(event_ids))
    ):
        out[r.id] = EventInput(
            doc_id=f"event-{r.id}",
            title=r.title,
            excerpt=r.excerpt,
            domain=r.source_domain,
            tier=r.trust_tier,
            published_at=r.published_at,
            tickers=tuple((s, aliases.get(s, s)) for s in tick[r.id]),
        )
    return out


def _attempts(conn: Connection, event_ids: Sequence[int]) -> dict[int, int]:
    if not event_ids:
        return {}
    rows = conn.execute(
        select(classify_state.c.event_id, classify_state.c.attempts).where(
            classify_state.c.event_id.in_(event_ids)
        )
    )
    return {eid: n for eid, n in rows}


# --------------------------------------------------------------------------- writes


def write_classification(
    conn: Connection,
    event_id: int,
    *,
    cls: str,
    category: str,
    materiality_raw: int,
    direction: int,
    directions: Mapping[str, int],
    confidence: float,
    rationale: str,
    evidence_quote: str | None,
    rule_id: str | None,
    model: str | None,
    version: str | None,
    quarantine: bool,
    now: datetime,
) -> None:
    upsert(
        conn,
        event_classifications,
        [
            {
                "event_id": event_id,
                "class": cls,
                "category": category,
                "materiality_raw": materiality_raw,
                "materiality": materiality_raw,  # capped just below
                "direction": direction,
                "confidence": confidence,
                "rationale": rationale,
                "evidence_quote": evidence_quote,
                "rule_id": rule_id,
                "model": model,
                "prompt_version": version,
                "created_at": to_iso(now),
            }
        ],
        key_cols=["event_id"],
    )
    for symbol, d in directions.items():
        conn.execute(
            update(event_tickers)
            .where(event_tickers.c.event_id == event_id, event_tickers.c.symbol == symbol)
            .values(direction=d)
        )
    if quarantine:
        conn.execute(
            update(events)
            .where(events.c.id == event_id)
            .values(injection_suspected=1, quarantined=1)
        )
    apply_caps(conn, event_id)


def _write_rule(
    conn: Connection, event_id: int, ev: EventInput, hit: RuleHit, now: datetime
) -> None:
    write_classification(
        conn,
        event_id,
        cls=hit.cls,
        category=hit.category,
        materiality_raw=hit.materiality,
        direction=hit.direction,
        directions={s: hit.direction for s in ev.symbols},
        confidence=hit.confidence,
        rationale=hit.rationale,
        evidence_quote=ev.title[:300],
        rule_id=hit.rule_id,
        model=None,
        version=None,
        quarantine=False,
        now=now,
    )


def write_model_result(
    conn: Connection,
    event_id: int,
    c: Classification,
    *,
    model: str,
    version: str,
    now: datetime,
) -> None:
    write_classification(
        conn,
        event_id,
        cls=c.cls,
        category=c.category,
        materiality_raw=c.materiality_raw,
        direction=c.direction,
        directions=c.directions,
        confidence=c.confidence,
        rationale=c.rationale,
        evidence_quote=c.evidence_quote,
        rule_id=None,
        model=model,
        version=version,
        quarantine=c.injection_suspected,
        now=now,
    )
    _set_state(conn, event_id, status="done", error=None, now=now)


def _set_state(
    conn: Connection,
    event_id: int,
    *,
    status: str,
    error: str | None,
    now: datetime,
    attempts: int | None = None,
    custom_id: str | None = None,
    batch_id: str | None = None,
) -> None:
    row: dict[str, Any] = {
        "event_id": event_id,
        "status": status,
        "last_error": None if error is None else error[:500],
        "custom_id": custom_id,
        "batch_id": batch_id,
        "updated_at": to_iso(now),
    }
    update_cols = ["status", "last_error", "custom_id", "batch_id", "updated_at"]
    if attempts is not None:
        row["attempts"] = attempts
        update_cols.append("attempts")
    upsert(conn, classify_state, [row], key_cols=["event_id"], update_cols=update_cols)


def record_failure(
    engine: Engine, event_id: int, error: str, max_attempts: int, now: datetime
) -> str:
    """Count a failed attempt; `failed` once `max_attempts` is reached, else `retry`."""
    with write_tx(engine) as conn:
        done = conn.execute(
            select(classify_state.c.attempts).where(classify_state.c.event_id == event_id)
        ).scalar()
        attempts = int(done or 0) + 1
        status = "failed" if attempts >= max_attempts else "retry"
        _set_state(conn, event_id, status=status, error=error, now=now, attempts=attempts)
    log.info("classify event %d: attempt %d %s (%s)", event_id, attempts, status, error[:200])
    return status


# --------------------------------------------------------------------------- run


def apply_rules(engine: Engine, ctx: ClassifierContext, now: datetime) -> int:
    with engine.connect() as conn:
        ids = _pending_ids(conn)
        inputs = load_inputs(conn, ids, ctx.aliases)
    hits = []
    for eid in ids:
        ev = inputs[eid]
        hit = classify_news(ev.title, ev.domain, ev.tier, ctx.rubric)
        if hit is not None:
            hits.append((eid, ev, hit))
    if hits:
        with write_tx(engine) as conn:
            for eid, ev, hit in hits:
                _write_rule(conn, eid, ev, hit, now)
    return len(hits)


def _reset_stale_submits(engine: Engine, now: datetime) -> None:
    with write_tx(engine) as conn:
        conn.execute(
            update(classify_state)
            .where(
                classify_state.c.status == "batched",
                classify_state.c.batch_id.is_(None),
                classify_state.c.updated_at < to_iso(now - STALE_SUBMIT),
            )
            .values(status="retry", custom_id=None, updated_at=to_iso(now))
        )


def classify_one(
    engine: Engine,
    llm: LlmClient,
    ctx: ClassifierContext,
    event_id: int,
    ev: EventInput,
    system: str,
    now: datetime,
) -> str:
    """Classify one event synchronously. Returns 'done', 'retry' or 'failed'. Raises
    BudgetExceeded / LlmError for the caller to handle."""
    while True:
        msg = llm.complete(
            purpose="classify",
            model=ctx.model,
            system=system,
            messages=messages(ev),
            max_tokens=ctx.params.max_tokens,
            effort=ctx.params.effort,
            output_format=output_format(),
        )
        try:
            c = parse_and_validate(msg, ev, ctx.rubric)
        except InvalidOutput as exc:
            status = record_failure(engine, event_id, str(exc), ctx.params.max_attempts, now)
            if status == "failed":
                return status
            continue
        with write_tx(engine) as conn:
            write_model_result(
                conn,
                event_id,
                c,
                model=str(msg.get("model") or ctx.model),
                version=ctx.version,
                now=now,
            )
        return "done"


def submit_backlog(
    engine: Engine,
    llm: LlmClient,
    ctx: ClassifierContext,
    ids: Sequence[int],
    now: datetime,
) -> int:
    ids = list(ids)[: ctx.params.batch_max_items]
    with engine.connect() as conn:
        inputs = load_inputs(conn, ids, ctx.aliases)
        attempts = _attempts(conn, ids)
    plan = [(eid, f"cls-{eid}-a{attempts.get(eid, 0) + 1}") for eid in ids if eid in inputs]
    with write_tx(engine) as conn:
        for eid, cid in plan:
            _set_state(conn, eid, status="batched", error=None, now=now, custom_id=cid)
    requests = [
        (
            cid,
            batch_params(
                inputs[eid], ctx.rubric, ctx.model, ctx.params.max_tokens, ctx.params.effort
            ),
        )
        for eid, cid in plan
    ]
    try:
        batch_id = llm.submit_batch(requests, purpose="classify_batch")
    except LlmError as exc:
        with write_tx(engine) as conn:
            for eid, _cid in plan:
                _set_state(conn, eid, status="retry", error=str(exc), now=now)
        raise
    with write_tx(engine) as conn:
        conn.execute(
            update(classify_state)
            .where(classify_state.c.custom_id.in_([cid for _e, cid in plan]))
            .values(batch_id=batch_id)
        )
    log.info("classifier backlog: batch %s with %d events", batch_id, len(plan))
    return len(plan)


def run_classify(
    engine: Engine,
    llm: LlmClient | None,
    ctx: ClassifierContext,
    now: datetime | None = None,
) -> JobResult:
    now = now or datetime.now(UTC)
    by_rule = apply_rules(engine, ctx, now)
    _reset_stale_submits(engine, now)
    with engine.connect() as conn:
        ids = _pending_ids(conn)
    if not ids:
        return JobResult(rows_written=by_rule, provider="rules" if by_rule else None)
    if llm is None:
        return JobResult(
            rows_written=by_rule,
            provider="rules",
            warning=f"ANTHROPIC_API_KEY is not set; {len(ids)} items wait for the classifier",
        )
    if len(ids) > ctx.params.batch_threshold:
        n = submit_backlog(engine, llm, ctx, ids, now)
        return JobResult(rows_written=by_rule, provider="batch", warning=f"{n} items batched")

    system = system_prompt(ctx.rubric)
    with engine.connect() as conn:
        inputs = load_inputs(conn, ids, ctx.aliases)
    done = failed = errors_in_row = 0
    notes: list[str] = []
    for eid in ids[: ctx.params.max_per_run]:
        try:
            status = classify_one(engine, llm, ctx, eid, inputs[eid], system, now)
        except BudgetExceeded as exc:
            notes.append(f"stopped by the budget guard: {exc}")
            break
        except LlmError as exc:
            record_failure(engine, eid, str(exc), ctx.params.max_attempts, now)
            errors_in_row += 1
            if errors_in_row >= MAX_CONSECUTIVE_ERRORS:
                notes.append(f"stopped after {errors_in_row} API errors in a row: {exc}")
                break
            continue
        errors_in_row = 0
        if status == "done":
            done += 1
        elif status == "failed":
            failed += 1
    if failed:
        notes.append(f"{failed} failed validation")
    if done == 0 and errors_in_row >= MAX_CONSECUTIVE_ERRORS:
        raise RuntimeError("; ".join(notes))  # a failed job_runs row → job-failing alert
    return JobResult(
        rows_written=by_rule + done,
        provider=ctx.model,
        warning="; ".join(notes) if notes else None,
    )


def poll_batches(
    engine: Engine,
    llm: LlmClient,
    ctx: ClassifierContext,
    now: datetime | None = None,
) -> JobResult | None:
    """Ingest finished classifier batches. None when nothing is open (no job_runs row)."""
    now = now or datetime.now(UTC)
    with engine.connect() as conn:
        open_rows = conn.execute(
            select(
                classify_state.c.event_id, classify_state.c.batch_id, classify_state.c.custom_id
            ).where(classify_state.c.status == "batched", classify_state.c.batch_id.is_not(None))
        ).all()
    if not open_rows:
        return None
    written = 0
    pending: list[str] = []
    for batch_id in sorted({r.batch_id for r in open_rows}):
        status = llm.batch_status(batch_id)
        if status != "ended":
            pending.append(f"{batch_id}: {status}")
            continue
        by_cid = {r.custom_id: r.event_id for r in open_rows if r.batch_id == batch_id}
        with engine.connect() as conn:
            inputs = load_inputs(conn, list(by_cid.values()), ctx.aliases)
        for res in llm.batch_results(batch_id):
            eid = by_cid.pop(res.custom_id, None)
            if eid is None or eid not in inputs:
                continue
            if res.status != "succeeded" or res.message is None:
                record_failure(engine, eid, res.error or res.status, ctx.params.max_attempts, now)
                continue
            model = str(res.message.get("model") or ctx.model)
            llm.record_batch_result(purpose="classify_batch", model=model, message=res.message)
            try:
                c = parse_and_validate(res.message, inputs[eid], ctx.rubric)
            except InvalidOutput as exc:
                record_failure(engine, eid, str(exc), ctx.params.max_attempts, now)
                continue
            with write_tx(engine) as conn:
                write_model_result(conn, eid, c, model=model, version=ctx.version, now=now)
            written += 1
        for eid in by_cid.values():
            record_failure(
                engine, eid, "no result in the ended batch", ctx.params.max_attempts, now
            )
    return JobResult(
        rows_written=written,
        provider="batch",
        warning=f"still processing: {'; '.join(pending)}" if pending else None,
    )
