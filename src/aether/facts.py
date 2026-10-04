"""Facts registry (spec §2.3): load `config/facts.yaml`, gate by status, sync to the DB,
render FACTS.md.

Only `signed_off` facts go into prompts as facts. Everything else is passed with an explicit
UNCONFIRMED label.

    python -m aether.facts render   # rewrite FACTS.md from config/facts.yaml
"""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import Connection

from aether.db.dialect import upsert
from aether.db.models import facts as facts_table
from aether.db.types import utcnow_iso

FactStatus = Literal["unverified", "verified_by_claude", "signed_off"]


class Fact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z0-9_]{3,64}$")
    claim: str = Field(min_length=1)
    sources: tuple[str, ...]
    retrieved_at: str | None
    status: FactStatus
    notes: str | None = None
    open_question: str | None = None  # what is still unverified; listed in FACTS.md

    @field_validator("sources")
    @classmethod
    def _http_only(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        for url in v:
            if not url.startswith(("https://", "http://")):
                raise ValueError(f"fact source must be http(s): {url!r}")
        return v

    @property
    def confirmed(self) -> bool:
        return self.status == "signed_off"


class FactsFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    facts: tuple[Fact, ...]

    @field_validator("facts")
    @classmethod
    def _unique_ids(cls, v: tuple[Fact, ...]) -> tuple[Fact, ...]:
        ids = [f.id for f in v]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate fact ids")
        return v


def load_facts(config_dir: Path) -> tuple[Fact, ...]:
    with (config_dir / "facts.yaml").open(encoding="utf-8") as fh:
        return FactsFile.model_validate(yaml.safe_load(fh)).facts


def render_for_prompt(facts: Sequence[Fact]) -> str:
    """Facts block for LLM context. Unconfirmed facts are labelled as such, never stated as fact."""
    lines = []
    for f in facts:
        label = "FACT" if f.confirmed else "UNCONFIRMED"
        lines.append(f"[{label} id={f.id} status={f.status}] {f.claim}")
    return "\n".join(lines)


def sync_facts(conn: Connection, facts: Sequence[Fact]) -> int:
    """Upsert the registry into the `facts` table (worker only, inside write_tx)."""
    now = utcnow_iso()
    rows = [
        {
            "id": f.id,
            "claim": f.claim,
            "source_urls": json.dumps(list(f.sources)),
            "retrieved_at": f.retrieved_at,
            "status": f.status,
            "notes": f.notes,
            "open_question": f.open_question,
            "synced_at": now,
        }
        for f in facts
    ]
    return upsert(conn, facts_table, rows, key_cols=["id"])


_STATUS_BADGE = {
    "unverified": "UNVERIFIED",
    "verified_by_claude": "VERIFIED BY CLAUDE (awaiting owner sign-off)",
    "signed_off": "SIGNED OFF",
}


def render_markdown(facts: Sequence[Fact]) -> str:
    out = [
        "# FACTS",
        "",
        "<!-- Generated from config/facts.yaml by `make facts`. Edit the YAML, not this file. -->",
        "",
        "Every factual claim seeded into Aether, with its source and verification status.",
        "Statuses: `unverified` → `verified_by_claude` → `signed_off` (owner, end of M3).",
        "Only `signed_off` facts reach LLM prompts as facts; the rest are labelled UNCONFIRMED.",
        "",
        "| ID | Claim | Sources | Retrieved | Status |",
        "|---|---|---|---|---|",
    ]
    for f in facts:
        srcs = "<br>".join(f.sources) if f.sources else "—"
        claim = f.claim.replace("|", "\\|")
        out.append(
            f"| `{f.id}` | {claim} | {srcs} | {f.retrieved_at or '—'} | {_STATUS_BADGE[f.status]} |"
        )
    out += ["", "## Notes", ""]
    for f in facts:
        if f.notes:
            out.append(f"- `{f.id}`: {f.notes}")
    out += ["", "## Open questions", ""]
    open_qs = [f for f in facts if not f.sources or f.open_question]
    out += [f"- `{f.id}`: {f.open_question or f.notes or f.claim}" for f in open_qs] or ["- None."]
    out.append("")
    return "\n".join(out)


def main(argv: Sequence[str]) -> int:
    if list(argv) != ["render"]:
        print("usage: python -m aether.facts render", file=sys.stderr)
        return 2
    from aether.config import get_settings

    facts = load_facts(get_settings().config_dir)
    Path("FACTS.md").write_text(render_markdown(facts), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
