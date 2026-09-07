#!/bin/zsh
set -euo pipefail

LABEL="com.vwbugowner.technoscout"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

launchctl bootout "$DOMAIN" "$PLIST" 2>/dev/null || true
rm -f "$PLIST"
echo "TechnoScout launchd service removed. Local database and logs were left intact."
