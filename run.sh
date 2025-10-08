#!/usr/bin/env bash
set -euo pipefail

# ================================================================
# Raspberry Pi 4G Proxy (SIM7600) — end-to-end bring-up + hardening
# - Safe cleanup (no root traffic marking; SSH remains stable)
# - Idempotent dependency install
# - Bring up modem via main.py (RNDIS -> PPP fallback)
# - Install squid.conf and bind to 0.0.0.0:3128
# - Open firewall for LAN: SSH (22) + Squid (3128) + (optional) dashboard (5000)
# - Policy-based routing: ONLY Squid UID/GID ('proxy' or 'squid') via table 'cellular'
# - Removes any legacy 'rndis' ip rule/table so there’s exactly one path
# - PM2 for orchestrator + web, with boot persistence via systemd
# ================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

[[ $EUID -eq 0 ]] || { echo "Run as root: sudo ./run.sh"; exit 1; }

REAL_USER="${SUDO_USER:-$USER}"
REAL_HOME="$(getent passwd "$REAL_USER" | cut -d: -f6)"
[[ -n "${REAL_HOME}" && -d "${REAL_HOME}" ]] || { echo "Cannot determine home for ${REAL_USER}"; exit 1; }

# ---------- Paths ----------
PKILL="$(command -v pkill || echo /usr/bin/pkill)"
IP="$(command -v ip || echo /usr/sbin/ip)"
IPTABLES="$(command -v iptables || echo /usr/sbin/iptables)"
SYSTEMCTL="$(command -v systemctl || echo /bin/systemctl)"
PM2_PATH="$(command -v pm2 || true)"
NODE_PATH="$(command -v node || true)"
SS_BIN="$(command -v ss || echo /usr/sbin/ss)"

echo "==> Clean processes/PM2…"
$PKILL -f "python.*orchestrator" 2>/dev/null || true
$PKILL -f "python.*web_interface" 2>/dev/null || true
$PKILL -f "flask" 2>/dev/null || true
$PKILL -f "uvicorn" 2>/dev/null || true
$PKILL -f "gunicorn" 2>/dev/null || true
$PKILL pppd 2>/dev/null || true
if [[ -n "${PM2_PATH}" ]]; then
  pm2 delete 4g-proxy-orchestrator 2>/dev/null || true
  pm2 delete 4g-proxy-web 2>/dev/null || true
  pm2 kill 2>/dev/null || true
fi

echo "==> Reset policy routing / NAT (idempotent)…"
# Remove legacy/duplicate rules & tables
$IP rule del fwmark 0x1 table rndis 2>/dev/null || true
$IP route flush table rndis 2>/dev/null || true
$IP rule del fwmark 0x1 table 100 2>/dev/null || true

# Clean cellular and rebuild fresh
$IP rule del fwmark 0x1 table cellular 2>/dev/null || true
$IP route flush table cellular 2>/dev/null || true

# Remove any old owner MARK rules for proxy/squid
for U in proxy squid; do
  if id -u "$U" >/dev/null 2>&1; then
    $IPTABLES -t mangle -D OUTPUT -m owner --uid-owner "$U" -j MARK --set-mark 1 2>/dev/null || true
    $IPTABLES -t mangle -D OUTPUT -m owner --gid-owner "$U" -j MARK --set-mark 1 2>/dev/null || true
  fi
done
# Never mark root.

# Remove any old MASQUERADEs (we add the correct one later)
for IFX in ppp0 usb0 wwan0 enx* eth1 eth2 eth3; do
  $IPTABLES -t nat -D POSTROUTING -o "$IFX" -j MASQUERADE 2>/dev/null || true
done

mkdir -p state
chown -R "${REAL_USER}:${REAL_USER}" state

echo "==> Dependencies…"
apt-get update -y
apt-get install -y \
  curl jq iptables iptables-persistent \
  python3 python3-pip python3-yaml python3-requests python3-serial \
  squid modemmanager ppp libqmi-utils udhcpc isc-dhcp-client

if [[ -z "${NODE_PATH}" ]]; then
  curl -fsSL https://deb.nodesource.com/setup_18.x | bash -
  apt-get install -y nodejs
fi
command -v pm2 >/dev/null 2>&1 || npm install -g pm2

echo "==> Bring up modem via main.py…"
python3 "${SCRIPT_DIR}/main.py" || true

echo "==> Install squid.conf; bind to 0.0.0.0:3128; allow LAN…"
if [[ -f "${SCRIPT_DIR}/squid.conf" ]]; then
  cp "${SCRIPT_DIR}/squid.conf" /etc/squid/squid.conf
fi
# Ensure it listens everywhere and LAN is allowed
sed -i 's/^http_port .*:3128/http_port 0.0.0.0:3128/' /etc/squid/squid.conf || true
grep -q '^http_port 0.0.0.0:3128' /etc/squid/squid.conf || echo 'http_port 0.0.0.0:3128' >> /etc/squid/squid.conf
grep -q '^acl localnet src 192\.168\.0\.0/16' /etc/squid/squid.conf || {
  cat >>/etc/squid/squid.conf <<'SQ'
acl localnet src 192.168.0.0/16
http_access allow localnet
http_access allow localhost
http_access deny all
SQ
}
systemctl restart squid || true

# Determine LAN IP (prefer eth0; fallback wlan0)
LAN_IP="$(ip -4 addr show eth0 2>/dev/null | awk '/inet /{print $2}' | cut -d/ -f1)"
[[ -z "$LAN_IP" ]] && LAN_IP="$(ip -4 addr show wlan0 2>/dev/null | awk '/inet /{print $2}' | cut -d/ -f1)"

# ==============================================================================
# ROUTING PRINCIPLE:
# - Main routing table: WiFi/Ethernet stays untouched (SSH, system traffic)
# - Cellular table (100): 4G route ONLY for marked traffic
# - Policy: ONLY Squid (proxy/squid UID/GID) gets fwmark 0x1 → cellular table
# - Result: Proxy uses 4G exclusively; everything else uses WiFi/Eth
# ==============================================================================

echo "==> Detect cellular iface (PPP/RNDIS/QMI)…"
detect_cell_iface() {
  if $IP -4 addr show ppp0 2>/dev/null | grep -q "inet "; then
    echo "ppp0"; return 0
  fi
  # Prefer active IPv4 interfaces that look like modem USB/QMI/RNDIS
  $IP -o -4 addr show | awk '{print $2}' | grep -E '^(enx|usb0|wwan[0-9]+|eth1|eth2|eth3)' | head -n1 || true
}
CELL_IFACE="$(detect_cell_iface || true)"
if [[ -z "${CELL_IFACE:-}" ]]; then
  echo "⚠️  No cellular iface with IPv4 yet (PPP/RNDIS/QMI). Leaving LAN-only; run again after modem is up."
else
  echo "   -> cellular iface: ${CELL_IFACE}"
  $IP link set dev "${CELL_IFACE}" up || true
  
  # Ensure cellular interface is properly up and connected
  echo "   -> Ensuring cellular interface is connected..."
  sleep 2
  $IP link set dev "${CELL_IFACE}" up || true
  
  # Test if cellular interface can reach internet, if not try to establish connection
  if ! ping -I "${CELL_IFACE}" -c 1 -W 3 8.8.8.8 >/dev/null 2>&1; then
    echo "   -> Cellular interface not connected, attempting to establish connection..."
    # Try dhclient to get proper IP configuration
    $DHCLIENT -v "${CELL_IFACE}" 2>/dev/null || true
    sleep 3
    # Bring interface up again after dhclient
    $IP link set dev "${CELL_IFACE}" up || true
  fi
  
  # Ensure cellular interface stays up and has internet connectivity
  echo "   -> Ensuring cellular interface maintains connection..."
  $IP link set dev "${CELL_IFACE}" up || true
  sleep 2
  # Test connectivity again and retry if needed
  if ! ping -I "${CELL_IFACE}" -c 1 -W 3 8.8.8.8 >/dev/null 2>&1; then
    echo "   -> Retrying cellular connection..."
    $DHCLIENT -v "${CELL_IFACE}" 2>/dev/null || true
    sleep 2
    $IP link set dev "${CELL_IFACE}" up || true
  fi

  # Ensure 'cellular' table exists
  RT_TABLES="/etc/iproute2/rt_tables"
  grep -qE "^[[:space:]]*100[[:space:]]+cellular$" "${RT_TABLES}" 2>/dev/null || echo "100 cellular" >> "${RT_TABLES}"

  # Write default route in cellular table with proper gateway
  if [[ "${CELL_IFACE}" == "ppp0" ]]; then
    PPP_GW="$($IP -4 addr show ppp0 | awk '/peer/ {print $4}' | cut -d/ -f1)"
    if [[ -n "${PPP_GW}" && "${PPP_GW}" != "link" ]]; then
      $IP route replace default via "${PPP_GW}" dev ppp0 table cellular
    else
      $IP route replace default dev ppp0 table cellular
    fi
  else
    # For RNDIS/QMI interfaces, get the gateway from the interface's route
    CELL_GW="$($IP route show dev "${CELL_IFACE}" | awk '/default/ {print $3}' | head -n1)"
    if [[ -n "${CELL_GW}" ]]; then
      $IP route replace default via "${CELL_GW}" dev "${CELL_IFACE}" table cellular
    else
      $IP route replace default dev "${CELL_IFACE}" table cellular
    fi
  fi

  # Single policy rule for marked traffic
  $IP rule add fwmark 0x1 table cellular pref 100 2>/dev/null || true

  # Mark ONLY Squid processes (both UID and GID; cover 'proxy' & 'squid')
  for U in proxy squid; do
    if id -u "$U" >/dev/null 2>&1; then
      $IPTABLES -t mangle -C OUTPUT -m owner --uid-owner "$U" -j MARK --set-mark 1 2>/dev/null || \
        $IPTABLES -t mangle -A OUTPUT -m owner --uid-owner "$U" -j MARK --set-mark 1
      $IPTABLES -t mangle -C OUTPUT -m owner --gid-owner "$U" -j MARK --set-mark 1 2>/dev/null || \
        $IPTABLES -t mangle -A OUTPUT -m owner --gid-owner "$U" -j MARK --set-mark 1
    fi
  done

  # NAT when leaving the cellular iface (good hygiene if helpers/ICMP go out)
  $IPTABLES -t nat -C POSTROUTING -o "${CELL_IFACE}" -j MASQUERADE 2>/dev/null || \
    $IPTABLES -t nat -A POSTROUTING -o "${CELL_IFACE}" -j MASQUERADE

  # Remove any lingering duplicate policy table
  $IP rule del fwmark 0x1 table rndis 2>/dev/null || true
  $IP route flush table rndis 2>/dev/null || true
fi

echo "==> Firewall: allow LAN to reach SSH(22) + Squid(3128) + (opt) Web(5000)…"
# Only manage INPUT; proxy does not need kernel forwarding
$IPTABLES -P FORWARD DROP
# We’ll keep INPUT ACCEPT to avoid surprises, then explicitly add allow rules (harmless with ACCEPT)
$IPTABLES -P INPUT ACCEPT

# (Optional) If you want a stricter policy, uncomment the next 2 lines:
# $IPTABLES -F INPUT
# $IPTABLES -P INPUT DROP && $IPTABLES -A INPUT -i lo -j ACCEPT && $IPTABLES -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT

# Auto-detect LAN CIDR from the Pi's own IP
LAN_CIDR="192.168.0.0/16"  # Default fallback
if [[ -n "${LAN_IP:-}" ]]; then
  # Extract network from Pi's IP (e.g., 192.168.1.37 -> 192.168.1.0/24)
  LAN_NET="$(echo "$LAN_IP" | cut -d. -f1-3).0/24"
  LAN_CIDR="$LAN_NET"
  echo "   -> Detected LAN: ${LAN_CIDR}"
fi

# Add firewall rules in correct order (more specific first)
$IPTABLES -I INPUT -p tcp -s "$LAN_CIDR" --dport 22   -j ACCEPT
$IPTABLES -I INPUT -p tcp -s "$LAN_CIDR" --dport 3128 -j ACCEPT
$IPTABLES -I INPUT -p tcp -s "$LAN_CIDR" --dport 5000 -j ACCEPT

# Add general rules for other networks (less specific)
$IPTABLES -I INPUT -p tcp -s 192.168.0.0/16 --dport 22   -j ACCEPT
$IPTABLES -I INPUT -p tcp -s 192.168.0.0/16 --dport 3128 -j ACCEPT
$IPTABLES -I INPUT -p tcp -s 192.168.0.0/16 --dport 5000 -j ACCEPT

netfilter-persistent save >/dev/null 2>&1 || true

echo "==> Start orchestrator + web (PM2) under ${REAL_USER}…"
sudo -u "${REAL_USER}" -H pm2 start "${SCRIPT_DIR}/ecosystem.config.js" || true
sudo -u "${REAL_USER}" -H pm2 save || true

echo "==> Install systemd unit for boot persistence…"
SERVICE=/etc/systemd/system/raspi-4g-proxy.service
cat >"$SERVICE"<<UNIT
[Unit]
Description=Raspberry Pi 4G Proxy bootstrap (run.sh)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=${SCRIPT_DIR}
ExecStart=/usr/bin/env bash ${SCRIPT_DIR}/run.sh
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable raspi-4g-proxy.service >/dev/null

# Create a service to keep cellular interface up
echo "==> Creating cellular interface keepalive service…"
cat >/etc/systemd/system/cellular-keepalive.service <<KEEPALIVE
[Unit]
Description=Keep cellular interface up and connected
After=network.target

[Service]
Type=oneshot
ExecStart=/bin/bash -c 'ip link set ${CELL_IFACE} up && dhclient ${CELL_IFACE}'
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
KEEPALIVE

systemctl daemon-reload
systemctl enable cellular-keepalive.service >/dev/null

echo
echo "==> Health:"
echo "    Squid listening: $($SS_BIN -lntp | awk '/:3128/ {print $4}' | head -n1)"
echo "    ip rule:"; ip rule | sed 's/^/      /'
echo "    table cellular:"; ip route show table cellular | sed 's/^/      /' || true
if [[ -n "${LAN_IP:-}" ]]; then
  echo "    Direct IP: $(curl -s --max-time 6 https://api.ipify.org || echo unknown)"
  echo "    Proxy IP : $(curl -s --max-time 6 -x http://${LAN_IP}:3128 https://api.ipify.org || echo unknown)"
fi

echo
echo "✅ Done. From your PC, set HTTP/HTTPS proxy to: http://${LAN_IP:-<your-LAN-IP>}:3128"
