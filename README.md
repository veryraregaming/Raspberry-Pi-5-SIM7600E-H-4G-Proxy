
# Raspberry Pi 5 + SIM7600E-H 4G Proxy

A lightweight 4G proxy solution for Raspberry Pi 5 with SIM7600E-H modem. Provides HTTP/SOCKS proxy with automatic IP rotation capabilities.

## 🚀 Features

- **Auto-detection**: Automatically finds SIM7600 cellular interface (wwan*/ppp*)
- **Dual Proxy Support**: HTTP & SOCKS proxy via 3proxy
- **NAT Routing**: Routes outbound traffic through 4G modem
- **IP Rotation**: Change public IP using AT commands via REST API
- **REST API**: Control and monitor via HTTP endpoints
- **Token Authentication**: Secure API access with configurable tokens
- **No Auth Option**: Optional proxy without username/password

## 📋 Requirements

- Raspberry Pi 5
- SIM7600E-H 4G modem
- Ubuntu Server 22.04+ (recommended)
- Active SIM card with data plan
- Root/sudo access

## 🔧 Installation

### 1. Clone Repository
```bash
git clone <repository-url>
cd raspi-4g-proxy-v2
```

### 2. Install Dependencies
```bash
sudo apt update
sudo apt install python3 python3-pip python3-yaml python3-serial python3-requests iptables -y
pip3 install -r requirements.txt --break-system-packages
```

**Note:** The `--break-system-packages` flag is required on Ubuntu 24.04+ to override system protection.

### 3. Configure
```bash
cp config.yaml.example config.yaml
nano config.yaml
```

Edit `config.yaml`:
- Set your LAN IP address
- Generate a secure random token
- Configure proxy credentials (or leave empty for no auth)

### 4. Run
```bash
sudo python3 orchestrator.py
```

## ⚙️ Configuration

### config.yaml
```yaml
lan_bind_ip: "192.168.1.37"   # Your Pi's LAN IP
api:
  bind: "127.0.0.1"           # API bind address
  port: 8088                  # API port
  token: "your-secure-token"  # API authentication token
proxy:
  user: ""                    # Proxy username (empty = no auth)
  password: ""                # Proxy password (empty = no auth)
```

## 🌐 API Endpoints

### GET /status
Check current IP status
```bash
curl http://127.0.0.1:8088/status
```

### POST /rotate
Rotate public IP (requires token)
```bash
curl -X POST \
  -H "Authorization: your-secure-token" \
  http://127.0.0.1:8088/rotate
```

## 🔄 Manual IP Rotation
```bash
# Using the API
curl -X POST -H "Authorization: your-token" http://127.0.0.1:8088/rotate

# Or directly via AT commands
echo -e "AT+CGACT=0,1\r" | sudo tee /dev/ttyUSB2
sleep 2
echo -e "AT+CGACT=1,1\r" | sudo tee /dev/ttyUSB2
```

## 🛡️ Security Notes

- **Never commit `config.yaml`** - it contains sensitive tokens
- Use strong, random tokens for API authentication
- Consider firewall rules to restrict API access
- Monitor logs for unauthorized access attempts

## 📁 Project Structure

```
raspi-4g-proxy-v2/
├── orchestrator.py          # Main application
├── config.yaml.example     # Configuration template
├── requirements.txt        # Python dependencies
├── scripts/
│   └── 4gproxy-net.sh      # Network setup script
└── README.md               # This file
```

## 🔍 Troubleshooting

### Modem Not Detected
```bash
# Check USB devices
lsusb | grep -i sim

# Check serial ports
ls /dev/ttyUSB*

# Test AT commands
echo -e "AT\r" | sudo tee /dev/ttyUSB2
```

### No Internet Connection
```bash
# Check interface status
ip addr show | grep -E 'wwan|ppp'

# Test connectivity
ping -I wwan0 8.8.8.8
```

### API Not Responding
```bash
# Check if service is running
ps aux | grep orchestrator.py

# Check port binding
netstat -tlnp | grep 8088
```

### Flask Module Not Found
```bash
# Install Flask and dependencies
pip3 install -r requirements.txt --break-system-packages

# Or install Flask via apt
sudo apt install python3-flask -y
```

## 📝 License

[Add your license here]

## 🤝 Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Test thoroughly
5. Submit a pull request

## ⚠️ Disclaimer

This software is for educational and legitimate use only. Users are responsible for complying with local laws and terms of service.
