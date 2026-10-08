# CLAUDE.md: working agreement for Aether

Start every session by reading this file, `AETHER_BUILD_PROMPT.md` (the spec), `FACTS.md` and `MILESTONE_REPORT.md`. Work **one milestone per session**: plan in plan mode → build → stop for review.

**Portfolio reviews and portfolio features:** also read `STRATEGY.md` (the owner's **Quantum Thesis**) and follow it in every portfolio review. It's owner guidance. It's **never** copied into LLM prompts or config: Aether applies it only as the deterministic checks in spec §6.10.

## Hard rules
- **Nothing is installed on the owner's Mac.** All tooling runs in Docker through `make` (`make test`, `make lint`, `make fmt`, `make lock`, `make secrets-scan`).
- **Single writer.** Only the `worker` writes SQLite (`make_rw_engine`, `write_tx`). The dashboard uses `make_ro_engine` (`mode=ro`). Its one exception is `db/commands.py::enqueue_command` on `make_command_engine`, where an SQLite authorizer allows only `INSERT INTO commands`. Never use `make_rw_engine` from `aether/web`.
- **Never hold a write transaction across network or LLM calls.** Fetch first, then write in one short batched `write_tx`.
- **Tests never touch the network** (pytest-socket). Record HTTP once with `make record-cassette` and replay it via `tests/cassettes.py`.
- **Never fabricate data:** no invented headlines, URLs, figures, dates or tickers. Fixtures use clearly synthetic names (`ACME`, `example.test`). Unverifiable claims go into `config/facts.yaml` as `unverified` and are listed under "Open questions".
- **No opinions in config or prompts.** YAML models forbid unknown keys. Prompts get facts (with status), evidence and computed metrics only. Facts that aren't `signed_off` go through `facts.render_for_prompt` with the UNCONFIRMED label.
- **S1:** all ingested text is untrusted. Wrap it with `security/untrusted.py::wrap_untrusted`. Classification and synthesis calls get **no tools**.
- **S5:** Jinja autoescape stays on. `|safe` and `Markup(` are banned outside `security/sanitize.py` (`scripts/check_no_safe.py` runs in `make lint`). Render rich text with the `md` filter and links with `extlink`.
- No inline `<script>`, `<style>` or `style=` attributes, because the CSP forbids them. Static assets are vendored in `web/static` (see `VENDORED.txt`).
- Ask before adding a paid data source or a dependency outside the spec's stack.

## SQLite / schema conventions
- Every table is `sqlite_strict=True`. Column types are only `Integer`, `REAL`, `Text`, `LargeBinary` and `Micros`. `String`/`Boolean`/`Float`/`DateTime` break STRICT.
- Enums are `CheckConstraint(... IN (...))`. JSON columns get `CHECK (json_valid(col))`. Timestamps use `db.types.utcnow_iso()` (`YYYY-MM-DDTHH:MM:SSZ`).
- Money is `Micros` (INTEGER ↔ `Decimal`). Never use floats for money. simhash goes through `u64_to_i64` / `i64_to_u64`.
- Upserts go through `db.dialect.upsert` only. Dialect-specific SQL lives in `db/dialect.py`.
- Migrations are **hand-written** in `src/aether/db/migrations/versions/` (render_as_batch). Update `db/models.py` alongside them: `tests/test_strict_schema.py` checks models == migrations and that every table is STRICT. Batch-mode table rebuilds must keep `sqlite_strict=True` (pass it via `table_kwargs`).
- Each milestone adds its own tables in a new migration. The §7 outline in the spec is the target.

## Git workflow
- **Never push to `main` directly.** Every change goes on a branch (e.g. `m1/market-data`, `fix/…`) and lands through a pull request.
- After opening the PR, **wait for the GitHub Actions run** (`.github/workflows/ci.yml`: test, lint, secrets-scan, runtime image) to finish. Fix any failures on the branch before calling the work done.
- Don't merge the PR yourself unless the owner asks; the owner reviews and merges. `./deploy.sh` then deploys `main`.
- CI runs the same Docker-wrapped make targets as local (`make test`, `make lint`, `make secrets-scan`). Keep them CI-safe: no TTY, no host tools beyond Docker and make.

## Milestone close-out
Tests green, `make lint` clean, `make secrets-scan` clean, README updated, a MILESTONE_REPORT.md entry (built / decisions / open questions / facts / owner checklist), then commit on a branch, open a PR, wait for CI to pass, and stop for review.

## Deploy
`./deploy.sh` (not a make target) fast-forwards to `origin/main` and runs `docker compose up -d --build --wait`. There's no IP allow-list (removed by the owner); the dashboard relies on the LAN boundary + CSRF.
