"""Trust-tier caps (spec S1, §5.2 step 3; M7). Synthetic domains only."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, select

from aether.classify.caps import cap_for
from aether.classify.pipeline import write_classification
from aether.config import SourceDomain, Sources
from aether.db.engine import write_tx
from aether.db.models import event_classifications
from aether.ingest.news_events import NewsItem, write_news_item
from tests.conftest import seed_tickers

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
SOURCES = Sources(
    domains=(
        SourceDomain(domain="acme-ir.test", tier="T1"),
        SourceDomain(domain="trade-press.test", tier="T2"),
        SourceDomain(domain="other-press.test", tier="T2"),
    ),
    syndicators=("wire-one.test",),
)
TITLE = "ACME Announces Synthetic Widget Contract With Example Agency"


def test_cap_table() -> None:
    assert cap_for([("blog.example.test", "T3", False)]) == 2
    assert cap_for([("a.test", "T3", False), ("b.test", "T3", False)]) == 2
    assert cap_for([("trade-press.test", "T2", False)]) == 3
    # Two hosts of one registrable domain are one source.
    assert cap_for([("trade-press.test", "T2", False), ("news.trade-press.test", "T2", False)]) == 3
    assert cap_for([("trade-press.test", "T2", False), ("other-press.test", "T2", False)]) == 5
    # A syndicated T2 copy is not independent.
    assert cap_for([("trade-press.test", "T2", False), ("other-press.test", "T2", True)]) == 3
    # Any T1 source lifts the cap, syndicated or not.
    assert cap_for([("x.test", "T3", False), ("acme-ir.test", "T1", True)]) == 5


@pytest.fixture
def engine(rw_engine: Engine) -> Engine:
    seed_tickers(rw_engine, [("ACME", "pure_play")])
    return rw_engine


def _write(engine: Engine, url: str, body: str | None = None) -> int:
    with write_tx(engine) as conn:
        r = write_news_item(
            conn,
            NewsItem(
                url=url,
                title=TITLE,
                published_at=NOW,
                origin="rss",
                excerpt=body,
                symbols=("ACME",),
            ),
            SOURCES,
            NOW,
        )
    return r.event_id


def _classify(engine: Engine, event_id: int, raw: int, cls: str = "RISK") -> None:
    with write_tx(engine) as conn:
        write_classification(
            conn,
            event_id,
            cls=cls,
            category="dilution" if cls == "RISK" else "contract_with_value",
            materiality_raw=raw,
            direction=-1,
            directions={"ACME": -1},
            confidence=0.9,
            rationale="test",
            evidence_quote=None,
            rule_id=None,
            model="claude-sonnet-5-5",
            version="classify-v1-test",
            quarantine=False,
            now=NOW,
        )


def _materiality(engine: Engine, event_id: int) -> tuple[int, int]:
    with engine.connect() as conn:
        r = conn.execute(
            select(
                event_classifications.c.materiality_raw, event_classifications.c.materiality
            ).where(event_classifications.c.event_id == event_id)
        ).one()
    return r.materiality_raw, r.materiality


def test_t3_only_event_cannot_exceed_two(engine: Engine) -> None:
    """Spec M7 acceptance: a T3-only event can't exceed materiality 2."""
    eid = _write(engine, "https://random-blog.test/acme-widget")
    _classify(engine, eid, 5)
    assert _materiality(engine, eid) == (5, 2)


def test_single_t2_is_capped_at_three(engine: Engine) -> None:
    eid = _write(engine, "https://trade-press.test/acme-widget")
    _classify(engine, eid, 5)
    assert _materiality(engine, eid) == (5, 3)


def test_merge_of_second_t2_then_t1_lifts_the_cap(engine: Engine) -> None:
    eid = _write(engine, "https://random-blog.test/acme-widget", body="Blog body text " * 8)
    _classify(engine, eid, 5)
    assert _materiality(engine, eid) == (5, 2)
    assert _write(engine, "https://trade-press.test/acme-widget", body="Press one text " * 8) == eid
    assert _materiality(engine, eid) == (5, 3)
    assert _write(engine, "https://other-press.test/acme-widget", body="Press two text " * 8) == eid
    assert _materiality(engine, eid) == (5, 5)


def test_t1_source_merge_lifts_cap_and_raw_below_cap_is_kept(engine: Engine) -> None:
    eid = _write(engine, "https://random-blog.test/acme-widget")
    _classify(engine, eid, 4)
    assert _materiality(engine, eid) == (4, 2)
    _write(engine, "https://acme-ir.test/news/acme-widget", body="Company release text " * 6)
    assert _materiality(engine, eid) == (4, 4)


def test_syndicated_copy_does_not_lift_the_cap(engine: Engine) -> None:
    eid = _write(engine, "https://trade-press.test/acme-widget", body="Same body " * 12)
    _classify(engine, eid, 5)
    # Same body on another T2 domain: syndicated, not independent.
    _write(engine, "https://other-press.test/acme-widget", body="Same body " * 12)
    assert _materiality(engine, eid) == (5, 3)
