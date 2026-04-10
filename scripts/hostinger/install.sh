#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SERVICE_NAME="${SERVICE_NAME:-motionxbot}"
SERVICE_PATH="/etc/systemd/system/${SERVICE_NAME}.service"

if [[ ! -f "${APP_DIR}/requirements.txt" ]]; then
  echo "requirements.txt not found in ${APP_DIR}" >&2
  exit 1
fi

if [[ ! -f "${APP_DIR}/.env" ]]; then
  echo ".env not found in ${APP_DIR}. Copy .env.example to .env first." >&2
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required." >&2
  exit 1
fi

python3 -m venv "${APP_DIR}/.venv"
source "${APP_DIR}/.venv/bin/activate"
python -m pip install --upgrade pip
python -m pip install -r "${APP_DIR}/requirements.txt"

cat > "${SERVICE_PATH}" <<EOF
[Unit]
Description=MotionXBot Discord Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${APP_DIR}
EnvironmentFile=${APP_DIR}/.env
Environment=PYTHONUNBUFFERED=1
ExecStart=${APP_DIR}/.venv/bin/python -m motionxbot
Restart=always
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}"
systemctl status "${SERVICE_NAME}" --no-pager
