#!/usr/bin/env python3
"""
Raspberry Pi 5 + SIM7600E-H 4G Proxy - Auto Setup (RNDIS + PPP fallback)
- Detects and uses RNDIS interface (enx*) for cellular connection
- Falls back to PPP if RNDIS is not available
- Prevents modem lockouts with proper error handling
- Idempotently writes: config.yaml, squid.conf, ecosystem.config.js
- Safe routing that preserves LAN connectivity
"""

import os
import sys
import re
import shlex
import socket
import time
import json
import secrets
import subprocess
from pathlib import Path

import yaml
import requests
import serial  # pyserial

BASE = Path(__file__).resolve().parent

# ---------- shell helpers ----------

def run_cmd(cmd, check=False, shell=False, timeout=None):
    if isinstance(cmd, str) and not shell:
        cmd = shlex.split(cmd)
    try:
        cp = subprocess.run(
            cmd, shell=shell, capture_output=True, text=True,
            timeout=timeout, check=check
        )
        return cp.stdout.strip(), cp.stderr.strip(), cp.returncode
    except subprocess.CalledProcessError as e:
        return e.stdout.strip(), e.stderr.strip(), e.returncode

def which(path, default=None):
    out, _, _ = run_cmd(["which", path])
    return out or default or path

IP_PATH = which("ip", "/usr/sbin/ip")
PPPD_PATH = which("pppd", "/usr/sbin/pppd")
CHAT_PATH = which("chat", "/usr/sbin/chat")
SYSTEMCTL_PATH = which("systemctl", "/bin/systemctl")

# ---------- IP helpers ----------

def detect_ipv4(iface: str):
    out, _, rc = run_cmd([IP_PATH, "-4", "addr", "show", iface])
    if rc != 0:
        return None
    m = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)/\d+", out)
    return m.group(1) if m else None

def detect_lan_ip():
    for iface in ("wlan0", "eth0"):
        ip = detect_ipv4(iface)
        if ip:
            return ip
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("1.1.1.1", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()

# ---------- modem detection (QMI + RNDIS + PPP) ----------

def detect_qmi_interface():
    """Detect QMI interface (wwan*) that provides cellular connectivity."""
    try:
        out, _, _ = run_cmd([IP_PATH, "-br", "link", "show"], check=False)
        for line in out.splitlines():
            if line.startswith("wwan"):
                parts = line.split()
                iface = parts[0]
                # Check if interface has an IP address
                ip = detect_ipv4(iface)
                if ip:
                    print(f"  ✅ QMI interface found: {iface} with IP {ip}")
                    return iface, ip
                else:
                    print(f"  🔍 QMI interface found: {iface} (no IP yet)")
                    return iface, None
    except Exception:
        pass
    return None, None

def setup_qmi_interface(iface):
    """Setup QMI interface using qmicli."""
    print(f"  🔧 Setting up QMI interface: {iface}")
    
    # Bring interface up
    run_cmd(["sudo", IP_PATH, "link", "set", "dev", iface, "up"], check=False)
    time.sleep(2)
    
    # Start network connection via qmi-network
    print(f"  📡 Starting QMI connection for {iface}...")
    out, err, rc = run_cmd(["sudo", "qmi-network", f"/dev/cdc-wdm0", "start"], check=False, timeout=30)
    
    if rc == 0:
        # Configure IP via qmicli
        print(f"  📡 Configuring IP for {iface}...")
        time.sleep(2)
        
        # Get DHCP settings from modem
        out, err, rc = run_cmd(["sudo", "qmicli", "-d", "/dev/cdc-wdm0", "--wds-get-current-settings"], 
                              check=False, timeout=10)
        
        # Check if we got an IP
        ip = detect_ipv4(iface)
        if ip:
            print(f"  ✅ QMI interface {iface} configured with IP: {ip}")
            return ip
        else:
            print(f"  ⚠️ QMI connection started but no IP detected on {iface}")
    else:
        print(f"  ⚠️ QMI network start failed: {err}")
    
    return None

def activate_modem_via_qmi():
    """Try to activate modem via QMI interface."""
    print("📡 Activating SIM7600E-H modem via QMI…")
    
    # First, try to detect existing QMI interface
    iface, ip = detect_qmi_interface()
    
    if iface and ip:
        print(f"  ✅ QMI interface {iface} already active with IP {ip}")
        return iface, ip
    
    if iface:
        # Interface exists but no IP, try to get one
        ip = setup_qmi_interface(iface)
        if ip:
            return iface, ip
    
    print("  ❌ QMI interface not available")
    return None, None

def detect_rndis_interface():
    """Detect RNDIS interface (enx*) that provides cellular connectivity."""
    try:
        out, _, _ = run_cmd([IP_PATH, "-br", "link", "show"], check=False)
        for line in out.splitlines():
            if line.startswith("enx") or line.startswith("eth1"):
                parts = line.split()
                iface = parts[0]
                # Check if interface has an IP address
                ip = detect_ipv4(iface)
                if ip:
                    print(f"  ✅ RNDIS interface found: {iface} with IP {ip}")
                    return iface, ip
                else:
                    print(f"  🔍 RNDIS interface found: {iface} (no IP yet)")
                    return iface, None
    except Exception:
        pass
    return None, None

def setup_rndis_interface(iface):
    """Setup RNDIS interface with DHCP."""
    print(f"  🔧 Setting up RNDIS interface: {iface}")
    
    # Bring interface up
    run_cmd(["sudo", IP_PATH, "link", "set", "dev", iface, "up"], check=False)
    time.sleep(2)
    
    # Get IP via DHCP
    print(f"  📡 Getting IP via DHCP for {iface}...")
    out, err, rc = run_cmd(["sudo", "dhclient", "-v", iface], check=False, timeout=30)
    
    if rc == 0:
        # Check if we got an IP
        ip = detect_ipv4(iface)
        if ip:
            print(f"  ✅ RNDIS interface {iface} configured with IP: {ip}")
            return ip
        else:
            print(f"  ⚠️ DHCP succeeded but no IP detected on {iface}")
    else:
        print(f"  ⚠️ DHCP failed for {iface}: {err}")
    
    return None

def detect_modem_port():
    """Detect AT command port for modem with safe error handling."""
    candidates = [
        "/dev/ttyUSB2", "/dev/ttyUSB1", "/dev/ttyUSB0",
        "/dev/ttyUSB3", "/dev/ttyUSB4"
    ]
    try:
        for dev in os.listdir("/dev"):
            if dev.startswith("ttyUSB"):
                p = f"/dev/{dev}"
                if p not in candidates:
                    candidates.append(p)
    except Exception:
        pass

    for port in candidates:
        if not os.path.exists(port):
            continue
        try:
            with serial.Serial(port, 115200, timeout=1) as ser:
                ser.write(b"AT\r")
                time.sleep(0.3)
                resp = ser.read_all().decode(errors="ignore")
                if "OK" in resp:
                    print(f"  ✅ Modem responding on {port}")
                    return port
        except Exception:
            continue
    
    print(f"  ⚠️ No responding AT port found, using default: /dev/ttyUSB2")
    return "/dev/ttyUSB2"

def safe_modem_reset():
    """Safely reset modem to prevent lockouts."""
    print("  🔄 Performing safe modem reset...")
    
    # Kill any existing PPP processes
    run_cmd(["sudo", "pkill", "pppd"], check=False)
    time.sleep(2)
    
    # Try to put modem in command mode
    try:
        at_port = detect_modem_port()
        with serial.Serial(at_port, 115200, timeout=1) as ser:
            ser.write(b"+++\r")
            time.sleep(3)
            ser.write(b"AT\r")
            time.sleep(1)
            resp = ser.read_all().decode(errors="ignore")
            if "OK" in resp:
                print(f"  ✅ Modem reset to command mode")
                return True
    except Exception as e:
        print(f"  ⚠️ Modem reset failed: {e}")
    
    return False

def create_ppp_config(apn: str, at_port: str):
    chat_file = "/etc/chatscripts/ee-chat"
    peer_file = "/etc/ppp/peers/ee"
    log_file = "/var/log/ppp-ee.log"

    run_cmd(["sudo", "mkdir", "-p", "/etc/chatscripts"], check=False)
    run_cmd(["sudo", "mkdir", "-p", "/etc/ppp/peers"], check=False)

    chat_script = f"""ABORT 'BUSY'
ABORT 'NO CARRIER'
ABORT 'ERROR'
ABORT 'NO DIALTONE'
ABORT 'NO ANSWER'
REPORT CONNECT
TIMEOUT 60
'' AT
OK ATZ
OK AT+CPIN?
OK AT+CFUN=1
OK AT+CGATT=1
OK AT+CGDCONT=1,"IP","{apn}"
OK ATD*99#
CONNECT ''
"""
    (BASE / "ee-chat.tmp").write_text(chat_script, encoding="utf-8")
    run_cmd(["sudo", "cp", str(BASE / "ee-chat.tmp"), chat_file], check=False)
    run_cmd(["sudo", "chmod", "644", chat_file], check=False)

    peer_config = f"""{at_port}
115200
crtscts
lock
noauth
defaultroute
usepeerdns
persist
hide-password
ipcp-accept-local
ipcp-accept-remote
lcp-echo-interval 10
lcp-echo-failure 6
debug
logfile {log_file}
connect "{CHAT_PATH} -v -f {chat_file}"
"""
    (BASE / "ee-peer.tmp").write_text(peer_config, encoding="utf-8")
    run_cmd(["sudo", "cp", str(BASE / "ee-peer.tmp"), peer_file], check=False)
    run_cmd(["sudo", "chmod", "644", peer_file], check=False)

def keep_primary_and_add_ppp_secondary():
    try:
        out, _, _ = run_cmd([IP_PATH, "route", "show", "default"], check=False)
        if not out:
            return
        line = out.splitlines()[0]
        parts = line.split()
        gw = dev = None
        metric = 100
        for i, p in enumerate(parts):
            if p == "via" and i + 1 < len(parts):
                gw = parts[i + 1]
            elif p == "dev" and i + 1 < len(parts):
                dev = parts[i + 1]
            elif p == "metric" and i + 1 < len(parts):
                try:
                    metric = int(parts[i + 1])
                except Exception:
                    pass
        if dev and dev != "ppp0":
            print(f"  🔄 Keeping {dev} primary (metric {metric}); adding ppp0 as secondary…")
            run_cmd(["sudo", IP_PATH, "route", "replace", "default", "via", gw, "dev", dev, "metric", str(metric)], check=False)
            run_cmd(["sudo", IP_PATH, "route", "add", "default", "dev", "ppp0", "metric", str(metric + 500)], check=False)
            print("  ✅ Primary preserved; ppp0 added with higher metric")
    except Exception:
        pass

# ---------- config writers ----------

def make_token(nbytes: int = 48) -> str:
    return secrets.token_urlsafe(nbytes)

def write_config_yaml():
    cfg_path = BASE / "config.yaml"
    if cfg_path.exists():
        try:
            existing = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        except Exception:
            existing = {}
    else:
        existing = {}

    defaults = {
        "lan_bind_ip": detect_lan_ip(),
        "api": {"bind": "127.0.0.1", "port": 8088, "token": make_token()},
        "proxy": {"auth_enabled": False, "user": "", "password": ""},
        "modem": {
            "mode": "auto",  # "auto", "rndis", "ppp"
            "apn": "everywhere",
            "port": "/dev/ttyUSB2",
            "timeout": 30
        },
        "rotation": {
            "ppp_teardown_wait": 30,
            "ppp_restart_wait": 60,
            "max_attempts": 2,
            "deep_reset_enabled": False,
            "deep_reset_method": "mmcli",
            "deep_reset_wait": 180
        },
        "pm2": {"enabled": True, "auto_restart": True, "ip_rotation_interval": 300, "max_restarts": 10, "restart_delay": 5000},
        "discord": {"webhook_url": ""}
    }

    merged = defaults.copy()
    for k, v in existing.items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k].update(v)
        else:
            merged[k] = v

    cfg_path.write_text(yaml.safe_dump(merged, sort_keys=False), encoding="utf-8")
    print(f"  ✅ config.yaml written (LAN={merged['lan_bind_ip']})")
    return merged

def write_squid_conf(cfg: dict, cellular_ip=None):
    lan_ip = cfg["lan_bind_ip"]
    auth_enabled = bool(cfg["proxy"]["auth_enabled"])
    user = cfg["proxy"]["user"] or ""
    pw = cfg["proxy"]["password"] or ""

    # Add cellular routing if we have a cellular IP
    cellular_routing = ""
    if cellular_ip:
        cellular_routing = f"""
# Route traffic through cellular interface
tcp_outgoing_address {cellular_ip}
"""

    if auth_enabled and user and pw:
        content = f"""# Squid proxy with auth and cellular routing
http_port {lan_ip}:3128
{cellular_routing}
auth_param basic program /usr/lib/squid/basic_ncsa_auth /etc/squid/passwd
auth_param basic children 5
auth_param basic realm Squid proxy
auth_param basic credentialsttl 2 hours
auth_param basic casesensitive off

acl authenticated proxy_auth REQUIRED
http_access allow authenticated
http_access deny all

forwarded_for off
request_header_access X-Forwarded-For deny all
request_header_access Via deny all

cache_dir ufs /var/spool/squid 100 16 256
cache_mem 64 MB

access_log /var/log/squid/access.log
cache_log /var/log/squid/cache.log

dns_nameservers 8.8.8.8 1.1.1.1
"""
    else:
        content = f"""# Squid proxy without auth and cellular routing
http_port {lan_ip}:3128
{cellular_routing}
# Allow CONNECT to SSL ports for local networks
acl localnet src 192.168.0.0/16 10.0.0.0/8 172.16.0.0/12
acl SSL_ports port 443
acl Safe_ports port 80 443 21 70 210 1025-65535

http_access allow localnet CONNECT SSL_ports
http_access allow localhost CONNECT SSL_ports
http_access allow localnet
http_access allow localhost
http_access deny all

forwarded_for off
request_header_access X-Forwarded-For deny all
request_header_access Via deny all

cache_dir ufs /var/spool/squid 100 16 256
cache_mem 64 MB

access_log /var/log/squid/access.log
cache_log /var/log/squid/cache.log

dns_nameservers 8.8.8.8 1.1.1.1
"""
    (BASE / "squid.conf").write_text(content, encoding="utf-8")
    run_cmd(["sudo", "chown", "proxyuser:proxyuser", str(BASE / "squid.conf")], check=False)
    run_cmd(["sudo", "chmod", "644", str(BASE / "squid.conf")], check=False)
    print("  ✅ squid.conf ready")

def write_ecosystem():
    eco = BASE / "ecosystem.config.js"
    content = f"""module.exports = {{
  apps: [
    {{
      name: "4g-proxy-orchestrator",
      script: "orchestrator.py",
      interpreter: "python3",
      cwd: "{BASE.as_posix()}",
      autorestart: true,
      max_restarts: 10,
      restart_delay: 5000,
      env: {{
        PYTHONPATH: "{BASE.as_posix()}"
      }}
    }},
    {{
      name: "4g-proxy-web",
      script: "web_interface.py",
      interpreter: "python3",
      cwd: "{BASE.as_posix()}",
      autorestart: true,
      max_restarts: 10,
      restart_delay: 5000,
      env: {{
        PYTHONPATH: "{BASE.as_posix()}"
      }}
    }}
  ]
}}
"""
    eco.write_text(content, encoding="utf-8")
    print("  ✅ ecosystem.config.js written")

# ---------- activation / tests ----------

def activate_modem_via_rndis():
    """Try to activate modem via RNDIS interface."""
    print("📡 Activating SIM7600E-H modem via RNDIS…")
    
    # First, try to detect existing RNDIS interface
    iface, ip = detect_rndis_interface()
    
    if iface and ip:
        print(f"  ✅ RNDIS interface {iface} already active with IP {ip}")
        return iface, ip
    
    if iface:
        # Interface exists but no IP, try to get one
        ip = setup_rndis_interface(iface)
        if ip:
            return iface, ip
    
    print("  ❌ RNDIS interface not available")
    return None, None

def activate_modem_via_ppp(apn: str):
    """Fallback PPP activation with safety measures."""
    print("📡 Activating SIM7600E-H modem over PPP (fallback)…")
    print(f"  📡 Using APN: {apn}")

    # Perform safe reset first
    safe_modem_reset()

    print("  🔄 Stopping conflicts (ModemManager, lingering pppd)…")
    run_cmd([SYSTEMCTL_PATH, "stop", "ModemManager"], check=False)
    run_cmd(["sudo", "pkill", "pppd"], check=False)
    time.sleep(2)

    print("  🔍 Detecting AT port…")
    at_port = detect_modem_port()
    print(f"  📡 Using AT port: {at_port}")

    print("  🔧 Writing PPP chat/peer files…")
    create_ppp_config(apn, at_port)

    print("  🚀 Starting PPP session (pppd call ee)…")
    out, err, rc = run_cmd(["sudo", PPPD_PATH, "call", "ee"], check=False, timeout=60)
    if rc != 0 and err:
        print(f"  ⚠️ pppd error: {err}")

    print("  ⏳ Waiting for ppp0 IPv4…")
    for _ in range(60):
        time.sleep(1)
        out, _, _ = run_cmd([IP_PATH, "-4", "addr", "show", "ppp0"], check=False)
        if "inet " in out:
            print("  ✅ ppp0 is UP with IPv4")
            return True

    print("  ❌ ppp0 did not come up in time.")
    return False

def activate_modem(apn: str, mode: str = "auto"):
    """Main modem activation function with configurable mode.
    
    Args:
        apn: APN to use
        mode: "auto" (QMI → RNDIS → PPP), "qmi" (QMI only), "rndis" (RNDIS only), "ppp" (PPP only)
    """
    print(f"🚀 Starting modem activation (mode: {mode})...")
    
    if mode == "qmi":
        # Force QMI mode
        print("  📡 Using QMI mode (forced)")
        iface, ip = activate_modem_via_qmi()
        if iface and ip:
            return "qmi", iface, ip
        print("  ❌ QMI activation failed")
        return None, None, None
    
    elif mode == "ppp":
        # Force PPP mode
        print("  📡 Using PPP mode (forced)")
        if activate_modem_via_ppp(apn):
            return "ppp", "ppp0", None
        print("  ❌ PPP activation failed")
        return None, None, None
    
    elif mode == "rndis":
        # Force RNDIS mode
        print("  📡 Using RNDIS mode (forced)")
        iface, ip = activate_modem_via_rndis()
        if iface and ip:
            return "rndis", iface, ip
        print("  ❌ RNDIS activation failed")
        return None, None, None
    
    else:
        # Auto mode: Try QMI first (best for IP rotation), then RNDIS, fallback to PPP
        print("  📡 Auto mode: trying QMI → RNDIS → PPP...")
        
        # Try QMI first (most reliable for IP rotation)
        iface, ip = activate_modem_via_qmi()
        if iface and ip:
            return "qmi", iface, ip
        
        # Try RNDIS second
        print("  🔄 QMI failed, trying RNDIS...")
        iface, ip = activate_modem_via_rndis()
        if iface and ip:
            return "rndis", iface, ip
        
        # Fallback to PPP
        print("  🔄 RNDIS failed, trying PPP fallback...")
        if activate_modem_via_ppp(apn):
            return "ppp", "ppp0", None
        
        print("  ❌ QMI, RNDIS, and PPP activation all failed")
        return None, None, None

def proxy_test(lan_ip: str):
    try:
        r = requests.get(
            "https://api.ipify.org",
            proxies={"http": f"http://{lan_ip}:3128", "https": f"http://{lan_ip}:3128"},
            timeout=10
        )
        if r.ok:
            print(f"  ✅ Proxy test OK – Public IP via proxy: {r.text.strip()}")
        else:
            print("  ⚠️ Proxy test failed (HTTP status)")
    except Exception as e:
        print(f"  ⚠️ Proxy test failed: {e}")

def summary(cfg: dict):
    try:
        cur = requests.get("https://ipv4.icanhazip.com", timeout=8)
        direct_ip = cur.text.strip() if cur.ok else "Unknown"
    except Exception:
        direct_ip = "Unknown"
    lan_ip = cfg["lan_bind_ip"]
    print("\n" + "=" * 60)
    print("🎉 SETUP COMPLETE (main.py)")
    print("=" * 60)
    print(f"📡 HTTP Proxy: {lan_ip}:3128")
    print(f"🌐 Direct (no proxy) Public IP: {direct_ip}")
    print(f"📊 API Endpoint (orchestrator): http://127.0.0.1:8088")
    print("")
    print("🧪 Tests:")
    print(f"  curl -s https://api.ipify.org && echo")
    print(f"  curl -x http://{lan_ip}:3128 -s https://api.ipify.org && echo")
    print("")
    print("💡 Use this exact command on your local machine:")
    print(f"  curl -x http://{lan_ip}:3128 -s https://api.ipify.org && echo")
    print("")
    print("📋 Proxy endpoints:")
    print(f"  HTTP Proxy: {lan_ip}:3128")
    print(f"  SOCKS Proxy: {lan_ip}:1080")
    print("")
    print("🌐 Use from other machines on your network:")
    print(f"  HTTP Proxy: {lan_ip}:3128")
    print(f"  SOCKS Proxy: {lan_ip}:1080")
    print("")
    print("💻 Example usage in applications:")
    print(f"  curl -x http://{lan_ip}:3128 https://api.ipify.org")
    print(f"  export http_proxy=http://{lan_ip}:3128")
    print(f"  export https_proxy=http://{lan_ip}:3128")
    print("=" * 60)

def main():
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--ecosystem-only":
        write_ecosystem()
        return 0
    
    if os.geteuid() != 0:
        print("❌ Run as root: sudo ./run.sh")
        return 1

    print("🚀 Raspberry Pi 5 + SIM7600E-H 4G Proxy (RNDIS + PPP fallback)")

    cfg = write_config_yaml()
    write_ecosystem()

    # Get modem settings from config
    modem_cfg = cfg.get("modem") or {}
    apn = modem_cfg.get("apn", "everywhere") if isinstance(modem_cfg, dict) else "everywhere"
    modem_mode = modem_cfg.get("mode", "auto") if isinstance(modem_cfg, dict) else "auto"
    
    # Try to activate modem with configured mode
    mode, iface, cellular_ip = activate_modem(apn, modem_mode)
    
    if mode == "rndis":
        print(f"  ✅ Cellular connection via RNDIS: {iface} ({cellular_ip})")
        write_squid_conf(cfg, cellular_ip)
        print("  🔄 Squid will be restarted by run.sh to apply new configuration")
    elif mode == "ppp":
        print(f"  ✅ Cellular connection via PPP: {iface}")
        write_squid_conf(cfg)
        keep_primary_and_add_ppp_secondary()
        # Restart Squid for PPP mode
        run_cmd([SYSTEMCTL_PATH, "restart", "squid"], check=False)
        proxy_test(cfg["lan_bind_ip"])
    else:
        print("  ⚠️ No cellular connection established; using LAN only")
        write_squid_conf(cfg)
        # Restart Squid for LAN-only mode
        run_cmd([SYSTEMCTL_PATH, "restart", "squid"], check=False)
        proxy_test(cfg["lan_bind_ip"])

    summary(cfg)
    return 0

if __name__ == "__main__":
    sys.exit(main())
