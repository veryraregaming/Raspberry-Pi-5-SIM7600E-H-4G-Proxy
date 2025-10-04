#!/usr/bin/env python3
"""
Auto IP rotation script for 4G proxy
Runs every X minutes to rotate IP address
"""

import time
import requests
import yaml
import sys
import os
from datetime import datetime

def load_config():
    """Load configuration from config.yaml"""
    try:
        with open('config.yaml', 'r') as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        print("❌ config.yaml not found")
        sys.exit(1)

def get_current_ip():
    """Get current public IP"""
    try:
        response = requests.get('https://ipv4.icanhazip.com', timeout=10)
        if response.status_code == 200:
            return response.text.strip()
    except:
        pass
    return "Unknown"

def rotate_ip(config):
    """Rotate the IP address using the API"""
    try:
        token = config['api']['token']
        url = f"http://{config['api']['bind']}:{config['api']['port']}/rotate"
        
        headers = {'Authorization': token}
        response = requests.post(url, headers=headers, timeout=30)
        
        if response.status_code == 200:
            data = response.json()
            new_ip = data.get('public_ip', 'Unknown')
            print(f"✅ IP rotated successfully: {new_ip}")
            return True, new_ip
        else:
            print(f"❌ IP rotation failed: {response.status_code}")
            return False, None
            
    except requests.RequestException as e:
        print(f"❌ Connection error: {e}")
        return False, None

def main():
    """Main rotation loop"""
    print("🔄 Starting auto IP rotation...")
    
    config = load_config()
    
    if not config.get('pm2', {}).get('enabled', False):
        print("❌ PM2 auto-rotation not enabled in config")
        sys.exit(1)
    
    interval = config['pm2']['ip_rotation_interval']
    interval_minutes = interval // 60
    
    print(f"⏰ Rotation interval: {interval} seconds ({interval_minutes} minutes)")
    
    # Get initial IP
    current_ip = get_current_ip()
    print(f"🌐 Current IP: {current_ip}")
    
    rotation_count = 0
    
    while True:
        try:
            rotation_count += 1
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            
            print(f"\n🔄 Rotation #{rotation_count} at {timestamp}")
            print(f"🌐 Current IP: {current_ip}")
            
            success, new_ip = rotate_ip(config)
            
            if success and new_ip:
                if new_ip != current_ip:
                    print(f"🎉 IP changed: {current_ip} → {new_ip}")
                    current_ip = new_ip
                else:
                    print(f"ℹ️  IP unchanged: {current_ip}")
            else:
                print("❌ IP rotation failed - will retry next cycle")
            
            print(f"⏰ Next rotation in {interval_minutes} minutes...")
            time.sleep(interval)
            
        except KeyboardInterrupt:
            print("\n🛑 Auto rotation stopped by user")
            break
        except Exception as e:
            print(f"❌ Error in rotation loop: {e}")
            time.sleep(60)  # Wait 1 minute before retrying

if __name__ == '__main__':
    main()
