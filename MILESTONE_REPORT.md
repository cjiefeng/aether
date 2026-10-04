# Milestone report

## Roadmap change: Portfolio phase added (2026-10-04, owner-approved)

Two new milestones go **straight after M3**, before the intelligence phase. Both cost $0 in LLM spend. Spec: §1.1, §1.3, §2.2 S2, §6.5, §6.6, §7, §8, §9, §11 of `AETHER_BUILD_PROMPT.md`.

- **M4: Backtest lab + model strategies.** Walk-forward backtests of rule-based QTUM-core + pure-play strategies; full risk/return metric set (CAGR, vol, downside deviation, max DD, VaR/CVaR95, Sharpe, Sortino, Calmar, alpha/beta vs QQQ and QTUM, capture, turnover); one recommended model strategy per profile (safe / medium / aggressive); Strategies page.
- **M5: Password, holdings & rebalance.** Site-wide password login; Holdings page (saved via the command queue); deterministic rebalance plan toward the selected profile, with daily caps and a no-trade band so targets stay stable unless a materiality-≥4 event or a market-wide shock overrides them.

**Owner decisions**
1. The pages present **model strategies**, with the "not financial advice" footer.
2. **No cash or T-bill sleeve.** A safer profile means more QTUM. Banner: "Backtest for reference only. Historical returns are not future gains."
3. **Backtests use dividend-adjusted (total-return) prices.** This revisits M1 decision 2 for backtests only; `prices_daily` stays split-adjusted.
4. **No IP allow-list; add a password page** instead. This amends S2 ("no login").
5. **numpy becomes a declared dependency.** No scipy.
6. **Profile limits and stability settings** are the proposed defaults in §6.5 and §6.6, kept in `config/strategies.yaml` for review.

**Renumbering.** Earlier entries below keep their original numbers.

| Old | New | Milestone |
|---|---|---|
| — | **M4** | Backtest lab + model strategies |
| — | **M5** | Password, holdings & rebalance |
| M4 | M6 | News & research ingest |
| M5 | M7 | Classifier |
| M6 | M8 | Catalysts + market structure |
| M7 | M9 | Scorecards, reactions, theme (positions drift moved to M5) |
| M8 | M10 | Conclusions, track record, brief |
| M9 | M11 | Escalation, ops & deploy |

**Old numbers in code and config.** The comments and docs that used the old numbers (`Makefile`, README, `config/rubric.yaml`, `config/sources.yaml`, `classify/`, `risk/flags.py`, `db/models.py`, `jobs.py`, `market.py`, `ticker.html`) were updated in this change. Only comments and text changed.

**Known limits, shown in the UI:**
- Only about 2 years of price history (the Massive free-tier limit). QNT and INFQ have much less.
- The universe is small and concentrated.
- The risk-free rate is 0.
- The password travels over plain HTTP on the LAN.

## M3: Alerts → MVP done (2026-10-04)

Phase 1 (risk watcher MVP, $0 LLM) is complete once you've signed off FACTS.md (checklist below).

### Acceptance criteria

| Criterion | Result | Evidence |
|---|---|---|
| Synthetic S-3 fixture → one Telegram message (mocked transport), no duplicate on re-run | ✅ | `tests/test_alerts.py::test_synthetic_s3_sends_one_message_and_no_duplicate_on_rerun`: an ACME S-3 goes through the real M2 path (`classify_filing` → `write_event`). Result: one `sendMessage` to the owner chat (plain text, no link preview), three re-runs, still one message and one `alerts` row (`sent`). |
| Failing-job alert fires | ✅ | `test_failing_job_alert_fires_once_then_recovers`: `prices` failing for 26h alerts once, `edgar` failing for 3h doesn't, and the next ok run sends one "recovered". `test_failing_job_detected_from_real_run_job` drives it through `run_job`. |
| Update from another user ID → dropped, no reply | ✅ | `test_update_from_other_user_is_dropped_without_reply`: only `deleteWebhook` + `getUpdates` are called. The log has the sender ID and chat type but not the text. |
| Group-chat update from the owner → dropped + `leaveChat` | ✅ | `test_group_update_from_owner_is_dropped_and_bot_leaves`. Also covered: being added to a supergroup (`my_chat_member`) and a channel post both trigger `leaveChat`, while a "kicked" update doesn't. |
| Missing `TELEGRAM_ALLOWED_USER_ID` → module disabled | ✅ | `test_token_without_valid_owner_id_disables_module`: missing, empty, `@owner`, negative, non-numeric and `0` all fail closed with an error log. `test_disabled_module_keeps_alerts_on_dashboard`: no Telegram call, and alerts are `dashboard_only`. `test_scheduler_registers_alert_jobs`: no inbound polling when disabled. |
| `getChat` returning a group → no messages sent | ✅ | `test_getchat_not_owner_private_chat_sends_nothing`: a group, or a private chat with another ID, means zero `sendMessage`. The pending alert is marked `failed` with the reason, and later alerts go to the dashboard. |
| Tests green, no network | ✅ | `make test`: 216 passed |
| ruff / mypy / pip-audit | ✅ | `make lint`: clean, `mypy --strict` on 60 files, no known vulnerabilities |
| `make secrets-scan` clean | ✅ | gitleaks: no leaks. Test tokens are synthetic (`test-token-not-real`; the redaction test builds a token-shaped string at runtime). |

### What was built
- **Schema** (`0004_alerts`, hand-written):
  - `alerts` is rebuilt as an outbox. New columns: `status` (`pending/sent/failed/expired/dashboard_only`), `text`, `created_at`, `attempts`, `last_error`. New CHECKs on kind, status, text length (≤4096) and attempts, plus indexes on (status, id) and created_at. It's STRICT, and the FK to `events` is kept. Nothing wrote `alerts` before, and the upgrade refuses to run if the table has rows.
  - `facts.open_question` is added and synced from `facts.yaml`.
- **`alerts/candidates.py`** (reads only): RISK events, insider clusters, lock-up and earnings T−7/T−1 reminders (the tightest reminder that applies, so a late first run doesn't send both), and job failing/recovered. Each has a stable dedupe key. Message text is built from DB fields only.
- **`alerts/dispatch.py`** (job `alerts`, every 10 min):
  - It collects candidates, then inserts new ones in one short `write_tx` (`ON CONFLICT DO NOTHING`).
  - It sends pending rows outside any transaction, about 1/s, up to 20 per run, and records each outcome in its own `write_tx`.
  - Rows expire after 48h and are `failed` after 5 attempts. A transient Telegram outage leaves rows pending.
- **`alerts/telegram.py`**: the Bot API client (checked against Bot API 10.3).
  - `TelegramConfig.from_settings` fails closed.
  - `send_message` has no chat parameter, so it can only reach the configured chat.
  - `verify()` runs `deleteWebhook` → `getChat` (private, id == owner), cached for 6h. A mismatch blocks sending for the process.
  - Long polling uses an in-memory offset. 429 `retry_after` is honoured once.
  - **Token hygiene**: the httpx/httpcore loggers are at WARNING, and errors are re-raised as `TelegramError` with the token scrubbed and `from None`. A test checks that the token reaches neither `job_runs.error` nor the logs.
- **`alerts/telegram_guard.py`**: `is_owner` (plain `message`/`edited_message` only; `from.id`, chat type and chat id must all match; bool IDs are rejected), `chats_to_leave`, and a rate-limited `DropLog` that never logs text.
- **Jobs**:
  - `alerts` every 10 min, first run 2 min after start.
  - `telegram_in` long poll (20s), only when Telegram is configured. It writes a `job_runs` row only when updates arrive, and failures are recorded at most every 10 min.
  - A `test_alert` dashboard command (CSRF, rate limit) that shares a lock with the job.
- **Dashboard**:
  - `/alerts`: delivery status with the reason, the last 100 alerts, and **Send test alert**.
  - `/facts`: status badges and counts, `extlink` source links, notes, open questions and sign-off instructions.
  - A "Recent alerts" card on the Overview and nav links. There's still no inline script or style.
- **Config**: `config/alerts.yaml` (numbers only, `extra=forbid`).

### Decisions (deviations from the spec / plan)
1. **The alert threshold is RISK materiality ≥ 3.** Single Form 4 sales (2), resale prospectuses (2) and 8-K 5.02 (2) don't alert on their own; insider clusters cover selling. Going concern (5), 424B primaries (4), 3.01 (4), S-3/S-1/3.02/NT (3) do.
2. **A 3-day event lookback.** Without it, the first deploy would send the whole 2025–26 backfill.
3. **Flags vs events.** ATM, shelf and going-concern flags don't alert separately, because their filings already alert as events. Lock-ups alert as T−7/T−1 reminders rather than at the 60-day flag.
4. **`TELEGRAM_CHAT_ID` is optional** and defaults to the user ID. When set, it must equal the user ID; a private chat's ID is the user's ID.
5. **One `alerts` job every 10 min** covers the spec's hourly job-health check.
6. **"Failing > 24h" means** the first failure since the last ok run is ≥24h old and no ok run has happened since. Jobs that silently stop running aren't covered; the dashboard's stale banners cover those.
7. **If `getChat` fails verification**, alerts already queued for Telegram are marked `failed` with the reason (visible on `/alerts`), and later ones are created `dashboard_only`.
8. **The test-alert button is on `/alerts`**, not Health as the plan said.
9. **No new dependencies.**

### Facts
- No facts changed in M3. All 8 are still `verified_by_claude` from M2, and the Facts page now shows them for your review.
- Sign-off is yours (spec §2.3). I haven't flipped any status.

### Open questions (carried over from M2)
- INFQ earn-out shares (S-4/proxy), the SkyWater closing date and consideration, and the first day QNT lock-up shares can be sold (2026-11-30 or 12-01).
- **Earnings reminders depend on the yfinance calendar** (unofficial). The message says to confirm on the company's IR site.

### Live check (isolated compose project `aether-m3` on port 8090, torn down afterwards; your stack wasn't touched)
- Migration `0004_alerts` applied on a fresh DB. The EDGAR backfill (270 events) and earnings calendar ran, then the first `alerts` run: `ok`, provider `dashboard`, warning `TELEGRAM_BOT_TOKEN not set`.
- **It created no alerts, which is correct.** The newest event is from 2026-09-22 (outside the 3-day lookback), the earnings dates (IONQ 11-04, QBTS 11-05, QNT 11-09, RGTI 11-10, INFQ 11-12) are more than 7 days away, and the QNT lock-up (11-30) is 57 days away.
- **Read-only dry run** with a 30-day lookback and a 40-day reminder window: it produced 8 candidates (RGTI/QNT/QBTS 8-K Item 3.02 dilution, plus earnings reminders), and their text read correctly.
- **Send test alert** → command `done` → a `test` row with `dashboard_only`. Its wording now says Telegram is off when it is.
- `/facts`, `/alerts` and the Overview render with **no console or CSP errors**.
- **Not exercised live: real Telegram delivery**, because there's no bot token yet. That's the first item below.

### Owner checklist
- [ ] Create the bot with **@BotFather** (`/newbot`), then `/setjoingroups` → **Disable** and `/setprivacy` → **Enable**.
- [ ] Add `TELEGRAM_BOT_TOKEN` to `.env` (mode 0600). Message the bot once, read your numeric `message.from.id` from `getUpdates` (see README), and set `TELEGRAM_ALLOWED_USER_ID` (and optionally `TELEGRAM_CHAT_ID`, the same number).
- [ ] After merging: `./deploy.sh`. Then **Send test alert** on `/alerts`; the delivery card should say "Telegram".
- [ ] Review `config/alerts.yaml` (threshold 3, 3-day lookback, T−7/T−1, 24h job failure, 48h expiry).
- [ ] **Review FACTS.md on `/facts` and sign off.** Set `status: signed_off` in `config/facts.yaml` for each fact you accept, run `make facts`, then commit via a PR. Look especially at the `qnt_ipo` discrepancy (Honeywell 47.8% vs the seed's 49.1%) and the three open questions.

### How to verify
```bash
make test            # 216 passed, network blocked
make lint            # ruff, mypy --strict, |safe ban, pip-audit
make secrets-scan    # gitleaks: no leaks
./deploy.sh          # after merge; then open http://<lan-ip>:8080/alerts and /facts
```

## M2: SEC EDGAR + deterministic risk (2026-10-04)

### Acceptance criteria

| Criterion | Result | Evidence |
|---|---|---|
| Fixture tests flag known S-3/424B/Form 4 for ≥2 tickers | ✅ | `tests/test_ingest_edgar.py::test_ingest_flags_dilution_insiders_and_qnt_lockup` replays recorded SEC responses. `dilution`: QBTS S-3ASR + 424B7, IONQ 424B5, QNT 424B4. `insider_selling`: real Form 4 sales for RGTI and IONQ. The 10b5-1-only sale gets the lower materiality, and a tax-withholding Form 4 (code F) produces no event. |
| QNT lock-up date extracted from a recorded prospectus fixture | ✅ | `tests/test_edgar_parsers.py::test_qnt_lockup_from_recorded_424b4`: the recorded 424B4 ("Prospectus dated June 3, 2026", "180 days after the date of this prospectus") gives **2026-11-30**, with early release possible. The ingest test checks the `lockups` row and that the 60-day open flag fires on 2026-10-04 but not on 2026-09-01. |
| Verify every `FACTS.md` seed against its source | ✅ | All 8 facts are now `verified_by_claude`, each with a primary source. Corrections and discrepancies are listed under "Facts" below. |
| Idempotent re-runs | ✅ | `test_ingest_is_idempotent`: a second run leaves row counts in all six tables unchanged, and parsed documents aren't fetched again. |
| Tests green, no network | ✅ | `make test`: 183 passed |
| ruff / mypy / pip-audit | ✅ | `make lint`: clean, `mypy --strict` on 53 files, no known vulnerabilities |
| `make secrets-scan` clean | ✅ | gitleaks: no leaks. The recorded fixtures don't contain the SEC User-Agent (checked with grep). |

### What was built
- **Schema** (`0003_edgar`, hand-written, every table STRICT):
  - `filings`, `insider_txns`, `fundamentals_q` (WITHOUT ROWID; PK includes `period_days` so quarter, FY and instant values can share a period end), `capital_structure`, `lockups` and `earnings_calendar` (WITHOUT ROWID).
  - The event tables `events`, `event_sources`, `event_tickers` (WITHOUT ROWID) and `event_classifications`. The last has CHECKs on class, category (all §5.1 categories), materiality 1–5, direction and confidence, a provenance CHECK (`rule_id` or model + prompt_version), and the §7 partial index `materiality WHERE class != 'NOISE'`.
  - `alerts.event_id` now has a foreign key to `events.id`, added through a batch rebuild that keeps STRICT.
- **`providers/edgar.py`**: the SEC client.
  - Refuses to start without a contact User-Agent, and sends it only to `www.sec.gov` / `data.sec.gov` over https.
  - Builds URLs from CIK + accession only and validates primary-document names (no traversal).
  - Throttles to 5 req/s and retries 429/5xx with backoff. A 404 fails immediately.
- **`edgar/`** (pure parsers):
  - `submissions.py`: `filings.recent` plus older `files[]` pages.
  - `form4.py`: stdlib XML, rejects any DTD. Reads the code, A/D, shares (whole shares, half-even rounding), price, the `aff10b5One` flag or a 10b5-1 footnote (a negated footnote doesn't count), and joint filers.
  - `text.py`: HTML → text, plus the lock-up, going-concern and ATM extractors. Each stores a ≤600-char excerpt and returns nothing rather than guess.
  - `xbrl.py`: companyfacts → quarter/FY/instant facts (no YTD values) and warrant/convertible rows.
- **`ingest/edgar.py`** (job `edgar`):
  - Fetches submissions since 2025-01-01, then documents only for Form 4, 424B4/B1, 424B5/B2 and 10-K/10-Q (at most 400 per run, newest first), then companyfacts when a new periodic report lands.
  - Writes everything in one `write_tx`.
  - **Form-based events fire on first sight**, so a failed document download never hides a dilution filing. Parsing adds the Form 4 sales, the going-concern finding and the lock-up excerpt later.
- **`classify/rules.py` + `config/rubric.yaml`**: form, 8-K item (2.02, 3.01, 3.02, 5.02), Form 4 and going-concern rules. The YAML holds numbers and identifiers only (`extra=forbid`, and a form may appear in only one rule). Titles and rationales are built from filing metadata.
- **`risk/flags.py`**: open flags for a lock-up within 60 days, an insider cluster (≥3 distinct insiders by CIK in a 30-day window, with the 10b5-1 count), an ATM ≤365 days old, a shelf ≤3 years old, and going concern in the latest 10-K/10-Q.
- **`ingest/earnings_calendar.py`** (job `earnings_calendar`, 07:15 SGT): reported dates from 8-K Item 2.02 filings and scheduled dates from the yfinance calendar. Future scheduled rows are replaced on every run.
- **Jobs**:
  - `edgar` runs every 30 min from 21:00 to 05:00 SGT on US weekdays, and at 09:00 and 17:00 SGT, with a startup catch-up.
  - A `refresh_edgar` dashboard command (CSRF, rate limit, a lock shared with the cron job, added to the command allow-list).
- **Dashboard**:
  - Overview: an "Open risk flags" card and a "RISK filings, last 30 days" card.
  - Ticker page (pure-plays): flags, lock-ups with the prospectus excerpt, earnings dates, classified SEC events, a shares-outstanding chart (`/api/dilution/<sym>`), the capital-structure table, Form 4 transactions (10b5-1 badge) and the filings list with rule badges.
  - All EDGAR links go through `extlink`. There's still no inline script or style.
- **Tooling**: `make record-cassette … UA=… GZIP=1` writes `.json.gz` cassettes (the QNT 424B4 is 4.5 MB of HTML, 410 KB gzipped). `tests/cassettes.py` loads either format.

### Decisions (deviations from the spec)
1. **The event tables are created in M2**, with EDGAR as the first event origin, instead of in M4/M5. M4 and M5 will extend these tables rather than create them.
2. **There's a new `lockups` table** (not in §7) so each extracted date carries its excerpt and accession. M6 catalysts will link to it.
3. **`fundamentals_q` stores either `value_micros` or `value_int`** (a CHECK enforces exactly one), and `period_days` is part of the PK.
4. **An 8-K with several ruled items gets one classification**: the highest materiality wins, and the other matches are named in the rationale. Item 5.02 covers both departures and appointments; the rule follows the spec (`exec_departure`) at materiality 2.
5. **Going concern is deliberately conservative.** Only an unhedged "substantial doubt … going concern" sentence counts. Risk-factor boilerplate ("could raise substantial doubt") never does.
6. **Insider share counts are rounded to whole shares** (half-even), because §7 calls for INTEGER share counts.
7. **The EDGAR schedule ignores NYSE holidays** until M7 brings an exchange calendar. On a holiday it just makes a few extra polls.
8. **Rubric materialities are initial values for your review.** Changing `rubric.yaml` doesn't reclassify filings that were already classified, except when their document is parsed again.
9. **No new dependencies.** XML and HTML parsing use the stdlib, and upcoming earnings dates come from yfinance, which was already a dependency.

### Facts (all 8 checked against primary sources; none signed off)
- `ionq_revenue_fy2025_guidance_2026` ✅ $130.0M FY2025 (8-K Ex. 99.1, 2026-02-25); FY26 guidance $280–290M, midpoint $285M (8-K Ex. 99.1, 2026-08-05).
- `ionq_acquire_skywater` ✅ Merger agreement dated 2026-01-25 (8-K Item 1.01). Closing is stated in IonQ's Q2 release.
- `qnt_ipo` **corrected**: the 424B4 dated 2026-06-03 shows Nasdaq "QNT", 28.0M Class A shares at $60.00, and Honeywell Entities at **47.8%** of voting power (47.0% with the option exercised).
  - **Discrepancy**: the seed said ≈49.1%.
  - The ~$14.3B "top of range" market cap predates pricing and isn't in the 424B4, so it was dropped.
- `qnt_lockup_expiry` **resolved**: 180 days after 2026-06-03 = **2026-11-30**. The underwriters can release shares early.
- `infq_listing` ✅ De-SPAC with Churchill Capital Corp X, consummated 2026-02-13 (closing 8-K filed 2026-02-17). Trades on the NYSE, with warrants `INFQ WS` at $11.50. Registration-rights holders have a 180-day transfer restriction with a $12.00 VWAP early release.
- `darpa_qbi_stage_b` ✅ All 11 names match darpa.mil ("as of Nov. 6, 2025").
- `ibm_roadmap_ftqc` ✅ Matches the IBM blog post (2025-06-10).
- `pqc_deadlines` **refined**: NIST IR 8547 is still an initial public draft. Its "deprecated after 2030" applies only to 112-bit strength, and everything is "disallowed after 2035". The EU roadmap v1.1 sets high-risk use cases at end-2030 and medium-risk at end-2035 (checked in the roadmap PDF).
- **New `open_question` field** in `facts.yaml`. FACTS.md lists the open questions.

### Open questions
- **INFQ earn-out shares**: the closing 8-K doesn't mention any. Read the S-4 / proxy.
- **SkyWater closing date and consideration**: check IonQ's closing 8-K (Item 2.01).
- **The exact first day QNT lock-up shares can be sold** (2026-11-30 or 12-01), and any announced early release.
- **QNT and INFQ don't tag `dei:EntityCommonStockSharesOutstanding` in companyfacts** (seen for QNT), so their shares-outstanding chart uses balance-sheet and weighted-average concepts where tagged. Fully diluted counts are M7.
- **Company IR domains (T1) are not configured yet.** They arrive with the IR RSS feeds in M4.
- **Capital-structure extraction from filing text is limited to ATM amounts.** Convertibles, warrants and earn-outs beyond XBRL tags need the M5+ LLM extraction (T1, cross-checked against XBRL), as the spec plans.

### Live check (isolated compose project `aether-m2` on port 8090; your stack wasn't touched)
- **First EDGAR backfill**: 984 filings since 2025-01-01 across the five pure-plays (IONQ 284, QBTS 307, INFQ 172, RGTI 170, QNT 51). This **matches an independent count** from the raw submissions JSON. 354 documents were parsed with 364 SEC requests in about 90 s, with no parse errors.
- **Results**:
  - 627 Form 4 transactions, 452 XBRL facts, 254 rule events: 151 insider_selling, 45 dilution, 29 earnings_release, 26 exec_departure, 3 delisting_or_compliance.
  - The QNT lock-up is 2026-11-30, and its 60-day flag is open.
  - ATM programs found, each with a stated size: IONQ $500M (2025-02-27), QBTS $150M (2025-01-10) and $400M (2025-06-10), RGTI $350M (2025-05-30).
- **Two fixes came out of the live run** (both with tests):
  - The going-concern extractor had flagged QBTS's FY2024 10-K. That 10-K only *refers back to* earlier disclosures ("we disclosed that there was substantial doubt …"). Retrospective and "alleviated" statements no longer count; the re-run has no going-concern flags.
  - ATM detection now requires a stated aggregate amount near the at-the-market wording. Base-prospectus boilerplate had produced two ATM rows with no amount.
- **Rules added after reviewing the pages**:
  - 8-K Item 3.02 (unregistered equity sale) → `dilution` (materiality 3). QNT's 2026-09-08 8-K (Items 1.01/3.02) was unflagged before this.
  - Titles no longer repeat the form ("QNT 424B4", not "QNT 424B4 (424B4)").
- **Pages**: Overview, `/t/QNT` and `/t/RGTI` render (lock-up card, flags, Form 4 table, shares-outstanding chart, filings) with **no console or CSP errors**.
- **Known first-deploy gap**: the `earnings_calendar` startup run can finish before the first EDGAR backfill, so past (8-K 2.02) dates appear after the next 07:15 run.

### Owner checklist
- [ ] `SEC_USER_AGENT` is now set in `.env`. **`chmod 600 .env`**: it's currently 0644, and `./deploy.sh` refuses to run until it's fixed.
- [ ] Review `config/rubric.yaml`: the materiality per form/item, and the thresholds (60-day lock-up window, 3 insiders / 30 days, ATM 365 days, shelf 3 years).
- [ ] Review FACTS.md, especially the `qnt_ipo` discrepancy (Honeywell 47.8% vs the seed's 49.1%). Sign-off happens at the end of M3.
- [ ] After merging, run `./deploy.sh`. The worker migrates to `0003_edgar` and backfills EDGAR (a few minutes on the first run).

### How to verify
```bash
make test            # 183 passed, network blocked
make lint            # ruff, mypy --strict, |safe ban, pip-audit
make secrets-scan    # gitleaks: no leaks
./deploy.sh          # after merge; then open http://<lan-ip>:8080/t/QNT
```

## M1: Market data (2026-10-04)

### Acceptance criteria

| Criterion | Result | Evidence |
|---|---|---|
| Idempotent re-runs | ✅ | `tests/test_ingest_prices.py::test_backfill_then_rerun_is_idempotent` (backfill, then incremental, then full refresh: same row count), `test_revised_bars_update_in_place`, `tests/test_qtum_holdings.py::test_ingest_is_idempotent_and_replaces_snapshot`. Live: a full 2-year refresh of 5,754 rows left the count at 5,754. |
| Simulated yfinance failure → fallback serves, `provider` recorded | ✅ (fallback is **Massive**, not Stooq; see decision 1) | `test_simulated_yfinance_failure_fallback_serves_and_provider_recorded`: every row has `provider='massive'`, and `job_runs.provider='massive'`. `tests/test_providers.py` covers the failover rules (trip after 3, probe after 24 h, a failed probe restarts the timer, an empty result is not a failure, both-fail errors). **Not run live**: there's no `MASSIVE_API_KEY` yet. |
| Charts render | ✅ | Checked live in a browser against an isolated stack: the Overview comparison chart (all five ranges, tooltips), the ticker page (QNT close + volume), dark and light themes, and **no console / CSP errors**. Tests check that no page has an inline `<script>` or `style=`. |
| Tests green, no network | ✅ | `make test`: 130 passed |
| ruff / mypy / pip-audit | ✅ | `make lint`: clean; `mypy --strict` on 39 files; no known vulnerabilities (yfinance's dependency tree included) |
| `make secrets-scan` clean | ✅ | gitleaks: no leaks (vendored `echarts.min.js` included) |

### What was built
- **Schema** (`0002_market_data`):
  - `prices_daily`: PK(symbol, d), FK to `tickers`, CHECKs on provider, OHLC > 0 and h ≥ l, volume ≥ 0, and the date format.
  - `qtum_holdings`: PK(snapshot_date, holding_symbol).
  - Both are STRICT and WITHOUT ROWID; the tests now check WITHOUT ROWID too.
- **Providers** (`providers/prices.py`):
  - `PriceProvider` protocol. `YFinanceProvider` uses `auto_adjust=False` and rounds to 6 dp to drop float32 noise. `MassiveProvider` uses Bearer auth, a 12.5 s throttle (the free tier allows 5 calls/min), follows `next_url` only on `api.massive.com`, and maps errors to `ProviderError`.
  - `FailoverPriceProvider` handles switching (see README).
- **Ingest:**
  - `ingest/prices.py`: 2-year backfill, a 10-day revision overlap, a Sunday full refresh, bar validation, fetch-then-one-`write_tx`, and partial failures recorded in `job_runs.error` while the run stays `ok`.
  - `ingest/qtum_holdings.py`: a fail-closed robots.txt gate, a stdlib HTML parse, and sanity checks (header, as-of date, weights summing to 95–105%, no duplicates). Each snapshot date is replaced wholesale.
- **Jobs:**
  - `prices` runs at 06:30 SGT and `qtum_holdings` at 06:35, each with a startup catch-up if its last ok run is more than 24 h old.
  - `refresh_prices` is a dashboard command (CSRF + rate limit) that shares a lock with the cron job.
  - `run_job` moved to `runs.py`, and `JobResult.warning` was added.
- **Dashboard:**
  - Overview: banners, a log-scale comparison chart, a ticker table, the watchlist's weight in QTUM, and the refresh button.
  - `/t/<symbol>`, plus the `/api/prices/overview?range=` and `/api/prices/<symbol>` JSON endpoints.
  - ECharts 6.1.0 is vendored (sha256 in `VENDORED.txt`). `static/charts.js` reads `data-*` attributes and uses canvas `richText` tooltips, so no HTML is injected.
- **Live check** (isolated compose project on port 8090; your M0 stack was not touched):
  - yfinance backfilled all 13 tickers (5,754 rows); QNT starts 2026-06-04 and INFQ 2026-02-17.
  - The holdings snapshot loaded 90 rows (as of 2026-10-05, summing to 99.95%), with all 5 pure-plays present, combined at 4.64% of QTUM.
  - The IONQ 2026-10-02 OHLC matched an independent source.

### Decisions (deviations from the spec)
1. **The fallback is Massive's free tier, not Stooq** (your choice during planning). On 2026-10-04, Stooq's CSV endpoint returned a JavaScript proof-of-work bot challenge, and `stooq.com/robots.txt` disallows `*`. Using it would mean getting past bot detection. `api.nasdaq.com/robots.txt` also disallows everything. Massive "Stocks Basic" is $0, 5 calls/min, 2 years of history, end-of-day, individual use.
2. **Split-adjusted, not dividend-adjusted, prices from both providers**, so a failover can't mix conventions. Total-return effects (QTUM/QQQ/SOXX dividends) are left out for now. Revisit in M7 if the event-reaction maths needs them.
3. **The backfill is exactly 730 days**, matching the Massive free tier's 2-year limit.
4. **Failover state is in memory.** A worker restart acts as a probe of yfinance. An empty yfinance result tries Massive without counting as a failure.
5. **The holdings page is parsed with the stdlib `html.parser`**, so no new dependency. Defiance's `robots.txt` is malformed; read literally (as Python's `robotparser` does) it allows everything. Its evident intent is to block `/wp-content/uploads/funddocs/`, which Aether never fetches.
6. **Staleness uses a 4-calendar-day tolerance per symbol and 30 h for the prices job.** It stands in for an NYSE holiday calendar until M7 adds `exchange_calendars`.
7. **The Overview chart uses a log scale**, because the pure-play basket rose about 17× over two years, which flattens the other lines on a linear axis.
8. **`/` is now the Overview.** Health lives at `/health` only.
9. **yfinance stays the primary source (owner decision, 2026-10-04).** Yahoo's `robots.txt` (`query1.finance.yahoo.com`) is `Disallow: /`. The owner confirmed this is personal-use API access by a single self-hosted instance, not crawling (spec S6).

### Facts
- No seed facts were verified or changed (verification is M2).
- Observed (not added as facts): Yahoo serves daily prices for `QNT` from 2026-06-04 and `INFQ` from 2026-02-17, and Defiance lists both in QTUM. Their listing route, date and CIKs remain M2 EDGAR items under `qnt_ipo` / `infq_listing`.

### Open questions
- **The Massive failover hasn't been exercised live** until you add `MASSIVE_API_KEY`.
- **Volume differs slightly between vendors** (IONQ 2026-10-02: 19.04M on Yahoo vs 19.09M on another source). It's informational now and matters for abnormal volume in M7, so the provider is kept per row.

### Owner checklist
- [ ] Sign up for **Massive Stocks Basic** (free) at massive.com, then add `MASSIVE_API_KEY=` to `.env` (still mode 0600). It's passed to the worker only.
- [ ] After merging: `./deploy.sh`. The worker migrates to `0002_market_data`, backfills 2 years of prices and takes the first holdings snapshot within about a minute.
- [ ] Review decision 2 (no dividend adjustment).

### How to verify
```bash
make test            # 130 passed, network blocked
make lint            # ruff, mypy --strict, |safe ban, pip-audit
make secrets-scan    # gitleaks: no leaks
./deploy.sh          # after merge; then open http://<lan-ip>:8080/
```

## M0: Scaffold + security baseline (2026-10-04)

### Acceptance criteria

| Criterion | Result | Evidence |
|---|---|---|
| Health page reports SQLite version + WAL | ✅ | `tests/test_web_security.py::test_health_page_reports_sqlite_version_and_wal`, `test_healthz_json`. Live: `make smoke` → `{"ok":true,"sqlite_version":"3.46.1","journal_mode":"wal","schema_revision":"0001_baseline","file_mode":"0o600"}` |
| ~~Request from an IP outside `AETHER_ALLOWED_CIDRS` → 403~~ | Removed | The owner dropped the CIDR allow-list after M0 review (see "M0 amendments"). |
| Command POST without a CSRF token → 403 | ✅ | `test_command_without_csrf_token_is_403`, plus missing-cookie, forged-token and cross-site `Origin`/`Sec-Fetch-Site` cases. Live: `curl -X POST …/commands/ping` → `403 CSRF check failed`. With a token → `202`, and the worker marked it `done` |
| `\|safe` lint check works | ✅ | `test_lint_flags_planted_violation` (5 planted variants incl. `Markup(` and `markupsafe.Markup`). `make lint` runs `scripts/check_no_safe.py` |
| Writer + 2 readers concurrency test, no `SQLITE_BUSY` | ✅ | `tests/test_concurrency.py`: 3 s of batched `BEGIN IMMEDIATE` inserts with 2 `mode=ro` readers. No errors; readers see monotonic counts and never a partial batch |
| Tests green, no network | ✅ | `make test` → 79 passed, 0 warnings (pytest-socket `--disable-socket`; `test_network_is_blocked`) |
| ruff / mypy clean | ✅ | `make lint`: ruff, ruff format, `mypy --strict` (31 files), `check_no_safe`, `pip-audit --require-hashes` → no known vulnerabilities |
| `make secrets-scan` clean | ✅ | gitleaks v8.30.1 over git history and working tree: no leaks |

### What was built
- **Toolchain, all in Docker.** Python 3.12.15 and uv 0.12.23 (pinned images), with a committed `uv.lock`. The `dev` compose service (profile `tools`) runs every make target, and the venv lives in a named volume. Nothing is installed on the Mac.
- **docker-compose.**
  - `worker` (single writer: migrate → sync config → APScheduler) and `app` (dashboard).
  - Both run non-root (uid 10001) with a read-only root FS, `cap_drop: ALL` and `no-new-privileges`.
  - They share the named volume `aether-data` at `/data`.
  - Ports are `8080:8082` (host 8080 → app on 8082), per owner direction.
  - Healthchecks: the worker checks the schema is at head and a recent heartbeat exists; the app checks `/healthz`. The app waits for a healthy worker.
  - Started and updated with `./deploy.sh` (see amendments).
- **SQLite (`db/`).**
  - Pragmas are set on every connection (WAL, `busy_timeout=5000`, FKs on, `synchronous=NORMAL`, `temp_store=MEMORY`, `cache_size=-20000`).
  - Every rw transaction is `BEGIN IMMEDIATE`, and `write_tx()` retries busy lock acquisition.
  - The ro engine uses a `mode=ro` URI plus `query_only`.
  - The DB file is 0600 in a 0700 directory.
  - `dialect.py` provides `upsert` (sqlite/postgres/mysql) and `json_extract`. `types.py` provides `Micros` (INTEGER ↔ Decimal, floats rejected), UTC ISO timestamps and simhash signed/unsigned helpers.
- **Alembic baseline `0001_baseline`.** STRICT tables `tickers`, `facts`, `commands`, `job_runs`, `llm_calls` and `alerts`, with CHECK enums and `json_valid` CHECKs. Tests verify that every table is STRICT and that the models match the migrations.
- **Config.** pydantic-settings reads env only (no env_file). Typed YAML loaders use `extra="forbid"` for `watchlist.yaml` (identifiers only) and `sources.yaml` (trust tiers; T3 is the default).
- **Facts registry.** `config/facts.yaml` holds the 8 seed facts from §2.3, all `unverified`. `FACTS.md` is generated from it, and a test checks the two stay in sync. `render_for_prompt` labels anything not `signed_off` as UNCONFIRMED. The worker upserts facts into the `facts` table at startup.
- **S2.**
  - No IP allow-list (removed after review, see amendments). uvicorn runs with `proxy_headers=False`.
  - CSRF: a signed double-submit token (HttpOnly, SameSite=Strict cookie plus the `X-CSRF-Token` header, which HTMX sends via `hx-headers`) and an Origin/Sec-Fetch-Site check.
  - Commands are rate-limited to 10/hour, counted from the `commands` table, so the limit survives restarts.
  - CSP has no inline script or style. Also set: `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `nosniff`, COOP and Permissions-Policy. These apply to error responses too.
- **S4.** `.env`, `config/positions.yaml` and `data/` are in both `.gitignore` and `.dockerignore`. Other S4 pieces:
  - `.env.example` lists the keys only.
  - `deploy.sh` refuses a `.env` that isn't 0600.
  - Secrets are interpolated only into the `worker` service.
  - The gitleaks pre-commit hook (`.githooks/pre-commit`, Docker-based, fails closed) is enabled via `core.hooksPath`.
  - `make secrets-scan` is in place.
- **S5.** `security/sanitize.py` (markdown-it with raw HTML disabled → nh3 strict allow-list, http/https-only links with `rel="noopener noreferrer nofollow"`) is the only module allowed to construct `Markup`. The `md` and `extlink` Jinja filters are registered, and `scripts/check_no_safe.py` enforces the ban.
- **S1 groundwork.** `security/untrusted.py::wrap_untrusted` adds `<untrusted_document id=…>` delimiters, neutralises delimiter-injection, and defines the system-prompt notice.
- **Test tooling.** pytest-socket, a temp-file DB with real migrations and pragmas, and respx cassette replay (`tests/cassettes.py`) with a synthetic `ACME` fixture. `scripts/record_cassette.py` records manually, keeps no request headers and allow-lists response headers.
- **Ops.** Online backup via `sqlite3.backup()` to `/data/backups/aether-YYYYMMDD.db` (0600, 14 days kept). A nightly 04:00 SGT job does backup → prune `job_runs` > 90 days → `PRAGMA optimize` → `wal_checkpoint(TRUNCATE)`. `make backup` was verified live.
- **Dashboard.** A health page (SQLite version, journal mode, schema revision, file mode, last run per job, ping button) with dark/light themes and the "not financial advice" footer.

### Decisions (deviations from the spec, approved in the M0 plan)
1. **The app's one write path.** "App only reads" conflicts with the dashboard filling the `commands` table. Resolution:
   - `enqueue_command` runs on a separate `mode=rw` (no-create) engine with an **SQLite authorizer that denies everything except `INSERT INTO commands`**. UPDATE, DELETE, DDL, PRAGMA, ATTACH and writes to other tables are all tested as denied.
   - A test asserts `aether/web` never uses the writer engine.
2. **Port binding is `8080:8082` on all host interfaces**, per owner direction (rather than `${AETHER_LAN_IP}:8000`).
3. **No CIDR allow-list** (owner decision after review; this deviates from spec S2). Verified live: Docker Desktop delivered every request as `192.168.65.1`, the VM gateway, so the allow-list couldn't tell clients apart on this host anyway. LAN-only access now depends on the network boundary and host firewall. Documented in the README.
4. **Schema is built per milestone.** M0 ships the infra tables only. Each milestone adds its own tables in a new hand-written migration, with §7 as the target.
5. **Hand-written migrations and STRICT-safe types only.** SQLAlchemy's String/Boolean/Float/DateTime aren't valid in STRICT tables.
6. **The `facts` table stores `source_urls` as a JSON array**, not the outline's single `source_url`, because several seed facts cite two sources.
7. **APScheduler 3.11** (stable), not 4.x.
8. **CSRF is header-only** (`X-CSRF-Token`). Plain non-HTMX form POSTs aren't supported, which is stricter.
9. **`pip-audit` runs via `uvx` against `uv export` with hashes** (`uv pip audit` doesn't exist). It needs network, so it belongs to `make lint` and is never a test.
10. **Backups use Python's `sqlite3.backup()`** (same online API as the CLI's `.backup`), because the slim image has no `sqlite3` binary.
11. **htmx 2.0.4 is vendored** (sha256 recorded in `web/static/VENDORED.txt`), with `includeIndicatorStyles`, `allowEval` and `allowScriptTags` turned off for the CSP.
12. **Job-run hygiene.** `process_commands` polls every 30 s but records a `job_runs` row only when there's work. The heartbeat runs every 10 min, and job runs are pruned after 90 days.
13. **`httpx2` is a dev dependency** (owner decision). Starlette 1.7's TestClient now uses it natively, so the deprecation-warning filter is gone. `httpx` stays the runtime HTTP client, and respx cassettes mock `httpx`.
14. **`./deploy.sh` replaces `make up`** (owner decision). It fast-forwards to `origin/main` and runs `docker compose up -d --build --wait`.

### Facts
- No facts were verified in M0; per spec, verification is M2. All 8 seeds are `unverified`.
- Source URLs are copied from the brief with only an `https://` scheme added. No hosts or paths were changed.
- `qnt_lockup_expiry` has no source. It's listed under Open questions, to be derived from the final 424B4 in M2.

### Open questions
- **QNT lock-up expiry.** Unknown until the 424B4 is parsed (M2).
- **QNT / INFQ.** Ticker existence, listing route and CIKs all need EDGAR verification in M2. Until then the watchlist CIKs are blank.
- **T1 IR domains and T2 RSS feed URLs** aren't configured yet (M2/M4). `sources.yaml` has `sec.gov` (T1) and the three T2 press domains named in the brief.
- **Model IDs and the web-search tool version** will be looked up from the Anthropic docs when the LLM wrapper is built (M4). `CLASSIFIER_MODEL` and `SYNTH_MODEL` are declared but unused.

### Owner checklist
- [ ] **Anthropic Console:** create a dedicated **workspace** for Aether, create an API key in it, and set a **monthly spend limit**. This is the real cost cap (S3).
- [ ] Create **separate keys** for dev and for the long-running app. Put the app key in `.env` as `ANTHROPIC_API_KEY` (not needed until M4).
- [ ] Set **`SEC_USER_AGENT`** in `.env` as `"Your Name your-email@example.com"`. SEC requires it, and it's sent only to sec.gov (needed from M2).
- [ ] `cp .env.example .env && chmod 600 .env`. `./deploy.sh` refuses anything else.
- [ ] **Never port-forward 8080** on your router, and never put it behind a public tunnel. Check that the macOS firewall is on.
- [ ] With no IP allow-list, anyone who can reach port 8080 can view the dashboard: make sure you trust everything on your LAN.
- [ ] Deploy and update with `./deploy.sh` (needs a clean checkout of `main` and SSH access to the GitHub remote).
- [ ] Copy `/data/backups` off-host occasionally if you want real disaster recovery: `docker compose cp worker:/data/backups ./backups-copy`.
- [ ] Review the decisions above, especially #1 (the command-engine write exception) and #4 (per-milestone schema).

### How to verify
```bash
make test            # 79 passed, network blocked
make lint            # ruff, mypy --strict, |safe ban, pip-audit
make secrets-scan    # gitleaks: no leaks
./deploy.sh          # pull main, build, start, wait healthy, check /healthz
```

### M0 amendments (after owner review, 2026-10-04)
- **Removed the CIDR allow-list.** That means `AETHER_ALLOWED_CIDRS`, `AETHER_TRUSTED_PROXY`, `security/network.py`, the X-Forwarded-For handling and their tests are all gone. The dashboard has no IP-based access control now. It relies on the LAN boundary and host firewall, plus CSRF and the rate limit for commands. This is a deliberate deviation from spec S2 and from the original M0 acceptance criterion.
- **Added `httpx2`** as a dev dependency and removed the pytest warning filter. The test run is now warning-free.
- **Added `./deploy.sh`** in place of `make up`/`make build`/`make check-env`. It checks `.env` is 0600, refuses to run with uncommitted tracked changes, fast-forwards to `origin/main`, then runs `docker compose up -d --build --remove-orphans --wait` and checks `/healthz`.
- **Added GitHub Actions CI** (`.github/workflows/ci.yml`), which runs on pull requests and on pushes to `main`. It has four jobs: test, lint, secrets-scan and a runtime image build. Actions are pinned to commit SHAs, and permissions are `contents: read`. From now on, changes land through PRs only (see CLAUDE.md "Git workflow").
