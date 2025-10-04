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
    # Try common ports in order of likelihood
    common_ports = ['/dev/ttyUSB2', '/dev/ttyUSB1', '/dev/ttyUSB0', '/dev/ttyUSB3', '/dev/ttyUSB4']
    
    for port in common_ports:
        if os.path.exists(port):
            # Test if modem responds on this port
            try:
                with serial.Serial(port, 115200, timeout=1) as ser:
                    ser.write(b'AT\r\n')
                    time.sleep(0.5)
                    response = ser.read_all().decode(errors='ignore')
                    if "OK" in response:
                        print(f"  ✅ Modem responding on {port}")
                        return port
            except:
                continue
    
    # Fallback to any available ttyUSB
    for dev in os.listdir('/dev'):
        if dev.startswith('ttyUSB'):
            return f'/dev/{dev}'
    
    return '/dev/ttyUSB2'  # Default fallback

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
    """Activate SIM7600E-H modem using ModemManager"""
    print("📡 Activating SIM7600E-H modem...")
    
    # Load config to get APN
    try:
        with open("config.yaml", "r") as f:
            config = yaml.safe_load(f)
        apn = config.get("modem", {}).get("apn", "everywhere")
    except:
        apn = "everywhere"
    
    print(f"  📡 Using APN: {apn}")
    
    # Reset modem first to clear any stuck state
    print("  🔄 Resetting modem...")
    run_cmd("sudo mmcli -m 0 --reset", check=False)
    time.sleep(5)
    
    # Try ModemManager first (more reliable)
    print("  🔄 Trying ModemManager connection...")
    result = run_cmd(f"sudo mmcli -m 0 --simple-connect=\"apn={apn}\"", check=False)
    
    if "successfully connected" in result[0].lower():
        print("  ✅ ModemManager connected successfully")
        # Wait a moment for interface to come up
        time.sleep(3)
        
        # Check bearer details to get the IP configuration
        bearer_result = run_cmd("sudo mmcli -b 1", check=False)
        bearer_text = "\n".join(bearer_result)
        
        if "IPv4 configuration" in bearer_text:
            # Extract IP from bearer info
            ip_addr = None
            gateway = None
            prefix = None
            
            for line in bearer_result:
                if "address:" in line and "IPv4" in bearer_text.split("address:")[0].split("\n")[-2]:
                    ip_addr = line.split("address:")[1].strip()
                    print(f"  📡 Bearer IP: {ip_addr}")
                elif "gateway:" in line and "IPv4" in bearer_text.split("gateway:")[0].split("\n")[-2]:
                    gateway = line.split("gateway:")[1].strip()
                    print(f"  📡 Bearer Gateway: {gateway}")
                elif "prefix:" in line and "IPv4" in bearer_text.split("prefix:")[0].split("\n")[-2]:
                    prefix = line.split("prefix:")[1].strip()
                    print(f"  📡 Bearer Prefix: {prefix}")
            
            if ip_addr and gateway:
                # Configure wwan0 with the bearer IP
                if prefix:
                    ip_with_prefix = f"{ip_addr}/{prefix}"
                else:
                    ip_with_prefix = f"{ip_addr}/30"  # Default /30 for cellular
                
                print(f"  📡 Configuring wwan0 with {ip_with_prefix}")
                run_cmd(f"sudo ip addr add {ip_with_prefix} dev wwan0", check=False)
                
                # Add routes to proxy table (not main table to avoid hijacking)
                print(f"  📡 Adding routes to proxy table")
                run_cmd(f"sudo ip route add default via {gateway} dev wwan0 table 100", check=False)
                
                # Add local network route
                if "/" in ip_with_prefix:
                    ip_base = ip_with_prefix.split("/")[0]
                    network = ".".join(ip_base.split(".")[:-1]) + ".0"
                    run_cmd(f"sudo ip route add {network}/24 dev wwan0 table 100", check=False)
        
        # Check if wwan0 got an IPv4 address
        ipv4_check = run_cmd("ip -4 addr show wwan0", check=False)
        if "inet " in ipv4_check[0]:
            print("  ✅ wwan0 has IPv4 address")
            return True
        else:
            print("  ⚠️ wwan0 still no IPv4, trying dhclient...")
            # Try to get IPv4 address with dhclient
            dhclient_result = run_cmd("sudo dhclient -4 wwan0", check=False)
            time.sleep(3)
            
            # Check again
            ipv4_check = run_cmd("ip -4 addr show wwan0", check=False)
            if "inet " in ipv4_check[0]:
                print("  ✅ wwan0 now has IPv4 address")
                return True
            else:
                print("  ⚠️ Still no IPv4, but interface is up")
                return True  # Continue anyway, might work with IPv6
    
    print("  ⚠️ ModemManager failed, trying direct AT commands...")
    
    # Fallback to direct AT commands
    port = detect_modem_port()
    
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
        print("  ⚠️ Configured APN failed, trying all APNs automatically...")
        # Always try all APNs to find the right one
        return try_common_apns(port)

def try_common_apns(port):
    """Try common APN configurations if default fails"""
    print("  🔄 Trying APNs from apn.txt...")
    
    # Load APNs from apn.txt file
    apns = []
    try:
        with open("apn.txt", "r") as f:
            for line in f:
                line = line.strip()
                # Skip empty lines and comments
                if line and not line.startswith("#"):
                    apns.append(line)
        print(f"  📋 Loaded {len(apns)} APNs from apn.txt")
    except FileNotFoundError:
        print("  ⚠️ apn.txt not found, using default APNs")
        # Fallback to hardcoded list
        apns = [
            "everywhere",   # EE (UK) - most common
            "internet",     # Generic
            "web",          # Some carriers
            "data",         # Some carriers  
            "broadband",    # Some carriers
            "mobile",       # Some carriers
            "3gnet",        # Some carriers
            "fast.t-mobile.com",  # T-Mobile
            "broadband",    # Verizon
            "internet",     # AT&T
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
            "apn": "everywhere",  # Default APN for EE (UK), can be overridden
            "port": "/dev/ttyUSB2",  # Default port, auto-detected
            "timeout": 2
        },
        "pm2": {"enabled": True, "auto_restart": True, "ip_rotation_interval": 300, "max_restarts": 10, "restart_delay": 5000}
    }
    with open("config.yaml","w") as f:
        yaml.dump(cfg, f, default_flow_style=False)
    print(f"  ✅ LAN IP: {lan_ip}")
    print(f"  ✅ API Token: {token[:20]}…")
    print(f"  ✅ Default APN: everywhere (EE UK - edit config.yaml to customize)")
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
        apn = config.get("modem", {}).get("apn", "everywhere")
        print(f"  📡 Using APN: {apn}")
    except:
        apn = "everywhere"
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
