"""Monthly published targets and the daily rebalance plan (spec §6.6, §6.6.1, §9). No LLM;
nothing leaves the machine.

- `publish_targets`: per profile, the latest strategy run's recommended weights (base) → the
  research overlay (layer 1 filing rules; layer 2 stance multipliers when `weights` is given,
  which the worker always does) → `profile_targets`, with each name's adjustment chain
  and an input hash. Runs on the 1st of the month (10:30 SGT, with the review pack) and when the
  owner presses "Publish targets now" (`publish_targets` command, trigger `off_cycle`). Between
  publishes the targets don't move.
- `run_rebalance`: the daily plan against the latest published targets (07:10 SGT and after any
  holdings/settings/Tiger update). If nothing was ever published, it publishes first (bootstrap).
  If the latest backtest ran under a different `strategies.yaml` (backtest/profiles) than the run
  behind the published targets, it republishes at once (trigger `config_change`, issue #13): the
  monthly freeze stops market-driven churn, but a config edit is the owner's decision.

Reads happen first; writes are short `write_tx` blocks.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, Engine, select

from aether.config import PROFILES, StrategiesConfig, WeightsConfig
from aether.db.dialect import upsert
from aether.db.engine import write_tx
from aether.db.models import (
    events,
    prices_daily,
    profile_targets,
    rebalance_plans,
    strategy_runs,
    strategy_weights,
)
from aether.db.types import utcnow_iso
from aether.portfolio.holdings import read_holdings, read_settings, universe_symbols
from aether.portfolio.job import canon, run_config_digest
from aether.portfolio.overlay import apply_overlay, layer1_findings, stance_adjustments
from aether.portfolio.rebalance import PlanInputs, build_plan
from aether.runs import JobResult

ALGO_VERSION = "m10.1"
SGT = ZoneInfo("Asia/Singapore")


def sgt_today() -> date:
    return datetime.now(SGT).date()


def next_publish(after: date) -> date:
    """The 1st of the month after `after`."""
    return date(after.year + (after.month == 12), after.month % 12 + 1, 1)


# --------------------------------------------------------------------------- reads


@dataclass(frozen=True)
class LatestRun:
    id: int
    as_of: str
    input_hash: bytes
    summary: dict[str, Any]


def latest_run(conn: Connection) -> LatestRun | None:
    row = conn.execute(
        select(
            strategy_runs.c.id,
            strategy_runs.c.as_of,
            strategy_runs.c.input_hash,
            strategy_runs.c.summary,
        )
        .order_by(strategy_runs.c.id.desc())
        .limit(1)
    ).first()
    if row is None:
        return None
    return LatestRun(row.id, row.as_of, row.input_hash, json.loads(row.summary))


def _base_weights(conn: Connection, run_id: int, sid: str) -> dict[str, float]:
    return {
        s: w
        for s, w in conn.execute(
            select(strategy_weights.c.symbol, strategy_weights.c.weight)
            .where(strategy_weights.c.run_id == run_id, strategy_weights.c.strategy_id == sid)
            .order_by(strategy_weights.c.symbol)
        )
    }


@dataclass(frozen=True)
class Published:
    profile: str
    as_of: str
    published_at: str
    prices_as_of: str
    strategy_id: str | None
    base: dict[str, float]
    weights: dict[str, float]
    adjustments: dict[str, Any]
    trigger: str
    trigger_event_id: int | None


def latest_published(conn: Connection, profile: str) -> Published | None:
    r = conn.execute(
        select(profile_targets)
        .where(profile_targets.c.profile == profile)
        .order_by(profile_targets.c.as_of.desc())
        .limit(1)
    ).first()
    if r is None:
        return None
    return Published(
        r.profile,
        r.as_of,
        r.published_at,
        r.prices_as_of,
        r.strategy_id,
        json.loads(r.base_weights),
        json.loads(r.published_weights),
        json.loads(r.adjustments),
        r.trigger,
        r.trigger_event_id,
    )


def last_prices(conn: Connection, symbols: list[str]) -> dict[str, tuple[str, Decimal]]:
    out: dict[str, tuple[str, Decimal]] = {}
    for s in symbols:
        row = conn.execute(
            select(prices_daily.c.d, prices_daily.c.c)
            .where(prices_daily.c.symbol == s)
            .order_by(prices_daily.c.d.desc())
            .limit(1)
        ).first()
        if row is not None:
            out[s] = (row.d, Decimal(repr(row.c)))
    return out


# --------------------------------------------------------------------------- publish


def compute_targets(
    conn: Connection,
    run: LatestRun,
    config: StrategiesConfig,
    today: date,
    trigger: str,
    trigger_event_id: int | None,
    weights: WeightsConfig | None = None,
) -> list[dict[str, Any]]:
    """`profile_targets` rows for every profile (a profile with no qualifying strategy keeps its
    previous targets; with no previous targets it gets no row)."""
    sleeve = [s for s in universe_symbols(conn) if s != "QTUM"]
    findings = layer1_findings(conn, sleeve, today, config.overlay)
    stances = (
        []
        if weights is None
        else stance_adjustments(
            conn,
            sleeve,
            today,
            config.overlay,
            weights.conclusions.stance_max_age_days,
            weights.track_record,
        )
    )
    now = utcnow_iso()
    rows = []
    for profile in PROFILES:
        pp = config.profiles[profile]
        sid = run.summary["profiles"][profile].get("strategy_id")
        if sid:
            base = _base_weights(conn, run.id, sid)
            published, chain = apply_overlay(base, sleeve, pp.max_per_name, findings, stances)
            adjustments: dict[str, Any] = {"chain": chain, "note": None}
        else:
            prev = latest_published(conn, profile)
            if prev is None:
                continue
            base, published = prev.base, prev.weights
            adjustments = {
                **prev.adjustments,
                "note": "No qualifying strategy in the latest backtest; previous targets kept.",
            }
        digest = hashlib.sha256(
            canon(
                {
                    "algo": ALGO_VERSION,
                    "run": run.input_hash.hex(),
                    "profile": profile,
                    "base": base,
                    "findings": [f.evidence() for f in findings],
                    "stances": [a.evidence(0.0) for a in stances],
                    "overlay": config.overlay.model_dump(mode="json"),
                    "cap": pp.max_per_name,
                    "trigger": trigger,
                    "trigger_event_id": trigger_event_id,
                }
            ).encode()
        ).digest()
        rows.append(
            {
                "profile": profile,
                "as_of": today.isoformat(),
                "published_at": now,
                "prices_as_of": run.as_of,
                "strategy_run_id": run.id,
                "strategy_id": sid,
                "base_weights": canon(base),
                "published_weights": canon(published),
                "adjustments": canon(adjustments),
                "trigger": trigger,
                "trigger_event_id": trigger_event_id,
                "input_hash": digest,
            }
        )
    return rows


def publish_targets(
    engine: Engine,
    config: StrategiesConfig,
    *,
    today: date | None = None,
    trigger: str = "monthly",
    trigger_event_id: int | None = None,
    weights: WeightsConfig | None = None,
) -> JobResult:
    today = today or sgt_today()
    with engine.connect() as conn:
        run = latest_run(conn)
        if run is None:
            return JobResult(warning="no strategy run yet; nothing to publish")
        if trigger_event_id is not None and (
            conn.execute(select(events.c.id).where(events.c.id == trigger_event_id)).first() is None
        ):
            trigger_event_id = None
        rows = compute_targets(conn, run, config, today, trigger, trigger_event_id, weights)
    with write_tx(engine) as conn:
        upsert(conn, profile_targets, rows, key_cols=["profile", "as_of"])
    plan = run_plan(engine, config)
    return JobResult(rows_written=len(rows) + plan.rows_written, warning=plan.warning)


# --------------------------------------------------------------------------- plan


def plan_inputs(conn: Connection, config: StrategiesConfig, target: Published) -> PlanInputs | None:
    h = read_holdings(conn)
    if h.empty:
        return None
    s = read_settings(conn)
    symbols = sorted(set(universe_symbols(conn)) | set(h.positions))
    notes = [target.adjustments["note"]] if target.adjustments.get("note") else []
    return PlanInputs(
        profile=target.profile,
        targets_as_of=target.as_of,
        strategy_id=target.strategy_id,
        targets=target.weights,
        positions={k: v.shares for k, v in h.positions.items()},
        cash=h.cash,
        prices=last_prices(conn, symbols),
        whole_shares=s.whole_shares,
        new_cash_only=s.new_cash_only,
        cost_bps=Decimal(repr(config.backtest.cost_bps)),
        params=config.rebalance,
        notes=tuple(notes),
    )


def plan_hash(inp: PlanInputs) -> bytes:
    return hashlib.sha256(canon({"algo": ALGO_VERSION, **inp.hash_payload()}).encode()).digest()


def run_plan(engine: Engine, config: StrategiesConfig) -> JobResult:
    """The selected profile's plan against its latest published targets."""
    with engine.connect() as conn:
        profile = read_settings(conn).selected_profile
        target = latest_published(conn, profile)
        if target is None:
            return JobResult(warning=f"no published targets for {profile} yet; no plan")
        inp = plan_inputs(conn, config, target)
    if inp is None:
        return JobResult()  # no holdings saved: position features stay hidden
    plan = build_plan(inp)
    plan["published_at"] = target.published_at
    plan["trigger"] = target.trigger
    plan["next_publish"] = next_publish(date.fromisoformat(target.as_of)).isoformat()
    with write_tx(engine) as conn:
        upsert(
            conn,
            rebalance_plans,
            [
                {
                    "profile": target.profile,
                    "as_of": target.as_of,
                    "input_hash": plan_hash(inp),
                    "plan": canon(plan),
                    "created_at": utcnow_iso(),
                }
            ],
            key_cols=["profile", "as_of"],
        )
    return JobResult(rows_written=1)


def published_config_stale(conn: Connection) -> bool:
    """True when the latest run's backtest config differs from that of the run behind the latest
    published targets. A data-only change (new prices, same config) is not stale."""
    published_run = conn.execute(
        select(profile_targets.c.strategy_run_id)
        .order_by(profile_targets.c.as_of.desc(), profile_targets.c.published_at.desc())
        .limit(1)
    ).scalar()
    run = latest_run(conn)
    if published_run is None or run is None or published_run == run.id:
        return False
    published = run_config_digest(conn, published_run)
    return published is not None and published != run_config_digest(conn, run.id)


def run_rebalance(
    engine: Engine,
    config: StrategiesConfig,
    today: date | None = None,
    weights: WeightsConfig | None = None,
) -> JobResult:
    """Daily: bootstrap the first publish if none exists, republish after a config change, then
    refresh the plan."""
    with engine.connect() as conn:
        any_published = conn.execute(select(profile_targets.c.profile).limit(1)).first()
        stale = any_published is not None and published_config_stale(conn)
    if any_published is None:
        return publish_targets(engine, config, today=today, trigger="monthly", weights=weights)
    if stale:
        return publish_targets(
            engine, config, today=today, trigger="config_change", weights=weights
        )
    return run_plan(engine, config)
