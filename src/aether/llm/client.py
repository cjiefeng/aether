"""The one Anthropic client wrapper (spec §10, M6). The only module allowed to import `anthropic`
(`scripts/check_llm_imports.py` runs in `make lint`).

Every call:
- is refused unless its model has a price in `config/llm.yaml` (cost must be computable);
- may carry tools only when `purpose` starts with `research`, and then only the web-search tool
  (S1: classification and synthesis get no tools);
- passes the **soft budget guard** first: today's (SGT) synchronous spend + the call's worst-case
  estimate must fit in `DAILY_LLM_BUDGET_USD`, otherwise `BudgetExceeded` is raised, no request is
  sent and a `budget_refused` row is logged. The hard cap is the Console workspace limit (S3);
- caches the static system prompt (`cache_control`);
- is logged as one `llm_calls` row (tokens, searches, cost; never prompt text), written in a short
  transaction **after** the response.

Calls whose `purpose` is in `OWN_BUDGET_PURPOSES` (the M12 universe review) must carry a
`RunBudget` and are checked against it instead; they are excluded from today's spend.

Calls whose `purpose` is in `ESCALATION_PURPOSES` (M13) also pass the escalation sub-budget:
today's escalation spend + the estimate must fit `ESCALATION_DAILY_BUDGET_USD`, otherwise
`EscalationBudgetExceeded` is raised. They still count toward the daily soft budget.

Message Batches (the research backfill, M6, and the classifier backlog, M7) bypass the daily soft
budget by owner decision (2026-10-05) and are excluded from today's spend; each result is still
logged with `batch = 1` at the batch price.

Redaction: the SDK and HTTP loggers are held at WARNING, and API errors are re-raised as
`LlmError` with the key scrubbed and no chained exception.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

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

log = logging.getLogger(__name__)

WEB_SEARCH_TOOL = "web_search_20260209"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
REQUEST_TIMEOUT_S = 300.0
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
    ) -> None:
        if settings.anthropic_api_key is None:
            raise LlmDisabled("ANTHROPIC_API_KEY is not set")
        self._key = settings.anthropic_api_key.get_secret_value()
        self._engine = engine
        self._cfg = cfg
        self._budget = settings.daily_llm_budget_usd
        self._escalation_budget = settings.escalation_daily_budget_usd
        self._now = clock or (lambda: datetime.now(UTC))
        kwargs: dict[str, Any] = {
            "api_key": self._key,
            "max_retries": 2,
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
    ) -> dict[str, Any]:
        """One Messages API call. Returns the response as a JSON dict. `output_format` is a
        structured-output format (`{"type": "json_schema", "schema": ...}`). With `run_budget`
        (own-budget purposes only) the call is checked against that cap, not the daily budget."""
        _check_tools(purpose, tools)
        price_for(self._cfg, model)  # UnknownModel before anything else
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
                self._log(purpose=purpose, model=model, status="budget_refused", error=detail)
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
                        purpose=purpose,
                        model=model,
                        status="budget_refused",
                        research_run_id=research_run_id,
                        error=detail,
                    )
                    raise EscalationBudgetExceeded(detail)
            spent = self.spent_today()
            if spent + estimate > self._budget:
                self._log(
                    purpose=purpose,
                    model=model,
                    status="budget_refused",
                    research_run_id=research_run_id,
                    error=f"spent ${spent} + estimate ${estimate} > budget ${self._budget}",
                )
                raise BudgetExceeded(
                    f"daily LLM budget: spent ${spent} + estimate ${estimate} > ${self._budget}"
                )
        params: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
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
        try:
            msg = self._client.beta.messages.create(**params)
        except anthropic.APIError as exc:
            detail = self._scrub(f"{type(exc).__name__}: {exc}")[:300]
            self._log(
                purpose=purpose,
                model=model,
                status="error",
                research_run_id=research_run_id,
                error=detail,
            )
            raise LlmError(detail) from None
        dump: dict[str, Any] = msg.model_dump(mode="json")
        cost = cost_usd(self._cfg, model, usage_from_dict(dump.get("usage")))
        if run_budget is not None:
            run_budget.spent += cost
        self._log(
            purpose=purpose,
            model=model,
            status="ok",
            cost=cost,
            message=dump,
            research_run_id=research_run_id,
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
        against `purpose` like synchronous calls."""
        for _cid, p in requests:
            _check_tools(purpose, p.get("tools"))
            price_for(self._cfg, str(p.get("model")))
        try:
            batch = self._client.messages.batches.create(
                requests=[
                    {"custom_id": cid, "params": dict(p)}  # type: ignore[typeddict-item]
                    for cid, p in requests
                ]
            )
        except anthropic.APIError as exc:
            raise LlmError(self._scrub(f"{type(exc).__name__}: {exc}")[:300]) from None
        return str(batch.id)

    def batch_status(self, batch_id: str) -> str:
        try:
            return str(self._client.messages.batches.retrieve(batch_id).processing_status)
        except anthropic.APIError as exc:
            raise LlmError(self._scrub(f"{type(exc).__name__}: {exc}")[:300]) from None

    def batch_results(self, batch_id: str) -> Iterator[BatchResult]:
        try:
            for r in self._client.messages.batches.results(batch_id):
                d: dict[str, Any] = r.model_dump(mode="json")
                res = d.get("result") or {}
                status = str(res.get("type", "errored"))
                err = None
                if status == "errored":
                    err = self._scrub(str(res.get("error"))[:300])
                yield BatchResult(str(d.get("custom_id")), status, res.get("message"), err)
        except anthropic.APIError as exc:
            raise LlmError(self._scrub(f"{type(exc).__name__}: {exc}")[:300]) from None

    def record_batch_result(
        self,
        *,
        purpose: str,
        model: str,
        message: Mapping[str, Any],
        research_run_id: int | None = None,
    ) -> Decimal:
        cost = cost_usd(self._cfg, model, usage_from_dict(message.get("usage")), batch=True)
        self._log(
            purpose=purpose,
            model=model,
            status="ok",
            cost=cost,
            message=message,
            batch=True,
            research_run_id=research_run_id,
        )
        return cost
