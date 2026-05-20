#!/usr/bin/env bash
set -eu
BRANCH="${1:-main}"
APP_DIR="${APP_DIR:-/app/spirit-scheduler}"
SERVICE_NAME="${SERVICE_NAME:-spirit-scheduler}"
cd "${APP_DIR}"
export http_proxy="${http_proxy:-http://127.0.0.1:7897}"
export https_proxy="${https_proxy:-$http_proxy}"
git fetch origin
git checkout "${BRANCH}"
git reset --hard "origin/${BRANCH}"
systemctl restart "${SERVICE_NAME}"
systemctl --no-pager --full status "${SERVICE_NAME}" | head -n 15
echo "Deploy ${BRANCH} done."
