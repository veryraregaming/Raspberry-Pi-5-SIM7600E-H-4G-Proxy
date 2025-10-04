
#!/usr/bin/env bash
set -euo pipefail

# Find cellular interface (4G modem)
CELL_IFACE=$(ip -o link show | awk -F': ' '{print $2}' | grep -E 'wwan|ppp' | head -n1 || true)
if [[ -z "$CELL_IFACE" ]]; then
  echo "[!] No cellular interface found."
  exit 1
fi

# Find LAN interface (wlan0 or eth0)
LAN_IFACE=$(ip -o link show | awk -F': ' '{print $2}' | grep -E '^wlan0$|^eth0$' | head -n1 || true)
if [[ -z "$LAN_IFACE" ]]; then
  echo "[!] No LAN interface (wlan0/eth0) found."
  exit 1
fi

echo "[+] Cellular interface: $CELL_IFACE"
echo "[+] LAN interface: $LAN_IFACE"

# Enable IP forwarding
sysctl -w net.ipv4.ip_forward=1

# Clear existing NAT rules for cellular interface
iptables -t nat -D POSTROUTING -o "$CELL_IFACE" -j MASQUERADE 2>/dev/null || true
iptables -D FORWARD -i "$LAN_IFACE" -o "$CELL_IFACE" -j ACCEPT 2>/dev/null || true
iptables -D FORWARD -i "$CELL_IFACE" -o "$LAN_IFACE" -m state --state RELATED,ESTABLISHED -j ACCEPT 2>/dev/null || true

# Add NAT rules for 4G proxy
iptables -t nat -A POSTROUTING -o "$CELL_IFACE" -j MASQUERADE
iptables -A FORWARD -i "$LAN_IFACE" -o "$CELL_IFACE" -j ACCEPT
iptables -A FORWARD -i "$CELL_IFACE" -o "$LAN_IFACE" -m state --state RELATED,ESTABLISHED -j ACCEPT

# Block direct internet access via LAN interfaces (force through 4G)
# Remove any existing default routes via LAN interfaces
ip route del default via $(ip route | awk '/default/ {print $3}' | head -n1) dev "$LAN_IFACE" 2>/dev/null || true

# Add default route via cellular interface (highest priority)
ip route add default dev "$CELL_IFACE" metric 100 2>/dev/null || true

echo "[+] NAT active via $CELL_IFACE"
echo "[+] Default route set to $CELL_IFACE"
echo "[+] All traffic forced through 4G connection"
