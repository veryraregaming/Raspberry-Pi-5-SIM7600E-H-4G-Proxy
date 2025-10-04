
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
    pub = requests.get('https://ifconfig.co/ip', timeout=10).text.strip()
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
    pub = requests.get('https://ifconfig.co/ip', timeout=10).text.strip()
    return jsonify({'pdp': pdp, 'public_ip': pub})

if __name__ == '__main__':
    config = load_config()
    os.system('bash scripts/4gproxy-net.sh')
    app.run(host=config['api']['bind'], port=config['api']['port'])
