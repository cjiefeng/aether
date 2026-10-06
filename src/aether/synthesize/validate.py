"""The citation validator (spec §6.2, S1): conclusions are schema-validated and evidence-bound.
An invalid answer is rejected, never repaired.

Rejected when:
- the response was refused or truncated, or isn't JSON matching the schema (pydantic, strict);
- the ticker or as_of don't match the request, or a range/length is out of bounds;
- any cited id isn't in the request's evidence map (quarantined events are never in it), or a
  thesis point cites nothing;
- a key date names a catalyst that isn't in the request;
- QTUM's conclusion or the theme tilt doesn't cite the theme decomposition (`X:theme`);
- the model flags `injection_suspected` (the conclusion is not stored; the failure is logged).

Error messages name only our own checks and ids that match the id grammar, so the retry prompt
never carries untrusted text.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from aether.synthesize.context import CORE, Context

ID_RE = re.compile(
    r"^(E\d{1,12}|C\d{1,12}|R:\d{1,12}|F:[a-z0-9_]{3,64}|S:[a-z_]{1,40}|"
    r"O:[A-Z][A-Z0-9.\-]{0,9}|K:([A-Z][A-Z0-9.\-]{0,9}|theme)|X:theme|"
    r"T:[A-Z][A-Z0-9.\-]{0,9})$"
)
DATE_RE = r"^\d{4}-\d{2}-\d{2}$"


class InvalidConclusion(ValueError):
    pass


class _Point(BaseModel):
    model_config = ConfigDict(extra="forbid")
    point: str = Field(min_length=1, max_length=600)
    evidence_ids: list[str] = Field(max_length=20)


class _KeyDate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    date: str = Field(pattern=DATE_RE)
    event: str = Field(min_length=1, max_length=300)
    catalyst_id: int | None


class _Justification(BaseModel):
    model_config = ConfigDict(extra="forbid")
    trigger: Literal["material_event", "score_threshold", "none"]
    evidence_ids: list[str] = Field(max_length=20)


class _Common(BaseModel):
    model_config = ConfigDict(extra="forbid")
    as_of: str
    confidence: float = Field(ge=0, le=1)
    horizon: Literal["12m", "36m"]
    one_line_verdict: str = Field(min_length=1, max_length=400)
    thesis: list[_Point] = Field(min_length=1, max_length=8)
    bear_case: list[_Point] = Field(max_length=8)
    what_would_change_my_mind: list[str] = Field(min_length=1, max_length=8)
    key_dates: list[_KeyDate] = Field(max_length=20)
    stance_change_justification: _Justification
    injection_suspected: bool


class _Ticker(_Common):
    ticker: str
    stance: Literal["ACCUMULATE", "HOLD", "TRIM", "AVOID"]


class _Theme(_Common):
    tilt: Literal["PURE_PLAYS", "NEUTRAL", "QTUM"]
    sleeve_note: str = Field(min_length=1, max_length=600)


@dataclass(frozen=True)
class Validated:
    stance: str  # the proposed stance (ticker) or tilt (theme)
    confidence: float
    horizon: str
    payload: dict[str, Any]  # the validated output, normalised
    cited: list[str]  # every cited id, in order, unique

    def cited_event_ids(self, ids: list[str] | None = None) -> list[int]:
        return [int(i[1:]) for i in (ids if ids is not None else self.cited) if i[0] == "E"]

    @property
    def justification_ids(self) -> list[str]:
        return list(self.payload["stance_change_justification"]["evidence_ids"])


def response_text(message: Mapping[str, Any]) -> str:
    stop = message.get("stop_reason")
    if stop == "refusal":
        raise InvalidConclusion("the model refused (stop_reason refusal)")
    if stop == "max_tokens":
        raise InvalidConclusion("output truncated (stop_reason max_tokens)")
    texts = [
        str(b.get("text", ""))
        for b in message.get("content") or []
        if isinstance(b, Mapping) and b.get("type") == "text"
    ]
    if not texts:
        raise InvalidConclusion("no text block in the response")
    return "".join(texts)


def _safe(i: str) -> str:
    return i if ID_RE.fullmatch(i) else "(malformed id)"


def validate(message: Mapping[str, Any], ctx: Context) -> Validated:
    raw = response_text(message)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InvalidConclusion(f"not JSON: {exc.msg}") from None
    model = _Theme if ctx.kind == "theme" else _Ticker
    try:
        out = model.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = ".".join(str(p) for p in first["loc"])
        raise InvalidConclusion(f"schema: {loc}: {first['msg']}") from None
    if out.injection_suspected:
        raise InvalidConclusion(
            "injection_suspected: the request's untrusted text tried to instruct the model"
        )
    if out.as_of != ctx.as_of:
        raise InvalidConclusion(f"as_of must be {ctx.as_of}")
    if isinstance(out, _Ticker) and out.ticker != ctx.symbol:
        raise InvalidConclusion(f"ticker must be {ctx.symbol}")

    cited: list[str] = []
    for i, pt in enumerate(out.thesis):
        if not pt.evidence_ids:
            raise InvalidConclusion(f"thesis point {i + 1} cites no evidence")
        cited += pt.evidence_ids
    for pt in out.bear_case:
        cited += pt.evidence_ids
    cited += out.stance_change_justification.evidence_ids
    unknown = sorted({i for i in cited if i not in ctx.evidence})
    if unknown:
        shown = ", ".join(_safe(i) for i in unknown[:5])
        raise InvalidConclusion(f"unknown or quarantined evidence ids: {shown}")
    for kd in out.key_dates:
        if kd.catalyst_id is not None and kd.catalyst_id not in ctx.catalyst_ids:
            raise InvalidConclusion(f"key date cites unknown catalyst {kd.catalyst_id}")
    thesis_ids = {i for pt in out.thesis for i in pt.evidence_ids}
    if (ctx.kind == "theme" or ctx.symbol == CORE) and "X:theme" not in thesis_ids:
        raise InvalidConclusion("the quantum-sleeve view must cite X:theme in the thesis")

    payload = out.model_dump(mode="json")
    stance = out.tilt if isinstance(out, _Theme) else out.stance
    payload["stance" if ctx.kind == "ticker" else "tilt"] = stance
    if ctx.kind == "theme" or ctx.symbol == CORE:
        payload["label"] = "quantum-sleeve view"
    return Validated(
        stance=stance,
        confidence=out.confidence,
        horizon=out.horizon,
        payload=payload,
        cited=list(dict.fromkeys(cited)),
    )
