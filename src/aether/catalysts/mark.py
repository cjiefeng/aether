"""Owner marks a catalyst (`mark_catalyst` command; spec S2 "mark catalyst"). Worker only.

Validated in the app (to show errors) and again here. A mark sets resolution `owner`; marking a
catalyst `upcoming` reopens it, after which the deterministic rules may resolve it again.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import Engine, select, update

from aether.db.engine import write_tx
from aether.db.models import catalysts, events
from aether.db.types import utcnow_iso


class CatalystMark(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    catalyst_id: int = Field(gt=0)
    status: Literal["upcoming", "hit", "slipped", "cancelled"]
    event_id: int | None = Field(default=None, gt=0)
    note: str | None = Field(default=None, max_length=200)

    @field_validator("note")
    @classmethod
    def _strip(cls, v: str | None) -> str | None:
        v = (v or "").strip()
        return v or None


def apply_mark(engine: Engine, mark: CatalystMark) -> dict[str, object]:
    now = utcnow_iso()
    with write_tx(engine) as conn:
        if (
            conn.execute(select(catalysts.c.id).where(catalysts.c.id == mark.catalyst_id)).first()
            is None
        ):
            raise ValueError(f"unknown catalyst {mark.catalyst_id}")
        if (
            mark.event_id is not None
            and conn.execute(select(events.c.id).where(events.c.id == mark.event_id)).first()
            is None
        ):
            raise ValueError(f"unknown event {mark.event_id}")
        reopen = mark.status == "upcoming"
        conn.execute(
            update(catalysts)
            .where(catalysts.c.id == mark.catalyst_id)
            .values(
                status=mark.status,
                resolution=None if reopen else "owner",
                resolved_at=None if reopen else now,
                resolved_by_event_id=None if reopen else mark.event_id,
                note=mark.note,
                updated_at=now,
            )
        )
    return {"catalyst_id": mark.catalyst_id, "status": mark.status}
