#!/usr/bin/env python3
import os, time, requests, serial, yaml, json, subprocess, shutil
from flask import Flask, request, jsonify, abort
from datetime import datetime
from pathlib import Path

app = Flask(__name__)

# Resolve absolute paths once so they match sudoers exactly
SUDO_PATH = shutil.which("sudo") or "/usr/bin/sudo"
IP_PATH = shutil.which("ip") or "/usr/sbin/ip"
PPPD_PATH = shutil.which("pppd") or "/usr/sbin/pppd"
PKILL_PATH = shutil.which("pkill") or "/usr/bin/pkill"

# State files for Discord message ID and IP history
STATE_DIR = Path(__file__).parent / "state"
STATE_DIR.mkdir(exist_ok=True)
MSG_ID_PATH = STATE_DIR / "discord_message_id.txt"
IP_HISTORY_PATH = STATE_DIR / "ip_history.json"

def load_config():
    with open('config.yaml', 'r') as f:
        return yaml.safe_load(f)

def load_text(file_path):
    """Load text from file, return None if file doesn't exist or is empty."""
    try:
        if file_path.exists():
            with open(file_path, 'r', encoding="utf-8") as f:
                content = f.read().strip()
                return content if content else None
    except:
        pass
    return None

def save_text(file_path, content):
    """Save text to file."""
    try:
        with open(file_path, 'w', encoding="utf-8") as f:
            f.write(str(content).strip())
    except Exception as e:
        print(f"Warning: Could not save to {file_path}: {e}")

def load_ip_history():
    """Load IP history from JSON file."""
    try:
        if IP_HISTORY_PATH.exists():
            with open(IP_HISTORY_PATH, 'r', encoding="utf-8") as f:
                return json.load(f)
    except:
        pass
    return {"ips": [], "rotations": 0, "first_seen": None}

def save_ip_history(history):
    """Save IP history to JSON file."""
    try:
        with open(IP_HISTORY_PATH, 'w', encoding="utf-8") as f:
            json.dump(history, f, indent=2)
    except Exception as e:
        print(f"Warning: Could not save IP history: {e}")

def update_ip_history(current_ip):
    """Update IP history with current IP."""
    history = load_ip_history()
    now = datetime.now().isoformat()

    # Check if this is a new IP
    if not history["ips"] or history["ips"][-1]["ip"] != current_ip:
        # Add new IP entry
        history["ips"].append({
            "ip": current_ip,
            "timestamp": now,
            "time": datetime.now().strftime("%H:%M:%S"),
            "date": datetime.now().strftime("%d/%m/%Y")
        })

        # Increment rotation count if not the first IP
        if history["first_seen"] is None:
            history["first_seen"] = now
        elif len(history["ips"]) > 1:
            history["rotations"] += 1

        # Keep only last 10 IPs to avoid huge messages
        if len(history["ips"]) > 10:
            history["ips"] = history["ips"][-10:]

        save_ip_history(history)

    return history

def get_current_ip():
    """Get current public IP address."""
    try:
        response = requests.get('https://ipv4.icanhazip.com', timeout=10)
        response.raise_for_status()
        return response.text.strip()
    except:
        try:
            response = requests.get('https://api.ipify.org', timeout=10)
            response.raise_for_status()
            return response.text.strip()
        except:
            return "Unknown"

def build_discord_embed(current_ip, previous_ip=None, is_rotation=False, is_failure=False, error_message=None):
    """Build Discord embed for IP notification with history."""
    history = load_ip_history()

    # Build IP history section
    history_text = ""
    if history["ips"]:
        history_text = "\n\n**📋 Recent IP History:**\n"
        # Show last 5 IPs
        recent_ips = history["ips"][-5:]
        for ip_entry in recent_ips:
            history_text += f"• `{ip_entry['ip']}` - {ip_entry['time']} {ip_entry['date']}\n"

    # Calculate uptime and rotation stats
    uptime_text = ""
    if history["first_seen"]:
        try:
            first_seen = datetime.fromisoformat(history["first_seen"])
            uptime = datetime.now() - first_seen
            hours = int(uptime.total_seconds() // 3600)
            minutes = int((uptime.total_seconds() % 3600) // 60)
            uptime_text = f"\n**⏱️ Uptime:** {hours}h {minutes}m | **🔄 Total Rotations:** {history['rotations']}"
        except Exception:
            pass

    if is_failure:
        title = "❌ IP Rotation Failed"
        description = f"**Rotation Attempt Failed**\n\n**Current IP:** `{current_ip}`\n**Error:** {error_message or 'Unknown error'}{uptime_text}{history_text}"
        color = 0xff0000  # Red for failure
        footer = f"4G Mobile Proxy Server • {datetime.now().strftime('%d/%m/%Y %H:%M')}"
    elif is_rotation and previous_ip:
        title = "🔄 4G Proxy IP Updated"
        description = f"**IP Rotation Complete**\n\n**Previous IP:** `{previous_ip}`\n**New IP:** `{current_ip}`{uptime_text}{history_text}"
        color = 0x00ff00  # Green for successful rotation
        footer = f"4G Mobile Proxy Server • {datetime.now().strftime('%d/%m/%Y %H:%M')}"
    elif is_rotation:
        title = "🚀 4G Proxy Initialized"
        description = f"**Proxy Started**\n\n**Current IP:** `{current_ip}`\n**Status:** Ready for connections{uptime_text}{history_text}"
        color = 0x0099ff  # Blue for initialization
        footer = f"4G Mobile Proxy Server • {datetime.now().strftime('%d/%m/%Y %H:%M')}"
    else:
        title = "📊 4G Proxy Status Update"
        description = f"**Current IP:** `{current_ip}`\n**Status:** Monitoring active{uptime_text}{history_text}"
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
            if e.response is not None and e.response.status_code == 404:
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
        data = r.json() if r.content else {}
        new_id = str(data.get("id", "")).strip()
        if new_id:
            save_text(msg_id_file, new_id)
        return ("posted", new_id)

def send_discord_notification(current_ip, previous_ip=None, is_rotation=False, is_failure=False, error_message=None):
    """Send Discord notification about IP change."""
    config = load_config()
    webhook_url = config.get('discord', {}).get('webhook_url', '').strip()

    if not webhook_url or webhook_url == "https://discord.com/api/webhooks/YOUR_WEBHOOK_ID/YOUR_TOKEN":
        return False

    # Update IP history only for successful operations
    if not is_failure and current_ip and current_ip != "Unknown":
        update_ip_history(current_ip)

    try:
        payload = build_discord_embed(current_ip, previous_ip, is_rotation, is_failure, error_message)
        action, msg_id = post_or_patch_discord(webhook_url, payload, MSG_ID_PATH)
        print(f"Discord notification {action} (ID: {msg_id})")
        return True
    except Exception as e:
        print(f"Failed to send Discord notification: {e}")
        return False

def detect_modem_port():
    try:
        for dev in os.listdir('/dev'):
            if dev.startswith('ttyUSB'):
                path = f'/dev/{dev}'
                if os.path.exists(path):
                    return path
    except Exception:
        pass
    return '/dev/ttyUSB2'

def at(cmd):
    """Send AT command with safe fallback (no crash if port missing)."""
    port = detect_modem_port()
    try:
        with serial.Serial(port, 115200, timeout=1) as ser:
            try:
                ser.reset_input_buffer()
            except Exception:
                pass
            ser.write((cmd + '\r').encode())
            time.sleep(0.5)
            return ser.read_all().decode(errors='ignore')
    except Exception:
        return ""

def ensure_ppp_default_route():
    """Ensure traffic goes through ppp0 using non-interactive sudo and absolute paths."""
    try:
        # Best-effort delete any existing default route
        subprocess.run([SUDO_PATH, "-n", IP_PATH, "route", "del", "default"],
                       check=False, timeout=5, capture_output=True, text=True)
        # Add default via ppp0 with lower priority than Wi-Fi/Eth if they reappear
        subprocess.run([SUDO_PATH, "-n", IP_PATH, "route", "add", "default", "dev", "ppp0", "metric", "200"],
                       check=True, timeout=5, capture_output=True, text=True)
        print("Routing fixed - traffic will go through ppp0")
    except Exception as e:
        print(f"Warning: Could not fix routing: {e}")

# Store previous IP for change detection
previous_ip = None

def _auth_or_403():
    """Strict Bearer token check (no substrings)."""
    config = load_config()
    expected = str(config['api']['token']).strip()
    auth = request.headers.get('Authorization', '')
    if not expected or not auth.startswith("Bearer ") or auth.split(" ", 1)[1] != expected:
        abort(403)

@app.get('/status')
def status():
    pdp = at('AT+CGPADDR')
    pub = get_current_ip()
    return jsonify({'pdp': pdp, 'public_ip': pub})

@app.post('/rotate')
def rotate():
    global previous_ip
    _auth_or_403()

    config = load_config()

    # Store current IP before rotation
    current_ip = get_current_ip()
    previous_ip = current_ip

    try:
        # Perform rotation using PPP restart (more reliable than AT commands)
        print("Starting IP rotation...")

        # Load rotation configuration
        rotation_config = config.get('rotation', {})
        teardown_wait = int(rotation_config.get('ppp_teardown_wait', 15))
        restart_wait = int(rotation_config.get('ppp_restart_wait', 60))
        max_attempts = int(rotation_config.get('max_attempts', 3))

        print(f"Rotation config: teardown_wait={teardown_wait}s, restart_wait={restart_wait}s, max_attempts={max_attempts}")

        # Kill existing PPP connection (non-interactive sudo)
        subprocess.run([SUDO_PATH, "-n", PKILL_PATH, "pppd"], check=False)
        print(f"Waiting {teardown_wait} seconds for PPP teardown...")
        time.sleep(teardown_wait)

        # Restart PPP (non-interactive sudo, absolute path)
        result = subprocess.run([SUDO_PATH, "-n", PPPD_PATH, "call", "ee"],
                                capture_output=True, text=True, timeout=30)

        if result.returncode != 0:
            error_msg = f"PPP restart failed: {result.stderr.strip() or 'unknown error'}"
            print(f"IP rotation failed: {error_msg}")
            send_discord_notification(current_ip, previous_ip, is_rotation=False, is_failure=True, error_message=error_msg)
            return jsonify({
                'status': 'failed',
                'error': error_msg,
                'public_ip': current_ip,
                'previous_ip': previous_ip
            }), 500

        # Wait for new IP assignment
        print(f"Waiting {restart_wait} seconds for new IP assignment...")
        time.sleep(restart_wait)

        # Check if ppp0 is up (use absolute ip path)
        try:
            result = subprocess.run([IP_PATH, '-4', 'addr', 'show', 'ppp0'],
                                    capture_output=True, text=True)
            if result.returncode != 0 or 'inet ' not in result.stdout:
                error_msg = "ppp0 interface not up after restart"
                print(f"IP rotation failed: {error_msg}")
                send_discord_notification(current_ip, previous_ip, is_rotation=False, is_failure=True, error_message=error_msg)
                return jsonify({
                    'status': 'failed',
                    'error': error_msg,
                    'public_ip': current_ip,
                    'previous_ip': previous_ip
                }), 500
        except Exception:
            error_msg = "Could not check ppp0 status"
            print(f"IP rotation failed: {error_msg}")
            send_discord_notification(current_ip, previous_ip, is_rotation=False, is_failure=True, error_message=error_msg)
            return jsonify({
                'status': 'failed',
                'error': error_msg,
                'public_ip': current_ip,
                'previous_ip': previous_ip
            }), 500

        # CRITICAL: Fix routing to ensure traffic goes through ppp0, not WiFi
        print("Fixing routing to use ppp0...")
        ensure_ppp_default_route()

        # Get new IP
        new_ip = get_current_ip()
        pdp = at('AT+CGPADDR') if new_ip != "Unknown" else ""

        # Check if rotation was successful
        if new_ip == previous_ip or new_ip == "Unknown":
            # Rotation failed - IP didn't change, try retry logic
            print(f"IP rotation failed: {new_ip} (same as {previous_ip})")

            # Try additional attempts if configured
            for attempt in range(1, max_attempts):
                print(f"Retry attempt {attempt + 1}/{max_attempts}...")

                # Wait a bit longer and try again
                additional_wait = max(5, restart_wait // 2)  # Half the restart wait, at least 5s
                print(f"Waiting additional {additional_wait} seconds...")
                time.sleep(additional_wait)

                # Check IP again
                new_ip = get_current_ip()
                if new_ip != previous_ip and new_ip != "Unknown":
                    print(f"IP rotation successful on retry {attempt + 1}: {previous_ip} -> {new_ip}")
                    send_discord_notification(new_ip, previous_ip, is_rotation=True)
                    return jsonify({
                        'status': 'success',
                        'pdp': pdp,
                        'public_ip': new_ip,
                        'previous_ip': previous_ip,
                        'attempts': attempt + 1
                    })

            # All attempts failed
            error_msg = f"IP did not change after {max_attempts} rotation attempts"
            if new_ip == "Unknown":
                error_msg = "Failed to get IP address after rotation attempts"

            print(f"IP rotation failed: {error_msg}")
            send_discord_notification(current_ip, previous_ip, is_rotation=False, is_failure=True, error_message=error_msg)

            return jsonify({
                'status': 'failed',
                'error': error_msg,
                'pdp': pdp,
                'public_ip': new_ip,
                'previous_ip': previous_ip,
                'attempts': max_attempts
            }), 400
        else:
            # Rotation successful on first attempt
            print(f"IP rotation successful: {previous_ip} -> {new_ip}")
            send_discord_notification(new_ip, previous_ip, is_rotation=True)

            return jsonify({
                'status': 'success',
                'pdp': pdp,
                'public_ip': new_ip,
                'previous_ip': previous_ip,
                'attempts': 1
            })

    except Exception as e:
        # Rotation failed with exception
        error_msg = f"Rotation failed: {str(e)}"
        print(f"IP rotation failed: {error_msg}")
        send_discord_notification(current_ip, previous_ip, is_rotation=False, is_failure=True, error_message=error_msg)

        return jsonify({
            'status': 'failed',
            'error': error_msg,
            'public_ip': current_ip,
            'previous_ip': previous_ip
        }), 500

@app.post('/notify')
def notify():
    """Send Discord notification with current status."""
    _auth_or_403()
    current_ip = get_current_ip()
    send_discord_notification(current_ip, is_rotation=False)
    return jsonify({'status': 'notification_sent', 'ip': current_ip})

@app.get('/history')
def history():
    """Get IP rotation history."""
    _auth_or_403()
    history = load_ip_history()
    return jsonify(history)

@app.post('/test-failure')
def test_failure():
    """Test failure notification (for debugging)."""
    _auth_or_403()
    current_ip = get_current_ip()
    error_msg = request.json.get('error', 'Test failure notification') if request.is_json else 'Test failure notification'
    send_discord_notification(current_ip, is_rotation=False, is_failure=True, error_message=error_msg)
    return jsonify({'status': 'failure_notification_sent', 'ip': current_ip, 'error': error_msg})

if __name__ == '__main__':
    # NOTE: routing is applied only during /rotate; no default route changes here.
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
    print("🔄 IP Rotation: POST http://127.0.0.1:8088/rotate  (Authorization: Bearer <token>)")
    print("📢 Send Notification: POST http://127.0.0.1:8088/notify  (Authorization: Bearer <token>)")
    print("📋 IP History: GET http://127.0.0.1:8088/history  (Authorization: Bearer <token>)")
    print("🧪 Test Failure: POST http://127.0.0.1:8088/test-failure  (Authorization: Bearer <token>)")

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
