"""Synthesis context (spec §6.2). Reads only; builds the structured evidence one conclusion run
may use, and the id → label map its citations are checked against.

Every item has a string id the model may cite:

| id            | what                                                      | trust            |
|---------------|-----------------------------------------------------------|------------------|
| `E<id>`       | a classified, non-quarantined event (last N days)         | text: untrusted  |
| `R:<id>`      | the market reaction to event `<id>` (computed)            | computed         |
| `C<id>`       | a catalyst (seed/earnings/lock-up titles are Aether's own)| internal         |
| `F:<fact_id>` | a registry fact, labelled FACT or UNCONFIRMED by status   | registry         |
| `S:<comp>`    | a scorecard component (computed)                          | computed         |
| `O:<sym>`     | the options panel (computed; research only)               | computed         |
| `X:theme`     | the QTUM theme decomposition (computed)                   | computed         |
| `K:<sym>`     | the ticker's own track record (computed)                  | computed         |
| `T:<sym>`     | (theme run) a ticker's current stance (computed record)   | computed         |

Event titles, excerpts and classifier rationales are ingested (or derived from ingested) text, so
they go inside one `wrap_untrusted` block (S1). Everything else is computed by Aether or comes
from the facts registry. Never in context: holdings, cash, the account, the mandate, config
commentary or opinions.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from sqlalchemy import Connection, Engine, select

from aether.config import ConclusionParams, TrackRecordParams
from aether.db.models import (
    catalysts,
    conclusions,
    event_classifications,
    event_reactions,
    event_tickers,
    events,
    facts,
    tickers,
)
from aether.facts import Fact, render_for_prompt
from aether.options.view import options_panel
from aether.score import track_record as tr
from aether.score.view import latest_scorecard, theme_card
from aether.security.untrusted import wrap_untrusted
from aether.synthesize.hysteresis import CitedEvent

CORE = "QTUM"
TEXT_MAX = 300  # per untrusted field
QUANTUM_SLEEVE = "quantum-sleeve view"


@dataclass
class Context:
    kind: str  # 'ticker' | 'theme'
    symbol: str | None
    name: str | None
    as_of: str
    text: str  # the user-message body (sections; untrusted text wrapped)
    evidence: dict[str, dict[str, Any]] = field(default_factory=dict)
    events: dict[int, CitedEvent] = field(default_factory=dict)  # for hysteresis
    catalyst_ids: set[int] = field(default_factory=set)
    previous_stance: str | None = None

    def digest(self, model: str, prompt_version: str) -> bytes:
        material = json.dumps(
            {"model": model, "prompt": prompt_version, "text": self.text}, sort_keys=True
        )
        return hashlib.sha256(material.encode()).digest()


def _clip(s: str | None, n: int = TEXT_MAX) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _dump(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


# --------------------------------------------------------------------------- pieces


def _events(
    conn: Connection, symbol: str | None, as_of: date, p: ConclusionParams
) -> list[dict[str, Any]]:
    start = f"{(as_of - timedelta(days=p.context_days)).isoformat()}T00:00:00Z"
    end = f"{as_of.isoformat()}T23:59:59Z"
    q = (
        select(
            events.c.id,
            events.c.title,
            events.c.excerpt,
            events.c.url,
            events.c.published_at,
            events.c.source_domain,
            events.c.trust_tier,
            events.c.independent_source_count,
            event_classifications.c["class"],
            event_classifications.c.category,
            event_classifications.c.materiality,
            event_classifications.c.direction,
            event_classifications.c.confidence,
            event_classifications.c.rationale,
        )
        .join(event_classifications, event_classifications.c.event_id == events.c.id)
        .where(
            events.c.quarantined == 0,
            events.c.published_at >= start,
            events.c.published_at <= end,
        )
    )
    if symbol is not None:
        q = q.add_columns(event_tickers.c.direction.label("ticker_direction")).join(
            event_tickers,
            (event_tickers.c.event_id == events.c.id) & (event_tickers.c.symbol == symbol),
        )
    rows = [dict(r._mapping) for r in conn.execute(q.order_by(events.c.published_at, events.c.id))]
    keep = [r for r in rows if r["materiality"] >= p.keep_min_materiality]
    keep.sort(key=lambda r: (r["published_at"], r["id"]), reverse=True)  # newest first ...
    keep.sort(key=lambda r: -r["materiality"])  # ... within each materiality (stable sort)
    keep = keep[: p.max_events]
    ids = {r["id"] for r in keep}
    rest = [r for r in reversed(rows) if r["id"] not in ids]
    chosen = keep + rest[: max(0, p.max_events - len(keep))]
    chosen.sort(key=lambda r: (r["published_at"], r["id"]))
    for r in chosen:
        td = r.pop("ticker_direction", None)
        r["direction"] = td if td is not None else r["direction"]
    return chosen


def _event_sections(ctx: Context, rows: Sequence[dict[str, Any]], doc_id: str) -> list[str]:
    if not rows:
        return ["## Events (last period)\nNone."]
    meta = [
        "id | published | class | category | materiality | direction | confidence | tier | "
        "independent sources | domain"
    ]
    texts = []
    for r in rows:
        eid = f"E{r['id']}"
        meta.append(
            f"{eid} | {r['published_at'][:10]} | {r['class']} | {r['category']} | "
            f"{r['materiality']} | {r['direction']:+d} | {r['confidence']:.2f} | "
            f"{r['trust_tier']} | {r['independent_source_count']} | {r['source_domain']}"
        )
        texts.append(
            f"{eid}\nTitle: {_clip(r['title'])}\nExcerpt: {_clip(r['excerpt'])}\n"
            f"Classifier rationale: {_clip(r['rationale'])}"
        )
        ctx.evidence[eid] = {
            "type": "event",
            "ref": r["id"],
            "label": _clip(r["title"], 120),
            "url": r["url"],
            "date": r["published_at"][:10],
        }
        ctx.events[r["id"]] = CitedEvent(
            r["id"],
            r["class"],
            r["materiality"],
            r["trust_tier"],
            r["published_at"],
            False,
        )
    return [
        "## Events (classified, non-quarantined; metadata computed by Aether)\n" + "\n".join(meta),
        "## Event text (ingested, untrusted)\n" + wrap_untrusted(doc_id, "\n\n".join(texts)),
    ]


def _reactions(conn: Connection, ctx: Context, symbol: str, event_ids: set[int]) -> str:
    rows = conn.execute(
        select(event_reactions)
        .where(
            event_reactions.c.symbol == symbol,
            event_reactions.c.event_id.in_(sorted(event_ids)),
            event_reactions.c.status.in_(("complete", "pending")),
        )
        .order_by(event_reactions.c.t0, event_reactions.c.event_id)
    ).all()
    lines = []
    for r in rows:
        rid = f"R:{r.event_id}"
        item = {
            "t0": r.t0,
            "benchmark": r.benchmark,
            "car_1": r.car_1,
            "car_5": r.car_5,
            "car_20": r.car_20,
            "z_1": r.z_1,
            "z_5": r.z_5,
            "abn_volume": r.abn_volume,
            "status": r.status,
        }
        lines.append(f"{rid} (reaction to E{r.event_id}): {_dump(item)}")
        ctx.evidence[rid] = {
            "type": "reaction",
            "ref": r.event_id,
            "label": f"reaction to #{r.event_id}",
        }
    body = "\n".join(lines) if lines else "None."
    return "## Market reactions (computed; abnormal returns vs benchmark)\n" + body


def _catalysts(conn: Connection, ctx: Context, symbol: str | None, as_of: date) -> str:
    q = select(
        catalysts.c.id,
        catalysts.c.symbol,
        catalysts.c.title,
        catalysts.c.kind,
        catalysts.c.window_start,
        catalysts.c.window_end,
        catalysts.c.status,
        catalysts.c.fact_id,
        facts.c.status.label("fact_status"),
    ).outerjoin(facts, facts.c.id == catalysts.c.fact_id)
    since = (as_of - timedelta(days=365)).isoformat()
    q = q.where((catalysts.c.status == "upcoming") | (catalysts.c.resolved_at >= since))
    if symbol is not None:
        q = q.where((catalysts.c.symbol == symbol) | catalysts.c.symbol.is_(None))
    lines = []
    for r in conn.execute(q.order_by(catalysts.c.window_start, catalysts.c.id)):
        cid = f"C{r.id}"
        label = "" if r.fact_status in (None, "signed_off") else " [UNCONFIRMED fact]"
        lines.append(
            f"{cid}: {r.symbol or 'theme'} | {r.title}{label} | {r.kind} | "
            f"{r.window_start} to {r.window_end or 'open'} | status {r.status}"
            + (f" | fact {r.fact_id}" if r.fact_id else "")
        )
        ctx.evidence[cid] = {"type": "catalyst", "ref": r.id, "label": r.title}
        ctx.catalyst_ids.add(r.id)
    return "## Catalysts\n" + ("\n".join(lines) if lines else "None.")


def _facts(ctx: Context, all_facts: Sequence[Fact], symbol: str | None, extra: set[str]) -> str:
    if symbol is None:
        chosen = list(all_facts)
    else:
        prefix = symbol.lower() + "_"
        chosen = [f for f in all_facts if f.id.startswith(prefix) or f.id in extra]
    for f in chosen:
        ctx.evidence[f"F:{f.id}"] = {
            "type": "fact",
            "ref": f.id,
            "label": f.id + ("" if f.confirmed else " (unconfirmed)"),
            "url": f.sources[0] if f.sources else None,
        }
    body = (
        render_for_prompt(chosen)
        .replace("[FACT id=", "[F:")
        .replace("[UNCONFIRMED id=", "[UNCONFIRMED F:")
    )
    return "## Facts registry (cite as F:<id>; UNCONFIRMED facts are not established)\n" + (
        body or "None."
    )


def _scorecard(ctx: Context, engine: Engine, symbol: str) -> str:
    sc = latest_scorecard(engine, symbol)
    if sc is None:
        return "## Scorecard\nNo scorecard yet."
    lines = [f"as of {sc['as_of']}; total {sc['total']} (-100..+100); coverage {sc['coverage']}"]
    for c in sc["components"]:
        sid = f"S:{c['key']}"
        metrics = dict(c.get("metrics") or {})
        lines.append(
            f"{sid}: score {c['score']} (weight {c['weight']}); reason {c.get('reason')}; "
            f"subscores {_dump(c.get('subscores'))}; metrics {_dump(metrics)}"
        )
        ctx.evidence[sid] = {"type": "score", "ref": c["key"], "label": c["label"]}
    return "## Deterministic scorecard (computed by Aether)\n" + "\n".join(lines)


def _options(ctx: Context, engine: Engine, symbol: str) -> str:
    panel = options_panel(engine, [symbol])[0]
    oid = f"O:{symbol}"
    ctx.evidence[oid] = {"type": "options", "ref": symbol, "label": f"{symbol} options panel"}
    return f"## Options analytics (computed; research input only)\n{oid}: {_dump(panel)}"


def _theme(ctx: Context, engine: Engine) -> str:
    card = theme_card(engine)
    ctx.evidence["X:theme"] = {"type": "theme", "ref": "theme", "label": "QTUM theme decomposition"}
    return (
        "## QTUM theme decomposition (computed: rolling OLS of QTUM on SOXX, QQQ and the "
        "equal-weight pure-play basket)\n"
        f"X:theme: {_dump(card) if card else 'not computed yet'}"
    )


def _track(ctx: Context, conn: Connection, symbol: str | None, p: TrackRecordParams) -> str:
    kind = "theme" if symbol is None else "ticker"
    rows = tr.outcome_rows_for(conn, symbol, kind=kind)
    st = tr.standing(rows, p)
    summary = tr.summarize(rows, p)
    key = f"K:{symbol or 'theme'}"
    ctx.evidence[key] = {"type": "track", "ref": symbol or "theme", "label": "track record"}
    body = {
        "status": st.label,
        "per_horizon": {h: v for h, v in summary.items() if v["n"]},
    }
    return f"## Own track record (computed)\n{key}: {_dump(body)}"


def _previous(conn: Connection, ctx: Context, symbol: str | None) -> str:
    q = select(
        conclusions.c.stance, conclusions.c.as_of, conclusions.c.confidence, conclusions.c.held
    )
    q = (
        q.where(conclusions.c.symbol == symbol)
        if symbol is not None
        else q.where(conclusions.c.kind == "theme")
    )
    r = conn.execute(q.order_by(conclusions.c.id.desc()).limit(1)).first()
    if r is None:
        return "## Current stance\nNone (first conclusion)."
    ctx.previous_stance = r.stance
    return (
        f"## Current stance\n{r.stance} since the conclusion of {r.as_of} "
        f"(confidence {r.confidence:.2f})."
    )


# --------------------------------------------------------------------------- builders


def ticker_context(
    engine: Engine,
    symbol: str,
    as_of: date,
    cp: ConclusionParams,
    tp: TrackRecordParams,
    all_facts: Sequence[Fact],
) -> Context:
    with engine.connect() as conn:
        t = conn.execute(
            select(tickers.c.name, tickers.c.type).where(tickers.c.symbol == symbol)
        ).first()
    if t is None:
        raise ValueError(f"unknown ticker {symbol}")
    ctx = Context("ticker", symbol, t.name, as_of.isoformat(), "")
    # Engine-level reads first: on the worker's engine every connection begins IMMEDIATE, so
    # they must never open inside the connection below.
    score_text = _scorecard(ctx, engine, symbol)
    options_text = _options(ctx, engine, symbol)
    theme_text = _theme(ctx, engine) if symbol == CORE else None
    label = f" ({QUANTUM_SLEEVE})" if symbol == CORE else ""
    bench = "QQQ" if symbol == CORE else CORE
    with engine.connect() as conn:
        rows = _events(conn, symbol, as_of, cp)
        catalyst_text = _catalysts(conn, ctx, symbol, as_of)
        fact_ids = {
            fid
            for (fid,) in conn.execute(
                select(catalysts.c.fact_id).where(
                    catalysts.c.symbol == symbol, catalysts.c.fact_id.is_not(None)
                )
            )
        }
        sections = [
            f"# Conclusion request: {symbol}{label}\n"
            f"Company: {t.name or symbol}; type {t.type}; as of {as_of.isoformat()}; "
            f"benchmark for excess returns: {bench}.",
            _previous(conn, ctx, symbol),
            score_text,
            *_event_sections(ctx, rows, f"events-{symbol}"),
            _reactions(conn, ctx, symbol, {r["id"] for r in rows}),
            catalyst_text,
            _facts(ctx, all_facts, symbol, fact_ids),
            options_text,
            _track(ctx, conn, symbol, tp),
        ]
    if theme_text is not None:
        sections.append(theme_text)
    ctx.text = "\n\n".join(sections)
    return ctx


def theme_context(
    engine: Engine,
    as_of: date,
    cp: ConclusionParams,
    tp: TrackRecordParams,
    all_facts: Sequence[Fact],
) -> Context:
    ctx = Context("theme", None, None, as_of.isoformat(), "")
    with engine.connect() as conn:
        basket = tr.pure_plays(conn)
    cards = {sym: latest_scorecard(engine, sym) for sym in [*basket, CORE]}
    theme_text = _theme(ctx, engine)
    options_text = _options(ctx, engine, CORE)
    with engine.connect() as conn:
        stance_lines = []
        for sym in [*basket, CORE]:
            r = conn.execute(
                select(conclusions.c.stance, conclusions.c.confidence, conclusions.c.as_of)
                .where(conclusions.c.symbol == sym)
                .order_by(conclusions.c.id.desc())
                .limit(1)
            ).first()
            sc = cards[sym]
            item = {
                "stance": r.stance if r else None,
                "confidence": r.confidence if r else None,
                "stance_as_of": r.as_of if r else None,
                "score_total": sc["total"] if sc else None,
                "score_as_of": sc["as_of"] if sc else None,
            }
            stance_lines.append(f"T:{sym}: {_dump(item)}")
            ctx.evidence[f"T:{sym}"] = {"type": "stance", "ref": sym, "label": f"{sym} stance"}
        rows = _events(conn, None, as_of, cp)
        sections = [
            f"# Theme tilt request ({QUANTUM_SLEEVE})\n"
            f"As of {as_of.isoformat()}. Pure-play basket: {', '.join(basket)}. "
            "The tilt is between QTUM and the equal-weight pure-play basket.",
            _previous(conn, ctx, None),
            theme_text,
            "## Per-ticker stances and scores (computed records)\n" + "\n".join(stance_lines),
            *_event_sections(ctx, rows, "events-theme"),
            _catalysts(conn, ctx, None, as_of),
            _facts(ctx, all_facts, None, set()),
            options_text,
            _track(ctx, conn, None, tp),
        ]
    ctx.text = "\n\n".join(sections)
    return ctx
