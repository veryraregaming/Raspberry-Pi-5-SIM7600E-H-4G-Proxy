#!/usr/bin/env bash
set -euo pipefail

# ================================================================
# One-shot bootstrap for your 4G proxy on Raspberry Pi (SIM7600)
# - Safe cleanup (no root traffic marking, SSH stays up)
# - Idempotent deps install (Python, Squid, ModemManager, QMI, etc.)
# - Runs main.py to bring up modem (RNDIS→PPP fallback)
# - Installs squid.conf (binds 0.0.0.0:3128)
# - Policy routing: ONLY Squid user 'proxy' via cellular table
# - Starts orchestrator + web (PM2)
# - Adds systemd service to re-run this on boot
# ================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [[ $EUID -ne 0 ]]; then
  echo "Please run as root: sudo ./run.sh"
  exit 1
fi

REAL_USER="${SUDO_USER:-$USER}"
REAL_HOME="$(getent passwd "$REAL_USER" | cut -d: -f6)"
if [[ -z "${REAL_HOME}" || ! -d "${REAL_HOME}" ]]; then
  echo "Cannot determine home directory for REAL_USER=${REAL_USER}"
  exit 1
fi

echo "==> Running bootstrap as root; PM2 apps will run as ${REAL_USER}"

# ---------- Resolve command paths ----------
PKILL="$(command -v pkill || echo /usr/bin/pkill)"
IP="$(command -v ip || echo /usr/sbin/ip)"
IPTABLES="$(command -v iptables || echo /usr/sbin/iptables)"
SYSTEMCTL="$(command -v systemctl || echo /bin/systemctl)"
DHCLIENT="$(command -v dhclient || echo /sbin/dhclient)"
QMICLI="$(command -v qmicli || echo /usr/bin/qmicli)"
UDHCPC="$(command -v udhcpc || echo /sbin/udhcpc)"
PM2_PATH="$(command -v pm2 || true)"
NODE_PATH="$(command -v node || true)"

# ---------- CLEANUP: processes / PM2 ----------
echo "==> Clean up lingering processes…"
$PKILL -f "python.*orchestrator" 2>/dev/null || true
$PKILL -f "python.*web_interface" 2>/dev/null || true
$PKILL -f "flask" 2>/dev/null || true
$PKILL -f "uvicorn" 2>/dev/null || true
$PKILL -f "gunicorn" 2>/dev/null || true
$PKILL pppd 2>/dev/null || true

echo "==> Clean PM2 apps…"
if [[ -n "${PM2_PATH}" ]]; then
  pm2 delete 4g-proxy-orchestrator 2>/dev/null || true
  pm2 delete 4g-proxy-web 2>/dev/null || true
  pm2 kill 2>/dev/null || true
fi

# ---------- CLEANUP: policy routing / iptables ----------
echo "==> Reset policy routing + NAT (idempotent)…"
$IP rule del fwmark 0x1 table cellular 2>/dev/null || true
$IP route flush table cellular 2>/dev/null || true
# some older runs may have used 'rndis' or table 100
$IP rule del fwmark 0x1 table rndis 2>/dev/null || true
$IP route flush table 100 2>/dev/null || true

$IPTABLES -t mangle -D OUTPUT -m owner --uid-owner proxy -j MARK --set-mark 1 2>/dev/null || true
# IMPORTANT: we never mark root, to keep SSH stable

for IFX in ppp0 usb0 wwan0 eth1 eth2 eth3; do
  $IPTABLES -t nat -D POSTROUTING -o "$IFX" -j MASQUERADE 2>/dev/null || true
done

# ---------- ensure state dir ----------
mkdir -p state
chown -R "${REAL_USER}:${REAL_USER}" state
chmod 755 state

# ---------- sudoers ----------
echo "==> Install sudoers entry for ${REAL_USER}…"
cat >/etc/sudoers.d/4g-proxy <<EOF
# 4G Proxy sudoers for ${REAL_USER}
Cmnd_Alias PROXY_CMDS = \\
  ${PKILL} pppd, \\
  /usr/sbin/pppd *, \\
  ${IP} route *, \\
  ${IP} link *, \\
  ${DHCLIENT} *, \\
  ${QMICLI} *, \\
  ${UDHCPC} *, \\
  ${SYSTEMCTL} start ModemManager, \\
  ${SYSTEMCTL} stop ModemManager, \\
  /usr/bin/mmcli -m 0 --disable, \\
  /usr/bin/mmcli -m 0 --enable

${REAL_USER} ALL=(root) NOPASSWD: PROXY_CMDS
Defaults:${REAL_USER} !requiretty
EOF
chmod 0440 /etc/sudoers.d/4g-proxy
visudo -c >/dev/null || { echo "sudoers invalid"; exit 1; }

# ---------- deps ----------
echo "==> Install dependencies (idempotent)…"
apt-get update -y
apt-get install -y \
  curl wget unzip build-essential iptables nftables jq \
  python3 python3-pip python3-yaml python3-serial python3-requests python3-flask \
  ca-certificates gnupg modemmanager ppp libqmi-utils udhcpc isc-dhcp-client \
  squid

# Node / PM2
if [[ -z "${NODE_PATH}" ]]; then
  echo "==> Installing Node.js 18.x…"
  curl -fsSL https://deb.nodesource.com/setup_18.x | bash -
  apt-get install -y nodejs
fi
if ! command -v pm2 >/dev/null 2>&1; then
  echo "==> Installing PM2…"
  npm install -g pm2
fi

# ---------- run main.py to write config + bring modem up ----------
echo "==> Running main.py (writes config, brings up cellular)…"
python3 "${SCRIPT_DIR}/main.py" || true

# ---------- install squid.conf + bind to 0.0.0.0:3128 ----------
echo "==> Installing squid.conf and binding to 0.0.0.0:3128…"
if [[ -f "${SCRIPT_DIR}/squid.conf" ]]; then
  cp "${SCRIPT_DIR}/squid.conf" /etc/squid/squid.conf
  # ensure bind is 0.0.0.0:3128 so clients can always reach it
  sed -i 's/^http_port .*:3128/http_port 0.0.0.0:3128/' /etc/squid/squid.conf || true
else
  # minimal config if file missing
  LAN_IP="$(ip -4 addr show eth0 2>/dev/null | awk '/inet /{print $2}' | cut -d/ -f1)"
  [[ -z "$LAN_IP" ]] && LAN_IP="$(ip -4 addr show wlan0 2>/dev/null | awk '/inet /{print $2}' | cut -d/ -f1)"
  cat >/etc/squid/squid.conf <<SQ
http_port 0.0.0.0:3128
acl localnet src 192.168.0.0/16 10.0.0.0/8 172.16.0.0/12
http_access allow localnet
http_access allow localhost
http_access deny all
forwarded_for off
request_header_access X-Forwarded-For deny all
request_header_access Via deny all
cache_dir ufs /var/spool/squid 100 16 256
cache_mem 64 MB
access_log stdio:/var/log/squid/access.log
cache_log /var/log/squid/cache.log
dns_nameservers 8.8.8.8 1.1.1.1
SQ
fi
$SYSTEMCTL restart squid || true

# ---------- policy routing: ONLY Squid egress over cellular ----------
echo "==> Applying policy routing for Squid (auto-detect cellular iface)…"

# Keep your LAN default route intact (metric 100)
DEF_GW="$($IP route show default | awk '/default/ {print $3; exit}')"
DEF_IF="$($IP route show default | awk '/default/ {print $5; exit}')"
if [[ -n "${DEF_GW}" && -n "${DEF_IF}" ]]; then
  $IP route replace default via "${DEF_GW}" dev "${DEF_IF}" metric 100 || true
fi

# detect cellular iface with IPv4: prefer PPP, else RNDIS/ECM/QMI names
detect_cell_iface() {
  if $IP -4 addr show ppp0 2>/dev/null | grep -q "inet "; then
    echo "ppp0"; return 0
  fi
  for pat in wwan enx usb0 eth1 eth2 eth3; do
    CAND="$($IP -o link show | awk -F': ' '{print $2}' | grep -E "^${pat}" | head -n1 || true)"
    if [[ -n "${CAND}" ]] && $IP -4 addr show "${CAND}" | grep -q "inet "; then
      echo "${CAND}"; return 0
    fi
  done
  return 1
}
CELL_IFACE="$(detect_cell_iface || true)"
if [[ -z "${CELL_IFACE:-}" ]]; then
  echo "⚠️  No active cellular iface detected (PPP/RNDIS/QMI). Leaving LAN-only. You can re-run after modem is up."
else
  echo "   -> Cellular iface: ${CELL_IFACE}"

  # ensure table name and route
  RT_TABLES="/etc/iproute2/rt_tables"
  grep -qE "^[[:space:]]*100[[:space:]]+cellular$" "${RT_TABLES}" 2>/dev/null || echo "100 cellular" >> "${RT_TABLES}"

  if [[ "${CELL_IFACE}" == "ppp0" ]]; then
    PPP_GW="$($IP -4 addr show ppp0 | awk '/peer/ {print $4}' | cut -d/ -f1)"
    if [[ -n "${PPP_GW}" && "${PPP_GW}" != "link" ]]; then
      $IP route replace default via "${PPP_GW}" dev ppp0 table cellular
    else
      $IP route replace default dev ppp0 table cellular
    fi
  else
    $IP route replace default dev "${CELL_IFACE}" table cellular
  fi

  # rule: fwmark 0x1 -> table cellular
  $IP rule add fwmark 0x1 table cellular pref 100 2>/dev/null || true

  # mark ONLY Squid user 'proxy' (never mark root!)
  $IPTABLES -t mangle -D OUTPUT -m owner --uid-owner proxy -j MARK --set-mark 1 2>/dev/null || true
  $IPTABLES -t mangle -A OUTPUT -m owner --uid-owner proxy -j MARK --set-mark 1

  # NAT when leaving the cellular iface
  $IPTABLES -t nat -C POSTROUTING -o "${CELL_IFACE}" -j MASQUERADE 2>/dev/null || \
    $IPTABLES -t nat -A POSTROUTING -o "${CELL_IFACE}" -j MASQUERADE

  echo "✅ fwmark 0x1 -> table 'cellular' -> ${CELL_IFACE}"
  $IP route show table cellular || true
fi

# ---------- start PM2 apps ----------
echo "==> Starting PM2 apps (orchestrator + web) under ${REAL_USER}…"
sudo -u "${REAL_USER}" -H pm2 start "${SCRIPT_DIR}/ecosystem.config.js" || true
sudo -u "${REAL_USER}" -H pm2 save || true
sudo -u "${REAL_USER}" -H pm2 status || true

# ---------- health checks ----------
LAN_IP="$(ip -4 addr show eth0 2>/dev/null | awk '/inet /{print $2}' | cut -d/ -f1)"
[[ -z "$LAN_IP" ]] && LAN_IP="$(ip -4 addr show wlan0 2>/dev/null | awk '/inet /{print $2}' | cut -d/ -f1)"
echo "==> Health checks:"
echo "    Squid listens on: $(ss -lntp | awk '/:3128/ {print $4}' | head -n1)"
echo "    ip rule: "; ip rule | sed 's/^/      /'
echo "    table cellular: "; ip route show table cellular | sed 's/^/      /' || true
echo "    Proxy test (via ${LAN_IP:-<unknown>}:3128):"
if [[ -n "${LAN_IP:-}" ]]; then
  curl -s -x "http://${LAN_IP}:3128" https://api.ipify.org && echo || true
fi

# ---------- systemd unit for boot persistence ----------
echo "==> Installing systemd unit to re-run on boot…"
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
echo "✅ Done. Reboot-safe."

echo
echo "=== Ready ==="
echo "From your PC, set HTTP/HTTPS proxy to: http://${LAN_IP:-<your-LAN-IP>}:3128"
echo "Direct IP (no proxy):    $(curl -s --max-time 6 https://api.ipify.org || echo unknown)"
if [[ -n "${LAN_IP:-}" ]]; then
  echo "Proxy IP (via Squid):     $(curl -s --max-time 6 -x http://${LAN_IP}:3128 https://api.ipify.org || echo unknown)"
fi
