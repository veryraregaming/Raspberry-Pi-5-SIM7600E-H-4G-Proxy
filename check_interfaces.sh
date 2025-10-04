#!/bin/bash
# SIM7600E-H Interface Detection Script

echo "=========================================="
echo "SIM7600E-H Interface Detection"
echo "=========================================="

echo "1. All network interfaces:"
ip -o link show | grep -v lo

echo ""
echo "2. Interfaces with IP addresses:"
ip -o addr show | grep "inet " | grep -v "127.0.0.1"

echo ""
echo "3. USB devices (looking for SIM7600E-H):"
lsusb | grep -i sim || echo "No SIM device found in lsusb"

echo ""
echo "4. Serial devices (ttyUSB):"
ls -la /dev/ttyUSB* 2>/dev/null || echo "No ttyUSB devices found"

echo ""
echo "5. Network interfaces by type:"
echo "Ethernet interfaces:"
ip -o link show | grep -E "^[0-9]+: eth"

echo "WiFi interfaces:"
ip -o link show | grep -E "^[0-9]+: wlan"

echo "USB/Modem interfaces:"
ip -o link show | grep -E "^[0-9]+: (usb|wwan|cdc)"

echo ""
echo "6. Routing table:"
ip route show

echo ""
echo "7. Default route:"
ip route | grep default

echo ""
echo "8. Test connectivity through each interface:"
for iface in $(ip -o link show | awk -F': ' '{print $2}' | grep -v lo); do
    if ip addr show "$iface" | grep -q "inet "; then
        echo "Testing $iface..."
        # Get IP of this interface
        ip=$(ip addr show "$iface" | grep "inet " | awk '{print $2}' | cut -d/ -f1)
        echo "  IP: $ip"
        
        # Test if this interface can reach internet
        if ping -c 1 -W 1 -I "$iface" 8.8.8.8 >/dev/null 2>&1; then
            echo "  ✅ Can reach internet via $iface"
            # Get public IP through this interface
            pub_ip=$(curl -s --interface "$iface" --max-time 5 https://api.ipify.org 2>/dev/null || echo "Failed")
            echo "  Public IP: $pub_ip"
        else
            echo "  ❌ Cannot reach internet via $iface"
        fi
        echo ""
    fi
done

echo "=========================================="
echo "Summary:"
echo "- Look for interfaces that can reach internet"
echo "- SIM7600E-H typically appears as: wwan0, usb0, usb1, cdc-wdm0"
echo "- The interface with a different public IP than your home network is likely the SIM card"
echo "=========================================="
