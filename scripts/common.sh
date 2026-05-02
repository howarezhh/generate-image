#!/usr/bin/env bash

load_env_file() {
  if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
  fi
}

resolve_port() {
  echo "${PORT:-8010}"
}

python_bin() {
  if [ -x backend/.venv/bin/python ]; then
    echo "backend/.venv/bin/python"
  elif [ -x backend/.venv/Scripts/python.exe ]; then
    echo "backend/.venv/Scripts/python.exe"
  else
    return 1
  fi
}

is_port_in_use() {
  local port="$1"
  if command -v ss >/dev/null 2>&1; then
    ss -ltn | awk '{print $4}' | grep -Eq "[:.]${port}$"
  elif command -v lsof >/dev/null 2>&1; then
    lsof -iTCP:"${port}" -sTCP:LISTEN >/dev/null 2>&1
  else
    return 1
  fi
}
