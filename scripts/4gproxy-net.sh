#!/usr/bin/env bash
set -euo pipefail

# 4gproxy-net.sh — policy routing ONLY for proxy traffic
# - Does NOT change system default route
# - Marks packets from Squid user "proxy"
# - Routes MARKed packets via cellular iface (table 100)
# - NATs only when exiting the cellular iface

echo "[4gproxy-net] starting…"

# 1) detect cellular interface
echo "[4gproxy-net] Available interfaces:"
ip -o link show | awk -F': ' '{print $2}' | grep -v lo

CELL_IFACE=""

echo "[4gproxy-net] Checking for ppp0 (PPP connection)."
if ip -4 addr show ppp0 2>/dev/null | grep -q "inet "; then
  CELL_IFACE="ppp0"
  echo "[4gproxy-net] ✅ Found active cellular interface: ppp0 (PPP) - USING THIS"
else
  echo "[4gproxy-net] ❌ ppp0 not found or no IPv4, checking other patterns."
  for pattern in 'wwan' 'ppp' 'usb' 'eth1' 'eth2' 'eth3' 'enx' 'cdc' 'usb0'; do
    CAND=$(ip -o link show | awk -F': ' '{print $2}' | grep -E "^${pattern}" | head -n1 || true)
    if [[ -n "${CAND}" ]]; then
      if [[ "${CAND}" == "eth0" || "${CAND}" == "wlan0" ]]; then
        echo "[4gproxy-net] Skipping ${CAND} (home network interface)"
        continue
      fi
      if ip link show "${CAND}" | grep -q "state UP\|state UNKNOWN"; then
        CELL_IFACE="${CAND}"
        echo "[4gproxy-net] Found cellular interface: ${CELL_IFACE} (pattern: ${pattern})"
        break
      else
        echo "[4gproxy-net] Found interface ${CAND} but not up, trying next."
      fi
    fi
  done
fi

if [[ -z "${CELL_IFACE:-}" ]]; then
  echo "[4gproxy-net] ERROR: no active cellular interface found."
  echo "[4gproxy-net] Tried patterns: wwan, ppp, usb, eth1, eth2, eth3, enx, cdc, usb0"
  echo "[4gproxy-net] Available interfaces with IPs:"
  ip -o addr show | grep "inet " | grep -v "127.0.0.1" | awk '{print $2}' | cut -d: -f1
  exit 1
fi
echo "[4gproxy-net] Using cellular interface: ${CELL_IFACE}"

# 2) ensure ipv4 forwarding
sysctl -w net.ipv4.ip_forward=1 >/dev/null

# 3) dedicated routing table
TABLE_ID=100
TABLE_NAME="proxy_table"
RT_TABLES="/etc/iproute2/rt_tables"
grep -qE "^[[:space:]]*${TABLE_ID}[[:space:]]+${TABLE_NAME}$" "${RT_TABLES}" 2>/dev/null || {
  echo "${TABLE_ID} ${TABLE_NAME}" >> "${RT_TABLES}"
}

# 4) default route in proxy_table via cellular iface (no change to main)
if [[ "${CELL_IFACE}" == "ppp0" ]]; then
  PPP_GATEWAY=$(ip -4 addr show ppp0 | awk '/peer/ {print $4}' | cut -d/ -f1)
  if [[ -n "${PPP_GATEWAY}" && "${PPP_GATEWAY}" != "link" ]]; then
    echo "[4gproxy-net] Using PPP gateway: ${PPP_GATEWAY}"
    ip route replace default via "${PPP_GATEWAY}" dev "${CELL_IFACE}" table "${TABLE_ID}"
  else
    echo "[4gproxy-net] No valid PPP gateway found, using dev-only route"
    ip route replace default dev "${CELL_IFACE}" table "${TABLE_ID}"
  fi
else
  ip route replace default dev "${CELL_IFACE}" table "${TABLE_ID}"
fi

# 5) ip rule for fwmark==1 -> proxy_table
ip rule add fwmark 0x1 table "${TABLE_ID}" pref 100 2>/dev/null || true

# 6) mark packets ONLY from Squid user 'proxy' (DO NOT MARK root)
iptables -t mangle -D OUTPUT -m owner --uid-owner proxy -j MARK --set-mark 1 2>/dev/null || true
iptables -t mangle -A OUTPUT -m owner --uid-owner proxy -j MARK --set-mark 1

# 7) NAT only when leaving the cellular iface
iptables -t nat -C POSTROUTING -o "${CELL_IFACE}" -j MASQUERADE 2>/dev/null || \
  iptables -t nat -A POSTROUTING -o "${CELL_IFACE}" -j MASQUERADE

echo "[4gproxy-net] policy routing active: fwmark 0x1 -> table ${TABLE_ID} -> ${CELL_IFACE}"
echo "[4gproxy-net] Routing table ${TABLE_ID} contents:"
ip route show table "${TABLE_ID}" || echo "  (table empty or not found)"
echo "[4gproxy-net] done."
