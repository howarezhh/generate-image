#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

source scripts/common.sh
load_env_file
PORT="$(resolve_port)"
URL="http://127.0.0.1:${PORT}"
PYTHON_BIN="$(python_bin || true)"

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
curl -fsS "${URL}/api/providers" >/tmp/gpt-image-studio-providers.json
grep -q '"items"' /tmp/gpt-image-studio-providers.json
curl -fsS "${URL}/api/app-settings" >/tmp/gpt-image-studio-app-settings.json
grep -q '"value"' /tmp/gpt-image-studio-app-settings.json

echo "Self check passed."
