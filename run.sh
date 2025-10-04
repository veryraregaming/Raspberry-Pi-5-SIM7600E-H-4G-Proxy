#!/usr/bin/env bash
set -euo pipefail

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

# ensure at least one usable resolver (for early Ubuntu netplans that miss it)
RESOLV_OK=$(grep -E 'nameserver' /etc/resolv.conf 2>/dev/null | wc -l)
if [ "$RESOLV_OK" -lt 1 ]; then
  echo "==> No resolvers found; setting temporary Cloudflare DNS"
  echo -e "nameserver 1.1.1.1\nnameserver 8.8.8.8" | sudo tee /etc/resolv.conf >/dev/null
fi

# quick DNS test with fallback resolvers
if ! curl -fsSL --max-time 5 https://deb.nodesource.com >/dev/null 2>&1; then
  echo "==> DNS may be lagging; adding fallback resolvers via resolvectl..."
  sudo resolvectl dns wlan0 1.1.1.1 9.9.9.9 8.8.8.8 2>/dev/null || true
  sudo resolvectl domain wlan0 ~. 2>/dev/null || true
  sudo resolvectl flush-caches 2>/dev/null || true
  sleep 2
fi

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

# ---- Squid (reliable proxy) -------------------------------------------------
if ! command -v squid >/dev/null 2>&1; then
  echo "==> Installing Squid proxy…"
  apt-get install -y squid
fi
echo "✅ Squid installed"

# ---- Ensure helper scripts exist (idempotent) ------------------------------
# 4gproxy-net.sh — safe policy routing (no default route change)
cat > "${SCRIPT_DIR}/4gproxy-net.sh" <<'EOSH'
#!/usr/bin/env bash
set -euo pipefail
echo "[4gproxy-net] starting…"
echo "[4gproxy-net] Available interfaces:"
ip -o link show | awk -F': ' '{print $2}' | grep -v lo

# Try multiple patterns for cellular interfaces (EXCLUDING eth0/wlan0)
CELL_IFACE=""
for pattern in 'wwan' 'ppp' 'usb' 'eth1' 'eth2' 'eth3' 'enx' 'cdc'; do
  CELL_IFACE=$(ip -o link show | awk -F': ' '{print $2}' | grep -E "^${pattern}" | head -n1 || true)
  if [[ -n "${CELL_IFACE:-}" ]]; then
    # CRITICAL: Never use eth0 or wlan0 (home network interfaces)
    if [[ "${CELL_IFACE}" == "eth0" || "${CELL_IFACE}" == "wlan0" ]]; then
      echo "[4gproxy-net] Skipping ${CELL_IFACE} (home network interface)"
      CELL_IFACE=""
      continue
    fi
    # Verify this interface has an IP address (indicating it's active)
    if ip addr show "${CELL_IFACE}" | grep -q "inet "; then
      echo "[4gproxy-net] Found active cellular interface: ${CELL_IFACE} (pattern: ${pattern})"
      break
    else
      echo "[4gproxy-net] Found interface ${CELL_IFACE} but no IP assigned, trying next..."
      CELL_IFACE=""
    fi
  fi
done

if [[ -z "${CELL_IFACE:-}" ]]; then
  echo "[4gproxy-net] ERROR: no active cellular interface found."
  echo "[4gproxy-net] Tried patterns: wwan, ppp, usb, eth1, eth2, eth3, enx, cdc"
  echo "[4gproxy-net] Available interfaces with IPs:"
  ip -o addr show | grep "inet " | grep -v "127.0.0.1" | awk '{print $2}' | cut -d: -f1
  exit 1
fi
echo "[4gproxy-net] Using cellular interface: ${CELL_IFACE}"
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

# run_squid.sh — run squid as proxyuser (for owner match)
cat > "${SCRIPT_DIR}/run_squid.sh" <<'EOSH'
#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CFG="${SCRIPT_DIR}/squid.conf"
PROXY_USER="proxyuser"
command -v squid >/dev/null || { echo "squid not found"; exit 1; }
id -u "${PROXY_USER}" >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin "${PROXY_USER}"
# Use sudo to drop to proxyuser (pm2 runs as REAL_USER)
exec sudo -u "${PROXY_USER}" squid -N -f "${CFG}"
EOSH
chmod +x "${SCRIPT_DIR}/run_squid.sh"

# ---- Allow REAL_USER to exec squid as proxyuser (NOPASSWD, minimal scope) -
SUDOERS_FILE="/etc/sudoers.d/squid"
if ! grep -q "^${REAL_USER} " "${SUDOERS_FILE}" 2>/dev/null; then
  echo "==> Adding sudoers rule for ${REAL_USER} -> proxyuser (squid only)…"
  echo "${REAL_USER} ALL=(proxyuser) NOPASSWD: /usr/sbin/squid" > "${SUDOERS_FILE}"
  chmod 440 "${SUDOERS_FILE}"
fi

# ---- Generate config + squid.conf + policy routing via Python --------------
echo "==> Running main.py to setup config and network…"
# main.py will:
#  - auto-detect LAN IP, write config.yaml & squid.conf (no auth by default)
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
echo "📡 HTTP Proxy: ${LAN_IP:-<detected-LAN>}:3128"
echo "📡 HTTPS Proxy: ${LAN_IP:-<detected-LAN>}:3128"
echo "🌐 Direct (no proxy) Public IP: ${DIRECT_IP}"
echo "🌐 Proxy Public IP: ${PROXY_IP}"
echo "🔧 PM2 (user ${REAL_USER}):  pm2 status | pm2 logs"
echo "⚙️  Edit ${SCRIPT_DIR}/config.yaml for auth, then: pm2 restart 4g-proxy-squid"
echo "🧪 Test direct:  curl -s https://api.ipify.org && echo"
echo "🧪 Test proxy :  curl -x http://${LAN_IP}:3128 -s https://api.ipify.org && echo"
echo "============================================================"
