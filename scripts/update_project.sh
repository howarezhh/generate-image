#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo main)"
GIT_PULL_URL="${GIT_PULL_URL:-}"

echo "[1/5] Pulling latest code on ${CURRENT_BRANCH}"
if [ -n "$GIT_PULL_URL" ]; then
  git pull --ff-only "$GIT_PULL_URL" "$CURRENT_BRANCH"
elif git remote get-url origin >/dev/null 2>&1; then
  git fetch origin "$CURRENT_BRANCH"
  if git merge-base --is-ancestor HEAD "origin/${CURRENT_BRANCH}"; then
    git merge --ff-only "origin/${CURRENT_BRANCH}"
  else
    echo "Local branch has commits that are not on origin/${CURRENT_BRANCH}; skipping fast-forward pull."
    echo "Resolve git history manually if this server is used for development."
  fi
else
  echo "No git origin remote found; skipping pull."
fi

echo "[2/5] Installing or updating dependencies"
bash scripts/install_ubuntu.sh

echo "[3/5] Applying local configuration and database defaults"
if [ -f .env ]; then
  source scripts/common.sh
  load_env_file
  if [ -n "${IMAGE_API_KEY:-}" ]; then
    IMAGE_API_BASE_URL="${IMAGE_API_BASE_URL:-}" IMAGE_API_KEY="${IMAGE_API_KEY}" PORT="${PORT:-8010}" bash scripts/configure_env.sh
  else
    echo "IMAGE_API_KEY is empty in .env; skipping configure_env.sh"
  fi
else
  echo ".env not found; create it with scripts/configure_env.sh before starting."
fi

echo "[4/5] Restarting background service"
bash scripts/stop.sh || true
bash scripts/start_background.sh

echo "[5/5] Running self check"
sleep 2
bash scripts/self_check.sh

echo "Update complete."
