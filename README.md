# Aether: Quantum Equity Watcher

A self-hosted watcher for a small set of quantum-computing equities. It runs on your own machine and serves a LAN-only dashboard.
**Personal research tool, not financial advice.** The full spec is in [AETHER_BUILD_PROMPT.md](AETHER_BUILD_PROMPT.md), and progress is tracked in [MILESTONE_REPORT.md](MILESTONE_REPORT.md).

Status: **M1** (market data). Daily prices for all 13 tickers with a 2-year backfill, a daily QTUM holdings snapshot and an Overview page with price charts. There are no filings or LLM usage yet.

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
# edit .env: MASSIVE_API_KEY (price fallback, free tier) and SEC_USER_AGENT (from M2)
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

## Dashboard

- `/`: the Overview.
  - Stale banners.
  - A chart comparing QTUM, an equal-weighted pure-play basket, SOXX and QQQ, rebased to 100 on a log scale, with ranges from 1M to 2Y.
  - A ticker table (last close, 1d/30d change, distance from the 52-week high, as-of date, provider).
  - The watchlist's weight in QTUM.
- `/t/<SYMBOL>`: price and volume chart plus a summary. Filings arrive in M2.
- `/health`: DB, schema and last run per job.

Charts use vendored ECharts (`web/static/VENDORED.txt`) and load their data from `/api/prices/*` as JSON. There are no inline scripts, so the CSP stays strict.

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
| `make record-cassette NAME=… URL=…` | Record one live HTTP response as a test fixture (manual) |

## Layout

```
src/aether/
  config.py       env settings + typed YAML loaders (identifiers only; unknown keys rejected)
  providers/      typed provider interfaces: yfinance, Massive, failover
  ingest/         prices (backfill/incremental) and QTUM holdings snapshot
  market.py       read-side computations for the dashboard (basket, rebasing, staleness)
  runs.py         run_job: every job gets a job_runs row
  facts.py        facts registry: status gating, DB sync, FACTS.md rendering
  db/             engines (rw / ro / command), models (STRICT tables), dialect.py, migrations
  security/       CSRF, security headers, nh3 sanitizer, untrusted-content wrapping
  ops/backup.py   online backup + retention
  jobs.py         APScheduler wiring (Asia/Singapore, max_instances=1)
  worker.py       single writer: migrate → sync config → schedule
  web/            FastAPI + Jinja2 + HTMX (vendored), no inline scripts/styles
config/           watchlist.yaml, sources.yaml, facts.yaml
tests/            pytest suite; fixtures/cassettes for recorded HTTP
```

## Data & safety notes

- SQLite lives in the named Docker volume `aether-data` at `/data/aether.db`, which is local disk inside the Docker VM. **Never** put the DB on NFS, SMB or a macOS bind mount, because file locking breaks.
- Backups land in the same volume (`/data/backups`). Copy them off the machine if you want real disaster recovery.
- Secrets come from `.env` only (`MASSIVE_API_KEY` included) and are passed to the `worker` service only. The Massive key is sent in an `Authorization` header, never in a URL. `.env`, `config/positions.yaml` and `data/` are git- and docker-ignored.
- If a secret ever leaks: rotate it (Anthropic Console → new key; Telegram `/revoke`), then update `.env`.
