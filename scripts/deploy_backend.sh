#!/usr/bin/env bash
set -euo pipefail
BRANCH="${1:-main}"
APP_DIR="${APP_DIR:-/app/spirit-scheduler}"
SERVICE_NAME="${SERVICE_NAME:-spirit-scheduler}"
cd "${APP_DIR}"
git fetch origin
git checkout "${BRANCH}"
git reset --hard "origin/${BRANCH}"
systemctl restart "${SERVICE_NAME}"
systemctl --no-pager --full status "${SERVICE_NAME}" | head -n 15
echo "Deploy ${BRANCH} done."
