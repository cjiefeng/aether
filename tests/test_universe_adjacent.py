"""M14 universe review: the adjacent-industry track (spec §6.7.1), slot rules and strong
candidates (§6.7.2) and the full re-evaluation (§6.7.3). Synthetic SEC data, prices and LLM answers
only (ACME, example.test); the real Anthropic SDK talks to the fake transport."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx2
import pytest
from sqlalchemy import Engine, insert, select

from aether.alerts.candidates import AlertCandidate
from aether.alerts.dispatch import enqueue
from aether.config import (
    AdjacentSector,
    AdjacentTrack,
    SourceDomain,
    Sources,
    TickerConfig,
    Watchlist,
    load_strategies,
    load_thesis_config,
    load_universe_config,
)
from aether.db.dialect import upsert
from aether.db.engine import write_tx
from aether.db.models import (
    alerts,
    qtum_holdings,
    tickers,
    universe_candidates,
    universe_evidence,
    universe_reviews,
)
from aether.providers.prices import Bar
from aether.universe import full, gates, propose, propose_adjacent, slots
from aether.universe.run import FullReviewCooldown, UniverseDeps, run_universe_review
from tests.conclusions_data import add_conclusion
from tests.conftest import CONFIG_DIR, REPO
from tests.holdings_data import add_filing
from tests.llm_fakes import (
    MODEL,
    FakeApi,
    llm_settings,
    make_client,
    message,
    research_message,
    search_result,
)

TODAY = date(2026, 11, 1)
NOW = datetime(2026, 11, 1, 2, 0, tzinfo=UTC)
SOURCES = Sources(
    domains=(
        SourceDomain(domain="sec.gov", tier="T1"),
        SourceDomain(domain="example.test", tier="T2"),
        SourceDomain(domain="news.example.test", tier="T2"),
        SourceDomain(domain="wire.test", tier="T2"),
    )
)
ADJ = AdjacentTrack(
    sectors={
        "pqc_cyber": AdjacentSector(priority=1, seeds=("CYBR", "HYPR")),
        "sensing_timing": AdjacentSector(priority=1, seeds=("CLCK", "SEMI")),
        "test_measurement": AdjacentSector(priority=2, seeds=("KEYZ",)),
        "photonics_lasers": AdjacentSector(priority=2, seeds=("LASE", "PRIV")),
        "cryogenics_gases": AdjacentSector(priority=2),
        "telecom_networking": AdjacentSector(priority=2),
        "specialty_materials": AdjacentSector(priority=3),
        "end_user": AdjacentSector(priority=3),
    },
    excluded_symbols=("HYPR",),
    excluded_sics=("3674",),
)
CFG = load_universe_config(CONFIG_DIR).model_copy(update={"adjacent": ADJ})
THESIS = load_thesis_config(CONFIG_DIR)

PURE = (
    "Overview {t} designs and builds quantum computers based on trapped ions. "
    "Quantum computing is our only business. "
)
CLOCK = (
    "Overview {t} makes precision timing products, atomic clocks and quantum sensors for "
    "satellites and defense customers. "
)
LASER = (
    "Overview {t} makes industrial lasers and photonics components, including lasers for "
    "quantum computing systems. "
)
TEST = (
    "Overview {t} makes electronic test and measurement instruments, including quantum control "
    "and test systems. "
)
PLAIN = "Overview {t} sells network firewalls and security subscriptions to enterprises worldwide. "


@dataclass
class Co:
    cik: str
    ticker: str
    exchange: str | None
    business: str
    sic: str = "3825"
    sessions: int = 300
    close: float = 20.0
    volume: int = 2_000_000
    shares: int = 200_000_000


COMPANIES = {
    "ACME": Co("0000000101", "ACME", "Nasdaq", PURE, sic="7373"),
    "DEMO": Co("0000000102", "DEMO", "NYSE", PURE, sic="7373"),
    "KEYZ": Co("0000000201", "KEYZ", "NYSE", TEST),
    "CLCK": Co("0000000202", "CLCK", "Nasdaq", CLOCK, close=50.0),  # $10B: mid
    "LASE": Co("0000000203", "LASE", "Nasdaq", LASER, close=5.0),  # $1B: small
    "CYBR": Co("0000000204", "CYBR", "Nasdaq", PLAIN, close=400.0),  # $80B: large
    "HYPR": Co("0000000205", "HYPR", "Nasdaq", PLAIN),
    "SEMI": Co("0000000206", "SEMI", "Nasdaq", CLOCK, sic="3674"),
}


def _watchlist(extra: Sequence[TickerConfig] = ()) -> Watchlist:
    return Watchlist(
        tickers=(
            TickerConfig(symbol="QTUM", type="etf"),
            TickerConfig(
                symbol="ACME",
                type="pure_play",
                cik="0000000101",
                modality="trapped_ion",
                modality_fact="acme_modality",
            ),
            TickerConfig(
                symbol="DEMO",
                type="pure_play",
                cik="0000000102",
                modality="superconducting",
                modality_fact="demo_modality",
            ),
            TickerConfig(
                symbol="KEYZ", type="adjacent", cik="0000000201", sector="test_measurement"
            ),
            *extra,
        )
    )


def _doc(c: Co) -> str:
    toc = "<p>Item 1. Business 4</p><p>Item 1A. Risk Factors 20</p>"
    body = (c.business.format(t=c.ticker) * 8) + "We compete on product quality. " * 30
    return (
        f"<html><body>{toc}<h2>Item 1. Business</h2><p>{body}</p>"
        "<h2>Item 1A. Risk Factors</h2><p>Risks.</p></body></html>"
    )


@dataclass
class FakeEdgar:
    companies: dict[str, Co] = field(default_factory=lambda: dict(COMPANIES))
    calls: list[str] = field(default_factory=list)

    def _by_cik(self, cik: str) -> Co:
        return next(c for c in self.companies.values() if c.cik == cik)

    def company_tickers_exchange(self) -> dict[str, Any]:
        rows = [
            [int(c.cik), f"{c.ticker} Corp", c.ticker, c.exchange]
            for c in self.companies.values()
            if c.exchange
        ]
        return {"fields": ["cik", "name", "ticker", "exchange"], "data": rows}

    def full_text_search(self, *_a: Any) -> list[dict[str, Any]]:
        return []

    def submissions(self, cik: str) -> dict[str, Any]:
        c = self._by_cik(cik)
        self.calls.append(f"submissions {c.ticker}")
        return {
            "sic": c.sic,
            "filings": {
                "recent": {
                    "form": ["10-K"],
                    "filingDate": ["2026-03-01"],
                    "accessionNumber": [f"{c.cik}-26-000001"],
                    "primaryDocument": ["doc.htm"],
                }
            },
        }

    def document(self, cik: str, accession: str, primary_doc: str, *, raw: bool = False) -> str:
        c = self._by_cik(cik)
        self.calls.append(f"document {c.ticker}")
        return _doc(c)

    def companyfacts(self, cik: str) -> dict[str, Any]:
        c = self._by_cik(cik)
        u = {"val": c.shares, "end": "2026-02-15", "filed": "2026-03-01", "accn": "x-1"}
        return {
            "facts": {"dei": {"EntityCommonStockSharesOutstanding": {"units": {"shares": [u]}}}}
        }


@dataclass
class FakePrices:
    name: str = "synthetic"

    def fetch_daily(self, symbol: str, start: date, end: date) -> list[Bar]:
        c = COMPANIES[symbol]
        days = [end - timedelta(days=i) for i in range(c.sessions)][::-1]
        return [Bar(d, c.close, c.close, c.close, c.close, c.volume, "synthetic") for d in days]


# Web evidence per researched company: (url, title, page_age, cited text).
WEB: dict[str, list[tuple[str, str, str, str]]] = {
    "CLCK": [
        (
            "https://example.test/clck",
            "CLCK wins quantum sensor contract",
            "October 1, 2026",
            "a quantum sensor contract",
        ),
        (
            "https://wire.test/clck",
            "CLCK quantum clock order",
            "September 20, 2026",
            "quantum clock order",
        ),
    ],
    "LASE": [
        (
            "https://news.example.test/lase",
            "LASE quantum laser line",
            "October 2, 2026",
            "lasers for quantum",
        )
    ],
    "CYBR": [
        (
            "https://blog.other.test/cybr",
            "CYBR post-quantum firewall",
            "October 3, 2026",
            "post-quantum",
        )
    ],
    "KEYZ": [
        ("https://example.test/keyz", "KEYZ quantum test system", "October 4, 2026", "quantum test")
    ],
    "ACME": [
        ("https://example.test/acme", "ACME quantum update", "October 5, 2026", "quantum update")
    ],
    "DEMO": [
        ("https://example.test/demo", "DEMO quantum update", "October 6, 2026", "quantum update")
    ],
}


def _research_for(prompt: str) -> dict[str, Any]:
    for sym, items in WEB.items():
        if f"identifier {sym}" in prompt:
            return research_message(
                [search_result(u, t, d) for u, t, d, _c in items],
                citations=[(u, c) for u, _t, _d, c in items],
            )
    return research_message(
        [
            search_result(
                "https://example.test/sweep", "Private Quantum Labs raises", "October 7, 2026"
            )
        ],
        citations=[("https://example.test/sweep", "a private company")],
    )


def _text(payload: dict[str, Any]) -> dict[str, Any]:
    return message([{"type": "text", "text": json.dumps(payload)}])


def _evidence(engine: Engine) -> tuple[int, str, dict[str | None, list[tuple[str, str, str]]]]:
    """(review id, as_of, symbol -> [(ref, kind, tier)]) for the latest review."""
    with engine.connect() as conn:
        rid, as_of = conn.execute(
            select(universe_reviews.c.id, universe_reviews.c.as_of).order_by(
                universe_reviews.c.id.desc()
            )
        ).first()  # type: ignore[misc]
        rows = conn.execute(
            select(
                universe_evidence.c.id,
                universe_evidence.c.symbol,
                universe_evidence.c.kind,
                universe_evidence.c.trust_tier,
            )
            .where(universe_evidence.c.review_id == rid)
            .order_by(universe_evidence.c.id)
        ).all()
    out: dict[str | None, list[tuple[str, str, str]]] = {}
    for r in rows:
        out.setdefault(r.symbol, []).append((f"U{r.id}", r.kind, r.trust_tier))
    return rid, as_of, out


def _x_refs(body: str) -> dict[str, str]:
    """X refs per symbol from the proposal request text (`- SYM (…): …; qualifying events X1`)."""
    out = {}
    for line in body.splitlines():
        if "qualifying events" in line and line.startswith("- "):
            sym = line[2:].split(" ", 1)[0]
            out[sym] = line.rsplit("qualifying events ", 1)[1].split(",")[0].strip()
    return out


@dataclass
class Plan:
    """What the fake model answers."""

    pure: dict[str, str] = field(default_factory=lambda: {"ACME": "keep", "DEMO": "keep"})
    pure_cite_x: set[str] = field(default_factory=set)  # cite the X ref for these removes
    adj: dict[str, tuple[str, str]] = field(
        default_factory=lambda: {
            "KEYZ": ("keep", "med"),
            "CLCK": ("add", "high"),
            "LASE": ("add", "high"),
            "CYBR": ("add", "med"),
        }
    )
    t1_exposure: set[str] = field(default_factory=lambda: {"CLCK"})  # cite the T1 excerpt
    weakest: str = "DEMO"
    full: Callable[[Engine, str], dict[str, Any]] | None = None


def _pure_answer(engine: Engine, plan: Plan, body: str) -> dict[str, Any]:
    _rid, as_of, ev = _evidence(engine)
    xs = _x_refs(body)
    cands = []
    for sym, action in plan.pure.items():
        ids = [r for r, _k, _t in ev.get(sym, [])][:1]
        if action == "remove":  # not the T1 excerpt (that's the criterion-2 removal path)
            ids = [r for r, k, _t in ev.get(sym, []) if k == "web"][:1]
        if sym in plan.pure_cite_x and sym in xs:
            ids = [xs[sym]]
        cands.append(
            {
                "symbol": sym,
                "action": action,
                "name": f"{sym} Corp",
                "description": f"{sym} builds quantum computers (synthetic).",
                "reasons": [{"text": f"Synthetic reason for {sym}.", "evidence_ids": ids}],
            }
        )
    return {"as_of": as_of, "candidates": cands, "announcements": [], "injection_suspected": False}


def _adj_answer(engine: Engine, plan: Plan, body: str) -> dict[str, Any]:
    _rid, as_of, ev = _evidence(engine)
    cands = []
    for sym, (action, exposure) in plan.adj.items():
        if f"## Candidate {sym}" not in body:
            continue
        items = ev.get(sym, [])
        t1 = [r for r, k, _t in items if k == "business_excerpt"]
        web = [r for r, k, _t in items if k == "web"]
        exp_ids = t1[:1] if sym in plan.t1_exposure else web[:1]
        weak = (
            {
                "symbol": plan.weakest,
                "text": f"{plan.weakest} has weaker evidence.",
                "evidence_ids": (t1 + web)[:1],
            }
            if action == "add"
            else {"symbol": "", "text": "", "evidence_ids": []}
        )
        cands.append(
            {
                "symbol": sym,
                "action": action,
                "name": f"{sym} Corp",
                "description": f"{sym} makes quantum-related products (synthetic).",
                "exposure": exposure,
                "exposure_evidence_ids": exp_ids,
                "market_cap_note": "",
                "reasons": [
                    {"text": f"Synthetic reason for {sym}.", "evidence_ids": (t1 + web)[:2]}
                ],
                "weakest_current": weak,
            }
        )
    sweep = [r for r, _k, _t in ev.get(None, [])]
    others = [
        {
            "name": "Private Quantum Labs",
            "status": "private",
            "description": "A private company (synthetic).",
            "evidence_ids": sweep[-1:],
        }
    ]
    return {
        "as_of": as_of,
        "candidates": cands,
        "others": others if sweep else [],
        "injection_suspected": False,
    }


@dataclass
class Harness:
    engine: Engine
    api: FakeApi
    deps: UniverseDeps
    sent: list[AlertCandidate]
    plan: Plan


def _harness(
    engine: Engine,
    db: Path,
    plan: Plan | None = None,
    *,
    cap: int = 9,
    adj_budget: str = "10.00",
    watchlist: Watchlist | None = None,
) -> Harness:
    plan = plan or Plan()
    wl = watchlist or _watchlist()
    with write_tx(engine) as conn:
        upsert(
            conn,
            tickers,
            [
                {
                    "symbol": t.symbol,
                    "type": t.type,
                    "active": 1,
                    "modality": t.modality,
                    "sector": t.sector,
                }
                for t in wl.tickers
            ],
            key_cols=["symbol"],
        )
        if conn.execute(select(qtum_holdings.c.holding_symbol)).first() is None:
            conn.execute(
                insert(qtum_holdings),
                [
                    {
                        "snapshot_date": "2026-10-30",
                        "holding_symbol": s,
                        "name": s,
                        "cusip": "000000000",
                        "weight": w,
                        "shares": 1,
                        "fetched_at": "2026-10-30T00:00:00Z",
                    }
                    for s, w in (("ACME", 2.0), ("CLCK", 1.5))
                ],
            )
    api = FakeApi()

    def on_request(request: httpx2.Request) -> None:
        body = json.loads(request.content)
        prompt = body["messages"][0]["content"]
        if body.get("tools"):
            api.messages.append(_research_for(prompt))
            return
        system = json.dumps(body.get("system"))
        if "re-evaluate, from scratch" in system:
            assert plan.full is not None
            api.messages.append(_text(plan.full(engine, prompt)))
        elif "industries around quantum computing" in system:
            api.messages.append(_text(_adj_answer(engine, plan, prompt)))
        else:
            api.messages.append(_text(_pure_answer(engine, plan, prompt)))

    api.on_request = on_request
    llm = make_client(engine, llm_settings(db), api, clock=lambda: NOW)
    sent: list[AlertCandidate] = []

    def notify(cands: Sequence[AlertCandidate]) -> None:
        sent.extend(cands)
        enqueue(engine, cands, telegram=True, now=NOW)

    deps = UniverseDeps(
        llm=llm,
        edgar=FakeEdgar(),  # type: ignore[arg-type]
        prices=FakePrices(),
        cfg=CFG.model_copy(update={"max_names_ex_qtum": cap}),
        sources=SOURCES,
        watchlist=wl,
        overlay=load_strategies(CONFIG_DIR).overlay,
        model=MODEL,
        budget_usd=Decimal("10.00"),
        notify=notify,
        adjacent_budget_usd=Decimal(adj_budget),
        thesis=THESIS,
        strategies=load_strategies(CONFIG_DIR),
    )
    return Harness(engine, api, deps, sent, plan)


def _cands(engine: Engine, rid: int | None = None) -> dict[str, Any]:
    with engine.connect() as conn:
        rid = (
            rid
            or conn.execute(
                select(universe_reviews.c.id).order_by(universe_reviews.c.id.desc())
            ).scalar()
        )
        return {
            r.symbol: r
            for r in conn.execute(
                select(universe_candidates).where(universe_candidates.c.review_id == rid)
            ).all()
        }


def _review(engine: Engine) -> Any:
    with engine.connect() as conn:
        return conn.execute(select(universe_reviews).order_by(universe_reviews.c.id.desc())).first()


def _prompts(api: FakeApi) -> list[str]:
    return [b["messages"][0]["content"] for b in api.bodies()]


# --------------------------------------------------------------------------- adjacent track


def test_adjacent_track_end_to_end(rw_engine: Engine, migrated_db: Path) -> None:
    h = _harness(rw_engine, migrated_db)
    res = run_universe_review(rw_engine, h.deps, today=TODAY, now=NOW)
    assert res.warning is None
    c = _cands(rw_engine)
    # Exclusions are checked in code before any research.
    prompts = _prompts(h.api)
    assert not any("HYPR" in p or "SEMI" in p for p in prompts)
    assert "document HYPR" not in h.deps.edgar.calls  # type: ignore[attr-defined]
    assert "HYPR" not in c and "SEMI" not in c
    payload = json.loads(_review(rw_engine).payload)
    screened = {s["symbol"]: s["reason"] for s in payload["adjacent"]["screened"]}
    assert screened["HYPR"].startswith("excluded: hyperscaler")
    assert screened["SEMI"] == "excluded: SEC SIC 3674 (semiconductors)"
    # Pure-plays are reviewed by their own track.
    assert c["ACME"].track == "pure_play" and c["KEYZ"].track == "adjacent"

    # CLCK: add; exposure high with a cited T1 excerpt; mid cap computed in code.
    assert c["CLCK"].action == "add" and c["CLCK"].exposure == "high"
    assert c["CLCK"].sector == "sensing_timing" and c["CLCK"].mcap_bucket == "mid"
    assert c["CLCK"].market_cap_micros == Decimal("10000000000")
    assert json.loads(c["CLCK"].overlap)["qtum_weight_pct"] == pytest.approx(1.5)
    crit = json.loads(c["CLCK"].criteria)
    assert crit["evidence_t1"] == 1 and crit["evidence_t2"] == 2 and crit["fills_gap"] is True
    # LASE: high without a cited T1 excerpt -> capped to med (still an add: it has T1 evidence).
    assert c["LASE"].exposure == "med" and c["LASE"].mcap_bucket == "small"
    assert "capped at med" in json.loads(c["LASE"].criteria)["exposure_note"]
    assert c["LASE"].action == "add"
    # CYBR: only T3 evidence (and no quantum in its 10-K) -> never add.
    assert c["CYBR"].action == "watch" and "quantum-related evidence: 0 T1" in c["CYBR"].gate_note
    assert c["CYBR"].mcap_bucket == "large"
    # An already-tracked name is never add.
    assert c["KEYZ"].action == "keep"
    # Private company -> "not investable" info only; PRIV (no US listing) -> outside mandate.
    info = payload["adjacent"]["info"]
    assert {i.get("name") or i.get("symbol"): i["status"] for i in info} == {
        "PRIV": "outside mandate",
        "Private Quantum Labs": "not investable",
    }
    assert "Private Quantum Labs" not in c
    # Shortlist: at most 5, sector priority first.
    assert payload["shortlist"] == ["CLCK", "CYBR", "LASE"]
    assert payload["slots"] == {
        "cap": 9,
        "active": 3,
        "removals": 0,
        "free": 6,
        "adds": ["CLCK", "LASE"],
        "blocked": [],
    }
    # No strong-candidate message while slots are free; one review message with both sections.
    assert [a.kind for a in h.sent] == ["universe_review"]
    text = h.sent[0].text
    assert "Adjacent industries:" in text and "- Add CLCK (sensing timing, mid cap" in text
    assert "Shortlist: CLCK, CYBR, LASE" in text and len(text) <= 4096
    # The proposal calls have no tools.
    finals = [b for b in h.api.bodies() if not b.get("tools")]
    assert len(finals) == 2


def test_t3_only_never_add_and_exposure_cap_gate() -> None:
    f = gates.AdjFacts(
        tracked=False,
        c1=True,
        c3=True,
        c4=True,
        c3_fails=[],
        c4_sessions=300,
        min_sessions=60,
        structural=[],
        qualifying={},
        t1_items=0,
        t2_domains=1,
        min_t2=2,
        t1_quantum_refs=frozenset(),
    )
    entry = {"action": "add", "exposure": "high", "exposure_evidence_ids": ["U9"]}
    g = gates.gate_adjacent(f, entry, ["U9"])
    assert g.action == "watch" and g.exposure == "med"
    g = gates.gate_adjacent(f.__class__(**{**f.__dict__, "t2_domains": 2}), entry, ["U9"])
    assert g.action == "add" and g.exposure == "med"
    g = gates.gate_adjacent(
        f.__class__(**{**f.__dict__, "t1_items": 1, "t1_quantum_refs": frozenset({"U9"})}),
        entry,
        ["U9"],
    )
    assert g.action == "add" and g.exposure == "high"


def test_remove_needs_a_qualifying_trigger(rw_engine: Engine, migrated_db: Path) -> None:
    plan = Plan(pure={"ACME": "remove", "DEMO": "remove"}, pure_cite_x={"DEMO"})
    plan.adj["KEYZ"] = ("remove", "low")
    h = _harness(rw_engine, migrated_db, plan)
    # DEMO: a T1 RISK event (materiality 4) since the previous review.
    add_filing(rw_engine, "DEMO", "10-Q", "2026-10-20", event=("RISK", "going_concern", 4, "t"))
    # ACME: a RISK 4 event backed only by... a quarantined event doesn't count.
    add_filing(
        rw_engine, "ACME", "8-K", "2026-10-21", event=("RISK", "dilution", 5, "t"), quarantined=True
    )
    run_universe_review(rw_engine, h.deps, today=TODAY, now=NOW)
    c = _cands(rw_engine)
    assert (
        c["DEMO"].action == "remove"
        and "qualifying event (code): RISK going concern" in c["DEMO"].gate_note
    )
    assert c["ACME"].action == "watch" and c["ACME"].gate_note.startswith(
        "concerns, no qualifying event"
    )
    assert c["KEYZ"].action == "watch" and c["KEYZ"].gate_note.startswith(
        "concerns, no qualifying event"
    )
    payload = json.loads(_review(rw_engine).payload)
    assert list(payload["triggers"]) == ["DEMO"] and payload["slots"]["removals"] == 1


def test_qualifying_triggers_cover_avoid_and_t2_pairs(rw_engine: Engine) -> None:
    from aether.config import RemovalRule

    upsert_rows = [("ACME", "pure_play"), ("DEMO", "pure_play")]
    with write_tx(rw_engine) as conn:
        upsert(
            conn,
            tickers,
            [{"symbol": s, "type": t, "active": 1} for s, t in upsert_rows],
            key_cols=["symbol"],
        )
    add_conclusion(rw_engine, "ACME", "2026-10-15", "AVOID")
    add_conclusion(rw_engine, "DEMO", "2026-10-15", "AVOID", held=True)  # held: doesn't count
    out = slots.qualifying_triggers(
        rw_engine,
        ["ACME", "DEMO"],
        date(2026, 10, 1),
        TODAY,
        load_strategies(CONFIG_DIR).overlay,
        RemovalRule(),
    )
    assert [t.kind for t in out["ACME"]] == ["avoid"] and out["DEMO"] == []
    assert out["ACME"][0].ref == "X1"


def test_all_slots_full_no_add_and_one_strong_candidate(
    rw_engine: Engine, migrated_db: Path
) -> None:
    h = _harness(rw_engine, migrated_db, cap=3)
    run_universe_review(rw_engine, h.deps, today=TODAY, now=NOW)
    c = _cands(rw_engine)
    assert not [s for s, r in c.items() if r.action == "add"]
    assert c["CLCK"].action == "watch" and c["CLCK"].gate_note.startswith("no free slot: 3 of 3")
    crit = json.loads(c["CLCK"].criteria)
    assert crit["strong"] is True and crit["slot_blocked"] is True
    # LASE misses a threshold (exposure capped to med): watch, no notification.
    assert c["LASE"].action == "watch"
    assert json.loads(c["LASE"].criteria)["strong"] is False
    strong = [a for a in h.sent if a.kind == "universe_strong_candidate"]
    assert len(strong) == 1
    text = strong[0].text
    assert text.startswith("Aether · strong candidate (#10) · CLCK")
    assert "Compares least favourably with: DEMO" in text
    assert "Adding needs a slot: remove a name or raise the cap. Review on /universe." in text
    assert "mid" in text and "1.50% of the fund" in text and len(text) <= 4096
    with rw_engine.connect() as conn:
        rows = conn.execute(
            select(alerts).where(alerts.c.kind == "universe_strong_candidate")
        ).all()
    assert len(rows) == 1 and rows[0].delivery == "immediate"

    # Next month: same evidence, no newer qualifying item -> not re-sent.
    h.sent.clear()
    run_universe_review(rw_engine, h.deps, today=date(2026, 12, 1), now=NOW + timedelta(days=30))
    c2 = _cands(rw_engine)
    assert json.loads(c2["CLCK"].criteria)["strong"] is False
    assert "no newer qualifying evidence" in json.loads(c2["CLCK"].criteria)["strong_note"]
    assert [a.kind for a in h.sent] == ["universe_review"]

    # The month after: newer qualifying evidence -> notified again.
    WEB["CLCK"].append(
        (
            "https://news.example.test/clck2",
            "CLCK quantum sensing deal",
            "December 20, 2026",
            "quantum deal",
        )
    )
    try:
        h.sent.clear()
        run_universe_review(rw_engine, h.deps, today=date(2027, 1, 1), now=NOW + timedelta(days=61))
    finally:
        WEB["CLCK"].pop()
    assert [a.kind for a in h.sent] == ["universe_review", "universe_strong_candidate"]


def test_adjacent_budget_cap_stops_only_the_adjacent_track(
    rw_engine: Engine, migrated_db: Path
) -> None:
    h = _harness(rw_engine, migrated_db, adj_budget="0.0001")
    res = run_universe_review(rw_engine, h.deps, today=TODAY, now=NOW)
    review = _review(rw_engine)
    assert review.status == "done"
    payload = json.loads(review.payload)
    assert payload["adjacent"]["status"] == "failed"
    assert payload["adjacent"]["error"].startswith("budget cap reached")
    c = _cands(rw_engine)
    assert set(c) == {
        "ACME",
        "DEMO",
    }  # pure-play rows only; nothing partial from the adjacent track
    assert "adjacent track failed" in (res.warning or "")
    assert "Track failed: budget cap reached" in h.sent[0].text


def test_strong_candidate_rules() -> None:
    rule = CFG.strong_candidate
    row = {"exposure": "high", "criteria_obj": {"slot_blocked": True}}
    ok, _ = slots.strong_candidate(row, slots.EvidenceCount(2, 1, "2026-10-01"), rule, [])
    assert ok
    assert not slots.strong_candidate(row, slots.EvidenceCount(1, 1, "2026-10-01"), rule, [])[0]
    assert not slots.strong_candidate(row, slots.EvidenceCount(2, 0, "2026-10-01"), rule, [])[0]
    med = {**row, "exposure": "med"}
    assert not slots.strong_candidate(med, slots.EvidenceCount(3, 2, "2026-10-01"), rule, [])[0]
    prior = [{"month": "2026-10", "as_of": "2026-10-01", "criteria": {"strong": True}}]
    assert not slots.strong_candidate(row, slots.EvidenceCount(2, 1, "2026-09-30"), rule, prior)[0]
    assert slots.strong_candidate(row, slots.EvidenceCount(2, 1, "2026-10-15"), rule, prior)[0]
    old = [{}, {}, {}, {"month": "2026-06", "as_of": "2026-06-01", "criteria": {"strong": True}}]
    assert slots.strong_candidate(row, slots.EvidenceCount(2, 1, "2026-05-01"), rule, old)[0]


def test_shortlist_has_at_most_five() -> None:
    rows = [
        {
            "symbol": f"AD{i}",
            "track": "adjacent",
            "action": "watch",
            "sector": "end_user",
            "exposure": "low",
            "criteria_obj": {},
        }
        for i in range(8)
    ]
    rows.append(
        {
            "symbol": "TOP",
            "track": "adjacent",
            "action": "add",
            "sector": "pqc_cyber",
            "exposure": "high",
            "criteria_obj": {},
        }
    )
    out = slots.shortlist(rows, {"pqc_cyber": 1, "end_user": 3}, 5)
    assert len(out) == 5 and out[0] == "TOP"


def test_weakest_current_must_be_an_active_name() -> None:
    body = {
        "as_of": "2026-11-01",
        "candidates": [
            {
                "symbol": "CLCK",
                "action": "add",
                "name": "CLCK Corp",
                "description": "d",
                "exposure": "high",
                "exposure_evidence_ids": ["U1"],
                "market_cap_note": "",
                "reasons": [{"text": "r", "evidence_ids": ["U1"]}],
                "weakest_current": {"symbol": "ZZZZ", "text": "t", "evidence_ids": ["U1"]},
            }
        ],
        "others": [],
        "injection_suspected": False,
    }
    with pytest.raises(propose.InvalidProposal, match="weakest_current"):
        propose_adjacent.validate(_text(body), "2026-11-01", ["CLCK"], {"U1"}, {"ACME"})
    body["candidates"][0]["weakest_current"]["symbol"] = "ACME"  # type: ignore[index]
    assert propose_adjacent.validate(_text(body), "2026-11-01", ["CLCK"], {"U1"}, {"ACME"})


# --------------------------------------------------------------------------- full re-evaluation


def _full_answer(
    members: list[tuple[str, str, str]], drops: Sequence[str]
) -> Callable[[Engine, str], dict[str, Any]]:
    def build(engine: Engine, _body: str) -> dict[str, Any]:
        _rid, as_of, ev = _evidence(engine)

        def ids(sym: str) -> list[str]:
            return [r for r, _k, _t in ev.get(sym, [])][:1]

        return {
            "as_of": as_of,
            "members": [
                {
                    "symbol": s,
                    "decision": d,
                    "modality": m,
                    "reasons": [{"text": f"{s} evidence.", "evidence_ids": ids(s)}],
                }
                for s, d, m in members
            ],
            "drops": [
                {
                    "symbol": s,
                    "reasons": [{"text": f"{s} redundant modality.", "evidence_ids": ids(s)}],
                }
                for s in drops
            ],
            "not_chosen": [],
            "injection_suspected": False,
        }

    return build


def test_full_reevaluation_proposes_a_complete_set(rw_engine: Engine, migrated_db: Path) -> None:
    plan = Plan(
        full=_full_answer(
            [("ACME", "keep", ""), ("CLCK", "add", ""), ("LASE", "add", "")], drops=["DEMO", "KEYZ"]
        )
    )
    h = _harness(rw_engine, migrated_db, plan)
    run_universe_review(rw_engine, h.deps, today=TODAY, kind="full", now=NOW)
    review = _review(rw_engine)
    assert review.kind == "full" and review.status == "done"
    f = json.loads(review.payload)["full"]
    # A current name with no §6.7.2 trigger can still be dropped, with cited reasons.
    assert [d["symbol"] for d in f["drops"]] == ["DEMO", "KEYZ"]
    assert all(d["reasons"][0]["evidence_ids"] for d in f["drops"])
    assert [m["symbol"] for m in f["members"]] == ["ACME", "CLCK", "LASE"]
    # The proposed set breaks concentration flags (one modality; each name 33%): marked.
    assert {x["flag"] for x in f["flags"]} == {"few_modalities", "name_concentration"}
    # Before/after weights by modality and sector under the selected profile.
    before = {r["category"] for r in f["before"]["rows"]}
    after = {r["category"] for r in f["after"]["rows"]}
    assert before == {"trapped_ion", "superconducting", "test_measurement"}
    assert after == {"trapped_ion", "sensing_timing", "photonics_lasers"}
    assert f["profile"] == "safe" and "Illustrative" in f["weights_note"]  # the default profile
    # Every other candidate is listed with why it wasn't chosen.
    assert [n["symbol"] for n in f["not_chosen"]] == []  # CYBR isn't add-qualified: not in the pool
    # One Telegram summary, no strong-candidate messages.
    assert [a.kind for a in h.sent] == ["universe_review"]
    assert h.sent[0].text.startswith("Aether full re-evaluation · 2026-11-01")
    assert "Drop DEMO: DEMO redundant modality." in h.sent[0].text
    # A second one within 7 days is refused.
    with pytest.raises(FullReviewCooldown):
        run_universe_review(
            rw_engine, h.deps, today=TODAY + timedelta(days=6), kind="full", now=NOW
        )
    # The monthly review still runs that month (a full review doesn't count as the month's).
    h.plan.full = None
    run_universe_review(rw_engine, h.deps, today=TODAY, now=NOW)
    assert _review(rw_engine).kind == "monthly"


def _pool() -> list[full.PoolEntry]:
    def e(sym: str, current: bool, track: str = "pure_play") -> full.PoolEntry:
        ev = propose.EvidenceItem(
            len(sym), sym, "web", "T2", "example.test", "t", "x", "2026-10-01"
        )
        return full.PoolEntry(sym, track, current, f"{sym} Corp", None, [], [], [ev])

    return [e("ACME", True), e("DEMO", True), e("CLCK", False, "adjacent")]


def _set(members: list[tuple[str, str]], drops: list[str]) -> dict[str, Any]:
    return {
        "as_of": "2026-11-01",
        "members": [
            {
                "symbol": s,
                "decision": d,
                "modality": "",
                "reasons": [{"text": "r", "evidence_ids": ["U4"]}],
            }
            for s, d in members
        ],
        "drops": [{"symbol": s, "reasons": [{"text": "r", "evidence_ids": ["U4"]}]} for s in drops],
        "not_chosen": [],
        "injection_suspected": False,
    }


def test_full_validator_rules() -> None:
    pool = _pool()
    known = {"U4"}
    ok = _set([("ACME", "keep"), ("CLCK", "add")], ["DEMO"])
    assert full.validate(_text(ok), "2026-11-01", pool, known, 9, {"HYPR"})
    # Never more than the cap.
    with pytest.raises(propose.InvalidProposal, match="maximum is 1"):
        full.validate(_text(ok), "2026-11-01", pool, known, 1, set())
    # Never an excluded or unknown name.
    bad = _set([("ACME", "keep"), ("HYPR", "add")], ["DEMO"])
    with pytest.raises(propose.InvalidProposal, match="HYPR is excluded"):
        full.validate(_text(bad), "2026-11-01", pool, known, 9, {"HYPR"})
    # Every current name kept or dropped.
    with pytest.raises(propose.InvalidProposal, match="neither kept nor dropped: DEMO"):
        full.validate(_text(_set([("ACME", "keep")], [])), "2026-11-01", pool, known, 9, set())
    # Drops cite known ids.
    uncited = _set([("ACME", "keep")], ["DEMO"])
    uncited["drops"][0]["reasons"][0]["evidence_ids"] = ["U99"]
    with pytest.raises(propose.InvalidProposal, match="unknown evidence ids"):
        full.validate(_text(uncited), "2026-11-01", pool, known, 9, set())
    nocite = _set([("ACME", "keep")], ["DEMO"])
    nocite["drops"][0]["reasons"][0]["evidence_ids"] = []
    with pytest.raises(propose.InvalidProposal, match="schema"):
        full.validate(_text(nocite), "2026-11-01", pool, known, 9, set())


def test_illustrative_weights_respect_caps() -> None:
    pp = load_strategies(CONFIG_DIR).profiles["medium"]
    w = full.illustrative_weights(["A", "B"], pp)
    assert w == {"A": 0.20, "B": 0.20, "QTUM": pytest.approx(0.60)}
    w = full.illustrative_weights([f"N{i}" for i in range(9)], pp)
    assert w["N0"] == pytest.approx(0.55 / 9) and w["QTUM"] == pytest.approx(0.45)


# --------------------------------------------------------------------------- S1: no thesis text


def _strategy_phrases() -> list[str]:
    text = (REPO / "STRATEGY.md").read_text(encoding="utf-8")
    phrases = []
    for line in text.splitlines():
        line = line.strip().lstrip("-0123456789. ").replace("**", "")
        words = line.split()
        if len(words) >= 8:
            phrases.append(" ".join(words[:8]))
    assert len(phrases) > 10
    return phrases


def test_strategy_md_never_reaches_a_prompt(rw_engine: Engine, migrated_db: Path) -> None:
    """The owner's thesis text is applied only as code checks: no phrase from STRATEGY.md appears
    in any prompt builder (source), any config file, or any request body sent to the model."""
    phrases = _strategy_phrases()
    sources = [p for p in (REPO / "src" / "aether").rglob("*.py")]
    sources += list((REPO / "config").glob("*.yaml"))
    for path in sources:
        body = " ".join(path.read_text(encoding="utf-8").split())
        hit = [ph for ph in phrases if ph in body]
        assert not hit, f"{path}: {hit[:2]}"
    plan = Plan(
        full=_full_answer([("ACME", "keep", ""), ("DEMO", "keep", ""), ("KEYZ", "keep", "")], [])
    )
    h = _harness(rw_engine, migrated_db, plan)
    run_universe_review(rw_engine, h.deps, today=TODAY, kind="full", now=NOW)
    sent = " ".join(json.dumps(b) for b in h.api.bodies())
    assert "STRATEGY.md" not in sent and "Quantum Thesis" not in sent
    assert not [ph for ph in phrases if ph in sent]


def test_scheduled_monthly_review_gets_the_m14_rules(
    rw_engine: Engine, migrated_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 1st-of-month job (and "Run review now") builds its deps from config with the M14
    inputs, so the monthly review applies the adjacent track, the 9-name slot rules and the
    thesis checks, not only the M12 pure-play rules."""
    from aether import jobs
    from tests.conftest import make_settings

    seen: dict[str, Any] = {}

    def fake_run(engine: Engine, deps: UniverseDeps, *, today: date, kind: str) -> Any:
        seen.update(deps=deps, kind=kind)
        from aether.runs import JobResult

        return JobResult()

    monkeypatch.setattr(jobs, "run_universe_review", fake_run)
    settings = make_settings(
        migrated_db, sec_user_agent="Test example@example.test", universe_adjacent_budget_usd="7.5"
    )
    jobs.universe_review_job(rw_engine, settings, object(), lambda _c: None)  # type: ignore[arg-type]
    deps: UniverseDeps = seen["deps"]
    assert seen["kind"] == "monthly"
    assert deps.cfg.max_names_ex_qtum == 9 and deps.cfg.adjacent is not None
    assert deps.cfg.adjacent.excluded_symbols == ("AMZN", "MSFT", "GOOGL", "ORCL", "BABA")
    assert deps.cfg.removal.min_materiality == 4 and deps.cfg.strong_candidate.cooldown_reviews == 3
    assert deps.adjacent_budget_usd == Decimal("7.5")
    assert deps.thesis == load_thesis_config(CONFIG_DIR)
    assert deps.strategies is not None and deps.strategies.sleeve_types == ("pure_play", "adjacent")
    assert {t.symbol for t in deps.watchlist.sleeve()} == {
        "IONQ",
        "QNT",
        "RGTI",
        "QBTS",
        "INFQ",
        "KEYS",
        "FEIM",
        "PANW",
    }
