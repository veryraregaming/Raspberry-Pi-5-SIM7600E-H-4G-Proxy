#!/usr/bin/env python3
"""
Raspberry Pi 5 + SIM7600E-H 4G Proxy - Auto Setup (safe routing version)
- Leaves system default route intact (eth0/wlan0 remain primary)
- Adds ppp0 as a LOWER-PRIORITY default (higher metric)
- Idempotently writes: config.yaml, squid.conf, ecosystem.config.js
- Creates correct PPP chat/peer files for APN dialing
- Brings PPP up and preserves LAN as primary route
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

# ---------- modem / PPP ----------

def detect_modem_port():
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
    return "/dev/ttyUSB2"

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

def write_squid_conf(cfg: dict):
    lan_ip = cfg["lan_bind_ip"]
    auth_enabled = bool(cfg["proxy"]["auth_enabled"])
    user = cfg["proxy"]["user"] or ""
    pw = cfg["proxy"]["password"] or ""

    if auth_enabled and user and pw:
        content = f"""# Squid proxy with auth (IPv4 + IPv6)
http_port {lan_ip}:3128
http_port [::]:3128

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
dns_v4_first off
"""
    else:
        content = f"""# Squid proxy without auth (IPv4 + IPv6)
http_port {lan_ip}:3128
http_port [::]:3128

http_access allow all

forwarded_for off
request_header_access X-Forwarded-For deny all
request_header_access Via deny all

cache_dir ufs /var/spool/squid 100 16 256
cache_mem 64 MB

access_log /var/log/squid/access.log
cache_log /var/log/squid/cache.log

dns_nameservers 8.8.8.8 1.1.1.1
dns_v4_first off
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

def activate_modem_via_ppp(apn: str):
    print("📡 Activating SIM7600E-H modem over PPP…")
    print(f"  📡 Using APN: {apn}")

    print("  🔄 Stopping conflicts (ModemManager, lingering pppd)…")
    run_cmd([SYSTEMCTL_PATH, "stop", "ModemManager"], check=False)
    run_cmd(["sudo", "pkill", "pppd"], check=False)
    time.sleep(1.5)

    print("  🔍 Detecting AT port…")
    at_port = detect_modem_port()
    print(f"  📡 Using AT port: {at_port}")

    print("  🔧 Writing PPP chat/peer files…")
    create_ppp_config(apn, at_port)

    print("  🚀 Starting PPP session (pppd call ee)…")
    out, err, rc = run_cmd(["sudo", PPPD_PATH, "call", "ee"], check=False, timeout=30)
    if rc != 0 and err:
        print(f"  ⚠️ pppd error: {err}")

    print("  ⏳ Waiting for ppp0 IPv4…")
    for _ in range(30):
        time.sleep(1)
        out, _, _ = run_cmd([IP_PATH, "-4", "addr", "show", "ppp0"], check=False)
        if "inet " in out:
            print("  ✅ ppp0 is UP with IPv4")
            return True

    print("  ❌ ppp0 did not come up in time.")
    return False

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
    print(f"  curl -x http://[::1]:3128 -s https://api6.ipify.org && echo  # IPv6 test")
    print("=" * 60)

def main():
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--ecosystem-only":
        write_ecosystem()
        return 0
    
    if os.geteuid() != 0:
        print("❌ Run as root: sudo ./run.sh")
        return 1

    print("🚀 Raspberry Pi 5 + SIM7600E-H 4G Proxy (safe policy-routing)")

    cfg = write_config_yaml()
    write_squid_conf(cfg)
    write_ecosystem()

    apn = (cfg.get("modem") or {}).get("apn", "everywhere") if isinstance(cfg.get("modem"), dict) else "everywhere"
    ok = activate_modem_via_ppp(apn)
    if not ok:
        print("⚠️ PPP activation did not bring up ppp0; continuing (check logs)")

    keep_primary_and_add_ppp_secondary()
    proxy_test(cfg["lan_bind_ip"])
    summary(cfg)
    return 0

if __name__ == "__main__":
    sys.exit(main())
