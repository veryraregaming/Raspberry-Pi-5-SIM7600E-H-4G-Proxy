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
  $IP -o -4 addr show | awk '{print $2}' | grep -E '^(wwan|enx|usb0|eth1|eth2|eth3)' | head -n1 || true
}

# Detect gateway for interface
detect_gateway() {
  local iface="$1"
  
  # Try from main routing table first
  local gw="$($IP route show dev "${iface}" | awk '/^default/ {print $3}' | head -n1)"
  if [[ -n "${gw}" ]]; then
    echo "${gw}"
    return 0
  fi
  
  # Try from all routes
  gw="$($IP route | awk "/^default.*${iface}/ {print \$3}" | head -n1)"
  if [[ -n "${gw}" ]]; then
    echo "${gw}"
    return 0
  fi
  
  # For RNDIS interfaces, try standard gateways
  if [[ "${iface}" == enx* ]] || [[ "${iface}" == eth1 ]]; then
    # Test 192.168.225.1 (common RNDIS gateway)
    if ping -I "${iface}" -c 1 -W 2 192.168.225.1 >/dev/null 2>&1; then
      echo "192.168.225.1"
      return 0
    fi
  fi
  
  # For QMI/wwan, gateway is usually the first hop
  if [[ "${iface}" == wwan* ]]; then
    # Check if we can reach 8.8.8.8 directly (some modems don't need explicit gateway)
    if ping -I "${iface}" -c 1 -W 2 8.8.8.8 >/dev/null 2>&1; then
      # Interface can route directly, no specific gateway needed
      echo "direct"
      return 0
    fi
  fi
  
  return 1
}

CELL_IFACE="$(detect_cell_iface || true)"
if [[ -z "${CELL_IFACE:-}" ]]; then
  echo "⚠️  No cellular iface with IPv4 yet (PPP/RNDIS/QMI). Leaving LAN-only; run again after modem is up."
else
  echo "   -> cellular iface: ${CELL_IFACE}"
  
  # Ensure cellular interface is UP
  echo "   -> Bringing ${CELL_IFACE} UP..."
  $IP link set dev "${CELL_IFACE}" up || true
  sleep 2
  
  # For RNDIS/QMI interfaces, ensure DHCP is configured
  if [[ "${CELL_IFACE}" != "ppp0" ]]; then
    if ! $IP -4 addr show "${CELL_IFACE}" | grep -q "inet "; then
      echo "   -> No IP on ${CELL_IFACE}, requesting DHCP..."
      
      # Try udhcpc first (lighter), then dhclient
      if command -v udhcpc >/dev/null 2>&1; then
        udhcpc -i "${CELL_IFACE}" -q -n 2>/dev/null || {
          echo "   -> udhcpc failed, trying dhclient..."
          dhclient -v "${CELL_IFACE}" 2>/dev/null || true
        }
      else
        dhclient -v "${CELL_IFACE}" 2>/dev/null || true
      fi
      sleep 3
      $IP link set dev "${CELL_IFACE}" up || true
    fi
    
    # Test connectivity
    echo "   -> Testing ${CELL_IFACE} connectivity..."
    if ping -I "${CELL_IFACE}" -c 1 -W 3 8.8.8.8 >/dev/null 2>&1; then
      echo "   ✅ ${CELL_IFACE} has internet connectivity"
    else
      echo "   ⚠️  ${CELL_IFACE} connectivity test failed, may need manual troubleshooting"
    fi
  fi
  
  # CRITICAL: Ensure WiFi remains the default route (not cellular)
  echo "   -> Ensuring WiFi stays as default route..."
  DEF_GW="$($IP route show default | awk '/default/ {print $3; exit}')"
  DEF_IF="$($IP route show default | awk '/default/ {print $5; exit}')"
  if [[ -n "${DEF_GW}" && -n "${DEF_IF}" && "${DEF_IF}" != "${CELL_IFACE}" ]]; then
    $IP route replace default via "${DEF_GW}" dev "${DEF_IF}" metric 100 || true
    echo "   -> Default route: ${DEF_GW} via ${DEF_IF}"
  fi

  # Ensure 'cellular' table exists
  RT_TABLES="/etc/iproute2/rt_tables"
  grep -qE "^[[:space:]]*100[[:space:]]+cellular$" "${RT_TABLES}" 2>/dev/null || echo "100 cellular" >> "${RT_TABLES}"

  # Write default route in cellular table with proper gateway
  echo "   -> Configuring cellular routing table..."
  if [[ "${CELL_IFACE}" == "ppp0" ]]; then
    PPP_GW="$($IP -4 addr show ppp0 | awk '/peer/ {print $4}' | cut -d/ -f1)"
    if [[ -n "${PPP_GW}" && "${PPP_GW}" != "link" ]]; then
      $IP route replace default via "${PPP_GW}" dev ppp0 table cellular
      echo "   -> Cellular table: default via ${PPP_GW} dev ppp0"
    else
      $IP route replace default dev ppp0 table cellular
      echo "   -> Cellular table: default dev ppp0"
    fi
  else
    # Detect gateway dynamically
    if CELL_GW="$(detect_gateway "${CELL_IFACE}")"; then
      if [[ "${CELL_GW}" == "direct" ]]; then
        # No explicit gateway needed, interface routes directly
        $IP route replace default dev "${CELL_IFACE}" table cellular
        echo "   -> Cellular table: default dev ${CELL_IFACE} (direct routing)"
      else
        $IP route replace default via "${CELL_GW}" dev "${CELL_IFACE}" table cellular
        echo "   ✅ Cellular table: default via ${CELL_GW} dev ${CELL_IFACE}"
      fi
    else
      echo "   ⚠️  Could not detect gateway for ${CELL_IFACE}, using direct routing"
      $IP route replace default dev "${CELL_IFACE}" table cellular
    fi
  fi

  # Verify cellular table has routes
  if ! $IP route show table cellular | grep -q "default"; then
    echo "   ⚠️  WARNING: Cellular table has no default route!"
  fi

  # Single policy rule for marked traffic
  $IP rule del fwmark 0x1 table cellular 2>/dev/null || true
  $IP rule add fwmark 0x1 table cellular pref 100

  # Mark ONLY Squid processes (both UID and GID; cover 'proxy' & 'squid')
  for U in proxy squid; do
    if id -u "$U" >/dev/null 2>&1; then
      $IPTABLES -t mangle -D OUTPUT -m owner --uid-owner "$U" -j MARK --set-mark 1 2>/dev/null || true
      $IPTABLES -t mangle -A OUTPUT -m owner --uid-owner "$U" -j MARK --set-mark 1
      $IPTABLES -t mangle -D OUTPUT -m owner --gid-owner "$U" -j MARK --set-mark 1 2>/dev/null || true
      $IPTABLES -t mangle -A OUTPUT -m owner --gid-owner "$U" -j MARK --set-mark 1
    fi
  done

  # NAT when leaving the cellular iface
  $IPTABLES -t nat -D POSTROUTING -o "${CELL_IFACE}" -j MASQUERADE 2>/dev/null || true
  $IPTABLES -t nat -A POSTROUTING -o "${CELL_IFACE}" -j MASQUERADE

  # Remove any lingering duplicate policy tables
  $IP rule del fwmark 0x1 table rndis 2>/dev/null || true
  $IP route flush table rndis 2>/dev/null || true
fi

echo "==> Firewall: allow LAN to reach SSH(22) + Squid(3128) + (opt) Web(5000)…"
# Only manage INPUT; proxy does not need kernel forwarding
$IPTABLES -P FORWARD DROP

# Clear existing INPUT rules to prevent duplicates
$IPTABLES -F INPUT

# Keep INPUT ACCEPT policy, then add specific allow rules
$IPTABLES -P INPUT ACCEPT
$IPTABLES -A INPUT -i lo -j ACCEPT
$IPTABLES -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT

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

# Install cellular keepalive service
echo "==> Installing cellular interface keepalive service…"
if [[ -f "${SCRIPT_DIR}/cellular-keepalive.sh" ]]; then
  cp "${SCRIPT_DIR}/cellular-keepalive.sh" /usr/local/bin/cellular-keepalive
  chmod +x /usr/local/bin/cellular-keepalive
  
  cat >/etc/systemd/system/cellular-keepalive.service <<'KEEPALIVE'
[Unit]
Description=Cellular Interface Keepalive Monitor
After=network.target
Wants=network.target

[Service]
Type=simple
ExecStart=/usr/local/bin/cellular-keepalive
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
KEEPALIVE

  systemctl daemon-reload
  systemctl enable cellular-keepalive.service >/dev/null
  systemctl restart cellular-keepalive.service >/dev/null || true
  echo "   ✅ Cellular keepalive service installed and started"
else
  echo "   ⚠️  cellular-keepalive.sh not found, skipping keepalive service"
fi

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
