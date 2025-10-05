#!/usr/bin/env python3
import os, time, requests, serial, yaml, json, subprocess
from flask import Flask, request, jsonify, abort
from datetime import datetime
from pathlib import Path

app = Flask(__name__)

# ========= Paths & helpers =========

def which(name, default=None):
    try:
        out = subprocess.run(["which", name], capture_output=True, text=True, check=False, timeout=3)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    return default or name

# Absolute paths used with sudo -n (no password/tty)
SUDO_PATH      = which("sudo", "/usr/bin/sudo")
PKILL_PATH     = which("pkill", "/usr/bin/pkill")
PPPD_PATH      = which("pppd", "/usr/sbin/pppd")
IP_PATH        = which("ip", "/usr/sbin/ip")
SYSTEMCTL_PATH = which("systemctl", "/bin/systemctl")
MMCLI_PATH     = which("mmcli", "/usr/bin/mmcli")

# ========= State files =========

STATE_DIR = Path(__file__).parent / "state"
STATE_DIR.mkdir(exist_ok=True)
MSG_ID_PATH = STATE_DIR / "discord_message_id.txt"
IP_HISTORY_PATH = STATE_DIR / "ip_history.json"

# ========= Config =========

def load_config():
    with open('config.yaml', 'r') as f:
        return yaml.safe_load(f)

# ========= File helpers =========

def load_text(file_path: Path):
    """Load text from file, return None if file doesn't exist or is empty."""
    try:
        if file_path.exists():
            txt = file_path.read_text(encoding="utf-8").strip()
            return txt if txt else None
    except Exception:
        pass
    return None

def save_text(file_path: Path, content):
    """Save text to file."""
    try:
        file_path.write_text(str(content).strip(), encoding="utf-8")
    except Exception as e:
        print(f"Warning: Could not save to {file_path}: {e}")

# ========= History =========

def load_ip_history():
    """Load IP history from JSON file."""
    try:
        if IP_HISTORY_PATH.exists():
            return json.loads(IP_HISTORY_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"ips": [], "rotations": 0, "first_seen": None}

def save_ip_history(history):
    """Save IP history to JSON file."""
    try:
        IP_HISTORY_PATH.write_text(json.dumps(history, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"Warning: Could not save IP history: {e}")

def update_ip_history(current_ip):
    """Update IP history with current IP."""
    history = load_ip_history()
    now = datetime.now().isoformat()
    if not history["ips"] or history["ips"][-1]["ip"] != current_ip:
        history["ips"].append({
            "ip": current_ip,
            "timestamp": now,
            "time": datetime.now().strftime("%H:%M:%S"),
            "date": datetime.now().strftime("%d/%m/%Y")
        })
        if history["first_seen"] is None:
            history["first_seen"] = now
        elif len(history["ips"]) > 1:
            history["rotations"] += 1
        if len(history["ips"]) > 10:
            history["ips"] = history["ips"][-10:]
        save_ip_history(history)
    return history

# ========= Network / modem =========

def detect_modem_port():
    for dev in os.listdir('/dev'):
        if dev.startswith('ttyUSB'):
            return f'/dev/{dev}'
    return '/dev/ttyUSB2'

def at(cmd, port=None, baud=115200, read_delay=0.5, timeout=1):
    port = port or detect_modem_port()
    try:
        with serial.Serial(port, baud, timeout=timeout) as ser:
            ser.write((cmd + '\r').encode())
            time.sleep(read_delay)
            return ser.read_all().decode(errors='ignore')
    except Exception as e:
        print(f"AT error on {port}: {e}")
        return ""

def deep_reset_modem(method: str, wait_seconds: int):
    """
    Perform a deeper detach to avoid CGNAT 'sticky IP':
      - 'mmcli': enable service, disable modem, hold, enable, stop service (so PPP can own ports)
      - 'at':    send AT+CFUN=1,1 (reboot RF), optional CGATT detach
    """
    method = (method or "").lower().strip()
    print(f"Deep reset method: {method or 'none'} (wait {wait_seconds}s)")
    if method == "mmcli":
        try:
            # Ensure ModemManager is running to control the modem
            print("MM: Starting ModemManager service...")
            subprocess.run([SUDO_PATH, "-n", SYSTEMCTL_PATH, "start", "ModemManager"],
                           check=False, capture_output=True, text=True, timeout=10)
            time.sleep(2)

            # Disable the modem (radio off / disconnect)
            print("MM: Disabling modem...")
            subprocess.run([SUDO_PATH, "-n", MMCLI_PATH, "-m", "0", "--disable"],
                           check=False, capture_output=True, text=True, timeout=15)
            time.sleep(2)

            # Hold offline to let CGNAT/PGW forget the session mapping
            print(f"MM: Waiting {wait_seconds}s for CGNAT detach...")
            time.sleep(max(5, wait_seconds))

            # Re-enable radio, let it re-register
            print("MM: Enabling modem...")
            subprocess.run([SUDO_PATH, "-n", MMCLI_PATH, "-m", "0", "--enable"],
                           check=False, capture_output=True, text=True, timeout=15)
            time.sleep(3)

            # Stop to avoid port grabs once PPP starts
            print("MM: Stopping ModemManager service...")
            subprocess.run([SUDO_PATH, "-n", SYSTEMCTL_PATH, "stop", "ModemManager"],
                           check=False, capture_output=True, text=True, timeout=10)
            time.sleep(5)  # extra settle time
            print("MM: Deep reset via ModemManager completed.")
        except Exception as e:
            print(f"MM: Deep reset (mmcli) failed: {e}")

    elif method == "at":
        try:
            p = detect_modem_port()
            print(f"AT: Using port {p}")
            print("AT: Sending CGATT=0 (detach)...")
            at("AT+CGATT=0", port=p, read_delay=0.8, timeout=2)
            time.sleep(1.0)
            print("AT: Sending CFUN=1,1 (full function reset)...")
            at("AT+CFUN=1,1", port=p, read_delay=0.8, timeout=2)
            print(f"AT: Waiting {wait_seconds}s for module reset/re-enumeration...")
            time.sleep(max(30, wait_seconds))
            print("AT: Deep reset via AT commands completed.")
        except Exception as e:
            print(f"AT: Deep reset (AT) failed: {e}")
    else:
        print("Deep reset skipped.")

def ensure_ppp_default_route():
    """
    Keep existing default (wifi/eth) as primary; add ppp0 as secondary (higher metric).
    This protects SSH/LAN while still letting you force proxy over ppp0 if desired.
    """
    try:
        res = subprocess.run([IP_PATH, "route", "show", "default"], capture_output=True, text=True, timeout=5)
        line = res.stdout.splitlines()[0] if res.stdout else ""
        parts = line.split()
        gw = dev = None
        metric = 100
        for i, p in enumerate(parts):
            if p == "via" and i+1 < len(parts): gw = parts[i+1]
            if p == "dev" and i+1 < len(parts): dev = parts[i+1]
            if p == "metric" and i+1 < len(parts):
                try: metric = int(parts[i+1])
                except: pass
        if dev and dev != "ppp0":
            subprocess.run([SUDO_PATH, "-n", IP_PATH, "route", "replace", "default",
                            "via", gw, "dev", dev, "metric", str(metric)],
                           check=False, capture_output=True, text=True, timeout=5)
            subprocess.run([SUDO_PATH, "-n", IP_PATH, "route", "add", "default",
                            "dev", "ppp0", "metric", str(metric + 500)],
                           check=False, capture_output=True, text=True, timeout=5)
            print(f"Routing: kept {dev} primary (metric {metric}); added ppp0 (metric {metric+500})")
        else:
            subprocess.run([SUDO_PATH, "-n", IP_PATH, "route", "add", "default",
                            "dev", "ppp0", "metric", "600"],
                           check=False, capture_output=True, text=True, timeout=5)
            print("Routing: added ppp0 as secondary (metric 600)")
    except Exception as e:
        print(f"Warning: Could not fix routing: {e}")

def get_current_ip():
    """Get current public IP address."""
    try:
        r = requests.get('https://ipv4.icanhazip.com', timeout=10)
        return r.text.strip()
    except Exception:
        try:
            r = requests.get('https://api.ipify.org', timeout=10)
            return r.text.strip()
        except Exception:
            return "Unknown"

# ========= Discord =========

def build_discord_embed(current_ip, previous_ip=None, is_rotation=False, is_failure=False, error_message=None):
    history = load_ip_history()

    history_text = ""
    if history["ips"]:
        history_text = "\n\n**📋 Recent IP History:**\n"
        for ip_entry in history["ips"][-5:]:
            history_text += f"• `{ip_entry['ip']}` - {ip_entry['time']} {ip_entry['date']}\n"

    uptime_text = ""
    if history["first_seen"]:
        first_seen = datetime.fromisoformat(history["first_seen"])
        uptime = datetime.now() - first_seen
        hours = int(uptime.total_seconds() // 3600)
        minutes = int((uptime.total_seconds() % 3600) // 60)
        uptime_text = f"\n**⏱️ Uptime:** {hours}h {minutes}m | **🔄 Total Rotations:** {history['rotations']}"

    if is_failure:
        title = "❌ IP Rotation Failed"
        description = f"**Rotation Attempt Failed**\n\n**Current IP:** `{current_ip}`\n**Error:** {error_message or 'Unknown error'}{uptime_text}{history_text}"
        color = 0xff0000
    elif is_rotation and previous_ip:
        title = "🔄 4G Proxy IP Updated"
        description = f"**IP Rotation Complete**\n\n**Previous IP:** `{previous_ip}`\n**New IP:** `{current_ip}`{uptime_text}{history_text}"
        color = 0x00ff00
    elif is_rotation:
        title = "🚀 4G Proxy Initialized"
        description = f"**Proxy Started**\n\n**Current IP:** `{current_ip}`\n**Status:** Ready for connections{uptime_text}{history_text}"
        color = 0x0099ff
    else:
        title = "📊 4G Proxy Status Update"
        description = f"**Current IP:** `{current_ip}`\n**Status:** Monitoring active{uptime_text}{history_text}"
        color = 0xff9900

    embed = {
        "title": title,
        "description": description,
        "color": color,
        "footer": {"text": f"4G Mobile Proxy Server • {datetime.now().strftime('%d/%m/%Y %H:%M')}"},
        "timestamp": datetime.now().isoformat()
    }
    return {
        "content": None,
        "embeds": [embed],
        "allowed_mentions": {"parse": []}
    }

def post_or_patch_discord(webhook_url, payload, msg_id_file):
    message_id = load_text(msg_id_file)
    if message_id:
        url = f"{webhook_url.split('?')[0]}/messages/{message_id}"
        try:
            r = requests.patch(url, json=payload, timeout=20)
            r.raise_for_status()
            return ("patched", message_id)
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 404:
                print("Old Discord message not found (deleted?), creating new one...")
                message_id = None
            else:
                raise
    if not message_id:
        url = f"{webhook_url}?wait=true" if "?wait" not in webhook_url else webhook_url
        r = requests.post(url, json=payload, timeout=20)
        r.raise_for_status()
        data = r.json()
        new_id = str(data.get("id", "")).strip()
        if new_id:
            save_text(msg_id_file, new_id)
        return ("posted", new_id)

def send_discord_notification(current_ip, previous_ip=None, is_rotation=False, is_failure=False, error_message=None):
    config = load_config()
    webhook_url = config.get('discord', {}).get('webhook_url', '').strip()
    if not webhook_url or webhook_url == "https://discord.com/api/webhooks/YOUR_WEBHOOK_ID/YOUR_TOKEN":
        return False
    if not is_failure:
        update_ip_history(current_ip)
    try:
        payload = build_discord_embed(current_ip, previous_ip, is_rotation, is_failure, error_message)
        action, msg_id = post_or_patch_discord(webhook_url, payload, MSG_ID_PATH)
        print(f"Discord notification {action} (ID: {msg_id})")
        return True
    except Exception as e:
        print(f"Failed to send Discord notification: {e}")
        return False

# ========= Helpers for PPP workflow =========

def teardown_ppp(wait_s: int):
    """Stop any running PPP session and wait a bit."""
    subprocess.run([SUDO_PATH, "-n", PKILL_PATH, "pppd"], check=False)
    print(f"Waiting {wait_s} seconds for PPP teardown...")
    time.sleep(max(1, int(wait_s)))

def start_ppp():
    """Start PPP session (non-interactive)."""
    res = subprocess.run([SUDO_PATH, "-n", PPPD_PATH, "call", "ee"],
                         capture_output=True, text=True, timeout=60, check=False)
    if res.returncode != 0:
        raise RuntimeError(f"PPP start failed: {res.stderr.strip() or res.stdout.strip()}")

def wait_for_ppp_up(timeout_s: int) -> bool:
    """Poll for ppp0 IPv4 address."""
    deadline = time.time() + max(5, int(timeout_s))
    while time.time() < deadline:
        r = subprocess.run([IP_PATH, "-4", "addr", "show", "ppp0"],
                           capture_output=True, text=True)
        if r.returncode == 0 and "inet " in r.stdout:
            return True
        time.sleep(2)
    return False

# ========= Global prev IP =========

previous_ip = None

# ========= API =========

@app.get('/status')
def status():
    pdp = at('AT+CGPADDR')
    pub = get_current_ip()
    return jsonify({'pdp': pdp, 'public_ip': pub})

@app.post('/rotate')
def rotate():
    """
    Conditional escalation:
      1) Attempt 1: PPP restart -> if IP changes, done.
      2) If unchanged (or PPP not up), attempt 2+: deep reset (mmcli/AT) → PPP → longer wait → route fix → check IP.
    """
    global previous_ip
    config = load_config()
    expected = config['api']['token']
    token = request.headers.get('Authorization', '')
    if expected not in token:
        abort(403)

    current_ip = get_current_ip()
    previous_ip = current_ip

    try:
        print("Starting IP rotation...")

        rotation_config = config.get('rotation', {}) or {}
        teardown_wait = int(rotation_config.get('ppp_teardown_wait', 30))
        restart_wait  = int(rotation_config.get('ppp_restart_wait', 60))
        max_attempts  = int(rotation_config.get('max_attempts', 2))
        deep_method   = (rotation_config.get('deep_reset', '') or '').lower()  # 'mmcli' | 'at' | 'conditional' | ''
        deep_wait     = int(rotation_config.get('deep_reset_wait', 180))       # seconds to wait during deep reset

        print(f"Rotation config: teardown_wait={teardown_wait}s, restart_wait={restart_wait}s, "
              f"max_attempts={max_attempts}, deep_reset={deep_method or 'off'} ({deep_wait}s)")

        for attempt in range(max_attempts):
            print(f"\n--- Rotation Attempt {attempt + 1}/{max_attempts} ---")
            # Always tear down PPP cleanly before any action
            teardown_ppp(teardown_wait)

            if attempt == 0:
                # Attempt 1: PPP-only restart
                print("Attempt 1: Simple PPP restart")
                try:
                    start_ppp()
                except Exception as e:
                    print(f"PPP restart failed on attempt 1: {e}")
                    if attempt == max_attempts - 1:
                        error_msg = "PPP restart failed on first attempt"
                        print(f"IP rotation failed: {error_msg}")
                        send_discord_notification(current_ip, previous_ip, is_rotation=False, is_failure=True, error_message=error_msg)
                        return jsonify({'status':'failed','error':error_msg,'public_ip':current_ip,'previous_ip':previous_ip}), 500
                    else:
                        continue
            else:
                # Attempt 2+: Deep reset (if configured) then PPP
                chosen = None
                if deep_method in ("mmcli", "conditional"):
                    chosen = "mmcli"
                elif deep_method == "at":
                    chosen = "at"

                if chosen:
                    print(f"Attempt {attempt + 1}: Deep reset ({chosen}) + PPP restart")
                    deep_reset_modem(chosen, deep_wait)

                    # Give USB serial ports time to re-enumerate after enable
                    print("Waiting up to 15s for modem ports to re-enumerate...")
                    t0 = time.time()
                    while time.time() - t0 < 15:
                        # If any ttyUSB is present, assume ready enough
                        if any(n.startswith("ttyUSB") for n in os.listdir("/dev")):
                            break
                        time.sleep(1)
                    else:
                        print("Warning: modem ports not visible yet; proceeding with PPP anyway.")
                else:
                    print(f"Attempt {attempt + 1}: No deep reset configured; doing PPP restart again")

                try:
                    start_ppp()
                except Exception as e:
                    print(f"PPP restart failed on attempt {attempt + 1}: {e}")
                    if attempt == max_attempts - 1:
                        error_msg = f"PPP restart failed after {max_attempts} attempts"
                        print(f"IP rotation failed: {error_msg}")
                        send_discord_notification(current_ip, previous_ip, is_rotation=False, is_failure=True, error_message=error_msg)
                        return jsonify({'status':'failed','error':error_msg,'public_ip':current_ip,'previous_ip':previous_ip}), 500
                    else:
                        continue

            # Wait for IP assignment (longer on deep attempts)
            extra = 0
            if attempt > 0:
                extra = max(30, restart_wait)   # add ~30–60s on deep attempts
            total_wait = restart_wait + extra
            print(f"Waiting {total_wait} seconds for new IP assignment...")
            if not wait_for_ppp_up(total_wait):
                print(f"ppp0 interface not up after attempt {attempt + 1}")
                if attempt == max_attempts - 1:
                    error_msg = "ppp0 interface not up after restart"
                    print(f"IP rotation failed: {error_msg}")
                    send_discord_notification(current_ip, previous_ip, is_rotation=False, is_failure=True, error_message=error_msg)
                    return jsonify({'status':'failed','error':error_msg,'public_ip':current_ip,'previous_ip':previous_ip}), 500
                else:
                    continue

            # Fix routing (keep LAN primary, add PPP secondary)
            print("Fixing routing to prefer primary and keep PPP as secondary...")
            ensure_ppp_default_route()

            # Check if we got a new IP
            new_ip = get_current_ip()
            pdp = at('AT+CGPADDR') if new_ip != "Unknown" else ""

            if new_ip != previous_ip and new_ip != "Unknown":
                print(f"✅ IP rotation successful on attempt {attempt + 1}: {previous_ip} -> {new_ip}")
                send_discord_notification(new_ip, previous_ip, is_rotation=True)
                return jsonify({
                    'status': 'success',
                    'pdp': pdp,
                    'public_ip': new_ip,
                    'previous_ip': previous_ip,
                    'attempts': attempt + 1
                })
            else:
                print(f"IP unchanged on attempt {attempt + 1}: {new_ip} (was {previous_ip})")
                if attempt < max_attempts - 1:
                    print("Trying next attempt with escalation...")
                    continue
                else:
                    error_msg = f"IP did not change after {max_attempts} attempts"
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

    except Exception as e:
        error_msg = f"Rotation failed: {str(e)}"
        print(f"IP rotation failed: {error_msg}")
        send_discord_notification(current_ip, previous_ip, is_rotation=False, is_failure=True, error_message=error_msg)
        return jsonify({'status':'failed','error':error_msg,'public_ip':current_ip,'previous_ip':previous_ip}), 500

@app.post('/notify')
def notify():
    config = load_config()
    expected = config['api']['token']
    token = request.headers.get('Authorization', '')
    if expected not in token:
        abort(403)
    current_ip = get_current_ip()
    send_discord_notification(current_ip, is_rotation=False)
    return jsonify({'status': 'notification_sent', 'ip': current_ip})

@app.get('/history')
def history():
    config = load_config()
    expected = config['api']['token']
    token = request.headers.get('Authorization', '')
    if expected not in token:
        abort(403)
    history = load_ip_history()
    return jsonify(history)

@app.post('/test-failure')
def test_failure():
    config = load_config()
    expected = config['api']['token']
    token = request.headers.get('Authorization', '')
    if expected not in token:
        abort(403)
    current_ip = get_current_ip()
    error_msg = request.json.get('error', 'Test failure notification') if request.is_json else 'Test failure notification'
    send_discord_notification(current_ip, is_rotation=False, is_failure=True, error_message=error_msg)
    return jsonify({'status': 'failure_notification_sent', 'ip': current_ip, 'error': error_msg})

if __name__ == '__main__':
    # NOTE: routing defaults are handled in main.py; only adjust during rotate()
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
    print("📋 IP History: GET http://127.0.0.1:8088/history")
    print("🧪 Test Failure: POST http://127.0.0.1:8088/test-failure")

    webhook_url = config.get('discord', {}).get('webhook_url', '').strip()
    if webhook_url and webhook_url != "https://discord.com/api/webhooks/YOUR_WEBHOOK_ID/YOUR_TOKEN":
        print("📱 Discord notifications: Enabled")
        current_ip = get_current_ip()
        send_discord_notification(current_ip, is_rotation=True)  # Initial startup
    else:
        print("📱 Discord notifications: Not configured")
    print("="*60)

    app.run(host=config['api']['bind'], port=config['api']['port'])
