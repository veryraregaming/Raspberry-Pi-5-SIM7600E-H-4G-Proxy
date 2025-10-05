#!/usr/bin/env python3
import os, time, requests, serial, yaml, json
from flask import Flask, request, jsonify, abort
from datetime import datetime
from pathlib import Path

app = Flask(__name__)

# State file for Discord message ID
STATE_DIR = Path(__file__).parent / "state"
STATE_DIR.mkdir(exist_ok=True)
MSG_ID_PATH = STATE_DIR / "discord_message_id.txt"

def load_config():
    with open('config.yaml', 'r') as f:
        return yaml.safe_load(f)

def load_text(file_path):
    """Load text from file, return None if file doesn't exist or is empty."""
    try:
        if file_path.exists():
            with open(file_path, 'r') as f:
                content = f.read().strip()
                return content if content else None
    except:
        pass
    return None

def save_text(file_path, content):
    """Save text to file."""
    try:
        with open(file_path, 'w') as f:
            f.write(str(content).strip())
    except Exception as e:
        print(f"Warning: Could not save to {file_path}: {e}")

def get_current_ip():
    """Get current public IP address."""
    try:
        response = requests.get('https://ipv4.icanhazip.com', timeout=10)
        return response.text.strip()
    except:
        try:
            response = requests.get('https://api.ipify.org', timeout=10)
            return response.text.strip()
        except:
            return "Unknown"

def build_discord_embed(current_ip, previous_ip=None, is_rotation=False):
    """Build Discord embed for IP notification."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    if is_rotation and previous_ip:
        title = "🔄 4G Proxy IP Updated"
        description = f"**IP Rotation Complete**\n\n**Previous IP:** {previous_ip}\n**New IP:** {current_ip}"
        color = 0x00ff00  # Green for successful rotation
        footer = f"4G Mobile Proxy Server • {datetime.now().strftime('%d/%m/%Y %H:%M')}"
    elif is_rotation:
        title = "🚀 4G Proxy Initialized"
        description = f"**Proxy Started**\n\n**Current IP:** {current_ip}\n**Status:** Ready for connections"
        color = 0x0099ff  # Blue for initialization
        footer = f"4G Mobile Proxy Server • {datetime.now().strftime('%d/%m/%Y %H:%M')}"
    else:
        title = "📊 4G Proxy Status Update"
        description = f"**Current IP:** {current_ip}\n**Status:** Monitoring active"
        color = 0xff9900  # Orange for status update
        footer = f"4G Mobile Proxy Server • {datetime.now().strftime('%d/%m/%Y %H:%M')}"
    
    embed = {
        "title": title,
        "description": description,
        "color": color,
        "footer": {"text": footer},
        "timestamp": datetime.now().isoformat()
    }
    
    return {
        "content": None,
        "embeds": [embed],
        "allowed_mentions": {"parse": []}
    }

def post_or_patch_discord(webhook_url, payload, msg_id_file):
    """POST new message or PATCH existing one."""
    message_id = load_text(msg_id_file)
    
    if message_id:
        # Try to PATCH existing message
        url = f"{webhook_url.split('?')[0]}/messages/{message_id}"
        try:
            r = requests.patch(url, json=payload, timeout=20)
            r.raise_for_status()
            return ("patched", message_id)
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 404:
                # Message was deleted, create a new one
                print(f"Old Discord message not found (deleted?), creating new one...")
                message_id = None  # Fall through to POST
            else:
                raise
    
    if not message_id:
        # POST new message with ?wait=true
        url = f"{webhook_url}?wait=true" if "?wait" not in webhook_url else webhook_url
        r = requests.post(url, json=payload, timeout=20)
        r.raise_for_status()
        data = r.json()
        new_id = str(data.get("id", "")).strip()
        if new_id:
            save_text(msg_id_file, new_id)
        return ("posted", new_id)

def send_discord_notification(current_ip, previous_ip=None, is_rotation=False):
    """Send Discord notification about IP change."""
    config = load_config()
    webhook_url = config.get('discord', {}).get('webhook_url', '').strip()
    
    if not webhook_url or webhook_url == "https://discord.com/api/webhooks/YOUR_WEBHOOK_ID/YOUR_TOKEN":
        return False
    
    try:
        payload = build_discord_embed(current_ip, previous_ip, is_rotation)
        action, msg_id = post_or_patch_discord(webhook_url, payload, MSG_ID_PATH)
        print(f"Discord notification {action} (ID: {msg_id})")
        return True
    except Exception as e:
        print(f"Failed to send Discord notification: {e}")
        return False

def detect_modem_port():
    for dev in os.listdir('/dev'):
        if dev.startswith('ttyUSB'):
            return f'/dev/{dev}'
    return '/dev/ttyUSB2'

def at(cmd):
    port = detect_modem_port()
    with serial.Serial(port, 115200, timeout=1) as ser:
        ser.write((cmd + '\r').encode())
        time.sleep(0.5)
        return ser.read_all().decode(errors='ignore')

# Store previous IP for change detection
previous_ip = None

@app.get('/status')
def status():
    pdp = at('AT+CGPADDR')
    pub = get_current_ip()
    return jsonify({'pdp': pdp, 'public_ip': pub})

@app.post('/rotate')
def rotate():
    global previous_ip
    config = load_config()
    expected = config['api']['token']
    token = request.headers.get('Authorization', '')
    if expected not in token:
        abort(403)
    
    # Store current IP before rotation
    current_ip = get_current_ip()
    previous_ip = current_ip
    
    # Perform rotation
    at('AT+CGACT=0,1'); time.sleep(2)
    at('AT+CGACT=1,1'); time.sleep(4)
    
    # Get new IP
    pdp = at('AT+CGPADDR')
    new_ip = get_current_ip()
    
    # Send Discord notification for IP rotation
    send_discord_notification(new_ip, previous_ip, is_rotation=True)
    
    return jsonify({'pdp': pdp, 'public_ip': new_ip, 'previous_ip': previous_ip})

@app.post('/notify')
def notify():
    """Send Discord notification with current status."""
    config = load_config()
    expected = config['api']['token']
    token = request.headers.get('Authorization', '')
    if expected not in token:
        abort(403)
    
    current_ip = get_current_ip()
    send_discord_notification(current_ip, is_rotation=False)
    return jsonify({'status': 'notification_sent', 'ip': current_ip})

if __name__ == '__main__':
    # NOTE: networking is handled in main.py; do NOT try to change default route here.
    config = load_config()

    lan_ip = config['lan_bind_ip']
    auth_enabled = config['proxy']['auth_enabled']
    proxy_user = config['proxy']['user']
    proxy_pass = config['proxy']['password']

    print("\n" + "="*60)
    print("🚀 4G Proxy Orchestrator Started")
    print("="*60)
    print(f"📡 HTTP Proxy: {lan_ip}:3128")
    if auth_enabled and proxy_user and proxy_pass:
        print(f"🔐 Authentication: {proxy_user}:{proxy_pass}")
        print(f"🧪 curl -x http://{proxy_user}:{proxy_pass}@{lan_ip}:3128 https://api.ipify.org")
    else:
        print("🔓 No authentication required")
        print(f"🧪 curl -x http://{lan_ip}:3128 https://api.ipify.org")
    print("📊 API Status: http://127.0.0.1:8088/status")
    print("🔄 IP Rotation: POST http://127.0.0.1:8088/rotate")
    print("📢 Send Notification: POST http://127.0.0.1:8088/notify")
    
    # Check Discord configuration
    webhook_url = config.get('discord', {}).get('webhook_url', '').strip()
    if webhook_url and webhook_url != "https://discord.com/api/webhooks/YOUR_WEBHOOK_ID/YOUR_TOKEN":
        print("📱 Discord notifications: Enabled")
        # Send initial notification
        current_ip = get_current_ip()
        send_discord_notification(current_ip, is_rotation=True)  # Initial startup
    else:
        print("📱 Discord notifications: Not configured")
    print("="*60)

    app.run(host=config['api']['bind'], port=config['api']['port'])
