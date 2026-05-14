#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

source scripts/common.sh
load_env_file
PORT="$(resolve_port)"
URL="http://127.0.0.1:${PORT}"
PYTHON_BIN="$(python_bin || true)"
ACCESS_CHECK_PASSWORD="${ACCESS_SELF_CHECK_PASSWORD:-hhs54666}"
COOKIE_JAR="/tmp/gpt-image-studio-self-check-cookie.txt"

echo "[1/5] Checking files"
test -f .env || { echo ".env not found"; exit 1; }
[ -n "$PYTHON_BIN" ] || { echo "backend/.venv Python not found"; exit 1; }
test -f frontend/dist/index.html || { echo "frontend/dist/index.html not found"; exit 1; }

echo "[2/5] Checking Python imports"
"$PYTHON_BIN" -m compileall -q backend/app backend/run.py
"$PYTHON_BIN" - <<'PY'
import openai
import httpx

print(f"openai {openai.__version__}")
print(f"httpx {httpx.__version__}")
PY

echo "[3/5] Checking service process"
rm -f "$COOKIE_JAR"
if ! curl -fsS -c "$COOKIE_JAR" \
  -d "password=${ACCESS_CHECK_PASSWORD}" \
  -d "next=/" \
  "${URL}/auth/login" >/tmp/gpt-image-studio-login.html; then
  echo "Login check failed. Verify ACCESS_SELF_CHECK_PASSWORD or project access password settings."
  exit 1
fi
if ! curl -fsS -b "$COOKIE_JAR" "${URL}/api/health" >/tmp/gpt-image-studio-health.json; then
  echo "Service is not reachable. Start it first: bash scripts/start_background.sh"
  exit 1
fi
cat /tmp/gpt-image-studio-health.json
echo

echo "[4/5] Checking frontend"
curl -fsS -b "$COOKIE_JAR" "${URL}/" >/tmp/gpt-image-studio-index.html
grep -q "GPT Image Studio" /tmp/gpt-image-studio-index.html

echo "[5/5] Checking settings APIs"
curl -fsS -b "$COOKIE_JAR" "${URL}/api/settings" >/tmp/gpt-image-studio-settings.json
"$PYTHON_BIN" - <<'PY'
import json

with open("/tmp/gpt-image-studio-settings.json", "r", encoding="utf-8") as handle:
    settings = json.load(handle)
print(json.dumps(
    {
        "base_url": settings.get("base_url"),
        "api_key_configured": bool(settings.get("api_key")),
    },
    ensure_ascii=False,
))
PY
curl -fsS -b "$COOKIE_JAR" "${URL}/api/providers" >/tmp/gpt-image-studio-providers.json
grep -q '"items"' /tmp/gpt-image-studio-providers.json
curl -fsS -b "$COOKIE_JAR" "${URL}/api/app-settings" >/tmp/gpt-image-studio-app-settings.json
grep -q '"value"' /tmp/gpt-image-studio-app-settings.json

echo "Self check passed."
