#!/bin/zsh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$(command -v python3)"
CONFIG="$ROOT/technoscout.config.json"
LABEL="com.vwbugowner.technoscout"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [[ ! -f "$CONFIG" ]]; then
  echo "Missing $CONFIG"
  echo "Run: cp technoscout.config.example.json technoscout.config.json"
  exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents" "$ROOT/logs" "$ROOT/data"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>$ROOT/technoscout.py</string>
    <string>--config</string>
    <string>$CONFIG</string>
    <string>--loop</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$ROOT</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ProcessType</key>
  <string>Background</string>
  <key>StandardOutPath</key>
  <string>$ROOT/logs/technoscout.log</string>
  <key>StandardErrorPath</key>
  <string>$ROOT/logs/technoscout.err.log</string>
</dict>
</plist>
EOF

DOMAIN="gui/$(id -u)"
launchctl bootout "$DOMAIN" "$PLIST" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$PLIST"
launchctl kickstart -k "$DOMAIN/$LABEL"

echo "TechnoScout started."
echo "Status: launchctl print $DOMAIN/$LABEL"
echo "Log:    tail -f $ROOT/logs/technoscout.log"
