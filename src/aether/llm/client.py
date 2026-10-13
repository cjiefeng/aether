"""The one Anthropic client wrapper (spec §10, M6). The only module allowed to import `anthropic`
(`scripts/check_llm_imports.py` runs in `make lint`).

Every call:
- is refused unless its model has a price in `config/llm.yaml` (cost must be computable);
- may carry tools only when `purpose` starts with `research`, and then only the web-search tool
  (S1: classification and synthesis get no tools);
- passes the **soft budget guard** first: today's (SGT) synchronous spend + the call's worst-case
  estimate must fit in `DAILY_LLM_BUDGET_USD`, otherwise `BudgetExceeded` is raised, no request is
  sent and a `refused_budget` row is logged. The hard cap is the Console workspace limit (S3);
- caches only the static system prompt (`cache_control` on the system block, never in `messages`;
  M15). Web-search calls follow `research.cache_prefix` in `config/llm.yaml`;
- is logged as **one `llm_calls` row per outcome** (M15, owner rule 2026-10-13): ok, error (after
  retries), refusal, refused_budget, batch_submitted and one row per batch result item, with
  tokens, searches, cost, price-table version, attempts, latency, effort and a context link (the
  running job from `runs.current_job`, plus the call site's IDs). Never prompt or response text.
  The row is written in a short transaction **after** the response.

Calls whose `purpose` is in `OWN_BUDGET_PURPOSES` (the M12 universe review) must carry a
`RunBudget` and are checked against it instead; they are excluded from today's spend.

Calls whose `purpose` is in `ESCALATION_PURPOSES` (M13) also pass the escalation sub-budget:
today's escalation spend + the estimate must fit `ESCALATION_DAILY_BUDGET_USD`, otherwise
`EscalationBudgetExceeded` is raised. They still count toward the daily soft budget.

Message Batches (the research backfill, M6, and the classifier backlog, M7) bypass the daily soft
budget by owner decision (2026-10-05) and are excluded from today's spend; each result is still
logged with `batch = 1` at the batch price.

Retries: the SDK's own retries are off (`max_retries=0`) and `_retrying` retries connection
errors, 408, 409, 429 and 5xx with backoff, so `attempts` on the row is exact.

Redaction: the SDK and HTTP loggers are held at WARNING, and API errors are re-raised as
`LlmError` with the key scrubbed and no chained exception.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, TypeVar

import anthropic
from anthropic import Anthropic
from sqlalchemy import Engine, insert, select

from aether.config import LlmConfig, Settings
from aether.db.engine import write_tx
from aether.db.models import llm_calls
from aether.db.types import micros_sum, micros_to_decimal, to_iso
from aether.escalate.spend import escalation_spent_since
from aether.llm.pricing import (
    ESCALATION_PURPOSES,
    OWN_BUDGET_PURPOSES,
    cost_usd,
    estimate_usd,
    price_for,
    sgt_day_start,
    usage_from_dict,
)
from aether.runs import current_job

log = logging.getLogger(__name__)

WEB_SEARCH_TOOL = "web_search_20260209"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
REQUEST_TIMEOUT_S = 300.0
# `make eval` and dev scripts (M15): logged like any call, into the DB they run against.
EVAL_PURPOSE = "eval"
# Web-search evals keep the `research` prefix: the S1 tool gate allows tools only there.
RESEARCH_EVAL_PURPOSE = "research_eval"
DEV_LOG_PATH = "exports/llm-dev-log.csv"  # where throwaway-DB rows are appended (spec §10)
MAX_RETRIES = 2  # after the first attempt (the SDK default), so at most 3 attempts
RETRY_STATUSES = frozenset({408, 409, 429})
MAX_BACKOFF_S = 60.0
# `llm_calls.context` keys (M15). Scalars only, so nothing like holdings can be attached.
CONTEXT_KEYS = frozenset(
    {
        "symbol",
        "event_id",
        "job",
        "job_run_id",
        "escalation_id",
        "universe_review_id",
        "conclusion_id",
        "batch_id",
        "custom_id",
        "items",
        "track",
        "kind",
        "arm",
    }
)
T = TypeVar("T")
# Models that accept the server-side refusal fallback (`fallbacks: "default"`); others get no
# fallback parameter. The Batches API rejects it for every model.
FALLBACK_MODELS = frozenset({"claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"})

for _name in ("anthropic", "anthropic._base_client", "httpx", "httpx2", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)


class LlmError(RuntimeError):
    pass


class BudgetExceeded(LlmError):
    pass


class EscalationBudgetExceeded(BudgetExceeded):
    """The escalation sub-budget (M13) refused the call; the daily budget may still have room."""


class LlmDisabled(LlmError):
    pass


@dataclass
class RunBudget:
    """A per-run cap (M12 universe review, spec §6.7): calls carrying one skip the daily soft
    budget and are refused once `spent` + the call's worst-case estimate would pass `cap`."""

    cap: Decimal
    spent: Decimal = Decimal(0)
    # M15: merged into each call's `llm_calls.context` (e.g. universe_review_id, track).
    context: Mapping[str, Any] = field(default_factory=dict)

    def fits(self, estimate: Decimal) -> bool:
        return self.spent + estimate <= self.cap


def web_search_tool(max_uses: int, allowed_domains: Sequence[str]) -> dict[str, Any]:
    """Direct (no dynamic filtering) so the raw `web_search_result` blocks come back: research
    builds events only from those blocks, never from model prose."""
    return {
        "type": WEB_SEARCH_TOOL,
        "name": "web_search",
        "max_uses": max_uses,
        "allowed_domains": list(allowed_domains),
        "allowed_callers": ["direct"],
    }


def _check_tools(purpose: str, tools: Sequence[Mapping[str, Any]] | None) -> None:
    if not tools:
        return
    if not purpose.startswith("research"):
        raise ValueError(f"tools are not allowed for purpose {purpose!r} (S1)")
    for t in tools:
        if not str(t.get("type", "")).startswith("web_search_") or t.get("name") != "web_search":
            raise ValueError("research calls may use only the web-search tool")


def _max_searches(tools: Sequence[Mapping[str, Any]] | None) -> int:
    return sum(int(t.get("max_uses") or 0) for t in tools or ())


def _context(extra: Mapping[str, Any] | None) -> str:
    """The row's `context` JSON: the running job (if any) plus the call site's IDs."""
    ctx: dict[str, Any] = {}
    job = current_job.get()
    if job is not None:
        ctx["job"], ctx["job_run_id"] = job
    for k, v in (extra or {}).items():
        if k not in CONTEXT_KEYS:
            raise ValueError(f"unknown llm_calls context key {k!r}")
        if v is not None and not isinstance(v, str | int | float | bool):
            raise ValueError(f"llm_calls context {k!r} must be a scalar")
        ctx[k] = v
    return json.dumps(ctx, sort_keys=True)


def _retryable(exc: anthropic.APIError) -> bool:
    if isinstance(exc, anthropic.APIConnectionError):  # includes timeouts
        return True
    if isinstance(exc, anthropic.APIStatusError):
        return exc.status_code in RETRY_STATUSES or exc.status_code >= 500
    return False


def _backoff_s(exc: anthropic.APIError, attempt: int) -> float:
    if isinstance(exc, anthropic.APIStatusError):
        ra = exc.response.headers.get("retry-after")
        try:
            if ra is not None:
                return min(max(float(ra), 0.0), MAX_BACKOFF_S)
        except ValueError:
            pass
    return float(min(0.5 * 2**attempt, MAX_BACKOFF_S))


@dataclass(frozen=True)
class BatchResult:
    custom_id: str
    status: str  # succeeded | errored | canceled | expired
    message: dict[str, Any] | None
    error: str | None


class LlmClient:
    def __init__(
        self,
        engine: Engine,
        settings: Settings,
        cfg: LlmConfig,
        *,
        http_client: Any = None,
        clock: Any = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if settings.anthropic_api_key is None:
            raise LlmDisabled("ANTHROPIC_API_KEY is not set")
        self._key = settings.anthropic_api_key.get_secret_value()
        self._engine = engine
        self._cfg = cfg
        self._budget = settings.daily_llm_budget_usd
        self._escalation_budget = settings.escalation_daily_budget_usd
        self._now = clock or (lambda: datetime.now(UTC))
        self._sleep = sleep
        kwargs: dict[str, Any] = {
            "api_key": self._key,
            "max_retries": 0,  # `_retrying` counts attempts for the row
            "timeout": REQUEST_TIMEOUT_S,
        }
        if http_client is not None:
            kwargs["http_client"] = http_client
        self._client = Anthropic(**kwargs)

    # ------------------------------------------------------------------ helpers

    @property
    def budget_usd(self) -> Decimal:
        return self._budget

    def _scrub(self, text: str) -> str:
        return text.replace(self._key, "[REDACTED]") if self._key else text

    def spent_today(self) -> Decimal:
        since = to_iso(sgt_day_start(self._now()))
        with self._engine.connect() as conn:
            total = conn.execute(
                select(micros_sum(llm_calls.c.cost_micros)).where(
                    llm_calls.c.created_at >= since,
                    llm_calls.c.batch == 0,
                    llm_calls.c.purpose.not_in(OWN_BUDGET_PURPOSES),
                )
            ).scalar_one()
        return micros_to_decimal(int(total))

    def escalation_spent_today(self) -> Decimal:
        with self._engine.connect() as conn:
            return escalation_spent_since(conn, sgt_day_start(self._now()))

    def _retrying(self, fn: Callable[[], T]) -> tuple[T, int]:
        """`fn()` with up to MAX_RETRIES retries on transient errors; returns (result, attempts).
        A final failure raises `_Failed` carrying the attempt count."""
        attempt = 0
        while True:
            attempt += 1
            try:
                return fn(), attempt
            except anthropic.APIError as exc:
                if attempt > MAX_RETRIES or not _retryable(exc):
                    raise _Failed(
                        self._scrub(f"{type(exc).__name__}: {exc}")[:300], attempt
                    ) from None
                self._sleep(_backoff_s(exc, attempt))

    def _log(
        self,
        *,
        purpose: str,
        model: str,
        status: str,
        cost: Decimal = Decimal(0),
        message: Mapping[str, Any] | None = None,
        batch: bool = False,
        research_run_id: int | None = None,
        error: str | None = None,
        attempts: int = 1,
        latency_ms: int | None = None,
        effort: str | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> None:
        u = usage_from_dict((message or {}).get("usage"))
        with write_tx(self._engine) as conn:
            conn.execute(
                insert(llm_calls).values(
                    purpose=purpose[:64],
                    model=model,
                    input_tokens=u.tokens.input,
                    output_tokens=u.tokens.output,
                    cache_read_tokens=u.tokens.cache_read,
                    cache_write_tokens=u.tokens.cache_write,
                    web_searches=u.web_searches,
                    cost_micros=cost,
                    created_at=to_iso(self._now()),
                    status=status,
                    batch=int(batch),
                    request_id=(message or {}).get("id"),
                    research_run_id=research_run_id,
                    error=None if error is None else self._scrub(error)[:500],
                    stop_reason=(message or {}).get("stop_reason"),
                    attempts=attempts,
                    latency_ms=latency_ms,
                    effort=effort,
                    price_version=self._cfg.price_version,
                    context=_context(context),
                )
            )

    # ------------------------------------------------------------------ synchronous calls

    def complete(
        self,
        *,
        purpose: str,
        model: str,
        system: str,
        messages: Sequence[Mapping[str, Any]],
        max_tokens: int,
        tools: Sequence[Mapping[str, Any]] | None = None,
        effort: str | None = None,
        output_format: Mapping[str, Any] | None = None,
        research_run_id: int | None = None,
        run_budget: RunBudget | None = None,
        context: Mapping[str, Any] | None = None,
        cache_prefix: bool | None = None,
    ) -> dict[str, Any]:
        """One Messages API call. Returns the response as a JSON dict. `output_format` is a
        structured-output format (`{"type": "json_schema", "schema": ...}`). With `run_budget`
        (own-budget purposes only) the call is checked against that cap, not the daily budget.
        `context` (symbol, run IDs; see CONTEXT_KEYS) goes on the `llm_calls` row. `cache_prefix`
        overrides the cache marker on the system prompt (default: on, except web-search calls,
        which follow `research.cache_prefix`). A `refusal` stop reason is returned to the caller
        and logged with status `refusal`."""
        _check_tools(purpose, tools)
        if run_budget is not None and run_budget.context:
            context = {**run_budget.context, **(context or {})}
        _context(context)  # bad keys fail before any request
        price_for(self._cfg, model)  # UnknownModel before anything else
        logged: dict[str, Any] = {
            "purpose": purpose,
            "model": model,
            "effort": effort,
            "context": context,
        }
        estimate = estimate_usd(
            self._cfg,
            model,
            system=system,
            messages=messages,
            max_tokens=max_tokens,
            max_searches=_max_searches(tools),
        )
        if (purpose in OWN_BUDGET_PURPOSES) != (run_budget is not None):
            raise ValueError(f"purpose {purpose!r} and run_budget don't match")
        if run_budget is not None:
            if not run_budget.fits(estimate):
                detail = (
                    f"run spent ${run_budget.spent} + estimate ${estimate} > run cap "
                    f"${run_budget.cap}"
                )
                self._log(**logged, status="refused_budget", error=detail)
                raise BudgetExceeded(detail)
        else:
            if purpose in ESCALATION_PURPOSES:
                esc = self.escalation_spent_today()
                if esc + estimate > self._escalation_budget:
                    detail = (
                        f"escalation sub-budget: spent ${esc} + estimate ${estimate} > "
                        f"${self._escalation_budget}"
                    )
                    self._log(
                        **logged,
                        status="refused_budget",
                        research_run_id=research_run_id,
                        error=detail,
                    )
                    raise EscalationBudgetExceeded(detail)
            spent = self.spent_today()
            if spent + estimate > self._budget:
                self._log(
                    **logged,
                    status="refused_budget",
                    research_run_id=research_run_id,
                    error=f"spent ${spent} + estimate ${estimate} > budget ${self._budget}",
                )
                raise BudgetExceeded(
                    f"daily LLM budget: spent ${spent} + estimate ${estimate} > ${self._budget}"
                )
        if cache_prefix is None:
            cache_prefix = self._cfg.research.cache_prefix if tools else True
        sys_block: dict[str, Any] = {"type": "text", "text": system}
        if cache_prefix:
            # The only breakpoint: the static prefix (tools + system). Never after per-request
            # content, tool results or search results (M15).
            sys_block["cache_control"] = {"type": "ephemeral"}
        params: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": [sys_block],
            "messages": list(messages),
        }
        if model in FALLBACK_MODELS:
            params["betas"] = [FALLBACK_BETA]
            params["fallbacks"] = "default"
        if tools:
            params["tools"] = list(tools)
        output_config: dict[str, Any] = {}
        if effort:
            output_config["effort"] = effort
        if output_format:
            output_config["format"] = dict(output_format)
        if output_config:
            params["output_config"] = output_config
        t0 = time.monotonic()
        try:
            msg, attempts = self._retrying(lambda: self._client.beta.messages.create(**params))
        except _Failed as exc:
            self._log(
                **logged,
                status="error",
                research_run_id=research_run_id,
                error=exc.detail,
                attempts=exc.attempts,
                latency_ms=int((time.monotonic() - t0) * 1000),
            )
            raise LlmError(exc.detail) from None
        dump: dict[str, Any] = msg.model_dump(mode="json")
        cost = cost_usd(self._cfg, model, usage_from_dict(dump.get("usage")))
        if run_budget is not None:
            run_budget.spent += cost
        self._log(
            **logged,
            status="refusal" if dump.get("stop_reason") == "refusal" else "ok",
            cost=cost,
            message=dump,
            research_run_id=research_run_id,
            attempts=attempts,
            latency_ms=int((time.monotonic() - t0) * 1000),
        )
        return dump

    # ------------------------------------------------------------------ Message Batches

    def submit_batch(
        self,
        requests: Sequence[tuple[str, Mapping[str, Any]]],
        purpose: str = "research_backfill",
    ) -> str:
        """Submit `(custom_id, params)` requests. No budget guard (owner decisions 2026-10-05: the
        research backfill and the classifier backlog; see module doc). Tools in params are checked
        against `purpose` like synchronous calls. Logs one `batch_submitted` row ($0); each item's
        cost is logged when its result is recorded."""
        for _cid, p in requests:
            _check_tools(purpose, p.get("tools"))
            price_for(self._cfg, str(p.get("model")))
        models = sorted({str(p.get("model")) for _cid, p in requests})
        model = models[0] if len(models) == 1 else ",".join(models)
        effort = {str((p.get("output_config") or {}).get("effort")) for _c, p in requests}
        logged: dict[str, Any] = {
            "purpose": purpose,
            "model": model,
            "effort": effort.pop() if len(effort) == 1 and "None" not in effort else None,
        }
        t0 = time.monotonic()
        try:
            batch, attempts = self._retrying(
                lambda: self._client.messages.batches.create(
                    requests=[
                        {"custom_id": cid, "params": dict(p)}  # type: ignore[typeddict-item]
                        for cid, p in requests
                    ]
                )
            )
        except _Failed as exc:
            self._log(
                **logged,
                status="error",
                error=exc.detail,
                attempts=exc.attempts,
                latency_ms=int((time.monotonic() - t0) * 1000),
                batch=True,
                context={"items": len(requests)},
            )
            raise LlmError(exc.detail) from None
        self._log(
            **logged,
            status="batch_submitted",
            batch=True,
            attempts=attempts,
            latency_ms=int((time.monotonic() - t0) * 1000),
            context={"batch_id": str(batch.id), "items": len(requests)},
        )
        return str(batch.id)

    def batch_status(self, batch_id: str, purpose: str = "research_backfill") -> str:
        try:
            b, _n = self._retrying(lambda: self._client.messages.batches.retrieve(batch_id))
        except _Failed as exc:
            self._log_batch_api_error(purpose, batch_id, exc)
            raise LlmError(exc.detail) from None
        return str(b.processing_status)

    def batch_results(
        self, batch_id: str, purpose: str = "research_backfill"
    ) -> Iterator[BatchResult]:
        try:
            stream, _n = self._retrying(lambda: self._client.messages.batches.results(batch_id))
            for r in stream:
                d: dict[str, Any] = r.model_dump(mode="json")
                res = d.get("result") or {}
                status = str(res.get("type", "errored"))
                err = None
                if status == "errored":
                    err = self._scrub(str(res.get("error"))[:300])
                yield BatchResult(str(d.get("custom_id")), status, res.get("message"), err)
        except _Failed as exc:
            self._log_batch_api_error(purpose, batch_id, exc)
            raise LlmError(exc.detail) from None
        except anthropic.APIError as exc:  # while streaming the results
            failed = _Failed(self._scrub(f"{type(exc).__name__}: {exc}")[:300], 1)
            self._log_batch_api_error(purpose, batch_id, failed)
            raise LlmError(failed.detail) from None

    def _log_batch_api_error(self, purpose: str, batch_id: str, exc: _Failed) -> None:
        self._log(
            purpose=purpose,
            model="n/a",
            status="error",
            error=exc.detail,
            attempts=exc.attempts,
            batch=True,
            context={"batch_id": batch_id},
        )

    def record_batch_result(
        self,
        *,
        purpose: str,
        model: str,
        message: Mapping[str, Any],
        research_run_id: int | None = None,
        custom_id: str | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> Decimal:
        """Log one succeeded batch item (`batch_result`, batch price). Returns its cost."""
        cost = cost_usd(self._cfg, model, usage_from_dict(message.get("usage")), batch=True)
        self._log(
            purpose=purpose,
            model=model,
            status="batch_result",
            cost=cost,
            message=message,
            batch=True,
            research_run_id=research_run_id,
            context={**(context or {}), "custom_id": custom_id},
        )
        return cost

    def record_batch_failure(
        self,
        *,
        purpose: str,
        model: str,
        result: BatchResult,
        research_run_id: int | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> None:
        """Log one errored / canceled / expired batch item (`error`, $0: not billed)."""
        self._log(
            purpose=purpose,
            model=model,
            status="error",
            batch=True,
            research_run_id=research_run_id,
            error=result.error or result.status,
            context={**(context or {}), "custom_id": result.custom_id},
        )


class _Failed(Exception):
    """A call that failed after `attempts` attempts (internal to the wrapper)."""

    def __init__(self, detail: str, attempts: int) -> None:
        super().__init__(detail)
        self.detail = detail
        self.attempts = attempts
