#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

BASE_URL="${IMAGE_API_BASE_URL:-https://api.xiaoxin.best/}"
API_KEY="${IMAGE_API_KEY:-}"
PORT_VALUE="${PORT:-8010}"
MAX_CONCURRENT="${MAX_CONCURRENT_TASKS:-3}"
IMAGE_TIMEOUT="${IMAGE_REQUEST_TIMEOUT_SECONDS:-300}"
IMAGE_ATTEMPTS="${IMAGE_REQUEST_MAX_ATTEMPTS:-2}"
PLANNER_TIMEOUT="${CHAT_PLANNER_TIMEOUT_SECONDS:-180}"
PLANNER_ATTEMPTS="${CHAT_PLANNER_MAX_ATTEMPTS:-2}"
STABLE_RETRY="${ENABLE_IMAGE_STABLE_RETRY:-1}"
STABLE_QUALITY="${IMAGE_STABLE_RETRY_QUALITY:-medium}"

if [ -z "$API_KEY" ]; then
  echo "IMAGE_API_KEY is required."
  echo "Usage:"
  echo "  IMAGE_API_KEY='sk-...' bash scripts/configure_env.sh"
  exit 1
fi

cat > .env <<ENV
IMAGE_API_BASE_URL=${BASE_URL}
IMAGE_API_KEY=${API_KEY}
HOST=0.0.0.0
PORT=${PORT_VALUE}
DATABASE_PATH=storage/app.db
STORAGE_DIR=storage
MAX_CONCURRENT_TASKS=${MAX_CONCURRENT}
IMAGE_REQUEST_TIMEOUT_SECONDS=${IMAGE_TIMEOUT}
IMAGE_REQUEST_MAX_ATTEMPTS=${IMAGE_ATTEMPTS}
CHAT_PLANNER_TIMEOUT_SECONDS=${PLANNER_TIMEOUT}
CHAT_PLANNER_MAX_ATTEMPTS=${PLANNER_ATTEMPTS}
ENABLE_IMAGE_STABLE_RETRY=${STABLE_RETRY}
IMAGE_STABLE_RETRY_QUALITY=${STABLE_QUALITY}
ENV

chmod 600 .env
echo ".env configured at ${ROOT_DIR}/.env"
echo "Base URL: ${BASE_URL}"
echo "Port: ${PORT_VALUE}"
echo "Max concurrent tasks: ${MAX_CONCURRENT}"

if [ -x backend/.venv/bin/python ]; then
  backend/.venv/bin/python - <<'PY'
from backend.app.config import DEFAULT_API_BASE_URL, DEFAULT_API_KEY, ensure_dirs
from backend.app import database as db

ensure_dirs()
db.init_db()
stamp = db.now_iso()
with db.connect() as conn:
    row = conn.execute("select id from providers order by id asc limit 1").fetchone()
    if row:
        conn.execute(
            "update providers set name = ?, base_url = ?, api_key = ?, updated_at = ? where id = ?",
            ("默认提供商", DEFAULT_API_BASE_URL, DEFAULT_API_KEY, stamp, row["id"]),
        )
    else:
        conn.execute(
            "insert into providers (name, base_url, api_key, created_at, updated_at) values (?, ?, ?, ?, ?)",
            ("默认提供商", DEFAULT_API_BASE_URL, DEFAULT_API_KEY, stamp, stamp),
        )
print("SQLite provider settings initialized.")
PY
else
  echo "Python virtualenv not found; provider settings will initialize on first app start."
fi
