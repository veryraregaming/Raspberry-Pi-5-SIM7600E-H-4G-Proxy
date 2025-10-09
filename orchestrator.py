#!/usr/bin/env python3
import os
import sys
import time
import requests
import serial
import yaml
import json
import subprocess
import threading
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
ORIGINAL_IMEI_PATH = STATE_DIR / "original_imei.txt"

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
            if p == "via" and i+1 < len(parts):
                gw = parts[i+1]
            if p == "dev" and i+1 < len(parts):
                dev = parts[i+1]
            if p == "metric" and i+1 < len(parts):
                try:
                    metric = int(parts[i+1])
                except Exception:
                    pass
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

    # Check for QMI interface first
    qmi_iface, qmi_has_ip = detect_qmi_interface()

    # If QMI is down, check for RNDIS
    if not (qmi_iface and qmi_has_ip):
        rndis_iface, rndis_has_ip = detect_rndis_interface()

        # If RNDIS is also down, check for PPP
        if not (rndis_iface and rndis_has_ip):
            try:
                r = subprocess.run([IP_PATH, "-4", "addr", "show", "ppp0"],
                                   capture_output=True, text=True, timeout=3)
                if r.returncode != 0 or "inet " not in r.stdout:
                    # No cellular interface is up
                    return "No cellular connection"
            except Exception:
                return "No cellular connection"

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
        r = requests.get('https://api.ipify.org', proxies=proxies, timeout=8)
        ip = r.text.strip()

        # Check if this looks like a valid public IP
        if not ip or len(ip) < 7:
            return "Unknown - proxy failed"
        
        # If we get a private IP, it means proxy is routing through WiFi/LAN
        # This indicates the cellular routing is broken
        if (
            ip.startswith('192.168.')
            or ip.startswith('10.')
            or ip.startswith('172.')
        ):
            # Check if it's the user's home IP (EE hub)
            if ip.startswith('86.151.'):
                return f"⚠️ WiFi IP: {ip} (cellular routing broken!)"
            return f"⚠️ LAN IP: {ip} (cellular routing broken!)"

        # Valid cellular IP
        return ip
        
    except requests.exceptions.Timeout:
        return "Proxy timeout (check cellular connection)"
    except requests.exceptions.ConnectionError:
        return "Proxy not responding (check Squid)"
    except Exception as e:
        return f"Error: {str(e)[:50]}"

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

    # Note: History update is now handled in rotation code before calling this function
    # This ensures the embed includes the latest entry (including failures)

    try:
        payload = build_discord_embed(current_ip, previous_ip, is_rotation, is_failure, error_message)
        action, msg_id = post_or_patch_discord(webhook_url, payload, MSG_ID_PATH)
        print(f"Discord notification {action} (ID: {msg_id})")
        return True
    except Exception as e:
        print(f"Failed to send Discord notification: {e}")
        return False

# ========= QMI helpers =========

def detect_qmi_interface():
    """Detect QMI interface (wwan*) that provides cellular connectivity."""
    try:
        ip_cmd = IP_PATH if IP_PATH and os.path.exists(IP_PATH) else "/usr/sbin/ip"

        result = subprocess.run([ip_cmd, "-br", "link", "show"],
                                capture_output=True, text=True, check=False, timeout=5)
        if result.returncode != 0:
            return None, False

        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith("wwan"):
                parts = line.split()
                if not parts:
                    continue
                iface = parts[0]

                # Check if interface has an IP address
                ip_result = subprocess.run([ip_cmd, "-4", "addr", "show", iface],
                                           capture_output=True, text=True, check=False, timeout=5)
                if ip_result.returncode != 0:
                    return iface, False

                if "inet " in ip_result.stdout:
                    return iface, True
                else:
                    return iface, False

    except Exception as e:
        # Only log exceptions during rotation or startup, not on every status check
        pass
    return None, False

def get_original_imei():
    """Get the original (factory) IMEI from state file."""
    try:
        if ORIGINAL_IMEI_PATH.exists():
            return ORIGINAL_IMEI_PATH.read_text(encoding="utf-8").strip()
        return None
    except Exception:
        return None

def save_original_imei(imei):
    """Save the original (factory) IMEI to state file."""
    try:
        if imei and imei != "Unknown" and len(imei) == 15:
            ORIGINAL_IMEI_PATH.write_text(imei, encoding="utf-8")
            print(f"  💾 Saved original IMEI: {imei}")
            return True
    except Exception as e:
        print(f"  ⚠️ Could not save original IMEI: {e}")
    return False

def get_current_imei():
    """Get current IMEI from modem."""
    try:
        # Find the modem control device
        modem_dev = "/dev/ttyUSB2"  # Default for SIM7600E-H
        if not os.path.exists(modem_dev):
            modem_dev = "/dev/ttyUSB0"
        
        if not os.path.exists(modem_dev):
            return "Unknown"
        
        # Try AT+GSN command (standard IMEI query)
        response = at("AT+GSN", port=modem_dev, read_delay=1.0, timeout=2)
        if response:
            # Parse IMEI from response (usually just the IMEI number)
            lines = response.strip().split('\n')
            for line in lines:
                line = line.strip()
                # IMEI is typically 15 digits
                if line and line.isdigit() and len(line) == 15:
                    # Save as original if not already saved
                    if not get_original_imei():
                        save_original_imei(line)
                    return line
                # Sometimes it's in format +GSN: XXXXXX
                if '+GSN:' in line:
                    imei = line.split(':')[1].strip()
                    if imei.isdigit() and len(imei) == 15:
                        # Save as original if not already saved
                        if not get_original_imei():
                            save_original_imei(imei)
                        return imei
        
        # Fallback: try AT+CGSN
        response = at("AT+CGSN", port=modem_dev, read_delay=1.0, timeout=2)
        if response:
            lines = response.strip().split('\n')
            for line in lines:
                line = line.strip()
                if line and line.isdigit() and len(line) == 15:
                    # Save as original if not already saved
                    if not get_original_imei():
                        save_original_imei(line)
                    return line
        
        return "Unknown"
    except Exception as e:
        print(f"Error getting IMEI: {e}")
        return "Unknown"

def randomise_imei():
    """Generate and set a random IMEI to help get different IPs."""
    try:
        import random
        
        # Generate random IMEI: 35000000 + 8 random digits
        random_suffix = random.randint(10000000, 99999999)
        random_imei = f"35000000{random_suffix}"
        
        print(f"📱 Setting random IMEI: {random_imei}")
        
        # Find the modem control device
        modem_dev = "/dev/ttyUSB2"  # Default for SIM7600E-H
        if not os.path.exists(modem_dev):
            print(f"⚠️ Modem device {modem_dev} not found, trying /dev/ttyUSB0")
            modem_dev = "/dev/ttyUSB0"
        
        if not os.path.exists(modem_dev):
            print("⚠️ No modem control device found for IMEI change")
            return False
        
        with serial.Serial(modem_dev, 115200, timeout=5) as ser:
            # Try AT+EGMR command (works on some modems)
            ser.write(f'AT+EGMR=1,7,"{random_imei}"\r\n'.encode())
            time.sleep(2)
            response = ser.read_all().decode(errors='ignore')
            print(f"  📡 IMEI set response: {response.strip()}")
            
            # Check if command was successful
            if "ERROR" in response.upper():
                print(f"  ⚠️ AT+EGMR command not supported by this modem")
                print(f"  ℹ️  SIM7600E-H may not support IMEI changes via AT commands")
                print(f"  ℹ️  IMEI randomisation is disabled for this modem model")
                return False
            
            if "OK" not in response.upper():
                print(f"  ⚠️ IMEI change command returned unexpected response")
                return False
            
            # Reset modem to apply IMEI change
            print("  📡 Rebooting modem to apply new IMEI...")
            ser.write(b"AT+CFUN=1,1\r\n")
            time.sleep(2)
            ser.read_all()
        
        print("  ⏱️ Waiting 30 seconds for modem to reboot...")
        time.sleep(30)
        print("  ✅ IMEI randomisation complete")
        return True
        
    except Exception as e:
        print(f"  ⚠️ IMEI randomisation failed: {e}")
        return False

def deep_reset_qmi_modem(randomise_imei_enabled=False):
    """Deep reset modem using AT commands to force new IP."""
    try:
        print("🔄 Performing deep QMI modem reset (radio + PDP context)...")
        
        # Randomise IMEI first if enabled (before any other operations)
        if randomise_imei_enabled:
            print("📱 Step 1: Randomising IMEI...")
            if randomise_imei():
                print("  ✅ IMEI changed successfully, modem already rebooted")
                # Wait a bit more for modem to stabilise after reboot
                print("  ⏱️ Waiting 15 seconds for modem to stabilise...")
                time.sleep(15)
            else:
                print("  ⚠️ IMEI change failed, continuing with standard reset...")

        # Find the modem control device
        modem_dev = "/dev/ttyUSB2"  # Default for SIM7600E-H
        if not os.path.exists(modem_dev):
            print(f"⚠️ Modem device {modem_dev} not found, trying /dev/ttyUSB0")
            modem_dev = "/dev/ttyUSB0"

        if not os.path.exists(modem_dev):
            print("⚠️ No modem control device found")
            return False

        with serial.Serial(modem_dev, 115200, timeout=5) as ser:
            # 1. Deactivate PDP context
            print("📡 Deactivating PDP context...")
            ser.write(b"AT+CGACT=0,1\r\n")
            time.sleep(2)
            ser.read(1000)

            # 2. Detach from network
            print("📡 Detaching from network...")
            ser.write(b"AT+CGATT=0\r\n")
            time.sleep(2)
            ser.read(1000)

            # 3. Radio off
            print("📴 Radio off...")
            ser.write(b"AT+CFUN=0\r\n")
            time.sleep(5)
            ser.read(1000)

            # 4. Radio on
            print("📡 Radio on...")
            ser.write(b"AT+CFUN=1\r\n")
            time.sleep(5)
            ser.read(1000)

            # 5. Reattach to network
            print("📡 Reattaching to network...")
            ser.write(b"AT+CGATT=1\r\n")
            time.sleep(2)
            ser.read(1000)

            # 6. Reactivate PDP context
            print("📡 Reactivating PDP context...")
            ser.write(b"AT+CGACT=1,1\r\n")
            time.sleep(2)
            ser.read(1000)

        print("✅ Deep QMI modem reset complete")
        return True

    except Exception as e:
        print(f"⚠️ Deep reset failed: {e}")
        return False

def teardown_qmi(wait_s: int, deep_reset: bool = False, randomise_imei_enabled: bool = False):
    """Tear down QMI interface properly releasing IP."""
    iface, _ = detect_qmi_interface()
    if iface:
        print(f"Bringing down QMI interface: {iface}")

        # Stop QMI network connection (properly releases IP from carrier)
        print("  📡 Stopping QMI network (releasing IP)...")
        qmi_dev = "/dev/cdc-wdm0"
        subprocess.run([
            SUDO_PATH, "-n", "qmicli", "-d", qmi_dev,
            "--wds-stop-network", "disable-autoconnect",
            "--client-no-release-cid"
        ], capture_output=True, text=True, check=False, timeout=10)
        time.sleep(2)

        # Bring interface down
        subprocess.run([SUDO_PATH, "-n", IP_PATH, "link", "set", "dev", iface, "down"],
                       capture_output=True, text=True, check=False)

        # Perform deep reset if requested
        if deep_reset:
            deep_reset_qmi_modem(randomise_imei_enabled=randomise_imei_enabled)
            wait_s = max(wait_s, 30)  # Add extra wait after deep reset

        print(f"Waiting {wait_s} seconds for QMI teardown...")
        time.sleep(wait_s)
    else:
        print("No QMI interface found, skipping teardown")

def start_qmi():
    """Start QMI interface with proper connection and DHCP."""
    iface, has_ip = detect_qmi_interface()
    if not iface:
        raise RuntimeError("No QMI interface found")

    print(f"Starting QMI connection for: {iface}")

    # Bring interface up
    res = subprocess.run([SUDO_PATH, "-n", IP_PATH, "link", "set", "dev", iface, "up"],
                         capture_output=True, text=True, check=False)
    if res.returncode != 0:
        raise RuntimeError(f"Failed to bring up {iface}: {res.stderr.strip()}")

    time.sleep(2)

    # Get APN from config
    config = load_config()
    apn = config.get('modem', {}).get('apn', 'everywhere')

    # Start QMI network connection (get fresh IP from carrier)
    print(f"  📡 Starting QMI network with APN: {apn} (getting new IP)...")
    qmi_dev = "/dev/cdc-wdm0"
    res = subprocess.run([
        SUDO_PATH, "-n", "qmicli", "-d", qmi_dev,
        "--wds-start-network", f"apn={apn}",
        "--client-no-release-cid"
    ], capture_output=True, text=True, timeout=30, check=False)

    if res.returncode != 0:
        raise RuntimeError(f"QMI network start failed: {res.stderr.strip()}")

    time.sleep(3)

    # Use udhcpc to get IP from modem (renew DHCP)
    print(f"  📡 Getting IP via DHCP for {iface}...")
    res = subprocess.run([SUDO_PATH, "-n", "udhcpc", "-i", iface, "-q"],
                         capture_output=True, text=True, timeout=10, check=False)

    if res.returncode != 0:
        print(f"  ⚠️ udhcpc warning: {res.stderr.strip()}")

    return iface

def wait_for_qmi_up(timeout_s: int) -> bool:
    """Wait for QMI interface to get an IP address."""
    deadline = time.time() + max(5, int(timeout_s))
    while time.time() < deadline:
        iface, has_ip = detect_qmi_interface()
        if iface and has_ip:
            return True
        time.sleep(2)
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
            return None, False

        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith("enx") or line.startswith("eth1"):
                parts = line.split()
                if not parts:
                    continue
                iface = parts[0]

                # Check if interface has an IP address
                ip_result = subprocess.run([ip_cmd, "-4", "addr", "show", iface],
                                           capture_output=True, text=True, check=False, timeout=5)
                if ip_result.returncode != 0:
                    return iface, False

                if "inet " in ip_result.stdout:
                    return iface, True
                else:
                    return iface, False

    except Exception as e:
        # Only log exceptions during rotation or startup, not on every status check
        pass
    return None, False

def smart_ip_rotation_rndis_modem(randomise_imei_enabled=False, wait_seconds=30):
    """Smart IP rotation using network mode switching and APN cycling - much faster than full reset."""
    print("🔄 Performing smart IP rotation (network mode + APN cycling)...")
    try:
        # Randomise IMEI first if enabled (before any other operations)
        if randomise_imei_enabled:
            print("📱 Step 1: Randomising IMEI...")
            if randomise_imei():
                print("  ✅ IMEI changed successfully, modem already rebooted")
                time.sleep(15)
            else:
                print("  ⚠️ IMEI change failed, continuing with smart rotation...")
        
        at_port = detect_modem_port()
        if not at_port or not os.path.exists(at_port):
            print(f"⚠️ Smart rotation skipped: AT port {at_port} not available")
            return False

        with serial.Serial(at_port, 115200, timeout=5) as ser:
            # Step 1: Deactivate PDP context (gentle disconnect)
            print("  📡 Deactivating PDP context...")
            ser.write(b"AT+CGACT=0,1\r\n")
            time.sleep(2)
            ser.read_all()

            # Step 2: Switch network mode (4G -> 3G -> 4G for new IP)
            print("  📡 Switching to 3G mode...")
            ser.write(b"AT+CNMP=14\r\n")  # 3G only
            time.sleep(3)
            ser.read_all()

            # Step 3: Wait for network to register on 3G
            print("  ⏱️ Waiting for 3G registration...")
            time.sleep(5)
            
            # Step 4: Switch back to 4G mode
            print("  📡 Switching back to 4G mode...")
            ser.write(b"AT+CNMP=38\r\n")  # 4G only
            time.sleep(3)
            ser.read_all()

            # Step 5: Wait for network to re-register on 4G
            print("  ⏱️ Waiting for 4G re-registration...")
            time.sleep(5)

            # Step 6: Try APN cycling (everywhere -> eesecure -> everywhere)
            print("  📡 Cycling APN for fresh IP...")
            ser.write(b'AT+CGDCONT=1,"IP","eesecure"\r\n')  # Switch to eesecure
            time.sleep(2)
            ser.read_all()
            
            ser.write(b'AT+CGDCONT=1,"IP","everywhere"\r\n')  # Back to everywhere
            time.sleep(2)
            ser.read_all()

            # Step 7: Reactivate PDP context with new settings
            print("  📡 Reactivating PDP context...")
            ser.write(b"AT+CGACT=1,1\r\n")
            time.sleep(3)
            ser.read_all()

        print("  ✅ Smart IP rotation complete")
        return True
    except Exception as e:
        print(f"  ⚠️ Smart rotation failed: {e}")
        return False

def deep_reset_rndis_modem(randomise_imei_enabled=False, wait_seconds=60):
    """Fallback deep reset - only used if smart rotation fails multiple times."""
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
            ser.read_all()

            # Detach from network
            print("  📡 Detaching from network...")
            ser.write(b"AT+CGATT=0\r\n")
            time.sleep(2)
            ser.read_all()
            
            # Deregister from network
            print("  ✈️ Deregistering from network...")
            ser.write(b"AT+COPS=2\r\n")
            time.sleep(3)
            ser.read_all()

            # Airplane mode
            print("  ✈️ Airplane mode...")
            ser.write(b"AT+CFUN=4\r\n")
            time.sleep(3)
            ser.read_all()

            # Wait in airplane mode
            print(f"  ⏱️ Wait in airplane mode ({wait_seconds}s)...")
            time.sleep(wait_seconds)

            # Radio back on
            print("  📡 Radio back on...")
            ser.write(b"AT+CFUN=1\r\n")
            time.sleep(8)
            ser.read_all()
            
            # Auto-register
            print("  📡 Auto-registering...")
            ser.write(b"AT+COPS=0\r\n")
            time.sleep(5)
            ser.read_all()

            # Reattach
            print("  📡 Reattaching...")
            ser.write(b"AT+CGATT=1\r\n")
            time.sleep(2)
            ser.read_all()

            # Reactivate
            print("  📡 Reactivating PDP...")
            ser.write(b"AT+CGACT=1,1\r\n")
            time.sleep(2)
            ser.read_all()

        print("  ✅ Deep modem reset complete")
        return True
    except Exception as e:
        print(f"  ⚠️ Deep modem reset failed: {e}")
        return False

def teardown_rndis(wait_s: int, deep_reset: bool = False, randomise_imei_enabled: bool = False, deep_reset_wait: int = 60):
    """Teardown RNDIS interface by bringing it down."""
    iface, _ = detect_rndis_interface()
    if iface:
        print(f"Bringing down RNDIS interface: {iface}")
        subprocess.run([SUDO_PATH, "-n", IP_PATH, "link", "set", "dev", iface, "down"],
                       check=False)

        if deep_reset:
            # Try smart rotation first (faster, less disruptive)
            print("  🔄 Trying smart IP rotation first...")
            smart_success = smart_ip_rotation_rndis_modem(randomise_imei_enabled=randomise_imei_enabled, wait_seconds=30)
            
            if not smart_success:
                print("  ⚠️ Smart rotation failed, falling back to deep reset...")
                deep_reset_rndis_modem(randomise_imei_enabled=randomise_imei_enabled, wait_seconds=deep_reset_wait)
            
            print(f"Waiting {wait_s} seconds after rotation...")
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

                        # Get configured modem mode
                        modem_mode = config.get('modem', {}).get('mode', 'auto')

                        # Check available interfaces
                        qmi_iface, qmi_has_ip = detect_qmi_interface()
                        rndis_iface, rndis_has_ip = detect_rndis_interface()

                        # Determine which mode to use
                        use_qmi = False
                        use_rndis = False
                        if modem_mode == "qmi":
                            use_qmi = True
                        elif modem_mode == "rndis":
                            use_rndis = True
                        elif modem_mode == "ppp":
                            use_qmi = False
                            use_rndis = False
                        elif modem_mode == "auto":
                            # Auto priority: QMI > RNDIS > PPP
                            if qmi_iface:
                                use_qmi = True
                            elif rndis_iface:
                                use_rndis = True

                        if use_qmi and qmi_iface:
                            print(f"Auto-rotation: Using QMI interface: {qmi_iface}")
                            rotation_config = config.get('rotation', {}) or {}
                            teardown_wait = int(rotation_config.get('ppp_teardown_wait', 30))
                            restart_wait  = int(rotation_config.get('ppp_restart_wait', 60))
                            max_attempts  = int(rotation_config.get('max_attempts', 2))
                            randomise_imei_enabled = rotation_config.get('randomise_imei', False)

                            for attempt in range(max_attempts):
                                print(f"Auto-rotation: QMI Rotation Attempt {attempt + 1}/{max_attempts}")

                                # Always use deep reset for better IP variety with sticky CGNAT
                                use_deep_reset = True
                                print(f"Auto-rotation: Using deep reset for better IP variety")

                                teardown_qmi(teardown_wait, deep_reset=use_deep_reset, randomise_imei_enabled=randomise_imei_enabled)

                                try:
                                    start_qmi()
                                except Exception as e:
                                    print(f"Auto-rotation: QMI restart failed on attempt {attempt + 1}: {e}")
                                    if attempt == max_attempts - 1:
                                        err = f"QMI restart failed after {max_attempts} attempts"
                                        print(f"Auto-rotation failed: {err}")
                                        send_discord_notification(current_ip, previous_ip, is_rotation=False, is_failure=True, error_message=err)
                                        continue
                                    continue

                                if not wait_for_qmi_up(restart_wait):
                                    print(f"Auto-rotation: QMI interface did not come up within {restart_wait} seconds")
                                    if attempt == max_attempts - 1:
                                        err = f"QMI interface failed to get IP after {max_attempts} attempts"
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

                        elif use_rndis and rndis_iface:
                            print(f"Auto-rotation: Using RNDIS interface: {rndis_iface}")
                            rotation_config = config.get('rotation', {}) or {}
                            teardown_wait = int(rotation_config.get('ppp_teardown_wait', 30))
                            restart_wait  = int(rotation_config.get('ppp_restart_wait', 60))
                            max_attempts  = int(rotation_config.get('max_attempts', 2))
                            randomise_imei_enabled = rotation_config.get('randomise_imei', False)
                            deep_reset_wait = int(rotation_config.get('deep_reset_wait', 60))

                            for attempt in range(max_attempts):
                                print(f"Auto-rotation: RNDIS Rotation Attempt {attempt + 1}/{max_attempts}")

                                # Always use deep reset for better IP variety with sticky CGNAT
                                use_deep_reset = True
                                print(f"Auto-rotation: Using deep reset for better IP variety")

                                teardown_rndis(teardown_wait, deep_reset=use_deep_reset, randomise_imei_enabled=randomise_imei_enabled, deep_reset_wait=deep_reset_wait)

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
                            print("Auto-rotation: No QMI/RNDIS interfaces found, trying PPP fallback...")
                            # PPP fallback logic (similar to manual rotation)
                            rotation_config = config.get('rotation', {}) or {}
                            teardown_wait = int(rotation_config.get('ppp_teardown_wait', 30))
                            restart_wait = int(rotation_config.get('ppp_restart_wait', 60))
                            max_attempts = int(rotation_config.get('max_attempts', 2))

                            for attempt in range(max_attempts):
                                print(f"Auto-rotation: PPP Rotation Attempt {attempt + 1}/{max_attempts}")
                                
                                teardown_ppp(teardown_wait)
                                
                                try:
                                    start_ppp()
                                except Exception as e:
                                    print(f"Auto-rotation: PPP restart failed on attempt {attempt + 1}: {e}")
                                    if attempt == max_attempts - 1:
                                        err = f"PPP restart failed after {max_attempts} attempts"
                                        print(f"Auto-rotation failed: {err}")
                                        send_discord_notification(current_ip, previous_ip, is_rotation=False, is_failure=True, error_message=err)
                                        break
                                    continue

                                if not wait_for_ppp_up(restart_wait):
                                    print(f"Auto-rotation: PPP interface did not come up within {restart_wait} seconds")
                                    if attempt == max_attempts - 1:
                                        err = f"PPP interface failed to get IP after {max_attempts} attempts"
                                        print(f"Auto-rotation failed: {err}")
                                        send_discord_notification(current_ip, previous_ip, is_rotation=False, is_failure=True, error_message=err)
                                        break
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
        auto_rotation_thread = threading.Thread(target=auto_rotation_worker, daemon=False)
        auto_rotation_thread.start()
        print("✅ Auto-rotation thread started")
        print(f"   Thread ID: {auto_rotation_thread.ident}")
        print(f"   Thread alive: {auto_rotation_thread.is_alive()}")

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
    # Fast status check with actual IP detection
    pdp = "N/A"  # Skip slow AT command
    
    # Detect which connection mode is active
    connection_mode = "Unknown"
    interface_name = "Unknown"
    up = False

    try:
        # Check QMI first
        qmi_iface, qmi_has_ip = detect_qmi_interface()
        if qmi_iface and qmi_has_ip:
            connection_mode = "QMI"
            interface_name = qmi_iface
            up = True
        else:
            # Check RNDIS
            rndis_iface, rndis_has_ip = detect_rndis_interface()
            if rndis_iface and rndis_has_ip:
                connection_mode = "RNDIS"
                interface_name = rndis_iface
                up = True
            else:
                # Check PPP
                r = subprocess.run([IP_PATH, "-4", "addr", "show", "ppp0"],
                                   capture_output=True, text=True, timeout=3)
                if r.returncode == 0 and "inet " in r.stdout:
                    connection_mode = "PPP"
                    interface_name = "ppp0"
                    up = True
    except Exception:
        pass

    # Get public IP (with timeout to avoid hanging)
    try:
        pub = get_current_ip()
    except Exception as e:
        pub = f"Error: {str(e)[:30]}"

    # Skip slow IMEI checks for fast status
    current_imei = "N/A"
    original_imei = "N/A"
    imei_spoofed = False

    return jsonify({
        'pdp': pdp,
        'public_ip': pub,
        'ppp_up': up,  # Keep for backwards compatibility
        'connection_mode': connection_mode,
        'interface': interface_name,
        'connected': up,
        'imei': {
            'current': current_imei,
            'original': original_imei,
            'spoofed': imei_spoofed
        }
    })

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

        # Get configured modem mode
        modem_mode = config.get('modem', {}).get('mode', 'auto')
        print(f"Modem mode: {modem_mode}")

        # Check available interfaces
        qmi_iface, qmi_has_ip = detect_qmi_interface()
        rndis_iface, rndis_has_ip = detect_rndis_interface()

        # Determine which mode to use based on config and availability
        use_qmi = False
        use_rndis = False

        if modem_mode == "qmi":
            use_qmi = True
        elif modem_mode == "rndis":
            use_rndis = True
        elif modem_mode == "ppp":
            use_qmi = False
            use_rndis = False
        elif modem_mode == "auto":
            # Auto priority: QMI > RNDIS > PPP
            if qmi_iface:
                use_qmi = True
            elif rndis_iface:
                use_rndis = True

        # ===== QMI path =====
        if use_qmi and qmi_iface:
            print(f"Using QMI interface: {qmi_iface}")
            rotation_config = config.get('rotation', {}) or {}
            teardown_wait = int(rotation_config.get('ppp_teardown_wait', 30))
            restart_wait  = int(rotation_config.get('ppp_restart_wait', 60))
            max_attempts  = int(rotation_config.get('max_attempts', 2))
            randomise_imei_enabled = rotation_config.get('randomise_imei', False)

            print(f"QMI rotation config: teardown_wait={teardown_wait}s, restart_wait={restart_wait}s, max_attempts={max_attempts}, randomise_imei={'enabled' if randomise_imei_enabled else 'disabled'}")

            for attempt in range(max_attempts):
                print(f"\n--- QMI Rotation Attempt {attempt + 1}/{max_attempts} ---")

                # Always use deep reset for better IP variety with sticky CGNAT
                use_deep_reset = True
                print(f"Using deep reset for better IP variety (sticky CGNAT workaround)")

                teardown_qmi(teardown_wait, deep_reset=use_deep_reset, randomise_imei_enabled=randomise_imei_enabled)

                try:
                    start_qmi()
                except Exception as e:
                    print(f"QMI restart failed on attempt {attempt + 1}: {e}")
                    if attempt == max_attempts - 1:
                        err = f"QMI restart failed after {max_attempts} attempts"
                        print(f"IP rotation failed: {err}")
                        send_discord_notification(current_ip, previous_ip, is_rotation=False, is_failure=True, error_message=err)
                        return jsonify({'status': 'failed', 'error': err, 'public_ip': current_ip, 'previous_ip': previous_ip}), 500
                    continue

                total_wait = restart_wait
                print(f"Waiting {total_wait} seconds for new IP assignment...")
                if not wait_for_qmi_up(total_wait):
                    print(f"QMI interface did not come up within {total_wait} seconds")
                    if attempt == max_attempts - 1:
                        err = f"QMI interface failed to get IP after {max_attempts} attempts"
                        print(f"IP rotation failed: {err}")
                        send_discord_notification(current_ip, previous_ip, is_rotation=False, is_failure=True, error_message=err)
                        return jsonify({'status': 'failed', 'error': err, 'public_ip': current_ip, 'previous_ip': previous_ip}), 500
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

        # ===== RNDIS path =====
        elif use_rndis and rndis_iface:
            print(f"Using RNDIS interface: {rndis_iface}")
            rotation_config = config.get('rotation', {}) or {}
            teardown_wait = int(rotation_config.get('ppp_teardown_wait', 30))
            restart_wait  = int(rotation_config.get('ppp_restart_wait', 60))
            max_attempts  = int(rotation_config.get('max_attempts', 2))
            randomise_imei_enabled = rotation_config.get('randomise_imei', False)
            deep_reset_wait = int(rotation_config.get('deep_reset_wait', 60))

            print(f"RNDIS rotation config: teardown_wait={teardown_wait}s, restart_wait={restart_wait}s, max_attempts={max_attempts}, deep_reset_wait={deep_reset_wait}s, randomise_imei={'enabled' if randomise_imei_enabled else 'disabled'}")

            for attempt in range(max_attempts):
                print(f"\n--- RNDIS Rotation Attempt {attempt + 1}/{max_attempts} ---")

                # Always use deep reset for better IP variety with sticky CGNAT
                use_deep_reset = True
                print(f"Using deep reset for better IP variety (sticky CGNAT workaround)")

                teardown_rndis(teardown_wait, deep_reset=use_deep_reset, randomise_imei_enabled=randomise_imei_enabled, deep_reset_wait=deep_reset_wait)

                try:
                    start_rndis()
                except Exception as e:
                    print(f"RNDIS restart failed on attempt {attempt + 1}: {e}")
                    if attempt == max_attempts - 1:
                        err = f"RNDIS restart failed after {max_attempts} attempts"
                        print(f"IP rotation failed: {err}")
                        send_discord_notification(current_ip, previous_ip, is_rotation=False, is_failure=True, error_message=err)
                        return jsonify({'status': 'failed', 'error': err, 'public_ip': current_ip, 'previous_ip': previous_ip}), 500
                    continue

                total_wait = restart_wait
                print(f"Waiting {total_wait} seconds for new IP assignment...")
                if not wait_for_rndis_up(total_wait):
                    print(f"RNDIS interface did not come up within {total_wait} seconds")
                    if attempt == max_attempts - 1:
                        err = f"RNDIS interface failed to get IP after {max_attempts} attempts"
                        print(f"IP rotation failed: {err}")
                        send_discord_notification(current_ip, previous_ip, is_rotation=False, is_failure=True, error_message=err)
                        return jsonify({'status': 'failed', 'error': err, 'public_ip': current_ip, 'previous_ip': previous_ip}), 500
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

        # ===== PPP fallback =====
        else:
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

            print(
                f"PPP rotation config: teardown_wait={teardown_wait}s, restart_wait={restart_wait}s, "
                f"max_attempts={max_attempts}, deep_reset={'enabled' if deep_enabled else 'disabled'} ({deep_method}, {deep_wait}s)"
            )

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
                        return jsonify({'status': 'failed', 'error': err, 'public_ip': current_ip, 'previous_ip': previous_ip}), 500
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
                        return jsonify({'status': 'failed', 'error': err, 'public_ip': current_ip, 'previous_ip': previous_ip}), 500
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
        return jsonify({'status': 'failed', 'error': err}), 500
    finally:
        in_progress = False
        rotate_lock.release()

@app.get('/status/detailed')
def status_detailed():
    """Detailed status endpoint with slow operations (for when needed)."""
    pdp = at('AT+CGPADDR')
    pub = get_current_ip()
    current_imei = get_current_imei()
    original_imei = get_original_imei()
    
    imei_spoofed = False
    if original_imei and current_imei != "Unknown" and original_imei != current_imei:
        imei_spoofed = True

    return jsonify({
        'pdp': pdp,
        'public_ip': pub,
        'imei': {
            'current': current_imei,
            'original': original_imei or "Not recorded",
            'spoofed': imei_spoofed
        }
    })

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
        'thread_alive': bool(auto_rotation_thread and auto_rotation_thread.is_alive())
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

def run_optimization_in_background():
    """Run optimization in background thread after Flask starts."""
    # Wait for Flask to start
    print("⏱️ Waiting 10 seconds for orchestrator API to start...")
    time.sleep(10)
    
    print("\n" + "="*60)
    print("🎯 STARTING OPTIMIZATION IN BACKGROUND")
    print("="*60)
    print("The optimizer will run while the proxy continues operating.")
    print("This will take ~2 hours to find optimal settings.")
    print("="*60 + "\n")
    
    try:
        # Run the optimizer
        result = subprocess.run(
            [sys.executable, '-u', str(Path(__file__).parent / 'optimize_rotation.py'), '--auto'],
            cwd=Path(__file__).parent,
            env={**os.environ, 'PYTHONUNBUFFERED': '1'}
        )
        
        if result.returncode == 0:
            print("\n✅ Optimization complete! Settings applied and orchestrator will use them on next rotation.")
        else:
            print("\n⚠️ Optimization failed or was cancelled")
    except Exception as e:
        print(f"\n⚠️ Optimization error: {e}")

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
    
    # Check if optimization should run
    should_optimize = config.get('rotation', {}).get('run_optimization', False)
    if should_optimize:
        print("\n🎯 Optimization scheduled to run after API starts")
        print("   Starting optimization in background thread...")
        optimization_thread = threading.Thread(target=run_optimization_in_background, daemon=False)
        optimization_thread.start()
        print("   ✅ Optimization thread started (check logs for progress)")
        print()

    app.run(host=config['api']['bind'], port=config['api']['port'])
