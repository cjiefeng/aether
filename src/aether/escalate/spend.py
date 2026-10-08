"""Escalation spend (M13, spec §5.2.5): the sum of today's escalation calls, read by the LLM
wrapper's sub-budget guard, the escalation pass and the Ops page."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Connection, select

from aether.db.models import llm_calls
from aether.db.types import micros_sum, micros_to_decimal, to_iso
from aether.llm.pricing import ESCALATION_PURPOSES


def escalation_spent_since(conn: Connection, since: datetime) -> Decimal:
    total = conn.execute(
        select(micros_sum(llm_calls.c.cost_micros)).where(
            llm_calls.c.created_at >= to_iso(since),
            llm_calls.c.purpose.in_(ESCALATION_PURPOSES),
        )
    ).scalar_one()
    return micros_to_decimal(int(total))
