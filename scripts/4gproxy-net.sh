
#!/usr/bin/env bash
set -euo pipefail
CELL_IFACE=$(ip -o link show | awk -F': ' '{print $2}' | grep -E 'wwan|ppp' | head -n1 || true)
if [[ -z "$CELL_IFACE" ]]; then
  echo "[!] No cellular interface found."
  exit 1
fi
LAN_IFACE=$(ip route | awk '/default/ {print $5}' | head -n1)
sysctl -w net.ipv4.ip_forward=1
iptables -t nat -C POSTROUTING -o "$CELL_IFACE" -j MASQUERADE 2>/dev/null || iptables -t nat -A POSTROUTING -o "$CELL_IFACE" -j MASQUERADE
iptables -C FORWARD -i "$LAN_IFACE" -o "$CELL_IFACE" -j ACCEPT 2>/dev/null || iptables -A FORWARD -i "$LAN_IFACE" -o "$CELL_IFACE" -j ACCEPT
echo "[+] NAT active via $CELL_IFACE"
