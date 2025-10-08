# 🚀 Deployment Guide - Complete Fix for 4G Proxy Issues

This guide will walk you through deploying all the fixes to resolve:
1. ❌ Can't access proxy from PC
2. ❌ Web interface showing "Checking..." forever
3. ❌ Proxy using WiFi IP instead of cellular IP

## 📋 What's Been Fixed

### 1. **Cellular Interface Stability** ✅
- Created `cellular-keepalive.sh` - monitors interface every 30s
- Auto-restarts DHCP if connection is lost
- Brings interface back UP if it goes DOWN
- Verifies internet connectivity continuously

### 2. **Improved Routing** ✅
- Dynamic gateway detection (not hardcoded)
- Proper connectivity testing before configuring routes
- Validates cellular table has routes
- Better error messages

### 3. **Web Interface** ✅
- Shows actual IP status (not stuck on "Checking...")
- Displays error messages when routing is broken
- Colour-coded IP display:
  - 🟢 Green = Cellular IP (working!)
  - 🟡 Yellow = WiFi/LAN IP (routing broken)
  - 🔴 Red = Error/No connection

### 4. **Orchestrator API** ✅
- Better timeout handling
- Informative error messages
- Shows when routing through WiFi instead of cellular

## 🛠️ Deployment Steps

### Step 1: Back Up Current Configuration

On your Pi5, run:
```bash
cd ~/raspi-4g-proxy-v2  # or wherever your project is
cp config.yaml config.yaml.backup  # Save your current config
```

### Step 2: Pull the Fixes

Copy all the new/updated files to your Pi5:
- `cellular-keepalive.sh` (NEW) - Interface monitoring service
- `run.sh` (UPDATED) - Better routing & gateway detection
- `orchestrator.py` (UPDATED) - Better IP detection
- `web_interface.py` (UPDATED) - Better error display
- `test-proxy.sh` (NEW) - Comprehensive diagnostic script
- `DIAGNOSIS.md` (NEW) - Issue documentation
- `DEPLOYMENT_GUIDE.md` (THIS FILE) - Deployment instructions

**Option A: Git Pull** (if you're using git)
```bash
cd ~/raspi-4g-proxy-v2
git pull
```

**Option B: Manual Copy** (if not using git)
Copy the files from your Windows machine to the Pi using SCP/SFTP

### Step 3: Make Scripts Executable

```bash
cd ~/raspi-4g-proxy-v2
chmod +x cellular-keepalive.sh test-proxy.sh run.sh
```

### Step 4: Stop Current Services

```bash
# Stop PM2 services
pm2 stop all

# Stop cellular keepalive if running (old version)
sudo systemctl stop cellular-keepalive 2>/dev/null || true
```

### Step 5: Run the Updated Setup

```bash
cd ~/raspi-4g-proxy-v2
sudo ./run.sh
```

**What this will do:**
1. ✅ Install cellular keepalive service
2. ✅ Detect cellular interface (wwan*/enx*/ppp0)
3. ✅ Ensure interface is UP and has IP
4. ✅ Test connectivity (ping 8.8.8.8)
5. ✅ Configure cellular routing table with proper gateway
6. ✅ Set up policy routing (fwmark 0x1 → cellular table)
7. ✅ Configure iptables to mark Squid traffic
8. ✅ Restart Squid with correct configuration
9. ✅ Start PM2 services (orchestrator + web)

### Step 6: Verify Installation

Wait 2-3 minutes for services to fully start, then run:

```bash
# Run comprehensive diagnostic
bash test-proxy.sh
```

**Expected output:**
```
╔════════════════════════════════════════════════╗
║   4G Proxy Comprehensive Diagnostic Test      ║
╚════════════════════════════════════════════════╝

━━━ Test 1: Cellular Interface Detection ━━━
✅ PASS: Cellular interface detected
   Interface: enxda287678377a

━━━ Test 2: Interface Status ━━━
✅ PASS: Interface is UP
✅ PASS: Interface has IP address
   IP: 192.168.225.100
✅ PASS: Interface internet connectivity

━━━ Test 3: Routing Configuration ━━━
✅ PASS: Main default route preserved
✅ PASS: Cellular routing table exists
✅ PASS: Cellular table has default route
✅ PASS: Policy routing rule exists

━━━ Test 4: iptables Configuration ━━━
✅ PASS: Proxy user traffic marking
✅ PASS: NAT/MASQUERADE for cellular

━━━ Test 5: Squid Proxy Service ━━━
✅ PASS: Squid service running
✅ PASS: Squid listening on port 3128

━━━ Test 6: Proxy Connectivity Test ━━━
✅ PASS: Proxy responds to requests
   Proxy returned IP: 88.100.10.123
✅ PASS: Proxy routes via cellular
✅ PASS: Proxy returns public IP

━━━ Test 7: PM2 Services ━━━
✅ PASS: Orchestrator service running
✅ PASS: Web interface running

━━━ Test 8: Cellular Keepalive Service ━━━
✅ PASS: Cellular keepalive service running
✅ PASS: Keepalive service logging

━━━ Test 9: Firewall Configuration ━━━
✅ PASS: SSH access allowed
✅ PASS: Squid access allowed
✅ PASS: Web dashboard access allowed

╔════════════════════════════════════════════════╗
║  ✅ ALL CRITICAL TESTS PASSED!                 ║
║  Your 4G proxy should be fully operational     ║
╚════════════════════════════════════════════════╝
```

### Step 7: Test from Your PC

**Test 1: Check Web Interface**
```
Open browser: http://YOUR_PI_IP:5000
```
Should show:
- Current IP: `88.100.10.123` (your cellular IP, not WiFi!)
- Connection Status: ✅ Connected
- Mode: RNDIS (or QMI/PPP)

**Test 2: Test Proxy from Windows PC**
```powershell
# CMD/PowerShell
curl -x http://YOUR_PI_IP:3128 https://api.ipify.org
```

Should return your **cellular IP**, not your home WiFi IP (86.151.x.x)

**Test 3: Direct IP Check**
```powershell
# This should return your WiFi IP (86.151.x.x)
curl https://api.ipify.org

# This should return cellular IP (different!)
curl -x http://YOUR_PI_IP:3128 https://api.ipify.org
```

## 🔧 Troubleshooting

### Issue: Still showing WiFi IP

**Check cellular interface:**
```bash
ip link show enx*  # or wwan0, depending on your interface
```
Should show: `state UP`

**Check routing table:**
```bash
ip route show table cellular
```
Should show: `default via 192.168.225.1 dev enxXXX` (or similar)

**Check connectivity:**
```bash
ping -I enxXXX 8.8.8.8
```
Should succeed (not "Network is unreachable")

**Fix:**
```bash
# Restart keepalive service
sudo systemctl restart cellular-keepalive

# Check logs
sudo journalctl -u cellular-keepalive -f
```

### Issue: Cellular interface keeps going DOWN

**Check keepalive service:**
```bash
sudo systemctl status cellular-keepalive
```

**View live monitoring:**
```bash
sudo journalctl -u cellular-keepalive -f
```

Should show:
```
cellular-keepalive: Starting cellular keepalive service...
cellular-keepalive: Check interval: 30 seconds
```

**Manual test:**
```bash
# Run keepalive script manually
sudo /usr/local/bin/cellular-keepalive
```

### Issue: Web interface still shows "Checking..."

**Check orchestrator logs:**
```bash
pm2 logs 4g-proxy-orchestrator
```

**Test API directly:**
```bash
curl http://127.0.0.1:8088/status
```

Should return JSON with `public_ip` field

**Restart orchestrator:**
```bash
pm2 restart 4g-proxy-orchestrator
pm2 logs 4g-proxy-orchestrator
```

### Issue: Can't access from PC

**Check firewall:**
```bash
sudo iptables -L INPUT -v -n | grep 3128
```

**Check Squid listening:**
```bash
ss -lntp | grep 3128
```

Should show: `0.0.0.0:3128`

**Test locally first:**
```bash
curl -x http://127.0.0.1:3128 https://api.ipify.org
```

## 📊 Monitoring

### Watch Cellular Interface

```bash
# Live interface status
watch -n 5 'ip link show enx* && ip addr show enx*'
```

### Watch Routing Table

```bash
# Live cellular routes
watch -n 5 'ip route show table cellular'
```

### Watch Keepalive Logs

```bash
# Live keepalive monitoring
sudo journalctl -u cellular-keepalive -f
```

### Watch Proxy Traffic

```bash
# Live Squid access log
sudo tail -f /var/log/squid/access.log
```

## 🎯 Expected Behaviour After Fix

1. ✅ Cellular interface stays UP permanently
2. ✅ DHCP lease auto-renewed (no IP loss)
3. ✅ Cellular table always has valid routes
4. ✅ Web interface shows cellular IP within 5 seconds
5. ✅ Proxy accessible from external PCs
6. ✅ Proxy returns cellular IP (not WiFi)
7. ✅ Auto-recovery if interface goes DOWN (within 30s)
8. ✅ Clear error messages if something is wrong

## 🔄 Reboot Test

After everything works, test that it survives a reboot:

```bash
sudo reboot
```

After reboot (wait 2 minutes):
```bash
# Should all be running
systemctl status cellular-keepalive
pm2 status
systemctl status squid

# Should pass all tests
bash test-proxy.sh
```

## 📞 Support

If issues persist:

1. **Collect diagnostic info:**
   ```bash
   bash test-proxy.sh > diagnostic-output.txt 2>&1
   sudo journalctl -u cellular-keepalive -n 50 >> diagnostic-output.txt
   pm2 logs --lines 50 >> diagnostic-output.txt
   ip addr show >> diagnostic-output.txt
   ip route show table cellular >> diagnostic-output.txt
   iptables -t mangle -L OUTPUT -v -n >> diagnostic-output.txt
   ```

2. **Check the diagnostic output** for specific error messages

3. **Common fixes:**
   - `sudo systemctl restart cellular-keepalive`
   - `pm2 restart all`
   - `sudo ./run.sh` (re-run setup)

---
**Version:** 1.0  
**Date:** 2025-10-08  
**Status:** Ready for deployment

