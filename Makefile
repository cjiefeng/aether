# Everything runs in Docker; nothing is installed on the host.
SHELL := /bin/sh
DC := docker compose
# -T: no TTY in CI (GitHub Actions sets CI=true).
DEV := $(DC) run --rm $(if $(CI),-T,) dev
UV := uv run --frozen
GITLEAKS_IMAGE := ghcr.io/gitleaks/gitleaks:v8.30.1
GITLEAKS := docker run --rm -v "$(CURDIR):/repo" -w /repo $(GITLEAKS_IMAGE)
PIP_AUDIT := pip-audit==2.9.0

.PHONY: help down logs ps dev-image lock test lint fmt typecheck eval migrate backup \
        secrets-scan smoke facts hooks record-cassette hash-password record-options \
        golden-candidates record-short-interest init restore restore-drill

help:
	@grep -E '^[a-z-]+:' Makefile | cut -d: -f1 | sort | xargs

## --- stack (deploy/start with ./deploy.sh) --------------------------------
down:
	$(DC) down

logs:
	$(DC) logs -f --tail=200

ps:
	$(DC) ps

migrate:
	$(DC) run --rm worker python -m aether.db.migrate

backup:
	$(DC) exec worker python -m aether.ops.backup

# M11: restore the newest backup into a temp dir, check it and render the dashboard from it
# (read-only for the live DB; also runs weekly in the worker).
restore-drill:
	$(DC) exec worker python -m aether.ops.restore drill

# M11: replace the live DB with a backup: make restore BACKUP=aether-YYYYMMDD.db
# Stops the stack first and keeps the current DB as aether.db.pre-restore-<stamp>.
restore:
	@test -n "$(BACKUP)" || { echo "usage: make restore BACKUP=aether-YYYYMMDD.db"; exit 2; }
	$(DC) stop app worker
	$(DC) run --rm --no-deps -T worker python -m aether.ops.restore restore "$(BACKUP)" --stack-stopped
	@echo "Restored. Start the stack again with ./deploy.sh"

smoke:
	curl -fsS http://localhost:8080/healthz && echo

# M11: first run on a fresh clone: writes .env (0600) with the password hash and secrets.
init:
	$(DC) run --rm $(if $(CI),-T,) -e AETHER_INIT_PASSWORD dev $(UV) python scripts/init_env.py

## --- dev tooling (dev container) -----------------------------------------
dev-image:
	$(DC) build dev

lock:
	$(DEV) uv lock

test:
	$(DEV) $(UV) pytest

fmt:
	$(DEV) sh -c '$(UV) ruff format . && $(UV) ruff check --fix .'

typecheck:
	$(DEV) $(UV) mypy

lint:
	$(DEV) sh -c 'set -e; \
	  $(UV) ruff check .; \
	  $(UV) ruff format --check .; \
	  $(UV) mypy; \
	  $(UV) python scripts/check_no_safe.py src; \
	  $(UV) python scripts/check_broker_readonly.py src; \
	  $(UV) python scripts/check_llm_imports.py src; \
	  uv export --frozen --no-emit-project --format requirements-txt -o /tmp/req.txt >/dev/null; \
	  uvx $(PIP_AUDIT) --strict --require-hashes --disable-pip -r /tmp/req.txt'

# Live classifier eval (spec §5.3; costs real money, never part of `make test`). The key is read
# from .env inside the dev container (it never appears on the host command line).
eval:
	$(DEV) sh -c 'export ANTHROPIC_API_KEY="$$(sed -n "s/^ANTHROPIC_API_KEY=//p" .env 2>/dev/null)"; \
	  export CLASSIFIER_MODEL="$$(sed -n "s/^CLASSIFIER_MODEL=//p" .env 2>/dev/null)"; \
	  $(UV) python -m aether.classify.eval $(EVAL_ARGS)'

# Export real ingested events from a DB as golden-set candidates (read-only):
# make golden-candidates DB=data/scratch.db
golden-candidates:
	$(DEV) $(UV) python scripts/golden_candidates.py "$(DB)"

facts:
	$(DEV) $(UV) python -m aether.facts render

# Prompts for the dashboard password; prints AETHER_DASHBOARD_PASSWORD_HASH + AETHER_SESSION_SECRET.
hash-password:
	$(DC) run --rm dev $(UV) python -m aether.security.auth hash

# Record one real option chain for tests (network): make record-options SYMBOL=IONQ
record-options:
	$(DEV) $(UV) python scripts/record_options_fixture.py "$(SYMBOL)"

# Record real FINRA short-interest files, filtered to the universe (network):
# make record-short-interest DATES="2026-08-29 2026-09-15"
record-short-interest:
	$(DEV) $(UV) python scripts/record_short_interest_fixture.py $(DATES)

record-cassette:
	$(DEV) $(UV) python scripts/record_cassette.py "$(NAME)" "$(URL)" --user-agent "$(or $(UA),aether-cassette-recorder)" $(if $(GZIP),--gzip,)

## --- security ------------------------------------------------------------
secrets-scan:
	$(GITLEAKS) git --redact --no-banner -v /repo
	$(GITLEAKS) dir --redact --no-banner -v /repo

hooks:
	git config core.hooksPath .githooks
