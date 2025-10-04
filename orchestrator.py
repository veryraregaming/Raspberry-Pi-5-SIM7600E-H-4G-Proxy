
#!/usr/bin/env python3
import os, time, requests, serial, yaml
from flask import Flask, request, jsonify, abort

app = Flask(__name__)

def load_config():
    with open('config.yaml', 'r') as f:
        return yaml.safe_load(f)

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

@app.route('/status')
def status():
    pdp = at('AT+CGPADDR')
    pub = requests.get('https://ipv4.icanhazip.com', timeout=10).text.strip()
    return jsonify({'pdp': pdp, 'public_ip': pub})

@app.route('/rotate', methods=['POST'])
def rotate():
    config = load_config()
    expected_token = config['api']['token']
    token = request.headers.get('Authorization', '')
    if expected_token not in token:
        abort(403)
    at('AT+CGACT=0,1')
    time.sleep(2)
    at('AT+CGACT=1,1')
    time.sleep(4)
    pdp = at('AT+CGPADDR')
    pub = requests.get('https://ipv4.icanhazip.com', timeout=10).text.strip()
    return jsonify({'pdp': pdp, 'public_ip': pub})

if __name__ == '__main__':
    config = load_config()
    os.system('bash scripts/4gproxy-net.sh')
    
    # Print startup info
    lan_ip = config['lan_bind_ip']
    proxy_user = config['proxy']['user']
    proxy_pass = config['proxy']['password']
    
    print("\n" + "=" * 60)
    print("🚀 4G Proxy Orchestrator Started")
    print("=" * 60)
    print(f"📡 HTTP Proxy: {lan_ip}:8080")
    print(f"📡 SOCKS Proxy: {lan_ip}:1080")
    
    if proxy_user and proxy_pass:
        print(f"🔐 Authentication: {proxy_user}:{proxy_pass}")
        print(f"🧪 Test Command: curl -x http://{proxy_user}:{proxy_pass}@{lan_ip}:8080 https://api.ipify.org")
    else:
        print("🔓 No authentication required")
        print(f"🧪 Test Command: curl -x http://{lan_ip}:8080 https://api.ipify.org")
    
    print("📊 API Status: http://127.0.0.1:8088/status")
    print("=" * 60)
    
    app.run(host=config['api']['bind'], port=config['api']['port'])
