"""M3 alerts: outbox dedupe, candidates, Telegram owner-only guard (S7). Synthetic ACME data; the
Telegram transport is mocked with respx (no network)."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import Engine, func, insert, select, update

from aether.alerts import candidates as cand
from aether.alerts.dispatch import enqueue_test_alert, run_alerts
from aether.alerts.telegram import (
    TelegramBot,
    TelegramConfig,
    TelegramError,
    TelegramService,
    redact,
)
from aether.alerts.telegram_guard import DropLog, chats_to_leave, is_owner
from aether.classify.rules import classify_filing
from aether.config import load_alerts_config, load_rubric
from aether.db.dialect import upsert
from aether.db.engine import write_tx
from aether.db.models import alerts, earnings_calendar, events, filings, job_runs, lockups
from aether.db.types import to_iso, utcnow_iso
from aether.edgar.submissions import FilingMeta
from aether.ingest.edgar import filing_row, write_event
from aether.runs import JobResult, run_job
from tests.conftest import CONFIG_DIR, make_settings, seed_tickers

CFG = load_alerts_config(CONFIG_DIR)
RUBRIC = load_rubric(CONFIG_DIR)
PARAMS = RUBRIC.risk_flags
OWNER = 424242  # synthetic Telegram user id
OTHER = 515151
TOKEN = "test-token-not-real"  # synthetic
API = f"https://api.telegram.org/bot{TOKEN}"
NOW = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)  # 07:00 US/Eastern, so US date 2026-03-10


def no_sleep(_s: float) -> None:
    return None


# --------------------------------------------------------------------------- fixtures


class FakeTelegram:
    """respx routes for the Bot API, recording every call."""

    def __init__(self, router: respx.MockRouter, chat: dict[str, Any] | None = None) -> None:
        self.calls: dict[str, list[dict[str, Any]]] = {}
        self.chat = chat if chat is not None else {"id": OWNER, "type": "private"}
        self.updates: list[dict[str, Any]] = []
        self.send_errors: list[dict[str, Any]] = []
        router.post(url__regex=rf"^{API}/(\w+)$").mock(side_effect=self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[1]
        body = json.loads(request.content or b"{}")
        self.calls.setdefault(method, []).append(body)
        if method == "getChat":
            return httpx.Response(200, json={"ok": True, "result": self.chat})
        if method == "getUpdates":
            ups, self.updates = self.updates, []
            return httpx.Response(200, json={"ok": True, "result": ups})
        if method == "sendMessage" and self.send_errors:
            return httpx.Response(200, json=self.send_errors.pop(0))
        if method == "sendMessage":
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
        return httpx.Response(200, json={"ok": True, "result": True})

    def n(self, method: str) -> int:
        return len(self.calls.get(method, []))


@pytest.fixture
def tg() -> Iterator[tuple[FakeTelegram, TelegramService]]:
    with respx.mock(assert_all_called=False) as router:
        fake = FakeTelegram(router)
        bot = TelegramBot(TelegramConfig(TOKEN, OWNER, OWNER), httpx.Client(), sleep=no_sleep)
        yield fake, TelegramService(bot)
        bot.close()


def _meta(acc: str, form: str, filed: str, items: tuple[str, ...] = ()) -> FilingMeta:
    return FilingMeta(
        accession=acc,
        cik="0000000001",
        form=form,
        filed_at=filed,
        accepted_at=f"{filed}T21:00:00Z",
        report_date=None,
        items=items,
        primary_doc="acme.htm",
        primary_doc_description=None,
        is_xbrl=False,
    )


def add_filing_event(engine: Engine, f: FilingMeta, symbol: str = "ACME") -> int:
    """The M2 ingest path: filing row + deterministic rule event."""
    hit = classify_filing(symbol, f, RUBRIC)
    assert hit is not None
    with write_tx(engine) as conn:
        upsert(conn, filings, [filing_row(symbol, f, utcnow_iso())], key_cols=["accession"])
        return write_event(conn, symbol, f, hit, utcnow_iso())


@pytest.fixture
def acme(rw_engine: Engine) -> Engine:
    seed_tickers(rw_engine, [("ACME", "pure_play")])
    return rw_engine


def alert_rows(engine: Engine) -> list[Any]:
    with engine.connect() as conn:
        return list(conn.execute(select(alerts).order_by(alerts.c.id)).all())


def run(engine: Engine, service: TelegramService | None, now: datetime = NOW) -> JobResult:
    reason = None if service else "TELEGRAM_BOT_TOKEN not set"
    return run_alerts(engine, CFG, PARAMS, service, reason, now=now, sleep=no_sleep)


# --------------------------------------------------------------------------- acceptance


def test_synthetic_s3_sends_one_message_and_no_duplicate_on_rerun(
    acme: Engine, tg: tuple[FakeTelegram, TelegramService]
) -> None:
    fake, service = tg
    add_filing_event(acme, _meta("0000000001-26-000010", "S-3", "2026-03-09"))

    res = run(acme, service)
    assert fake.n("sendMessage") == 1
    msg = fake.calls["sendMessage"][0]
    assert msg["chat_id"] == OWNER
    assert "parse_mode" not in msg
    assert msg["link_preview_options"] == {"is_disabled": True}
    assert msg["text"].startswith("RISK · ACME · dilution (materiality 3/5)")
    assert "https://www.sec.gov/" in msg["text"]
    assert res.provider == "telegram" and res.warning is None
    # Verified the chat before sending, and removed any webhook.
    assert fake.n("deleteWebhook") == 1 and fake.calls["getChat"][0] == {"chat_id": OWNER}

    run(acme, service)
    run(acme, service, now=NOW + timedelta(minutes=10))
    assert fake.n("sendMessage") == 1
    rows = alert_rows(acme)
    assert len(rows) == 1
    assert (rows[0].kind, rows[0].channel, rows[0].status, rows[0].attempts) == (
        "risk_event",
        "telegram",
        "sent",
        1,
    )
    assert rows[0].dedupe_key.startswith("risk_event:") and rows[0].event_id is not None


def test_failing_job_alert_fires_once_then_recovers(
    rw_engine: Engine, tg: tuple[FakeTelegram, TelegramService]
) -> None:
    fake, service = tg

    def add_run(job: str, status: str, at: datetime, error: str | None = None) -> None:
        with write_tx(rw_engine) as conn:
            conn.execute(
                insert(job_runs).values(
                    job=job,
                    started_at=to_iso(at),
                    finished_at=to_iso(at),
                    status=status,
                    error=error,
                )
            )

    add_run("prices", "ok", NOW - timedelta(hours=50))
    add_run("prices", "failed", NOW - timedelta(hours=26), "ProviderError('both down')")
    add_run("prices", "failed", NOW - timedelta(hours=2), "ProviderError('both down')")
    # A job failing for under 24h doesn't alert.
    add_run("edgar", "failed", NOW - timedelta(hours=3), "boom")

    run(rw_engine, service)
    run(rw_engine, service)
    texts = [c["text"] for c in fake.calls["sendMessage"]]
    assert len(texts) == 1
    assert texts[0].startswith("Ops · job prices failing for more than 24h")
    assert "2 failed run(s)" in texts[0] and "both down" in texts[0]

    add_run("prices", "ok", NOW + timedelta(minutes=5))
    run(rw_engine, service, now=NOW + timedelta(minutes=10))
    run(rw_engine, service, now=NOW + timedelta(minutes=20))
    texts = [c["text"] for c in fake.calls["sendMessage"]]
    assert len(texts) == 2 and texts[1].startswith("Ops · job prices recovered")
    assert [r.kind for r in alert_rows(rw_engine)] == ["job_failing", "job_recovered"]


def test_failing_job_detected_from_real_run_job(rw_engine: Engine) -> None:
    def boom() -> JobResult:
        raise RuntimeError("synthetic failure")

    run_job(rw_engine, "acme_job", boom)
    with write_tx(rw_engine) as conn:
        conn.execute(update(job_runs).values(started_at=to_iso(NOW - timedelta(hours=25))))
    with rw_engine.connect() as conn:
        found = cand.job_health(conn, CFG, NOW)
    assert [c.dedupe_key for c in found] == [
        f"job_failing:acme_job:{to_iso(NOW - timedelta(hours=25))}"
    ]


# --------------------------------------------------------------------------- S7 guard


def _msg(sender: int, chat_id: int, chat_type: str, text: str = "hello") -> dict[str, Any]:
    return {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "from": {"id": sender, "is_bot": False, "first_name": "X"},
            "chat": {"id": chat_id, "type": chat_type},
            "text": text,
        },
    }


def test_is_owner_requires_all_three_conditions() -> None:
    assert is_owner(_msg(OWNER, OWNER, "private"), OWNER)
    assert not is_owner(_msg(OTHER, OTHER, "private"), OWNER)
    assert not is_owner(_msg(OWNER, -100123, "group"), OWNER)
    assert not is_owner(_msg(OWNER, OTHER, "private"), OWNER)
    assert not is_owner(_msg(OWNER, OWNER, "supergroup"), OWNER)
    callback = {"update_id": 2, "callback_query": {"id": "q", "from": {"id": OWNER}}}
    inline = {"update_id": 3, "inline_query": {"id": "i", "from": {"id": OWNER}, "query": ""}}
    assert not is_owner(callback, OWNER) and not is_owner(inline, OWNER)
    # Malformed shapes never pass.
    assert not is_owner({"update_id": 4, "message": "x"}, OWNER)
    bogus = _msg(OWNER, OWNER, "private")
    bogus["message"]["from"]["id"] = True
    assert not is_owner(bogus, OWNER)


def test_update_from_other_user_is_dropped_without_reply(
    tg: tuple[FakeTelegram, TelegramService], caplog: pytest.LogCaptureFixture
) -> None:
    fake, service = tg
    fake.updates = [_msg(OTHER, OTHER, "private", text="secret text")]
    with caplog.at_level(logging.WARNING):
        assert service.poll_inbound(timeout=0) == 1
    assert set(fake.calls) == {"deleteWebhook", "getUpdates"}  # no sendMessage, no leaveChat
    assert f"sender_id={OTHER}" in caplog.text and "chat_type=private" in caplog.text
    assert "secret text" not in caplog.text


def test_group_update_from_owner_is_dropped_and_bot_leaves(
    tg: tuple[FakeTelegram, TelegramService],
) -> None:
    fake, service = tg
    fake.updates = [_msg(OWNER, -100777, "group")]
    service.poll_inbound(timeout=0)
    assert fake.calls["leaveChat"] == [{"chat_id": -100777}]
    assert fake.n("sendMessage") == 0


def test_added_to_group_or_channel_post_triggers_leave(
    tg: tuple[FakeTelegram, TelegramService],
) -> None:
    fake, service = tg
    added = {
        "update_id": 10,
        "my_chat_member": {
            "chat": {"id": -100888, "type": "supergroup"},
            "from": {"id": OTHER},
            "date": 0,
            "old_chat_member": {"status": "left"},
            "new_chat_member": {"status": "member"},
        },
    }
    post = {"update_id": 11, "channel_post": {"chat": {"id": -100999, "type": "channel"}}}
    kicked = {
        "update_id": 12,
        "my_chat_member": {
            "chat": {"id": -100555, "type": "group"},
            "new_chat_member": {"status": "kicked"},
        },
    }
    assert chats_to_leave(kicked) == set()
    fake.updates = [added, post, kicked]
    service.poll_inbound(timeout=0)
    assert [c["chat_id"] for c in fake.calls["leaveChat"]] == [-100888, -100999]
    # The offset advanced past the last update.
    service.poll_inbound(timeout=0)
    assert fake.calls["getUpdates"][-1]["offset"] == 13


def test_owner_private_message_gets_no_reply_in_mvp(
    tg: tuple[FakeTelegram, TelegramService],
) -> None:
    fake, service = tg
    fake.updates = [_msg(OWNER, OWNER, "private")]
    service.poll_inbound(timeout=0)
    assert set(fake.calls) == {"deleteWebhook", "getUpdates"}


def test_drop_log_is_rate_limited(caplog: pytest.LogCaptureFixture) -> None:
    drops = DropLog(per_minute=2)
    with caplog.at_level(logging.WARNING):
        for _ in range(5):
            drops.drop(_msg(OTHER, OTHER, "private"), now=100.0)
        drops.drop(_msg(OTHER, OTHER, "private"), now=161.0)
    assert caplog.text.count("dropped message update") == 3
    assert "3 more updates dropped" in caplog.text


# --------------------------------------------------------------------------- fail-closed config


@pytest.mark.parametrize("owner", [None, "", "@owner", "-5", "12x", "0"])
def test_token_without_valid_owner_id_disables_module(
    owner: str | None, migrated_db: Any, caplog: pytest.LogCaptureFixture
) -> None:
    settings = make_settings(migrated_db, telegram_bot_token=TOKEN, telegram_allowed_user_id=owner)
    with caplog.at_level(logging.ERROR):
        config, reason = TelegramConfig.from_settings(settings)
    assert config is None and reason and "TELEGRAM_ALLOWED_USER_ID" in reason
    assert "fail closed" in caplog.text and TOKEN not in caplog.text


def test_chat_id_must_equal_owner(migrated_db: Any) -> None:
    s = make_settings(
        migrated_db,
        telegram_bot_token=TOKEN,
        telegram_allowed_user_id=str(OWNER),
        telegram_chat_id="-100123",
    )
    assert TelegramConfig.from_settings(s)[0] is None
    ok = make_settings(migrated_db, telegram_bot_token=TOKEN, telegram_allowed_user_id=str(OWNER))
    config, reason = TelegramConfig.from_settings(ok)
    assert config is not None and reason is None and config.chat_id == OWNER
    assert TOKEN not in repr(config)
    assert TelegramConfig.from_settings(make_settings(migrated_db))[0] is None  # no token


def test_disabled_module_keeps_alerts_on_dashboard(acme: Engine) -> None:
    add_filing_event(acme, _meta("0000000001-26-000011", "S-3", "2026-03-09"))
    with respx.mock(assert_all_called=False) as router:
        route = router.route(host="api.telegram.org")
        res = run(acme, None)
        run(acme, None)
    assert not route.called
    assert res.provider == "dashboard" and res.warning == "TELEGRAM_BOT_TOKEN not set"
    rows = alert_rows(acme)
    assert [(r.channel, r.status) for r in rows] == [("dashboard", "dashboard_only")]


@pytest.mark.parametrize(
    "chat", [{"id": -100123, "type": "group"}, {"id": OTHER, "type": "private"}]
)
def test_getchat_not_owner_private_chat_sends_nothing(acme: Engine, chat: dict[str, Any]) -> None:
    add_filing_event(acme, _meta("0000000001-26-000012", "S-3", "2026-03-09"))
    with respx.mock(assert_all_called=False) as router:
        fake = FakeTelegram(router, chat=chat)
        bot = TelegramBot(TelegramConfig(TOKEN, OWNER, OWNER), httpx.Client(), sleep=no_sleep)
        service = TelegramService(bot)
        res = run(acme, service)
        add_filing_event(acme, _meta("0000000001-26-000013", "S-3ASR", "2026-03-09"))
        res2 = run(acme, service)
    assert fake.n("sendMessage") == 0
    assert service.blocked and res.warning == service.blocked
    assert res2.provider == "dashboard"
    statuses = [(r.channel, r.status) for r in alert_rows(acme)]
    assert statuses == [("telegram", "failed"), ("dashboard", "dashboard_only")]


# --------------------------------------------------------------------------- client hygiene


def test_errors_never_contain_the_token(acme: Engine, caplog: pytest.LogCaptureFixture) -> None:
    real_looking = "1" * 9 + ":" + "Ab" * 18  # built at runtime; synthetic
    assert redact(f"https://x/bot{real_looking}/m", "") == "https://x/bot<redacted>/m"

    def explode(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

    add_filing_event(acme, _meta("0000000001-26-000014", "S-3", "2026-03-09"))
    with respx.mock(assert_all_called=False) as router, caplog.at_level(logging.DEBUG):
        router.route(host="api.telegram.org").mock(side_effect=explode)
        bot = TelegramBot(TelegramConfig(TOKEN, OWNER, OWNER), httpx.Client(), sleep=no_sleep)
        service = TelegramService(bot)
        with pytest.raises(TelegramError) as ei:
            bot.get_chat(OWNER)
        assert TOKEN not in str(ei.value) and "<redacted>" in str(ei.value)
        assert ei.value.__cause__ is None and ei.value.__suppress_context__
        result = run_job(acme, "alerts", lambda: run(acme, service))
    assert result is not None and result.warning and "telegram unreachable" in result.warning
    with acme.connect() as conn:
        err = conn.execute(select(job_runs.c.error).where(job_runs.c.job == "alerts")).scalar()
    assert err and TOKEN not in err
    assert TOKEN not in caplog.text
    # Transient: the alert stays pending for the next run.
    assert [r.status for r in alert_rows(acme)] == ["pending"]


def test_429_is_retried_after_retry_after(tg: tuple[FakeTelegram, TelegramService]) -> None:
    fake, service = tg
    slept: list[float] = []
    service.bot._sleep = slept.append
    fake.send_errors = [
        {
            "ok": False,
            "error_code": 429,
            "description": "Too Many Requests: retry after 3",
            "parameters": {"retry_after": 3},
        },
    ]
    service.bot.send_message("hi")
    assert fake.n("sendMessage") == 2 and slept == [3]


def test_send_failures_retry_then_fail(
    acme: Engine, tg: tuple[FakeTelegram, TelegramService]
) -> None:
    fake, service = tg
    add_filing_event(acme, _meta("0000000001-26-000015", "S-3", "2026-03-09"))
    err = {"ok": False, "error_code": 400, "description": "Bad Request: synthetic"}
    fake.send_errors = [err] * CFG.max_attempts
    for i in range(CFG.max_attempts):
        run(acme, service, now=NOW + timedelta(minutes=i))
    row = alert_rows(acme)[0]
    assert (row.status, row.attempts) == ("failed", CFG.max_attempts)
    assert "synthetic" in row.last_error
    run(acme, service, now=NOW + timedelta(hours=1))
    assert fake.n("sendMessage") == CFG.max_attempts


def test_pending_alert_expires_instead_of_arriving_late(acme: Engine) -> None:
    add_filing_event(acme, _meta("0000000001-26-000016", "S-3", "2026-03-09"))
    with respx.mock(assert_all_called=False) as router:
        router.route(host="api.telegram.org").mock(return_value=httpx.Response(502, text="x"))
        bot = TelegramBot(TelegramConfig(TOKEN, OWNER, OWNER), httpx.Client(), sleep=no_sleep)
        run(acme, TelegramService(bot))
    assert alert_rows(acme)[0].status == "pending"
    with respx.mock(assert_all_called=False) as router:
        fake = FakeTelegram(router)
        bot = TelegramBot(TelegramConfig(TOKEN, OWNER, OWNER), httpx.Client(), sleep=no_sleep)
        run(acme, TelegramService(bot), now=NOW + timedelta(hours=CFG.pending_expiry_hours + 1))
    assert fake.n("sendMessage") == 0
    assert alert_rows(acme)[0].status == "expired"


def test_test_alert_goes_through_outbox(
    rw_engine: Engine, tg: tuple[FakeTelegram, TelegramService]
) -> None:
    fake, service = tg
    key = enqueue_test_alert(rw_engine, telegram=True, now=NOW)
    run(rw_engine, service)
    assert key.startswith("test:") and fake.n("sendMessage") == 1
    assert "test alert" in fake.calls["sendMessage"][0]["text"]


# --------------------------------------------------------------------------- candidates


def test_risk_event_filters(acme: Engine) -> None:
    add_filing_event(acme, _meta("0000000001-26-000020", "S-3", "2026-03-09"))  # alerts
    add_filing_event(acme, _meta("0000000001-26-000021", "S-3", "2026-02-01"))  # too old
    add_filing_event(acme, _meta("0000000001-26-000022", "424B7", "2026-03-09"))  # materiality 2
    add_filing_event(acme, _meta("0000000001-26-000023", "8-K", "2026-03-09", ("2.02",)))  # SIGNAL
    q = add_filing_event(acme, _meta("0000000001-26-000024", "S-1", "2026-03-09"))
    with write_tx(acme) as conn:
        conn.execute(update(events).where(events.c.id == q).values(quarantined=1))
    with acme.connect() as conn:
        found = cand.risk_events(conn, CFG, NOW)
    assert [c.payload["category"] for c in found] == ["dilution"]
    assert found[0].dedupe_key == f"risk_event:{found[0].event_id}"
    assert found[0].text.startswith("RISK · ACME · dilution (materiality 3/5)\nACME S-3")


def test_reminder_due_picks_tightest() -> None:
    days = (7, 1)
    assert cand.reminder_due(8, days) is None
    assert cand.reminder_due(7, days) == 7
    assert cand.reminder_due(2, days) == 7
    assert cand.reminder_due(1, days) == 1
    assert cand.reminder_due(0, days) == 1


def test_lockup_and_earnings_reminders_fire_once_each(
    acme: Engine, tg: tuple[FakeTelegram, TelegramService]
) -> None:
    fake, service = tg
    acc = "0000000001-26-000030"
    f = _meta(acc, "424B4", "2026-01-01")
    with write_tx(acme) as conn:
        upsert(conn, filings, [filing_row("ACME", f, utcnow_iso())], key_cols=["accession"])
        conn.execute(
            insert(lockups).values(
                accession=acc,
                symbol="ACME",
                prospectus_date="2026-01-01",
                lockup_days=75,
                expiry_date="2026-03-17",  # T-7 on 2026-03-10
                early_release_possible=1,
                excerpt="synthetic excerpt",
            )
        )
        conn.execute(
            insert(earnings_calendar).values(
                symbol="ACME",
                date="2026-03-11",  # T-1 on 2026-03-10
                status="scheduled",
                source="yfinance",
                fetched_at=utcnow_iso(),
            )
        )
    for day in range(0, 9):  # daily runs through both dates
        run(acme, service, now=NOW + timedelta(days=day))
        run(acme, service, now=NOW + timedelta(days=day, hours=1))
    keys = [r.dedupe_key for r in alert_rows(acme)]
    assert keys == [
        f"lockup:{acc}:T-7",
        "earnings:ACME:2026-03-11:T-1",
        f"lockup:{acc}:T-1",
    ]
    texts = [c["text"] for c in fake.calls["sendMessage"]]
    assert len(texts) == 3
    assert "lock-up expiry in 7 day(s): 2026-03-17" in texts[0]
    assert "underwriters may release shares early" in texts[0]
    assert "Not in the facts registry" in texts[0]


def test_insider_cluster_alerts_once_per_window(acme: Engine) -> None:
    from aether.db.models import insider_txns

    def sale(i: int, d: date) -> dict[str, object]:
        acc = f"0000000001-26-0001{i:02d}"
        f = _meta(acc, "4", d.isoformat())
        with write_tx(acme) as conn:
            upsert(conn, filings, [filing_row("ACME", f, utcnow_iso())], key_cols=["accession"])
        return {
            "accession": acc,
            "seq": 0,
            "symbol": "ACME",
            "insider": f"Insider {i}",
            "insider_cik": f"{i:010d}",
            "role": None,
            "code": "S",
            "acquired_disposed": "D",
            "shares": 100,
            "price": 1.0,
            "is_10b5_1": 0,
            "is_derivative": 0,
            "txn_date": d.isoformat(),
        }

    today = date(2026, 3, 10)
    rows = [sale(i, today - timedelta(days=i)) for i in range(1, 4)]
    with write_tx(acme) as conn:
        conn.execute(insert(insider_txns), rows)
    found = cand.insider_clusters(acme, PARAMS, NOW)
    assert [c.dedupe_key for c in found] == ["insider_cluster:ACME:2026-03-07"]
    run(acme, None)
    # A fourth sale moves the cluster start; the cooldown stops a second alert.
    late = [sale(9, today)]
    with write_tx(acme) as conn:
        conn.execute(insert(insider_txns), late)
    run(acme, None, now=NOW + timedelta(days=1))
    with acme.connect() as conn:
        n = conn.execute(
            select(func.count()).select_from(alerts).where(alerts.c.kind == "insider_cluster")
        ).scalar_one()
    assert n == 1


# --------------------------------------------------------------------------- scheduler wiring


def test_scheduler_registers_alert_jobs(migrated_db: Any, rw_engine: Engine) -> None:
    from aether.jobs import build_scheduler

    off = build_scheduler(rw_engine, make_settings(migrated_db))
    ids = {j.id for j in off.get_jobs()}
    assert "alerts" in ids and "telegram_in" not in ids  # no inbound polling when disabled
    on = build_scheduler(
        rw_engine,
        make_settings(migrated_db, telegram_bot_token=TOKEN, telegram_allowed_user_id=str(OWNER)),
    )
    assert {"alerts", "telegram_in"} <= {j.id for j in on.get_jobs()}
