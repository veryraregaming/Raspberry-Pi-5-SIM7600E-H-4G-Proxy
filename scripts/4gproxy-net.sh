#!/usr/bin/env bash
set -euo pipefail

# 4gproxy-net.sh — policy routing ONLY for proxy traffic
# - Does NOT change system default route
# - Marks packets from user "proxyuser"
# - Routes MARKed packets via cellular iface (table 100)
# - NATs only when exiting the cellular iface

echo "[4gproxy-net] starting…"

# 1) detect cellular interface
CELL_IFACE=$(ip -o link show | awk -F': ' '{print $2}' | grep -E 'wwan|ppp|usb' | head -n1 || true)
if [[ -z "${CELL_IFACE:-}" ]]; then
  echo "[4gproxy-net] ERROR: no cellular interface (wwan/ppp/usb) found."
  exit 1
fi
echo "[4gproxy-net] cellular iface: ${CELL_IFACE}"

# 2) ensure proxy user exists
PROXY_USER="proxyuser"
if ! id -u "${PROXY_USER}" >/dev/null 2>&1; then
  echo "[4gproxy-net] creating user: ${PROXY_USER}"
  useradd --system --no-create-home --shell /usr/sbin/nologin "${PROXY_USER}" || true
fi

# 3) enable ipv4 forwarding
sysctl -w net.ipv4.ip_forward=1 >/dev/null

# 4) dedicated routing table
TABLE_ID=100
TABLE_NAME="proxy_table"
RT_TABLES="/etc/iproute2/rt_tables"
grep -qE "^[[:space:]]*${TABLE_ID}[[:space:]]+${TABLE_NAME}$" "${RT_TABLES}" 2>/dev/null || {
  echo "${TABLE_ID} ${TABLE_NAME}" >> "${RT_TABLES}"
}

# 5) default route in proxy_table via cellular iface (no change to main)
ip route replace default dev "${CELL_IFACE}" table "${TABLE_ID}"

# 6) ip rule for fwmark==1 -> proxy_table
ip rule add fwmark 0x1 table "${TABLE_ID}" pref 100 2>/dev/null || true

# 7) mark packets from proxy user
iptables -t mangle -D OUTPUT -m owner --uid-owner "${PROXY_USER}" -j MARK --set-mark 1 2>/dev/null || true
iptables -t mangle -A OUTPUT -m owner --uid-owner "${PROXY_USER}" -j MARK --set-mark 1

# 8) NAT only when leaving the cellular iface
iptables -t nat -C POSTROUTING -o "${CELL_IFACE}" -j MASQUERADE 2>/dev/null || \
  iptables -t nat -A POSTROUTING -o "${CELL_IFACE}" -j MASQUERADE

echo "[4gproxy-net] policy routing active: fwmark 0x1 -> table ${TABLE_ID} -> ${CELL_IFACE}"
echo "[4gproxy-net] done."
