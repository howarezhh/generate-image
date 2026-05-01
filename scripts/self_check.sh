#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

source scripts/common.sh
load_env_file
PORT="$(resolve_port)"
URL="http://127.0.0.1:${PORT}"

echo "[1/5] Checking files"
test -f .env || { echo ".env not found"; exit 1; }
test -d backend/.venv || { echo "backend/.venv not found"; exit 1; }
test -f frontend/dist/index.html || { echo "frontend/dist/index.html not found"; exit 1; }

echo "[2/5] Checking Python imports"
backend/.venv/bin/python -m compileall -q backend/app

echo "[3/5] Checking service process"
if ! curl -fsS "${URL}/api/health" >/tmp/gpt-image-studio-health.json; then
  echo "Service is not reachable. Start it first: bash scripts/start_background.sh"
  exit 1
fi
cat /tmp/gpt-image-studio-health.json
echo

echo "[4/5] Checking frontend"
curl -fsS "${URL}/" >/tmp/gpt-image-studio-index.html
grep -q "GPT Image Studio" /tmp/gpt-image-studio-index.html

echo "[5/5] Checking settings APIs"
curl -fsS "${URL}/api/settings" >/tmp/gpt-image-studio-settings.json
cat /tmp/gpt-image-studio-settings.json
echo
curl -fsS "${URL}/api/providers" >/tmp/gpt-image-studio-providers.json
grep -q '"items"' /tmp/gpt-image-studio-providers.json
curl -fsS "${URL}/api/app-settings" >/tmp/gpt-image-studio-app-settings.json
grep -q '"value"' /tmp/gpt-image-studio-app-settings.json

echo "Self check passed."
