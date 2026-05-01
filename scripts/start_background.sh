#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

source scripts/common.sh
load_env_file
PORT="$(resolve_port)"

mkdir -p logs
if [ -f logs/app.pid ] && kill -0 "$(cat logs/app.pid)" 2>/dev/null; then
  echo "Already running with PID $(cat logs/app.pid)"
  exit 0
fi

if is_port_in_use "$PORT"; then
  echo "Port ${PORT} is already in use."
  echo "Edit .env and set another PORT, for example: PORT=8020"
  exit 1
fi

nohup bash scripts/start.sh > logs/app.log 2>&1 &
echo $! > logs/app.pid
echo "Started with PID $(cat logs/app.pid)"
echo "Log: $ROOT_DIR/logs/app.log"
