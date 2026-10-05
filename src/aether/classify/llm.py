"""Validate the classifier's structured output (spec S1: schema-validated, rejected when invalid,
never repaired).

Checks on top of the JSON schema:
- the category belongs to the class (spec §5.1);
- `evidence_quote` is a verbatim quote from the item's title or excerpt (whitespace and quote
  characters normalised for the comparison only), so the model can't invent evidence;
- `directions` cover exactly the event's tickers, each once;
- ranges and lengths (confidence 0-1, rationale and quote lengths).

`injection_suspected` is the model's flag OR the deterministic backstop
(`rubric.classifier.injection_patterns`).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from aether.classify.prompt import EventInput
from aether.config import CATEGORY_CLASS, ClassifierRubric

RATIONALE_MAX = 600  # the prompt asks for 300; anything far beyond is rejected, not cut
QUOTE_MAX = 300

_SPACE_RE = re.compile(r"\s+")
_QUOTES = str.maketrans(
    {"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2013": "-", "\u2014": "-"}
)


class InvalidOutput(ValueError):
    pass


class _Direction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str
    direction: Literal[-1, 0, 1]


class _Output(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    cls: Literal["SIGNAL", "NOISE", "RISK"] = Field(alias="class")
    category: str
    materiality: int = Field(ge=1, le=5)
    direction: Literal[-1, 0, 1]
    directions: list[_Direction]
    confidence: float = Field(ge=0, le=1)
    rationale: str = Field(min_length=1, max_length=RATIONALE_MAX)
    evidence_quote: str = Field(min_length=1, max_length=QUOTE_MAX)
    injection_suspected: bool


@dataclass(frozen=True)
class Classification:
    cls: str
    category: str
    materiality_raw: int
    direction: int
    directions: dict[str, int]
    confidence: float
    rationale: str
    evidence_quote: str
    injection_model: bool  # what the model said
    injection_backstop: bool  # what the regex backstop said

    @property
    def injection_suspected(self) -> bool:
        return self.injection_model or self.injection_backstop


def _norm(text: str) -> str:
    return _SPACE_RE.sub(" ", text.translate(_QUOTES)).strip()


def injection_backstop(text: str, rubric: ClassifierRubric) -> bool:
    return any(re.search(p, text) for p in rubric.injection_patterns)


def response_text(message: Mapping[str, Any]) -> str:
    stop = message.get("stop_reason")
    if stop == "refusal":
        raise InvalidOutput("the model refused (stop_reason refusal)")
    if stop == "max_tokens":
        raise InvalidOutput("output truncated (stop_reason max_tokens)")
    texts = [
        str(b.get("text", ""))
        for b in message.get("content") or []
        if isinstance(b, Mapping) and b.get("type") == "text"
    ]
    if not texts:
        raise InvalidOutput("no text block in the response")
    return "".join(texts)


def parse_and_validate(
    message: Mapping[str, Any], ev: EventInput, rubric: ClassifierRubric
) -> Classification:
    raw = response_text(message)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InvalidOutput(f"not JSON: {exc.msg}") from None
    try:
        out = _Output.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = ".".join(str(p) for p in first["loc"])
        raise InvalidOutput(f"schema: {loc}: {first['msg']}") from None
    if out.category not in CATEGORY_CLASS:
        raise InvalidOutput(f"unknown category {out.category!r}")
    if CATEGORY_CLASS[out.category] != out.cls:
        raise InvalidOutput(f"category {out.category} is not a {out.cls} category")
    symbols = [d.symbol for d in out.directions]
    if sorted(symbols) != sorted(ev.symbols) or len(set(symbols)) != len(symbols):
        raise InvalidOutput(f"directions must cover exactly {list(ev.symbols)}, got {symbols}")
    if _norm(out.evidence_quote) not in _norm(f"{ev.title}\n{ev.excerpt or ''}"):
        raise InvalidOutput("evidence_quote is not a verbatim quote from the item")
    return Classification(
        cls=out.cls,
        category=out.category,
        materiality_raw=out.materiality,
        direction=out.direction,
        directions={d.symbol: d.direction for d in out.directions},
        confidence=out.confidence,
        rationale=out.rationale.strip(),
        evidence_quote=out.evidence_quote.strip(),
        injection_model=out.injection_suspected,
        injection_backstop=injection_backstop(ev.text, rubric),
    )
