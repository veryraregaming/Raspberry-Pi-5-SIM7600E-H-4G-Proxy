#!/usr/bin/env bash
# ============================================================
# Cellular Interface Keepalive Service
# Monitors cellular interface and keeps it UP and connected
# ============================================================

set -euo pipefail

SCRIPT_NAME="cellular-keepalive"
LOG_TAG="[${SCRIPT_NAME}]"

# Paths
IP_BIN="$(command -v ip || echo /usr/sbin/ip)"
DHCLIENT_BIN="$(command -v dhclient || echo /sbin/dhclient)"
UDHCPC_BIN="$(command -v udhcpc || echo /sbin/udhcpc)"
QMICLI_BIN="$(command -v qmicli || echo /usr/bin/qmicli)"

# Configuration
CHECK_INTERVAL=30  # Check every 30 seconds
MAX_RETRIES=3
PING_TIMEOUT=5

# Logging
log() {
    echo "${LOG_TAG} $(date '+%Y-%m-%d %H:%M:%S') $*" | tee -a /var/log/cellular-keepalive.log
}

log_err() {
    echo "${LOG_TAG} ERROR: $*" >&2 | tee -a /var/log/cellular-keepalive.log
}

# Detect cellular interface
detect_cellular_interface() {
    # Priority: wwan0 > enx* > usb0 > eth1
    for prefix in wwan enx usb0 eth1; do
        iface=$($IP_BIN -o link show | awk -F': ' '{print $2}' | grep "^${prefix}" | head -n1 || true)
        if [[ -n "${iface}" ]]; then
            echo "${iface}"
            return 0
        fi
    done
    
    # Check for ppp0 as last resort
    if $IP_BIN link show ppp0 >/dev/null 2>&1; then
        echo "ppp0"
        return 0
    fi
    
    return 1
}

# Check if interface is UP
is_interface_up() {
    local iface="$1"
    $IP_BIN link show "${iface}" 2>/dev/null | grep -q "state UP"
}

# Check if interface has IP
has_ip_address() {
    local iface="$1"
    $IP_BIN -4 addr show "${iface}" 2>/dev/null | grep -q "inet "
}

# Get gateway for interface
get_gateway() {
    local iface="$1"
    
    # Try to get from routing table
    local gw=$($IP_BIN route show dev "${iface}" 2>/dev/null | awk '/default/ {print $3}' | head -n1 || true)
    
    if [[ -n "${gw}" ]]; then
        echo "${gw}"
        return 0
    fi
    
    # For RNDIS, try standard gateway
    if [[ "${iface}" == enx* ]] || [[ "${iface}" == eth1 ]]; then
        echo "192.168.225.1"
        return 0
    fi
    
    # For QMI/wwan, try to detect
    if [[ "${iface}" == wwan* ]]; then
        gw=$($IP_BIN route | awk "/^default.*${iface}/ {print \$3}" | head -n1 || true)
        if [[ -n "${gw}" ]]; then
            echo "${gw}"
            return 0
        fi
    fi
    
    return 1
}

# Test internet connectivity
test_connectivity() {
    local iface="$1"
    
    # Try to ping Google DNS through this interface
    if ping -I "${iface}" -c 1 -W "${PING_TIMEOUT}" 8.8.8.8 >/dev/null 2>&1; then
        return 0
    fi
    
    # Also try Cloudflare
    if ping -I "${iface}" -c 1 -W "${PING_TIMEOUT}" 1.1.1.1 >/dev/null 2>&1; then
        return 0
    fi
    
    return 1
}

# Bring interface UP
bring_interface_up() {
    local iface="$1"
    log "Bringing ${iface} UP..."
    $IP_BIN link set "${iface}" up || {
        log_err "Failed to bring ${iface} UP"
        return 1
    }
    sleep 2
    return 0
}

# Renew DHCP lease
renew_dhcp() {
    local iface="$1"
    local mode="${2:-dhclient}"  # dhclient or udhcpc
    
    log "Renewing DHCP lease for ${iface} (mode: ${mode})..."
    
    # Kill existing DHCP clients for this interface
    pkill -f "dhclient.*${iface}" 2>/dev/null || true
    pkill -f "udhcpc.*${iface}" 2>/dev/null || true
    sleep 1
    
    # Start DHCP client
    if [[ "${mode}" == "udhcpc" ]] && command -v udhcpc >/dev/null 2>&1; then
        $UDHCPC_BIN -i "${iface}" -q -n >/dev/null 2>&1 || {
            log_err "udhcpc failed for ${iface}"
            return 1
        }
    else
        $DHCLIENT_BIN -v "${iface}" >/dev/null 2>&1 || {
            log_err "dhclient failed for ${iface}"
            return 1
        }
    fi
    
    sleep 3
    return 0
}

# Restart QMI connection
restart_qmi() {
    local iface="$1"
    local qmi_dev="/dev/cdc-wdm0"
    
    if [[ ! -e "${qmi_dev}" ]]; then
        log_err "QMI device ${qmi_dev} not found"
        return 1
    fi
    
    log "Restarting QMI connection for ${iface}..."
    
    # Stop existing connection
    $QMICLI_BIN -d "${qmi_dev}" --wds-stop-network disable-autoconnect --client-no-release-cid >/dev/null 2>&1 || true
    sleep 2
    
    # Bring interface down then up
    $IP_BIN link set "${iface}" down
    sleep 2
    $IP_BIN link set "${iface}" up
    sleep 3
    
    # Start new connection
    $QMICLI_BIN -d "${qmi_dev}" --wds-start-network "apn=everywhere" --client-no-release-cid >/dev/null 2>&1 || {
        log_err "QMI network start failed"
        return 1
    }
    
    sleep 3
    
    # Get IP via DHCP
    renew_dhcp "${iface}" "udhcpc"
    return $?
}

# Update cellular routing table
update_cellular_table() {
    local iface="$1"
    local gw
    
    # Ensure cellular table exists
    if ! grep -qE "^[[:space:]]*100[[:space:]]+cellular$" /etc/iproute2/rt_tables 2>/dev/null; then
        echo "100 cellular" >> /etc/iproute2/rt_tables
    fi
    
    # Get gateway
    if ! gw=$(get_gateway "${iface}"); then
        log_err "Could not determine gateway for ${iface}"
        return 1
    fi
    
    log "Updating cellular table: default via ${gw} dev ${iface}"
    
    # Update route in cellular table
    if [[ "${iface}" == ppp0 ]]; then
        $IP_BIN route replace default dev ppp0 table cellular 2>/dev/null || return 1
    else
        $IP_BIN route replace default via "${gw}" dev "${iface}" table cellular 2>/dev/null || return 1
    fi
    
    # Ensure policy rule exists
    if ! $IP_BIN rule show | grep -q "fwmark 0x1 lookup cellular"; then
        $IP_BIN rule add fwmark 0x1 table cellular pref 100 2>/dev/null || true
    fi
    
    return 0
}

# Main recovery function
recover_interface() {
    local iface="$1"
    local retry=0
    
    log "Starting recovery for ${iface}..."
    
    while [[ $retry -lt $MAX_RETRIES ]]; do
        retry=$((retry + 1))
        log "Recovery attempt ${retry}/${MAX_RETRIES}..."
        
        # Step 1: Bring interface UP if DOWN
        if ! is_interface_up "${iface}"; then
            bring_interface_up "${iface}" || {
                sleep 5
                continue
            }
        fi
        
        # Step 2: Ensure it has an IP
        if ! has_ip_address "${iface}"; then
            log "Interface ${iface} has no IP, requesting DHCP..."
            
            # For QMI/wwan interfaces, use QMI restart
            if [[ "${iface}" == wwan* ]] && command -v qmicli >/dev/null 2>&1; then
                restart_qmi "${iface}" || {
                    sleep 5
                    continue
                }
            else
                # For RNDIS, use DHCP
                renew_dhcp "${iface}" || {
                    sleep 5
                    continue
                }
            fi
        fi
        
        # Step 3: Test connectivity
        log "Testing connectivity for ${iface}..."
        if test_connectivity "${iface}"; then
            log "✅ Connectivity restored for ${iface}"
            
            # Update routing table
            update_cellular_table "${iface}" || {
                log_err "Failed to update cellular table"
            }
            
            return 0
        else
            log_err "Connectivity test failed for ${iface}"
            sleep 5
        fi
    done
    
    log_err "Recovery failed after ${MAX_RETRIES} attempts"
    return 1
}

# Main monitoring loop
main() {
    log "Starting cellular keepalive service..."
    log "Check interval: ${CHECK_INTERVAL} seconds"
    
    local consecutive_failures=0
    local max_consecutive_failures=10
    
    while true; do
        # Detect cellular interface
        if ! CELL_IFACE=$(detect_cellular_interface); then
            if [[ $consecutive_failures -eq 0 ]]; then
                log "⚠️  No cellular interface found, waiting for it to appear..."
            fi
            consecutive_failures=$((consecutive_failures + 1))
            
            if [[ $consecutive_failures -ge $max_consecutive_failures ]]; then
                log_err "No cellular interface found after ${max_consecutive_failures} checks, giving up"
                exit 1
            fi
            
            sleep "${CHECK_INTERVAL}"
            continue
        fi
        
        # Reset consecutive failures counter
        consecutive_failures=0
        
        # Check interface status
        if ! is_interface_up "${CELL_IFACE}"; then
            log "⚠️  Interface ${CELL_IFACE} is DOWN"
            recover_interface "${CELL_IFACE}"
        elif ! has_ip_address "${CELL_IFACE}"; then
            log "⚠️  Interface ${CELL_IFACE} has no IP address"
            recover_interface "${CELL_IFACE}"
        elif ! test_connectivity "${CELL_IFACE}"; then
            log "⚠️  Interface ${CELL_IFACE} has no internet connectivity"
            recover_interface "${CELL_IFACE}"
        else
            # All good, just ensure routing table is correct
            if ! $IP_BIN route show table cellular | grep -q "default"; then
                log "⚠️  Cellular routing table is empty, updating..."
                update_cellular_table "${CELL_IFACE}"
            fi
        fi
        
        # Wait before next check
        sleep "${CHECK_INTERVAL}"
    done
}

# Run main loop
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    # Create log file if it doesn't exist
    touch /var/log/cellular-keepalive.log || true
    chmod 644 /var/log/cellular-keepalive.log || true
    
    main "$@"
fi

