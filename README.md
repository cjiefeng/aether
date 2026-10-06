# Aether: Quantum Equity Watcher

A self-hosted watcher for a small set of quantum-computing equities. It runs on your own machine and serves a LAN-only dashboard.
**Personal research tool, not financial advice.** The full spec is in [AETHER_BUILD_PROMPT.md](AETHER_BUILD_PROMPT.md), and progress is tracked in [MILESTONE_REPORT.md](MILESTONE_REPORT.md).

Status: **M10** (conclusions, track record, brief), the last Phase 2 milestone. Phase 1 (M0–M3) and Phase 1b (M4–M5) are done: prices, SEC filings and deterministic RISK rules, Telegram alerts, backtested model strategies, a password-protected dashboard, your holdings, monthly published targets with a filing-rule overlay, a rebalance plan, a monthly review pack, a USD/SGD view and daily options snapshots. M6 added RSS news, Claude web-search research runs and the budget-guarded LLM wrapper. M7 classifies every news and research item as SIGNAL, NOISE or RISK (rules first, then Claude with no tools), applies the trust-tier caps in code, quarantines injection attempts and adds the Feed page and `make eval`. M8 adds dated catalysts with deterministic hit/slip resolution, FINRA short interest with a spike rule, and options analytics (skew, implied moves into catalysts, positioning, IV rank) on the ticker page and in the review pack. M9 adds the deterministic scorecard (with fully diluted EV and cash runway from SEC XBRL), the event-reaction engine and Calibration page, the QTUM theme decomposition, and the overlay's dilution and runway haircuts. M8 and M9 spend nothing on LLMs. M10 adds the weekly conclusions (Claude Opus 5.5, no tools, citation-checked, with code-enforced stance hysteresis), the track record against two naive baselines, stance multipliers in the overlay with an earned-trust clamp, the overlay value-added check, and the weekly brief.

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

**Options snapshot (§6.8, research only).** Daily 06:40 SGT from yfinance for QTUM and the pure-plays: ATM IV at 30/60/90 days (variance-interpolated between listed expiries, never extrapolated), and put/call volume and open-interest ratios. Contracts must pass quality gates (`config/options.yaml`); a thin chain is stored as null with a reason. Options never feed sizing or trades. M8 adds the analytics and the ticker-page panel (below). Tiger option chains aren't used: Tiger sells API option quotes as a separate paid permission.

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
- **Soft budget:** a call runs only if today's spend (SGT day) plus the call's worst case fits `DAILY_LLM_BUDGET_USD` (default $5 from M7). Otherwise it's refused and logged as `budget_refused`, and no request is sent. One Telegram/dashboard alert goes out per day at 80%.
- **The hard cap is your Console workspace spend limit.**
- **Tools:** only `research*` purposes may carry tools, and then only web search. Classification and synthesis never get tools.
- **Logging:** every call is a row in `llm_calls` (tokens, searches, cost; never prompt text). The API key is scrubbed from errors and the HTTP loggers stay at WARNING.

**Research runs (`research/`, `RESEARCH_MODEL`, default `claude-opus-5-5`).** Claude with the web-search tool, limited to the T1+T2 domains in `sources.yaml`, looks for reports about one name in a date window.
- **Identifiers only:** the prompt carries the ticker, company name and dates, nothing else.
- **No fabricated items:** events come only from the search engine's result blocks (URL and title), with the excerpt taken from a verbatim citation. The model's own text is kept for audit only and never becomes an item.
- **Dates:** from the result's `page_age`. Items without a date are marked "found" and dated when Aether found them.
- **Sweep:** 08:00 and 20:00 SGT for QTUM and the five pure-plays, last 3 days, ≤5 searches each. Roughly $1.5–2/day with Opus. There's also **Run research sweep now** on `/news`.
- **Backfill:** **runs once, automatically**, a couple of minutes after the worker first starts with `ANTHROPIC_API_KEY` set. It's one Message Batch: 6 names × 12 monthly windows, ≤5 searches each.
  - Estimated **$10–15 one-time** with Opus (tokens at the 50% batch price, searches at $10/1,000).
  - It sits outside the daily soft budget by your decision; only the Console limit caps it.
  - `RESEARCH_BACKFILL=false` turns it off.

## Classifier (M7)

Every news and research item gets a classification record (spec §5): class (SIGNAL / NOISE / RISK), category, materiality 1–5 (with the pre-cap value kept), a direction per affected ticker, confidence, rationale, an evidence quote, the injection flag, and either a `rule_id` or the model plus `prompt_version`. SEC filings keep their deterministic rules from M2.

The pipeline (job `classify`, every 10 minutes and right after each RSS or research ingest):
1. **Rules first** (`classify/rules.py`, no LLM): headline patterns for analyst ratings and listicles, and an optional noise-domain list (`config/rubric.yaml`). A company's own (T1) release always goes to the model.
2. **The model** (`CLASSIFIER_MODEL`, default `claude-sonnet-5-5`; **no tools**). The system prompt is the rubric in `config/rubric.yaml` (the spec's category definitions and materiality anchors) plus the untrusted-content notice. The user message holds only the item's watchlist tickers, its source and tier, its date and the wrapped title and excerpt. The answer is structured JSON, validated in code:
   - the category must belong to the class;
   - the evidence quote must be a verbatim quote from the item;
   - directions must cover exactly the item's tickers.
   An invalid answer is rejected, never repaired. It gets one retry, after which the item is marked `failed` and listed on the Feed.
3. **Trust-tier caps** (`classify/caps.py`), applied in code after the model. T3 sources only → at most 2; a single independent T2 source → at most 3; 4–5 needs a T1 source or two independent T2 domains (syndicated copies don't count). When a better source merges into the story later, the stored materiality rises back towards the model's value.
4. **Injection guard.** If the model flags an instruction aimed at it, or a backstop regex (`injection_patterns`) matches, the event is **quarantined**. It's shown on the Feed with a warning and is excluded from alerts, the off-cycle review, scores and synthesis.

**Backlog.** When more than 25 items wait (e.g. after the research backfill), they go to one Message Batch (50% price). By your decision it sits outside the daily soft budget; a poller ingests the results.

**Alerts.** Classified news RISK at or above `risk_event_min_materiality` alerts like an EDGAR RISK event, and materiality ≥4 on a pure-play suggests an off-cycle review (M5). Quarantined items never alert.

**Evals (`make eval`, spec §5.3).** `evals/classifier_golden.jsonl` holds **real ingested items only**. Claude Code proposed the labels and you reviewed them (`labeled_by: owner`); the M7 run scored 93.4% class agreement and flagged 5/5 adversarial cases, with RISK recall not yet measurable (no RISK rows). The harness also builds 5 adversarial cases: a real excerpt with an injected instruction appended, which must be flagged without changing class or materiality. The report gives per-class precision/recall, a confusion matrix and the acceptance bar (≥85% class agreement, ≥95% RISK recall, 100% of adversarial cases flagged).
- The eval is live: it calls the API with the key in `.env`, against a throwaway DB (about $0.20 per run).
- Each result is committed as `evals/results/<prompt_version>.json` and shown on the Feed.
- Any rubric or prompt edit is a new `prompt_version`, so rerun the eval.
- See `evals/README.md` for the under-sampled categories and how to extend the set (`make golden-candidates DB=…`).

## Catalysts + market structure (M8)

No LLM calls; every number is computed in code.

**Catalysts.** `config/catalysts_seed.yaml` holds dated milestones, each linked to a fact in `config/facts.yaml` (its status badge shows wherever the catalyst does): the IBM roadmap (Kookaburra 2026, Cockatoo 2027, Starling 2029), the DARPA QBI Stage C decision for IONQ and QNT (window from 2026-11-06, no end date because DARPA states none), and the QNT IPO lock-up (2026-11-30). Earnings dates from the earnings calendar and lock-ups from final prospectuses become catalysts automatically. The `catalysts` job (every 30 minutes, and after the EDGAR, earnings and classifier runs) resolves them deterministically:
- earnings → **hit** when an 8-K Item 2.02 lands within ±3 days; a future date that leaves the calendar → **cancelled** (moved);
- lock-ups → **hit** once the date arrives;
- roadmap/program → **hit** or **slipped** from a non-quarantined classified event (materiality ≥ 3 after caps) in one of the seed's categories that names a seed keyword, inside the window ± 90 days; **slipped** if nothing resolves it 90 days after the window ends.
Each resolution cites the event. You can mark any catalyst hit / slipped / cancelled, or reopen it, from the Catalysts page (CSRF'd `mark_catalyst` command).

**Short interest.** Daily 07:20 SGT the worker checks FINRA's free bi-weekly short-interest files (`cdn.finra.org`, mid-month and month-end settlement dates, published about a week later; 12 months backfilled). Only QTUM and pure-play rows are kept. FINRA publishes no float, so the percentage is **short shares ÷ shares outstanding** (SEC XBRL cover page), which understates short % of float. The `finra_short_interest_spike` rule (`config/rubric.yaml` → `short_interest`) raises a RISK event (materiality 3, T1) when a pure-play's percentage rises by ≥ 5 points vs the prior report or crosses above 25%, and the open-flag list shows it while it lasts. Backfilled spikes are dated by their settlement date, so they never alert.

**Options analytics (§6.8, research only).** The daily options snapshot now also stores:
- 30-day **skew** (25-delta put IV − 25-delta call IV; Black-Scholes deltas with r = 0 from each contract's own IV, interpolated in delta, then in days between expiries);
- the **implied move** into each upcoming catalyst: the at-the-money straddle mid ÷ spot on the first listed expiry after the catalyst;
- **positioning** (put/call volume and open interest, volume vs the median of the last 20 snapshots);
- **IV rank / percentile** over Aether's own last 252 snapshots ("building history (N days)" until then).
Nothing is extrapolated past the listed expiries; a thin chain is flagged, never reported. Options never reach targets, sizing or trades (a test checks that changing every snapshot leaves published targets byte-identical).

## Scorecards, reactions, theme (M9)

No LLM calls; every number is computed in code. Parameters live in `config/weights.yaml` (numbers only, initial values for your review).

**Fundamentals (SEC XBRL, point in time).** `score/fundamentals.py` uses only facts filed on or before the date it's asked about:
- TTM revenue and operating cash flow: the latest fiscal year + year-to-date − the prior year's same YTD (10-Qs report Q2/Q3 cash flows only as YTD, so the EDGAR ingest now keeps 6- and 9-month values for these concepts).
- **Fully diluted shares** = common shares outstanding + warrants + options + unvested RSUs + shares underlying convertibles, each as tagged. An untagged component is listed as "not tagged", never estimated.
- **FD YoY** compares the same components about a year apart. The year-ago count must be dated on or after the stock's first trading session, so de-SPACs and IPOs (INFQ, QNT) show "listed < 1 year" rather than a SPAC-to-company jump.
- Liquidity = cash + current and non-current marketable debt securities (one XBRL concept per bucket; `LongTermInvestments` is left out because it can hold strategic stakes); cash runway = liquidity ÷ (−TTM operating cash flow ÷ 4) × 3 months, or "not burning".
- **EV** = FD shares × last close + debt + convertibles − liquidity; EV/Sales on TTM revenue.
Flows and balances more than 400 days old are treated as stale, not used. A parser-version bump makes the next EDGAR run refetch companyfacts once for every symbol (`xbrl_fetches`).

**Overlay layer 1, M9 haircuts** (`config/strategies.yaml` → `overlay`): FD shares up more than 20% YoY → × 0.5; runway under 12 months → × 0.5 (both → × 0.25). They apply at the next publish, cite the 10-Q/10-K behind the figure, and the freed weight goes to the other pure-plays within caps, then QTUM. Valuation and options never enter the overlay.

**Scorecard** (daily 07:00 SGT, pure-plays and QTUM): fundamentals, dilution, signal momentum, risk load (events + open flags), short interest, catalyst position, noise ratio (with a hype flag), price context and market reaction (weight 0). Each component is the mean of its sub-scores, each mapped from a raw metric to −1…+1 by a piecewise-linear anchor map. The total is −100…+100 over the components that have data, with the coverage shown. It's an input for M10's stances, not a stance.

**Event reactions** (daily 06:45 SGT, §6.3). For each non-quarantined classified event × pure-play (benchmark QTUM) or QTUM (benchmark QQQ):
- t0 = the first NYSE session whose close is after the event time (`exchange_calendars`: holidays and 13:00 half-days);
- a market model on total-return daily returns over the 120 sessions before t0 (β = 1 with fewer than 60);
- CAR and z over [t0, t0+1], [t0, t0+5], [t0, t0+20], abnormal volume and the reversal ratio;
- `confounded` if another materiality ≥ 3 event, an earnings release or a RISK filing hits the ticker within [t0−1, t0+5].
Research items dated only by retrieval time or a date-only `page_age` are flagged "approximate time" and kept out of calibration.

**Calibration** (Sunday 08:00 SGT; `/calibration`): per class and category (n ≥ 15), mean |z₁| and |z₅|, the share with |z₅| > 2, the median reversal ratio, abnormal volume and the direction hit rate, with flags that suggest (never apply) rubric changes, plus **implied vs realized move** for each catalyst resolved by an event, using the last options snapshot before it.

**Theme decomposition** (daily 07:00 SGT; Overview, "quantum-sleeve view"): a 120-session OLS of QTUM's daily total returns on SOXX, QQQ and an equal-weighted pure-play basket (a name joins after 60 sessions), with betas, the basket's partial R², a 30/90-session attribution and the watchlist's weight in QTUM's holdings as a cross-check.

## Conclusions, track record, brief (M10)

**Conclusions** (Sunday 08:30 SGT; `SYNTH_MODEL`, default `claude-opus-5-5`, effort `high`, **no tools**). One run per pure-play, then QTUM ("quantum-sleeve view"), then the theme tilt between QTUM and the equal-weight pure-play basket (PURE_PLAYS / NEUTRAL / QTUM).
- **Context** (`synthesize/context.py`): the scorecard, the last 90 days of non-quarantined classified events (at most 60, materiality ≥ 3 first), their market reactions, catalysts, the ticker's facts (UNCONFIRMED unless signed off), the options panel, the theme decomposition and the ticker's own track record. Every item has a citable id (`E812`, `R:812`, `C5`, `F:fact_id`, `S:dilution`, `O:IONQ`, `X:theme`, `K:IONQ`, `T:IONQ`). Event titles, excerpts and rationales go inside one `<untrusted_document>` block. Holdings, cash, the account and the mandate never enter a prompt.
- **Citation validator** (`synthesize/validate.py`): strict schema; every cited id must be in the context (quarantined events never are); every thesis point needs evidence; QTUM and the theme must cite `X:theme`; `injection_suspected` is a failure. One retry with the validator's message, then a `conclusion_failures` row; nothing invalid is stored.
- **Hysteresis** (`synthesize/hysteresis.py`, `config/weights.yaml` → `conclusions`): a different stance is accepted only with a cited non-quarantined event of materiality ≥ 4 since the previous conclusion, or the scorecard total beyond the current band by 20 points for 5 straight days, **and** 14 days since the last change (a cited T1 RISK event bypasses the cooldown on a downgrade). Otherwise it's stored as **held**, with the reason.
- The daily soft budget applies; a budget refusal stops the run. **Re-run conclusion** on a ticker page (or all on `/track-record`) is a CSRF'd `synthesize` command. If no conclusion is newer than 8 days, the worker runs once 15 minutes after start.

**Track record** (daily 07:30 SGT; `score/track_record.py`, `config/weights.yaml` → `track_record`). Every non-held conclusion's excess total return vs QTUM (vs QQQ for QTUM; basket − QTUM for the tilt) at 1, 3, 6, 12, 24 and 36 months, filled in as each horizon matures. Hits: ACCUMULATE > 0, TRIM/AVOID < 0, HOLD inside a band (10% at 6 months, sqrt-scaled). Baselines on the same windows: "always HOLD" and 90-day momentum. Reported per stance and ticker with confidence buckets and a Brier score. Weekly reaffirmations count as calls, so their windows overlap; the page says so. "No track record yet." until a 6-month result exists.

**Overlay layer 2** (`config/strategies.yaml` → `overlay`): each pure-play's latest stance (≤ 35 days old) scales its weight: ACCUMULATE × 1.25, HOLD × 1, TRIM × 0.5, AVOID × 0. **Earned trust:** until a ticker has 10 mature 6-month calls and beats both baselines, its multiplier is clamped to [0.75, 1.25]. Cut names never receive freed weight; if stances push the sleeve above its base total, the uncut names are scaled back, so QTUM's fixed weight is never squeezed. Each step cites its conclusion id. **Layer 3:** every publish's base and adjusted targets are tracked as buy-and-hold paper portfolios; after 12 monthly publishes `/track-record` says whether the adjusted targets have beaten the base.

**Weekly brief** (Sunday 09:00 SGT; `/briefs`): built from the database, no LLM. What changed, top signals, risks, filtered-noise counts, the next 30 days, the stance table with track record, a reaction note and position drift. Telegram gets a plain-text copy once a week (no holdings values; only the number of names outside the no-trade band). The monthly review pack now includes the stances and the overlay value-added line.

## Dashboard

- `/`: the Overview.
  - Stale banners.
  - A chart comparing QTUM, an equal-weighted pure-play basket, SOXX and QQQ, rebased to 100 on a log scale, with ranges from 1M to 2Y.
  - A ticker table (last close, 1d/30d change, distance from the 52-week high, as-of date, provider).
  - The watchlist's weight in QTUM.
  - Open risk flags, recent alerts and RISK filings from the last 30 days.
  - Position drift vs the selected profile, when holdings are saved (M5).
  - Catalysts and earnings for the next 12 months (M8).
  - The scorecard total per ticker and the QTUM theme decomposition (M9).
  - The theme tilt banner and stance columns (stance, confidence, change vs last week, track record) (M10).
- `/t/<SYMBOL>`: price and volume chart plus a summary. For pure-plays it also shows:
  - open risk flags, lock-ups (with the prospectus excerpt) and earnings dates
  - classified SEC events, a shares-outstanding chart (XBRL) and the capital-structure table
  - Form 4 insider transactions (10b5-1 badge) and the filings list with each rule hit.
  All SEC links go to EDGAR. Every ticker page lists its catalysts (M8); QTUM and the pure-plays also get the short-interest table and the options panel (ATM IV and rank, term structure, skew, implied moves, positioning, quality flags), and from M9 the scorecard breakdown, the reaction table with "market agreed / disagreed" badges and event markers on the price chart. Pure-plays also get the valuation and dilution card (fully diluted shares by component, EV, EV/Sales, cash runway). From M10, QTUM and the pure-plays show the conclusion card: stance, verdict, thesis and bear case with clickable citations, hysteresis status, track record, history with a diff, and **Re-run conclusion**.
- `/catalysts`: a 12-month timeline, the upcoming list with fact-status badges and a **Mark** form per row, and the hit/slip record (M8).
- `/strategies`: per profile, the model strategy and its current target weights, equity and drawdown charts vs QTUM/QQQ, every candidate with its pass/fail reasons, and the full metrics table (M4).
- `/holdings`: holdings and cash (editable, or read-only rows in Tiger mode), settings (profile, whole/fractional shares, new-cash-only, holdings source), published targets with each overlay chain, off-cycle events and **Publish targets now**, and the rebalance plan with USD and SGD values (M5).
- `/review`: monthly review packs, latest first (M5); from M8 with the next 90 days of catalysts and the options panel per name.
- `/track-record`: hit rates vs the baselines per horizon, stance and ticker, confidence calibration, the theme tilt, the overlay value-added check and failed runs (M10).
- `/briefs`: the weekly brief archive (M10).
- `/calibration`: the weekly calibration report, flags, high-materiality events the market ignored, implied vs realized moves and the trend (M9).
- `/feed`: every classified event (SEC filings and news), newest first. Filter by class, category, ticker, minimum materiality and trust tier; NOISE is hidden unless you ask for it. Each row shows the materiality after caps ("capped from N"), the direction per ticker, confidence, rationale, evidence quote, sources with tiers, and the rule or model/prompt version. Quarantined items carry a warning. A strip at the top shows S/N/R counts for 30 days, quarantined, waiting and failed items, and the latest eval for the current prompt (M7).
- `/news`: news and research items with their class badge (or "pending"), each with its tier and its independent and syndicated source counts. Filter by ticker and origin. Also LLM spend vs the soft budget, backfill status, feed health, research sweeps and **Run research sweep now** (M6). Ticker pages for QTUM and the pure-plays show their 10 latest items.
- `/login`: the password form (M5).
- `/alerts`: delivery status (Telegram or dashboard only, and why), the last 100 alerts, and **Send test alert**.
- `/facts`: the facts registry with status badges, source links, notes, open questions and how to sign off.
- `/health`: DB, schema and last run per job.

Charts use vendored ECharts (`web/static/VENDORED.txt`) and load their data from `/api/prices/*`, `/api/dilution/*`, `/api/strategies/curves`, `/api/catalysts` and `/api/reactions/*` as JSON. There are no inline scripts, so the CSP stays strict.

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
| `make eval` | Live classifier eval on the golden set + adversarial cases (reads the key from `.env`; costs about $0.20) |
| `make golden-candidates DB=…` | Export real ingested events from a DB copy as golden-set candidates (read-only) |
| `make smoke` | Live check against the running stack (never part of acceptance) |
| `make record-cassette NAME=… URL=… [UA=…] [GZIP=1]` | Record one live HTTP response as a test fixture (manual; SEC needs `UA`, large documents use `GZIP=1`) |
| `make record-options SYMBOL=…` | Record one real yfinance option chain as a test fixture (manual, network) |
| `make record-short-interest DATES="…"` | Record real FINRA short-interest files, filtered to the universe, as test fixtures (manual, network) |

## Layout

```
src/aether/
  config.py       env settings + typed YAML loaders (identifiers only; unknown keys rejected)
  providers/      typed provider interfaces: yfinance, Massive, failover; dividends; SEC EDGAR client
  ingest/         prices, dividends, QTUM holdings, EDGAR (filings/Form 4/XBRL), earnings calendar,
                  FINRA short interest (M8),
                  RSS news + the shared news/research event writer (dedupe, syndication)
  portfolio/      total return, metrics, strategy families, walk-forward backtest, selection, job, views
  edgar/          pure parsers: submissions, Form 4 XML, filing text extractors, XBRL
  llm/            the one Anthropic client: budget guard, tool gate, pricing, llm_calls (M6)
  research/       web-search research runs: sweep + Message Batches backfill (M6)
  news_view.py    read-side queries for /news
  classify/       rules (filings + news headlines), prompt, output validation, caps, the queue
                  (sync + Message Batches backlog) and `make eval` (M7)
  feed_view.py    read-side queries for /feed
  risk/           flags.py: open risk flags (lock-up, insider cluster, ATM/shelf, going concern,
                  short-interest spike)
  catalysts/      seed/earnings/lock-up sync, deterministic resolution, owner marks, views (M8)
  score/          point-in-time fundamentals, scorecards, event reactions, calibration, theme
                  decomposition, views (M9)
  synthesize/     conclusions: context, prompt, citation validator, hysteresis, runs, weekly
                  brief, views (M10); score/track_record.py holds the track record
  nyse.py         NYSE session calendar (the only exchange_calendars importer)
  options/        snapshot metrics, analytics (skew, implied moves, IV rank), job, panel view
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
                  options.yaml, llm.yaml, catalysts_seed.yaml, weights.yaml
evals/            classifier golden set (real items) + committed eval results
tests/            pytest suite; fixtures/cassettes for recorded HTTP
```

## Data & safety notes

- SQLite lives in the named Docker volume `aether-data` at `/data/aether.db`, which is local disk inside the Docker VM. **Never** put the DB on NFS, SMB or a macOS bind mount, because file locking breaks.
- Backups land in the same volume (`/data/backups`). Copy them off the machine if you want real disaster recovery.
- Secrets come from `.env` only (`MASSIVE_API_KEY` and `SEC_USER_AGENT` included) and are passed to the `worker` service only. The Massive key is sent in an `Authorization` header, never in a URL. `.env`, `config/positions.yaml` and `data/` are git- and docker-ignored.
- If a secret ever leaks: rotate it (Anthropic Console → new key; Telegram `/revoke`), then update `.env`.
