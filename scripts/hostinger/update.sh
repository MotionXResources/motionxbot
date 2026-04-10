#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SERVICE_NAME="${SERVICE_NAME:-motionxbot}"

if [[ ! -d "${APP_DIR}/.git" ]]; then
  echo "Git repository not found in ${APP_DIR}" >&2
  exit 1
fi

cd "${APP_DIR}"
git pull --ff-only
source "${APP_DIR}/.venv/bin/activate"
python -m pip install -r "${APP_DIR}/requirements.txt"
systemctl restart "${SERVICE_NAME}"
systemctl status "${SERVICE_NAME}" --no-pager
