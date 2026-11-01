"""Event predicates shared by the paths that act on events (issue #50).

A research result with no parseable date is stored with `published_at` = its retrieval time and
`raw.date_source = "retrieved"`. That date says when it was found, not when it happened (old SEC
filings and press releases looked new), so such an event is shown on the Feed but never drives a
decision: removal triggers (§6.7.2), escalations, off-cycle reviews. The overlay's hard rules read
EDGAR `filings`, not research events, so they're unaffected.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import ColumnElement, func

from aether.db.dialect import json_extract
from aether.db.models import events

RETRIEVED = "retrieved"


def dated_event(dialect_name: str) -> ColumnElement[Any]:
    """WHERE clause: the event has a real date (anything but `raw.date_source = 'retrieved'`)."""
    source = json_extract(events.c.raw, "$.date_source", dialect_name)
    return func.coalesce(source, "") != RETRIEVED
