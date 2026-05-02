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
  if command -v pgrep >/dev/null 2>&1; then
    CHILDREN="$(pgrep -P "$PID" || true)"
  else
    CHILDREN=""
  fi
  kill "$PID"
  echo "Stopped PID $PID"
  if [ -n "$CHILDREN" ]; then
    for child in $CHILDREN; do
      if kill -0 "$child" 2>/dev/null; then
        kill "$child" 2>/dev/null || true
        echo "Stopped child PID $child"
      fi
    done
  fi
else
  echo "Process $PID is not running."
fi
rm -f logs/app.pid
