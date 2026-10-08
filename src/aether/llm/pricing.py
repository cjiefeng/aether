"""LLM cost accounting (M6). Pure: no I/O. All arithmetic is `Decimal`; rows store micros.

Prices come from `config/llm.yaml`. A model missing there is refused before any call
(`UnknownModel`), because its cost couldn't be computed or budgeted. The one exception is a
server-side refusal fallback that ran on a model we don't list: it is priced at the most expensive
listed rates, so spend is never under-counted.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, time
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from aether.config import LlmConfig, ModelPrice

MTOK = Decimal(1_000_000)
SGT = ZoneInfo("Asia/Singapore")

# Purposes with their own per-run cap (M12 universe review, spec §6.7 "Cost control"). They're
# excluded from the daily soft budget so the monthly review can't starve classification.
OWN_BUDGET_PURPOSES = ("research_universe", "universe_proposal")


def sgt_day_start(now: datetime) -> datetime:
    """Start of the current Singapore day, in UTC: the soft budget resets at SGT midnight."""
    local = now.astimezone(SGT)
    return datetime.combine(local.date(), time(0), tzinfo=SGT).astimezone(UTC)


class UnknownModel(ValueError):
    pass


@dataclass(frozen=True)
class TokenCounts:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0


@dataclass(frozen=True)
class Usage:
    tokens: TokenCounts
    web_searches: int = 0
    # Per-iteration (model, tokens) when the API reports them (e.g. a refusal fallback ran).
    iterations: tuple[tuple[str | None, TokenCounts], ...] = field(default=())


def _int(d: Mapping[str, Any], key: str) -> int:
    v = d.get(key)
    return int(v) if isinstance(v, int) and not isinstance(v, bool) and v > 0 else 0


def _counts(d: Mapping[str, Any]) -> TokenCounts:
    return TokenCounts(
        input=_int(d, "input_tokens"),
        output=_int(d, "output_tokens"),
        cache_read=_int(d, "cache_read_input_tokens"),
        cache_write=_int(d, "cache_creation_input_tokens"),
    )


def usage_from_dict(usage: Mapping[str, Any] | None) -> Usage:
    """From a Messages API `usage` object (as a JSON dict)."""
    if not usage:
        return Usage(TokenCounts())
    stu = usage.get("server_tool_use") or {}
    iterations: list[tuple[str | None, TokenCounts]] = []
    for it in usage.get("iterations") or ():
        if isinstance(it, Mapping):
            model = it.get("model")
            iterations.append((model if isinstance(model, str) else None, _counts(it)))
    return Usage(
        tokens=_counts(usage),
        web_searches=_int(stu, "web_search_requests") if isinstance(stu, Mapping) else 0,
        iterations=tuple(iterations),
    )


def price_for(cfg: LlmConfig, model: str) -> ModelPrice:
    try:
        return cfg.prices[model]
    except KeyError:
        raise UnknownModel(f"no price for model {model!r} in config/llm.yaml") from None


def _max_price(cfg: LlmConfig) -> ModelPrice:
    ps = list(cfg.prices.values())
    return ModelPrice(
        input=max(p.input for p in ps),
        output=max(p.output for p in ps),
        cache_write=max(p.cache_write for p in ps),
        cache_read=max(p.cache_read for p in ps),
    )


def _token_cost(p: ModelPrice, t: TokenCounts) -> Decimal:
    return (
        Decimal(t.input) * p.input
        + Decimal(t.output) * p.output
        + Decimal(t.cache_read) * p.cache_read
        + Decimal(t.cache_write) * p.cache_write
    ) / MTOK


def cost_usd(cfg: LlmConfig, model: str, usage: Usage, *, batch: bool = False) -> Decimal:
    """Token cost (per iteration model when reported) + web searches. Batch tokens are discounted;
    searches never are (web-search docs: batch searches cost the same)."""
    if usage.iterations:
        tokens = sum(
            (
                _token_cost(cfg.prices.get(m or model) or _max_price(cfg), t)
                for m, t in usage.iterations
            ),
            Decimal(0),
        )
    else:
        tokens = _token_cost(price_for(cfg, model), usage.tokens)
    if batch:
        tokens *= cfg.batch_discount
    searches = Decimal(usage.web_searches) * cfg.web_search_per_1k / Decimal(1000)
    return (tokens + searches).quantize(Decimal("0.000001"))


def estimate_usd(
    cfg: LlmConfig,
    model: str,
    *,
    system: str,
    messages: Sequence[Mapping[str, Any]],
    max_tokens: int,
    max_searches: int = 0,
) -> Decimal:
    """Worst-case cost of one call, for the budget guard: the whole prompt as uncached input (at the
    cache-write rate, the dearest input rate), plus a per-search allowance of result tokens, all of
    `max_tokens` as output, and every allowed search."""
    p = price_for(cfg, model)
    prompt_chars = len(system) + len(json.dumps(list(messages), ensure_ascii=False))
    input_tokens = prompt_chars // cfg.chars_per_token + 1
    if max_searches:
        input_tokens += (
            cfg.research.est_base_input_tokens
            + max_searches * cfg.research.est_input_tokens_per_search
        )
    worst = TokenCounts(cache_write=input_tokens, output=max_tokens)
    searches = Decimal(max_searches) * cfg.web_search_per_1k / Decimal(1000)
    return (_token_cost(p, worst) + searches).quantize(Decimal("0.000001"))
