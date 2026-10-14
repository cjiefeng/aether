"""No regressions (spec §11): Safe gives byte-identical targets before and after M16 and M17.

The fixture was generated from the M15 code and also holds Medium's M15 targets. From M17 Medium is
bucketed (spec §6.5), so only the Safe slice is compared."""

from __future__ import annotations

import json

from tests.m16_golden import FIXTURE, payload


def test_safe_targets_are_unchanged_by_m16_and_m17() -> None:
    expected = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert payload() == {"safe": expected["safe"]}
