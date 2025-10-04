#!/usr/bin/env bash
set -euo pipefail

# run_3proxy.sh — launch 3proxy as unprivileged "proxyuser"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CFG="${SCRIPT_DIR}/3proxy.cfg"
BIN="/usr/local/bin/3proxy"
PROXY_USER="proxyuser"

if ! command -v "${BIN}" >/dev/null 2>&1; then
  echo "3proxy binary not found at ${BIN}"
  exit 1
fi

if ! id -u "${PROXY_USER}" >/dev/null 2>&1; then
  echo "Creating user ${PROXY_USER}…"
  useradd --system --no-create-home --shell /usr/sbin/nologin "${PROXY_USER}"
fi

exec sudo -u "${PROXY_USER}" "${BIN}" "${CFG}"
