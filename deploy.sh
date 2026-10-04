#!/usr/bin/env bash
# Deploy Aether: fast-forward this checkout to the latest origin/main, then rebuild and
# (re)start the stack with docker compose, waiting until both services are healthy.
#
#   ./deploy.sh
set -euo pipefail

BRANCH="main"
HEALTH_URL="http://localhost:8080/healthz"

cd "$(dirname "$0")"

die() { echo "deploy: $*" >&2; exit 1; }

command -v docker >/dev/null || die "docker not found"
docker compose version >/dev/null 2>&1 || die "docker compose v2 not available"

# S4: .env (if present) must be owner-only.
if [ -f .env ]; then
  perms="$(ls -l .env | cut -c2-10)"
  [ "$perms" = "rw-------" ] || die ".env must be mode 0600 (is $perms). Run: chmod 600 .env"
fi

# Never deploy over local edits to tracked files.
if ! git diff --quiet || ! git diff --cached --quiet; then
  die "uncommitted changes in tracked files; commit or stash them first"
fi

echo "deploy: fetching origin/$BRANCH"
git fetch --quiet origin "$BRANCH"
current="$(git rev-parse --abbrev-ref HEAD)"
if [ "$current" != "$BRANCH" ]; then
  git checkout --quiet "$BRANCH"
fi
git merge --ff-only --quiet "origin/$BRANCH" || die "local $BRANCH has diverged from origin/$BRANCH"
commit="$(git rev-parse --short HEAD)"
echo "deploy: at $commit ($(git log -1 --format=%s))"

echo "deploy: building and starting containers"
docker compose up -d --build --remove-orphans --wait --wait-timeout 180

if command -v curl >/dev/null; then
  curl -fsS "$HEALTH_URL" >/dev/null || die "health check failed: $HEALTH_URL"
fi
echo "deploy: $commit is up and healthy -> $HEALTH_URL"
