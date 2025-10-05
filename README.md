# Raspberry Pi 5 + SIM7600E-H 4G Proxy

A reliable 4G proxy solution for Raspberry Pi 5 with SIM7600E-H modem. Provides HTTP proxy with automatic SIM card routing via PPP connection.

## 🚀 Features

- **One-Shot Setup**: Single command installation with `sudo ./run.sh`
- **PPP Connection**: Reliable SIM7600E-H activation via PPP dial-up
- **Automatic APN Detection**: Works with any UK carrier SIM card
- **Simple Routing**: Routes proxy traffic through SIM card, keeps WiFi for SSH
- **HTTP Proxy**: Squid proxy on port 3128
- **Universal**: Works with any username on any system
- **No SSH Disconnections**: WiFi remains primary route for stable access

## 📋 Requirements

- Raspberry Pi 5
- SIM7600E-H 4G modem
- Ubuntu Server 24.04+ (recommended)
- Active SIM card with data plan
- Root/sudo access

## 🔧 Installation

### **One-Command Setup (Recommended)**
```bash
git clone https://github.com/veryraregaming/Raspberry-Pi-5-SIM7600E-H-4G-Proxy.git
cd Raspberry-Pi-5-SIM7600E-H-4G-Proxy
sudo ./run.sh
```

**That's it!** The script will:
- ✅ Install all dependencies (Squid, PPP, PM2, etc.)
- ✅ Auto-detect your LAN IP
- ✅ Generate secure configuration
- ✅ Activate SIM7600E-H modem via PPP
- ✅ Setup simple routing through SIM card
- ✅ Start Squid proxy on port 3128
- ✅ Show test commands and proxy details

**After setup, you'll see:**
- 📡 HTTP Proxy: `192.168.1.37:3128`
- 🌐 Current Public IP: `[SIM-card-IP]` (not your home network IP)
- 🧪 Test command: `curl -x http://192.168.1.37:3128 https://api.ipify.org`

## 🌐 Supported Networks

### **Currently Supported:**
- **UK Carriers**: EE, O2, Vodafone, Three UK
- **UK MVNOs**: giffgaff, Tesco Mobile, ASDA Mobile, BT Mobile, 1pMobile, Sky Mobile, Lycamobile UK
- **International SIMs**: Any SIM card that supports PPP dial-up (`ATD*99#`)

### **Adding New Carriers:**
See [CARRIER_SETUP.md](CARRIER_SETUP.md) for detailed instructions on adding support for new carriers and networks worldwide.

## ⚙️ Configuration

### config.yaml
```yaml
lan_bind_ip: "192.168.1.37"   # Your Pi's LAN IP (auto-detected)
api:
  bind: "127.0.0.1"           # API bind address
  port: 8088                  # API port
  token: "your-secure-token"  # API authentication token (auto-generated)
proxy:
  auth_enabled: false         # Set to true to enable proxy authentication
  user: ""                    # Proxy username (only used if auth_enabled: true)
  password: ""                # Proxy password (only used if auth_enabled: true)
modem:
  apn: "everywhere"           # APN (auto-detected from carriers.json)
  port: "/dev/ttyUSB2"        # Modem AT port (auto-detected)
  timeout: 30                 # Connection timeout
pm2:
  enabled: true               # Enable PM2 process management
  auto_restart: true          # Auto-restart on crash
  ip_rotation_interval: 300   # IP rotation interval (seconds)
  max_restarts: 10            # Maximum restart attempts
  restart_delay: 5000         # Delay between restarts (ms)
discord:
  webhook_url: "https://discord.com/api/webhooks/YOUR_WEBHOOK_ID/YOUR_TOKEN"  # Discord notifications
```

## 📱 Discord Notifications

### **Setup Discord Notifications**
1. **Create Discord Webhook:**
   - Go to your Discord server → Server Settings → Integrations → Webhooks
   - Create a new webhook and copy the URL

2. **Configure in config.yaml:**
   ```yaml
   discord:
     webhook_url: "https://discord.com/api/webhooks/YOUR_WEBHOOK_ID/YOUR_TOKEN"
   ```

3. **Test notifications:**
   ```bash
   python3 test_discord.py
   ```

### **Notification Types**
- **🚀 Proxy Initialization** - When proxy starts up
- **🔄 IP Rotation Complete** - When IP changes successfully  
- **📊 Status Update** - Manual status notifications

### **API Endpoints**
```bash
# Send status notification
curl -X POST http://127.0.0.1:8088/notify \
  -H "Authorization: Bearer YOUR_API_TOKEN"

# Rotate IP and notify
curl -X POST http://127.0.0.1:8088/rotate \
  -H "Authorization: Bearer YOUR_API_TOKEN"
```

## 🔧 Management Commands

### **Squid Proxy**
```bash
# Check status
sudo systemctl status squid

# Restart proxy
sudo systemctl restart squid

# View logs
sudo journalctl -u squid -f
```

### **PM2 (Orchestrator)**
```bash
# Check status
pm2 status

# View logs
pm2 logs

# Restart
pm2 restart 4g-proxy-orchestrator
```

### **PPP Connection**
```bash
# Check PPP status
ip -4 addr show ppp0

# Restart PPP
sudo pkill pppd
sudo pppd call ee
```

## 🧪 Testing

### **Test Proxy**
```bash
# Test from Pi
curl -x http://192.168.1.37:3128 https://api.ipify.org

# Test from another machine on same network
curl -x http://[PI-IP]:3128 https://api.ipify.org
```

### **Check SIM Card IP**
```bash
# Should return SIM card IP, not home network IP
curl -x http://192.168.1.37:3128 https://api.ipify.org
```

## 🔍 Troubleshooting

### **Modem Not Detected**
```bash
# Check USB devices
lsusb | grep -i sim

# Check serial ports
ls /dev/ttyUSB*

# Test AT commands
echo "AT" | sudo tee /dev/ttyUSB2
```

### **PPP Connection Failed**
```bash
# Check PPP logs
sudo tail -f /var/log/ppp-ee.log

# Restart PPP
sudo pkill pppd
sudo pppd call ee
```

### **Proxy Not Working**
```bash
# Check Squid status
sudo systemctl status squid

# Check Squid logs
sudo tail -f /var/log/squid/access.log

# Test direct connection
curl -s https://api.ipify.org
```

### **Wrong IP (Home Network Instead of SIM)**
```bash
# Check routing
ip route show

# Check ppp0 interface
ip -4 addr show ppp0

# Restart setup
sudo ./run.sh
```

## 📁 Project Structure

```
raspi-4g-proxy-v2/
├── run.sh                    # Main setup script
├── main.py                   # Core logic and PPP activation
├── orchestrator.py           # PM2 management
├── carriers.json             # UK carrier APN configurations
├── config.yaml.example       # Configuration template
├── requirements.txt          # Python dependencies
├── scripts/
│   └── 4gproxy-net.sh        # Network routing script
└── README.md                 # This file
```

## 🔄 How It Works

1. **PPP Activation**: Uses `pppd call ee` to establish PPP connection
2. **Simple Routing**: Adds default route via ppp0 with higher metric
3. **WiFi Stability**: Keeps WiFi as primary route for SSH access
4. **Proxy Traffic**: Squid routes through ppp0 to SIM card
5. **APN Detection**: Automatically tries APNs from carriers.json

## 🛡️ Security Notes

- **Never commit `config.yaml`** - it contains sensitive tokens
- Use strong, random tokens for API authentication
- Consider firewall rules to restrict proxy access
- Monitor logs for unauthorized access attempts

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