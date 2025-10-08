#!/usr/bin/env bash
# ============================================================
# Comprehensive Proxy Test & Diagnostic Script
# Tests all aspects of the 4G proxy setup
# ============================================================

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Colour

echo -e "${BLUE}╔════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║   4G Proxy Comprehensive Diagnostic Test      ║${NC}"
echo -e "${BLUE}╚════════════════════════════════════════════════╝${NC}"
echo ""

# Paths
IP_BIN="$(command -v ip || echo /usr/sbin/ip)"
IPTABLES_BIN="$(command -v iptables || echo /usr/sbin/iptables)"
SS_BIN="$(command -v ss || echo /usr/sbin/ss)"

# Test counters
TOTAL_TESTS=0
PASSED_TESTS=0
FAILED_TESTS=0
WARNINGS=0

# Test result tracking
test_result() {
    local test_name="$1"
    local result="$2"
    local message="${3:-}"
    
    TOTAL_TESTS=$((TOTAL_TESTS + 1))
    
    if [[ "${result}" == "PASS" ]]; then
        echo -e "${GREEN}✅ PASS${NC}: ${test_name}"
        PASSED_TESTS=$((PASSED_TESTS + 1))
    elif [[ "${result}" == "FAIL" ]]; then
        echo -e "${RED}❌ FAIL${NC}: ${test_name}"
        [[ -n "${message}" ]] && echo -e "   ${RED}→${NC} ${message}"
        FAILED_TESTS=$((FAILED_TESTS + 1))
    elif [[ "${result}" == "WARN" ]]; then
        echo -e "${YELLOW}⚠️  WARN${NC}: ${test_name}"
        [[ -n "${message}" ]] && echo -e "   ${YELLOW}→${NC} ${message}"
        WARNINGS=$((WARNINGS + 1))
    fi
}

# Section header
section() {
    echo ""
    echo -e "${BLUE}━━━ $1 ━━━${NC}"
}

# ==================================================
# TEST 1: Cellular Interface Detection
# ==================================================
section "Test 1: Cellular Interface Detection"

detect_cellular() {
    # Check for wwan, enx, usb0, eth1, ppp0
    for prefix in wwan enx usb0 eth1; do
        iface=$($IP_BIN -o link show | awk -F': ' '{print $2}' | grep "^${prefix}" | head -n1 || true)
        if [[ -n "${iface}" ]]; then
            echo "${iface}"
            return 0
        fi
    done
    
    if $IP_BIN link show ppp0 >/dev/null 2>&1; then
        echo "ppp0"
        return 0
    fi
    
    return 1
}

if CELL_IFACE=$(detect_cellular); then
    test_result "Cellular interface detected" "PASS" "Interface: ${CELL_IFACE}"
else
    test_result "Cellular interface detected" "FAIL" "No cellular interface found (wwan*/enx*/usb0/eth1/ppp0)"
    CELL_IFACE=""
fi

# ==================================================
# TEST 2: Interface Status
# ==================================================
section "Test 2: Interface Status"

if [[ -n "${CELL_IFACE}" ]]; then
    # Check if UP
    if $IP_BIN link show "${CELL_IFACE}" | grep -q "state UP"; then
        test_result "Interface is UP" "PASS" "${CELL_IFACE} is UP"
    else
        test_result "Interface is UP" "FAIL" "${CELL_IFACE} is DOWN"
    fi
    
    # Check if has IP
    if $IP_BIN -4 addr show "${CELL_IFACE}" | grep -q "inet "; then
        CELL_IP=$($IP_BIN -4 addr show "${CELL_IFACE}" | awk '/inet / {print $2}' | cut -d/ -f1 | head -n1)
        test_result "Interface has IP address" "PASS" "IP: ${CELL_IP}"
    else
        test_result "Interface has IP address" "FAIL" "${CELL_IFACE} has no IPv4 address"
    fi
    
    # Test connectivity
    if ping -I "${CELL_IFACE}" -c 1 -W 3 8.8.8.8 >/dev/null 2>&1; then
        test_result "Interface internet connectivity" "PASS" "Can ping 8.8.8.8"
    else
        test_result "Interface internet connectivity" "FAIL" "Cannot ping 8.8.8.8 via ${CELL_IFACE}"
    fi
else
    test_result "Interface status tests" "FAIL" "Skipped (no interface)"
fi

# ==================================================
# TEST 3: Routing Configuration
# ==================================================
section "Test 3: Routing Configuration"

# Check WiFi/Ethernet default route
if $IP_BIN route show default | grep -qE "wlan0|eth0"; then
    DEF_IF=$($IP_BIN route show default | awk '/default/ {print $5; exit}')
    DEF_GW=$($IP_BIN route show default | awk '/default/ {print $3; exit}')
    test_result "Main default route preserved" "PASS" "via ${DEF_GW} dev ${DEF_IF}"
else
    test_result "Main default route preserved" "WARN" "Default route not via WiFi/Ethernet"
fi

# Check cellular routing table exists
if grep -qE "^[[:space:]]*100[[:space:]]+cellular$" /etc/iproute2/rt_tables 2>/dev/null; then
    test_result "Cellular routing table exists" "PASS" "Table 100 (cellular) configured"
else
    test_result "Cellular routing table exists" "FAIL" "Cellular table not in /etc/iproute2/rt_tables"
fi

# Check cellular table has routes
if $IP_BIN route show table cellular | grep -q "default"; then
    CELL_ROUTE=$($IP_BIN route show table cellular | grep "default" | head -n1)
    test_result "Cellular table has default route" "PASS" "${CELL_ROUTE}"
else
    test_result "Cellular table has default route" "FAIL" "No default route in cellular table"
fi

# Check policy rule
if $IP_BIN rule show | grep -q "fwmark 0x1 lookup cellular"; then
    test_result "Policy routing rule exists" "PASS" "fwmark 0x1 → cellular table"
else
    test_result "Policy routing rule exists" "FAIL" "No fwmark rule for cellular table"
fi

# ==================================================
# TEST 4: iptables Configuration
# ==================================================
section "Test 4: iptables Configuration"

# Check packet marking
proxy_marked=false
squid_marked=false

if $IPTABLES_BIN -t mangle -L OUTPUT -v -n | grep -q "owner UID match proxy"; then
    test_result "Proxy user traffic marking" "PASS" "proxy user marked with fwmark 0x1"
    proxy_marked=true
elif $IPTABLES_BIN -t mangle -L OUTPUT -v -n | grep -q "owner UID match squid"; then
    test_result "Squid user traffic marking" "PASS" "squid user marked with fwmark 0x1"
    squid_marked=true
else
    test_result "Traffic marking rules" "FAIL" "No iptables rules to mark proxy/squid traffic"
fi

# Check NAT/MASQUERADE
if [[ -n "${CELL_IFACE}" ]]; then
    if $IPTABLES_BIN -t nat -L POSTROUTING -v -n | grep -q "${CELL_IFACE}"; then
        test_result "NAT/MASQUERADE for cellular" "PASS" "MASQUERADE configured for ${CELL_IFACE}"
    else
        test_result "NAT/MASQUERADE for cellular" "WARN" "No MASQUERADE rule for ${CELL_IFACE}"
    fi
fi

# ==================================================
# TEST 5: Squid Proxy
# ==================================================
section "Test 5: Squid Proxy Service"

# Check Squid is running
if systemctl is-active --quiet squid 2>/dev/null; then
    test_result "Squid service running" "PASS" "systemctl status squid: active"
else
    test_result "Squid service running" "FAIL" "Squid is not active"
fi

# Check Squid listening on 3128
if $SS_BIN -lntp 2>/dev/null | grep -q ":3128"; then
    SQUID_BIND=$($SS_BIN -lntp 2>/dev/null | awk '/:3128/ {print $4}' | head -n1)
    if [[ "${SQUID_BIND}" == "0.0.0.0:3128" ]] || [[ "${SQUID_BIND}" == *":3128" ]]; then
        test_result "Squid listening on port 3128" "PASS" "Bind: ${SQUID_BIND}"
    else
        test_result "Squid listening on port 3128" "WARN" "Squid listening but not on 0.0.0.0:3128"
    fi
else
    test_result "Squid listening on port 3128" "FAIL" "Nothing listening on port 3128"
fi

# ==================================================
# TEST 6: Proxy Connectivity
# ==================================================
section "Test 6: Proxy Connectivity Test"

# Detect LAN IP
LAN_IP="$(ip -4 addr show eth0 2>/dev/null | awk '/inet /{print $2}' | cut -d/ -f1 || true)"
[[ -z "$LAN_IP" ]] && LAN_IP="$(ip -4 addr show wlan0 2>/dev/null | awk '/inet /{print $2}' | cut -d/ -f1 || true)"
[[ -z "$LAN_IP" ]] && LAN_IP="127.0.0.1"

# Test proxy locally
if command -v curl >/dev/null 2>&1; then
    echo -e "Testing proxy at ${LAN_IP}:3128..."
    
    # Test 1: Basic connectivity
    if PROXY_IP=$(curl -x "http://${LAN_IP}:3128" -s --max-time 10 https://api.ipify.org 2>/dev/null); then
        test_result "Proxy responds to requests" "PASS" "Proxy returned IP: ${PROXY_IP}"
        
        # Test 2: Check if using cellular or WiFi
        DIRECT_IP=$(curl -s --max-time 5 https://api.ipify.org 2>/dev/null || echo "unknown")
        
        if [[ "${PROXY_IP}" != "${DIRECT_IP}" ]] && [[ "${PROXY_IP}" != "unknown" ]]; then
            test_result "Proxy routes via cellular" "PASS" "Proxy IP (${PROXY_IP}) ≠ Direct IP (${DIRECT_IP})"
        elif [[ "${PROXY_IP}" == "${DIRECT_IP}" ]]; then
            test_result "Proxy routes via cellular" "FAIL" "Proxy using same IP as WiFi: ${PROXY_IP}"
        else
            test_result "Proxy routes via cellular" "WARN" "Could not determine direct IP for comparison"
        fi
        
        # Test 3: Check if IP is private (indicates routing problem)
        if [[ "${PROXY_IP}" =~ ^192\.168\. ]] || [[ "${PROXY_IP}" =~ ^10\. ]] || [[ "${PROXY_IP}" =~ ^172\. ]]; then
            test_result "Proxy returns public IP" "FAIL" "Proxy returned private IP: ${PROXY_IP}"
        else
            test_result "Proxy returns public IP" "PASS" "IP is public: ${PROXY_IP}"
        fi
    else
        test_result "Proxy responds to requests" "FAIL" "Proxy connection failed or timed out"
    fi
else
    test_result "Proxy connectivity tests" "WARN" "curl not installed, skipping proxy tests"
fi

# ==================================================
# TEST 7: PM2 Services
# ==================================================
section "Test 7: PM2 Services"

if command -v pm2 >/dev/null 2>&1; then
    # Check orchestrator
    if pm2 list 2>/dev/null | grep -q "4g-proxy-orchestrator"; then
        STATUS=$(pm2 jlist 2>/dev/null | jq -r '.[] | select(.name=="4g-proxy-orchestrator") | .pm2_env.status' 2>/dev/null || echo "unknown")
        if [[ "${STATUS}" == "online" ]]; then
            test_result "Orchestrator service running" "PASS" "PM2 status: online"
        else
            test_result "Orchestrator service running" "FAIL" "PM2 status: ${STATUS}"
        fi
    else
        test_result "Orchestrator service running" "FAIL" "4g-proxy-orchestrator not found in PM2"
    fi
    
    # Check web interface
    if pm2 list 2>/dev/null | grep -q "4g-proxy-web"; then
        STATUS=$(pm2 jlist 2>/dev/null | jq -r '.[] | select(.name=="4g-proxy-web") | .pm2_env.status' 2>/dev/null || echo "unknown")
        if [[ "${STATUS}" == "online" ]]; then
            test_result "Web interface running" "PASS" "PM2 status: online"
        else
            test_result "Web interface running" "FAIL" "PM2 status: ${STATUS}"
        fi
    else
        test_result "Web interface running" "FAIL" "4g-proxy-web not found in PM2"
    fi
else
    test_result "PM2 services" "WARN" "PM2 not installed"
fi

# ==================================================
# TEST 8: Cellular Keepalive Service
# ==================================================
section "Test 8: Cellular Keepalive Service"

if systemctl is-active --quiet cellular-keepalive 2>/dev/null; then
    test_result "Cellular keepalive service running" "PASS" "systemctl status cellular-keepalive: active"
    
    # Check if service is actually monitoring
    if journalctl -u cellular-keepalive -n 5 --no-pager 2>/dev/null | grep -q "cellular-keepalive"; then
        test_result "Keepalive service logging" "PASS" "Service is actively monitoring"
    else
        test_result "Keepalive service logging" "WARN" "No recent logs from keepalive service"
    fi
else
    test_result "Cellular keepalive service running" "WARN" "cellular-keepalive not active (may not be installed yet)"
fi

# ==================================================
# TEST 9: Firewall Rules
# ==================================================
section "Test 9: Firewall Configuration"

# Check SSH allowed
if $IPTABLES_BIN -L INPUT -v -n | grep -q "dpt:22"; then
    test_result "SSH access allowed" "PASS" "Port 22 open in firewall"
else
    test_result "SSH access allowed" "WARN" "No explicit SSH rule (may use default ACCEPT)"
fi

# Check Squid allowed
if $IPTABLES_BIN -L INPUT -v -n | grep -q "dpt:3128"; then
    test_result "Squid access allowed" "PASS" "Port 3128 open in firewall"
else
    test_result "Squid access allowed" "WARN" "No explicit Squid rule (may use default ACCEPT)"
fi

# Check web dashboard allowed
if $IPTABLES_BIN -L INPUT -v -n | grep -q "dpt:5000"; then
    test_result "Web dashboard access allowed" "PASS" "Port 5000 open in firewall"
else
    test_result "Web dashboard access allowed" "WARN" "No explicit web dashboard rule"
fi

# ==================================================
# SUMMARY
# ==================================================
echo ""
echo -e "${BLUE}╔════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║              TEST SUMMARY                      ║${NC}"
echo -e "${BLUE}╚════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "Total Tests:   ${BLUE}${TOTAL_TESTS}${NC}"
echo -e "Passed:        ${GREEN}${PASSED_TESTS}${NC}"
echo -e "Failed:        ${RED}${FAILED_TESTS}${NC}"
echo -e "Warnings:      ${YELLOW}${WARNINGS}${NC}"
echo ""

if [[ ${FAILED_TESTS} -eq 0 ]]; then
    echo -e "${GREEN}╔════════════════════════════════════════════════╗${NC}"
    echo -e "${GREEN}║  ✅ ALL CRITICAL TESTS PASSED!                 ║${NC}"
    echo -e "${GREEN}║  Your 4G proxy should be fully operational     ║${NC}"
    echo -e "${GREEN}╚════════════════════════════════════════════════╝${NC}"
    exit 0
else
    echo -e "${RED}╔════════════════════════════════════════════════╗${NC}"
    echo -e "${RED}║  ❌ SOME TESTS FAILED                           ║${NC}"
    echo -e "${RED}║  Please review the failures above              ║${NC}"
    echo -e "${RED}║  Run 'sudo ./run.sh' to fix configuration      ║${NC}"
    echo -e "${RED}╚════════════════════════════════════════════════╝${NC}"
    exit 1
fi

