#!/usr/bin/env bash
set -euo pipefail

# 4gproxy-net.sh — policy routing ONLY for proxy traffic
# - Does NOT change system default route
# - Marks packets from user "proxyuser"
# - Routes MARKed packets via cellular iface (table 100)
# - NATs only when exiting the cellular iface

echo "[4gproxy-net] starting…"

# 1) detect cellular interface
echo "[4gproxy-net] Available interfaces:"
ip -o link show | awk -F': ' '{print $2}' | grep -v lo

# Try multiple patterns for cellular interfaces (EXCLUDING eth0/wlan0)
CELL_IFACE=""

# Check for ppp0 first (PPP connection) - HIGHEST PRIORITY
echo "[4gproxy-net] Checking for ppp0 (PPP connection)..."
if ip -4 addr show ppp0 2>/dev/null | grep -q "inet "; then
  CELL_IFACE="ppp0"
  echo "[4gproxy-net] ✅ Found active cellular interface: ppp0 (PPP) - USING THIS"
else
  echo "[4gproxy-net] ❌ ppp0 not found or no IPv4, checking other patterns..."
  # Fallback to other patterns
  for pattern in 'wwan' 'ppp' 'usb' 'eth1' 'eth2' 'eth3' 'enx' 'cdc'; do
    CELL_IFACE=$(ip -o link show | awk -F': ' '{print $2}' | grep -E "^${pattern}" | head -n1 || true)
    if [[ -n "${CELL_IFACE:-}" ]]; then
      # CRITICAL: Never use eth0 or wlan0 (home network interfaces)
      if [[ "${CELL_IFACE}" == "eth0" || "${CELL_IFACE}" == "wlan0" ]]; then
        echo "[4gproxy-net] Skipping ${CELL_IFACE} (home network interface)"
        CELL_IFACE=""
        continue
      fi
      # For direct modem mode, we might not have an IP on the interface
      # Check if interface exists and is up
      if ip link show "${CELL_IFACE}" | grep -q "state UP\|state UNKNOWN"; then
        echo "[4gproxy-net] Found cellular interface: ${CELL_IFACE} (pattern: ${pattern})"
        break
      else
        echo "[4gproxy-net] Found interface ${CELL_IFACE} but not up, trying next..."
        CELL_IFACE=""
      fi
    fi
  done
fi

if [[ -z "${CELL_IFACE:-}" ]]; then
  echo "[4gproxy-net] ERROR: no active cellular interface found."
  echo "[4gproxy-net] Tried patterns: wwan, ppp, usb, eth1, eth2, eth3, enx, cdc"
  echo "[4gproxy-net] Available interfaces with IPs:"
  ip -o addr show | grep "inet " | grep -v "127.0.0.1" | awk '{print $2}' | cut -d: -f1
  exit 1
fi
echo "[4gproxy-net] Using cellular interface: ${CELL_IFACE}"

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
