# Aether: Quantum Equity Watcher

A self-hosted watcher for a small set of quantum-computing equities. It runs on your own machine and serves a LAN-only dashboard.
**Personal research tool, not financial advice.** The full spec is in [AETHER_BUILD_PROMPT.md](AETHER_BUILD_PROMPT.md), and progress is tracked in [MILESTONE_REPORT.md](MILESTONE_REPORT.md).

Status: **M6** (news & research ingest), the first Phase 2 milestone. Phase 1 (M0–M3) and Phase 1b (M4–M5) are done: prices, SEC filings and deterministic RISK rules, Telegram alerts, backtested model strategies, a password-protected dashboard, your holdings, monthly published targets with a filing-rule overlay, a rebalance plan, a monthly review pack, a USD/SGD view and daily options snapshots. M6 adds RSS news, Claude web-search research runs and the budget-guarded LLM wrapper. News items are stored **unclassified** until M7.

## ⚠️ LAN only: never expose it to the internet

The dashboard has **one site-wide password** (no user accounts) and is meant only for the owner's local network. It runs over **plain HTTP**, so the password and session cookie can be sniffed by anyone on the same network; TLS is out of scope.

- **Do not** port-forward `8080` on your router, and don't put it behind a public tunnel (ngrok, Cloudflare Tunnel, Tailscale Funnel, etc.).
- There's no IP allow-list in the app. Whether the dashboard is reachable depends on your network boundary and host firewall; the password is the only gate behind them.
- Any action that triggers work (the `commands` queue) needs a CSRF token and is rate-limited to 10 per hour.

## Requirements

Only **Docker** (with Compose v2), **make** and **git**. Python, uv, the linters and gitleaks all run inside containers, and nothing is installed on the host.

## Quick start

```bash
cp .env.example .env
chmod 600 .env          # deploy.sh refuses to start if .env is readable by others
# edit .env: SEC_USER_AGENT="Your Name you@example.com" (required for EDGAR) and
#            MASSIVE_API_KEY (price fallback, free tier)
make hash-password      # prompts for the dashboard password; paste both printed lines into .env
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

## Backtest lab + model strategies (M4)

`/strategies` shows how a few rule-based mixes of the watchlist would have behaved, and picks one **model strategy** per risk profile. Everything is computed in code from stored prices (`portfolio/`, numpy); nothing goes to an LLM. **Backtest for reference only. Historical returns are not future gains.**

| Step | What happens |
|---|---|
| Dividends | Cash dividends per share (split-adjusted) for QTUM, the pure-plays, QQQ and SOXX over 2 years: **yfinance**, falling back to **Massive** `/stocks/v1/dividends` only on an error (an empty result is normal). Table `dividends`. |
| Total return | `TR_t = TR_{t-1} · (close_t + dividend_t) / close_{t-1}`, computed in code from `prices_daily` (split-adjusted) + `dividends`, so provider conventions never mix. `prices_daily` itself stays split-adjusted. |
| Candidates | For each profile: 4 families (`core_equal`, `core_inv_vol`, `core_min_var`, `core_momentum`) at the profile's **fixed QTUM weight** (safe 75%, medium 45%, aggressive 15%; M5). The backtest picks only the sleeve method. Per-name caps 10% / 20% / 35%; sleeve weight the caps can't place goes to QTUM (there is no cash sleeve). |
| Backtest | Walk-forward on QTUM's sessions. Weights for session *t* use data up to *t−1* only; 120-session estimation window; a name joins after 60 daily returns; monthly rebalance; 10 bps per unit of turnover. Metrics cover only the out-of-sample sessions. |
| Selection | Drop candidates that break the profile's limits, if any are set (relative to QTUM's own out-of-sample volatility and max drawdown; from M5 they're `null`, shown but not enforced, because the mandate accepts a 100% drawdown), rank by the profile metric (safe: lowest CVaR95; medium/aggressive: highest Sortino), tie-break on max drawdown, then ID. If nothing qualifies, the page says **"No qualifying strategy"** with the reason. |
| Storage | `strategy_runs` (one per distinct `as_of` + input hash), `strategy_metrics`, `strategy_weights` (current targets, read by M5), `strategy_curves` (equity curves, latest 30 runs only). |

- **When:** daily 07:10 SGT (dividends, then backtests), a startup catch-up 5 minutes after boot if the last ok run is over 24 h old, and **Recompute** on `/strategies` (CSRF, rate-limited).
- **Deterministic:** the input hash covers closes, dividends, `config/strategies.yaml` and an algorithm version. Same hash → no new run; stored JSON is canonical, so identical inputs give byte-identical rows.
- **Parameters** live in `config/strategies.yaml` (numbers only, unknown keys rejected): estimation window, cost, momentum lookback, and per profile the fixed QTUM weight, per-name cap, volatility/drawdown limits and ranking metric.
- **Metric definitions** (risk-free rate 0, 252 sessions/year): CAGR, total return, volatility, downside deviation, max drawdown + duration, historical daily VaR95/CVaR95, Sharpe, Sortino, Calmar, beta and Jensen's alpha vs QQQ and QTUM, tracking error and information ratio, up/down capture vs QQQ, worst month, % positive months, average turnover. Exact formulas are in `portfolio/metrics.py`.
- **Known limits:** only about 2 years of prices (QNT and INFQ have much less, and the page says how much), a small concentrated universe, and daily closes only.

## Password, holdings & rebalance (M5)

**Login (S2).** One password, stored only as an scrypt hash (`AETHER_DASHBOARD_PASSWORD_HASH`, format `scrypt:n:r:p:salt:hash`). `make hash-password` prompts for it and prints the hash plus a random `AETHER_SESSION_SECRET`; both go in `.env` and only to the `app` service. The app **refuses to start** without both. A login sets an HMAC-signed, HttpOnly, SameSite=Strict cookie for 30 days; changing the password logs every browser out. After 5 failed logins in 15 minutes from one IP, further attempts get 429 (counted in memory, so an app restart resets them). Failed logins are logged with the IP, never the password. Every route needs a session except `/login`, `/healthz` and static files.

**Holdings.** `/holdings` holds the sleeve: QTUM, the pure-plays and USD cash (nothing else), with optional average cost per share. Saving queues an `update_holdings` command (CSRF, rate-limited); the worker applies it and writes `holdings_history`. The dashboard never writes holdings. Holdings never leave the machine and never go into an LLM prompt. A deprecated `config/positions.yaml` is imported once at worker startup if present (the file is docker-ignored, so in Docker this only happens if you mount it).

**Monthly targets (§6.6).** The selected profile's targets are **published on the 1st at 10:30 SGT** (retried on the 2nd if it failed), or when you press **Publish targets now**. Between publishes they don't move. The very first publish happens as soon as a backtest exists.

**Research overlay, layer 1 (§6.6.1).** At each publish, filing hard rules set a pure-play's weight to 0:
- going concern in its latest 10-K/10-Q;
- an 8-K Item 3.01 listing-compliance notice (active for 180 days);
- acquisition or delisting: Form 25 / 25-NSE, Form 15-12B / 15-12G, or 8-K Item 5.01.

Freed weight goes to the other pure-plays pro rata, up to their caps; the rest goes to QTUM. Each target shows its chain, e.g. `base 6.3% → going concern (event #812) → 0.0%`, linked to the filing. To clear a reviewed filing (say, a Form 25 for an exchange transfer), add its accession to `overlay.cleared_accessions` in `config/strategies.yaml`.

**Off-cycle review.** An event with materiality ≥4 on a pure-play sends one Telegram alert suggesting a review. Targets change only if you press **Publish targets now**, which cites the event.

**Rebalance plan.** Recomputed daily (07:10 SGT) and after any update, against the published targets. A holding trades only if its drift is ≥3 pp or ≥25% of its target and the trade is ≥$100. Sells come first, whole shares by default. "New cash only" mode never sells. Names that qualify but can't be funded say "needs cash". The plan is deterministic (input hash stored).

**Review pack (§6.9).** Built on the 1st with the publish: targets with their chains, the plan, drift, value in USD and SGD, open risk flags, upcoming earnings and lock-ups. It's on `/review`, and a plain-text Telegram version goes out once per month. That version carries target weights, flags, dates and the number of trades only: no share counts, dollar values or account number.

**USD/SGD (reporting only).** Daily 06:50 SGT from yfinance `SGD=X`, falling back to the ECB reference rates (EUR cross). Used only to show values in SGD.

**Options snapshot (§6.8, research only).** Daily 06:40 SGT from yfinance for QTUM and the pure-plays: ATM IV at 30/60/90 days (variance-interpolated between listed expiries, never extrapolated), and put/call volume and open-interest ratios. Contracts must pass quality gates (`config/options.yaml`); a thin chain is stored as null with a reason. Options never feed sizing or trades. Analytics and the ticker-page panel come in M8. Tiger option chains aren't used: Tiger sells API option quotes as a separate paid permission.

**Tiger Brokers (optional, read-only, S8).** Set `TIGER_ID`, `TIGER_PRIVATE_KEY` (RSA key body; PEM headers and `\n` escapes are fine) and `TIGER_ACCOUNT`. They go to the worker only. Then switch "Holdings source" to Tiger. A daily 07:05 SGT job and **Sync from Tiger** replace the share counts of QTUM and the pure-plays with your account's positions. Other positions are ignored (only counted), and cash stays manual. A failed sync keeps the last snapshot with a "stale since" banner.
- **The key can trade.** Tiger offers no read-only API key, so Aether walls it off in code: only `providers/tiger.py` may import `tigeropen`, it exposes positions only, and `scripts/check_broker_readonly.py` (in `make lint`) fails on any order method or stray import. Use a dedicated key and revoke it if unused.
- The SDK sends a `device_id` (a MAC address) with requests; inside Docker that's the container's virtual MAC. Dynamic-domain discovery is off, so it talks only to `openapi.tigerfintech.com`.
- Positions and the account number never reach logs, alerts or prompts; the page shows the account masked (last 4 digits).

## News & research (M6)

**RSS (no LLM, hourly).** Feeds are listed in `config/sources.yaml`:
- **T1 (company IR):** Rigetti, Quantinuum and Infleqtion. Every item is kept and pinned to its ticker.
- **T2 (industry press):** The Quantum Insider and Quantum Computing Report. An item is kept only if it names a watchlist company (`aliases` in `watchlist.yaml`, or an uppercase ticker) or a theme keyword. Everything else is dropped.

How feeds are fetched:
- https only, with conditional GET (ETag / Last-Modified), a 5 MB cap and DTD-rejecting XML parsing.
- robots.txt is checked; an explicit `Disallow` skips the feed.
- **Not covered:** IonQ's and D-Wave's IR feeds and HPCwire's feed answer 403 to non-browser clients. IonQ and D-Wave releases still arrive through EDGAR 8-Ks and research runs.

**Dedupe.** URLs are canonicalized (tracking params, `www.`, fragments and trailing slashes removed). A headline within 3 bits of simhash of a news item from the past 7 days is treated as the same story and becomes another source of that event.
- A copy on a press-release wire or mirror (`syndicators`), or one with the same body text, is **syndicated**.
- `independent_source_count` counts distinct registrable domains among the non-syndicated sources, so a release copied to three sites counts once.
- Excerpts are capped at 500 chars, with a DB check at 600.

**LLM wrapper (`llm/client.py`).** The only module that may import `anthropic` (`scripts/check_llm_imports.py` in `make lint` enforces this).
- **Model must be priced:** every call needs a price in `config/llm.yaml`, or it's refused.
- **Soft budget:** a call runs only if today's spend (SGT day) plus the call's worst case fits `DAILY_LLM_BUDGET_USD` (default $3). Otherwise it's refused and logged as `budget_refused`, and no request is sent. One Telegram/dashboard alert goes out per day at 80%.
- **The hard cap is your Console workspace spend limit.**
- **Tools:** only `research*` purposes may carry tools, and then only web search. Classification and synthesis never get tools.
- **Logging:** every call is a row in `llm_calls` (tokens, searches, cost; never prompt text). The API key is scrubbed from errors and the HTTP loggers stay at WARNING.

**Research runs (`research/`, `RESEARCH_MODEL`, default `claude-opus-5-5`).** Claude with the web-search tool, limited to the T1+T2 domains in `sources.yaml`, looks for reports about one name in a date window.
- **Identifiers only:** the prompt carries the ticker, company name and dates, nothing else.
- **No fabricated items:** events come only from the search engine's result blocks (URL and title), with the excerpt taken from a verbatim citation. The model's own text is kept for audit only and never becomes an item.
- **Dates:** from the result's `page_age`. Items without a date are marked "found" and dated when Aether found them.
- **Sweep:** 08:00 and 20:00 SGT for QTUM and the five pure-plays, last 3 days, ≤5 searches each. Roughly $1.5–2/day with Opus, so most of the $3 soft budget. There's also **Run research sweep now** on `/news`.
- **Backfill:** **runs once, automatically**, a couple of minutes after the worker first starts with `ANTHROPIC_API_KEY` set. It's one Message Batch: 6 names × 12 monthly windows, ≤5 searches each.
  - Estimated **$10–15 one-time** with Opus (tokens at the 50% batch price, searches at $10/1,000).
  - It sits outside the daily soft budget by your decision; only the Console limit caps it.
  - `RESEARCH_BACKFILL=false` turns it off.

## Dashboard

- `/`: the Overview.
  - Stale banners.
  - A chart comparing QTUM, an equal-weighted pure-play basket, SOXX and QQQ, rebased to 100 on a log scale, with ranges from 1M to 2Y.
  - A ticker table (last close, 1d/30d change, distance from the 52-week high, as-of date, provider).
  - The watchlist's weight in QTUM.
  - Open risk flags, recent alerts and RISK filings from the last 30 days.
  - Position drift vs the selected profile, when holdings are saved (M5).
- `/t/<SYMBOL>`: price and volume chart plus a summary. For pure-plays it also shows:
  - open risk flags, lock-ups (with the prospectus excerpt) and earnings dates
  - classified SEC events, a shares-outstanding chart (XBRL) and the capital-structure table
  - Form 4 insider transactions (10b5-1 badge) and the filings list with each rule hit.
  All SEC links go to EDGAR.
- `/strategies`: per profile, the model strategy and its current target weights, equity and drawdown charts vs QTUM/QQQ, every candidate with its pass/fail reasons, and the full metrics table (M4).
- `/holdings`: holdings and cash (editable, or read-only rows in Tiger mode), settings (profile, whole/fractional shares, new-cash-only, holdings source), published targets with each overlay chain, off-cycle events and **Publish targets now**, and the rebalance plan with USD and SGD values (M5).
- `/review`: monthly review packs, latest first (M5).
- `/news`: news and research items (unclassified), each with its tier and its independent and syndicated source counts. Filter by ticker and origin. Also LLM spend vs the soft budget, backfill status, feed health, research sweeps and **Run research sweep now** (M6). Ticker pages for QTUM and the pure-plays show their 10 latest items.
- `/login`: the password form (M5).
- `/alerts`: delivery status (Telegram or dashboard only, and why), the last 100 alerts, and **Send test alert**.
- `/facts`: the facts registry with status badges, source links, notes, open questions and how to sign off.
- `/health`: DB, schema and last run per job.

Charts use vendored ECharts (`web/static/VENDORED.txt`) and load their data from `/api/prices/*`, `/api/dilution/*` and `/api/strategies/curves` as JSON. There are no inline scripts, so the CSP stays strict.

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
| `make lint` | ruff, ruff format check, `mypy --strict`, the `\|safe`/`Markup` ban, the read-only broker check, and `pip-audit` on the hashed lockfile |
| `make hash-password` | Prompt for the dashboard password; print `AETHER_DASHBOARD_PASSWORD_HASH` and a fresh `AETHER_SESSION_SECRET` |
| `make fmt` | ruff format + autofix |
| `make migrate` | `alembic upgrade head` (the worker also does this at startup) |
| `make backup` | Online SQLite backup to `/data/backups/aether-YYYYMMDD.db` (14 days kept, mode 0600) |
| `make secrets-scan` | gitleaks over git history and the working tree |
| `make facts` | Regenerate `FACTS.md` from `config/facts.yaml` |
| `make lock` | Re-resolve `uv.lock` |
| `make eval` | Classifier evals (from M7) |
| `make smoke` | Live check against the running stack (never part of acceptance) |
| `make record-cassette NAME=… URL=… [UA=…] [GZIP=1]` | Record one live HTTP response as a test fixture (manual; SEC needs `UA`, large documents use `GZIP=1`) |
| `make record-options SYMBOL=…` | Record one real yfinance option chain as a test fixture (manual, network) |

## Layout

```
src/aether/
  config.py       env settings + typed YAML loaders (identifiers only; unknown keys rejected)
  providers/      typed provider interfaces: yfinance, Massive, failover; dividends; SEC EDGAR client
  ingest/         prices, dividends, QTUM holdings, EDGAR (filings/Form 4/XBRL), earnings calendar,
                  RSS news + the shared news/research event writer (dedupe, syndication)
  portfolio/      total return, metrics, strategy families, walk-forward backtest, selection, job, views
  edgar/          pure parsers: submissions, Form 4 XML, filing text extractors, XBRL
  llm/            the one Anthropic client: budget guard, tool gate, pricing, llm_calls (M6)
  research/       web-search research runs: sweep + Message Batches backfill (M6)
  news_view.py    read-side queries for /news
  classify/       rules.py: deterministic filing rules (the LLM classifier arrives in M7)
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
config/           watchlist.yaml, sources.yaml, facts.yaml, rubric.yaml, alerts.yaml, strategies.yaml,
                  options.yaml, llm.yaml
tests/            pytest suite; fixtures/cassettes for recorded HTTP
```

## Data & safety notes

- SQLite lives in the named Docker volume `aether-data` at `/data/aether.db`, which is local disk inside the Docker VM. **Never** put the DB on NFS, SMB or a macOS bind mount, because file locking breaks.
- Backups land in the same volume (`/data/backups`). Copy them off the machine if you want real disaster recovery.
- Secrets come from `.env` only (`MASSIVE_API_KEY` and `SEC_USER_AGENT` included) and are passed to the `worker` service only. The Massive key is sent in an `Authorization` header, never in a URL. `.env`, `config/positions.yaml` and `data/` are git- and docker-ignored.
- If a secret ever leaks: rotate it (Anthropic Console → new key; Telegram `/revoke`), then update `.env`.
