#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

STATE_DIR=".install-state"
mkdir -p "$STATE_DIR"

if [ "$(id -u)" -eq 0 ]; then
  SUDO=""
else
  SUDO="sudo"
fi

file_hash() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$@" | sha256sum | awk '{print $1}'
  else
    shasum -a 256 "$@" | shasum -a 256 | awk '{print $1}'
  fi
}

has_apt_package() {
  dpkg -s "$1" >/dev/null 2>&1
}

echo "[1/5] Checking system packages"
APT_PACKAGES=(python3 python3-venv python3-pip curl ca-certificates)
MISSING_PACKAGES=()
for package in "${APT_PACKAGES[@]}"; do
  if ! has_apt_package "$package"; then
    MISSING_PACKAGES+=("$package")
  fi
done

if [ "${#MISSING_PACKAGES[@]}" -gt 0 ]; then
  echo "Installing missing packages: ${MISSING_PACKAGES[*]}"
  $SUDO apt-get update
  $SUDO apt-get install -y "${MISSING_PACKAGES[@]}"
else
  echo "System packages already installed"
fi

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

echo "[3/5] Checking Python dependencies"
REQ_HASH="$(file_hash backend/requirements.txt)"
PY_STATE_FILE="$STATE_DIR/python-requirements.sha256"
if [ ! -d backend/.venv ]; then
  echo "Creating Python virtual environment"
  python3 -m venv backend/.venv
fi

if [ ! -f "$PY_STATE_FILE" ] || [ "$(cat "$PY_STATE_FILE")" != "$REQ_HASH" ]; then
  echo "Installing Python dependencies"
  backend/.venv/bin/pip install --upgrade pip
  backend/.venv/bin/pip install -r backend/requirements.txt
  echo "$REQ_HASH" > "$PY_STATE_FILE"
else
  echo "Python dependencies already installed"
fi

echo "[4/5] Checking frontend dependencies"
FRONTEND_HASH="$(file_hash frontend/package.json frontend/package-lock.json)"
FRONTEND_STATE_FILE="$STATE_DIR/frontend-deps.sha256"
if [ ! -d frontend/node_modules ] || [ ! -f "$FRONTEND_STATE_FILE" ] || [ "$(cat "$FRONTEND_STATE_FILE")" != "$FRONTEND_HASH" ]; then
  echo "Installing frontend dependencies"
  cd frontend
  npm install
  cd "$ROOT_DIR"
  echo "$FRONTEND_HASH" > "$FRONTEND_STATE_FILE"
else
  echo "Frontend dependencies already installed"
fi

BUILD_HASH="$(file_hash frontend/package.json frontend/package-lock.json frontend/index.html frontend/src/main.jsx frontend/src/styles.css)"
BUILD_STATE_FILE="$STATE_DIR/frontend-build.sha256"
if [ ! -f frontend/dist/index.html ] || [ ! -f "$BUILD_STATE_FILE" ] || [ "$(cat "$BUILD_STATE_FILE")" != "$BUILD_HASH" ]; then
  echo "Building frontend"
  cd frontend
  npm run build
  cd "$ROOT_DIR"
  echo "$BUILD_HASH" > "$BUILD_STATE_FILE"
else
  echo "Frontend build already up to date"
fi

echo "[5/5] Preparing local config"
mkdir -p storage/uploads storage/outputs logs
if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env from .env.example. Please edit it before starting."
fi

echo "Default service port is $(grep -E '^PORT=' .env | cut -d= -f2). Change PORT in .env if another project already uses it."
echo "Install complete. Start with: bash scripts/start.sh"
