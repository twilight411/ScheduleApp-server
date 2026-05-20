#!/usr/bin/env bash
set -euo pipefail

: "${REPO_URL:?REPO_URL is required}"
BRANCH="${BRANCH:-main}"
APP_DIR="${APP_DIR:-/app/spirit-scheduler}"
OLD_RUNTIME="${OLD_RUNTIME:-/app/spirit-scheduler-pre-git-20260516-161024}"
SERVICE_NAME="${SERVICE_NAME:-spirit-scheduler}"
TMP_CLONE="/tmp/spirit-scheduler-clone-$$"

if [[ -d "${APP_DIR}/.git" ]]; then
  echo "Already git-managed at ${APP_DIR}; run deploy_backend.sh instead."
  exit 0
fi

echo "[1/8] clone ${REPO_URL}#${BRANCH}"
git clone -b "${BRANCH}" "${REPO_URL}" "${TMP_CLONE}"

RUNTIME_SRC="${OLD_RUNTIME}"
if [[ ! -d "${RUNTIME_SRC}" ]]; then
  RUNTIME_SRC="${APP_DIR}"
fi

echo "[2/8] preserve runtime from ${RUNTIME_SRC}"
for f in .env spirit.db; do
  if [[ -f "${RUNTIME_SRC}/${f}" ]]; then
    cp -a "${RUNTIME_SRC}/${f}" "${TMP_CLONE}/${f}"
  fi
done
if [[ -d "${RUNTIME_SRC}/web_build" ]]; then
  rm -rf "${TMP_CLONE}/web_build"
  cp -a "${RUNTIME_SRC}/web_build" "${TMP_CLONE}/web_build"
fi

echo "[3/8] install to ${APP_DIR}"
if [[ -d "${APP_DIR}" && ! -d "${APP_DIR}/.git" ]]; then
  mv "${APP_DIR}" "${APP_DIR}.pre-git-bak.$(date +%s)"
fi
mkdir -p "$(dirname "${APP_DIR}")"
mv "${TMP_CLONE}" "${APP_DIR}"

echo "[4/8] patch systemd WorkingDirectory -> ${APP_DIR}"
UNIT="/etc/systemd/system/${SERVICE_NAME}.service"
if grep -q "WorkingDirectory=" "${UNIT}"; then
  sed -i "s#WorkingDirectory=.*#WorkingDirectory=${APP_DIR}#" "${UNIT}"
else
  sed -i "/\[Service\]/a WorkingDirectory=${APP_DIR}" "${UNIT}"
fi
systemctl daemon-reload

echo "[5/8] restart ${SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}"

echo "[6/8] status"
systemctl --no-pager --full status "${SERVICE_NAME}" | head -n 15

echo "[7/8] health"
sleep 2
curl -sf "http://127.0.0.1:8000/docs" >/dev/null && echo "OK /docs" || echo "WARN: /docs not reachable"

echo "[8/8] done. Old runtime kept at ${OLD_RUNTIME}"
