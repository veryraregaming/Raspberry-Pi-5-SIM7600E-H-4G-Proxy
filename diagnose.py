#!/usr/bin/env python3
"""
Diagnostic script for 4G proxy troubleshooting
"""

import os
import yaml
import requests
import subprocess
from pathlib import Path

def check_config():
    """Check if config.yaml exists and is valid"""
    print("🔍 Checking configuration...")
    config_file = Path("config.yaml")
    if not config_file.exists():
        print("❌ config.yaml not found")
        return False
    
    try:
        with open(config_file, 'r') as f:
            config = yaml.safe_load(f)
        print(f"✅ config.yaml found")
        print(f"   LAN IP: {config.get('lan_bind_ip', 'Not set')}")
        print(f"   API Port: {config.get('api', {}).get('port', 'Not set')}")
        print(f"   API Token: {config.get('api', {}).get('token', 'Not set')[:20]}...")
        return config
    except Exception as e:
        print(f"❌ Error reading config.yaml: {e}")
        return False

def check_services():
    """Check if services are running"""
    print("\n🔍 Checking services...")
    
    # Check PM2
    try:
        result = subprocess.run(['pm2', 'status'], capture_output=True, text=True)
        if result.returncode == 0:
            print("✅ PM2 is running")
            if '4g-proxy-orchestrator' in result.stdout:
                print("✅ Orchestrator service found in PM2")
            else:
                print("❌ Orchestrator service not found in PM2")
            
            if '4g-proxy-web' in result.stdout:
                print("✅ Web interface service found in PM2")
            else:
                print("❌ Web interface service not found in PM2")
        else:
            print("❌ PM2 not running")
    except:
        print("❌ PM2 command failed")
    
    # Check Squid
    try:
        result = subprocess.run(['sudo', 'systemctl', 'is-active', 'squid'], capture_output=True, text=True)
        if result.stdout.strip() == 'active':
            print("✅ Squid proxy is running")
        else:
            print(f"❌ Squid proxy status: {result.stdout.strip()}")
    except:
        print("❌ Cannot check Squid status")

def check_api(config):
    """Check if API is responding"""
    print("\n🔍 Checking API...")
    
    if not config:
        print("❌ No config available")
        return
    
    api_port = config.get('api', {}).get('port', 8088)
    token = config.get('api', {}).get('token', '')
    
    try:
        # Test API connection
        url = f"http://127.0.0.1:{api_port}/status"
        headers = {"Authorization": f"Bearer {token}"}
        
        print(f"Testing API at: {url}")
        response = requests.get(url, headers=headers, timeout=5)
        
        if response.status_code == 200:
            data = response.json()
            print(f"✅ API responding")
            print(f"   Current IP: {data.get('public_ip', 'Unknown')}")
            print(f"   PDP: {data.get('pdp', 'Unknown')}")
        else:
            print(f"❌ API error: {response.status_code} - {response.text}")
    except Exception as e:
        print(f"❌ API connection failed: {e}")

def check_ip_history():
    """Check IP history file"""
    print("\n🔍 Checking IP history...")
    
    history_file = Path("state/ip_history.json")
    if history_file.exists():
        try:
            import json
            with open(history_file, 'r') as f:
                history = json.load(f)
            print(f"✅ IP history file found")
            print(f"   Total rotations: {history.get('rotations', 0)}")
            print(f"   IPs recorded: {len(history.get('ips', []))}")
            if history.get('first_seen'):
                print(f"   First seen: {history['first_seen']}")
            
            # Show recent IPs
            ips = history.get('ips', [])
            if ips:
                print("   Recent IPs:")
                for ip in ips[-3:]:  # Last 3 IPs
                    print(f"     - {ip.get('ip', 'Unknown')} at {ip.get('date', 'Unknown')} {ip.get('time', 'Unknown')}")
        except Exception as e:
            print(f"❌ Error reading IP history: {e}")
    else:
        print("❌ IP history file not found")

def check_network():
    """Check network interfaces"""
    print("\n🔍 Checking network interfaces...")
    
    try:
        # Check ppp0
        result = subprocess.run(['ip', '-4', 'addr', 'show', 'ppp0'], capture_output=True, text=True)
        if result.returncode == 0:
            print("✅ ppp0 interface exists")
            if 'inet ' in result.stdout:
                print("✅ ppp0 has IPv4 address")
                # Extract IP
                for line in result.stdout.split('\n'):
                    if 'inet ' in line:
                        ip = line.split()[1].split('/')[0]
                        print(f"   ppp0 IP: {ip}")
            else:
                print("❌ ppp0 has no IPv4 address")
        else:
            print("❌ ppp0 interface not found")
        
        # Check wwan0
        result = subprocess.run(['ip', '-4', 'addr', 'show', 'wwan0'], capture_output=True, text=True)
        if result.returncode == 0:
            print("✅ wwan0 interface exists")
            if 'inet ' in result.stdout:
                print("✅ wwan0 has IPv4 address")
            else:
                print("❌ wwan0 has no IPv4 address")
        else:
            print("❌ wwan0 interface not found")
            
    except Exception as e:
        print(f"❌ Network check failed: {e}")

def check_current_ip():
    """Check current public IP"""
    print("\n🔍 Checking current public IP...")
    
    try:
        result = subprocess.run(['curl', '-s', '--max-time', '10', 'https://ipv4.icanhazip.com'], 
                              capture_output=True, text=True)
        if result.returncode == 0:
            ip = result.stdout.strip()
            print(f"✅ Current public IP: {ip}")
        else:
            print("❌ Cannot get public IP")
    except Exception as e:
        print(f"❌ IP check failed: {e}")

def main():
    print("🔧 4G Proxy Diagnostic Tool")
    print("=" * 50)
    
    config = check_config()
    check_services()
    check_api(config)
    check_ip_history()
    check_network()
    check_current_ip()
    
    print("\n" + "=" * 50)
    print("💡 Troubleshooting tips:")
    print("1. If API is not responding, restart orchestrator: pm2 restart 4g-proxy-orchestrator")
    print("2. If ppp0 has no IP, restart PPP: sudo pkill pppd && sudo pppd call ee")
    print("3. Check logs: pm2 logs 4g-proxy-orchestrator")
    print("4. Check Squid: sudo systemctl status squid")

if __name__ == '__main__':
    main()
