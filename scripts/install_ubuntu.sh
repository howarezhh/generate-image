#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [ "$(id -u)" -eq 0 ]; then
  SUDO=""
else
  SUDO="sudo"
fi

echo "[1/5] Checking system packages"
$SUDO apt-get update
$SUDO apt-get install -y python3 python3-venv python3-pip curl ca-certificates

NEED_NODE=0
if ! command -v node >/dev/null 2>&1; then
  NEED_NODE=1
else
  NODE_MAJOR="$(node -v | sed 's/v//' | cut -d. -f1)"
  if [ "$NODE_MAJOR" -lt 20 ]; then
    NEED_NODE=1
  fi
fi

if [ "$NEED_NODE" -eq 1 ]; then
  echo "[2/5] Installing Node.js 22"
  if [ -z "$SUDO" ]; then
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
  else
    curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
  fi
  $SUDO apt-get install -y nodejs
else
  echo "[2/5] Node.js already installed: $(node -v)"
fi

echo "[3/5] Installing Python dependencies"
python3 -m venv backend/.venv
backend/.venv/bin/pip install --upgrade pip
backend/.venv/bin/pip install -r backend/requirements.txt

echo "[4/5] Installing frontend dependencies"
cd frontend
npm install
npm run build
cd "$ROOT_DIR"

echo "[5/5] Preparing local config"
mkdir -p storage/uploads storage/outputs logs
if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env from .env.example. Please edit it before starting."
fi

echo "Default service port is $(grep -E '^PORT=' .env | cut -d= -f2). Change PORT in .env if another project already uses it."
echo "Install complete. Start with: bash scripts/start.sh"
