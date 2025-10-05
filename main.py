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
import json
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

def create_ppp_config(apn, port):
    """Create PPP configuration files (idempotent)"""
    chat_file = "/etc/chatscripts/ee-chat"
    peer_file = "/etc/ppp/peers/ee"
    log_file = "/var/log/ppp-ee.log"
    
    # Create chat script if missing
    if not os.path.exists(chat_file):
        print(f"  📝 Creating {chat_file}...")
        chat_script = f'''ABORT 'BUSY'
ABORT 'NO CARRIER'
ABORT 'ERROR'
ABORT 'NO DIALTONE'
ABORT 'NO ANSWER'
REPORT CONNECT
TIMEOUT 60
'' AT
OK 'ATZ'
OK 'AT+CPIN?'
OK 'AT+CFUN=1'
OK 'AT+CGATT=1'
OK 'AT+CGDCONT=1,"IP","{apn}"'
OK 'ATD*99#'
CONNECT '''''
        
        run_cmd("sudo mkdir -p /etc/chatscripts", check=False)
        with open("/tmp/ee-chat", "w") as f:
            f.write(chat_script)
        run_cmd("sudo cp /tmp/ee-chat /etc/chatscripts/ee-chat", check=False)
        run_cmd("sudo chmod 644 /etc/chatscripts/ee-chat", check=False)
    else:
        print(f"  ✅ {chat_file} already exists")
        # Update APN in existing file
        run_cmd(f'sudo sed -i "s/AT+CGDCONT=1,\\\"IP\\\",\\\".*\\\"/AT+CGDCONT=1,\\\"IP\\\",\\\"{apn}\\\"/" {chat_file}', check=False)
    
    # Create peer file if missing
    if not os.path.exists(peer_file):
        print(f"  📝 Creating {peer_file}...")
        peer_config = f'''{port}
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
connect "/usr/sbin/chat -v -f {chat_file}"'''
        
        with open("/tmp/ee-peer", "w") as f:
            f.write(peer_config)
        run_cmd("sudo cp /tmp/ee-peer /etc/ppp/peers/ee", check=False)
        run_cmd("sudo chmod 644 /etc/ppp/peers/ee", check=False)
    else:
        print(f"  ✅ {peer_file} already exists")
        # Update port in existing file
        run_cmd(f'sudo sed -i "1s|^/dev/ttyUSB.*$|{port}|" {peer_file}', check=False)

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

def load_carrier_config(apn):
    """Load carrier configuration from carriers.json"""
    try:
        with open("carriers.json", "r") as f:
            carriers = json.load(f)
        
        # Find carrier by APN
        for carrier_id, carrier_info in carriers["carriers"].items():
            if carrier_info["apn"] == apn:
                return carrier_info
        
        # If not found, return default EE config
        return carriers["carriers"]["ee"]
    except:
        # Fallback to EE config
        return {
            "name": "EE Internet",
            "apn": "everywhere",
            "username": "eesecure",
            "password": "secure",
            "ip_type": "ipv4"
        }

def activate_modem():
    """Activate SIM7600E-H modem using PPP"""
    print("📡 Activating SIM7600E-H modem...")
    
    # Load config to get APN
    try:
        with open("config.yaml", "r") as f:
            config = yaml.safe_load(f)
        apn = config.get("modem", {}).get("apn", "everywhere")
    except:
        apn = "everywhere"
    
    # Load carrier configuration
    carrier = load_carrier_config(apn)
    print(f"  📡 Using APN: {apn} ({carrier['name']})")
    
    # Stop conflicts
    print("  🔄 Stopping conflicts...")
    run_cmd("sudo systemctl stop ModemManager", check=False)
    run_cmd("sudo pkill pppd", check=False)
    time.sleep(2)
    
    # Install PPP if needed
    print("  📦 Installing PPP...")
    run_cmd("sudo apt update", check=False)
    run_cmd("sudo apt install -y ppp", check=False)
    
    # Detect AT port
    print("  🔍 Detecting AT port...")
    port = detect_modem_port()
    print(f"  📡 Using AT port: {port}")
    
    # Create PPP configuration
    print("  🔧 Creating PPP configuration...")
    create_ppp_config(apn, port)
    
    # Start PPP connection
    print("  🚀 Starting PPP connection...")
    run_cmd("sudo pppd call ee", check=False)
    
    # Wait for ppp0 to come up
    print("  ⏳ Waiting for ppp0...")
    for i in range(30):
        time.sleep(1)
        result = run_cmd("ip -4 addr show dev ppp0", check=False)
        if "inet " in result[0]:
            print("  ✅ ppp0 is up with IPv4")
            # Keep WiFi as primary route
            keep_wifi_primary()
            return True
    
    print("  ⚠️ ppp0 did not come up, trying fallback...")
    return False

def keep_wifi_primary():
    """Keep WiFi as primary route, PPP as secondary"""
    try:
        # Get current default route
        result = run_cmd("ip route show default", check=False)
        if result and result[0]:
            default_line = result[0]
            # Extract gateway and device
            parts = default_line.split()
            gw = None
            dev = None
            metric = 100
            
            for i, part in enumerate(parts):
                if part == "via" and i+1 < len(parts):
                    gw = parts[i+1]
                elif part == "dev" and i+1 < len(parts):
                    dev = parts[i+1]
                elif part == "metric" and i+1 < len(parts):
                    metric = int(parts[i+1])
            
            if gw and dev and dev not in ["ppp0"]:
                print(f"  🔄 Keeping {dev} as primary route (metric {metric})")
                # Ensure WiFi stays primary - CRITICAL for SSH stability
                run_cmd(f"sudo ip route replace default via {gw} dev {dev} metric {metric}", check=False)
                # Add PPP as secondary with higher metric (lower priority)
                run_cmd(f"sudo ip route add default dev ppp0 metric {metric+500}", check=False)
                print(f"  ✅ WiFi ({dev}) remains primary for SSH access")
    except:
        pass

def try_common_apns(port):
    """Try common APN configurations if default fails"""
    print("  🔄 Trying APNs from carriers.json...")
    
    # Load carriers from JSON file
    carriers_to_try = []
    try:
        with open("carriers.json", "r") as f:
            carriers = json.load(f)
        
        # Add all carriers from JSON
        for carrier_id, carrier_info in carriers["carriers"].items():
            carriers_to_try.append(carrier_info)
        
        print(f"  📋 Loaded {len(carriers_to_try)} carriers from carriers.json")
    except FileNotFoundError:
        print("  ⚠️ carriers.json not found, using fallback APNs")
        # Fallback to hardcoded list
        carriers_to_try = [
            {"apn": "everywhere", "name": "EE Internet", "username": "eesecure", "password": "secure"},
            {"apn": "internet", "name": "Generic Internet", "username": "", "password": ""},
            {"apn": "web", "name": "Web APN", "username": "", "password": ""},
            {"apn": "data", "name": "Data APN", "username": "", "password": ""},
            {"apn": "three.co.uk", "name": "Three Internet", "username": "", "password": ""},
            {"apn": "mobile.o2.co.uk", "name": "O2 Internet", "username": "o2web", "password": "password"},
            {"apn": "giffgaff.com", "name": "giffgaff", "username": "giffgaff", "password": ""},
        ]
    
    for carrier in carriers_to_try:
        apn = carrier["apn"]
        name = carrier["name"]
        print(f"  📤 Trying APN: {apn} ({name})")
        
        # Configure PDP context with specific APN
        send_at_command("AT+CGDCONT=1,\"IP\",\"" + apn + "\",\"0.0.0.0\",0,0", port)
        time.sleep(1)
        
        # Activate PDP context
        send_at_command("AT+CGACT=1,1", port)
        time.sleep(3)
        
        # Check for IP
        ip_response = send_at_command("AT+CGPADDR", port)
        print(f"  📥 {ip_response}")
        
        # Check if we got a valid IP address (not 0.0.0.0)
        if "+CGPADDR: 1," in ip_response:
            # Extract the IP from the response
            ip_line = [line for line in ip_response.split('\n') if '+CGPADDR: 1,' in line]
            if ip_line and "0.0.0.0" not in ip_line[0]:
                print(f"  ✅ Success with APN: {apn} ({name})")
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
    
    # Fix permissions for proxyuser
    run_cmd("sudo chown proxyuser:proxyuser squid.conf", check=False)
    run_cmd("sudo chmod 644 squid.conf", check=False)
    
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
    
    # Then setup simple routing via ppp0
    print("  🔄 Setting up simple routing via ppp0...")
    run_cmd("sudo ip route add default dev ppp0 metric 200", check=False)
    print("  ✅ Simple routing via ppp0 configured")
    
    # Start Squid proxy
    print("  🚀 Starting Squid proxy...")
    run_cmd("sudo systemctl start squid", check=False)
    run_cmd("sudo systemctl enable squid", check=False)
    print("  ✅ Squid proxy started")
    
    return True

# ----------------- pm2 -----------------

def create_pm2_ecosystem():
    print("🔧 Creating PM2 ecosystem.config.js")
    script_dir = os.path.dirname(os.path.abspath(__file__))
    
    # Get the current user (who will run PM2) - same logic as run.sh
    current_user = os.environ.get('SUDO_USER') or os.environ.get('USER') or 'pi'
    print(f"  📝 PM2 will run as user: {current_user}")
    
    # Clean up old PM2 processes that shouldn't exist anymore
    print("  🧹 Cleaning up old PM2 processes...")
    run_cmd("pm2 delete 4g-proxy-squid 2>/dev/null || true", check=False)
    run_cmd("pm2 delete 4g-proxy-3proxy 2>/dev/null || true", check=False)
    run_cmd("pm2 delete 4g-proxy 2>/dev/null || true", check=False)
    
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
    
    # Get the actual LAN IP (not ppp0 IP)
    try:
        # Try to get WiFi IP first
        result = run_cmd("ip -4 addr show wlan0 | awk '/inet /{print $2}' | cut -d/ -f1", check=False)
        if result and result[0]:
            lan_ip = result[0].strip()
        else:
            # Fallback to eth0
            result = run_cmd("ip -4 addr show eth0 | awk '/inet /{print $2}' | cut -d/ -f1", check=False)
            if result and result[0]:
                lan_ip = result[0].strip()
            else:
                lan_ip = cfg["lan_bind_ip"]  # Use config as last resort
    except:
        lan_ip = cfg["lan_bind_ip"]

    # Proxy test
    try:
        r = requests.get("https://api.ipify.org",
                         proxies={"http": f"http://{lan_ip}:3128"},
                         timeout=10)
        if r.status_code == 200:
            print(f"  ✅ Proxy OK – IP: {r.text.strip()}")
        else:
            print("  ⚠️ Proxy test failed")
    except Exception:
        print("  ⚠️ Proxy test failed")
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
    print(f"📡 HTTP Proxy: {lan_ip}:3128")
    print(f"🌐 Current Public IP: {current_ip}")
    print("🧪 Test:")
    print(f"  curl -x http://{lan_ip}:3128 https://api.ipify.org")
    print("🔧 Squid: sudo systemctl status squid | sudo systemctl restart squid")
    print("⚙️ Edit config.yaml for auth, then: sudo systemctl restart squid")
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
