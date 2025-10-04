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

def install_3proxy():
    """Install 3proxy from source."""
    print("  Installing 3proxy from source...")
    
    # Install build dependencies
    run_command("apt install -y build-essential wget", check=False)
    
    # Download and compile 3proxy
    run_command("cd /tmp && wget https://github.com/z3APA3A/3proxy/archive/refs/heads/master.zip", check=False)
    run_command("cd /tmp && unzip master.zip", check=False)
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
        "python3-requests", "iptables", "python3-flask", "build-essential", "wget", "unzip"
    ]
    
    for package in packages:
        print(f"  Installing {package}...")
        stdout, stderr = run_command(f"apt install {package} -y", check=False)
        if stderr and "already the newest version" not in stderr:
            print(f"  Warning: {stderr}")
    
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
            'user': '',
            'password': ''
        }
    }
    
    with open('config.yaml', 'w') as f:
        yaml.dump(config, f, default_flow_style=False)
    
    print(f"  ✅ LAN IP: {lan_ip}")
    print(f"  ✅ API Token: {token[:20]}...")
    print("  ✅ Proxy: No authentication")
    
    return config

def create_3proxy_config():
    """Create 3proxy configuration."""
    print("🔧 Configuring 3proxy...")
    
    config = """
# 3proxy configuration
nserver 8.8.8.8
nserver 8.8.4.4

# HTTP and SOCKS proxy
proxy -p8080
socks -p1080

# Allow connections from LAN
allow * * * 192.168.*.*
allow * * * 127.0.0.1

# No authentication by default
auth none
"""
    
    with open('3proxy.cfg', 'w') as f:
        f.write(config)
    
    print("  ✅ 3proxy configured for HTTP (8080) and SOCKS (1080)")
    print("  ✅ No authentication required")

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

def start_services():
    """Start the proxy services."""
    print("🚀 Starting services...")
    
    # Start 3proxy in background
    print("  Starting 3proxy...")
    run_command("3proxy 3proxy.cfg &", check=False)
    time.sleep(2)
    
    # Start the orchestrator
    print("  Starting orchestrator...")
    run_command("python3 orchestrator.py &", check=False)
    time.sleep(2)
    
    print("  ✅ Services started")

def test_connection():
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
        create_3proxy_config()
        
        # Setup network
        if not setup_network():
            print("⚠️  Network setup failed - continuing anyway")
        
        # Start services
        start_services()
        
        # Test connection
        test_connection()
        
        print("\n" + "=" * 50)
        print("🎉 Setup complete!")
        print(f"📡 Proxy available at: {config['lan_bind_ip']}:8080 (HTTP)")
        print(f"📡 SOCKS proxy at: {config['lan_bind_ip']}:1080")
        print(f"🔑 API token: {config['api']['token'][:20]}...")
        print("📊 API status: http://127.0.0.1:8088/status")
        print("🔄 IP rotation: curl -X POST -H 'Authorization: YOUR_TOKEN' http://127.0.0.1:8088/rotate")
        print("=" * 50)
        
    except KeyboardInterrupt:
        print("\n❌ Setup cancelled by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Setup failed: {e}")
        sys.exit(1)

if __name__ == '__main__':
    main()
