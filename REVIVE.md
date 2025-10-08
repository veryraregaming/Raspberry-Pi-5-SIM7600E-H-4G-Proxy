how do i fix perm issue
# 4G Proxy Revival Guide

## Problem
The cellular interface keeps going DOWN (`state DOWN`) and losing internet connectivity, causing the proxy to fall back to WiFi instead of using the 4G connection.

## Symptoms
- Proxy works locally on Pi but not from Windows PC
- Cellular interface shows `state DOWN`
- `ping -I enxda287678377a 8.8.8.8` returns "Network is unreachable"
- Proxy returns WiFi IP instead of cellular IP
- Cellular table is empty (no routes)

## Manual Fix (Temporary)
```bash
# 1. Bring up the cellular interface
sudo ip link set enxda287678377a up

# 2. Get internet connectivity
sudo dhclient -v enxda287678377a

# 3. Test connectivity
ping -I enxda287678377a 8.8.8.8

# 4. Add route to cellular table
sudo ip route add default via 192.168.225.1 dev enxda287678377a table cellular

# 5. Test proxy
curl -x http://127.0.0.1:3128 -s https://api.ipify.org
```

## Root Cause
The cellular interface (RNDIS) is unstable and keeps going down, losing its internet connection. This causes:
- Policy routing to fail (no cellular table routes)
- Proxy to fall back to main routing table (WiFi)
- External connections to fail

## Automated Fix (Permanent)
The `run.sh` script now includes:

1. **Multiple interface bring-up attempts**
2. **Connectivity testing and retry logic**
3. **Systemd keepalive service** to maintain connection
4. **Automatic cellular table route setup**

## Key Commands for Debugging
```bash
# Check interface status
ip link show enxda287678377a

# Check cellular table
ip route show table cellular

# Check policy routing
ip rule show

# Check iptables marking
sudo iptables -t mangle -L OUTPUT -v -n

# Test cellular connectivity
ping -I enxda287678377a 8.8.8.8

# Test proxy locally
curl -x http://127.0.0.1:3128 -s https://api.ipify.org
```

## Prevention
- Use the updated `run.sh` script with keepalive service
- Monitor cellular interface status
- Ensure proper systemd service configuration
- Regular connectivity testing

---
*Last updated: 2025-10-08*
*Issue: Cellular interface instability causing proxy fallback to WiFi*
