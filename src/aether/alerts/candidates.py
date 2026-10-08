"""What should alert right now (spec M3). Pure reads; nothing here writes or touches the network.

Each candidate carries a stable `dedupe_key`; the outbox (`alerts.dedupe_key UNIQUE`) makes every
alert fire at most once however often this runs.

- `risk_event:{event_id}`: a non-quarantined RISK event at or above the materiality threshold,
  published within the lookback window.
- `insider_cluster:{symbol}:{cluster start}`: ≥N insiders selling within the window (rubric), with
  a per-symbol cooldown of one window so a sliding cluster doesn't re-alert.
- `lockup:{accession}:T-{n}` / `earnings:{symbol}:{date}:T-{n}`: the tightest reminder that
  applies (T-7, then T-1); a missed day still alerts, a late first run doesn't send both.
- `job_failing:{job}:{first failure}` and later `job_recovered:{job}:{first failure}`.
- `off_cycle:{event_id}` (M5): a non-quarantined event at or above the publish threshold on a
  pure-play suggests an off-cycle review. Targets never change automatically; the owner decides
  with "Publish targets now".
- `llm_budget:{SGT date}` (M6): today's synchronous LLM spend reached the alert fraction (80%) of
  the daily soft budget. At 100% the LLM wrapper refuses calls.

Message text is plain text built from DB fields; Telegram gets no parse_mode, so nothing in a
filing title is interpreted.

M13 notification policy (spec §5.2.6): each candidate carries its `delivery`. RISK events below
`immediate_min_materiality`, insider clusters and reminders outside `immediate_reminder_days`
(T-7) are `digest` (one message at `digest_time`); everything else is `immediate`. `risk_event`,
`off_cycle_review` and `escalation` alerts for one event merge into one message in the outbox
(`dispatch.enqueue`); `label` is the tag a merged alert adds to that message's first line.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, Engine, select

from aether.config import SLEEVE_TYPES, AlertsConfig, RiskFlagParams
from aether.db.models import (
    alerts,
    earnings_calendar,
    event_classifications,
    event_tickers,
    events,
    facts,
    filings,
    job_runs,
    llm_calls,
    lockups,
    tickers,
)
from aether.db.types import micros_sum, micros_to_decimal, to_iso
from aether.llm.pricing import OWN_BUDGET_PURPOSES
from aether.providers.prices import US_EASTERN
from aether.risk.flags import cluster_in_window, load_sales

MAX_TEXT = 4096


@dataclass(frozen=True)
class AlertCandidate:
    kind: str
    dedupe_key: str
    text: str
    payload: dict[str, Any] = field(default_factory=dict)
    event_id: int | None = None
    delivery: str = "immediate"  # immediate | digest | dashboard_only (M13)
    label: str | None = None  # tag added to the event's merged message (M13)


def _clip(text: str, n: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def us_today(now: datetime) -> date:
    return now.astimezone(US_EASTERN).date()


def reminder_due(days_left: int, reminder_days: tuple[int, ...]) -> int | None:
    """The tightest T-N reminder that applies (None if the date is further out than every N)."""
    due = [n for n in reminder_days if days_left <= n]
    return min(due) if due else None


def reminder_delivery(n: int, cfg: AlertsConfig) -> str:
    """T-1 goes out at once; T-7 waits for the digest (spec §5.2.6)."""
    return "immediate" if n in cfg.immediate_reminder_days else "digest"


# --------------------------------------------------------------------------- RISK events


def risk_events(conn: Connection, cfg: AlertsConfig, now: datetime) -> list[AlertCandidate]:
    since = to_iso(now - timedelta(days=cfg.event_lookback_days))
    rows = conn.execute(
        select(
            events.c.id,
            events.c.title,
            events.c.url,
            events.c.published_at,
            events.c.trust_tier,
            event_classifications.c.category,
            event_classifications.c.materiality,
            event_classifications.c.rationale,
            event_tickers.c.symbol,
        )
        .join(event_classifications, event_classifications.c.event_id == events.c.id)
        .outerjoin(event_tickers, event_tickers.c.event_id == events.c.id)
        .where(
            event_classifications.c["class"] == "RISK",
            event_classifications.c.materiality >= cfg.risk_event_min_materiality,
            events.c.quarantined == 0,
            events.c.injection_suspected == 0,
            events.c.published_at >= since,
        )
        .order_by(events.c.published_at, events.c.id)
    ).all()
    symbols: dict[int, list[str]] = defaultdict(list)
    first: dict[int, Any] = {}
    for r in rows:
        if r.symbol:
            symbols[r.id].append(r.symbol)
        first.setdefault(r.id, r)
    out = []
    for eid, r in first.items():
        syms = ", ".join(sorted(symbols[eid])) or "—"
        lines = [
            f"RISK · {syms} · {r.category.replace('_', ' ')} (materiality {r.materiality}/5)",
            _clip(r.title, 300),
        ]
        if r.rationale:
            lines.append(_clip(r.rationale, 400))
        lines += [f"Published {r.published_at[:10]} · source tier {r.trust_tier}", r.url]
        out.append(
            AlertCandidate(
                kind="risk_event",
                dedupe_key=f"risk_event:{eid}",
                text="\n".join(lines),
                payload={
                    "symbols": sorted(symbols[eid]),
                    "category": r.category,
                    "materiality": r.materiality,
                },
                event_id=eid,
                delivery=(
                    "immediate" if r.materiality >= cfg.immediate_min_materiality else "digest"
                ),
            )
        )
    return out


# --------------------------------------------------------------------------- off-cycle review (M5)


def off_cycle_reviews(
    conn: Connection, cfg: AlertsConfig, now: datetime, min_materiality: int
) -> list[AlertCandidate]:
    since = to_iso(now - timedelta(days=cfg.event_lookback_days))
    rows = conn.execute(
        select(
            events.c.id,
            events.c.title,
            events.c.published_at,
            event_classifications.c["class"],
            event_classifications.c.category,
            event_classifications.c.materiality,
            event_tickers.c.symbol,
        )
        .join(event_classifications, event_classifications.c.event_id == events.c.id)
        .join(event_tickers, event_tickers.c.event_id == events.c.id)
        .join(tickers, tickers.c.symbol == event_tickers.c.symbol)
        .where(
            tickers.c.type.in_(SLEEVE_TYPES),
            event_classifications.c.materiality >= min_materiality,
            events.c.quarantined == 0,
            events.c.injection_suspected == 0,
            events.c.published_at >= since,
        )
        .order_by(events.c.published_at, events.c.id, event_tickers.c.symbol)
    ).all()
    out: dict[int, AlertCandidate] = {}
    for r in rows:
        if r.id in out:
            continue
        cls = r._mapping["class"]
        out[r.id] = AlertCandidate(
            kind="off_cycle_review",
            dedupe_key=f"off_cycle:{r.id}",
            text="\n".join(
                [
                    f"Off-cycle review suggested · {r.symbol} · {cls} "
                    f"{r.category.replace('_', ' ')} (materiality {r.materiality}/5)",
                    _clip(r.title, 300),
                    f"Published {r.published_at[:10]} · event #{r.id}",
                    "Targets are unchanged until you press Publish targets now on the "
                    "Holdings page.",
                ]
            ),
            payload={"symbol": r.symbol, "category": r.category, "materiality": r.materiality},
            event_id=r.id,
            label="off-cycle review suggested",
        )
    return list(out.values())


# --------------------------------------------------------------------------- insider clusters


def insider_clusters(engine: Engine, params: RiskFlagParams, now: datetime) -> list[AlertCandidate]:
    # Own connections, never nested: the worker's engine begins every transaction IMMEDIATE.
    today = us_today(now)
    window = params.insider_cluster_window_days
    cooldown_since = to_iso(now - timedelta(days=window))
    with engine.connect() as conn:
        syms = list(
            conn.execute(
                select(tickers.c.symbol).where(
                    tickers.c.type.in_(SLEEVE_TYPES), tickers.c.active == 1
                )
            ).scalars()
        )
        cooling = {
            key.split(":")[1]
            for key in conn.execute(
                select(alerts.c.dedupe_key).where(
                    alerts.c.kind == "insider_cluster", alerts.c.created_at >= cooldown_since
                )
            ).scalars()
        }
    out = []
    for sym in syms:
        if sym in cooling:
            continue
        c = cluster_in_window(
            load_sales(engine, sym, today - timedelta(days=window)),
            today,
            window,
            params.insider_cluster_min_insiders,
        )
        if c is None:
            continue
        names = _clip(", ".join(c.insiders), 600)
        out.append(
            AlertCandidate(
                kind="insider_cluster",
                dedupe_key=f"insider_cluster:{sym}:{c.start.isoformat()}",
                text="\n".join(
                    [
                        f"RISK · {sym} · insider selling cluster",
                        f"{c.n_insiders} insiders sold {c.shares:,} shares between "
                        f"{c.start.isoformat()} and {c.end.isoformat()} "
                        f"({c.plan_sales} of {c.sales} sales under 10b5-1 plans).",
                        f"Insiders: {names}",
                        "Source: SEC Form 4 filings (T1)",
                    ]
                ),
                payload={"symbol": sym, "start": c.start.isoformat(), "end": c.end.isoformat()},
                delivery="digest",
            )
        )
    return out


# --------------------------------------------------------------------------- reminders


def _fact_status_for(conn: Connection, url: str) -> tuple[str, str] | None:
    """(fact id, status) of a registry fact citing this URL, if any (lock-up gating)."""
    for fid, srcs, status in conn.execute(select(facts.c.id, facts.c.source_urls, facts.c.status)):
        if url in json.loads(srcs):
            return fid, status
    return None


def lockup_reminders(conn: Connection, cfg: AlertsConfig, now: datetime) -> list[AlertCandidate]:
    today = us_today(now)
    out = []
    for r in conn.execute(
        select(
            lockups.c.accession,
            lockups.c.symbol,
            lockups.c.prospectus_date,
            lockups.c.lockup_days,
            lockups.c.expiry_date,
            lockups.c.early_release_possible,
            filings.c.form,
            filings.c.url,
        )
        .join(filings, filings.c.accession == lockups.c.accession)
        .where(lockups.c.expiry_date >= today.isoformat())
    ).all():
        days_left = (date.fromisoformat(r.expiry_date) - today).days
        n = reminder_due(days_left, cfg.reminder_days)
        if n is None:
            continue
        fact = _fact_status_for(conn, r.url)
        if fact is None:
            fact_line = "Not in the facts registry: derived from the prospectus text only."
        elif fact[1] == "signed_off":
            fact_line = f"Fact {fact[0]}: signed off."
        else:
            fact_line = f"Fact {fact[0]}: UNCONFIRMED ({fact[1]}); owner sign-off pending."
        lines = [
            f"RISK · {r.symbol} · lock-up expiry in {days_left} day(s): {r.expiry_date}",
            f"{r.lockup_days}-day lock-up from the {r.form} dated {r.prospectus_date}."
            + (" The underwriters may release shares early." if r.early_release_possible else ""),
            "Lock-ups usually end at the open of the next trading day.",
            fact_line,
            r.url,
        ]
        out.append(
            AlertCandidate(
                kind="lockup_reminder",
                dedupe_key=f"lockup:{r.accession}:T-{n}",
                text="\n".join(lines),
                payload={"symbol": r.symbol, "expiry_date": r.expiry_date, "reminder": n},
                delivery=reminder_delivery(n, cfg),
            )
        )
    return out


def earnings_reminders(conn: Connection, cfg: AlertsConfig, now: datetime) -> list[AlertCandidate]:
    today = us_today(now)
    out = []
    for sym, d, source in conn.execute(
        select(earnings_calendar.c.symbol, earnings_calendar.c.date, earnings_calendar.c.source)
        .where(
            earnings_calendar.c.status == "scheduled",
            earnings_calendar.c.date >= today.isoformat(),
        )
        .order_by(earnings_calendar.c.date)
    ).all():
        days_left = (date.fromisoformat(d) - today).days
        n = reminder_due(days_left, cfg.reminder_days)
        if n is None:
            continue
        out.append(
            AlertCandidate(
                kind="earnings_reminder",
                dedupe_key=f"earnings:{sym}:{d}:T-{n}",
                text="\n".join(
                    [
                        f"Earnings · {sym} · scheduled {d} (in {days_left} day(s))",
                        f"Source: {source} calendar (unofficial; confirm on the company IR site).",
                    ]
                ),
                payload={"symbol": sym, "date": d, "reminder": n},
                delivery=reminder_delivery(n, cfg),
            )
        )
    return out


# --------------------------------------------------------------------------- job health


@dataclass(frozen=True)
class FailingJob:
    job: str
    first_failure: str  # started_at of the first failed run since the last ok run
    failures: int
    last_error: str | None


def _runs_by_job(conn: Connection) -> dict[str, list[Any]]:
    runs: dict[str, list[Any]] = defaultdict(list)
    for r in conn.execute(
        select(job_runs.c.job, job_runs.c.status, job_runs.c.started_at, job_runs.c.error)
        .where(job_runs.c.status != "running")
        .order_by(job_runs.c.id)
    ).all():
        runs[r.job].append(r)
    return runs


def failing_jobs(
    conn: Connection, hours: int, now: datetime, runs: dict[str, list[Any]] | None = None
) -> list[FailingJob]:
    """Jobs with no ok run since a failure that started more than `hours` ago (alerts + Ops)."""
    cutoff = now - timedelta(hours=hours)
    out = []
    for job, rs in sorted((runs if runs is not None else _runs_by_job(conn)).items()):
        last_ok = max((i for i, r in enumerate(rs) if r.status == "ok"), default=-1)
        failures = rs[last_ok + 1 :]
        if not failures or datetime.fromisoformat(failures[0].started_at) > cutoff:
            continue
        out.append(FailingJob(job, failures[0].started_at, len(failures), failures[-1].error))
    return out


def job_health(conn: Connection, cfg: AlertsConfig, now: datetime) -> list[AlertCandidate]:
    runs = _runs_by_job(conn)
    out: list[AlertCandidate] = []
    for f in failing_jobs(conn, cfg.job_failing_hours, now, runs):
        err = _clip(f.last_error or "no error recorded", 300)
        out.append(
            AlertCandidate(
                kind="job_failing",
                dedupe_key=f"job_failing:{f.job}:{f.first_failure}",
                text="\n".join(
                    [
                        f"Ops · job {f.job} failing for more than {cfg.job_failing_hours}h",
                        f"Failing since {f.first_failure}: {f.failures} failed run(s), no "
                        "successful run since.",
                        f"Last error: {err}",
                        "Dashboard data from this job is stale.",
                    ]
                ),
                payload={"job": f.job, "first_failure": f.first_failure},
            )
        )

    # Recovery: a job_failing alert whose job has since completed an ok run.
    recovered = set(
        conn.execute(select(alerts.c.dedupe_key).where(alerts.c.kind == "job_recovered")).scalars()
    )
    for payload in conn.execute(
        select(alerts.c.payload).where(alerts.c.kind == "job_failing")
    ).scalars():
        p = json.loads(payload)
        job, first = p.get("job"), p.get("first_failure")
        rkey = f"job_recovered:{job}:{first}"
        if not job or not first or rkey in recovered:
            continue
        ok_after = [r for r in runs.get(job, []) if r.status == "ok" and r.started_at > first]
        if ok_after:
            out.append(
                AlertCandidate(
                    kind="job_recovered",
                    dedupe_key=rkey,
                    text=f"Ops · job {job} recovered: ok run at {ok_after[0].started_at} "
                    f"(failing since {first}).",
                    payload={"job": job, "first_failure": first},
                )
            )
    return out


SGT = ZoneInfo("Asia/Singapore")


def llm_budget(
    conn: Connection, budget: Decimal, fraction: Decimal, now: datetime
) -> list[AlertCandidate]:
    """One alert per SGT day once synchronous spend reaches `fraction` of the soft budget (batch
    calls and the universe review's own-budget calls are excluded, as in the wrapper's guard)."""
    if budget <= 0:
        return []
    local = now.astimezone(SGT)
    since = datetime.combine(local.date(), time(0), tzinfo=SGT)
    spent = micros_to_decimal(
        int(
            conn.execute(
                select(micros_sum(llm_calls.c.cost_micros)).where(
                    llm_calls.c.created_at >= to_iso(since),
                    llm_calls.c.batch == 0,
                    llm_calls.c.purpose.not_in(OWN_BUDGET_PURPOSES),
                )
            ).scalar_one()
        )
    )
    if spent < budget * fraction:
        return []
    pct = int(spent / budget * 100)
    text = (
        f"LLM spend today (SGT) is ${spent:.2f}, {pct}% of the ${budget:.2f} soft budget. "
        "Calls stop at 100% until tomorrow. The Console workspace limit is the hard cap."
    )
    return [
        AlertCandidate(
            "llm_budget",
            f"llm_budget:{local.date().isoformat()}",
            text,
            {"spent": str(spent), "budget": str(budget)},
        )
    ]


def collect(
    engine: Engine,
    cfg: AlertsConfig,
    params: RiskFlagParams,
    now: datetime,
    off_cycle_min_materiality: int | None = None,
    llm_budget_alert: tuple[Decimal, Decimal] | None = None,
) -> list[AlertCandidate]:
    with engine.connect() as conn:
        out = [
            *risk_events(conn, cfg, now),
            *(
                off_cycle_reviews(conn, cfg, now, off_cycle_min_materiality)
                if off_cycle_min_materiality is not None
                else []
            ),
            *lockup_reminders(conn, cfg, now),
            *earnings_reminders(conn, cfg, now),
            *job_health(conn, cfg, now),
            *(llm_budget(conn, *llm_budget_alert, now) if llm_budget_alert else []),
        ]
    out += insider_clusters(engine, params, now)
    return [c if len(c.text) <= MAX_TEXT else _truncate(c) for c in out]


def _truncate(c: AlertCandidate) -> AlertCandidate:
    return replace(c, text=c.text[: MAX_TEXT - 1] + "…")
