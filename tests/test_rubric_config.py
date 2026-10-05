"""The classifier rubric config (M7): strict, matches the spec category set, regexes compile."""

from __future__ import annotations

from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from aether.config import CATEGORY_CLASS, ClassifierRubric, load_llm_config, load_rubric
from aether.db.models import EVENT_CATEGORIES
from tests.conftest import CONFIG_DIR


def _raw() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load((CONFIG_DIR / "rubric.yaml").read_text("utf-8"))
    return data["classifier"]


def test_shipped_rubric_loads_and_matches_the_db_categories() -> None:
    rubric = load_rubric(CONFIG_DIR).classifier
    assert set(rubric.categories) == set(EVENT_CATEGORIES) == set(CATEGORY_CLASS)
    assert load_llm_config(CONFIG_DIR).classify.batch_threshold >= 1


def test_unknown_key_is_rejected() -> None:
    raw = _raw()
    raw["opinion"] = "speculative"
    with pytest.raises(ValidationError, match="opinion"):
        ClassifierRubric.model_validate(raw)


def test_category_class_mismatch_is_rejected() -> None:
    raw = _raw()
    raw["categories"]["analyst_rating"]["class"] = "SIGNAL"
    with pytest.raises(ValidationError, match="mismatch"):
        ClassifierRubric.model_validate(raw)


def test_missing_or_extra_category_is_rejected() -> None:
    raw = _raw()
    raw["categories"].pop("going_concern")
    with pytest.raises(ValidationError, match="going_concern"):
        ClassifierRubric.model_validate(raw)
    raw = _raw()
    raw["categories"]["hype"] = {"class": "NOISE", "materiality": [1, 1], "definition": "x" * 20}
    with pytest.raises(ValidationError, match="hype"):
        ClassifierRubric.model_validate(raw)


def test_bad_regex_is_rejected() -> None:
    raw = _raw()
    raw["injection_patterns"] = ["(unclosed"]
    with pytest.raises(ValidationError, match="bad regex"):
        ClassifierRubric.model_validate(raw)
