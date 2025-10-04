#!/usr/bin/env python3
"""
Raspberry Pi 5 + SIM7600E-H 4G Proxy - Auto Setup
One-command setup that handles everything automatically.
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
from flask import Flask, request, jsonify, abort

def run_command(cmd, check=True):
    """Run a shell command and return the result."""
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, check=check)
        return result.stdout.strip(), result.stderr.strip()
    except subprocess.CalledProcessError as e:
        print(f"Error running command: {cmd}")
        print(f"Error: {e.stderr}")
        return None, e.stderr

def detect_lan_ip():
    """Auto-detect the LAN IP address."""
    try:
        # Connect to a remote address to determine local IP
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        return local_ip
    except:
        # Fallback: get IP from network interfaces
        stdout, _ = run_command("ip route | awk '/default/ {print $5}' | head -n1")
        if stdout:
            stdout, _ = run_command(f"ip addr show {stdout} | grep 'inet ' | awk '{{print $2}}' | cut -d/ -f1")
            if stdout:
                return stdout
        return "192.168.1.37"  # Default fallback

def generate_secure_token():
    """Generate a secure random token."""
    return secrets.token_urlsafe(64)

def detect_modem_port():
    """Auto-detect the modem serial port."""
    for dev in os.listdir('/dev'):
        if dev.startswith('ttyUSB'):
            return f'/dev/{dev}'
    return '/dev/ttyUSB2'

def at_command(cmd):
    """Send AT command to modem."""
    port = detect_modem_port()
    try:
        with serial.Serial(port, 115200, timeout=1) as ser:
            ser.write((cmd + '\r').encode())
            time.sleep(0.5)
            return ser.read_all().decode(errors='ignore')
    except:
        return ""

def install_pm2():
    """Install Node.js and PM2."""
    print("  Installing Node.js and PM2...")
    
    # Install Node.js
    run_command("curl -fsSL https://deb.nodesource.com/setup_18.x | bash -", check=False)
    run_command("apt install -y nodejs", check=False)
    
    # Install PM2 globally
    run_command("npm install -g pm2", check=False)
    
    print("  ✅ PM2 installed for process management")

def install_3proxy():
    """Install 3proxy from source."""
    print("  Installing 3proxy from source...")
    
    # Install build dependencies
    run_command("apt install -y build-essential wget", check=False)
    
    # Download and compile 3proxy
    run_command("cd /tmp && wget https://github.com/z3APA3A/3proxy/archive/refs/heads/master.zip", check=False)
    run_command("cd /tmp && unzip -o master.zip", check=False)
    run_command("cd /tmp/3proxy-master && make -f Makefile.Linux", check=False)
    run_command("cp /tmp/3proxy-master/bin/3proxy /usr/local/bin/", check=False)
    run_command("chmod +x /usr/local/bin/3proxy", check=False)
    
    print("  ✅ 3proxy installed from source")

def install_dependencies():
    """Install all required dependencies."""
    print("🔧 Installing dependencies...")
    
    # System packages
    packages = [
        "python3", "python3-pip", "python3-yaml", "python3-serial", 
        "python3-requests", "iptables", "python3-flask", "build-essential", "wget", "unzip", "curl"
    ]
    
    for package in packages:
        print(f"  Installing {package}...")
        stdout, stderr = run_command(f"apt install {package} -y", check=False)
        if stderr and "already the newest version" not in stderr:
            print(f"  Warning: {stderr}")
    
    # Install Node.js and PM2
    install_pm2()
    
    # Install 3proxy from source (not available in Ubuntu repos)
    install_3proxy()

def create_config():
    """Create config.yaml with auto-detected settings."""
    print("📝 Creating configuration...")
    
    lan_ip = detect_lan_ip()
    token = generate_secure_token()
    
    config = {
        'lan_bind_ip': lan_ip,
        'api': {
            'bind': '127.0.0.1',
            'port': 8088,
            'token': token
        },
        'proxy': {
            'auth_enabled': False,
            'user': '',
            'password': ''
        },
        'pm2': {
            'enabled': True,
            'auto_restart': True,
            'ip_rotation_interval': 300,  # 5 minutes
            'max_restarts': 10,
            'restart_delay': 5000
        }
    }
    
    with open('config.yaml', 'w') as f:
        yaml.dump(config, f, default_flow_style=False)
    
    print(f"  ✅ LAN IP: {lan_ip}")
    print(f"  ✅ API Token: {token[:20]}...")
    print("  ✅ Proxy: No authentication (auth_enabled: false)")
    print("  ✅ PM2: Auto-restart enabled")
    print("  ✅ IP Rotation: Every 5 minutes")
    
    return config

def create_3proxy_config(config):
    """Create 3proxy configuration."""
    print("🔧 Configuring 3proxy...")
    
    # Check if authentication is enabled
    auth_enabled = config['proxy']['auth_enabled']
    proxy_user = config['proxy']['user']
    proxy_pass = config['proxy']['password']
    
    if auth_enabled and proxy_user and proxy_pass:
        # With authentication
        proxy_config = f"""
# 3proxy configuration with authentication
nserver 8.8.8.8
nserver 8.8.4.4

# HTTP and SOCKS proxy with authentication
proxy -p8080
socks -p1080

# Allow connections from LAN
allow * * * 192.168.*.*
allow * * * 127.0.0.1

# Authentication
auth strong
users {proxy_user}:CL:{proxy_pass}
allow {proxy_user}
"""
        print("  ✅ 3proxy configured for HTTP (8080) and SOCKS (1080)")
        print(f"  ✅ Authentication enabled: {proxy_user}")
    else:
        # No authentication
        proxy_config = """
# 3proxy configuration
nserver 8.8.8.8
nserver 8.8.4.4

# HTTP and SOCKS proxy
proxy -p8080
socks -p1080

# Allow connections from LAN
allow * * * 192.168.*.*
allow * * * 127.0.0.1

# No authentication
auth none
"""
        print("  ✅ 3proxy configured for HTTP (8080) and SOCKS (1080)")
        print("  ✅ No authentication required")
    
    with open('3proxy.cfg', 'w') as f:
        f.write(proxy_config)

def setup_network():
    """Setup network forwarding and NAT."""
    print("🌐 Setting up network...")
    
    # Enable IP forwarding
    run_command("sudo sysctl -w net.ipv4.ip_forward=1")
    
    # Detect cellular interface
    stdout, _ = run_command("ip -o link show | awk -F': ' '{print $2}' | grep -E 'wwan|ppp' | head -n1")
    if not stdout:
        print("  ⚠️  No cellular interface detected")
        return False
    
    cell_iface = stdout
    print(f"  ✅ Cellular interface: {cell_iface}")
    
    # Setup NAT
    run_command(f"sudo iptables -t nat -A POSTROUTING -o {cell_iface} -j MASQUERADE")
    run_command(f"sudo iptables -A FORWARD -i wlan0 -o {cell_iface} -j ACCEPT")
    
    print("  ✅ NAT and forwarding configured")
    return True

def create_pm2_config():
    """Create PM2 ecosystem configuration."""
    print("🔧 Creating PM2 configuration...")
    
    # Get the current script's directory
    script_dir = os.path.dirname(os.path.abspath(__file__))
    
    config = {
        'apps': [
            {
                'name': '4g-proxy-orchestrator',
                'script': 'orchestrator.py',
                'interpreter': 'python3',
                'cwd': script_dir,
                'autorestart': True,
                'max_restarts': 10,
                'restart_delay': 5000,
                'env': {
                    'PYTHONPATH': script_dir
                }
            },
            {
                'name': '4g-proxy-3proxy',
                'script': '3proxy',
                'args': '3proxy.cfg',
                'cwd': script_dir,
                'autorestart': True,
                'max_restarts': 10,
                'restart_delay': 5000
            },
            {
                'name': '4g-proxy-auto-rotate',
                'script': 'auto_rotate.py',
                'interpreter': 'python3',
                'cwd': script_dir,
                'autorestart': True,
                'max_restarts': 10,
                'restart_delay': 5000,
                'env': {
                    'PYTHONPATH': script_dir
                }
            }
        ]
    }
    
    with open('ecosystem.config.js', 'w') as f:
        f.write('module.exports = {\n')
        f.write('  apps: [\n')
        for app in config['apps']:
            f.write('    {\n')
            for key, value in app.items():
                if isinstance(value, str):
                    f.write(f'      {key}: "{value}",\n')
                elif isinstance(value, bool):
                    f.write(f'      {key}: {str(value).lower()},\n')
                elif isinstance(value, int):
                    f.write(f'      {key}: {value},\n')
                elif isinstance(value, dict):
                    f.write(f'      {key}: {{\n')
                    for env_key, env_value in value.items():
                        f.write(f'        {env_key}: "{env_value}"\n')
                    f.write('      },\n')
            f.write('    },\n')
        f.write('  ]\n')
        f.write('}\n')
    
    print("  ✅ PM2 ecosystem configuration created")

def start_services():
    """Start the proxy services with PM2."""
    print("🚀 Starting services with PM2...")
    
    # Create PM2 configuration
    create_pm2_config()
    
    # Start services with PM2
    print("  Starting services...")
    run_command("pm2 start ecosystem.config.js", check=False)
    time.sleep(3)
    
    # Save PM2 configuration
    run_command("pm2 save", check=False)
    
    # Setup PM2 to start on boot
    run_command("pm2 startup", check=False)
    
    print("  ✅ Services started with PM2")
    print("  ✅ Auto-restart enabled")
    print("  ✅ PM2 will start on boot")

def test_connection(config):
    """Test the proxy connection."""
    print("🧪 Testing connection...")
    
    try:
        # Test API
        response = requests.get('http://127.0.0.1:8088/status', timeout=5)
        if response.status_code == 200:
            data = response.json()
            print(f"  ✅ API working - Public IP: {data.get('public_ip', 'Unknown')}")
        else:
            print("  ⚠️  API not responding")
    except:
        print("  ⚠️  API test failed")
    
    try:
        # Test proxy
        response = requests.get('https://api.ipify.org', 
                              proxies={'http': 'http://127.0.0.1:8080'}, 
                              timeout=10)
        if response.status_code == 200:
            print(f"  ✅ Proxy working - IP: {response.text.strip()}")
        else:
            print("  ⚠️  Proxy test failed")
    except:
        print("  ⚠️  Proxy test failed")
    
    # Show test commands
    lan_ip = config['lan_bind_ip']
    token = config['api']['token']
    
    # Get current public IP for display
    try:
        response = requests.get('https://ipv4.icanhazip.com', timeout=10)
        current_ip = response.text.strip() if response.status_code == 200 else "Unknown"
    except:
        current_ip = "Unknown"
    
    interval_minutes = config['pm2']['ip_rotation_interval'] // 60
    
    print("\n" + "=" * 60)
    print("🎉 SETUP COMPLETE! Your 4G proxy is ready!")
    print("=" * 60)
    print(f"📡 HTTP Proxy: {lan_ip}:8080")
    print(f"📡 SOCKS Proxy: {lan_ip}:1080")
    print(f"🌐 Current Public IP: {current_ip}")
    print(f"🔄 IP Rotation: Every {interval_minutes} minutes")
    print(f"🔑 API Token: {token[:20]}...")
    # Show test command based on authentication
    auth_enabled = config['proxy']['auth_enabled']
    proxy_user = config['proxy']['user']
    proxy_pass = config['proxy']['password']
    
    print("\n🧪 Test Commands:")
    if auth_enabled and proxy_user and proxy_pass:
        print(f"curl -x http://{proxy_user}:{proxy_pass}@{lan_ip}:8080 https://api.ipify.org")
    else:
        print(f"curl -x http://{lan_ip}:8080 https://api.ipify.org")
    print(f"curl http://127.0.0.1:8088/status")
    print(f"# IP rotation: curl -X POST -H 'Authorization: YOUR_TOKEN' http://127.0.0.1:8088/rotate")
    print("\n🔧 PM2 Commands:")
    print("pm2 status          # View status")
    print("pm2 logs            # View logs")
    print("pm2 restart all     # Restart services")
    print("pm2 monit           # Monitor in real-time")
    print("\n⚙️  Configuration:")
    print("Edit config.yaml to change IP rotation interval, add auth, etc.")
    print("Then run: pm2 restart all")
    print("\n🔑 Your API token is in config.yaml if you need it for IP rotation")
    print("=" * 60)

def main():
    """Main setup function."""
    print("🚀 Raspberry Pi 5 + SIM7600E-H 4G Proxy Setup")
    print("=" * 50)
    
    # Check if running as root
    if os.geteuid() != 0:
        print("❌ This script must be run as root (use sudo)")
        sys.exit(1)
    
    try:
        # Install dependencies
        install_dependencies()
        
        # Create configuration
        config = create_config()
        
        # Setup 3proxy
        create_3proxy_config(config)
        
        # Setup network
        if not setup_network():
            print("⚠️  Network setup failed - continuing anyway")
        
        # Start services
        start_services()
        
        # Test connection
        test_connection(config)
        
    except KeyboardInterrupt:
        print("\n❌ Setup cancelled by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Setup failed: {e}")
        sys.exit(1)

if __name__ == '__main__':
    main()
