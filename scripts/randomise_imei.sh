#!/bin/bash
# Randomise IMEI for 4G modem
# This script generates a random IMEI and sets it on the modem

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Find modem device
MODEM_DEV="/dev/ttyUSB2"
if [ ! -e "$MODEM_DEV" ]; then
    echo -e "${YELLOW}⚠️  /dev/ttyUSB2 not found, trying /dev/ttyUSB0${NC}"
    MODEM_DEV="/dev/ttyUSB0"
fi

if [ ! -e "$MODEM_DEV" ]; then
    echo -e "${RED}❌ No modem device found${NC}"
    exit 1
fi

# Generate random IMEI: 35000000 + 8 random digits
RANDOM_SUFFIX=$(shuf -i 10000000-99999999 -n 1)
RANDOM_IMEI="35000000${RANDOM_SUFFIX}"

echo -e "${GREEN}📱 Setting new IMEI: ${RANDOM_IMEI}${NC}"

# Set new IMEI
echo -e "AT+EGMR=1,7,\"$RANDOM_IMEI\"\r" > "$MODEM_DEV"
sleep 2

# Reset modem to apply
echo -e "${YELLOW}📡 Rebooting modem to apply new IMEI...${NC}"
echo -e "AT+CFUN=1,1\r" > "$MODEM_DEV"

echo -e "${YELLOW}⏱️  Waiting 30 seconds for modem to reboot...${NC}"
sleep 30

echo -e "${GREEN}✅ Done! New IMEI should be active: ${RANDOM_IMEI}${NC}"
echo -e "${GREEN}🔄 You can now reconnect to get a new IP address${NC}"

