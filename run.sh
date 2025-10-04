#!/usr/bin/env bash
set -euo pipefail

# ============================================================================
# One-shot installer/runner for Raspberry-Pi-5-SIM7600E-H-4G-Proxy
# - Installs deps (apt, Node.js, PM2, 3proxy)
# - Writes/ensures helper scripts (4gproxy-net.sh, run_3proxy.sh)
# - Generates config, sets safe policy routing (no default route change)
# - Starts services with PM2 under the REAL login user (not root)
# - Ensures rare->proxyuser sudo for /usr/local/bin/3proxy ONLY (NOPASSWD)
# - Verifies API, Proxy, and prints a summary
# ============================================================================

# ---- guardrails -------------------------------------------------------------
if [[ "$EUID" -ne 0 ]]; then
  echo "Please run as root: sudo ./run.sh"
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

REAL_USER="${SUDO_USER:-$USER}"
REAL_HOME="$(getent passwd "$REAL_USER" | cut -d: -f6)"
if [[ -z "${REAL_HOME}" || ! -d "${REAL_HOME}" ]]; then
  echo "Could not determine home directory for REAL_USER=$REAL_USER"
  exit 1
fi

echo "==> Running as root for setup; PM2 will run as user: ${REAL_USER} (${REAL_HOME})"

# ---- apt & tools ------------------------------------------------------------
echo "==> Installing base packages…"
apt-get update -y
DEBS=(
  curl wget unzip build-essential iptables python3 python3-pip
  python3-yaml python3-serial python3-requests python3-flask
  ca-certificates gnupg
)
apt-get install -y "${DEBS[@]}"

# ---- Node.js + PM2 (global) ------------------------------------------------
if ! command -v node >/dev/null 2>&1; then
  echo "==> Installing Node.js 18.x…"
  curl -fsSL https://deb.nodesource.com/setup_18.x | bash -
  apt-get install -y nodejs
fi

if ! command -v pm2 >/dev/null 2>&1; then
  echo "==> Installing PM2 globally…"
  npm install -g pm2
fi
echo "✅ PM2 ready"

# ---- 3proxy (build once) ---------------------------------------------------
if ! command -v /usr/local/bin/3proxy >/dev/null 2>&1; then
  echo "==> Installing 3proxy from source…"
  pushd /tmp >/dev/null
  rm -rf 3proxy-master master.zip
  wget -q https://github.com/z3APA3A/3proxy/archive/refs/heads/master.zip
  unzip -q master.zip
  cd 3proxy-master
  make -s -f Makefile.Linux
  cp bin/3proxy /usr/local/bin/
  chmod +x /usr/local/bin/3proxy
  popd >/dev/null
fi
echo "✅ 3proxy installed"

# ---- Ensure helper scripts exist (idempotent) ------------------------------
# 4gproxy-net.sh — safe policy routing (no default route change)
cat > "${SCRIPT_DIR}/4gproxy-net.sh" <<'EOSH'
#!/usr/bin/env bash
set -euo pipefail
echo "[4gproxy-net] starting…"
CELL_IFACE=$(ip -o link show | awk -F': ' '{print $2}' | grep -E 'wwan|ppp|usb' | head -n1 || true)
if [[ -z "${CELL_IFACE:-}" ]]; then echo "[4gproxy-net] ERROR: no cellular iface"; exit 1; fi
echo "[4gproxy-net] cellular iface: ${CELL_IFACE}"
PROXY_USER="proxyuser"
id -u "$PROXY_USER" >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin "$PROXY_USER" || true
sysctl -w net.ipv4.ip_forward=1 >/dev/null
TABLE_ID=100; TABLE_NAME="proxy_table"; RT_TABLES="/etc/iproute2/rt_tables"
grep -qE "^[[:space:]]*${TABLE_ID}[[:space:]]+${TABLE_NAME}$" "$RT_TABLES" 2>/dev/null || echo "${TABLE_ID} ${TABLE_NAME}" >> "$RT_TABLES"
ip route replace default dev "${CELL_IFACE}" table "${TABLE_ID}"
ip rule add fwmark 0x1 table "${TABLE_ID}" pref 100 2>/dev/null || true
iptables -t mangle -D OUTPUT -m owner --uid-owner "${PROXY_USER}" -j MARK --set-mark 1 2>/dev/null || true
iptables -t mangle -A OUTPUT -m owner --uid-owner "${PROXY_USER}" -j MARK --set-mark 1
iptables -t nat -C POSTROUTING -o "${CELL_IFACE}" -j MASQUERADE 2>/dev/null || iptables -t nat -A POSTROUTING -o "${CELL_IFACE}" -j MASQUERADE
echo "[4gproxy-net] fwmark 0x1 -> table ${TABLE_ID} via ${CELL_IFACE} active"
EOSH
chmod +x "${SCRIPT_DIR}/4gproxy-net.sh"

# run_3proxy.sh — run 3proxy as proxyuser (for owner match)
cat > "${SCRIPT_DIR}/run_3proxy.sh" <<'EOSH'
#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CFG="${SCRIPT_DIR}/3proxy.cfg"
BIN="/usr/local/bin/3proxy"
PROXY_USER="proxyuser"
command -v "${BIN}" >/dev/null || { echo "3proxy not found at ${BIN}"; exit 1; }
id -u "${PROXY_USER}" >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin "${PROXY_USER}"
# Use sudo to drop to proxyuser (pm2 runs as REAL_USER)
exec sudo -u "${PROXY_USER}" "${BIN}" "${CFG}"
EOSH
chmod +x "${SCRIPT_DIR}/run_3proxy.sh"

# ---- Allow REAL_USER to exec 3proxy as proxyuser (NOPASSWD, minimal scope) -
SUDOERS_FILE="/etc/sudoers.d/3proxy"
if ! grep -q "^${REAL_USER} " "${SUDOERS_FILE}" 2>/dev/null; then
  echo "==> Adding sudoers rule for ${REAL_USER} -> proxyuser (3proxy only)…"
  echo "${REAL_USER} ALL=(proxyuser) NOPASSWD: /usr/local/bin/3proxy" > "${SUDOERS_FILE}"
  chmod 440 "${SUDOERS_FILE}"
fi

# ---- Generate config + 3proxy.cfg + policy routing via Python --------------
echo "==> Running main.py to setup config and network…"
# main.py will:
#  - auto-detect LAN IP, write config.yaml & 3proxy.cfg (no auth by default)
#  - call ./4gproxy-net.sh (policy routing)
#  - write ecosystem.config.js
python3 "${SCRIPT_DIR}/main.py" || true

# ---- ENSURE PM2 is NOT running as root ------------------------------------
echo "==> Ensuring root-PM2 is stopped and cleaned…"
pm2 kill || true
systemctl disable --now pm2-root 2>/dev/null || true
rm -rf /root/.pm2

# ---- Start PM2 as REAL_USER (and enable systemd autostart) -----------------
echo "==> Starting PM2 as ${REAL_USER}…"
sudo -u "${REAL_USER}" pm2 start "${SCRIPT_DIR}/ecosystem.config.js" || true
sudo -u "${REAL_USER}" pm2 save || true

# Generate and run the startup command PM2 expects
START_CMD=$(sudo -u "${REAL_USER}" pm2 startup systemd -u "${REAL_USER}" --hp "${REAL_HOME}" | tail -n 1 | sed 's/^.*PM2.*: //')
# Some PM2 versions output the exact command differently; fallback if empty
if [[ -z "${START_CMD}" ]]; then
  START_CMD="sudo env PATH=$PATH pm2 startup systemd -u ${REAL_USER} --hp ${REAL_HOME} -y"
fi
eval "${START_CMD}" || true

# ---- Gentle wait for the API to boot ---------------------------------------
echo "==> Waiting for API (127.0.0.1:8088)…"
for i in {1..12}; do
  if curl -s --max-time 2 http://127.0.0.1:8088/status >/dev/null; then
    echo "✅ API is up"
    break
  fi
  sleep 1
done

# ---- Tests & summary -------------------------------------------------------
LAN_IP="$(ip -4 addr show wlan0 | awk '/inet /{print $2}' | cut -d/ -f1)"
if [[ -z "${LAN_IP}" ]]; then
  # try eth0 as a fallback bind
  LAN_IP="$(ip -4 addr show eth0 | awk '/inet /{print $2}' | cut -d/ -f1)"
fi

DIRECT_IP="$(curl -s --max-time 5 https://api.ipify.org || echo 'Unknown')"
PROXY_IP="Unknown"
if [[ -n "${LAN_IP}" ]]; then
  PROXY_IP="$(curl -s --max-time 8 -x "http://${LAN_IP}:8080" https://api.ipify.org || echo 'Unknown')"
fi

echo
echo "============================================================"
echo "🎉 SETUP COMPLETE!"
echo "============================================================"
echo "📡 HTTP Proxy: ${LAN_IP:-<detected-LAN>}:8080"
echo "📡 SOCKS Proxy: ${LAN_IP:-<detected-LAN>}:1080"
echo "🌐 Direct (no proxy) Public IP: ${DIRECT_IP}"
echo "🌐 Proxy Public IP: ${PROXY_IP}"
echo "🔧 PM2 (user ${REAL_USER}):  pm2 status | pm2 logs"
echo "⚙️  Edit ${SCRIPT_DIR}/config.yaml for auth, then: pm2 restart 4g-proxy-3proxy"
echo "🧪 Test direct:  curl -s https://api.ipify.org && echo"
echo "🧪 Test proxy :  curl -x http://${LAN_IP}:8080 -s https://api.ipify.org && echo"
echo "============================================================"
