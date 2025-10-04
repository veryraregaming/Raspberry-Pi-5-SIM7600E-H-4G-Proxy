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

def load_config():
    """Load configuration from config.yaml"""
    try:
        with open('config.yaml', 'r') as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        print("❌ config.yaml not found")
        sys.exit(1)

def rotate_ip(config):
    """Rotate the IP address using the API"""
    try:
        token = config['api']['token']
        url = f"http://{config['api']['bind']}:{config['api']['port']}/rotate"
        
        headers = {'Authorization': token}
        response = requests.post(url, headers=headers, timeout=30)
        
        if response.status_code == 200:
            data = response.json()
            print(f"✅ IP rotated successfully: {data.get('public_ip', 'Unknown')}")
            return True
        else:
            print(f"❌ IP rotation failed: {response.status_code}")
            return False
            
    except requests.RequestException as e:
        print(f"❌ Connection error: {e}")
        return False

def main():
    """Main rotation loop"""
    print("🔄 Starting auto IP rotation...")
    
    config = load_config()
    
    if not config.get('pm2', {}).get('enabled', False):
        print("❌ PM2 auto-rotation not enabled in config")
        sys.exit(1)
    
    interval = config['pm2']['ip_rotation_interval']
    print(f"⏰ Rotation interval: {interval} seconds")
    
    while True:
        try:
            print(f"\n🔄 Rotating IP at {time.strftime('%Y-%m-%d %H:%M:%S')}")
            
            if rotate_ip(config):
                print("✅ IP rotation successful")
            else:
                print("❌ IP rotation failed - will retry next cycle")
            
            print(f"⏰ Waiting {interval} seconds until next rotation...")
            time.sleep(interval)
            
        except KeyboardInterrupt:
            print("\n🛑 Auto rotation stopped by user")
            break
        except Exception as e:
            print(f"❌ Error in rotation loop: {e}")
            time.sleep(60)  # Wait 1 minute before retrying

if __name__ == '__main__':
    main()
