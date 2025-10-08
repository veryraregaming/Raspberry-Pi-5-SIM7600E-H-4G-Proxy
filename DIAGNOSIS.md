# 🔍 Complete System Diagnosis & Fix Plan

## Issues Found

### 1. Cellular Interface Instability (Critical)
**Location:** RNDIS/QMI interface keeps going DOWN  
**Symptoms:**
- `ip link show enx*` shows `state DOWN`
- `ping -I enx* 8.8.8.8` returns "Network is unreachable"
- DHCP lease expires and not renewed

**Root Cause:**
- No automatic keepalive mechanism
- DHCP client not monitoring connection
- No recovery when interface goes DOWN

**Impact:**
- Cellular routing table becomes empty
- Proxy falls back to WiFi
- Can't access proxy from external PCs

### 2. Policy Routing Configuration Issues
**Location:** `run.sh` lines 119-214  
**Symptoms:**
- Cellular table has no routes when interface is DOWN
- Gateway detection fails for RNDIS interfaces
- Policy rules present but table is empty

**Root Cause:**
- Gateway hardcoded to `192.168.225.1` (may not be correct)
- No validation that routes actually work
- No monitoring/recovery if routes disappear

**Impact:**
- Marked Squid traffic has nowhere to go
- Falls back to main table (WiFi)
- Wrong IP returned through proxy

### 3. Web Interface Status Detection
**Location:** `orchestrator.py` get_current_ip() & web_interface.py  
**Symptoms:**
- Shows "Checking..." forever
- Never displays actual IP
- API requests time out

**Root Cause:**
- `get_current_ip()` filters out home IPs (86.151.*, 192.168.*, 10.*, 172.*)
- When proxy uses WiFi, it returns "Unknown"
- Status endpoint returns "Checking..." as default
- No timeout handling in web interface

**Impact:**
- Can't see current IP status
- Can't diagnose what IP proxy is using
- User can't tell if system is working

### 4. Squid Routing Configuration
**Location:** `run.sh` policy routing setup  
**Symptoms:**
- Squid works but uses wrong interface
- Returns WiFi IP instead of cellular IP

**Root Cause:**
- Policy routing depends on cellular table having routes
- When table is empty, iptables MARK has no effect
- Falls back to main routing table

**Impact:**
- Proxy accessible but uses wrong network
- External PCs can connect but get WiFi IP
- Defeats purpose of 4G proxy

## Fix Strategy

### Phase 1: Cellular Interface Stability ✅
1. Create dedicated keepalive service
2. Monitor interface state every 30 seconds
3. Auto-restart DHCP if connection lost
4. Bring interface back UP if it goes DOWN
5. Verify internet connectivity (ping test)

### Phase 2: Policy Routing Hardening ✅
1. Detect gateway dynamically (not hardcoded)
2. Validate gateway is reachable
3. Add fallback gateway options
4. Monitor cellular table and recreate if empty
5. Add diagnostic logging

### Phase 3: Web Interface Fix ✅
1. Fix status endpoint to return actual IP
2. Remove home IP filtering when in diagnostic mode
3. Add timeout handling in web interface
4. Show detailed connection status
5. Display which interface is active

### Phase 4: Squid Configuration ✅
1. Ensure Squid binds to 0.0.0.0:3128
2. Add tcp_outgoing_address for cellular IP
3. Validate policy routing before starting
4. Add connection test on startup

## Implementation Plan

1. **cellular-keepalive.service** - Systemd service to maintain connection
2. **run.sh improvements** - Better gateway detection & validation
3. **orchestrator.py fixes** - Better IP detection & error handling
4. **web_interface.py fixes** - Timeout handling & better UX
5. **monitoring script** - Continuous health checks

## Expected Results After Fix

✅ Cellular interface stays UP consistently  
✅ DHCP lease auto-renewed  
✅ Cellular routing table always has valid routes  
✅ Proxy routes through cellular (not WiFi)  
✅ External PCs can connect and get cellular IP  
✅ Web interface shows actual IP within 5 seconds  
✅ System auto-recovers from failures

---
*Generated: 2025-10-08*
*Status: Ready to implement fixes*

