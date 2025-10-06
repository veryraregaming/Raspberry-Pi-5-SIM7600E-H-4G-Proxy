# Raspberry Pi 5 + SIM7600E-H 4G Proxy

A complete 4G mobile proxy solution for Raspberry Pi 5 with SIM7600E-H modem. Routes internet traffic through your SIM card instead of home network, with automatic APN detection, Discord notifications, IP rotation tracking, and comprehensive error handling.

## 🚀 Features

- **🌐 4G Proxy**: HTTP proxy through SIM card (port 3128)
- **🔄 IP Rotation**: Automatic IP changes with AT commands and failure tracking
- **📱 Discord Notifications**: Real-time IP change notifications with history and error reporting
- **🎯 APN Auto-Detection**: Works with any UK carrier SIM card automatically
- **🛡️ Error Handling**: Comprehensive failure tracking and detailed error messages
- **📊 IP History**: Track rotation frequency, uptime, and IP changes
- **🚀 One-Shot Setup**: Single command installation with `sudo ./run.sh`
- **🔄 Self-Healing**: Automatic recovery from common issues and PM2 cleanup
- **🛡️ Universal**: Works with any username on any system
- **📱 Message Patching**: Discord notifications update same message (no spam)

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
- 🌐 Web Dashboard: `http://192.168.1.37:5000`
- 📊 API Endpoint: `http://127.0.0.1:8088`
- 🌐 Current Public IP: `[SIM-card-IP]` (not your home network IP)
- 🧪 Test command: `curl -x http://192.168.1.37:3128 https://api.ipify.org`

## 🌐 Supported Networks

### **Currently Supported:**
- **UK Carriers**: EE, O2, Vodafone, Three UK
- **UK MVNOs**: giffgaff, Tesco Mobile, ASDA Mobile, BT Mobile, 1pMobile, Sky Mobile, Lycamobile UK
- **International SIMs**: Any SIM card that supports PPP dial-up (`ATD*99#`)

### **Adding New Carriers:**
See [CARRIER_SETUP.md](CARRIER_SETUP.md) for detailed instructions on adding support for new carriers and networks worldwide.

## 🔄 Changing SIM Cards

### **Safe SIM Card Swap Procedure**

The system supports hot-swapping SIM cards, but the safest method is:

1. **Power down the Pi**:
   ```bash
   sudo shutdown -h now
   ```

2. **Wait for complete shutdown** (all LEDs off)

3. **Remove old SIM, insert new SIM**

4. **Power on the Pi**

5. **SSH back in and run setup**:
   ```bash
   cd ~/Raspberry-Pi-5-SIM7600E-H-4G-Proxy
   git pull
   sudo ./run.sh
   ```

### **What Happens Automatically**:
- ✅ New carrier detected (e.g., EE → Three UK)
- ✅ Correct APN configured from carriers.json
- ✅ RNDIS interface reinitialized
- ✅ Auto-rotation continues with new carrier
- ✅ Discord notifications resume with new IPs
- ✅ No manual configuration needed

### **Hot-Swap (Advanced)**:
If you want to hot-swap without powering down:
```bash
# Stop services first
pm2 stop all

# Physically swap SIM card

# Re-run setup
sudo ./run.sh
```

**Note**: Hot-swapping may not be detected immediately. Power cycling is more reliable.

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
- **❌ IP Rotation Failed** - When rotation attempts fail
- **📊 Status Update** - Manual status notifications

### **API Endpoints**
```bash
# Send status notification
curl -X POST http://127.0.0.1:8088/notify \
  -H "Authorization: Bearer YOUR_API_TOKEN"

# Rotate IP and notify
curl -X POST http://127.0.0.1:8088/rotate \
  -H "Authorization: Bearer YOUR_API_TOKEN"

# View IP rotation history
curl -H "Authorization: Bearer YOUR_API_TOKEN" \
  http://127.0.0.1:8088/history

# Test failure notification (for debugging)
curl -X POST http://127.0.0.1:8088/test-failure \
  -H "Authorization: Bearer YOUR_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"error": "Connection timeout"}'
```

### **Discord Notification Features**
- **📋 IP History** - Shows last 5 IP addresses with timestamps
- **⏱️ Uptime Tracking** - Displays total uptime since first connection
- **🔄 Rotation Counter** - Counts total IP rotations
- **❌ Error Handling** - Reports rotation failures with detailed error messages
- **📱 Message Patching** - Updates same message (no spam)
- **🎨 Color Coding** - Green (success), Blue (startup), Orange (status), Red (failure)

## 🌐 Web Dashboard

### **Access the Dashboard**
Open your browser and go to: `http://YOUR_PI_IP:5000`

### **Dashboard Features**
- **📊 Real-time Status** - Current IP, connection status, uptime
- **🔄 IP Rotation** - One-click IP rotation with success/failure feedback
- **📋 IP History** - Visual history of all IP changes with timestamps
- **📱 Discord Notifications** - Send manual notifications
- **📈 Statistics** - Rotation count, uptime, last rotation time
- **🎮 Controls** - Easy-to-use buttons for all operations
- **🔄 Auto-refresh** - Updates every 30 seconds automatically

### **Dashboard Screenshots**
The dashboard provides a modern, responsive interface with:
- Current proxy status and IP address
- Visual IP rotation history
- One-click controls for all operations
- Real-time error reporting
- Mobile-friendly design

## 🔧 Management Commands

### **Web Interface**
```bash
# Access dashboard
http://YOUR_PI_IP:5000

# Check web interface status
pm2 status 4g-proxy-web

# View web interface logs
pm2 logs 4g-proxy-web
```

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

## 📡 API Documentation

### **Endpoints**

#### **GET /status**
Get current proxy status and IP address.
```bash
curl -H "Authorization: Bearer YOUR_TOKEN" http://127.0.0.1:8088/status
```

#### **POST /rotate**
Attempt IP rotation and notify Discord.
```bash
curl -X POST -H "Authorization: Bearer YOUR_TOKEN" http://127.0.0.1:8088/rotate
```

#### **POST /notify**
Send Discord status notification.
```bash
curl -X POST -H "Authorization: Bearer YOUR_TOKEN" http://127.0.0.1:8088/notify
```

#### **GET /history**
Get IP rotation history and statistics.
```bash
curl -H "Authorization: Bearer YOUR_TOKEN" http://127.0.0.1:8088/history
```

#### **POST /test-failure**
Test failure notification (for debugging).
```bash
curl -X POST -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"error": "Test error message"}' \
  http://127.0.0.1:8088/test-failure
```

### **Response Formats**

#### **Success Response**
```json
{
  "status": "success",
  "public_ip": "192.168.1.100",
  "previous_ip": "188.28.48.189",
  "pdp": "+CGPADDR: 1,192.168.1.100"
}
```

#### **Failure Response**
```json
{
  "status": "failed",
  "error": "IP did not change after rotation attempt",
  "public_ip": "188.28.48.189",
  "previous_ip": "188.28.48.189"
}
```

#### **History Response**
```json
{
  "ips": [
    {
      "ip": "192.168.1.100",
      "timestamp": "2025-10-05T14:30:15",
      "time": "14:30:15",
      "date": "05/10/2025"
    }
  ],
  "rotations": 3,
  "first_seen": "2025-10-05T12:15:00"
}
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

## 🔧 Troubleshooting

### **Common Issues**

#### **Proxy routes through home network instead of SIM**
```bash
# Check if ppp0 is up and has IP
ip addr show ppp0

# Check routing table
ip route show

# Restart PPP connection
sudo pkill pppd
sudo pppd call ee
```

#### **Discord notifications not working**
```bash
# Check webhook URL in config
grep webhook_url config.yaml

# Test notification
python3 test_discord.py

# Check orchestrator logs
pm2 logs 4g-proxy-orchestrator
```

#### **IP rotation fails**
```bash
# Check modem connection
sudo mmcli -m 0

# Test AT commands
echo "AT" | sudo tee /dev/ttyUSB2

# Check rotation history
curl -H "Authorization: Bearer YOUR_TOKEN" http://127.0.0.1:8088/history
```

#### **PM2 services not starting**
```bash
# Check PM2 status
pm2 status

# Restart PM2 services
pm2 restart all

# Check logs
pm2 logs
```

### **Log Locations**
- **Squid**: `sudo tail -f /var/log/squid/access.log`
- **PPP**: `sudo tail -f /var/log/ppp-ee.log`
- **PM2**: `pm2 logs 4g-proxy-orchestrator`
- **System**: `sudo journalctl -f`

### **Diagnostic Tool**

Run the diagnostic script to troubleshoot issues:

```bash
python3 diagnose.py
```

This will check:
- Configuration validity
- Service status (PM2, Squid, orchestrator)
- API connectivity
- IP history
- Network interfaces (ppp0, wwan0)
- Current public IP

## 🛡️ Security Notes

- **Never commit `config.yaml`** - it contains sensitive tokens
- Use strong, random tokens for API authentication
- Consider firewall rules to restrict proxy access
- Monitor logs for unauthorized access attempts
- Discord webhook URLs are sensitive - keep them private

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