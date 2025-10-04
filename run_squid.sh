#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CFG="${SCRIPT_DIR}/squid.conf"
PROXY_USER="proxyuser"

# Check if squid is installed
if ! command -v squid >/dev/null; then
  echo "Squid not found. Installing..."
  sudo apt update
  sudo apt install -y squid
fi

# Ensure proxyuser exists
if ! id -u "${PROXY_USER}" >/dev/null 2>&1; then
  sudo useradd --system --no-create-home --shell /usr/sbin/nologin "${PROXY_USER}"
fi

# Check if config file exists
if [[ ! -f "${CFG}" ]]; then
  echo "Squid config not found at ${CFG}"
  exit 1
fi

# Create log directory
sudo mkdir -p /var/log/squid
sudo chown proxyuser:proxyuser /var/log/squid

# Create cache directory
sudo mkdir -p /var/spool/squid
sudo chown proxyuser:proxyuser /var/spool/squid

# Start squid
echo "Starting Squid with config: ${CFG}"
exec sudo -u "${PROXY_USER}" squid -N -f "${CFG}"
