#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_NAME="gpt-image-studio"

if [ "$(id -u)" -eq 0 ]; then
  SUDO=""
else
  SUDO="sudo"
fi

$SUDO tee "/etc/systemd/system/${SERVICE_NAME}.service" >/dev/null <<SERVICE
[Unit]
Description=GPT Image Studio
After=network.target

[Service]
Type=simple
WorkingDirectory=${ROOT_DIR}
ExecStart=${ROOT_DIR}/backend/.venv/bin/python ${ROOT_DIR}/backend/run.py
Restart=always
RestartSec=3
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=-${ROOT_DIR}/.env

[Install]
WantedBy=multi-user.target
SERVICE

$SUDO systemctl daemon-reload
$SUDO systemctl enable "$SERVICE_NAME"
$SUDO systemctl restart "$SERVICE_NAME"
$SUDO systemctl status "$SERVICE_NAME" --no-pager
