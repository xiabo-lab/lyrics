#!/usr/bin/env bash
# wifi.sh — point the Pi at a new Wi-Fi network.
#
#   1. Edit the SSID and PASSWORD lines just below.
#   2. Run it:   ./wifi.sh        (it will ask for your sudo password)
#
# NetworkManager SAVES every network it joins and auto-reconnects on boot, so
# you only need to run this once per new network. Adding a network does NOT
# delete the others — the Pi will auto-pick whichever known network is in
# range (e.g. home Wi-Fi at home, your phone hotspot in the car).
# ---------------------------------------------------------------------------

# ====== EDIT THESE TWO LINES ======
SSID=""
PASSWORD=""
# ==================================

# Hidden network (SSID not broadcast)? Set to "yes". Otherwise leave "no".
HIDDEN="no"

set -euo pipefail

# nmcli needs root to change connections — re-run ourselves under sudo.
if [ "$(id -u)" -ne 0 ]; then
    exec sudo -- "$0" "$@"
fi

if [ -z "$SSID" ]; then
    echo "ERROR: open this file and set SSID (and PASSWORD) at the top first."
    echo "       nano ~/wifi.sh"
    exit 1
fi

echo "Currently connected to: $(iwgetid -r 2>/dev/null || nmcli -t -f NAME connection show --active | head -n1 || echo '(none)')"
echo "Scanning for '$SSID'…"
nmcli device wifi rescan >/dev/null 2>&1 || true
sleep 2

# Build the connect command; only pass a password for secured networks.
args=(device wifi connect "$SSID")
[ -n "$PASSWORD" ] && args+=(password "$PASSWORD")
[ "$HIDDEN" = "yes" ] && args+=(hidden yes)

echo "Connecting to '$SSID'…"
if nmcli "${args[@]}"; then
    sleep 2
    ip=$(hostname -I | awk '{print $1}')
    echo
    echo "✅ Connected to '$SSID'."
    echo "   IP address: ${ip:-<pending>}"
    echo "   SSH in with:  ssh fuwenxu@${ip:-carlyric.local}"
else
    echo
    echo "❌ Could not connect to '$SSID'."
    echo "   Check the SSID/PASSWORD spelling, and that the network is in range."
    echo "   Your previous connection should still be active."
    nmcli device wifi list | head -n 12
    exit 1
fi
