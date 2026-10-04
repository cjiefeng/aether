# Everything runs in Docker; nothing is installed on the host.
SHELL := /bin/sh
DC := docker compose
DEV := $(DC) run --rm dev
UV := uv run --frozen
GITLEAKS_IMAGE := ghcr.io/gitleaks/gitleaks:v8.30.1
GITLEAKS := docker run --rm -v "$(CURDIR):/repo" -w /repo $(GITLEAKS_IMAGE)
PIP_AUDIT := pip-audit==2.9.0

.PHONY: help up down logs ps build dev-image lock test lint fmt typecheck eval migrate backup \
        secrets-scan smoke facts check-env hooks record-cassette

help:
	@grep -E '^[a-z-]+:' Makefile | cut -d: -f1 | sort | xargs

## --- stack ---------------------------------------------------------------
check-env:
	@if [ -f .env ]; then \
	  perms="$$(ls -l .env | cut -c2-10)"; \
	  if [ "$$perms" != "rw-------" ]; then \
	    echo "ERROR: .env must be mode 0600 (is $$perms). Run: chmod 600 .env" >&2; exit 1; \
	  fi; \
	fi

build:
	$(DC) build worker

up: check-env
	$(DC) up -d --build

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

smoke:
	curl -fsS http://localhost:8080/healthz && echo

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
	  uv export --frozen --no-emit-project --format requirements-txt -o /tmp/req.txt >/dev/null; \
	  uvx $(PIP_AUDIT) --strict --require-hashes --disable-pip -r /tmp/req.txt'

eval:
	@echo "No evals until M5 (classifier). Nothing to run."

facts:
	$(DEV) $(UV) python -m aether.facts render

record-cassette:
	$(DEV) $(UV) python scripts/record_cassette.py "$(NAME)" "$(URL)" --user-agent "$(or $(UA),aether-cassette-recorder)"

## --- security ------------------------------------------------------------
secrets-scan:
	$(GITLEAKS) git --redact --no-banner -v /repo
	$(GITLEAKS) dir --redact --no-banner -v /repo

hooks:
	git config core.hooksPath .githooks
