#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

source scripts/common.sh
load_env_file
PORT="$(resolve_port)"

if [ ! -d backend/.venv ]; then
  echo "Python virtualenv not found. Run: bash scripts/install_ubuntu.sh"
  exit 1
fi

if is_port_in_use "$PORT"; then
  echo "Port ${PORT} is already in use."
  echo "Edit .env and set another PORT, for example: PORT=8020"
  exit 1
fi

mkdir -p storage/uploads storage/outputs logs
exec backend/.venv/bin/python backend/run.py
