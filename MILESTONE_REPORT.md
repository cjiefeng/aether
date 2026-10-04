# Milestone report

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

### Facts
- No seed facts were verified or changed (verification is M2).
- Observed (not added as facts): Yahoo serves daily prices for `QNT` from 2026-06-04 and `INFQ` from 2026-02-17, and Defiance lists both in QTUM. Their listing route, date and CIKs remain M2 EDGAR items under `qnt_ipo` / `infq_listing`.

### Open questions
- **Yahoo's `robots.txt`** (`query1.finance.yahoo.com`) is `Disallow: /`. The spec already accepts yfinance for personal use (S6). Flagging it so the decision is a conscious one.
- **The Massive failover hasn't been exercised live** until you add `MASSIVE_API_KEY`.
- **Volume differs slightly between vendors** (IONQ 2026-10-02: 19.04M on Yahoo vs 19.09M on another source). It's informational now and matters for abnormal volume in M7, so the provider is kept per row.

### Owner checklist
- [ ] Sign up for **Massive Stocks Basic** (free) at massive.com, then add `MASSIVE_API_KEY=` to `.env` (still mode 0600). It's passed to the worker only.
- [ ] After merging: `./deploy.sh`. The worker migrates to `0002_market_data`, backfills 2 years of prices and takes the first holdings snapshot within about a minute.
- [ ] Review decision 2 (no dividend adjustment) and the Yahoo robots.txt open question.

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
