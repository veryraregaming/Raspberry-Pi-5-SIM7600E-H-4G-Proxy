#!/usr/bin/env python3
"""
Web interface for 4G Proxy management
Provides a user-friendly dashboard for monitoring and controlling the proxy
"""

import os
import json
import requests
import yaml
from datetime import datetime
from pathlib import Path
from flask import Flask, render_template_string, request, jsonify, redirect, url_for

app = Flask(__name__)

# Configuration
CONFIG_FILE = Path(__file__).parent / "config.yaml"
STATE_DIR = Path(__file__).parent / "state"
IP_HISTORY_PATH = STATE_DIR / "ip_history.json"

def load_config():
    """Load configuration from config.yaml"""
    try:
        with open(CONFIG_FILE, 'r') as f:
            return yaml.safe_load(f)
    except:
        return {}

def get_api_token():
    """Get API token from config"""
    config = load_config()
    return config.get('api', {}).get('token', '')

def get_api_base_url():
    """Get API base URL"""
    config = load_config()
    return f"http://127.0.0.1:{config.get('api', {}).get('port', 8088)}"

def api_request(endpoint, method='GET', data=None):
    """Make authenticated API request"""
    token = get_api_token()
    url = f"{get_api_base_url()}{endpoint}"
    headers = {"Authorization": f"Bearer {token}"}
    
    try:
        if method == 'POST':
            response = requests.post(url, headers=headers, json=data, timeout=10)
        else:
            response = requests.get(url, headers=headers, timeout=10)
        
        print(f"API Request: {method} {url} -> {response.status_code}")
        if response.status_code == 200:
            return response.json()
        else:
            print(f"API Error: {response.status_code} - {response.text}")
            return None
    except Exception as e:
        print(f"API Request Failed: {e}")
        return None

def load_ip_history():
    """Load IP history"""
    try:
        if IP_HISTORY_PATH.exists():
            with open(IP_HISTORY_PATH, 'r') as f:
                return json.load(f)
    except:
        pass
    return {"ips": [], "rotations": 0, "first_seen": None}

# HTML Template
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>4G Proxy Dashboard</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { 
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            padding: 20px;
        }
        .container { 
            max-width: 1200px; 
            margin: 0 auto; 
            background: white; 
            border-radius: 15px; 
            box-shadow: 0 20px 40px rgba(0,0,0,0.1);
            overflow: hidden;
        }
        .header { 
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white; 
            padding: 30px; 
            text-align: center; 
        }
        .header h1 { font-size: 2.5em; margin-bottom: 10px; }
        .header p { opacity: 0.9; font-size: 1.1em; }
        .content { padding: 30px; }
        .grid { 
            display: grid; 
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); 
            gap: 20px; 
            margin-bottom: 30px;
        }
        .card { 
            background: #f8f9fa; 
            border-radius: 10px; 
            padding: 25px; 
            border-left: 4px solid #667eea;
            transition: transform 0.2s;
        }
        .card:hover { transform: translateY(-2px); }
        .card h3 { 
            color: #333; 
            margin-bottom: 15px; 
            font-size: 1.3em;
        }
        .status { 
            display: inline-block; 
            padding: 5px 15px; 
            border-radius: 20px; 
            font-weight: bold; 
            font-size: 0.9em;
        }
        .status.success { background: #d4edda; color: #155724; }
        .status.error { background: #f8d7da; color: #721c24; }
        .status.warning { background: #fff3cd; color: #856404; }
        .ip-display { 
            font-family: 'Courier New', monospace; 
            font-size: 1.2em; 
            font-weight: bold; 
            color: #667eea; 
            background: #f0f2ff; 
            padding: 10px; 
            border-radius: 5px; 
            margin: 10px 0;
        }
        .button { 
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white; 
            border: none; 
            padding: 12px 25px; 
            border-radius: 25px; 
            cursor: pointer; 
            font-size: 1em; 
            font-weight: bold;
            transition: all 0.2s;
            margin: 5px;
        }
        .button:hover { 
            transform: translateY(-2px); 
            box-shadow: 0 5px 15px rgba(102, 126, 234, 0.4);
        }
        .button:disabled { 
            opacity: 0.6; 
            cursor: not-allowed; 
            transform: none;
        }
        .button.danger { 
            background: linear-gradient(135deg, #e74c3c 0%, #c0392b 100%);
        }
        .button.success { 
            background: linear-gradient(135deg, #27ae60 0%, #229954 100%);
        }
        .history-item { 
            display: flex; 
            justify-content: space-between; 
            align-items: center; 
            padding: 10px; 
            border-bottom: 1px solid #eee; 
        }
        .history-item:last-child { border-bottom: none; }
        .history-ip { 
            font-family: 'Courier New', monospace; 
            font-weight: bold; 
            color: #667eea;
        }
        .history-time { 
            color: #666; 
            font-size: 0.9em;
        }
        .loading { 
            display: none; 
            text-align: center; 
            padding: 20px;
        }
        .spinner { 
            border: 3px solid #f3f3f3; 
            border-top: 3px solid #667eea; 
            border-radius: 50%; 
            width: 30px; 
            height: 30px; 
            animation: spin 1s linear infinite; 
            margin: 0 auto 10px;
        }
        @keyframes spin { 0% { transform: rotate(0deg); } 100% { transform: rotate(360deg); } }
        .alert { 
            padding: 15px; 
            margin: 20px 0; 
            border-radius: 5px; 
            display: none;
        }
        .alert.success { background: #d4edda; color: #155724; border: 1px solid #c3e6cb; }
        .alert.error { background: #f8d7da; color: #721c24; border: 1px solid #f5c6cb; }
        .footer { 
            text-align: center; 
            padding: 20px; 
            color: #666; 
            border-top: 1px solid #eee; 
            margin-top: 30px;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🌐 4G Proxy Dashboard</h1>
            <p>Monitor and control your SIM card proxy</p>
        </div>
        
        <div class="content">
            <div id="alert" class="alert"></div>
            
            <div class="grid">
                <!-- Current Status -->
                <div class="card">
                    <h3>📊 Current Status</h3>
                    <div id="proxy-status">
                        <div class="ip-display" id="current-ip">Loading...</div>
                        <div class="status" id="connection-status">Checking...</div>
                    </div>
                </div>
                
                <!-- Proxy Info -->
                <div class="card">
                    <h3>🔧 Proxy Configuration</h3>
                    <div id="proxy-info">
                        <p><strong>HTTP Proxy:</strong> <span id="proxy-url">Loading...</span></p>
                        <p><strong>API Endpoint:</strong> <span id="api-url">Loading...</span></p>
                        <p><strong>Status:</strong> <span class="status success">Active</span></p>
                    </div>
                </div>
                
                <!-- Statistics -->
                <div class="card">
                    <h3>📈 Statistics</h3>
                    <div id="statistics">
                        <p><strong>Total Rotations:</strong> <span id="rotation-count">-</span></p>
                        <p><strong>Uptime:</strong> <span id="uptime">-</span></p>
                        <p><strong>Last Rotation:</strong> <span id="last-rotation">-</span></p>
                    </div>
                </div>

                <!-- Configuration -->
                <div class="card">
                    <h3>⚙️ Configuration</h3>
                    <div id="configuration">
                        <p><strong>APN:</strong> <span id="config-apn">Loading...</span></p>
                        <p><strong>Rotation Timing:</strong> <span id="config-rotation">Loading...</span></p>
                        <p><strong>Discord:</strong> <span id="config-discord">Loading...</span></p>
                        <p><strong>Proxy Auth:</strong> <span id="config-auth">Loading...</span></p>
                    </div>
                </div>
                
                <!-- Controls -->
                <div class="card">
                    <h3>🎮 Controls</h3>
                    <div id="controls">
                        <button class="button success" onclick="rotateIP()">🔄 Rotate IP</button>
                        <button class="button" onclick="sendNotification()">📱 Send Notification</button>
                        <button class="button" onclick="refreshData()">🔄 Refresh</button>
                    </div>
                </div>
            </div>
            
            <!-- IP History -->
            <div class="card">
                <h3>📋 IP History</h3>
                <div id="ip-history">
                    <div class="loading" id="history-loading">
                        <div class="spinner"></div>
                        <p>Loading IP history...</p>
                    </div>
                    <div id="history-content"></div>
                </div>
            </div>
        </div>
        
        <div class="footer">
            <p>4G Mobile Proxy Server • Last updated: <span id="last-updated">-</span></p>
        </div>
    </div>

    <script>
        let refreshInterval;
        
        // Initialize dashboard
        document.addEventListener('DOMContentLoaded', function() {
            loadData();
            refreshInterval = setInterval(loadData, 30000); // Refresh every 30 seconds
        });
        
        async function loadData() {
            try {
                await Promise.all([
                    loadStatus(),
                    loadHistory(),
                    loadConfig()
                ]);
                document.getElementById('last-updated').textContent = new Date().toLocaleString();
            } catch (error) {
                showAlert('Error loading data: ' + error.message, 'error');
            }
        }
        
        async function loadStatus() {
            const response = await fetch('/api/status');
            const data = await response.json();
            
            document.getElementById('current-ip').textContent = data.public_ip || 'Unknown';
            document.getElementById('connection-status').textContent = data.public_ip ? 'Connected' : 'Disconnected';
            document.getElementById('connection-status').className = 'status ' + (data.public_ip ? 'success' : 'error');
        }
        
        async function loadHistory() {
            document.getElementById('history-loading').style.display = 'block';
            document.getElementById('history-content').innerHTML = '';
            
            const response = await fetch('/api/history');
            const data = await response.json();
            
            document.getElementById('rotation-count').textContent = data.rotations || 0;
            
            if (data.first_seen) {
                const firstSeen = new Date(data.first_seen);
                const uptime = Date.now() - firstSeen.getTime();
                const hours = Math.floor(uptime / (1000 * 60 * 60));
                const minutes = Math.floor((uptime % (1000 * 60 * 60)) / (1000 * 60));
                document.getElementById('uptime').textContent = `${hours}h ${minutes}m`;
            }
            
            if (data.ips && data.ips.length > 0) {
                const lastIP = data.ips[data.ips.length - 1];
                document.getElementById('last-rotation').textContent = lastIP.date + ' ' + lastIP.time;
                
                let historyHTML = '';
                data.ips.slice(-10).reverse().forEach(ip => {
                    historyHTML += `
                        <div class="history-item">
                            <span class="history-ip">${ip.ip}</span>
                            <span class="history-time">${ip.date} ${ip.time}</span>
                        </div>
                    `;
                });
                document.getElementById('history-content').innerHTML = historyHTML;
            } else {
                document.getElementById('history-content').innerHTML = '<p>No IP history available</p>';
            }
            
            document.getElementById('history-loading').style.display = 'none';
        }
        
        async function loadConfig() {
            try {
                const response = await fetch('/api/config');
                const data = await response.json();
                
                if (data.error) {
                    console.error('Config error:', data.error);
                    return;
                }

                // Update proxy info
                document.getElementById('proxy-url').textContent = `${data.lan_ip || 'Loading'}:3128`;
                document.getElementById('api-url').textContent = `127.0.0.1:${data.api_port || '8088'}`;
                
                // Update configuration display
                document.getElementById('config-apn').textContent = 'Auto-detected by run.sh';
                
                const rotation = data.rotation;
                if (rotation) {
                    const rotationText = `${rotation.ppp_teardown_wait}s + ${rotation.ppp_restart_wait}s (${rotation.max_attempts} attempts)`;
                    document.getElementById('config-rotation').textContent = rotationText;
                } else {
                    document.getElementById('config-rotation').textContent = 'N/A';
                }

                document.getElementById('config-discord').textContent = data.discord?.configured ? '✅ Configured' : '❌ Not configured';
                document.getElementById('config-auth').textContent = data.proxy?.auth_enabled ? '🔒 Enabled' : '🔓 Disabled';
            } catch (error) {
                console.error('Error fetching config:', error);
            }
        }
        
        async function rotateIP() {
            const button = event.target;
            button.disabled = true;
            button.textContent = '🔄 Rotating...';
            
            try {
                const response = await fetch('/api/rotate', { method: 'POST' });
                const data = await response.json();
                
                if (data.status === 'success') {
                    showAlert('IP rotation successful!', 'success');
                } else {
                    showAlert('IP rotation failed: ' + data.error, 'error');
                }
                
                await loadData(); // Refresh all data
            } catch (error) {
                showAlert('Error during rotation: ' + error.message, 'error');
            } finally {
                button.disabled = false;
                button.textContent = '🔄 Rotate IP';
            }
        }
        
        async function sendNotification() {
            const button = event.target;
            button.disabled = true;
            button.textContent = '📱 Sending...';
            
            try {
                const response = await fetch('/api/notify', { method: 'POST' });
                const data = await response.json();
                
                showAlert('Discord notification sent!', 'success');
            } catch (error) {
                showAlert('Error sending notification: ' + error.message, 'error');
            } finally {
                button.disabled = false;
                button.textContent = '📱 Send Notification';
            }
        }
        
        function refreshData() {
            loadData();
            showAlert('Data refreshed', 'success');
        }
        
        function showAlert(message, type) {
            const alert = document.getElementById('alert');
            alert.textContent = message;
            alert.className = 'alert ' + type;
            alert.style.display = 'block';
            
            setTimeout(() => {
                alert.style.display = 'none';
            }, 5000);
        }
    </script>
</body>
</html>
"""

@app.route('/')
def dashboard():
    """Main dashboard page"""
    return render_template_string(HTML_TEMPLATE)

@app.route('/api/status')
def api_status():
    """Get current proxy status"""
    data = api_request('/status')
    if data:
        return jsonify(data)
    
    # Try to get IP directly if API fails
    try:
        import subprocess
        result = subprocess.run(['curl', '-s', 'https://ipv4.icanhazip.com'], 
                              capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            ip = result.stdout.strip()
            return jsonify({'public_ip': ip, 'error': 'API unavailable, using direct IP check'})
    except:
        pass
    
    return jsonify({'public_ip': 'Unknown', 'error': 'API unavailable and direct IP check failed'}), 500

@app.route('/api/config')
def api_config():
    """Get current configuration settings"""
    try:
        config = load_config()
        
        # Only return safe config values (no tokens or sensitive data)
        safe_config = {
            'lan_ip': config.get('lan_bind_ip', 'Unknown'),
            'api_port': config.get('api', {}).get('port', 8088),
            'rotation': config.get('rotation', {}),
            # Modem settings are handled by run.sh, not config
            'proxy': {
                'auth_enabled': config.get('proxy', {}).get('auth_enabled', False)
            },
            'pm2': config.get('pm2', {}),
            'discord': {
                'configured': bool(config.get('discord', {}).get('webhook_url', '').startswith('https://discord.com/api/webhooks/') and 
                                 'YOUR_WEBHOOK_ID' not in config.get('discord', {}).get('webhook_url', ''))
            }
        }
        
        return jsonify(safe_config)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/history')
def api_history():
    """Get IP rotation history"""
    data = api_request('/history')
    if data:
        return jsonify(data)
    return jsonify({'ips': [], 'rotations': 0, 'error': 'API unavailable'}), 500


@app.route('/api/rotate', methods=['POST'])
def api_rotate():
    """Trigger IP rotation"""
    data = api_request('/rotate', method='POST')
    if data:
        return jsonify(data)
    return jsonify({'status': 'failed', 'error': 'API unavailable'}), 500

@app.route('/api/notify', methods=['POST'])
def api_notify():
    """Send Discord notification"""
    data = api_request('/notify', method='POST')
    if data:
        return jsonify(data)
    return jsonify({'error': 'API unavailable'}), 500

if __name__ == '__main__':
    print("🌐 Starting 4G Proxy Web Dashboard...")
    print("📱 Dashboard will be available at: http://192.168.1.37:5000")
    print("🔧 Make sure the orchestrator API is running on port 8088")
    
    # Get LAN IP for display
    config = load_config()
    lan_ip = config.get('lan_bind_ip', 'localhost')
    
    app.run(host='0.0.0.0', port=5000, debug=False)
