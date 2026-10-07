# Aether runbook

Operations for the single-host Docker deployment (the supported deploy). Everything runs through `./deploy.sh`, `make` and `docker compose`. Nothing is installed on the host.

## Fresh install (target: under 10 minutes)

```bash
git clone git@github.com:cjiefeng/aether.git && cd aether
make init        # prompts for the dashboard password; writes .env (0600) with the hash and secrets
# edit .env: SEC_USER_AGENT="Your Name you@example.com"; optional keys (Anthropic, Massive, Telegram, Tiger)
./deploy.sh      # build, start, wait until both services are healthy
```

`make init` never overwrites an existing `.env`. Without API keys the stack still runs: the LLM features, the price fallback, Telegram and Tiger stay off (fail closed), and the dashboard says why.

## Deploy and roll back

- **Deploy:** `./deploy.sh` fast-forwards `main`, then runs `docker compose up -d --build --wait` and checks `/healthz`.
- **Roll back:**
  1. `git checkout <good commit>`
  2. `docker compose up -d --build --wait`
  3. Fix forward on `main` afterwards.
- **Migrations only go forward.** Rolling back code across a migration needs `alembic downgrade` first. Run it with the stack stopped:

  ```bash
  docker compose run --rm worker python -c "from alembic import command; from aether.db.migrate import alembic_config; from aether.config import get_settings; command.downgrade(alembic_config(get_settings().db_path), '<rev>')"
  ```

  Restoring the pre-deploy backup is usually simpler.

## Logs

Both services log one JSON object per line (`AETHER_LOG_FORMAT=json`, the compose default; set `text` for the classic format). Secrets are masked before anything is written.

```bash
docker compose logs -f --no-log-prefix worker | jq -c 'select(.level != "INFO")'
docker compose logs --no-log-prefix worker | jq -c 'select(.job == "escalations")'
docker compose logs --no-log-prefix worker | jq -r 'select(.status == "failed") | [.ts, .job, .msg] | @tsv'
```

`run_job` logs `job`, `run_id`, `status`, `rows`, `provider`, `warning` and `duration_ms` for every scheduled job. The same runs are in `job_runs`, which the Ops page shows.

## Backups, the restore drill and restore

- **Nightly backup** (04:00 SGT): `/data/backups/aether-YYYYMMDD.db`, online and consistent, mode 0600, 14 days kept.
  - `make backup` takes one now.
  - Copy backups off the machine yourself if you need off-host copies. `docker compose cp worker:/data/backups ./backups-copy` works.
- **Restore drill** (Sundays 04:30 SGT; `make restore-drill` for one now):
  1. Copies the newest backup to a temp dir.
  2. Runs `PRAGMA integrity_check`.
  3. Migrates the copy forward if it's behind.
  4. Compares row counts with the live DB.
  5. Renders the main dashboard pages from the copy in-process. Each must return 200.

  It never touches the live DB. A failing drill fails the `restore_drill` job, and after 24h that raises the job-failing alert. The result is on the Ops page.
- **Restore:**

  ```bash
  make restore BACKUP=aether-20261008.db   # stops app + worker, swaps the DB in
  ./deploy.sh                               # start again; the worker migrates forward if needed
  ```

  The previous DB is kept next to it as `aether.db.pre-restore-<UTC stamp>` (plus its `-wal`/`-shm`). Delete it once you're happy:

  ```bash
  docker compose run --rm worker sh -c 'rm /data/aether.db.pre-restore-*'
  ```

- **Lost volume:** recreate the stack (`./deploy.sh` creates an empty volume). Then copy a backup into it and restore:

  ```bash
  docker compose cp ./aether-YYYYMMDD.db worker:/data/backups/
  make restore BACKUP=aether-YYYYMMDD.db
  ```

## Escalation (spec §5.2.5)

- **What triggers it:** an event with post-cap materiality ≥ 4 (any class), or a RISK event ≥ 3 on a T1 source.
- **What it does:** a Telegram alert, one verification search (web search, `verify_max_uses`), the classifier on what that search found, a re-synthesis of the ticker, and a result alert.
- **Hysteresis still applies.** A flip needs a qualifying trigger, so an escalation may end with a "held" stance.
- **Caps:**
  - `MAX_ESCALATIONS_PER_DAY` (env, default 5, per SGT day).
  - One per ticker per `cooldown_hours` (`config/llm.yaml`, 6).
  - A refused escalation is recorded with its reason, is final, and sends nothing. The event's own RISK alert is unaffected.
- **Cost:** about one Opus web-search call ($0.05–0.15) plus one Opus conclusion ($0.10–0.30). It counts against `DAILY_LLM_BUDGET_USD`. A budget refusal still uses the day's escalation slot.
- **Tuning:** thresholds and the cooldown are in the `escalation:` block of `config/llm.yaml`; the daily cap is in `.env`. Then `./deploy.sh`.
- **To pause escalations:** set `MAX_ESCALATIONS_PER_DAY=0`.

## Rotate keys and secrets

After any change to `.env`: `chmod 600 .env && docker compose up -d --wait` (or `./deploy.sh`).

| Secret | How |
|---|---|
| `ANTHROPIC_API_KEY` | Console → the Aether workspace → create a new key, put it in `.env`, restart, check the Ops page shows LLM calls working, then **disable the old key** in the Console. Keep the dev key (`make eval`) separate from the app key. Check that the workspace's monthly spend limit is still set (the hard cap). |
| `TELEGRAM_BOT_TOKEN` | BotFather `/revoke` → new token into `.env` → restart → **Send test alert** on `/alerts`. The user and chat IDs don't change. |
| `TIGER_*` | Tiger developer portal: regenerate the RSA key pair and upload the new public key, put the private key in `.env`, restart, then **Sync from Tiger** on `/holdings`. Revoke the old key. |
| `MASSIVE_API_KEY` | Massive dashboard → new key → `.env` → restart. |
| `SEC_USER_AGENT` | Not a secret. Keep a working contact address in it. |
| Dashboard password | `make hash-password` → replace `AETHER_DASHBOARD_PASSWORD_HASH` **and** `AETHER_SESSION_SECRET` (a new secret logs out every browser) → restart. |
| `AETHER_CSRF_SECRET` | Any random string (`make init` uses 32 url-safe bytes). Changing it invalidates open forms; reload the page. |

If a key leaked into git: rotate it first, then check with `make secrets-scan` (gitleaks over the full history) and rewrite history only if the repo was ever shared.

## Migrate to MySQL / Postgres

SQLite is the right size for this workload (tens of MB). If you ever move, the code is set up for it:

1. **Dialect seams.** All SQL goes through SQLAlchemy Core. The dialect-specific pieces are in `db/dialect.py`: `upsert` (`ON CONFLICT` vs `ON DUPLICATE KEY UPDATE`) and `json_extract`. The pragmas and `BEGIN IMMEDIATE` are in `db/engine.py`. Add the MySQL/Postgres branches there.
2. **Engines.**
   - `make_rw_engine`: the worker's user, with DML and DDL rights.
   - `make_ro_engine`: a read-only user (`GRANT SELECT`). This replaces `mode=ro`.
   - `make_command_engine`: a user with `GRANT SELECT` plus `GRANT INSERT ON commands` only. This replaces the SQLite authorizer.
   - Take the DSNs from env, and keep the single-writer rule even though the server allows more writers.
3. **Schema.**
   - The models already avoid `String`/`Boolean`/`DateTime`. Money is `Micros` (BIGINT).
   - `CHECK (json_valid(col))` becomes a `JSON`/`JSONB` column type.
   - STRICT and `WITHOUT ROWID` are SQLite-only table options; guard them by dialect in `models.py`.
   - Run the migrations against an empty server DB with `alembic upgrade head`. They are hand-written with `render_as_batch`; batch ops become plain `ALTER TABLE`s on a server DB.
4. **Data copy.**
   1. Stop the stack.
   2. Take a fresh backup.
   3. Run a one-off script that reads each table from the SQLite backup with SQLAlchemy Core and inserts in FK order (`metadata.sorted_tables`), in batches. simhash is already signed 64-bit, so it fits BIGINT.
   4. Compare row counts per table.
5. **Cut over.**
   1. Point the env DSNs at the server.
   2. `./deploy.sh`.
   3. Check the Ops page and the restore drill. The drill needs a server-side equivalent (`mysqldump`/`pg_dump` plus a scratch database); replace `ops/backup.py` and `ops/restore.py` before relying on it.
6. **Tests.** Keep the SQLite suite. Add a CI job with a service container for the new engine before switching production.

## Litestream (documented, not built)

[Litestream](https://litestream.io) streams the SQLite WAL to S3-compatible storage for point-in-time recovery. The latest release when this was written is v0.5.17, 2026-08-31; check again before using it. Aether doesn't ship it: nightly backups plus the weekly drill are the supported path. To add it:

- **Sidecar.** Add a `litestream` service on the same `aether-data` volume, read-write, because Litestream manages its own `-litestream` metadata and checkpoints. Pin the image by version and digest. Run `litestream replicate -config /etc/litestream.yml` with the replica URL and credentials from `.env` (`LITESTREAM_ACCESS_KEY_ID` / `LITESTREAM_SECRET_ACCESS_KEY`, passed only to that service).
- **Checkpoints.** Litestream takes over checkpointing. Disable our nightly `PRAGMA wal_checkpoint(TRUNCATE)` in `jobs.nightly_maintenance`; it would contend with Litestream's read lock. Consider `PRAGMA wal_autocheckpoint=0` on the worker connection, as Litestream's tips recommend under write load. Our `busy_timeout=5000` already matches its advice.
- **Restore** with `litestream restore -o /data/aether.db <replica-url>`, stack stopped. If you recreate the DB, delete the `.aether.db-litestream` directory.
- Litestream is a third container with network egress and cloud credentials. Weigh that against the nightly backups before enabling it.

## Kubernetes (not built)

Docker on the Mac is the supported deploy, and K8s manifests were skipped (owner decision, 2026-10-08). If you add them later, keep these constraints:

- **One pod** with two containers, `worker` and `app`, sharing one **ReadWriteOnce PVC on local storage**. Never use NFS, because SQLite locking breaks.
- `replicas: 1` and `strategy: Recreate`, so two writers never overlap during a rollout.
- Each container gets its own env from a Secret: worker keys only in `worker`, dashboard secrets only in `app`. Run as non-root (uid 10001) with `readOnlyRootFilesystem`, an `emptyDir` at `/tmp` and all capabilities dropped.
- A NetworkPolicy:
  - **Ingress:** only to the app port, only from the LAN CIDR.
  - **Egress:** DNS plus 443. Vanilla NetworkPolicy can't allow-list FQDNs; use Cilium/Calico policies to pin hosts such as `api.anthropic.com`, `*.sec.gov`, `api.telegram.org` and Yahoo.
- Liveness and readiness use `python -m aether.healthcheck worker|app`.

## Incident checklist

1. **Ops page:** failing jobs (>24h), LLM spend against the budget, escalations used today, the last restore drill.
2. **Alerts page:** whether Telegram delivery is blocked, and why.
3. **Logs:** `docker compose logs --no-log-prefix worker | jq -c 'select(.level=="ERROR")'`.
4. **Data source down** (yfinance, SEC, an RSS feed): the job fails, the page shows "stale since…", and nothing is guessed. Wait or fix the provider. Don't hand-edit the DB.
5. **Budget hit:** LLM calls stop until SGT midnight. Raise `DAILY_LLM_BUDGET_USD` only deliberately, and keep the Console limit as the hard cap.
6. **DB trouble:**
   1. `make backup` if the DB still opens.
   2. `make restore-drill` to confirm the newest backup is good.
   3. `make restore BACKUP=…`.
