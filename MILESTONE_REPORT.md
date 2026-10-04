# Milestone report

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
