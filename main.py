#!/usr/bin/env python3
"""
Raspberry Pi 5 + SIM7600E-H 4G Proxy - Auto Setup (RNDIS + PPP fallback)
- Detects and uses RNDIS/ECM interface (enx*/eth1/usb0) for cellular connection
- Optional QMI bring-up (best effort) with dhclient for DHCP
- Falls back to PPP if RNDIS/QMI is not available
- Prevents modem lockouts with proper error handling
- Idempotently writes: config.yaml, squid.conf, ecosystem.config.js
- Safe routing that preserves LAN connectivity
- Carrier-aware PPP/QMI setup via carriers.json (EE / Three / others)
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
DHCLIENT_PATH = which("dhclient", "/sbin/dhclient")  # used for RNDIS/QMI DHCP

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

# ---------- carriers / APN auto-detect ----------

def load_carriers():
    """Load carriers.json mapping (APN templates)."""
    path = BASE / "carriers.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("carriers", {})
    except Exception:
        return {}

def at_query(port, cmd, sleep=0.3, read_bytes=2048):
    with serial.Serial(port, 115200, timeout=1) as ser:
        ser.write((cmd + "\r\n").encode())
        time.sleep(sleep)
        return ser.read(read_bytes).decode(errors="ignore")

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

    print("  ⚠️ No responding AT port found, using default: /dev/ttyUSB2")
    return "/dev/ttyUSB2"

def get_imsi_and_operator():
    """Return (imsi, operator_name) using AT+CIMI and AT+COPS?"""
    port = detect_modem_port()
    imsi = None
    op = None
    try:
        r = at_query(port, "AT+CIMI", sleep=0.5, read_bytes=128)
        m = re.search(r"\b(\d{15})\b", r)
        if m:
            imsi = m.group(1)
    except Exception:
        pass
    try:
        r = at_query(port, "AT+COPS?", sleep=0.5, read_bytes=256)
        # +COPS: 0,0,"EE",7   OR   +COPS: 0,2,"23420",7
        m = re.search(r'\+COPS:.*?"([^"]+)"', r)
        if m:
            op = m.group(1).strip()
    except Exception:
        pass
    return imsi, op

def mcc_mnc_from_imsi(imsi):
    if not imsi or len(imsi) < 5:
        return None, None
    mcc = imsi[:3]
    mnc2 = imsi[3:5]
    mnc3 = imsi[3:6] if len(imsi) >= 6 else None
    return mcc, (mnc3 if mnc3 and mnc3[0] != '0' else mnc2)

def guess_carrier_key(imsi, operator, carriers):
    """Map to a key in carriers.json (best-effort for EE/Three)."""
    mcc, mnc = mcc_mnc_from_imsi(imsi)
    if mcc == "234" and mnc in {"30", "33"}:
        return "ee"
    if mcc == "234" and mnc in {"20"}:
        return "three" if "three" in carriers else "three_payg"
    if operator:
        op_low = operator.lower()
        if "ee" in op_low:
            return "ee"
        if op_low == "3" or op_low.startswith("3 ") or "three" in op_low:
            return "three" if "three" in carriers else "three_payg"
        if "vodafone" in op_low:
            return "vodafone_payg" if "vodafone_payg" in carriers else "vodafone"
        if "o2" in op_low:
            return "o2_contract" if "o2_contract" in carriers else "o2_payg"
    for pref in ("ee", "three", "three_payg"):
        if pref in carriers:
            return pref
    return None

def choose_apn_credentials(configured_apn):
    """
    Decide APN/username/password:
      - if configured_apn == 'auto' (or missing) → detect from SIM using carriers.json
      - else use configured_apn and try to pull matching creds from carriers.json by value
    """
    carriers = load_carriers()
    apn, user, pw = None, "", ""
    if not configured_apn or str(configured_apn).lower() == "auto":
        print("  🔍 Auto-detecting carrier/APN from SIM…")
        imsi, operator = get_imsi_and_operator()
        print(f"    IMSI: {imsi or 'Unknown'}, Operator: {operator or 'Unknown'}")
        key = guess_carrier_key(imsi, operator, carriers)
        if key and key in carriers:
            c = carriers[key]
            apn = c.get("apn")
            user = c.get("username") or ""
            pw = c.get("password") or ""
            print(f"  ✅ Carrier matched: {c.get('name','unknown')} → APN {apn}")
    else:
        apn = configured_apn
        for _, c in carriers.items():
            if str(c.get("apn","")).lower() == str(apn).lower():
                user = c.get("username") or ""
                pw = c.get("password") or ""
                break
    if not apn:
        apn, user, pw = "everywhere", "eesecure", "secure"  # fallback (EE UK)
        print("  ⚠️ Could not auto-detect; falling back to EE defaults (everywhere).")
    return apn, user, pw

# ---------- modem detection (QMI + RNDIS + PPP) ----------

def detect_qmi_interface():
    """Detect QMI/WWAN interface (wwan*) that could provide cellular connectivity."""
    try:
        out, _, _ = run_cmd([IP_PATH, "-br", "link", "show"], check=False)
        for line in out.splitlines():
            if line.startswith("wwan"):
                iface = line.split()[0]
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

def switch_modem_to_qmi():
    """Switch modem to QMI/RNDIS-capable USB mode using AT+CUSBPIDSWITCH=9011,1,1."""
    try:
        print("  🔄 Checking if modem needs to be switched to QMI/RNDIS USB mode...")
        modem_dev = detect_modem_port()
        if not os.path.exists(modem_dev):
            print("  ⚠️ No modem control port found")
            return False

        with serial.Serial(modem_dev, 115200, timeout=5) as ser:
            ser.write(b"AT+CUSBPIDSWITCH?\r\n")
            time.sleep(1)
            response = ser.read(1000).decode('utf-8', errors='ignore')
            if '9011' in response:
                print("  ✅ Modem already in 9011 (QMI/RNDIS) mode")
                return True

            print("  🔧 Switching modem to 9011 (QMI/RNDIS) mode...")
            ser.write(b"AT+CUSBPIDSWITCH=9011,1,1\r\n")
            time.sleep(2)
            response = ser.read(1000).decode('utf-8', errors='ignore')

            if 'OK' in response:
                print("  ✅ Modem switched; rebooting module…")
                ser.write(b"AT+CRESET\r\n")
                time.sleep(5)
                print("  ⏳ Waiting 15 seconds for modem to re-enumerate…")
                time.sleep(15)
                return True
            else:
                print(f"  ⚠️ Failed to switch to 9011: {response}")
                return False

    except Exception as e:
        print(f"  ⚠️ Error switching to QMI/RNDIS mode: {e}")
        return False

def setup_qmi_interface(iface, apn="everywhere"):
    """Setup QMI interface using qmicli and DHCP via dhclient."""
    print(f"  🔧 Setting up QMI interface: {iface}")

    qmi_dev = "/dev/cdc-wdm0"
    if not os.path.exists(qmi_dev):
        print(f"  ⚠️ QMI device {qmi_dev} not found")
        return None

    run_cmd(["sudo", IP_PATH, "link", "set", "dev", iface, "up"], check=False)
    time.sleep(2)

    print(f"  📡 Starting QMI connection for {iface} with APN: {apn}...")
    out, err, rc = run_cmd([
        "sudo", "qmicli", "-d", qmi_dev,
        "--wds-start-network", f"apn={apn}",
        "--client-no-release-cid"
    ], check=False, timeout=30)

    if rc == 0:
        print(f"  📡 Getting IP via DHCP (dhclient) for {iface}...")
        _, err2, rc2 = run_cmd(["sudo", DHCLIENT_PATH, "-v", iface], check=False, timeout=30)
        ip = detect_ipv4(iface)
        if ip:
            print(f"  ✅ QMI interface {iface} configured with IP: {ip}")
            return ip
        else:
            print(f"  ⚠️ QMI started but no IP on {iface} (dhclient rc={rc2}, err={err2})")
    else:
        print(f"  ⚠️ QMI network start failed: {err}")

    return None

def activate_modem_via_qmi(apn):
    """Try to activate modem via QMI interface."""
    print("📡 Activating SIM7600E-H modem via QMI…")

    switch_modem_to_qmi()

    iface, ip = detect_qmi_interface()
    if iface and ip:
        print(f"  ✅ QMI interface {iface} already active with IP {ip}")
        return iface, ip

    if iface:
        ip = setup_qmi_interface(iface, apn)
        if ip:
            return iface, ip

    print("  ❌ QMI interface not available")
    return None, None

def detect_rndis_interface():
    """Detect RNDIS/ECM interface (enx*/eth1/usb0) that provides cellular connectivity."""
    try:
        out, _, _ = run_cmd([IP_PATH, "-br", "link", "show"], check=False)
        for line in out.splitlines():
            if line.startswith(("enx", "eth1", "usb0")):
                iface = line.split()[0]
                ip = detect_ipv4(iface)
                if ip:
                    print(f"  ✅ RNDIS/ECM interface found: {iface} with IP {ip}")
                    return iface, ip
                else:
                    print(f"  🔍 RNDIS/ECM interface found: {iface} (no IP yet)")
                    return iface, None
    except Exception:
        pass
    return None, None

def setup_rndis_interface(iface):
    """Setup RNDIS/ECM interface with DHCP via dhclient."""
    print(f"  🔧 Setting up RNDIS/ECM interface: {iface}")
    run_cmd(["sudo", IP_PATH, "link", "set", "dev", iface, "up"], check=False)
    time.sleep(2)

    print(f"  📡 Getting IP via DHCP (dhclient) for {iface}...")
    out, err, rc = run_cmd(["sudo", DHCLIENT_PATH, "-v", iface], check=False, timeout=30)

    if rc == 0:
        ip = detect_ipv4(iface)
        if ip:
            print(f"  ✅ RNDIS/ECM interface {iface} configured with IP: {ip}")
            return ip
        else:
            print(f"  ⚠️ DHCP succeeded but no IP detected on {iface}")
    else:
        print(f"  ⚠️ DHCP failed for {iface}: {err}")

    return None

def safe_modem_reset():
    """Safely reset modem to prevent lockouts."""
    print("  🔄 Performing safe modem reset...")

    run_cmd(["sudo", "pkill", "pppd"], check=False)
    time.sleep(2)

    try:
        at_port = detect_modem_port()
        with serial.Serial(at_port, 115200, timeout=1) as ser:
            ser.write(b"+++\r")
            time.sleep(3)
            ser.write(b"AT\r")
            time.sleep(1)
            resp = ser.read_all().decode(errors="ignore")
            if "OK" in resp:
                print("  ✅ Modem reset to command mode")
                return True
    except Exception as e:
        print(f"  ⚠️ Modem reset failed: {e}")

    return False

def create_ppp_config(apn: str, at_port: str, username: str = "", password: str = ""):
    """
    Write PPP chat/peer files.
    - For EE: username=eesecure, password=secure
    - For Three: both blank
    """
    chat_file = "/etc/chatscripts/carrier-chat"
    peer_file = "/etc/ppp/peers/carrier"
    log_file = "/var/log/ppp-carrier.log"
    chap_secrets_file = "/etc/ppp/chap-secrets"

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
    (BASE / "carrier-chat.tmp").write_text(chat_script, encoding="utf-8")
    run_cmd(["sudo", "cp", str(BASE / "carrier-chat.tmp"), chat_file], check=False)
    run_cmd(["sudo", "chmod", "644", chat_file], check=False)

    if username or password:
        chap_secrets_content = f"""# Secrets for CHAP
# client        server  secret                  IP addresses
{username or '*'}        *       {password or '*'}                  *
"""
        (BASE / "chap-secrets.tmp").write_text(chap_secrets_content, encoding="utf-8")
        run_cmd(["sudo", "cp", str(BASE / "chap-secrets.tmp"), chap_secrets_file], check=False)
        run_cmd(["sudo", "chmod", "600", chap_secrets_file], check=False)

    name_line = f'name "{username}"' if username else "noauth"
    peer_config = f"""{at_port}
115200
crtscts
lock
{name_line}
defaultroute
usepeerdns
persist
hide-password
ipcp-accept-local
ipcp-accept-remote
noipv6
noipdefault
lcp-echo-interval 10
lcp-echo-failure 6
noccp
novj
novjccomp
nobsdcomp
nodeflate
nopcomp
noaccomp
debug
logfile {log_file}
connect "{CHAT_PATH} -v -f {chat_file}"
"""
    (BASE / "carrier-peer.tmp").write_text(peer_config, encoding="utf-8")
    run_cmd(["sudo", "cp", str(BASE / "carrier-peer.tmp"), peer_file], check=False)
    run_cmd(["sudo", "chmod", "644", peer_file], check=False)

def setup_rndis_policy_routing(rndis_iface):
    """Policy routing for RNDIS/ECM; mark traffic from Squid user 'proxy'."""
    try:
        print(f"  🔧 Setting up policy routing for RNDIS/ECM interface: {rndis_iface}")
        table_id = 101
        table_name = "rndis"
        rt_tables = "/etc/iproute2/rt_tables"

        if run_cmd(["sudo", "grep", "-q", f"^{table_id} {table_name}$", rt_tables], check=False)[2] != 0:
            run_cmd(["sudo", "bash", "-c", f"echo '{table_id} {table_name}' >> {rt_tables}"], check=False)

        run_cmd(["sudo", IP_PATH, "route", "replace", "default", "dev", rndis_iface, "table", table_name], check=False)

        run_cmd(["sudo", IP_PATH, "rule", "del", "fwmark", "0x1", "lookup", table_name], check=False)
        run_cmd(["sudo", IP_PATH, "rule", "add", "fwmark", "0x1", "lookup", table_name, "priority", "1001"], check=False)

        run_cmd(["sudo", "iptables", "-t", "mangle", "-D", "OUTPUT", "-m", "owner", "--uid-owner", "proxy", "-j", "MARK", "--set-mark", "1"], check=False)
        # IMPORTANT: do NOT mark root (to keep SSH stable)
        run_cmd(["sudo", "iptables", "-t", "mangle", "-A", "OUTPUT", "-m", "owner", "--uid-owner", "proxy", "-j", "MARK", "--set-mark", "1"], check=False)
        print(f"  ✅ Policy routing configured: Squid traffic via {rndis_iface}")
    except Exception as e:
        print(f"  ⚠️ Policy routing setup failed: {e}")

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
    is_new_install = not cfg_path.exists()

    if cfg_path.exists():
        try:
            existing = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        except Exception:
            existing = {}
    else:
        existing = {}

    default_run_optimization = is_new_install
    defaults = {
        "lan_bind_ip": detect_lan_ip(),
        "api": {"bind": "127.0.0.1", "port": 8088, "token": make_token()},
        "proxy": {"auth_enabled": False, "user": "", "password": ""},
        "modem": {
            "mode": "auto",        # "auto", "rndis", "qmi", "ppp"
            "apn": "auto",         # "auto" chooses from carriers.json via SIM
            "port": "/dev/ttyUSB2",
            "timeout": 30
        },
        "rotation": {
            "ppp_teardown_wait": 30,
            "ppp_restart_wait": 60,
            "max_attempts": 2,
            "run_optimization": default_run_optimization,
            "randomise_imei": False,
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

    if is_new_install:
        print(f"  ✅ config.yaml written (LAN={merged['lan_bind_ip']}, NEW INSTALL - optimization enabled)")
    else:
        print(f"  ✅ config.yaml updated (LAN={merged['lan_bind_ip']}, existing settings preserved)")

    return merged

def write_squid_conf(cfg: dict, cellular_ip=None):
    lan_ip = cfg["lan_bind_ip"]
    auth_enabled = bool(cfg["proxy"]["auth_enabled"])
    user = cfg["proxy"]["user"] or ""
    pw = cfg["proxy"]["password"] or ""

    cellular_routing = ""
    if cellular_ip:
        cellular_routing = f"""
# Route traffic through cellular interface (ppp/qmi)
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

    iface, ip = detect_rndis_interface()
    if iface and ip:
        print(f"  ✅ RNDIS interface {iface} already active with IP {ip}")
        return iface, ip

    if iface:
        ip = setup_rndis_interface(iface)
        if ip:
            return iface, ip

    print("  ❌ RNDIS interface not available")
    return None, None

def activate_modem_via_ppp(apn: str, username: str, password: str):
    """Fallback PPP activation with safety measures."""
    print("📡 Activating SIM7600E-H modem over PPP (fallback)…")
    print(f"  📡 Using APN: {apn}")

    safe_modem_reset()

    print("  🔄 Stopping conflicts (ModemManager, lingering pppd)…")
    run_cmd([SYSTEMCTL_PATH, "stop", "ModemManager"], check=False)
    run_cmd(["sudo", "pkill", "pppd"], check=False)
    time.sleep(2)

    print("  🔍 Detecting AT port…")
    at_port = detect_modem_port()
    print(f"  📡 Using AT port: {at_port}")

    print("  🔧 Writing PPP chat/peer files…")
    create_ppp_config(apn, at_port, username=username, password=password)

    print("  🚀 Starting PPP session (pppd call carrier)…")
    out, err, rc = run_cmd(["sudo", PPPD_PATH, "call", "carrier"], check=False, timeout=150)
    if rc != 0 and err:
        print(f"  ⚠️ pppd error: {err}")

    print("  ⏳ Waiting for ppp0 IPv4…")
    for i in range(120):
        time.sleep(1)
        out, _, _ = run_cmd([IP_PATH, "-4", "addr", "show", "ppp0"], check=False)
        if "inet " in out:
            print("  ✅ ppp0 is UP with IPv4")
            ipm = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)/", out)
            return True, (ipm.group(1) if ipm else None)
        if (i + 1) % 10 == 0:
            print(f"  ⏳ Still waiting... ({i + 1}s)")

    print("  ❌ ppp0 did not come up in time.")
    return False, None

def activate_modem(apn_setting: str, mode: str = "auto", username: str = "", password: str = ""):
    """Main modem activation with selectable mode."""
    print(f"🚀 Starting modem activation (mode: {mode})...")

    if mode == "qmi":
        print("  📡 Using QMI mode (forced)")
        iface, ip = activate_modem_via_qmi(apn_setting)
        if iface and ip:
            return "qmi", iface, ip
        print("  ❌ QMI activation failed")
        return None, None, None

    elif mode == "ppp":
        print("  📡 Using PPP mode (forced)")
        ok, ppp_ip = activate_modem_via_ppp(apn_setting, username, password)
        if ok:
            return "ppp", "ppp0", ppp_ip
        print("  ❌ PPP activation failed")
        return None, None, None

    elif mode == "rndis":
        print("  📡 Using RNDIS mode (forced)")
        iface, ip = activate_modem_via_rndis()
        if iface and ip:
            return "rndis", iface, ip
        print("  ❌ RNDIS activation failed")
        return None, None, None

    else:
        print("  📡 Auto mode: trying RNDIS → PPP...")
        iface, ip = activate_modem_via_rndis()
        if iface and ip:
            return "rndis", iface, ip

        print("  🔄 RNDIS not available, trying PPP fallback...")
        ok, ppp_ip = activate_modem_via_ppp(apn_setting, username, password)
        if ok:
            return "ppp", "ppp0", ppp_ip

        print("  ❌ Both RNDIS and PPP activation failed")
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
    if len(sys.argv) > 1 and sys.argv[1] == "--ecosystem-only":
        write_ecosystem()
        return 0

    if os.geteuid() != 0:
        print("❌ Run as root: sudo ./run.sh")
        return 1

    print("🚀 Raspberry Pi 5 + SIM7600E-H 4G Proxy (RNDIS + PPP fallback)")

    cfg = write_config_yaml()
    write_ecosystem()

    modem_cfg = cfg.get("modem") or {}
    requested_apn = modem_cfg.get("apn", "auto") if isinstance(modem_cfg, dict) else "auto"
    modem_mode = modem_cfg.get("mode", "auto") if isinstance(modem_cfg, dict) else "auto"

    apn, username, password = choose_apn_credentials(requested_apn)

    mode, iface, cellular_ip = activate_modem(apn, modem_mode, username=username, password=password)

    if mode == "rndis":
        print(f"  ✅ Cellular connection via RNDIS: {iface} ({cellular_ip})")
        write_squid_conf(cfg)  # policy routing handles egress
        setup_rndis_policy_routing(iface)
        print("  🔄 Squid will be restarted by run.sh to apply new configuration")
    elif mode == "ppp":
        print(f"  ✅ Cellular connection via PPP: {iface}")
        write_squid_conf(cfg, cellular_ip=cellular_ip)  # bind to PPP IP
        keep_primary_and_add_ppp_secondary()
        run_cmd([SYSTEMCTL_PATH, "restart", "squid"], check=False)
        proxy_test(cfg["lan_bind_ip"])
    elif mode == "qmi":
        print(f"  ✅ Cellular connection via QMI: {iface} ({cellular_ip})")
        write_squid_conf(cfg, cellular_ip=cellular_ip)
        run_cmd([SYSTEMCTL_PATH, "restart", "squid"], check=False)
        proxy_test(cfg["lan_bind_ip"])
    else:
        print("  ⚠️ No cellular connection established; using LAN only")
        write_squid_conf(cfg)
        run_cmd([SYSTEMCTL_PATH, "restart", "squid"], check=False)
        proxy_test(cfg["lan_bind_ip"])

    summary(cfg)
    return 0

if __name__ == "__main__":
    sys.exit(main())
