#!/usr/bin/env bash
set -euo pipefail

# rotate_ip.sh — Try to force a fresh public IP by cycling the cellular session.
# Supports PPP, QMI, and RNDIS/ECM via ModemManager.
# Safe to run repeatedly. Requires root.

IP="$(command -v ip || echo /usr/sbin/ip)"
QMICLI="$(command -v qmicli || echo /usr/bin/qmicli)"
MMCLI="$(command -v mmcli || echo /usr/bin/mmcli)"
PPPD="$(command -v pppd || echo /usr/sbin/pppd)"
PKILL="$(command -v pkill || echo /usr/bin/pkill)"
DHCLIENT="$(command -v dhclient || echo /sbin/dhclient)"

echo "[rotate] starting at $(date -Is)"

detect_iface() {
  # Prefer PPP if up
  if $IP -4 addr show ppp0 2>/dev/null | grep -q "inet "; then
    echo "ppp0"; return 0
  fi
  # Try QMI/USB-ethernet style names with IPv4
  for pat in wwan enx usb0 eth1 eth2 eth3; do
    CAND="$($IP -o link show | awk -F': ' '{print $2}' | grep -E "^${pat}" | head -n1 || true)"
    if [[ -n "${CAND}" ]] && $IP -4 addr show "${CAND}" | grep -q "inet "; then
      echo "${CAND}"; return 0
    fi
  done
  return 1
}

CELL_IFACE="$(detect_iface || true)"
echo "[rotate] detected iface: ${CELL_IFACE:-<none>}"

# Get APN from your config.yaml if present
APN="$(grep -E '^[[:space:]]*apn:' config.yaml 2>/dev/null | awk -F: '{print $2}' | xargs || true)"
[[ -z "${APN}" || "${APN}" == "auto" ]] && APN="everywhere"   # sane default; your main.py auto-detects anyway

# Try fast path per interface type
if [[ "${CELL_IFACE:-}" == "ppp0" ]]; then
  echo "[rotate] PPP path: tearing down pppd and redialing…"
  $PKILL pppd 2>/dev/null || true
  sleep 2
  # your main stack uses peer 'carrier'
  $PPPD call carrier &
  # Wait for ppp0 to come back
  for i in {1..90}; do
    sleep 1
    if $IP -4 addr show ppp0 | grep -q "inet "; then
      echo "[rotate] ✅ ppp0 up"
      break
    fi
  done
elif [[ -c /dev/cdc-wdm0 ]]; then
  echo "[rotate] QMI path: stopping/starting WDS for APN=${APN}…"
  # Stop all running WDS sessions (ignore failures), then start a new one
  $QMICLI -d /dev/cdc-wdm0 --wds-stop-network=0 --client-no-release-cid 2>/dev/null || true
  $QMICLI -d /dev/cdc-wdm0 --wds-start-network="apn=${APN}" --client-no-release-cid
  # DHCP renew on the wwan iface, if present
  WWAN_IF="$($IP -br link show | awk '{print $1}' | grep -E '^wwan' | head -n1 || true)"
  if [[ -n "${WWAN_IF}" ]]; then
    $DHCLIENT -r "${WWAN_IF}" 2>/dev/null || true
    $DHCLIENT -v "${WWAN_IF}" || true
  fi
else
  echo "[rotate] RNDIS/ECM path: cycling bearer via ModemManager…"
  # Find modem index (usually 0) and bearer path
  MODEM="$($MMCLI -L | awk -F'[/ ]' '/Modem/ {print $NF; exit}' || echo 0)"
  echo "[rotate] using modem index: ${MODEM}"
  # List bearers and pick the first connected one
  BEARER="$($MMCLI -m ${MODEM} --bearer-list | awk -F'/' '/Bearer/ {print $NF; exit}' 2>/dev/null || true)"
  if [[ -n "${BEARER}" ]]; then
    echo "[rotate] deactivating bearer $BEARER…"
    $MMCLI -b "/org/freedesktop/ModemManager1/Bearer/${BEARER}" -m ${MODEM} --disconnect || true
    sleep 3
  fi
  echo "[rotate] reactivating data (simple-connect)…"
  $MMCLI -m ${MODEM} --simple-connect="apn=${APN}" || true
  # Renew DHCP on the USB-ethernet iface if present
  if [[ -n "${CELL_IFACE:-}" ]]; then
    $DHCLIENT -r "${CELL_IFACE}" 2>/dev/null || true
    $DHCLIENT -v "${CELL_IFACE}" || true
  fi
fi

# Show public IPs (direct and via proxy) for verification
LAN_IP="$(hostname -I | awk '{print $1}')"
PUB1="$(curl -s --max-time 8 https://api.ipify.org || echo unknown)"
PUB2="$(curl -s --max-time 8 -x http://${LAN_IP}:3128 https://api.ipify.org || echo unknown)"

echo "[rotate] direct public: ${PUB1}"
echo "[rotate] proxy  public: ${PUB2}"
echo "[rotate] done at $(date -Is)"
