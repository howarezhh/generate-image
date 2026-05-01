#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

BASE_URL="${IMAGE_API_BASE_URL:-https://api.xiaoxin.best/}"
API_KEY="${IMAGE_API_KEY:-}"
PORT_VALUE="${PORT:-8010}"

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
ENV

chmod 600 .env
echo ".env configured at ${ROOT_DIR}/.env"
echo "Base URL: ${BASE_URL}"
echo "Port: ${PORT_VALUE}"

