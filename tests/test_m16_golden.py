"""M16 no regressions (spec §11): Safe and Medium give byte-identical targets before and after."""

from __future__ import annotations

import json

from tests.m16_golden import FIXTURE, payload


def test_safe_and_medium_targets_are_unchanged_by_m16() -> None:
    expected = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert payload() == expected
