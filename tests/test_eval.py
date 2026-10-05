"""`make eval` machinery (M7, spec §5.3), offline: scoring maths, adversarial construction, the
end-to-end run against the fake API, and loading results into eval_runs. Synthetic rows only
(ACME, example.test); the real golden set is only validated, never sent anywhere here."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import Engine, select

from aether.classify.eval import (
    ADVERSARIAL_SUFFIXES,
    GOLDEN_PATH,
    GoldenRow,
    Prediction,
    format_report,
    load_golden,
    pick_adversarial,
    run_eval,
    score,
    sync_eval_results,
)
from aether.classify.pipeline import ClassifierContext
from aether.config import CATEGORY_CLASS, load_rubric
from aether.db.models import eval_runs
from tests.conftest import CONFIG_DIR
from tests.llm_fakes import (
    CLASSIFIER_MODEL,
    FakeApi,
    classify_message,
    classify_payload,
    llm_config,
    llm_settings,
    make_client,
)

NOW = datetime(2026, 10, 5, 4, 0, tzinfo=UTC)
REPO = Path(__file__).resolve().parents[1]


def _row(i: int, cls: str, category: str, materiality: int = 2, **kw: object) -> GoldenRow:
    data: dict[str, object] = {
        "id": f"g{i:03d}",
        "source": "synthetic-test",
        "url": f"https://example.test/{i}",
        "title": f"ACME synthetic item {i}",
        "excerpt": f"ACME synthetic contract number {i} for widgets.",
        "domain": "acme-ir.test",
        "tier": "T1",
        "published_at": "2026-10-01T00:00:00Z",
        "tickers": ["ACME"],
        "label": {"class": cls, "category": category, "materiality": materiality},
        "labeled_by": "owner",
    }
    data.update(kw)
    return GoldenRow.model_validate(data)


def _pred(cls: str | None, materiality: int | None = 2, inj: bool = False) -> Prediction:
    cat = next((c for c, k in CATEGORY_CLASS.items() if k == cls), None)
    return Prediction(cls, cat, materiality, materiality, inj, False, None, None if cls else "bad")


def test_score_maths_confusion_and_acceptance() -> None:
    rows = [
        _row(1, "SIGNAL", "earnings_release"),
        _row(2, "SIGNAL", "earnings_release"),
        _row(3, "NOISE", "analyst_rating"),
        _row(4, "RISK", "dilution"),
        _row(5, "RISK", "dilution"),
    ]
    preds = {
        "g001": _pred("SIGNAL"),
        "g002": _pred("NOISE"),
        "g003": _pred("NOISE"),
        "g004": _pred("RISK"),
        "g005": _pred(None, None),  # invalid output: counts as a miss
    }
    m = score(rows, preds, [(rows[0], _pred("SIGNAL", inj=True))] * 5)
    assert m["n"] == 5 and m["class_agreement"] == 0.6 and m["errors"] == 1
    assert m["confusion"]["SIGNAL"] == {"NOISE": 1, "SIGNAL": 1}
    assert m["confusion"]["RISK"] == {"ERROR": 1, "RISK": 1}
    assert m["per_class"]["NOISE"] == {"n": 1, "precision": 0.5, "recall": 1.0}
    assert m["per_class"]["RISK"]["recall"] == 0.5 and m["risk_recall"] == 0.5
    assert m["adversarial"]["flagged_rate"] == 1.0
    assert m["acceptance"] == {
        "class_agreement": False,
        "risk_recall": False,
        "adversarial_flagged": True,
        "passed": False,
    }
    assert "dilution" in m["under_sampled"] and not m["provisional"]
    assert "Confusion" in format_report(
        {"prompt_version": "v", "model": "m", "created_at": "t", "metrics": m, "cost_usd": "0"}
    )


def test_no_risk_rows_means_risk_recall_fails_and_is_reported() -> None:
    rows = [_row(1, "NOISE", "analyst_rating")]
    m = score(rows, {"g001": _pred("NOISE")}, [])
    assert m["risk_recall"] is None
    assert m["acceptance"]["risk_recall"] is False
    assert m["acceptance"]["adversarial_flagged"] is False  # needs ≥5 cases


def test_unflagged_or_changed_adversarial_case_fails() -> None:
    rows = [_row(i, "NOISE", "analyst_rating") for i in range(1, 6)]
    preds = {r.id: _pred("NOISE", 1) for r in rows}
    adv = [(r, _pred("NOISE", 1, inj=True)) for r in rows[:4]] + [(rows[4], _pred("SIGNAL", 5))]
    m = score(rows, preds, adv)
    assert (
        m["adversarial"]["flagged"] == 4
        and m["adversarial"]["unchanged_class_and_materiality"] == 4
    )
    assert m["acceptance"]["adversarial_flagged"] is False


def test_pick_adversarial_spreads_over_classes_deterministically() -> None:
    rows = [_row(i, "NOISE", "analyst_rating") for i in range(1, 5)]
    rows += [_row(10, "SIGNAL", "earnings_release"), _row(20, "RISK", "dilution", excerpt=None)]
    picked = pick_adversarial(rows, 5)
    assert [r.id for r in picked] == ["g010", "g001", "g002", "g003", "g004"]
    assert picked == pick_adversarial(list(reversed(rows)), 5)


def test_run_eval_end_to_end_against_the_fake_api(rw_engine: Engine, migrated_db: Path) -> None:
    rows = [_row(i, "SIGNAL", "contract_with_value", 4) for i in range(1, 6)]
    ok = classify_message(classify_payload(evidence_quote="synthetic contract number"))
    flagged = classify_message(
        classify_payload(evidence_quote="synthetic contract number", injection_suspected=True)
    )
    api = FakeApi(messages=[ok] * 5 + [flagged] * 5)
    llm = make_client(rw_engine, llm_settings(migrated_db), api, clock=lambda: NOW)
    ctx = ClassifierContext(load_rubric(CONFIG_DIR).classifier, llm_config().classify,
                            CLASSIFIER_MODEL, {"ACME": "Acme Corp"})  # fmt: skip
    result = run_eval(llm, rw_engine, ctx, rows, now=NOW)
    m = result["metrics"]
    assert m["class_agreement"] == 1.0 and m["adversarial"]["flagged"] == 5
    assert m["adversarial"]["unchanged_class_and_materiality"] == 5
    bodies = api.bodies()
    assert len(bodies) == 10 and all("tools" not in b for b in bodies)
    # Adversarial inputs are the real excerpt plus a harness suffix, inside the untrusted block.
    adv_text = bodies[5]["messages"][0]["content"]
    assert ADVERSARIAL_SUFFIXES[0].split(" ")[0] in adv_text and 'id="g001-adv"' in adv_text
    assert all(a["synthetic_adversarial"] for a in result["adversarial_items"])
    assert result["prompt_version"] == ctx.version and float(result["cost_usd"]) > 0


def test_eval_results_load_into_eval_runs_once(rw_engine: Engine, tmp_path: Path) -> None:
    rows = [_row(1, "NOISE", "analyst_rating")]
    m = score(rows, {"g001": _pred("NOISE")}, [])
    (tmp_path / "classify-v1-abc.json").write_text(
        json.dumps(
            {"prompt_version": "classify-v1-abc", "model": "m",
             "created_at": "2026-10-05T00:00:00Z", "metrics": m}
        )
    )  # fmt: skip
    assert sync_eval_results(rw_engine, tmp_path) == 1
    assert sync_eval_results(rw_engine, tmp_path) == 0
    with rw_engine.connect() as conn:
        r = conn.execute(select(eval_runs)).one()
    assert r.prompt_version == "classify-v1-abc" and r.passed == 0 and r.n_items == 1
    assert sync_eval_results(rw_engine, tmp_path / "missing") == 0


def test_shipped_golden_set_is_valid_and_real_looking() -> None:
    """The committed golden set loads, has ≥60 rows, real https URLs on known domains and no
    synthetic test names."""
    rows = load_golden(REPO / GOLDEN_PATH)
    assert len(rows) >= 60
    assert all(r.url.startswith("https://") for r in rows)
    assert not any("example.test" in r.url or "ACME" in r.title for r in rows)
    assert {r.labeled_by for r in rows} <= {"owner", "claude_proposed"}
