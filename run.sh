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
DHCLIENT_PATH="$(command -v dhclient || echo /sbin/dhclient)"
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

# -------- apply policy routing to pin Squid to ppp0 --------
echo "==> Pinning Squid egress to ppp0 (policy routing)…"

# Keep existing default (Wi-Fi/Eth) as primary
DEF_GW=$(ip route show default | awk '/default/ {print $3; exit}')
DEF_IF=$(ip route show default | awk '/default/ {print $5; exit}')
if [[ -n "$DEF_GW" && -n "$DEF_IF" ]]; then
  sudo ip route replace default via "$DEF_GW" dev "$DEF_IF" metric 100
fi

# Only proceed if ppp0 exists
if ip -4 addr show ppp0 >/dev/null 2>&1; then
  # Ensure a dedicated routing table for PPP
  grep -q '^100 ppp$' /etc/iproute2/rt_tables || echo '100 ppp' | sudo tee -a /etc/iproute2/rt_tables >/dev/null

  # Default route in the PPP table
  sudo ip route replace default dev ppp0 table ppp

  # Policy rule: packets marked 0x1 use table 'ppp'
  sudo ip rule del fwmark 0x1 lookup ppp 2>/dev/null || true
  sudo ip rule add fwmark 0x1 lookup ppp priority 1000

  # Mark all OUTPUT traffic from Squid user (usually 'proxy') with 0x1
  if command -v nft >/dev/null 2>&1; then
    sudo nft add table inet mangle 2>/dev/null || true
    sudo nft add chain inet mangle output '{ type filter hook output priority mangle ; }' 2>/dev/null || true
    # delete any old rule first
    sudo nft list chain inet mangle output 2>/dev/null | grep -q 'meta skuid "proxy" meta mark set 0x1' \
      && sudo nft delete rule inet mangle output $(sudo nft -a list chain inet mangle output | awk '/meta skuid "proxy" meta mark set 0x1/ {print $(NF)}')
    sudo nft add rule inet mangle output meta skuid "proxy" meta mark set 0x1
  else
    # iptables-legacy compatibility path
    sudo iptables -t mangle -D OUTPUT -m owner --uid-owner proxy -j MARK --set-mark 1 2>/dev/null || true
    sudo iptables -t mangle -A OUTPUT -m owner --uid-owner proxy -j MARK --set-mark 1
  fi

  # Avoid rp_filter dropping asymmetric replies from ppp0
  echo 'net.ipv4.conf.all.rp_filter=2' | sudo tee /etc/sysctl.d/99-ppp-rpf.conf >/dev/null
  sudo sysctl --system >/dev/null || true

  echo "✅ Squid egress pinned to ppp0; Wi-Fi/Eth remains the system default."
else
  echo "⚠️ ppp0 not up yet; policy routing will be applied next run/rotation."
fi

# -------- ensure services are in correct state ------------------------
# ensure Squid is enabled and running (usually auto-starts after install)
systemctl enable --now squid || true

# restart Squid to pick up new configuration (RNDIS routing, etc.)
echo "==> Restarting Squid to apply new configuration..."
systemctl restart squid || true

# Test proxy after Squid restart
echo "==> Testing proxy after configuration..."
LAN_IP="$(ip -4 addr show wlan0 | awk '/inet /{print $2}' | cut -d/ -f1)"
if [[ -z "${LAN_IP}" ]]; then
  LAN_IP="$(ip -4 addr show eth0 | awk '/inet /{print $2}' | cut -d/ -f1)"
fi

if [[ -n "${LAN_IP}" ]]; then
  PROXY_TEST_IP="$(curl -s --max-time 10 -x "http://${LAN_IP}:3128" https://api.ipify.org 2>/dev/null || echo 'Failed')"
  if [[ "${PROXY_TEST_IP}" != "Failed" ]]; then
    echo "✅ Proxy test successful - IP via proxy: ${PROXY_TEST_IP}"
  else
    echo "⚠️ Proxy test failed - check Squid configuration"
  fi
fi

# ensure ModemManager is not holding ports for PPP (main.py stops it, but keep idempotent)
systemctl stop ModemManager || true

# ---- make sure PM2 is NOT running as root --------------------------------
echo "==> Cleaning any root PM2 instance…"
pm2 kill || true
systemctl disable --now pm2-root 2>/dev/null || true
rm -rf /root/.pm2 || true

# ---- start orchestrator with PM2 as REAL_USER -----------------------------
echo "==> Starting PM2 apps as ${REAL_USER}…"

# wipe the user's stale process list/dump to avoid pm_id ghost refs
sudo -u "${REAL_USER}" pm2 delete all || true
sudo -u "${REAL_USER}" pm2 kill || true
rm -f "${REAL_HOME}/.pm2/dump.pm2"

# ensure ecosystem.config.js exists before pm2 start
[[ -f "${SCRIPT_DIR}/ecosystem.config.js" ]] || python3 "${SCRIPT_DIR}/main.py" --ecosystem-only

# brief wait to ensure all services are stable before starting PM2
echo "==> Waiting for services to stabilize..."
sleep 3

# start all apps from ecosystem (orchestrator + web interface)
sudo -u "${REAL_USER}" pm2 start "${SCRIPT_DIR}/ecosystem.config.js"

# save and enable at boot
sudo -u "${REAL_USER}" pm2 save || true
START_CMD=$(sudo -u "${REAL_USER}" pm2 startup systemd -u "${REAL_USER}" --hp "${REAL_HOME}" | tail -n 1 | sed 's/^.*PM2.*: //')
if [[ -z "${START_CMD}" ]]; then
  START_CMD="sudo env PATH=$PATH pm2 startup systemd -u ${REAL_USER} --hp ${REAL_HOME} -y"
fi
eval "${START_CMD}" || true

# brief status
sudo -u "${REAL_USER}" pm2 ls || true

# verify orchestrator is actually responding
echo "==> Verifying orchestrator API..."
for i in {1..10}; do
  if curl -s --max-time 2 http://127.0.0.1:8088/status >/dev/null 2>&1; then
    echo "✅ Orchestrator API responding"
    break
  fi
  if [ $i -eq 10 ]; then
    echo "⚠️ Orchestrator API not responding - may need manual restart"
  fi
  sleep 1
done

# -------- final health check ----------------------------------------------
echo "==> Final API health check..."
if curl -s --max-time 5 http://127.0.0.1:8088/status >/dev/null; then
  echo "✅ API is healthy"
else
  echo "⚠️ API health check failed"
fi

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
  
  # Ensure jq is available for JSON formatting
  if ! command -v jq >/dev/null 2>&1; then
    echo "==> Installing jq for JSON formatting..."
    apt update -qq && apt install -y jq
  fi
  
  TOKEN="$(python3 -c 'import yaml,sys; print(yaml.safe_load(open("config.yaml"))["api"]["token"])')"
  curl -s -X POST -H "Authorization: Bearer ${TOKEN}" http://127.0.0.1:8088/rotate | jq . || true
fi
