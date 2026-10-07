"""Issue #13: `profile_targets.trigger` gains `config_change` (targets republished because
`strategies.yaml` changed the backtest).

Revision ID: 0012_config_change_trigger
Revises: 0011_conclusions
Create Date: 2026-10-08
"""

from __future__ import annotations

from alembic import op

revision = "0012_config_change_trigger"
down_revision = "0011_conclusions"
branch_labels = None
depends_on = None

STRICT = {"sqlite_strict": True}
TRIGGERS_M6 = ("monthly", "off_cycle")
TRIGGERS_13 = (*TRIGGERS_M6, "config_change")


def _triggers(triggers: tuple[str, ...]) -> None:
    with op.batch_alter_table("profile_targets", recreate="always", table_kwargs=STRICT) as batch:
        batch.drop_constraint("ck_profile_targets_trigger", type_="check")
        batch.create_check_constraint(
            "ck_profile_targets_trigger",
            "trigger IN (" + ",".join(f"'{t}'" for t in triggers) + ")",
        )


def upgrade() -> None:
    _triggers(TRIGGERS_13)


def downgrade() -> None:
    # The old CHECK can't hold the new value; an owner-driven republish is closest to off-cycle.
    op.execute("UPDATE profile_targets SET trigger = 'off_cycle' WHERE trigger = 'config_change'")
    _triggers(TRIGGERS_M6)
