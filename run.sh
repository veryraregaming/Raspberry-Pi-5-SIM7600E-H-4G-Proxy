#!/usr/bin/env bash
set -euo pipefail

# =====================================================================
# Raspberry Pi 5 + SIM7600E-H 4G Proxy — bootstrap + clean start
# - Cleans lingering processes, ip rules and iptables marks
# - Installs dependencies (idempotent)
# - Ensures sudoers for the login user
# - Runs main.py to write config and bring up cellular
# - Detects cellular iface (PPP/RNDIS/QMI) and pins ONLY Squid egress
# - Starts orchestrator + web via PM2 under the real user
# - Keeps LAN default route intact (eth0/wlan0)
# =====================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

REAL_USER="${SUDO_USER:-$USER}"
REAL_HOME="$(getent passwd "$REAL_USER" | cut -d: -f6)"
if [[ -z "${REAL_HOME}" || ! -d "${REAL_HOME}" ]]; then
  echo "Could not determine home directory for REAL_USER=$REAL_USER"
  exit 1
fi

echo "==> Preparing clean start (user: ${REAL_USER}, home: ${REAL_HOME})"

# -------- resolve command paths -------------
PKILL_PATH="$(command -v pkill || echo /usr/bin/pkill)"
PPPD_PATH="$(command -v pppd || echo /usr/sbin/pppd)"
IP_PATH="$(command -v ip || echo /usr/sbin/ip)"
SYSTEMCTL_PATH="$(command -v systemctl || echo /bin/systemctl)"
MMCLI_PATH="$(command -v mmcli || echo /usr/bin/mmcli)"
QMI_NETWORK_PATH="$(command -v qmi-network || echo /usr/bin/qmi-network)"
QMICLI_PATH="$(command -v qmicli || echo /usr/bin/qmicli)"
UDHCPC_PATH="$(command -v udhcpc || echo /sbin/udhcpc)"
DHCLIENT_PATH="$(command -v dhclient || echo /sbin/dhclient)"
IPTABLES_PATH="$(command -v iptables || echo /usr/sbin/iptables)"
PM2_PATH="$(command -v pm2 || true)"

# -------- CLEANUP: processes ----------------
echo "==> Killing lingering processes (safe, idempotent)…"
${PKILL_PATH} -f "python.*orchestrator" 2>/dev/null || true
${PKILL_PATH} -f "python.*web_interface" 2>/dev/null || true
${PKILL_PATH} -f "flask" 2>/dev/null || true
${PKILL_PATH} -f "uvicorn" 2>/dev/null || true
${PKILL_PATH} -f "gunicorn" 2>/dev/null || true
${PKILL_PATH} pppd 2>/dev/null || true

# -------- CLEANUP: PM2 apps/daemon ----------
echo "==> Cleaning PM2 apps/daemon…"
if [[ -n "${PM2_PATH}" ]]; then
  pm2 delete 4g-proxy-orchestrator 2>/dev/null || true
  pm2 delete 4g-proxy-web 2>/dev/null || true
  pm2 kill 2>/dev/null || true
fi

# -------- CLEANUP: policy routing / iptables marks ----
echo "==> Cleaning ip rules/marks (idempotent)…"
${IP_PATH} rule del fwmark 0x1 table cellular 2>/dev/null || true
${IP_PATH} rule del fwmark 0x1 table 100 2>/dev/null || true
${IP_PATH} route flush table cellular 2>/dev/null || true
${IP_PATH} route flush table 100 2>/dev/null || true

${IPTABLES_PATH} -t mangle -D OUTPUT -m owner --uid-owner proxy -j MARK --set-mark 1 2>/dev/null || true
# DO NOT mark root (prevents SSH drop)

for IFX in ppp0 usb0 eth1 eth2 eth3 wwan0; do
  ${IPTABLES_PATH} -t nat -D POSTROUTING -o "$IFX" -j MASQUERADE 2>/dev/null || true
done
echo "✅ Clean start complete"

# -------- ensure state dir -------------------
mkdir -p state
chown -R "${REAL_USER}:${REAL_USER}" state
chmod 755 state

# -------- sudoers for the real user ---------
echo "==> Writing /etc/sudoers.d/4g-proxy…"
cat >/etc/sudoers.d/4g-proxy <<EOF
# 4G Proxy sudoers for ${REAL_USER}
Cmnd_Alias PROXY_CMDS = \\
  ${PKILL_PATH} pppd, \\
  ${PPPD_PATH} *, \\
  ${IP_PATH} route del default, \\
  ${IP_PATH} route add default dev ppp0 metric 200, \\
  ${IP_PATH} link set dev * up, \\
  ${IP_PATH} link set dev * down, \\
  ${DHCLIENT_PATH} *, \\
  ${QMI_NETWORK_PATH} *, \\
  ${QMICLI_PATH} *, \\
  ${UDHCPC_PATH} *, \\
  ${SYSTEMCTL_PATH} start ModemManager, \\
  ${SYSTEMCTL_PATH} stop ModemManager, \\
  ${MMCLI_PATH} -m 0 --disable, \\
  ${MMCLI_PATH} -m 0 --enable

${REAL_USER} ALL=(root) NOPASSWD: PROXY_CMDS
Defaults:${REAL_USER} !requiretty
EOF
chmod 0440 /etc/sudoers.d/4g-proxy
visudo -c >/dev/null
echo "✅ sudoers validated"

# -------- deps (idempotent) -----------------
echo "==> Installing dependencies…"
apt-get update -y
DEBS=(
  curl wget unzip build-essential iptables
  python3 python3-pip python3-yaml python3-serial python3-requests python3-flask
  ca-certificates gnupg modemmanager ppp libqmi-utils udhcpc isc-dhcp-client
  squid
)
apt-get install -y "${DEBS[@]}"

# Node.js + PM2 (if needed)
if ! command -v node >/dev/null 2>&1; then
  echo "==> Installing Node.js 18.x…"
  curl -fsSL https://deb.nodesource.com/setup_18.x | bash -
  apt-get install -y nodejs
fi
if ! command -v pm2 >/dev/null 2>&1; then
  echo "==> Installing PM2…"
  npm install -g pm2
fi

# -------- run main.py to write configs + bring up modem --------------
echo "==> Running main.py (writes config, brings up cellular)…"
python3 "${SCRIPT_DIR}/main.py" || true

# -------- policy routing: auto-detect cellular iface -----------------
echo "==> Applying policy routing for Squid (auto-detect iface)…"

# Keep existing default (LAN) as primary route
DEF_GW="$(${IP_PATH} route show default | awk '/default/ {print $3; exit}')"
DEF_IF="$(${IP_PATH} route show default | awk '/default/ {print $5; exit}')"
if [[ -n "${DEF_GW}" && -n "${DEF_IF}" ]]; then
  ${IP_PATH} route replace default via "${DEF_GW}" dev "${DEF_IF}" metric 100 || true
fi

detect_cell_iface() {
  if ${IP_PATH} -4 addr show ppp0 2>/dev/null | grep -q "inet "; then
    echo "ppp0"; return 0
  fi
  for pat in wwan enx usb0 eth1 eth2 eth3; do
    CAND="$(${IP_PATH} -o link show | awk -F': ' '{print $2}' | grep -E "^${pat}" | head -n1 || true)"
    if [[ -n "${CAND}" ]] && ${IP_PATH} link show "${CAND}" | grep -q "state UP\|state UNKNOWN"; then
      if ${IP_PATH} -4 addr show "${CAND}" | grep -q "inet "; then
        echo "${CAND}"; return 0
      fi
    fi
  done
  return 1
}

CELL_IFACE="$(detect_cell_iface || true)"
if [[ -z "${CELL_IFACE:-}" ]]; then
  echo "⚠️ No active cellular iface detected (PPP/RNDIS/QMI). Skipping policy routing."
else
  echo "   -> Cellular iface: ${CELL_IFACE}"

  RT_TABLES="/etc/iproute2/rt_tables"
  grep -qE "^[[:space:]]*100[[:space:]]+cellular$" "${RT_TABLES}" 2>/dev/null || \
    echo "100 cellular" >> "${RT_TABLES}"

  if [[ "${CELL_IFACE}" == "ppp0" ]]; then
    PPP_GATEWAY="$(${IP_PATH} -4 addr show ppp0 | awk '/peer/ {print $4}' | cut -d/ -f1)"
    if [[ -n "${PPP_GATEWAY}" && "${PPP_GATEWAY}" != "link" ]]; then
      ${IP_PATH} route replace default via "${PPP_GATEWAY}" dev "${CELL_IFACE}" table cellular
    else
      ${IP_PATH} route replace default dev "${CELL_IFACE}" table cellular
    fi
  else
    ${IP_PATH} route replace default dev "${CELL_IFACE}" table cellular
  fi

  ${IP_PATH} rule add fwmark 0x1 table cellular pref 100 2>/dev/null || true

  ${IPTABLES_PATH} -t mangle -D OUTPUT -m owner --uid-owner proxy -j MARK --set-mark 1 2>/dev/null || true
  ${IPTABLES_PATH} -t mangle -A OUTPUT -m owner --uid-owner proxy -j MARK --set-mark 1

  ${IPTABLES_PATH} -t nat -C POSTROUTING -o "${CELL_IFACE}" -j MASQUERADE 2>/dev/null || \
    ${IPTABLES_PATH} -t nat -A POSTROUTING -o "${CELL_IFACE}" -j MASQUERADE

  echo "✅ Policy routing active: fwmark 0x1 -> table 'cellular' -> ${CELL_IFACE}"
  ${IP_PATH} route show table cellular || true
fi

# -------- restart squid to load squid.conf ---------------------------
echo "==> Restarting squid…"
${SYSTEMCTL_PATH} restart squid || true

# -------- start PM2 apps as the real user ----------------------------
echo "==> Starting PM2 apps under ${REAL_USER}…"
sudo -u "${REAL_USER}" -H pm2 start "${SCRIPT_DIR}/ecosystem.config.js" || true
sudo -u "${REAL_USER}" -H pm2 save || true
sudo -u "${REAL_USER}" -H pm2 status || true

LAN_IP="$(hostname -I | awk '{print $1}')"
echo "==> Done."
echo "Try:"
echo "  curl -s https://api.ipify.org && echo"
echo "  curl -x http://${LAN_IP}:3128 -s https://api/ipify.org && echo"
