# 🚀 Quick Reference Guide - 4G Proxy Management

## 📋 Quick Commands

### One-Command Setup
```bash
cd ~/raspi-4g-proxy-v2
sudo ./run.sh
```

### Test Everything
```bash
bash test-proxy.sh
```

### Check What IP You're Getting
```bash
# From Pi (should show cellular IP)
curl -x http://127.0.0.1:3128 https://api.ipify.org

# From Windows PC (should show cellular IP)
curl -x http://PI_IP:3128 https://api.ipify.org
```

### Check Web Dashboard
```
http://PI_IP:5000
```

---

## 🔍 Diagnostics

### Check Cellular Interface
```bash
# See if interface is UP and has IP
ip addr show enx*  # or wwan0, usb0, ppp0

# Should show: state UP
# Should have: inet 192.168.225.x
```

### Check Routing
```bash
# Main routing table (WiFi should be default)
ip route show

# Cellular routing table (cellular should be here)
ip route show table cellular

# Policy routing rule
ip rule show | grep cellular
```

### Check Connectivity
```bash
# Ping through cellular interface
ping -I enxXXXX 8.8.8.8

# Should NOT say "Network is unreachable"
```

### Check Squid
```bash
# Is Squid running?
sudo systemctl status squid

# Is it listening?
ss -lntp | grep 3128

# Test proxy locally
curl -x http://127.0.0.1:3128 https://api.ipify.org
```

### Check Services
```bash
# Cellular keepalive
sudo systemctl status cellular-keepalive

# PM2 services
pm2 status

# All together
sudo systemctl status cellular-keepalive squid && pm2 status
```

---

## 📊 Monitoring

### Watch Cellular Interface Live
```bash
# Updates every 5 seconds
watch -n 5 'ip link show enx* && ip -4 addr show enx*'
```

### Watch Keepalive Logs Live
```bash
sudo journalctl -u cellular-keepalive -f
```

### Watch Proxy Traffic Live
```bash
sudo tail -f /var/log/squid/access.log
```

### Watch PM2 Logs Live
```bash
pm2 logs
```

---

## 🛠️ Common Fixes

### Cellular Interface DOWN
```bash
# Restart keepalive service
sudo systemctl restart cellular-keepalive

# Bring interface UP manually
sudo ip link set enxXXXX up
sudo dhclient -v enxXXXX

# Check logs
sudo journalctl -u cellular-keepalive -n 50
```

### Proxy Showing WiFi IP
```bash
# 1. Check routing table
ip route show table cellular

# 2. If empty, run setup again
sudo ./run.sh

# 3. Restart Squid
sudo systemctl restart squid

# 4. Test again
curl -x http://127.0.0.1:3128 https://api.ipify.org
```

### Web Interface Not Showing IP
```bash
# Restart orchestrator
pm2 restart 4g-proxy-orchestrator

# Check logs
pm2 logs 4g-proxy-orchestrator

# Test API directly
curl http://127.0.0.1:8088/status
```

### Can't Access from PC
```bash
# 1. Check firewall
sudo iptables -L INPUT -v -n | grep 3128

# 2. Check Squid binding
ss -lntp | grep 3128
# Should show: 0.0.0.0:3128

# 3. Test from Pi first
curl -x http://127.0.0.1:3128 https://api.ipify.org

# 4. If that works, check PC can reach Pi
# From PC: ping PI_IP
```

### Everything Broken
```bash
# Nuclear option: re-run full setup
cd ~/raspi-4g-proxy-v2
pm2 stop all
sudo systemctl stop cellular-keepalive
sudo ./run.sh

# Wait 2-3 minutes, then test
bash test-proxy.sh
```

---

## 🎯 Expected Results

### ✅ Healthy System

**Interface Status:**
```
ip link show enx...
  -> state UP
```

**Has IP:**
```
ip -4 addr show enx...
  -> inet 192.168.225.100/24
```

**Can Ping:**
```
ping -I enx... 8.8.8.8
  -> 64 bytes from 8.8.8.8: icmp_seq=1 ttl=118 time=45.2 ms
```

**Routing Table:**
```
ip route show table cellular
  -> default via 192.168.225.1 dev enx... (or similar)
```

**Proxy Returns Cellular IP:**
```
curl -x http://127.0.0.1:3128 https://api.ipify.org
  -> 88.100.10.123 (NOT 86.151.x.x!)
```

**Web Dashboard:**
```
http://PI_IP:5000
  -> Shows: 88.100.10.123 (cellular IP)
  -> Status: ✅ Connected
  -> Mode: RNDIS (or QMI/PPP)
```

**Test Script:**
```
bash test-proxy.sh
  -> ✅ ALL CRITICAL TESTS PASSED!
```

---

## 🔄 Service Management

### Restart Everything
```bash
# Restart all services
sudo systemctl restart cellular-keepalive squid
pm2 restart all
```

### Stop Everything
```bash
pm2 stop all
sudo systemctl stop cellular-keepalive squid
```

### Start Everything
```bash
sudo systemctl start cellular-keepalive squid
pm2 start all
```

### Check All Service Status
```bash
sudo systemctl status cellular-keepalive squid
pm2 status
```

---

## 📱 From Windows PC

### Test Proxy
```powershell
# PowerShell/CMD
curl -x http://PI_IP:3128 https://api.ipify.org
```

### Set System Proxy (Temporary)
```powershell
# PowerShell
$env:HTTP_PROXY="http://PI_IP:3128"
$env:HTTPS_PROXY="http://PI_IP:3128"

# Test
curl https://api.ipify.org
```

### Browser Proxy Settings
```
Manual proxy configuration:
HTTP Proxy: PI_IP
Port: 3128
Use for all protocols: Yes
```

---

## 🚨 Red Flags (Things That Are Wrong)

❌ **Interface shows `state DOWN`**
→ Run: `sudo systemctl restart cellular-keepalive`

❌ **`ping -I enx... 8.8.8.8` says "Network is unreachable"**
→ Run: `sudo dhclient -v enxXXXX` then `sudo ./run.sh`

❌ **`ip route show table cellular` is empty**
→ Run: `sudo ./run.sh`

❌ **Proxy returns WiFi IP (86.151.x.x)**
→ Routing is broken, run: `sudo ./run.sh`

❌ **Web dashboard shows "⚠️ WiFi IP: 86.151.x.x (cellular routing broken!)"**
→ This is the error message telling you routing is wrong, run: `sudo ./run.sh`

❌ **`ss -lntp | grep 3128` shows nothing**
→ Squid not running, run: `sudo systemctl start squid`

❌ **PM2 services not online**
→ Run: `pm2 restart all`

❌ **Keepalive service not running**
→ Run: `sudo systemctl start cellular-keepalive`

---

## 📖 Detailed Guides

- **Full deployment:** See `DEPLOYMENT_GUIDE.md`
- **Issue diagnosis:** See `DIAGNOSIS.md`
- **Comprehensive test:** Run `bash test-proxy.sh`
- **Main README:** See `README.md`

---

## 💡 Pro Tips

1. **Always check the test script first:**
   ```bash
   bash test-proxy.sh
   ```
   This will tell you exactly what's wrong.

2. **Check keepalive logs when interface problems occur:**
   ```bash
   sudo journalctl -u cellular-keepalive -f
   ```

3. **Use the web dashboard for quick status:**
   ```
   http://PI_IP:5000
   ```
   It will show colour-coded warnings if routing is wrong.

4. **When in doubt, re-run setup:**
   ```bash
   sudo ./run.sh
   ```
   It's idempotent (safe to run multiple times).

5. **Test locally on Pi before testing from PC:**
   If `curl -x http://127.0.0.1:3128 https://api.ipify.org` doesn't work on the Pi,
   it won't work from your PC either.

---

**Quick Summary:**
- ✅ Test everything: `bash test-proxy.sh`
- ✅ Check status: `http://PI_IP:5000`
- ✅ Fix problems: `sudo ./run.sh`
- ✅ Monitor: `sudo journalctl -u cellular-keepalive -f`

