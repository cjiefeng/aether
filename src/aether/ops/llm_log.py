"""LLM expense log export (M15, spec §10). Read-only.

One line per `llm_calls` row, then a daily summary (Singapore days) by purpose and by cost
component. Metadata only: the export never includes prompt or response text, the row's `error`
text (it can echo request fragments), holdings, the account number or any secret.

Component costs are recomputed from the current price table (`config/llm.yaml`, its
`price_version` is stated in the summary). `other` is the recorded cost minus those components
(e.g. a refusal fallback billed on another model), so every summary row adds up to the recorded
cost exactly.

    python -m aether.ops.llm_log --from 2026-10-01 --to 2026-10-31 [--format csv|json]

writes to stdout (`make llm-log FROM= TO=` redirects it to `exports/`).
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, select

from aether.config import LlmConfig, ModelPrice, get_settings, load_llm_config
from aether.db.engine import make_ro_engine
from aether.db.models import llm_calls
from aether.db.types import micros_to_decimal, to_iso
from aether.llm.pricing import MTOK, SGT

MAX_RANGE_DAYS = 400
COLUMNS = (
    "id",
    "created_at",
    "purpose",
    "model",
    "status",
    "batch",
    "effort",
    "stop_reason",
    "attempts",
    "latency_ms",
    "input_tokens",
    "output_tokens",
    "cache_write_tokens",
    "cache_read_tokens",
    "web_searches",
    "cost_usd",
    "price_version",
    "request_id",
    "research_run_id",
    "context",
)
COMPONENTS = ("input", "output", "cache_write", "cache_read", "searches", "batch_discount", "other")
SUMMARY_COLUMNS = ("day", "purpose", "calls", *COMPONENTS, "cost_usd")
Q = Decimal("0.000001")


class RangeError(ValueError):
    pass


def parse_range(start: str | None, end: str | None, today: date) -> tuple[date, date]:
    """Inclusive SGT date range; defaults to the last 30 days. Raises `RangeError`."""
    try:
        d_to = date.fromisoformat(end) if end else today
        d_from = date.fromisoformat(start) if start else d_to - timedelta(days=29)
    except ValueError:
        raise RangeError("dates must be YYYY-MM-DD") from None
    if d_from > d_to:
        raise RangeError("'from' is after 'to'")
    if (d_to - d_from).days + 1 > MAX_RANGE_DAYS:
        raise RangeError(f"at most {MAX_RANGE_DAYS} days per export")
    return d_from, d_to


def _utc_bounds(d_from: date, d_to: date) -> tuple[str, str]:
    lo = datetime.combine(d_from, time(0), tzinfo=SGT).astimezone(UTC)
    hi = datetime.combine(d_to + timedelta(days=1), time(0), tzinfo=SGT).astimezone(UTC)
    return to_iso(lo), to_iso(hi)


def sgt_day(created_at: str) -> str:
    return datetime.fromisoformat(created_at).astimezone(SGT).date().isoformat()


def rows(engine: Engine, d_from: date, d_to: date) -> list[dict[str, Any]]:
    lo, hi = _utc_bounds(d_from, d_to)
    cols = [llm_calls.c[c] for c in COLUMNS if c != "cost_usd"] + [llm_calls.c.cost_micros]
    with engine.connect() as conn:
        found = conn.execute(
            select(*cols)
            .where(llm_calls.c.created_at >= lo, llm_calls.c.created_at < hi)
            .order_by(llm_calls.c.created_at, llm_calls.c.id)
        ).all()
    out: list[dict[str, Any]] = []
    for r in found:
        d = dict(r._mapping)
        cost = d.pop("cost_micros")
        d["cost_usd"] = micros_to_decimal(int(cost)) if not isinstance(cost, Decimal) else cost
        out.append({c: d[c] for c in COLUMNS})
    return out


def _zero_price() -> ModelPrice:
    z = Decimal(0)
    return ModelPrice.model_construct(input=z, output=z, cache_write=z, cache_read=z)


def components(row: dict[str, Any], cfg: LlmConfig) -> dict[str, Decimal]:
    """The row's cost split by component at the current price table; `other` closes the gap to
    the recorded cost."""
    p = cfg.prices.get(str(row["model"])) or _zero_price()
    c = {
        "input": Decimal(row["input_tokens"]) * p.input / MTOK,
        "output": Decimal(row["output_tokens"]) * p.output / MTOK,
        "cache_write": Decimal(row["cache_write_tokens"]) * p.cache_write / MTOK,
        "cache_read": Decimal(row["cache_read_tokens"]) * p.cache_read / MTOK,
        "searches": Decimal(row["web_searches"]) * cfg.web_search_per_1k / Decimal(1000),
    }
    tokens = c["input"] + c["output"] + c["cache_write"] + c["cache_read"]
    c["batch_discount"] = -tokens * (1 - cfg.batch_discount) if row["batch"] else Decimal(0)
    c = {k: v.quantize(Q) for k, v in c.items()}
    c["other"] = Decimal(row["cost_usd"]) - sum(c.values(), Decimal(0))
    return c


@dataclass(frozen=True)
class SummaryRow:
    day: str
    purpose: str
    calls: int
    parts: dict[str, Decimal]
    cost: Decimal

    def as_dict(self) -> dict[str, Any]:
        return {
            "day": self.day,
            "purpose": self.purpose,
            "calls": self.calls,
            **{k: str(self.parts[k]) for k in COMPONENTS},
            "cost_usd": str(self.cost),
        }


def _accumulate(
    data: Iterable[dict[str, Any]], cfg: LlmConfig, keys: Any
) -> dict[tuple[str, str], list[Any]]:
    acc: dict[tuple[str, str], list[Any]] = defaultdict(
        lambda: [0, dict.fromkeys(COMPONENTS, Decimal(0)), Decimal(0)]
    )
    for r in data:
        parts = components(r, cfg)
        for key in keys(r):
            a = acc[key]
            a[0] += 1
            for k in COMPONENTS:
                a[1][k] += parts[k]
            a[2] += Decimal(r["cost_usd"])
    return acc


def purpose_summary(data: Iterable[dict[str, Any]], cfg: LlmConfig, label: str) -> list[SummaryRow]:
    """By purpose over the whole range (day column = `label`), then a final `TOTAL` row."""
    acc = _accumulate(data, cfg, lambda r: ((label, str(r["purpose"])), (label, "TOTAL")))
    keys = sorted(acc, key=lambda k: (k[1] == "TOTAL", k[1]))
    return [SummaryRow(d, p, *acc[(d, p)]) for d, p in keys]


def daily_summary(data: Iterable[dict[str, Any]], cfg: LlmConfig) -> list[SummaryRow]:
    """By SGT day and purpose, then a per-day `TOTAL` and a final `ALL`/`TOTAL` row."""

    def keys(r: dict[str, Any]) -> tuple[tuple[str, str], ...]:
        day = sgt_day(str(r["created_at"]))
        return ((day, str(r["purpose"])), (day, "TOTAL"), ("ALL", "TOTAL"))

    acc = _accumulate(data, cfg, keys)

    def order(k: tuple[str, str]) -> tuple[int, str, int, str]:
        day, purpose = k
        return (day == "ALL", day, purpose == "TOTAL", purpose)

    return [SummaryRow(d, p, *acc[(d, p)]) for d, p in sorted(acc, key=order)]


def _cell(v: Any) -> Any:
    return "" if v is None else str(v)


def to_csv(
    data: Sequence[dict[str, Any]], summary: Sequence[SummaryRow], meta: dict[str, str]
) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(COLUMNS)
    for r in data:
        w.writerow([_cell(r[c]) for c in COLUMNS])
    w.writerow([])
    w.writerow(
        [
            f"# daily summary (Singapore days) {meta['from']} to {meta['to']}; component costs at "
            f"price table {meta['price_version']}; the Console bills at actual rates"
        ]
    )
    w.writerow(SUMMARY_COLUMNS)
    for s in summary:
        d = s.as_dict()
        w.writerow([d[c] for c in SUMMARY_COLUMNS])
    return buf.getvalue()


def to_json(
    data: Sequence[dict[str, Any]], summary: Sequence[SummaryRow], meta: dict[str, str]
) -> str:
    return json.dumps(
        {
            **meta,
            "rows": [{c: _json_val(c, r[c]) for c in COLUMNS} for r in data],
            "summary": [s.as_dict() for s in summary],
        },
        indent=1,
        sort_keys=True,
    )


def _json_val(col: str, v: Any) -> Any:
    if col == "context":
        return json.loads(v) if v else {}
    if isinstance(v, Decimal):
        return str(v)
    return v


def export(engine: Engine, cfg: LlmConfig, d_from: date, d_to: date, fmt: str = "csv") -> str:
    data = rows(engine, d_from, d_to)
    summary = daily_summary(data, cfg)
    meta = {"from": d_from.isoformat(), "to": d_to.isoformat(), "price_version": cfg.price_version}
    return to_json(data, summary, meta) if fmt == "json" else to_csv(data, summary, meta)


def append_dev_log(engine: Engine, path: Path) -> int:
    """Append every `llm_calls` row of a throwaway DB (evals, dev scripts) to `path` as CSV, so
    their spend survives the temp DB (spec §10). Returns the number of rows written."""
    data = rows(engine, date(2000, 1, 1), date(2999, 12, 31))
    if not data:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists() or path.stat().st_size == 0
    with path.open("a", encoding="utf-8", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        if new:
            w.writerow(COLUMNS)
        for r in data:
            w.writerow([_cell(r[c]) for c in COLUMNS])
    return len(data)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Export the LLM expense log (metadata only).")
    ap.add_argument("--from", dest="d_from", default=None)
    ap.add_argument("--to", dest="d_to", default=None)
    ap.add_argument("--format", choices=("csv", "json"), default="csv")
    args = ap.parse_args(argv)
    settings = get_settings()
    try:
        d_from, d_to = parse_range(args.d_from, args.d_to, datetime.now(SGT).date())
    except RangeError as exc:
        print(f"llm-log: {exc}", file=sys.stderr)
        return 2
    engine = make_ro_engine(settings.db_path)
    try:
        sys.stdout.write(
            export(engine, load_llm_config(settings.config_dir), d_from, d_to, args.format)
        )
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
