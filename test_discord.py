#!/usr/bin/env python3
"""
Test Discord notifications for 4G proxy.
Usage: python3 test_discord.py
"""

import requests
import yaml

def load_config():
    with open('config.yaml', 'r') as f:
        return yaml.safe_load(f)

def test_discord_notification():
    """Test Discord notification via API."""
    config = load_config()
    api_token = config.get('api', {}).get('token', '')
    
    if not api_token:
        print("❌ No API token found in config.yaml")
        return False
    
    # Test the notify endpoint
    url = "http://127.0.0.1:8088/notify"
    headers = {"Authorization": f"Bearer {api_token}"}
    
    try:
        response = requests.post(url, headers=headers, timeout=10)
        if response.status_code == 200:
            data = response.json()
            print(f"✅ Discord notification sent successfully!")
            print(f"📱 Current IP: {data.get('ip', 'Unknown')}")
            return True
        else:
            print(f"❌ Failed to send notification: {response.status_code}")
            print(f"Response: {response.text}")
            return False
    except requests.exceptions.ConnectionError:
        print("❌ Could not connect to orchestrator API")
        print("Make sure the orchestrator is running: pm2 status")
        return False
    except Exception as e:
        print(f"❌ Error: {e}")
        return False

if __name__ == '__main__':
    print("🧪 Testing Discord notification...")
    print("=" * 40)
    
    # Check if Discord is configured
    config = load_config()
    webhook_url = config.get('discord', {}).get('webhook_url', '').strip()
    
    if not webhook_url or webhook_url == "https://discord.com/api/webhooks/YOUR_WEBHOOK_ID/YOUR_TOKEN":
        print("⚠️  Discord webhook not configured in config.yaml")
        print("Add your webhook URL under the 'discord' section")
        print("Example:")
        print("discord:")
        print("  webhook_url: \"https://discord.com/api/webhooks/123456789/abcdefghijk\"")
        exit(1)
    
    print(f"📱 Discord webhook configured")
    print(f"🔄 Sending test notification...")
    
    success = test_discord_notification()
    
    if success:
        print("\n✅ Test completed successfully!")
        print("Check your Discord channel for the notification.")
    else:
        print("\n❌ Test failed!")
        print("Check the orchestrator logs: pm2 logs 4g-proxy-orchestrator")
