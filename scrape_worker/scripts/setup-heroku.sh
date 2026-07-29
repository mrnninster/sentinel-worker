#!/usr/bin/env bash
# Configure a Heroku app for GitHub deploy (classic buildpacks — NOT Docker).
#
# Prerequisites:
#   - heroku CLI logged in (heroku login)
#   - This directory is the GitHub repo root connected to the Heroku app
#
# Usage:
#   ./scripts/setup-heroku.sh <app-name> [team-or-omit]
#
# Examples:
#   ./scripts/setup-heroku.sh sentinel-scrape-worker-1
#   ./scripts/setup-heroku.sh sentinel-scrape-worker-1 my-team
#
# After this script:
#   1. Heroku Dashboard → app → Deploy → Connect to GitHub → enable auto-deploy
#   2. Set remaining config: heroku config:set COORDINATOR_URL=… WORKER_SHARED_TOKEN=…
#   3. Deploy from the Deploy tab (or push to the connected branch)

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

APP="${1:-}"
TEAM="${2:-}"

if [[ -z "$APP" ]]; then
  echo "Usage: $0 <app-name> [team]" >&2
  exit 1
fi

if ! command -v heroku >/dev/null 2>&1; then
  echo "heroku CLI not found. Install: https://devcenter.heroku.com/articles/heroku-cli" >&2
  exit 1
fi

echo "==> App: $APP"
echo "==> Root: $ROOT (must be the GitHub repo root for Deploy from GitHub)"

if heroku apps:info -a "$APP" >/dev/null 2>&1; then
  echo "==> App already exists"
else
  echo "==> Creating app (heroku-24, buildpack stack — not container)"
  if [[ -n "$TEAM" ]]; then
    heroku create "$APP" --team "$TEAM" --stack heroku-24
  else
    heroku create "$APP" --stack heroku-24
  fi
fi

# Ensure we are NOT on the container stack (that would use heroku.yml/Docker).
CURRENT_STACK="$(heroku stack -a "$APP" 2>/dev/null | awk '/^\*/ {print $2}')"
if [[ "$CURRENT_STACK" == "container" ]]; then
  echo "==> Switching stack from container → heroku-24 (disables Docker deploys)"
  heroku stack:set heroku-24 -a "$APP"
fi

echo "==> Buildpacks: apt (Playwright libs) + python"
heroku buildpacks:clear -a "$APP" >/dev/null 2>&1 || true
heroku buildpacks:add --index 1 https://github.com/heroku/heroku-buildpack-apt -a "$APP"
heroku buildpacks:add --index 2 heroku/python -a "$APP"

PUBLIC_URL="https://${APP}.herokuapp.com"

echo "==> Setting default config vars (secrets left for you to set)"
heroku config:set -a "$APP" \
  WORKER_ID="${WORKER_ID:-scrape-worker-1}" \
  WORKER_CAPACITY="${WORKER_CAPACITY:-50}" \
  HEROKU_ONEOFF_LIMIT="${HEROKU_ONEOFF_LIMIT:-50}" \
  RESERVE_SCRAPE="${RESERVE_SCRAPE:-1}" \
  RESERVE_TRANSCRIPT="${RESERVE_TRANSCRIPT:-1}" \
  DISPATCH_MODE="${DISPATCH_MODE:-auto}" \
  HEROKU_APP_NAME="${HEROKU_APP_NAME:-$APP}" \
  HEROKU_DYNO_SIZE="${HEROKU_DYNO_SIZE:-standard-1x}" \
  WORKER_ALLOWED_SOURCES="${WORKER_ALLOWED_SOURCES:-*}" \
  WORKER_REGISTRATION_OPEN="${WORKER_REGISTRATION_OPEN:-1}" \
  WORKER_HEARTBEAT_SECONDS="${WORKER_HEARTBEAT_SECONDS:-30}" \
  WORKER_PUBLIC_URL="${WORKER_PUBLIC_URL:-$PUBLIC_URL}" \
  SCRAPER_MODE=embedded \
  SCRAPER_ROOT=/app \
  PLAYWRIGHT_BROWSERS_PATH=/app/.playwright \
  YOUTUBE_MAX_LIVE_AGE_HOURS="${YOUTUBE_MAX_LIVE_AGE_HOURS:-24}" \
  LOG_LEVEL="${LOG_LEVEL:-info}" \
  PYTHONUNBUFFERED=1

echo
echo "==> Required secrets (set before or after first deploy):"
echo "    heroku config:set -a $APP WORKER_SHARED_TOKEN=… COORDINATOR_URL=https://…"
echo "    heroku config:set -a $APP HEROKU_API_KEY=…   # Platform API token to spawn one-offs"
echo "    heroku config:set -a $APP OPENAI_API_KEY=…   # optional, LLM mode only"
echo
echo "==> Capacity: HEROKU_ONEOFF_LIMIT=50 (web = middleman; jobs = one-off dynos)"
echo "==> Dyno size: Playwright needs RAM. Prefer standard-1x or larger (not eco/basic if OOMing)."
echo "    heroku ps:type standard-1x -a $APP"
echo
echo "==> Next: Deploy from GitHub"
echo "    1. Open https://dashboard.heroku.com/apps/${APP}/deploy/github"
echo "    2. Connect this repo (root = scrape_worker with Procfile / requirements.txt)"
echo "    3. Enable automatic deploys and click Deploy Branch"
echo
echo "Done. Health check after deploy: curl -sS ${PUBLIC_URL}/health"
