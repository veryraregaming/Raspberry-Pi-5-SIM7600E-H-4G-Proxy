#!/bin/bash
# Raspberry Pi 5 + SIM7600E-H 4G Proxy - One Command Setup

set -e

echo "🚀 Raspberry Pi 5 + SIM7600E-H 4G Proxy Setup"
echo "=============================================="

# Check if running as root
if [ "$EUID" -ne 0 ]; then
    echo "❌ This script must be run as root (use sudo)"
    echo "Usage: sudo bash run.sh"
    exit 1
fi

# Update system
echo "📦 Updating system packages..."
apt update

# Install Python dependencies
echo "🐍 Installing Python dependencies..."
apt install -y python3 python3-pip python3-yaml python3-serial python3-requests python3-flask curl

# Install Node.js and PM2
echo "🔧 Installing Node.js and PM2..."
curl -fsSL https://deb.nodesource.com/setup_18.x | bash -
apt install -y nodejs
npm install -g pm2

# Install 3proxy from source
echo "🔧 Installing 3proxy from source..."
apt install -y build-essential wget unzip
cd /tmp
wget https://github.com/z3APA3A/3proxy/archive/refs/heads/master.zip
unzip -o master.zip
cd 3proxy-master
make -f Makefile.Linux
cp bin/3proxy /usr/local/bin/
chmod +x /usr/local/bin/3proxy

# Install iptables
echo "🌐 Installing network tools..."
apt install -y iptables

# Return to script's directory (works from anywhere)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
echo "📁 Working directory: $(pwd)"

# Make main.py executable
chmod +x main.py

# Run the main setup
echo "🚀 Starting automated setup..."
python3 main.py

echo "✅ Setup complete! Your 4G proxy is ready to use."
