#!/usr/bin/env python3
"""
RNDIS Interface and Policy Routing Fix Script
Automatically detects and fixes RNDIS interface issues
"""

import subprocess
import sys
import os

def run_cmd(cmd, check=False):
    """Run a command and return (stdout, stderr, returncode)"""
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, check=check)
        return result.stdout.strip(), result.stderr.strip(), result.returncode
    except subprocess.CalledProcessError as e:
        return e.stdout.strip(), e.stderr.strip(), e.returncode

def detect_rndis_interface():
    """Detect RNDIS interface"""
    stdout, _, _ = run_cmd("ip -o link show | awk -F': ' '{print $2}' | grep -E '^enx|^eth1' | head -1")
    return stdout if stdout else None

def check_interface_status(iface):
    """Check if interface is up and has IP"""
    stdout, _, _ = run_cmd(f"ip addr show {iface}")
    has_ip = "inet " in stdout
    is_up = "state UP" in stdout or "state UNKNOWN" in stdout
    return has_ip, is_up

def check_routing_table():
    """Check if rndis routing table has default route"""
    stdout, _, _ = run_cmd("ip route show table rndis")
    has_route = "default" in stdout
    return has_route

def check_policy_rules():
    """Check if policy routing rules exist"""
    stdout, _, _ = run_cmd("ip rule show | grep 'fwmark 0x1'")
    has_rule = "fwmark 0x1" in stdout
    return has_rule

def fix_interface(iface):
    """Bring interface up"""
    print(f"🔧 Bringing interface {iface} up...")
    stdout, stderr, rc = run_cmd(f"sudo ip link set {iface} up")
    if rc == 0:
        print(f"✅ Interface {iface} is now up")
        return True
    else:
        print(f"❌ Failed to bring {iface} up: {stderr}")
        return False

def fix_routing_table(iface):
    """Add default route to rndis table"""
    print(f"🔧 Adding default route via {iface} to rndis table...")
    stdout, stderr, rc = run_cmd(f"sudo ip route add default dev {iface} table rndis")
    if rc == 0:
        print(f"✅ Default route added to rndis table")
        return True
    else:
        print(f"❌ Failed to add route: {stderr}")
        return False

def create_routing_table():
    """Create rndis routing table if it doesn't exist"""
    print("🔧 Creating rndis routing table...")
    stdout, _, rc = run_cmd("grep -q '^101 rndis$' /etc/iproute2/rt_tables")
    if rc != 0:
        stdout, stderr, rc = run_cmd("sudo bash -c 'echo \"101 rndis\" >> /etc/iproute2/rt_tables'")
        if rc == 0:
            print("✅ RNDIS routing table created")
            return True
        else:
            print(f"❌ Failed to create routing table: {stderr}")
            return False
    else:
        print("✅ RNDIS routing table already exists")
        return True

def create_policy_rule():
    """Create policy routing rule if it doesn't exist"""
    print("🔧 Creating policy routing rule...")
    stdout, _, rc = run_cmd("ip rule show | grep -q 'fwmark 0x1'")
    if rc != 0:
        stdout, stderr, rc = run_cmd("sudo ip rule add fwmark 0x1 lookup rndis priority 1001")
        if rc == 0:
            print("✅ Policy routing rule created")
            return True
        else:
            print(f"❌ Failed to create policy rule: {stderr}")
            return False
    else:
        print("✅ Policy routing rule already exists")
        return True

def create_packet_marking():
    """Create packet marking rule if it doesn't exist"""
    print("🔧 Creating packet marking rule...")
    stdout, _, rc = run_cmd("sudo iptables -t mangle -L OUTPUT | grep -q 'owner UID match proxy'")
    if rc != 0:
        stdout, stderr, rc = run_cmd("sudo iptables -t mangle -A OUTPUT -m owner --uid-owner proxy -j MARK --set-mark 1")
        if rc == 0:
            print("✅ Packet marking rule created")
            return True
        else:
            print(f"❌ Failed to create marking rule: {stderr}")
            return False
    else:
        print("✅ Packet marking rule already exists")
        return True

def main():
    print("🔍 RNDIS Interface and Policy Routing Fix Script")
    print("=" * 50)
    
    # Check if running as root
    if os.geteuid() == 0:
        print("⚠️  Warning: Running as root. Consider running as regular user with sudo.")
    
    # Detect RNDIS interface
    print("🔍 Detecting RNDIS interface...")
    iface = detect_rndis_interface()
    if not iface:
        print("❌ No RNDIS interface found (enx* or eth1)")
        print("   Available interfaces:")
        run_cmd("ip -o link show | awk -F': ' '{print $2}' | grep -v lo")
        sys.exit(1)
    
    print(f"✅ Found RNDIS interface: {iface}")
    
    # Check interface status
    print(f"🔍 Checking interface {iface} status...")
    has_ip, is_up = check_interface_status(iface)
    
    if not has_ip:
        print(f"❌ Interface {iface} has no IP address")
        print("   You may need to run the main setup script first")
        sys.exit(1)
    
    print(f"✅ Interface {iface} has IP address")
    
    fixes_needed = []
    
    # Check if interface is up
    if not is_up:
        fixes_needed.append(("interface", iface))
        print(f"⚠️  Interface {iface} is DOWN")
    else:
        print(f"✅ Interface {iface} is UP")
    
    # Check routing table
    print("🔍 Checking rndis routing table...")
    has_route = check_routing_table()
    if not has_route:
        fixes_needed.append(("routing", iface))
        print("⚠️  No default route in rndis table")
    else:
        print("✅ Default route exists in rndis table")
    
    # Check policy rules
    print("🔍 Checking policy routing rules...")
    has_rule = check_policy_rules()
    if not has_rule:
        fixes_needed.append(("policy", None))
        print("⚠️  No policy routing rule for fwmark 0x1")
    else:
        print("✅ Policy routing rule exists")
    
    # Check packet marking
    print("🔍 Checking packet marking rules...")
    stdout, _, rc = run_cmd("sudo iptables -t mangle -L OUTPUT | grep -q 'owner UID match proxy'")
    if rc != 0:
        fixes_needed.append(("marking", None))
        print("⚠️  No packet marking rule for proxy user")
    else:
        print("✅ Packet marking rule exists")
    
    # Apply fixes
    if fixes_needed:
        print(f"\n🔧 Applying {len(fixes_needed)} fixes...")
        
        for fix_type, iface_param in fixes_needed:
            if fix_type == "interface":
                if not fix_interface(iface_param):
                    sys.exit(1)
            elif fix_type == "routing":
                if not create_routing_table():
                    sys.exit(1)
                if not fix_routing_table(iface_param):
                    sys.exit(1)
            elif fix_type == "policy":
                if not create_policy_rule():
                    sys.exit(1)
            elif fix_type == "marking":
                if not create_packet_marking():
                    sys.exit(1)
        
        print("\n✅ All fixes applied successfully!")
    else:
        print("\n✅ No fixes needed - everything is working correctly!")
    
    # Final verification
    print("\n🔍 Final verification...")
    has_ip, is_up = check_interface_status(iface)
    has_route = check_routing_table()
    has_rule = check_policy_rules()
    
    if has_ip and is_up and has_route and has_rule:
        print("🎉 RNDIS proxy routing is fully functional!")
        print(f"   Interface: {iface}")
        print("   Policy routing: ✅")
        print("   Packet marking: ✅")
        print("\n🧪 Test your proxy:")
        print(f"   curl -x http://192.168.1.37:3128 https://api.ipify.org")
    else:
        print("❌ Some issues remain after fixes")
        sys.exit(1)

if __name__ == "__main__":
    main()
