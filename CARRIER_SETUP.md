# Adding New Carrier Support

This guide explains how to add support for new carriers and networks to the 4G proxy project.

## 📋 What You Need

To add a new carrier, you need these details:

### **Required Information:**
- **APN (Access Point Name)** - The network identifier
- **Username** - Usually empty for most modern carriers
- **Password** - Usually empty for most modern carriers
- **IP Type** - `ipv4` (most common) or `ipv4v6`

### **Optional Information:**
- **DNS Servers** - Usually auto-configured
- **Authentication Type** - Usually PAP or none

## 🔍 Finding APN Information

### **Method 1: Check Your Phone**
1. Go to **Settings** → **Mobile Networks** → **Access Point Names (APN)**
2. Look for your carrier's APN settings
3. Note down the APN, username, and password

### **Method 2: Carrier Website**
1. Visit your carrier's support website
2. Search for "APN settings" or "data configuration"
3. Look for manual configuration instructions

### **Method 3: Online Databases**
- **APN databases** - Search for your carrier
- **Community forums** - Other users' experiences
- **Carrier documentation** - Official setup guides

## 📝 Adding to carriers.json

### **Step 1: Open the file**
```bash
nano carriers.json
```

### **Step 2: Add your carrier**
Find the `carriers` section and add your entry:

```json
{
  "carriers": {
    "your_carrier_key": {
      "name": "Your Carrier Name",
      "apn": "your.apn.here",
      "username": "username_if_needed",
      "password": "password_if_needed",
      "ip_type": "ipv4"
    }
  }
}
```

### **Step 3: Use a unique key**
- Use descriptive names like `"vodafone_uk"`, `"three_ireland"`
- Avoid spaces and special characters
- Use underscores instead of spaces

## 🌍 Examples by Region

### **United Kingdom**
```json
"ee": {
  "name": "EE Internet",
  "apn": "everywhere",
  "username": "eesecure",
  "password": "secure",
  "ip_type": "ipv4"
},
"o2_contract": {
  "name": "O2 Internet",
  "apn": "mobile.o2.co.uk",
  "username": "o2web",
  "password": "password",
  "ip_type": "ipv4"
},
"vodafone_uk": {
  "name": "Vodafone UK",
  "apn": "internet",
  "username": "",
  "password": "",
  "ip_type": "ipv4"
}
```

### **United States**
```json
"verizon_us": {
  "name": "Verizon Wireless",
  "apn": "vzwinternet",
  "username": "",
  "password": "",
  "ip_type": "ipv4"
},
"att_us": {
  "name": "AT&T",
  "apn": "broadband",
  "username": "",
  "password": "",
  "ip_type": "ipv4"
},
"tmobile_us": {
  "name": "T-Mobile",
  "apn": "fast.t-mobile.com",
  "username": "",
  "password": "",
  "ip_type": "ipv4"
}
```

### **Europe**
```json
"orange_france": {
  "name": "Orange France",
  "apn": "orange",
  "username": "",
  "password": "",
  "ip_type": "ipv4"
},
"vodafone_germany": {
  "name": "Vodafone Germany",
  "apn": "web.vodafone.de",
  "username": "",
  "password": "",
  "ip_type": "ipv4"
},
"three_ireland": {
  "name": "Three Ireland",
  "apn": "3internet.ie",
  "username": "",
  "password": "",
  "ip_type": "ipv4"
}
```

### **Asia**
```json
"docomo_japan": {
  "name": "NTT Docomo",
  "apn": "mpr.ex-pac.jp",
  "username": "",
  "password": "",
  "ip_type": "ipv4"
},
"singtel_singapore": {
  "name": "Singtel",
  "apn": "e-ideas",
  "username": "",
  "password": "",
  "ip_type": "ipv4"
}
```

## 🧪 Testing Your Configuration

### **Step 1: Test the setup**
```bash
sudo ./run.sh
```

### **Step 2: Check if it works**
```bash
# Should return your SIM card IP, not home network IP
curl -x http://192.168.1.37:3128 https://api.ipify.org
```

### **Step 3: Check logs if it fails**
```bash
# Check PPP logs
sudo tail -f /var/log/ppp-ee.log

# Check Squid logs
sudo tail -f /var/log/squid/access.log
```

## 🔧 Troubleshooting

### **Common Issues:**

#### **1. APN Not Working**
- **Check APN spelling** - Must be exact
- **Try without username/password** - Most modern carriers don't need them
- **Check IP type** - Usually `ipv4`

#### **2. Authentication Failed**
- **Empty username/password** - Try `""` instead of actual values
- **Check carrier documentation** - Some carriers use specific credentials

#### **3. No Internet Connection**
- **Verify SIM card** - Ensure it has data plan
- **Check signal strength** - Move to better location
- **Try different APN** - Carrier might have multiple APNs

#### **4. Wrong IP Address**
- **Check routing** - Ensure traffic goes through ppp0
- **Restart PPP** - `sudo pkill pppd && sudo pppd call ee`

## 📤 Contributing

### **How to Submit Your Carrier:**

1. **Test thoroughly** with your SIM card
2. **Verify it works** - Returns SIM card IP, not home network IP
3. **Fork the repository**
4. **Add your carrier** to `carriers.json`
5. **Submit a pull request** with:
   - Carrier name and region
   - Test results
   - Any special notes

### **Pull Request Template:**
```markdown
## New Carrier: [Carrier Name] - [Region]

### APN Details:
- **APN**: [apn.name.here]
- **Username**: [username or empty]
- **Password**: [password or empty]
- **IP Type**: ipv4

### Testing:
- [x] SIM card connects successfully
- [x] Proxy returns SIM card IP
- [x] Internet access works through proxy
- [x] No authentication errors

### Notes:
[Any special configuration or notes]
```

## 🆘 Need Help?

### **Getting APN Information:**
1. **Check your phone** - Most reliable source
2. **Carrier support** - Contact your carrier
3. **Community forums** - Ask other users
4. **Online databases** - Search for APN settings

### **Testing Issues:**
1. **Check logs** - PPP and Squid logs
2. **Verify SIM card** - Ensure it has data plan
3. **Test manually** - Try AT commands directly
4. **Ask for help** - Open an issue on GitHub

## 📚 Additional Resources

### **AT Commands for Testing:**
```bash
# Test modem response
echo "AT" | sudo tee /dev/ttyUSB2

# Check SIM status
echo "AT+CPIN?" | sudo tee /dev/ttyUSB2

# Check network registration
echo "AT+CREG?" | sudo tee /dev/ttyUSB2

# Check GPRS attachment
echo "AT+CGATT?" | sudo tee /dev/ttyUSB2

# Configure APN
echo 'AT+CGDCONT=1,"IP","your.apn.here"' | sudo tee /dev/ttyUSB2

# Activate PDP context
echo "AT+CGACT=1,1" | sudo tee /dev/ttyUSB2
```

### **Useful Websites:**
- [APN Settings Database](https://www.apnsettings.org/)
- [Carrier APN Lists](https://www.whistleout.com/CellPhones/Guides/APN-settings)
- [PPP Configuration Guide](https://ppp.samba.org/pppd.html)

Remember: The goal is to get your SIM card's IP address through the proxy, not your home network IP!
