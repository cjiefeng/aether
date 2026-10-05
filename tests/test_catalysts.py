"""Catalysts (M8): seed sync, deterministic resolution from events, earnings and lock-ups, the
owner's mark command, and the seed config checks. Every company, URL and event is synthetic
(ACME / EXMP / example.test)."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import Engine, insert, select

from aether.catalysts.mark import CatalystMark, apply_mark
from aether.catalysts.sync import refresh_catalysts
from aether.config import CatalystRules, CatalystsConfig, SeedCatalyst, load_catalysts_config
from aether.db.engine import write_tx
from aether.db.models import (
    catalysts,
    earnings_calendar,
    event_classifications,
    event_tickers,
    events,
    facts,
    filings,
    lockups,
)
from tests.conftest import CONFIG_DIR, seed_tickers

RULES = CatalystRules(
    min_materiality=3,
    lead_days=90,
    grace_days=90,
    earnings_match_days=3,
    lockup_lookahead_days=365,
)
TODAY = date(2026, 10, 5)


def seed(**kw: object) -> SeedCatalyst:
    base: dict[str, object] = {
        "id": "acme_widget",
        "symbol": "ACME",
        "title": "ACME Widget processor (roadmap year 2026)",
        "kind": "roadmap",
        "window_start": "2026-01-01",
        "window_end": "2026-12-31",
        "fact_id": "acme_roadmap",
        "source_url": "https://example.test/roadmap",
        "keywords": ["Widget"],
        "resolve_categories": ["roadmap_hit", "roadmap_slip"],
    }
    return SeedCatalyst.model_validate({**base, **kw})


def config(*seeds: SeedCatalyst) -> CatalystsConfig:
    return CatalystsConfig(rules=RULES, catalysts=seeds or (seed(),))


@pytest.fixture
def db(rw_engine: Engine) -> Engine:
    seed_tickers(rw_engine, [("ACME", "pure_play"), ("EXMP", "pure_play"), ("CTXA", "context")])
    with write_tx(rw_engine) as conn:
        conn.execute(
            insert(facts).values(
                id="acme_roadmap",
                claim="synthetic roadmap fact",
                source_urls='["https://example.test/roadmap"]',
                retrieved_at="2026-10-05",
                status="verified_by_claude",
                synced_at="2026-10-05T00:00:00Z",
            )
        )
    return rw_engine


_n = [0]


def add_event(
    engine: Engine,
    *,
    title: str,
    category: str,
    symbols: tuple[str, ...] = ("ACME",),
    materiality: int = 4,
    direction: int = 1,
    published: str = "2026-09-01T12:00:00Z",
    quarantined: bool = False,
    origin: str = "rss",
    excerpt: str | None = None,
) -> int:
    _n[0] += 1
    url = f"https://example.test/news/{_n[0]}"
    cls = {"earnings_release": "SIGNAL"}.get(category, "SIGNAL")
    with write_tx(engine) as conn:
        eid = conn.execute(
            insert(events)
            .values(
                url_hash=hashlib.sha256(url.encode()).digest(),
                title=title,
                url=url,
                source_domain="example.test",
                trust_tier="T1",
                published_at=published,
                excerpt=excerpt,
                origin=origin,
                quarantined=int(quarantined),
                injection_suspected=int(quarantined),
                created_at=published,
            )
            .returning(events.c.id)
        ).scalar_one()
        for s in symbols:
            conn.execute(insert(event_tickers).values(event_id=eid, symbol=s, direction=direction))
        conn.execute(
            insert(event_classifications).values(
                event_id=eid,
                **{"class": cls},
                category=category,
                materiality_raw=materiality,
                materiality=materiality,
                direction=direction,
                confidence=0.9,
                rule_id="test_rule",
                created_at=published,
            )
        )
    return int(eid)


def row(engine: Engine, key: str) -> dict[str, object]:
    with engine.connect() as conn:
        return dict(conn.execute(select(catalysts).where(catalysts.c.key == key)).one()._mapping)


# --------------------------------------------------------------------------- seeds + events


def test_seed_sync_is_idempotent_and_keeps_status(db: Engine) -> None:
    s1 = refresh_catalysts(db, config(), TODAY)
    assert s1.inserted == 1 and s1.changed == 1
    assert refresh_catalysts(db, config(), TODAY).changed == 0  # idle run writes nothing
    r = row(db, "seed:acme_widget")
    assert r["status"] == "upcoming" and r["fact_id"] == "acme_roadmap"
    # A definition change updates in place.
    s3 = refresh_catalysts(db, config(seed(window_end="2027-03-31")), TODAY)
    assert s3.updated == 1 and row(db, "seed:acme_widget")["window_end"] == "2027-03-31"


def test_test_event_resolves_catalyst_citing_it(db: Engine) -> None:
    """Acceptance: a test event resolves a catalyst."""
    refresh_catalysts(db, config(), TODAY)
    eid = add_event(db, title="ACME delivers its Widget processor", category="roadmap_hit")
    s = refresh_catalysts(db, config(), TODAY)
    assert [r.catalyst_id for r in s.resolved] == [row(db, "seed:acme_widget")["id"]]
    r = row(db, "seed:acme_widget")
    assert (r["status"], r["resolution"], r["resolved_by_event_id"]) == ("hit", "event", eid)
    assert f"#{eid}" in str(r["note"])
    assert refresh_catalysts(db, config(), TODAY).changed == 0


def test_slip_event_marks_slipped(db: Engine) -> None:
    refresh_catalysts(db, config(), TODAY)
    add_event(db, title="ACME pushes Widget to next year", category="roadmap_slip", direction=-1)
    refresh_catalysts(db, config(), TODAY)
    assert row(db, "seed:acme_widget")["status"] == "slipped"


@pytest.mark.parametrize(
    "kw",
    [
        {"quarantined": True},
        {"materiality": 2},
        {"title": "ACME ships a gadget"},  # keyword missing
        {"title": "ACME Widgetry update"},  # not a whole word
        {"category": "partnership_no_value"},
        {"symbols": ("EXMP",)},  # another company's event
        {"published": "2025-09-01T00:00:00Z"},  # before window_start - lead_days
    ],
)
def test_non_qualifying_events_do_not_resolve(db: Engine, kw: dict[str, object]) -> None:
    args: dict[str, object] = {
        "title": "ACME delivers its Widget processor",
        "category": "roadmap_hit",
    }
    add_event(db, **{**args, **kw})  # type: ignore[arg-type]
    refresh_catalysts(db, config(), TODAY)
    assert row(db, "seed:acme_widget")["status"] == "upcoming"


def test_theme_event_without_tickers_resolves(db: Engine) -> None:
    add_event(db, title="Widget processor delivered", category="roadmap_hit", symbols=())
    refresh_catalysts(db, config(), TODAY)
    assert row(db, "seed:acme_widget")["status"] == "hit"


def test_context_ticker_catalyst_matches_on_keyword(db: Engine) -> None:
    """News isn't tagged with context tickers (e.g. IBM), so the keyword alone ties it."""
    cfg = config(seed(id="ctxa_widget", symbol="CTXA"))
    add_event(db, title="CTXA unveils Widget", category="roadmap_hit", symbols=("ACME",))
    refresh_catalysts(db, cfg, TODAY)
    assert row(db, "seed:ctxa_widget")["status"] == "hit"


def test_program_decision_uses_event_direction(db: Engine) -> None:
    cfg = config(
        seed(
            id="acme_stage_c",
            kind="program",
            window_start="2026-06-01",
            window_end=None,
            keywords=["Stage C"],
            resolve_categories=["qbi_stage_change"],
        )
    )
    add_event(db, title="ACME not selected for Stage C", category="qbi_stage_change", direction=-1)
    refresh_catalysts(db, cfg, TODAY)
    assert row(db, "seed:acme_stage_c")["status"] == "slipped"


def test_window_passed_slips_but_open_window_never_does(db: Engine) -> None:
    cfg = config(
        seed(id="old", window_start="2025-01-01", window_end="2025-06-30"),
        seed(id="open", window_start="2025-01-01", window_end=None),
    )
    refresh_catalysts(db, cfg, TODAY)
    old = row(db, "seed:old")
    assert (old["status"], old["resolution"]) == ("slipped", "window_passed")
    assert row(db, "seed:open")["status"] == "upcoming"


# --------------------------------------------------------------------------- earnings + lock-ups


def _earnings(engine: Engine, d: str, status: str = "scheduled") -> None:
    with write_tx(engine) as conn:
        conn.execute(
            insert(earnings_calendar).values(
                symbol="ACME",
                date=d,
                status=status,
                source="yfinance",
                fetched_at="2026-10-05T00:00:00Z",
            )
        )


def test_earnings_hit_by_8k_and_rescheduled_date_cancelled(db: Engine) -> None:
    _earnings(db, "2026-09-10")
    _earnings(db, "2026-11-05")
    refresh_catalysts(db, config(), TODAY)
    eid = add_event(
        db,
        title="ACME 8-K Items 2.02",
        category="earnings_release",
        origin="edgar",
        direction=0,
        published="2026-09-11T21:05:00Z",
    )
    # The company moves the November date: the calendar job replaces it.
    with write_tx(db) as conn:
        conn.execute(earnings_calendar.delete().where(earnings_calendar.c.date == "2026-11-05"))
    _earnings(db, "2026-11-12")
    refresh_catalysts(db, config(), TODAY)
    past = row(db, "earnings:ACME:2026-09-10")
    assert (past["status"], past["resolved_by_event_id"]) == ("hit", eid)
    moved = row(db, "earnings:ACME:2026-11-05")
    assert (moved["status"], moved["resolution"]) == ("cancelled", "rescheduled")
    assert row(db, "earnings:ACME:2026-11-12")["status"] == "upcoming"


def test_lockup_resolves_by_date_and_seed_covers_duplicate(db: Engine) -> None:
    with write_tx(db) as conn:
        for acc, expiry in (
            ("0000000001-26-000001", "2026-10-01"),
            ("0000000001-26-000002", "2026-12-01"),
        ):
            conn.execute(
                insert(filings).values(
                    accession=acc,
                    symbol="ACME",
                    cik="0000000001",
                    form="424B4",
                    filed_at="2026-06-01",
                    url=f"https://example.test/{acc}",
                    fetched_at="2026-06-01T00:00:00Z",
                )
            )
            conn.execute(
                insert(lockups).values(
                    accession=acc,
                    symbol="ACME",
                    prospectus_date="2026-06-01",
                    lockup_days=120,
                    expiry_date=expiry,
                    excerpt="synthetic",
                )
            )
    cfg = config(
        seed(
            id="acme_lockup",
            kind="lockup",
            window_start="2026-12-01",
            window_end="2026-12-01",
            keywords=[],
            resolve_categories=[],
        )
    )
    refresh_catalysts(db, cfg, TODAY)
    past = row(db, "lockup:0000000001-26-000001")
    assert (past["status"], past["resolution"]) == ("hit", "date")
    with db.connect() as conn:
        keys = set(conn.execute(select(catalysts.c.key)).scalars())
    assert "lockup:0000000001-26-000002" not in keys  # the seed covers it
    assert row(db, "seed:acme_lockup")["status"] == "upcoming"


# --------------------------------------------------------------------------- owner marks


def test_owner_mark_sticks_and_reopen_allows_rules_again(db: Engine) -> None:
    refresh_catalysts(db, config(), TODAY)
    cid = int(row(db, "seed:acme_widget")["id"])  # type: ignore[call-overload]
    apply_mark(db, CatalystMark(catalyst_id=cid, status="cancelled", note="program dropped"))
    add_event(db, title="ACME delivers its Widget processor", category="roadmap_hit")
    refresh_catalysts(db, config(), TODAY)
    r = row(db, "seed:acme_widget")
    assert (r["status"], r["resolution"], r["note"]) == ("cancelled", "owner", "program dropped")
    apply_mark(db, CatalystMark(catalyst_id=cid, status="upcoming"))
    refresh_catalysts(db, config(), TODAY)
    assert row(db, "seed:acme_widget")["status"] == "hit"


def test_mark_rejects_unknown_ids(db: Engine) -> None:
    refresh_catalysts(db, config(), TODAY)
    with pytest.raises(ValueError, match="unknown catalyst"):
        apply_mark(db, CatalystMark(catalyst_id=999, status="hit"))
    cid = int(row(db, "seed:acme_widget")["id"])  # type: ignore[call-overload]
    with pytest.raises(ValueError, match="unknown event"):
        apply_mark(db, CatalystMark(catalyst_id=cid, status="hit", event_id=999))


# --------------------------------------------------------------------------- config


def test_repo_seed_is_valid_and_linked_to_facts() -> None:
    cfg = load_catalysts_config(CONFIG_DIR)
    ids = {c.id for c in cfg.catalysts}
    assert {"ibm_kookaburra", "qbi_stage_c_ionq", "qbi_stage_c_qnt", "qnt_ipo_lockup"} <= ids
    lock = next(c for c in cfg.catalysts if c.id == "qnt_ipo_lockup")
    assert (lock.window_start, lock.fact_id) == ("2026-11-30", "qnt_lockup_expiry")


def _write(tmp: Path, body: dict[str, object]) -> Path:
    for name in ("facts.yaml", "watchlist.yaml"):
        (tmp / name).write_text((CONFIG_DIR / name).read_text("utf-8"), "utf-8")
    (tmp / "catalysts_seed.yaml").write_text(json.dumps(body), "utf-8")
    return tmp


def test_seed_with_unknown_fact_or_symbol_is_rejected(tmp_path: Path) -> None:
    good = json.loads(json.dumps(seed().model_dump()))
    rules = RULES.model_dump()
    _write(tmp_path, {"rules": rules, "catalysts": [{**good, "symbol": "IONQ", "fact_id": "nope"}]})
    with pytest.raises(ValueError, match="unknown fact_id"):
        load_catalysts_config(tmp_path)
    _write(
        tmp_path,
        {"rules": rules, "catalysts": [{**good, "symbol": "ZZZZ", "fact_id": "ibm_roadmap_ftqc"}]},
    )
    with pytest.raises(ValueError, match="not on the watchlist"):
        load_catalysts_config(tmp_path)


def test_seed_forbids_free_text_and_unknown_keys() -> None:
    with pytest.raises(ValueError):
        seed(commentary="strongest roadmap")
    with pytest.raises(ValueError):
        seed(title="Great!! <b>buy</b>")
    with pytest.raises(ValueError):
        seed(keywords=[])  # resolving categories need a keyword
