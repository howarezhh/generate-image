#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [ ! -f logs/app.pid ]; then
  echo "No PID file found."
  exit 0
fi

PID="$(cat logs/app.pid)"
if kill -0 "$PID" 2>/dev/null; then
  kill "$PID"
  echo "Stopped PID $PID"
else
  echo "Process $PID is not running."
fi
rm -f logs/app.pid

