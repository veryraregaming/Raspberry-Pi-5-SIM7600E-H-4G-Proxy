#!/usr/bin/env bash
set -euo pipefail

# =====================================================================
# Raspberry-Pi-5-SIM7600E-H-4G-Proxy — One-shot, fully automated setup
# - Cleans old PM2 state
# - Creates sudoers (passwordless/!requiretty) for required commands
# - Installs deps, writes config files, activates PPP/routing via main.py
# - Starts orchestrator under PM2 as REAL_USER (not root)
# - Verifies API + proxy; optionally runs a rotation once
# =====================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# -------- resolve the real login user that will own PM2 ---------------
REAL_USER="${SUDO_USER:-$USER}"
REAL_HOME="$(getent passwd "$REAL_USER" | cut -d: -f6)"
if [[ -z "${REAL_HOME}" || ! -d "${REAL_HOME}" ]]; then
  echo "Could not determine home directory for REAL_USER=$REAL_USER"
  exit 1
fi

echo "==> Running as root for setup; PM2 will run as user: ${REAL_USER} (${REAL_HOME})"

# -------- kill any lingering PM2/app processes (idempotent) ----------
echo "==> Cleaning up old PM2 processes..."
pm2 delete 4g-proxy-orchestrator 2>/dev/null || true
pm2 delete 4g-proxy-web 2>/dev/null || true
pm2 kill 2>/dev/null || true
echo "✅ Old PM2 processes cleaned up"

# -------- state directory (owned by REAL_USER) ------------------------
echo "==> Preparing state directory..."
mkdir -p state
chown -R "${REAL_USER}:${REAL_USER}" state
chmod 755 state
echo "✅ state/ ready"

# -------- discover exact command paths (for sudoers correctness) -----
echo "==> Resolving command paths for sudoers..."
PKILL_PATH="$(command -v pkill || echo /usr/bin/pkill)"
PPPD_PATH="$(command -v pppd || echo /usr/sbin/pppd)"
IP_PATH="$(command -v ip || echo /usr/sbin/ip)"
SYSTEMCTL_PATH="$(command -v systemctl || echo /bin/systemctl)"
MMCLI_PATH="$(command -v mmcli || echo /usr/bin/mmcli)"
SUDO_PATH="$(command -v sudo || echo /usr/bin/sudo)"
echo "   pkill=${PKILL_PATH}"
echo "   pppd=${PPPD_PATH}"
echo "   ip=${IP_PATH}"
echo "   systemctl=${SYSTEMCTL_PATH}"
echo "   mmcli=${MMCLI_PATH}"
echo "   sudo=${SUDO_PATH}"

# -------- write sudoers with NOPASSWD + !requiretty ------------------
echo "==> Writing /etc/sudoers.d/4g-proxy..."
cat >/etc/sudoers.d/4g-proxy <<EOF
# 4G Proxy sudoers for ${REAL_USER}
Cmnd_Alias PROXY_CMDS = \\
  ${PKILL_PATH} pppd, \\
  ${PPPD_PATH} *, \\
  ${IP_PATH} route del default, \\
  ${IP_PATH} route add default dev ppp0 metric 200, \\
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

# -------- base packages (idempotent) ---------------------------------
echo "==> Installing base packages…"
apt-get update -y
DEBS=(
  curl wget unzip build-essential iptables
  python3 python3-pip python3-yaml python3-serial python3-requests python3-flask
  ca-certificates gnupg modemmanager ppp
  squid
)
apt-get install -y "${DEBS[@]}"

# -------- Node + PM2 -------------------------------------------------
if ! command -v node >/dev/null 2>&1; then
  echo "==> Installing Node.js 18.x…"
  curl -fsSL https://deb.nodesource.com/setup_18.x | bash -
  apt-get install -y nodejs
fi
if ! command -v pm2 >/dev/null 2>&1; then
  echo "==> Installing PM2 globally…"
  npm install -g pm2
fi
echo "✅ PM2 installed"

# -------- secure the config before main.py runs ----------------------
# If config.yaml exists and token is a placeholder, replace it now.
echo "==> Ensuring config.yaml has a secure token (if present)…"
if [[ -f "${SCRIPT_DIR}/config.yaml" ]]; then
  if grep -q 'token: your-secure-random-token-here' "${SCRIPT_DIR}/config.yaml"; then
    NEW_TOKEN="$(python3 - <<'PY'
import secrets; print(secrets.token_urlsafe(64))
PY
)"
    sed -i "s|token: your-secure-random-token-here|token: ${NEW_TOKEN}|" "${SCRIPT_DIR}/config.yaml"
    echo "   -> Replaced placeholder API token"
  fi
fi

# -------- run main.py (writes config, squid.conf, ecosystem, PPP up) -
echo "==> Running main.py to write configs and bring PPP up…"
python3 "${SCRIPT_DIR}/main.py" || true

# -------- ensure ModemManager is not holding ports for PPP -----------
# (main.py stops it already, but we keep it idempotent here)
systemctl stop ModemManager || true

# -------- make sure PM2 is not running as root and is clean ----------
echo "==> Cleaning any root PM2 instance…"
pm2 kill || true
systemctl disable --now pm2-root 2>/dev/null || true
rm -rf /root/.pm2 || true

# -------- start orchestrator with PM2 as REAL_USER -------------------
echo "==> Starting PM2 apps as ${REAL_USER}…"
sudo -u "${REAL_USER}" pm2 start "${SCRIPT_DIR}/ecosystem.config.js" || true
sudo -u "${REAL_USER}" pm2 save || true

# Enable PM2 at boot for REAL_USER
START_CMD=$(sudo -u "${REAL_USER}" pm2 startup systemd -u "${REAL_USER}" --hp "${REAL_HOME}" | tail -n 1 | sed 's/^.*PM2.*: //')
if [[ -z "${START_CMD}" ]]; then
  START_CMD="sudo env PATH=$PATH pm2 startup systemd -u ${REAL_USER} --hp ${REAL_HOME} -y"
fi
eval "${START_CMD}" || true

# -------- health checks ----------------------------------------------
echo "==> Waiting for API (127.0.0.1:8088)…"
for i in {1..20}; do
  if curl -s --max-time 2 http://127.0.0.1:8088/status >/dev/null; then
    echo "✅ API is up"
    break
  fi
  sleep 1
done

LAN_IP="$(ip -4 addr show wlan0 | awk '/inet /{print $2}' | cut -d/ -f1)"
if [[ -z "${LAN_IP}" ]]; then
  LAN_IP="$(ip -4 addr show eth0 | awk '/inet /{print $2}' | cut -d/ -f1)"
fi

DIRECT_IP="$(curl -s --max-time 8 https://api.ipify.org || echo 'Unknown')"
PROXY_IP="Unknown"
if [[ -n "${LAN_IP}" ]]; then
  PROXY_IP="$(curl -s --max-time 10 -x "http://${LAN_IP}:3128" https://api.ipify.org || echo 'Unknown')"
fi

echo
echo "============================================================"
echo "🎉 SETUP COMPLETE!"
echo "============================================================"
echo "📡 HTTP Proxy: ${LAN_IP:-<LAN>}:3128"
echo "🌐 Direct IP: ${DIRECT_IP}"
echo "🌐 Proxy IP:  ${PROXY_IP}"
echo "📊 API:       http://127.0.0.1:8088"
echo "PM2 (user):   ${REAL_USER}"
echo "============================================================"

# -------- optional: one-shot rotation on first install --------------
# Set RUN_ROTATE=1 in the environment to force a single rotation now.
if [[ "${RUN_ROTATE:-0}" == "1" ]]; then
  echo "==> Triggering one-shot rotation (RUN_ROTATE=1)…"
  TOKEN="$(python3 -c 'import yaml,sys; print(yaml.safe_load(open("config.yaml"))["api"]["token"])')"
  curl -s -X POST -H "Authorization: Bearer ${TOKEN}" http://127.0.0.1:8088/rotate | jq . || true
fi
