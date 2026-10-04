# Aether: Quantum Equity Watcher

A self-hosted watcher for a small set of quantum-computing equities. It runs on your own machine and serves a LAN-only dashboard.
**Personal research tool, not financial advice.** The full spec is in [AETHER_BUILD_PROMPT.md](AETHER_BUILD_PROMPT.md), and progress is tracked in [MILESTONE_REPORT.md](MILESTONE_REPORT.md).

Status: **M2** (SEC EDGAR + deterministic risk). M1's prices and QTUM holdings, plus SEC filings, Form 4 insider trades, XBRL fundamentals, capital structure, lock-ups, earnings dates, deterministic RISK rules and open risk flags on the dashboard. There's no LLM usage yet.

## ⚠️ LAN only: never expose it to the internet

The dashboard has **no login**. It's meant only for the owner's local network.

- **Do not** port-forward `8080` on your router, and don't put it behind a public tunnel (ngrok, Cloudflare Tunnel, Tailscale Funnel, etc.).
- There's no IP allow-list in the app. Whether the dashboard is reachable depends entirely on your network boundary and host firewall: anyone who can reach port 8080 can view it.
- Any action that triggers work (the `commands` queue) needs a CSRF token and is rate-limited to 10 per hour.

## Requirements

Only **Docker** (with Compose v2), **make** and **git**. Python, uv, the linters and gitleaks all run inside containers, and nothing is installed on the host.

## Quick start

```bash
cp .env.example .env
chmod 600 .env          # deploy.sh refuses to start if .env is readable by others
# edit .env: SEC_USER_AGENT="Your Name you@example.com" (required for EDGAR) and
#            MASSIVE_API_KEY (price fallback, free tier)
make hooks              # enable the gitleaks pre-commit hook (runs in Docker)
./deploy.sh             # pull latest main, build, start, wait until healthy
```

Open `http://<this-machine's-LAN-IP>:8080/` from a device on your LAN.

## Market data (M1)

| Data | Source | When |
|---|---|---|
| Daily OHLCV, 13 tickers | **yfinance** (primary). It fails over to **Massive** (formerly Polygon), free "Stocks Basic" tier, after 3 consecutive yfinance failures, and probes yfinance again after 24 h. A symbol that fails on yfinance is retried on Massive straight away. | Daily 06:30 SGT. Sundays re-fetch the full 2 years. A startup run happens if the last ok run is over 24 h old. Also on demand via **Refresh prices** on the Overview. |
| QTUM holdings | Defiance's public full-holdings page (`robots.txt` is checked before every fetch) | Daily 06:35 SGT, plus a startup run if stale |

- **Prices are split-adjusted, not dividend-adjusted.** Both providers use the same convention, so a failover can't mix the two. Every row records the provider that served it (`prices_daily.provider`), and each job run records which providers it used (`job_runs.provider`).
- **No Massive key means no fallback.** The worker logs a warning, and if yfinance fails the dashboard shows "stale since …" banners.
- **Stooq is not used.** It's the spec's original fallback, but it now serves a bot challenge and disallows crawlers in `robots.txt`.
- yfinance / Yahoo data is for **personal use only** (S6). Massive's free tier is for individual use.

## SEC EDGAR + deterministic risk (M2)

| Data | Source | When |
|---|---|---|
| Filings since 2025-01-01 for the five pure-plays (QTUM is an ETF, so it has none) | SEC `submissions` API (CIKs in `config/watchlist.yaml`) | Every 30 min 21:00–05:00 SGT on US weekdays, 09:00 and 17:00 SGT every day, a startup run if stale, and **Refresh SEC filings** on the Overview |
| Form 4 insider transactions (code, shares, price, 10b5-1 flag) | Raw ownership XML of each Form 4 | With the filings |
| Lock-ups (final prospectus 424B4/424B1), ATM programs (424B5/424B2), going-concern language (10-K/10-Q) | Primary-document text, deterministic regex extractors; each stores a ≤600-char excerpt | When a new filing of that form lands |
| Fundamentals and XBRL-tagged warrants/convertibles | SEC XBRL `companyfacts` | When a new 10-K/10-Q lands (or no fundamentals yet) |
| Earnings dates | Past: 8-K Item 2.02 filing dates. Upcoming: yfinance calendar | Daily 07:15 SGT |

- **SEC fair access.** Every request carries `SEC_USER_AGENT`, is throttled to 5 req/s (SEC allows 10) and retried with backoff on 429/5xx. The header is only sent to `www.sec.gov` / `data.sec.gov`, and URLs are built from CIK + accession, never taken from payloads. Without `SEC_USER_AGENT` the `edgar` job fails and the dashboard shows "stale since …".
- **Deterministic rules** (`classify/rules.py`, parameters in `config/rubric.yaml`). Each rule creates an `events` row (origin `edgar`, tier T1) with an `event_classifications` row that records the `rule_id`:
  - `S-3`/`S-3ASR`/`S-1`/`424B*` → `dilution`
  - Form 4 open-market sales → `insider_selling` (lower materiality when every sale is under a 10b5-1 plan)
  - 8-K Item 2.02 → `earnings_release`, 3.02 → `dilution`, 5.02 → `exec_departure`, 3.01 → `delisting_or_compliance`
  - `NT 10-K/Q` → `delisting_or_compliance`
  - unhedged going-concern statements → `going_concern`
- **Open risk flags** (`risk/flags.py`): a lock-up ending within 60 days, an insider-selling cluster (3 or more insiders in 30 days), an ATM program filed in the last year, a shelf filed in the last three years, and going-concern language in the latest periodic report. Insider clusters alert (M3); the other flags alert through their underlying filing events.
- Ingest is idempotent: filings key on accession and events on the filing URL. Each document is fetched once (`filings.parsed`), and form-based events never wait on a document download. Network I/O happens first, then one short write transaction.

## Alerts + Telegram (M3)

The `alerts` job runs every 10 minutes (it is also the hourly job-health check). Every alert has a dedupe key and goes into the `alerts` table (an outbox), so it fires **once**, however often the job runs. Thresholds are in `config/alerts.yaml`.

| Alert | Fires when | Dedupe key |
|---|---|---|
| RISK event | A deterministic RISK event (T1, not quarantined) at materiality ≥ 3, published in the last 3 days. The lookback stops the first backfill from flooding the chat. | `risk_event:<event id>` |
| Insider cluster | ≥3 insiders sold in 30 days (the `rubric.yaml` flag); at most one alert per symbol per 30 days | `insider_cluster:<sym>:<start>` |
| Lock-up reminder | T−7 and T−1 before a prospectus lock-up ends. The message shows the facts-registry status of that date. | `lockup:<accession>:T-<n>` |
| Earnings reminder | T−7 and T−1 before a scheduled earnings date | `earnings:<sym>:<date>:T-<n>` |
| Job failing | A job has failed for more than 24h with no ok run since. A "recovered" message follows its next ok run. | `job_failing:<job>:<first failure>` |

Delivery:
- Telegram is used when it's configured. Otherwise every alert is `dashboard_only` and appears on `/alerts` and the Overview.
- Messages are plain text (no `parse_mode`, no link previews) and are sent outside any database transaction.
- Undelivered messages expire after 48h instead of arriving late, and are marked `failed` after 5 attempts.
- **Send test alert** on `/alerts` checks the path end-to-end.

**Telegram is owner-only (S7):**
- **Fail closed.** If `TELEGRAM_BOT_TOKEN` is set but `TELEGRAM_ALLOWED_USER_ID` is missing or not a numeric ID, the module stays off and logs an error. `TELEGRAM_CHAT_ID` is optional; if set, it must equal the user ID.
- **Outbound.** Before the first send (and every 6h), the worker calls `deleteWebhook`, then `getChat`, and sends only if the chat is `private` with `id ==` your user ID. Anything else disables sending for the process.
- **Inbound.** Long polling (`getUpdates`) only, never a webhook, so no port is opened. `alerts/telegram_guard.py::is_owner` runs before anything else: only a private message from your user ID in your own chat passes. Everything else is dropped silently, logged with sender ID and chat type only (never the text). If the bot is in any group or channel, it calls `leaveChat`. The MVP has no bot commands.
- **The token** is never logged. The httpx request log is off, and Telegram errors are scrubbed before they reach logs or `job_runs`.

**Setting up the bot:**
1. In Telegram, message **@BotFather**: `/newbot`, then `/setjoingroups` → **Disable** and `/setprivacy` → **Enable**.
2. Put the token in `.env` as `TELEGRAM_BOT_TOKEN=` (keep `.env` mode 0600). If it ever leaks, rotate it with `/revoke`.
3. Find your **numeric** user ID. Send your bot any message, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` in your own browser and read `message.from.id`. Never use the @username.
4. Set `TELEGRAM_ALLOWED_USER_ID=<that number>` (and optionally `TELEGRAM_CHAT_ID=<same number>`), then run `./deploy.sh`.
5. Open `/alerts` and press **Send test alert**.

## Dashboard

- `/`: the Overview.
  - Stale banners.
  - A chart comparing QTUM, an equal-weighted pure-play basket, SOXX and QQQ, rebased to 100 on a log scale, with ranges from 1M to 2Y.
  - A ticker table (last close, 1d/30d change, distance from the 52-week high, as-of date, provider).
  - The watchlist's weight in QTUM.
  - Open risk flags, recent alerts and RISK filings from the last 30 days.
- `/t/<SYMBOL>`: price and volume chart plus a summary. For pure-plays it also shows:
  - open risk flags, lock-ups (with the prospectus excerpt) and earnings dates
  - classified SEC events, a shares-outstanding chart (XBRL) and the capital-structure table
  - Form 4 insider transactions (10b5-1 badge) and the filings list with each rule hit.
  All SEC links go to EDGAR.
- `/alerts`: delivery status (Telegram or dashboard only, and why), the last 100 alerts, and **Send test alert**.
- `/facts`: the facts registry with status badges, source links, notes, open questions and how to sign off.
- `/health`: DB, schema and last run per job.

Charts use vendored ECharts (`web/static/VENDORED.txt`) and load their data from `/api/prices/*` and `/api/dilution/*` as JSON. There are no inline scripts, so the CSP stays strict.

## Deploying

`./deploy.sh` is the only way to start or update the stack. In order, it:

1. checks that `.env` (if present) is mode 0600,
2. refuses to run if tracked files have uncommitted changes,
3. runs `git fetch origin main`, checks out `main` and fast-forwards (it fails if local `main` has diverged),
4. runs `docker compose up -d --build --remove-orphans --wait`, which waits for both services to report healthy,
5. checks `http://localhost:8080/healthz`.

Run it again whenever new commits land on `main`.

## CI

Every pull request runs `.github/workflows/ci.yml`. It has four jobs: `make test`, `make lint`, `make secrets-scan` (full git history) and a runtime image build. These are the same Docker-wrapped targets used locally. Changes land on `main` only through PRs that pass CI.

## Make targets

| Target | What it does |
|---|---|
| `./deploy.sh` | Pull latest `main`, rebuild and (re)start the stack (`worker` = single writer + scheduler, `app` = read-only dashboard) |
| `make down` / `logs` / `ps` | Stop / tail / inspect the stack |
| `make test` | pytest in the dev container. Network is blocked (`pytest-socket`); tests use a temp-file SQLite DB with prod pragmas and migrations |
| `make lint` | ruff, ruff format check, `mypy --strict`, the `\|safe`/`Markup` ban, and `pip-audit` on the hashed lockfile |
| `make fmt` | ruff format + autofix |
| `make migrate` | `alembic upgrade head` (the worker also does this at startup) |
| `make backup` | Online SQLite backup to `/data/backups/aether-YYYYMMDD.db` (14 days kept, mode 0600) |
| `make secrets-scan` | gitleaks over git history and the working tree |
| `make facts` | Regenerate `FACTS.md` from `config/facts.yaml` |
| `make lock` | Re-resolve `uv.lock` |
| `make eval` | Classifier evals (from M5) |
| `make smoke` | Live check against the running stack (never part of acceptance) |
| `make record-cassette NAME=… URL=… [UA=…] [GZIP=1]` | Record one live HTTP response as a test fixture (manual; SEC needs `UA`, large documents use `GZIP=1`) |

## Layout

```
src/aether/
  config.py       env settings + typed YAML loaders (identifiers only; unknown keys rejected)
  providers/      typed provider interfaces: yfinance, Massive, failover; SEC EDGAR client
  ingest/         prices, QTUM holdings, EDGAR (filings/Form 4/XBRL), earnings calendar
  edgar/          pure parsers: submissions, Form 4 XML, filing text extractors, XBRL
  classify/       rules.py: deterministic filing rules (the LLM classifier arrives in M5)
  risk/           flags.py: open risk flags (lock-up, insider cluster, ATM/shelf, going concern)
  alerts/         candidates → outbox → Telegram; telegram_guard.py (S7 is_owner), dashboard views
  sec_view.py     read-side SEC queries for the dashboard
  market.py       read-side computations for the dashboard (basket, rebasing, staleness)
  runs.py         run_job: every job gets a job_runs row
  facts.py        facts registry: status gating, DB sync, FACTS.md rendering
  db/             engines (rw / ro / command), models (STRICT tables), dialect.py, migrations
  security/       CSRF, security headers, nh3 sanitizer, untrusted-content wrapping
  ops/backup.py   online backup + retention
  jobs.py         APScheduler wiring (Asia/Singapore, max_instances=1)
  worker.py       single writer: migrate → sync config → schedule
  web/            FastAPI + Jinja2 + HTMX (vendored), no inline scripts/styles
config/           watchlist.yaml, sources.yaml, facts.yaml, rubric.yaml, alerts.yaml
tests/            pytest suite; fixtures/cassettes for recorded HTTP
```

## Data & safety notes

- SQLite lives in the named Docker volume `aether-data` at `/data/aether.db`, which is local disk inside the Docker VM. **Never** put the DB on NFS, SMB or a macOS bind mount, because file locking breaks.
- Backups land in the same volume (`/data/backups`). Copy them off the machine if you want real disaster recovery.
- Secrets come from `.env` only (`MASSIVE_API_KEY` and `SEC_USER_AGENT` included) and are passed to the `worker` service only. The Massive key is sent in an `Authorization` header, never in a URL. `.env`, `config/positions.yaml` and `data/` are git- and docker-ignored.
- If a secret ever leaks: rotate it (Anthropic Console → new key; Telegram `/revoke`), then update `.env`.
