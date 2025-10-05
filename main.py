#!/usr/bin/env bash
set -euo pipefail

# --- cleanup old PM2 processes first ---
echo "==> Cleaning up old PM2 processes..."
pm2 delete 4g-proxy-squid 2>/dev/null || true
pm2 delete 4g-proxy-3proxy 2>/dev/null || true
pm2 delete 4g-proxy 2>/dev/null || true
pm2 delete 4g-proxy-auto-rotate 2>/dev/null || true
pm2 kill 2>/dev/null || true
echo "✅ Old PM2 processes cleaned up"

# --- define user variables early ---
REAL_USER="${SUDO_USER:-$USER}"
REAL_HOME="$(getent passwd "$REAL_USER" | cut -d: -f6)"
if [[ -z "${REAL_HOME}" || ! -d "${REAL_HOME}" ]]; then
  echo "Could not determine home directory for REAL_USER=$REAL_USER"
  exit 1
fi

echo "==> Running as root for setup; PM2 will run as user: ${REAL_USER} (${REAL_HOME})"

# --- create state directory with proper permissions ---
echo "==> Setting up state directory..."
mkdir -p state
chown -R ${REAL_USER}:${REAL_USER} state 2>/dev/null || true
chmod 755 state
echo "✅ State directory created"

# --- setup sudoers for PPP and routing commands ---
echo "==> Setting up sudoers for PPP and routing..."

# Find full paths for commands (they matter in sudoers)
PKILL_PATH=$(which pkill || echo /usr/bin/pkill)
PPPD_PATH=$(which pppd || echo /usr/sbin/pppd)
IP_PATH=$(which ip || echo /usr/sbin/ip)

echo "  📍 Command paths: pkill=$PKILL_PATH, pppd=$PPPD_PATH, ip=$IP_PATH"

# Create comprehensive sudoers rule with command aliases and !requiretty
cat >/etc/sudoers.d/4g-proxy <<EOF
# 4G Proxy sudoers rule for ${REAL_USER}
Cmnd_Alias PROXY_CMDS = \\
  ${PKILL_PATH} pppd, \\
  ${PPPD_PATH} *, \\
  ${IP_PATH} route del default, \\
  ${IP_PATH} route add default dev ppp0 metric 200

${REAL_USER} ALL=(root) NOPASSWD: PROXY_CMDS
Defaults:${REAL_USER} !requiretty
EOF

chmod 0440 /etc/sudoers.d/4g-proxy
if visudo -c >/dev/null 2>&1; then
  echo "✅ Sudoers configured"
else
  echo "⚠️  Sudoers validation failed"
fi

# --- ensure networking/DNS available before anything else ---
echo "==> Checking internet connectivity..."
TRIES=0
while ! ping -c1 -W1 1.1.1.1 >/dev/null 2>&1; do
  TRIES=$((TRIES+1))
  if [ "$TRIES" -gt 15 ]; then
    echo "⚠️  Network still unreachable after 15s, continuing anyway"
    break
  fi
  sleep 1
done

# ensure at least one usable resolver
RESOLV_OK=$(grep -E 'nameserver' /etc/resolv.conf 2>/dev/null | wc -l || echo 0)
if [ "$RESOLV_OK" -lt 1 ]; then
  echo "==> No resolvers found; setting temporary Cloudflare/Google DNS"
  printf 'nameserver 1.1.1.1\nnameserver 8.8.8.8\n' >/etc/resolv.conf
fi

# Optional: try to nudge systemd-resolved if available
if ! curl -fsSL --max-time 5 https://deb.nodesource.com >/dev/null 2>&1; then
  echo "==> DNS may be lagging; attempting resolvectl (best-effort)…"
  resolvectl dns wlan0 1.1.1.1 9.9.9.9 8.8.8.8 2>/dev/null || true
  resolvectl domain wlan0 ~. 2>/dev/null || true
  resolvectl flush-caches 2>/dev/null || true
  sleep 2
fi

# ============================================================================
# One-shot installer/runner for Raspberry-Pi-5-SIM7600E-H-4G-Proxy
# ============================================================================

# ---- guardrails -------------------------------------------------------------
if [[ "$EUID" -ne 0 ]]; then
  echo "Please run as root: sudo ./run.sh"
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ---- apt & tools ------------------------------------------------------------
echo "==> Installing base packages…"
apt-get update -y
DEBS=(
  curl wget unzip build-essential iptables python3 python3-pip
  python3-yaml python3-serial python3-requests python3-flask
  ca-certificates gnupg modemmanager
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

# ---- Squid (reliable proxy) -------------------------------------------------
if ! command -v squid >/dev/null 2>&1; then
  echo "==> Installing Squid proxy…"
  apt-get install -y squid
fi
echo "✅ Squid installed"

# ---- Ensure helper scripts exist (idempotent) ------------------------------
if [[ ! -f "${SCRIPT_DIR}/apn.txt" ]]; then
  echo "==> Creating apn.txt with common APNs…"
  cat > "${SCRIPT_DIR}/apn.txt" <<'EOF'
# APN list (one per line)
everywhere
internet
web
data
broadband
mobile
3gnet
fast.t-mobile.com
wap
gprs
mms
hsdpa
umts
lte
EOF
fi

# Create/refresh run_squid.sh (runs squid as proxyuser)
cat > "${SCRIPT_DIR}/run_squid.sh" <<'EOSH'
#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CFG="${SCRIPT_DIR}/squid.conf"
PROXY_USER="proxyuser"
command -v squid >/dev/null || { echo "squid not found"; exit 1; }
id -u "${PROXY_USER}" >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin "${PROXY_USER}"
exec sudo -u "${PROXY_USER}" squid -N -f "${CFG}"
EOSH
chmod +x "${SCRIPT_DIR}/run_squid.sh"

# ---- Allow REAL_USER to exec squid as proxyuser (NOPASSWD, minimal scope) --
SUDOERS_FILE="/etc/sudoers.d/squid"
if ! grep -q "^${REAL_USER} " "${SUDOERS_FILE}" 2>/dev/null; then
  echo "==> Adding sudoers rule for ${REAL_USER} -> proxyuser (squid only)…"
  echo "${REAL_USER} ALL=(proxyuser) NOPASSWD: /usr/sbin/squid" > "${SUDOERS_FILE}"
  chmod 440 "${SUDOERS_FILE}"
fi

# ---- Generate config, ensure modem, write ecosystem, etc. -------------------
echo "==> Running main.py to setup config and network…"
python3 "${SCRIPT_DIR}/main.py" || true

echo "==> Ensuring cellular interface is ready…"
sleep 3

# Try to bring up a likely cellular iface
if ip link show wwan0 >/dev/null 2>&1; then
  ip link set wwan0 up || true
  sleep 2
fi

# Discover a cellular-like interface for diagnostics
CELL_IFACE=""
for pattern in 'wwan' 'ppp' 'usb' 'eth1' 'eth2' 'eth3' 'enx' 'cdc'; do
  CELL_IFACE=$(ip -o link show | awk -F': ' '{print $2}' | grep -E "^${pattern}" | head -n1 || true)
  [[ -n "${CELL_IFACE:-}" ]] || continue
  if [[ "${CELL_IFACE}" == "eth0" || "${CELL_IFACE}" == "wlan0" ]]; then
    CELL_IFACE=""
    continue
  fi
  if ip link show "${CELL_IFACE}" | grep -qE "state (UP|UNKNOWN)"; then
    echo "==> Found cellular interface: ${CELL_IFACE}"
    break
  fi
  CELL_IFACE=""
done

if [[ -z "${CELL_IFACE:-}" ]]; then
  echo "⚠️  No cellular interface found - proxy may use home network"
else
  echo "✅ Cellular interface ready: ${CELL_IFACE}"
fi

# ---- ENSURE PM2 is NOT running as root ------------------------------------
echo "==> Ensuring root-PM2 is stopped and cleaned…"
pm2 kill || true
systemctl disable --now pm2-root 2>/dev/null || true
rm -rf /root/.pm2

# ---- Start PM2 as REAL_USER (and enable systemd autostart) -----------------
echo "==> Starting PM2 as ${REAL_USER}…"
sudo -u "${REAL_USER}" pm2 start "${SCRIPT_DIR}/ecosystem.config.js" || true
sudo -u "${REAL_USER}" pm2 save || true

START_CMD=$(sudo -u "${REAL_USER}" pm2 startup systemd -u "${REAL_USER}" --hp "${REAL_HOME}" | tail -n 1 | sed 's/^.*PM2.*: //')
if [[ -z "${START_CMD}" ]]; then
  START_CMD="sudo env PATH=$PATH pm2 startup systemd -u ${REAL_USER} --hp ${REAL_HOME} -y"
fi
eval "${START_CMD}" || true

# ---- Wait for services to start and verify ---------------------------------
echo "==> Waiting for services to start…"
sleep 5

# Check Squid
if ss -ltnp 2>/dev/null | grep -q ":3128"; then
  echo "✅ Squid proxy is running on port 3128"
else
  echo "⚠️  Squid proxy not detected on port 3128, attempting restart..."
  chmod +x "${SCRIPT_DIR}/run_squid.sh" 2>/dev/null || true
  sudo -u "${REAL_USER}" pm2 restart all || true
  sleep 3
  if ss -ltnp 2>/dev/null | grep -q ":3128"; then
    echo "✅ Squid proxy is now running on port 3128"
  else
    echo "⚠️  Squid proxy still not running, continuing..."
  fi
fi

# Check API
if ss -ltnp 2>/dev/null | grep -q ":8088"; then
  echo "✅ API server is running on port 8088"
else
  echo "⚠️  API server not detected on port 8088"
fi

# Wait for API readiness
echo "==> Waiting for API (127.0.0.1:8088)…"
for i in {1..12}; do
  if curl -s --max-time 2 http://127.0.0.1:8088/status >/dev/null; then
    echo "✅ API is up"
    break
  fi
  sleep 1
done

# Summary
LAN_IP="$(ip -4 addr show wlan0 | awk '/inet /{print $2}' | cut -d/ -f1)"
if [[ -z "${LAN_IP}" ]]; then
  LAN_IP="$(ip -4 addr show eth0 | awk '/inet /{print $2}' | cut -d/ -f1)"
fi

DIRECT_IP="$(curl -s --max-time 5 https://api.ipify.org || echo 'Unknown')"
PROXY_IP="Unknown"
if [[ -n "${LAN_IP}" ]]; then
  PROXY_IP="$(curl -s --max-time 8 -x "http://${LAN_IP}:3128" https://api.ipify.org || echo 'Unknown')"
fi

if [[ "${PROXY_IP}" != "Unknown" && "${PROXY_IP}" != "${DIRECT_IP}" ]]; then
  NETWORK_TYPE="Cellular (SIM card)"
else
  NETWORK_TYPE="Home network (fallback)"
fi

echo
echo "============================================================"
echo "🎉 SETUP COMPLETE!"
echo "============================================================"
echo "📡 HTTP/HTTPS Proxy: ${LAN_IP:-<detected-LAN>}:3128"
echo "🌐 Direct (no proxy) Public IP: ${DIRECT_IP}"
echo "🌐 Proxy Public IP: ${PROXY_IP}"
echo "📶 Network Type: ${NETWORK_TYPE}"
echo ""
echo "🔧 Management Commands:"
echo "  pm2 status                    # View service status"
echo "  pm2 logs                      # View all logs"
echo "  pm2 restart 4g-proxy-squid    # Restart proxy"
echo "  pm2 restart all               # Restart all services"
echo ""
echo "⚙️  Configuration:"
echo "  Edit ${SCRIPT_DIR}/config.yaml for APN/auth settings"
echo "  Then: pm2 restart 4g-proxy-squid"
echo ""
echo "🧪 Test Commands:"
echo "  curl -s https://api.ipify.org && echo"
echo "  curl -x http://${LAN_IP}:3128 -s https://api.ipify.org && echo"
echo "============================================================"
