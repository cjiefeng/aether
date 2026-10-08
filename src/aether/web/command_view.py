"""Turns a `commands` row into the inline status shown next to the button that queued it
(issue #18). Messages are derived from the row's status and the handler's `result` JSON, so
the worker's handlers don't need to know about the dashboard's wording.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Engine

from aether.db.commands import active_command

SGT = ZoneInfo("Asia/Singapore")

# A pending command older than this means the worker isn't picking work up.
PENDING_STALE = timedelta(minutes=5)
# Matches db.commands.ACTIVE_WINDOW: past this a running command is treated as stuck.
RUNNING_STALE = timedelta(hours=1)

# Kinds without per-request arguments: a second click while one is pending/running shows the
# existing command instead of queueing a duplicate (that would only burn the hourly limit).
DEDUPE_KINDS = frozenset(
    {
        "refresh_prices",
        "refresh_edgar",
        "research_sweep",
        "recompute_strategies",
        "sync_holdings",
        "test_alert",
        "universe_review",
        "universe_full_review",
    }
)

LABELS = {
    "ping": "Ping",
    "refresh_prices": "Price refresh",
    "refresh_edgar": "SEC filings refresh",
    "research_sweep": "Research sweep",
    "test_alert": "Test alert",
    "recompute_strategies": "Strategy recompute",
    "update_holdings": "Holdings update",
    "update_portfolio_settings": "Settings update",
    "sync_holdings": "Tiger sync",
    "publish_targets": "Target publish",
    "mark_catalyst": "Catalyst update",
    "synthesize": "Conclusion run",
    "universe_review": "Universe review",
    "universe_full_review": "Full re-evaluation",
}

# States the template knows. queued/running keep polling; the rest are final for this view.
POLLING = frozenset({"queued", "running"})


@dataclass(frozen=True)
class CommandStatus:
    state: str  # queued | running | done | warning | failed | stale | error
    message: str
    command_id: int | None = None
    detail: str | None = None
    reload: bool = False

    @property
    def polling(self) -> bool:
        return self.command_id is not None and self.state in POLLING

    @property
    def is_error(self) -> bool:
        return self.state in ("failed", "error")


def _rows(n: object) -> str:
    count = n if isinstance(n, int) else 0
    return "no new rows" if count == 0 else f"{count:,} row{'s' if count != 1 else ''} written"


def _done_message(kind: str, result: dict[str, Any]) -> tuple[str, bool]:
    """(message, offer reload) for a command whose handler returned normally."""
    rows = _rows(result.get("rows"))
    match kind:
        case "ping":
            return "Worker responded.", False
        case "refresh_prices":
            return f"Prices updated, {rows}.", True
        case "refresh_edgar":
            return f"SEC filings checked, {rows}.", True
        case "research_sweep":
            return f"Research sweep finished, {rows}.", True
        case "recompute_strategies":
            return f"Strategies recomputed, {rows}.", True
        case "test_alert":
            if result.get("channel") == "telegram":
                return "Test alert sent to Telegram.", True
            return "Test alert recorded on the dashboard (Telegram is off).", True
        case "update_holdings":
            return "Holdings saved." + (" Plan rebuilt." if result.get("replanned") else ""), True
        case "update_portfolio_settings":
            return "Settings saved." + (" Plan rebuilt." if result.get("replanned") else ""), True
        case "sync_holdings":
            return f"Synced from Tiger, {rows}.", True
        case "publish_targets":
            return "Targets published.", True
        case "mark_catalyst":
            return "Catalyst updated.", True
        case "universe_review":
            n = result.get("rows") if isinstance(result.get("rows"), int) else 0
            return f"Universe review done: {n} companies reviewed.", True
        case "universe_full_review":
            n = result.get("rows") if isinstance(result.get("rows"), int) else 0
            return f"Full re-evaluation done: {n} companies reviewed.", True
        case "synthesize":
            n = result.get("rows") if isinstance(result.get("rows"), int) else 0
            return (
                f"Conclusions run: {n} stored (see the job's warning for any held or failed).",
                True,
            )
    return "Done.", True


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def sgt_hm(ts: datetime) -> str:
    return ts.astimezone(SGT).strftime("%H:%M")


def status_for(row: Any, now: datetime | None = None) -> CommandStatus:
    """`row`: a `commands` row (id, kind, status, requested_at, result)."""
    now = now or datetime.now(UTC)
    label = LABELS.get(row.kind, row.kind.replace("_", " ").capitalize())
    age = now - _parse(row.requested_at)
    cid = int(row.id)
    try:
        result = json.loads(row.result) if row.result else {}
    except ValueError:
        result = {}
    if not isinstance(result, dict):
        result = {}
    error = str(result.get("error") or "")[:200] or None

    if row.status == "pending":
        if age > PENDING_STALE:
            return CommandStatus(
                "stale",
                f"{label} still waiting after {int(age.total_seconds() // 60)} min. "
                "Is the worker running?",
                cid,
            )
        return CommandStatus(
            "queued", f"{label} requested. The worker picks it up within 30 s.", cid
        )
    if row.status == "running":
        if age > RUNNING_STALE:
            return CommandStatus(
                "stale", f"{label} has been running for over an hour. It may be stuck.", cid
            )
        return CommandStatus("running", f"{label} running…", cid)
    if row.status == "rejected":
        return CommandStatus("failed", "The worker doesn't recognise this command.", cid)
    if row.status == "failed":
        return CommandStatus("failed", f"{label} failed.", cid, detail=error)
    # done: the handler returned, but some handlers report a soft failure in the result.
    if result.get("busy"):
        return CommandStatus(
            "warning",
            f"A scheduled {label.lower()} was already running; it covers this request.",
            cid,
            reload=True,
        )
    if result.get("ok") is False:
        return CommandStatus("failed", f"{label} didn't complete.", cid, detail=error)
    message, reload = _done_message(row.kind, result)
    return CommandStatus("done", message, cid, reload=reload)


def already_active(status: CommandStatus) -> CommandStatus:
    """Status shown when a duplicate click finds the same kind still in progress."""
    return replace(status, message=f"Already in progress. {status.message}")


def rate_limited(limit: int, resets_at: datetime) -> CommandStatus:
    return CommandStatus(
        "error",
        f"Hourly limit of {limit} requests reached. Try again at {sgt_hm(resets_at)} SGT.",
    )


def invalid(message: str) -> CommandStatus:
    return CommandStatus("error", f"{message[:300].rstrip('.')}. Fix it and save again.")


def active_status(ro_engine: Engine, *kinds: str) -> CommandStatus | None:
    """For page loads: the in-progress command of these kinds, so a reload keeps showing it."""
    row = active_command(ro_engine, kinds)
    return status_for(row) if row is not None else None
