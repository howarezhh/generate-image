#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

source scripts/common.sh
load_env_file
PYTHON_BIN="$(python_bin || true)"

if [ -z "$PYTHON_BIN" ]; then
  echo "Python virtualenv not found. Run: bash scripts/install_ubuntu.sh"
  exit 1
fi

echo "Applying SQLite schema migrations without overwriting business data"
"$PYTHON_BIN" - <<'PY'
from backend.app import database as db
from backend.app.config import DATABASE_PATH, ensure_dirs

ensure_dirs()
db.init_db()
print(f"SQLite schema is up to date: {DATABASE_PATH}")
PY
