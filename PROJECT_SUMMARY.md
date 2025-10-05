# Project Summary: Raspberry Pi 5 + SIM7600E-H 4G Proxy

## 🎯 Project Overview

A complete 4G mobile proxy solution that routes internet traffic through a SIM card instead of home network. Features automatic APN detection, Discord notifications, IP rotation tracking, and comprehensive error handling.

## ✨ Key Features

### **Core Functionality**
- **🌐 HTTP Proxy**: Squid proxy on port 3128 through SIM card
- **🔄 IP Rotation**: Automatic IP changes with AT commands
- **📱 Discord Integration**: Real-time notifications with message patching
- **🎯 APN Auto-Detection**: Works with any UK carrier automatically
- **🛡️ Error Handling**: Comprehensive failure tracking and reporting

### **Advanced Features**
- **📊 IP History**: Track rotation frequency, uptime, and changes
- **🚀 One-Shot Setup**: Single command installation
- **🔄 Self-Healing**: Automatic recovery from common issues
- **📱 Message Patching**: Discord notifications update same message (no spam)
- **🛡️ Universal**: Works with any username on any system

## 🏗️ Architecture

### **Components**
1. **main.py** - Setup script and configuration
2. **orchestrator.py** - API server and Discord notifications
3. **run.sh** - One-shot installation script
4. **scripts/4gproxy-net.sh** - Network routing management
5. **carriers.json** - UK carrier APN configurations
6. **test_discord.py** - Discord notification testing

### **Data Flow**
```
SIM7600E-H → PPP Connection → ppp0 Interface → Squid Proxy → Client Traffic
                ↓
            Orchestrator API → Discord Notifications
```

## 📁 File Structure

```
raspi-4g-proxy-v2/
├── main.py                 # Main setup script
├── orchestrator.py         # API server with Discord notifications
├── run.sh                  # One-shot installation script
├── carriers.json           # UK carrier APN configurations
├── config.yaml.example     # Configuration template
├── requirements.txt        # Python dependencies
├── test_discord.py         # Discord testing script
├── CARRIER_SETUP.md        # Guide for adding new carriers
├── README.md              # Comprehensive documentation
├── PROJECT_SUMMARY.md     # This file
└── scripts/
    └── 4gproxy-net.sh     # Network routing script
```

## 🔧 Configuration

### **Auto-Generated config.yaml**
```yaml
lan_bind_ip: "192.168.1.37"   # Auto-detected LAN IP
api:
  bind: "127.0.0.1"
  port: 8088
  token: "auto-generated-secure-token"
proxy:
  auth_enabled: false         # Optional authentication
  user: ""
  password: ""
modem:
  apn: "everywhere"           # Default EE APN
  port: "/dev/ttyUSB2"        # Auto-detected
  timeout: 2
pm2:
  enabled: true
  ip_rotation_interval: 300   # 5 minutes
  auto_restart: true
discord:
  webhook_url: "PLACEHOLDER"  # User must configure
```

## 📡 API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/status` | GET | Current proxy status and IP |
| `/rotate` | POST | Attempt IP rotation |
| `/notify` | POST | Send Discord notification |
| `/history` | GET | IP rotation history |
| `/test-failure` | POST | Test failure notification |

## 📱 Discord Notifications

### **Notification Types**
- **🚀 Proxy Initialization** (Blue) - Startup notification
- **🔄 IP Rotation Complete** (Green) - Successful rotation
- **❌ IP Rotation Failed** (Red) - Failed rotation with error
- **📊 Status Update** (Orange) - Manual status notification

### **Features**
- **Message Patching** - Updates same message (no spam)
- **IP History** - Shows last 5 IP addresses with timestamps
- **Uptime Tracking** - Total time since first connection
- **Rotation Counter** - Counts total IP rotations
- **Error Details** - Specific error messages for failures

## 🚀 Installation Process

### **One-Shot Setup**
```bash
sudo ./run.sh
```

### **What It Does**
1. **System Setup**
   - Installs dependencies (Node.js, PM2, Squid, PPP)
   - Creates proxyuser with limited sudo access
   - Sets up self-healing DNS bootstrap

2. **Configuration**
   - Auto-detects LAN IP address
   - Generates secure API token
   - Creates Squid configuration
   - Sets up PM2 ecosystem

3. **Network Setup**
   - Activates SIM7600E-H modem via PPP
   - Configures routing through SIM card
   - Keeps WiFi primary for SSH stability

4. **Service Management**
   - Starts Squid proxy as system service
   - Launches orchestrator API via PM2
   - Enables auto-restart and startup

## 🔄 Operation Flow

### **Startup**
1. PPP connection established via `pppd call ee`
2. Squid proxy starts and binds to LAN IP:3128
3. Orchestrator API starts on 127.0.0.1:8088
4. Discord notification sent (if configured)

### **IP Rotation**
1. API receives `/rotate` request
2. AT commands deactivate/reactivate PDP context
3. New IP detected and compared
4. Discord notification sent with success/failure status
5. IP history updated

### **Error Handling**
1. Failed rotations detected automatically
2. Error messages captured and categorized
3. Discord failure notifications sent
4. Detailed logging for troubleshooting

## 🛡️ Security Features

- **API Authentication** - Bearer token required for all endpoints
- **Limited Sudo Access** - proxyuser can only manage Squid
- **Network Isolation** - Proxy traffic isolated from home network
- **Secure Configuration** - Auto-generated secure tokens
- **Private Webhooks** - Discord webhook URLs kept secure

## 📊 Monitoring & Logging

### **Log Locations**
- **Squid**: `/var/log/squid/access.log`
- **PPP**: `/var/log/ppp-ee.log`
- **PM2**: `pm2 logs 4g-proxy-orchestrator`
- **System**: `sudo journalctl -f`

### **State Files**
- **Discord Message ID**: `state/discord_message_id.txt`
- **IP History**: `state/ip_history.json`

## 🌍 Carrier Support

### **UK Carriers (carriers.json)**
- **EE**: everywhere, eesecure/secure
- **O2**: mobile.o2.co.uk, payandgo.o2.co.uk
- **Vodafone**: internet, pp.vodafone.co.uk
- **Three UK**: three.co.uk
- **MVNOs**: giffgaff, Tesco Mobile, ASDA Mobile, etc.

### **Adding New Carriers**
See `CARRIER_SETUP.md` for detailed instructions on adding support for new carriers and regions worldwide.

## 🧪 Testing

### **Basic Testing**
```bash
# Test proxy
curl -x http://192.168.1.37:3128 https://api.ipify.org

# Test Discord notifications
python3 test_discord.py

# Test IP rotation
curl -X POST -H "Authorization: Bearer TOKEN" http://127.0.0.1:8088/rotate
```

### **Verification**
- Proxy returns SIM card IP (not home network IP)
- Discord notifications work with message patching
- IP rotations succeed and are tracked
- Error handling works for failed rotations

## 🔧 Maintenance

### **Regular Tasks**
- Monitor Discord notifications for rotation success/failure
- Check IP history for rotation frequency
- Review logs for any issues
- Update carriers.json for new regions if needed

### **Troubleshooting**
- Use comprehensive troubleshooting section in README.md
- Check all log locations for error details
- Test individual components (PPP, Squid, API, Discord)
- Use test endpoints for debugging

## 🎯 Success Criteria

### **Functional Requirements**
- ✅ Proxy routes through SIM card (not home network)
- ✅ Discord notifications work with message patching
- ✅ IP rotations succeed and are tracked
- ✅ Error handling reports failures with details
- ✅ One-shot setup works on fresh Pi installations
- ✅ Self-healing recovers from common issues

### **Non-Functional Requirements**
- ✅ Works with any username on any system
- ✅ No SSH disconnections during setup
- ✅ Comprehensive documentation and troubleshooting
- ✅ Secure configuration and API authentication
- ✅ Clean project structure with no unnecessary files

## 📈 Future Enhancements

### **Potential Improvements**
- **Auto-rotation scheduling** - Configurable rotation intervals
- **Multiple Discord channels** - Different notifications for different events
- **Web dashboard** - Browser-based monitoring interface
- **More carriers** - International carrier support expansion
- **Health checks** - Automated monitoring and alerting
- **Backup routing** - Fallback to different APNs on failure

## 📝 Documentation

### **User Documentation**
- **README.md** - Complete setup and usage guide
- **CARRIER_SETUP.md** - Guide for adding new carriers
- **PROJECT_SUMMARY.md** - This comprehensive overview

### **API Documentation**
- Complete endpoint documentation with examples
- Response format specifications
- Error code explanations
- Authentication requirements

## 🏆 Project Status

**Status**: ✅ **PRODUCTION READY**

The project is complete, thoroughly tested, and ready for production use. All core features work reliably, documentation is comprehensive, and the system is robust with proper error handling and self-healing capabilities.
