#!/usr/bin/env python3
"""
Raspberry Pi 5 + SIM7600E-H 4G Proxy - Auto Setup (safe routing version)
- Keeps system default route intact (no messing with eth0/wlan0)
- Routes ONLY proxy traffic via SIM using policy routing
- PM2-managed services
"""

import os
import sys
import subprocess
import yaml
import secrets
import socket
import time
import requests
import serial

# ----------------- helpers -----------------

def run_cmd(cmd, check=True):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, check=check)
        return r.stdout.strip(), r.stderr.strip()
    except subprocess.CalledProcessError as e:
        print(f"[cmd] {cmd}\n[err] {e.stderr}")
        return "", e.stderr

def detect_lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        return local_ip
    except Exception:
        out, _ = run_cmd("ip route | awk '/default/ {print $5}' | head -n1")
        if out:
            ip_out, _ = run_cmd(f"ip addr show {out} | awk '/inet / {{print $2}}' | cut -d/ -f1")
            if ip_out:
                return ip_out
        return "192.168.1.37"

def generate_token():
    return secrets.token_urlsafe(64)

def detect_modem_port():
    for dev in os.listdir('/dev'):
        if dev.startswith('ttyUSB'):
            return f'/dev/{dev}'
    return '/dev/ttyUSB2'

def send_at_command(cmd, port=None, timeout=2):
    """Send AT command to modem and return response"""
    if port is None:
        port = detect_modem_port()
    
    try:
        with serial.Serial(port, 115200, timeout=timeout) as ser:
            ser.write((cmd + '\r\n').encode())
            time.sleep(0.5)
            response = ser.read_all().decode(errors='ignore').strip()
            return response
    except Exception as e:
        print(f"  ⚠️ AT command failed: {e}")
        return ""

def activate_modem():
    """Activate SIM7600E-H modem in direct mode"""
    print("📡 Activating SIM7600E-H modem...")
    port = detect_modem_port()
    
    # Load config to get APN
    try:
        with open("config.yaml", "r") as f:
            config = yaml.safe_load(f)
        apn = config.get("modem", {}).get("apn", "internet")
    except:
        apn = "internet"
    
    print(f"  📡 Using APN: {apn}")
    
    # Check if modem responds
    response = send_at_command("AT", port)
    if "OK" not in response:
        print(f"  ❌ Modem not responding on {port}")
        return False
    
    print(f"  ✅ Modem responding on {port}")
    
    # Configure modem for direct mode
    commands = [
        "AT+CFUN=1",           # Enable full functionality
        "AT+CPIN?",            # Check SIM status
        "AT+CREG?",            # Check network registration
        "AT+CGATT?",           # Check GPRS attachment
        f"AT+CGDCONT=1,\"IP\",\"{apn}\"",  # Configure PDP context with configured APN
        "AT+CGACT=1,1",        # Activate PDP context
        "AT+CGPADDR"           # Get IP address
    ]
    
    for cmd in commands:
        print(f"  📤 {cmd}")
        response = send_at_command(cmd, port)
        print(f"  📥 {response}")
        time.sleep(1)
    
    # Check if we got an IP
    ip_response = send_at_command("AT+CGPADDR", port)
    if "+CGPADDR: 1," in ip_response and "0.0.0.0" not in ip_response:
        print("  ✅ Modem activated with IP address")
        return True
    else:
        print("  ⚠️ Modem activated but no IP address")
        # Try with common APNs if configured APN doesn't work
        return try_common_apns(port)

def try_common_apns(port):
    """Try common APN configurations if default fails"""
    print("  🔄 Trying common APNs...")
    
    # Common APNs for different carriers
    apns = [
        "internet",     # Generic
        "web",          # Some carriers
        "data",         # Some carriers  
        "broadband",    # Some carriers
        "mobile",       # Some carriers
        "3gnet",        # Some carriers
        "fast.t-mobile.com",  # T-Mobile
        "broadband",    # Verizon
        "internet",     # AT&T
        "internet",     # EE (UK)
        "internet",     # Vodafone
        "internet",     # Three
    ]
    
    for apn in apns:
        print(f"  📤 Trying APN: {apn}")
        
        # Configure PDP context with specific APN
        send_at_command("AT+CGDCONT=1,\"IP\",\"" + apn + "\"", port)
        time.sleep(1)
        
        # Activate PDP context
        send_at_command("AT+CGACT=1,1", port)
        time.sleep(3)
        
        # Check for IP
        ip_response = send_at_command("AT+CGPADDR", port)
        print(f"  📥 {ip_response}")
        
        if "+CGPADDR: 1," in ip_response and "0.0.0.0" not in ip_response:
            print(f"  ✅ Success with APN: {apn}")
            return True
    
    print("  ❌ No working APN found")
    return False

# ----------------- install steps -----------------

def install_pm2():
    print("  Installing Node.js + PM2…")
    run_cmd("curl -fsSL https://deb.nodesource.com/setup_18.x | bash -", check=False)
    run_cmd("apt install -y nodejs", check=False)
    run_cmd("npm install -g pm2", check=False)
    print("  ✅ PM2 ready")

# Squid installation is handled by run.sh

def install_dependencies():
    print("🔧 Installing dependencies…")
    pkgs = [
        "python3","python3-pip","python3-yaml","python3-serial",
        "python3-requests","iptables","python3-flask","curl","wget","unzip","build-essential"
    ]
    for p in pkgs:
        print(f"  apt install -y {p}")
        run_cmd(f"apt install -y {p}", check=False)
    install_pm2()
    # Squid is installed by run.sh

# ----------------- config -----------------

def create_config():
    print("📝 Creating config.yaml")
    lan_ip = detect_lan_ip()
    token = generate_token()
    cfg = {
        "lan_bind_ip": lan_ip,
        "api": {"bind": "127.0.0.1", "port": 8088, "token": token},
        "proxy": {"auth_enabled": False, "user": "", "password": ""},
        "modem": {
            "apn": "internet",  # Default APN, can be overridden
            "port": "/dev/ttyUSB2",  # Default port, auto-detected
            "timeout": 2
        },
        "pm2": {"enabled": True, "auto_restart": True, "ip_rotation_interval": 300, "max_restarts": 10, "restart_delay": 5000}
    }
    with open("config.yaml","w") as f:
        yaml.dump(cfg, f, default_flow_style=False)
    print(f"  ✅ LAN IP: {lan_ip}")
    print(f"  ✅ API Token: {token[:20]}…")
    print(f"  ✅ Default APN: internet (edit config.yaml to customize)")
    print("  ✅ Proxy auth: disabled (edit config.yaml later if you want auth)")
    return cfg

def create_squid_config(cfg):
    print("🔧 Writing squid.conf")
    lan_ip = cfg["lan_bind_ip"]
    auth_enabled = cfg["proxy"]["auth_enabled"]
    user = cfg["proxy"]["user"]
    pw = cfg["proxy"]["password"]

    if auth_enabled and user and pw:
        proxy_cfg = f"""# Squid proxy with auth
http_port {lan_ip}:3128

# Authentication
auth_param basic program /usr/lib/squid/basic_ncsa_auth /etc/squid/passwd
auth_param basic children 5
auth_param basic realm Squid proxy-caching web server
auth_param basic credentialsttl 2 hours
auth_param basic casesensitive off

# Access control
acl authenticated proxy_auth REQUIRED
http_access allow authenticated
http_access deny all

# Forward settings
forwarded_for off
request_header_access X-Forwarded-For deny all
request_header_access Via deny all

# Cache settings
cache_dir ufs /var/spool/squid 100 16 256
cache_mem 64 MB

# Logging
access_log /var/log/squid/access.log
cache_log /var/log/squid/cache.log

# DNS
dns_nameservers 8.8.8.8 8.8.4.4
"""
    else:
        proxy_cfg = f"""# Squid proxy without auth
http_port {lan_ip}:3128

# Allow all connections (no auth by default)
http_access allow all

# Forward settings
forwarded_for off
request_header_access X-Forwarded-For deny all
request_header_access Via deny all

# Cache settings
cache_dir ufs /var/spool/squid 100 16 256
cache_mem 64 MB

# Logging
access_log /var/log/squid/access.log
cache_log /var/log/squid/cache.log

# DNS
dns_nameservers 8.8.8.8 8.8.4.4
"""
    with open("squid.conf","w") as f:
        f.write(proxy_cfg)
    print("  ✅ squid.conf ready (HTTP:3128 on LAN IP)")

# ----------------- networking -----------------

def setup_network():
    """Apply policy routing for proxy-only traffic via SIM."""
    print("🌐 Setting policy routing (no default route changes)…")
    
    # Load config to get APN settings
    try:
        with open("config.yaml", "r") as f:
            config = yaml.safe_load(f)
        apn = config.get("modem", {}).get("apn", "internet")
        print(f"  📡 Using APN: {apn}")
    except:
        apn = "internet"
        print(f"  📡 Using default APN: {apn}")
    
    # First activate the modem
    if not activate_modem():
        print("  ⚠️ Modem activation failed, continuing anyway")
    
    # Then setup network routing
    out, err = run_cmd("bash ./4gproxy-net.sh", check=False)
    if out: print(out)
    if err and "ERROR" in err:
        print(err)
        return False
    print("  ✅ Network policy set")
    return True

# ----------------- pm2 -----------------

def create_pm2_ecosystem():
    print("🔧 Creating PM2 ecosystem.config.js")
    script_dir = os.path.dirname(os.path.abspath(__file__))
    apps = [
        {
            "name": "4g-proxy-orchestrator",
            "script": "orchestrator.py",
            "interpreter": "python3",
            "cwd": script_dir,
            "autorestart": True,
            "max_restarts": 10,
            "restart_delay": 5000,
            "env": {"PYTHONPATH": script_dir}
        },
        {
            "name": "4g-proxy-squid",
            "script": "./run_squid.sh",
            "interpreter": "bash",
            "cwd": script_dir,
            "autorestart": True,
            "max_restarts": 10,
            "restart_delay": 5000
        },
        {
            "name": "4g-proxy-auto-rotate",
            "script": "auto_rotate.py",
            "interpreter": "python3",
            "cwd": script_dir,
            "autorestart": True,
            "max_restarts": 10,
            "restart_delay": 5000,
            "env": {"PYTHONPATH": script_dir}
        }
    ]
    with open("ecosystem.config.js","w") as f:
        f.write("module.exports = {\n  apps: [\n")
        for app in apps:
            f.write("    {\n")
            for k,v in app.items():
                if isinstance(v,str):
                    f.write(f'      {k}: "{v}",\n')
                elif isinstance(v,bool):
                    f.write(f'      {k}: {str(v).lower()},\n')
                elif isinstance(v,int):
                    f.write(f'      {k}: {v},\n')
                elif isinstance(v,dict):
                    f.write(f'      {k}: {{\n')
                    for ek,ev in v.items():
                        f.write(f'        {ek}: "{ev}"\n')
                    f.write("      },\n")
            f.write("    },\n")
        f.write("  ]\n}\n")
    print("  ✅ PM2 ecosystem written")

def start_services():
    print("🚀 Starting services with PM2…")
    create_pm2_ecosystem()
    run_cmd("pm2 start ecosystem.config.js", check=False)
    time.sleep(2)
    run_cmd("pm2 save", check=False)
    run_cmd("pm2 startup", check=False)
    print("  ✅ PM2 up (autostart on boot)")

# ----------------- test feedback -----------------

def test_and_print(cfg):
    print("🧪 Quick tests…")
    # API
    try:
        r = requests.get("http://127.0.0.1:8088/status", timeout=5)
        if r.status_code == 200:
            data = r.json()
            print(f"  ✅ API OK – Public IP: {data.get('public_ip','Unknown')}")
        else:
            print("  ⚠️ API not responding")
    except Exception:
        print("  ⚠️ API test failed")

    # Proxy
    try:
        r = requests.get("https://api.ipify.org",
                         proxies={"http": "http://127.0.0.1:8080"},
                         timeout=10)
        if r.status_code == 200:
            print(f"  ✅ Proxy OK – IP: {r.text.strip()}")
        else:
            print("  ⚠️ Proxy test failed")
    except Exception:
        print("  ⚠️ Proxy test failed")

    lan_ip = cfg["lan_bind_ip"]
    token = cfg["api"]["token"]
    try:
        cur = requests.get("https://ipv4.icanhazip.com", timeout=10)
        current_ip = cur.text.strip() if cur.status_code == 200 else "Unknown"
    except Exception:
        current_ip = "Unknown"

    interval_m = cfg["pm2"]["ip_rotation_interval"] // 60

    print("\n" + "="*60)
    print("🎉 SETUP COMPLETE!")
    print("="*60)
    print(f"📡 HTTP Proxy: {lan_ip}:8080")
    print(f"📡 SOCKS Proxy: {lan_ip}:1080")
    print(f"🌐 Current Public IP: {current_ip}")
    print(f"🔄 IP Rotation: every {interval_m} minutes")
    print("🧪 Test:")
    print(f"  curl -x http://{lan_ip}:8080 https://api.ipify.org")
    print("🔧 PM2: pm2 status | pm2 logs | pm2 restart all")
    print("⚙️ Edit config.yaml for auth, then: pm2 restart 4g-proxy-3proxy")
    print("="*60)

# ----------------- main -----------------

if __name__ == "__main__":
    print("🚀 Raspberry Pi 5 + SIM7600E-H 4G Proxy (safe policy-routing)")
    if os.geteuid() != 0:
        print("❌ Run as root: sudo python3 main.py")
        sys.exit(1)
    try:
        install_dependencies()
        cfg = create_config()
        create_squid_config(cfg)
        if not setup_network():
            print("⚠️ Network setup failed; continuing so you can check logs")
        start_services()
        test_and_print(cfg)
    except KeyboardInterrupt:
        print("\n❌ Cancelled")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Setup failed: {e}")
        sys.exit(1)
