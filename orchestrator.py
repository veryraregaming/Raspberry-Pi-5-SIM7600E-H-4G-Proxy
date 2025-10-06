#!/usr/bin/env python3
import os, time, requests, serial, yaml, json, subprocess, threading
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
    try:
        if file_path.exists():
            return file_path.read_text(encoding="utf-8").strip() or None
    except Exception:
        pass
    return None

def save_text(file_path: Path, content):
    try:
        file_path.write_text(str(content).strip(), encoding="utf-8")
    except Exception as e:
        print(f"Warning: Could not save to {file_path}: {e}")

# ========= History =========

def load_ip_history():
    try:
        if IP_HISTORY_PATH.exists():
            return json.loads(IP_HISTORY_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"ips": [], "rotations": 0, "first_seen": None}

def save_ip_history(history):
    try:
        IP_HISTORY_PATH.write_text(json.dumps(history, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"Warning: Could not save IP history: {e}")

def update_ip_history(current_ip, force_add=False, is_failure=False):
    """
    Update IP history with current IP.
    
    Args:
        current_ip: The current IP address
        force_add: If True, always add entry even if IP is the same (for failed rotations)
        is_failure: If True, mark this entry as a failed rotation
    """
    history = load_ip_history()
    now = datetime.now().isoformat()
    
    # Add entry if IP changed OR if force_add is True (for failed rotation attempts)
    if force_add or not history["ips"] or history["ips"][-1]["ip"] != current_ip:
        entry = {
            "ip": current_ip,
            "timestamp": now,
            "time": datetime.now().strftime("%H:%M:%S"),
            "date": datetime.now().strftime("%d/%m/%Y")
        }
        
        # Add failure flag if this is a failed rotation
        if is_failure:
            entry["failed"] = True
            entry["note"] = "Rotation Failed - Same IP"
        
        history["ips"].append(entry)
        
        if history["first_seen"] is None:
            history["first_seen"] = now
        elif len(history["ips"]) > 1 or force_add:
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
    method = (method or "").lower().strip()
    print(f"Deep reset method: {method or 'none'} (wait {wait_seconds}s)")
    if method == "mmcli":
        try:
            print("MM: Starting ModemManager service...")
            subprocess.run([SUDO_PATH, "-n", SYSTEMCTL_PATH, "start", "ModemManager"],
                           check=False, capture_output=True, text=True, timeout=10)
            time.sleep(2)

            print("MM: Disabling modem...")
            subprocess.run([SUDO_PATH, "-n", MMCLI_PATH, "-m", "0", "--disable"],
                           check=False, capture_output=True, text=True, timeout=15)
            time.sleep(2)

            print(f"MM: Waiting {wait_seconds}s for CGNAT detach...")
            time.sleep(max(5, wait_seconds))

            print("MM: Enabling modem...")
            subprocess.run([SUDO_PATH, "-n", MMCLI_PATH, "-m", "0", "--enable"],
                           check=False, capture_output=True, text=True, timeout=15)
            time.sleep(3)

            print("MM: Stopping ModemManager service...")
            subprocess.run([SUDO_PATH, "-n", SYSTEMCTL_PATH, "stop", "ModemManager"],
                           check=False, capture_output=True, text=True, timeout=10)
            time.sleep(5)
            print("MM: Deep reset via ModemManager completed.")
        except Exception as e:
            print(f"MM: Deep reset (mmcli) failed: {e}")

    elif method == "at":
        try:
            p = detect_modem_port()
            print(f"AT: Using port {p}")
            print("AT: Sending CGATT=0 (detach)…")
            at("AT+CGATT=0", port=p, read_delay=0.8, timeout=2)
            time.sleep(1.0)
            print("AT: Sending CFUN=1,1 (full function reset)…")
            at("AT+CFUN=1,1", port=p, read_delay=0.8, timeout=2)
            print(f"AT: Waiting {wait_seconds}s for module to re-enumerate…")
            time.sleep(max(30, wait_seconds))
            print("AT: Deep reset via AT completed.")
        except Exception as e:
            print(f"AT: Deep reset failed: {e}")
    else:
        print("Deep reset skipped.")

def ensure_ppp_default_route():
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
    """Get current public IPv4 address via cellular interface only."""
    global in_progress
    
    # If rotation is in progress, return "Rotating..." to avoid showing WiFi IP
    if in_progress:
        return "Rotating..."
    
    # Check if RNDIS interface is up
    rndis_iface, has_ip = detect_rndis_interface()
    
    # If RNDIS is down or has no IP, check for PPP
    if not (rndis_iface and has_ip):
        try:
            r = subprocess.run([IP_PATH, "-4", "addr", "show", "ppp0"], 
                             capture_output=True, text=True, timeout=3)
            if r.returncode != 0 or "inet " not in r.stdout:
                # Neither RNDIS nor PPP is up
                return "Unknown"
        except Exception:
            return "Unknown"
    
    # Cellular interface is up, check public IP via proxy
    try:
        # Use proxy to ensure we're checking cellular IP, not WiFi
        # This forces the request through Squid which routes via cellular
        config = load_config()
        lan_ip = config.get('lan_bind_ip', '127.0.0.1')
        proxies = {
            'http': f'http://{lan_ip}:3128',
            'https': f'http://{lan_ip}:3128'
        }
        r = requests.get('https://api.ipify.org', proxies=proxies, timeout=10)
        ip = r.text.strip()
        
        # Verify this is a public IP (not private/home network)
        if ip.startswith('192.168.') or ip.startswith('10.') or ip.startswith('172.') or ip.startswith('86.151.'):
            return "Unknown"
        
        return ip
    except Exception:
        # If proxy fails, return Unknown (don't fallback to WiFi)
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

# ========= RNDIS helpers =========

def detect_rndis_interface():
    """Detect RNDIS interface (enx*) that provides cellular connectivity."""
    try:
        # Use absolute path to avoid subprocess issues
        ip_cmd = IP_PATH if IP_PATH and os.path.exists(IP_PATH) else "/usr/sbin/ip"
        
        result = subprocess.run([ip_cmd, "-br", "link", "show"], 
                               capture_output=True, text=True, check=False, timeout=5)
        if result.returncode != 0:
            print(f"detect_rndis_interface: ip link failed: {result.stderr}")
            return None, False
        
        print(f"detect_rndis_interface: Checking interfaces...")
        print(f"detect_rndis_interface: Output: {result.stdout[:200]}")
            
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith("enx") or line.startswith("eth1"):
                parts = line.split()
                if not parts:
                    continue
                iface = parts[0]
                print(f"detect_rndis_interface: Found interface {iface}")
                
                # Check if interface has an IP address
                ip_result = subprocess.run([ip_cmd, "-4", "addr", "show", iface],
                                          capture_output=True, text=True, check=False, timeout=5)
                if ip_result.returncode != 0:
                    print(f"detect_rndis_interface: ip addr failed for {iface}: {ip_result.stderr}")
                    return iface, False
                    
                if "inet " in ip_result.stdout:
                    print(f"detect_rndis_interface: Interface {iface} has IP")
                    return iface, True
                else:
                    print(f"detect_rndis_interface: Interface {iface} has no IP")
                    return iface, False
                    
        print("detect_rndis_interface: No RNDIS interfaces found in output")
    except Exception as e:
        print(f"detect_rndis_interface: Exception: {e}")
        import traceback
        traceback.print_exc()
    return None, False

def deep_reset_rndis_modem():
    """Deep reset modem radio to force new PDP context and better IP variety."""
    print("🔄 Performing deep modem reset (radio + PDP context)...")
    try:
        at_port = detect_modem_port()
        if not at_port or not os.path.exists(at_port):
            print(f"⚠️ Deep reset skipped: AT port {at_port} not available")
            return False
            
        with serial.Serial(at_port, 115200, timeout=5) as ser:
            # Deactivate PDP context
            print("  📡 Deactivating PDP context...")
            ser.write(b"AT+CGACT=0,1\r\n")
            time.sleep(2)
            ser.read_all()  # Clear buffer
            
            # Detach from network
            print("  📡 Detaching from network...")
            ser.write(b"AT+CGATT=0\r\n")
            time.sleep(2)
            ser.read_all()
            
            # Radio off
            print("  📴 Radio off...")
            ser.write(b"AT+CFUN=0\r\n")
            time.sleep(5)
            ser.read_all()
            
            # Radio on
            print("  📡 Radio on...")
            ser.write(b"AT+CFUN=1\r\n")
            time.sleep(8)
            ser.read_all()
            
            # Reattach to network
            print("  📡 Reattaching to network...")
            ser.write(b"AT+CGATT=1\r\n")
            time.sleep(2)
            ser.read_all()
            
            # Reactivate PDP context
            print("  📡 Reactivating PDP context...")
            ser.write(b"AT+CGACT=1,1\r\n")
            time.sleep(2)
            ser.read_all()
            
        print("  ✅ Deep modem reset complete")
        return True
    except Exception as e:
        print(f"  ⚠️ Deep modem reset failed: {e}")
        return False

def teardown_rndis(wait_s: int, deep_reset: bool = False):
    """Teardown RNDIS interface by bringing it down."""
    iface, _ = detect_rndis_interface()
    if iface:
        print(f"Bringing down RNDIS interface: {iface}")
        subprocess.run([SUDO_PATH, "-n", IP_PATH, "link", "set", "dev", iface, "down"], 
                      check=False)
        
        if deep_reset:
            # Perform deep modem reset for better IP variety
            deep_reset_rndis_modem()
            print(f"Waiting {wait_s} seconds after deep reset...")
        else:
            print(f"Waiting {wait_s} seconds for RNDIS teardown...")
        
        time.sleep(max(1, int(wait_s)))
    else:
        print("No RNDIS interface found, skipping teardown")

def start_rndis():
    """Start RNDIS interface by bringing it up and getting DHCP."""
    iface, has_ip = detect_rndis_interface()
    if not iface:
        raise RuntimeError("No RNDIS interface found")
    
    print(f"Bringing up RNDIS interface: {iface}")
    # Bring interface up
    res = subprocess.run([SUDO_PATH, "-n", IP_PATH, "link", "set", "dev", iface, "up"],
                        capture_output=True, text=True, check=False)
    if res.returncode != 0:
        raise RuntimeError(f"Failed to bring up {iface}: {res.stderr.strip()}")
    
    time.sleep(2)
    
    # Get IP via DHCP
    print(f"Getting IP via DHCP for {iface}...")
    res = subprocess.run([SUDO_PATH, "-n", "dhclient", "-v", iface],
                        capture_output=True, text=True, timeout=30, check=False)
    
    if res.returncode != 0:
        raise RuntimeError(f"DHCP failed for {iface}: {res.stderr.strip()}")
    
    return iface

def wait_for_rndis_up(timeout_s: int) -> bool:
    """Wait for RNDIS interface to get an IP address."""
    deadline = time.time() + max(5, int(timeout_s))
    while time.time() < deadline:
        iface, has_ip = detect_rndis_interface()
        if iface and has_ip:
            return True
        time.sleep(2)
    return False

# ========= PPP helpers (fallback) =========

def teardown_ppp(wait_s: int):
    subprocess.run([SUDO_PATH, "-n", PKILL_PATH, "pppd"], check=False)
    print(f"Waiting {wait_s} seconds for PPP teardown...")
    time.sleep(max(1, int(wait_s)))

def start_ppp():
    res = subprocess.run([SUDO_PATH, "-n", PPPD_PATH, "call", "ee"],
                         capture_output=True, text=True, timeout=60, check=False)
    if res.returncode != 0:
        raise RuntimeError(f"PPP start failed: {res.stderr.strip() or res.stdout.strip()}")

def wait_for_ppp_up(timeout_s: int) -> bool:
    deadline = time.time() + max(5, int(timeout_s))
    while time.time() < deadline:
        r = subprocess.run([IP_PATH, "-4", "addr", "show", "ppp0"],
                           capture_output=True, text=True)
        if r.returncode == 0 and "inet " in r.stdout:
            return True
        time.sleep(2)
    return False

# ========= Prevent concurrent rotates =========
rotate_lock = threading.Lock()
in_progress = False

# ========= Auto-rotation timer =========
auto_rotation_thread = None
auto_rotation_stop_event = threading.Event()
auto_rotation_enabled = True

def auto_rotation_worker():
    """Background thread that performs automatic IP rotation."""
    global auto_rotation_enabled
    
    while not auto_rotation_stop_event.is_set():
        try:
            config = load_config()
            interval = config.get('pm2', {}).get('ip_rotation_interval', 300)  # Default 5 minutes
            
            if auto_rotation_enabled and interval > 0:
                print(f"Auto-rotation: Waiting {interval} seconds until next rotation...")
                
                # Wait for the interval, but check for stop event periodically
                for _ in range(interval):
                    if auto_rotation_stop_event.is_set():
                        return
                    time.sleep(1)
                
                if not auto_rotation_stop_event.is_set() and auto_rotation_enabled:
                    print("Auto-rotation: Triggering scheduled IP rotation...")
                    
                    # Call the rotation function directly (bypass API auth)
                    try:
                        current_ip = get_current_ip()
                        previous_ip = current_ip

                        # Check if we have RNDIS interface available
                        rndis_iface, rndis_has_ip = detect_rndis_interface()
                        
                        if rndis_iface:
                            print(f"Auto-rotation: Using RNDIS interface: {rndis_iface}")
                            rotation_config = config.get('rotation', {}) or {}
                            teardown_wait = int(rotation_config.get('ppp_teardown_wait', 30))
                            restart_wait  = int(rotation_config.get('ppp_restart_wait', 60))
                            max_attempts  = int(rotation_config.get('max_attempts', 2))

                            for attempt in range(max_attempts):
                                print(f"Auto-rotation: RNDIS Rotation Attempt {attempt + 1}/{max_attempts}")
                                
                                # Use deep reset on second attempt for better IP variety
                                use_deep_reset = (attempt > 0)
                                if use_deep_reset:
                                    print(f"Auto-rotation: Using deep reset on attempt {attempt + 1}")
                                
                                teardown_rndis(teardown_wait, deep_reset=use_deep_reset)

                                try:
                                    start_rndis()
                                except Exception as e:
                                    print(f"Auto-rotation: RNDIS restart failed on attempt {attempt + 1}: {e}")
                                    if attempt == max_attempts - 1:
                                        err = f"RNDIS restart failed after {max_attempts} attempts"
                                        print(f"Auto-rotation failed: {err}")
                                        send_discord_notification(current_ip, previous_ip, is_rotation=False, is_failure=True, error_message=err)
                                        continue
                                    continue

                                if not wait_for_rndis_up(restart_wait):
                                    print(f"Auto-rotation: RNDIS interface did not come up within {restart_wait} seconds")
                                    if attempt == max_attempts - 1:
                                        err = f"RNDIS interface failed to get IP after {max_attempts} attempts"
                                        print(f"Auto-rotation failed: {err}")
                                        send_discord_notification(current_ip, previous_ip, is_rotation=False, is_failure=True, error_message=err)
                                        continue
                                    continue

                                # Check if IP changed
                                time.sleep(5)  # Give it a moment to stabilize
                                new_ip = get_current_ip()
                                
                                # Update IP history regardless of success/failure
                                if new_ip != "Unknown":
                                    update_ip_history(new_ip)
                                
                                if new_ip != previous_ip and new_ip != "Unknown":
                                    print(f"✅ Auto-rotation successful: {previous_ip} -> {new_ip}")
                                    send_discord_notification(new_ip, previous_ip, is_rotation=True)
                                    break
                                else:
                                    print(f"Auto-rotation: IP unchanged on attempt {attempt + 1}: {new_ip} (was {previous_ip})")
                                    if attempt < max_attempts - 1:
                                        continue
                                    err = f"IP did not change after {max_attempts} attempts"
                                    print(f"Auto-rotation failed: {err}")
                                    # Force add to history even though IP is same (to show failed rotation attempt)
                                    update_ip_history(new_ip, force_add=True, is_failure=True)
                                    send_discord_notification(current_ip, previous_ip, is_rotation=False, is_failure=True, error_message=err)
                        else:
                            print("Auto-rotation: No RNDIS interface found, skipping rotation")
                            
                    except Exception as e:
                        err = f"Auto-rotation failed: {str(e)}"
                        print(f"Auto-rotation error: {err}")
                        try:
                            current_ip = get_current_ip()
                        except Exception:
                            current_ip = "Unknown"
                        send_discord_notification(current_ip, None, is_rotation=False, is_failure=True, error_message=err)
            else:
                # If disabled or interval is 0, wait longer and check again
                time.sleep(60)
                
        except Exception as e:
            print(f"Auto-rotation worker error: {e}")
            time.sleep(60)  # Wait before retrying

def start_auto_rotation():
    """Start the auto-rotation background thread."""
    global auto_rotation_thread, auto_rotation_stop_event
    
    if auto_rotation_thread is None or not auto_rotation_thread.is_alive():
        auto_rotation_stop_event.clear()
        auto_rotation_thread = threading.Thread(target=auto_rotation_worker, daemon=True)
        auto_rotation_thread.start()
        print("✅ Auto-rotation thread started")

def stop_auto_rotation():
    """Stop the auto-rotation background thread."""
    global auto_rotation_thread, auto_rotation_stop_event
    
    if auto_rotation_thread and auto_rotation_thread.is_alive():
        auto_rotation_stop_event.set()
        auto_rotation_thread.join(timeout=5)
        print("✅ Auto-rotation thread stopped")

def set_auto_rotation_enabled(enabled):
    """Enable or disable auto-rotation."""
    global auto_rotation_enabled
    auto_rotation_enabled = enabled
    status = "enabled" if enabled else "disabled"
    print(f"Auto-rotation {status}")

# ========= API =========

@app.get('/status')
def status():
    pdp = at('AT+CGPADDR')
    pub = get_current_ip()
    up = False
    try:
        r = subprocess.run([IP_PATH, "-4", "addr", "show", "ppp0"], capture_output=True, text=True)
        up = (r.returncode == 0 and "inet " in r.stdout)
    except Exception:
        pass
    return jsonify({'pdp': pdp, 'public_ip': pub, 'ppp_up': up})

@app.post('/rotate')
def rotate():
    global in_progress
    if not rotate_lock.acquire(blocking=False):
        return jsonify({'status': 'busy', 'message': 'rotation already in progress'}), 429
    in_progress = True
    try:
        config = load_config()
        expected = config['api']['token']
        token = request.headers.get('Authorization', '')
        if expected not in token:
            abort(403)

        current_ip = get_current_ip()
        previous_ip = current_ip

        print("Starting IP rotation...")

        # Check if we have RNDIS interface available
        rndis_iface, rndis_has_ip = detect_rndis_interface()
        
        if rndis_iface:
            print(f"Using RNDIS interface: {rndis_iface}")
            rotation_config = config.get('rotation', {}) or {}
            teardown_wait = int(rotation_config.get('ppp_teardown_wait', 30))
            restart_wait  = int(rotation_config.get('ppp_restart_wait', 60))
            max_attempts  = int(rotation_config.get('max_attempts', 2))

            print(f"RNDIS rotation config: teardown_wait={teardown_wait}s, restart_wait={restart_wait}s, max_attempts={max_attempts}")

            for attempt in range(max_attempts):
                print(f"\n--- RNDIS Rotation Attempt {attempt + 1}/{max_attempts} ---")
                
                # Use deep reset on second attempt for better IP variety
                use_deep_reset = (attempt > 0)
                if use_deep_reset:
                    print(f"Using deep reset on attempt {attempt + 1} for better IP variety")
                
                teardown_rndis(teardown_wait, deep_reset=use_deep_reset)

                try:
                    start_rndis()
                except Exception as e:
                    print(f"RNDIS restart failed on attempt {attempt + 1}: {e}")
                    if attempt == max_attempts - 1:
                        err = f"RNDIS restart failed after {max_attempts} attempts"
                        print(f"IP rotation failed: {err}")
                        send_discord_notification(current_ip, previous_ip, is_rotation=False, is_failure=True, error_message=err)
                        return jsonify({'status':'failed','error':err,'public_ip':current_ip,'previous_ip':previous_ip}), 500
                    continue

                total_wait = restart_wait
                print(f"Waiting {total_wait} seconds for new IP assignment...")
                if not wait_for_rndis_up(total_wait):
                    print(f"RNDIS interface did not come up within {total_wait} seconds")
                    if attempt == max_attempts - 1:
                        err = f"RNDIS interface failed to get IP after {max_attempts} attempts"
                        print(f"IP rotation failed: {err}")
                        send_discord_notification(current_ip, previous_ip, is_rotation=False, is_failure=True, error_message=err)
                        return jsonify({'status':'failed','error':err,'public_ip':current_ip,'previous_ip':previous_ip}), 500
                    continue

                # Check if IP changed
                time.sleep(5)  # Give it a moment to stabilize
                new_ip = get_current_ip()
                
                # Update IP history regardless of success/failure
                if new_ip != "Unknown":
                    update_ip_history(new_ip)
                
                if new_ip != previous_ip and new_ip != "Unknown":
                    print(f"✅ IP rotation successful on attempt {attempt + 1}: {previous_ip} -> {new_ip}")
                    send_discord_notification(new_ip, previous_ip, is_rotation=True)
                    return jsonify({
                        'status': 'success',
                        'public_ip': new_ip,
                        'previous_ip': previous_ip,
                        'attempts': attempt + 1
                    })
                else:
                    print(f"IP unchanged on attempt {attempt + 1}: {new_ip} (was {previous_ip})")
                    if attempt < max_attempts - 1:
                        print("Trying next attempt...")
                        continue
                    err = f"IP did not change after {max_attempts} attempts"
                    print(f"IP rotation failed: {err}")
                    # Force add to history even though IP is same (to show failed rotation attempt)
                    update_ip_history(new_ip, force_add=True, is_failure=True)
                    send_discord_notification(current_ip, previous_ip, is_rotation=False, is_failure=True, error_message=err)
                    return jsonify({
                        'status': 'failed',
                        'error': err,
                        'public_ip': new_ip,
                        'previous_ip': previous_ip,
                        'attempts': max_attempts
                    }), 400
        else:
            # Fallback to PPP rotation
            print("No RNDIS interface found, using PPP fallback")
            rotation_config = config.get('rotation', {}) or {}
            teardown_wait = int(rotation_config.get('ppp_teardown_wait', 30))
            restart_wait  = int(rotation_config.get('ppp_restart_wait', 60))
            max_attempts  = int(rotation_config.get('max_attempts', 2))

            deep_enabled = rotation_config.get('deep_reset_enabled', False)
            deep_method  = (rotation_config.get('deep_reset_method', 'mmcli') or 'mmcli').lower()
            if 'deep_reset' in rotation_config:  # backward compat
                old = (rotation_config.get('deep_reset', '') or '').lower()
                if old in ('mmcli', 'at'):
                    deep_enabled, deep_method = True, old
                elif old in ('off', ''):
                    deep_enabled = False
            deep_wait = int(rotation_config.get('deep_reset_wait', 180))

            print(f"PPP rotation config: teardown_wait={teardown_wait}s, restart_wait={restart_wait}s, "
                  f"max_attempts={max_attempts}, deep_reset={'enabled' if deep_enabled else 'disabled'} ({deep_method}, {deep_wait}s)")

            for attempt in range(max_attempts):
                print(f"\n--- PPP Rotation Attempt {attempt + 1}/{max_attempts} ---")
                teardown_ppp(teardown_wait)

                if attempt == 0:
                    print("Attempt 1: Simple PPP restart")
                else:
                    if deep_enabled:
                        print(f"Attempt {attempt + 1}: Deep reset ({deep_method}) before PPP")
                        deep_reset_modem(deep_method, deep_wait)
                        print("Waiting up to 15s for modem ports to re-enumerate…")
                        t0 = time.time()
                        while time.time() - t0 < 15:
                            if any(n.startswith("ttyUSB") for n in os.listdir("/dev")):
                                break
                            time.sleep(1)
                    else:
                        print(f"Attempt {attempt + 1}: Deep reset disabled; trying PPP restart again")

                try:
                    start_ppp()
                except Exception as e:
                    print(f"PPP restart failed on attempt {attempt + 1}: {e}")
                    if attempt == max_attempts - 1:
                        err = f"PPP restart failed after {max_attempts} attempts"
                        print(f"IP rotation failed: {err}")
                        send_discord_notification(current_ip, previous_ip, is_rotation=False, is_failure=True, error_message=err)
                        return jsonify({'status':'failed','error':err,'public_ip':current_ip,'previous_ip':previous_ip}), 500
                    continue

                extra = 0
                if attempt > 0:
                    extra = max(30, restart_wait)
                total_wait = restart_wait + extra
                print(f"Waiting {total_wait} seconds for new IP assignment...")
                if not wait_for_ppp_up(total_wait):
                    print(f"ppp0 interface not up after attempt {attempt + 1}")
                    if attempt == max_attempts - 1:
                        err = "ppp0 interface not up after restart"
                        print(f"IP rotation failed: {err}")
                        send_discord_notification(current_ip, previous_ip, is_rotation=False, is_failure=True, error_message=err)
                        return jsonify({'status':'failed','error':err,'public_ip':current_ip,'previous_ip':previous_ip}), 500
                    continue

                print("Fixing routing to prefer primary and keep PPP as secondary...")
                ensure_ppp_default_route()
                
                # Re-apply policy routing after PPP restart
                print("Re-applying policy routing for Squid...")
                try:
                    # Ensure PPP routing table exists and has default route
                    subprocess.run([IP_PATH, "route", "replace", "default", "dev", "ppp0", "table", "ppp"], check=False)
                    
                    # Re-apply policy rule for marked packets
                    subprocess.run(["ip", "rule", "del", "fwmark", "0x1", "lookup", "ppp"], check=False)
                    subprocess.run(["ip", "rule", "add", "fwmark", "0x1", "lookup", "ppp", "priority", "1000"], check=False)
                    
                    # Re-apply packet marking rule for proxy user
                    subprocess.run([
                        "iptables", "-t", "mangle", "-D", "OUTPUT", 
                        "-m", "owner", "--uid-owner", "proxy", 
                        "-j", "MARK", "--set-mark", "1"
                    ], check=False)
                    subprocess.run([
                        "iptables", "-t", "mangle", "-A", "OUTPUT", 
                        "-m", "owner", "--uid-owner", "proxy", 
                        "-j", "MARK", "--set-mark", "1"
                    ], check=False)
                    print("✅ Policy routing re-applied")
                except Exception as e:
                    print(f"⚠️ Policy routing re-application failed: {e}")

                # Check if we got a new IP
                new_ip = get_current_ip()
                pdp = at('AT+CGPADDR') if new_ip != "Unknown" else ""

                if new_ip != previous_ip and new_ip != "Unknown":
                    # Success! New IP obtained
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
                        print("Trying next attempt with escalation…")
                        continue
                    err = f"IP did not change after {max_attempts} attempts"
                    print(f"IP rotation failed: {err}")
                    send_discord_notification(current_ip, previous_ip, is_rotation=False, is_failure=True, error_message=err)
                    return jsonify({
                        'status': 'failed',
                        'error': err,
                        'pdp': pdp,
                        'public_ip': new_ip,
                        'previous_ip': previous_ip,
                        'attempts': max_attempts
                    }), 400

    except Exception as e:
        err = f"Rotation failed: {str(e)}"
        print(f"IP rotation failed: {err}")
        try:
            current_ip = get_current_ip()
        except Exception:
            current_ip = "Unknown"
        send_discord_notification(current_ip, None, is_rotation=False, is_failure=True, error_message=err)
        return jsonify({'status':'failed','error':err}), 500
    finally:
        in_progress = False
        rotate_lock.release()

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
    return jsonify(load_ip_history())

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

@app.get('/auto-rotation/status')
def auto_rotation_status():
    """Get auto-rotation status and settings."""
    config = load_config()
    interval = config.get('pm2', {}).get('ip_rotation_interval', 300)
    
    return jsonify({
        'enabled': auto_rotation_enabled,
        'interval_seconds': interval,
        'interval_minutes': interval // 60,
        'thread_alive': auto_rotation_thread and auto_rotation_thread.is_alive()
    })

@app.post('/auto-rotation/enable')
def auto_rotation_enable():
    """Enable auto-rotation."""
    config = load_config()
    expected = config['api']['token']
    token = request.headers.get('Authorization', '')
    if expected not in token:
        abort(403)
    
    set_auto_rotation_enabled(True)
    return jsonify({'status': 'enabled', 'message': 'Auto-rotation enabled'})

@app.post('/auto-rotation/disable')
def auto_rotation_disable():
    """Disable auto-rotation."""
    config = load_config()
    expected = config['api']['token']
    token = request.headers.get('Authorization', '')
    if expected not in token:
        abort(403)
    
    set_auto_rotation_enabled(False)
    return jsonify({'status': 'disabled', 'message': 'Auto-rotation disabled'})

@app.post('/auto-rotation/restart')
def auto_rotation_restart():
    """Restart auto-rotation thread."""
    config = load_config()
    expected = config['api']['token']
    token = request.headers.get('Authorization', '')
    if expected not in token:
        abort(403)
    
    stop_auto_rotation()
    time.sleep(1)
    start_auto_rotation()
    return jsonify({'status': 'restarted', 'message': 'Auto-rotation thread restarted'})

if __name__ == '__main__':
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
    print("⚙️ Auto-rotation Control:")
    print("   Status: GET http://127.0.0.1:8088/auto-rotation/status")
    print("   Enable: POST http://127.0.0.1:8088/auto-rotation/enable")
    print("   Disable: POST http://127.0.0.1:8088/auto-rotation/disable")
    print("   Restart: POST http://127.0.0.1:8088/auto-rotation/restart")

    webhook_url = config.get('discord', {}).get('webhook_url', '').strip()
    if webhook_url and webhook_url != "https://discord.com/api/webhooks/YOUR_WEBHOOK_ID/YOUR_TOKEN":
        print("📱 Discord notifications: Enabled")
        current_ip = get_current_ip()
        send_discord_notification(current_ip, is_rotation=True)
    else:
        print("📱 Discord notifications: Not configured")
    
    # Start auto-rotation
    interval = config.get('pm2', {}).get('ip_rotation_interval', 300)
    print(f"🔄 Auto-rotation: Starting with {interval//60} minute intervals")
    start_auto_rotation()
    
    print("="*60)

    app.run(host=config['api']['bind'], port=config['api']['port'])
