#!/bin/bash
set -euo pipefail

LABEL="com.vwbugowner.technoscout-jobrefiner"
REPO="$HOME/technocore-tech-radar"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PY="$REPO/.venv/bin/python"

if [[ ! -x "$PY" ]]; then
  echo "missing python: $PY" >&2
  exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents" "$REPO/logs"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>$REPO/job_refined_watcher.py</string>
    <string>--limit</string>
    <string>1</string>
    <string>--max-age-seconds</string>
    <string>900</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$REPO</string>
  <key>RunAtLoad</key>
  <true/>
  <key>StartInterval</key>
  <integer>60</integer>
  <key>ProcessType</key>
  <string>Background</string>
  <key>StandardOutPath</key>
  <string>$REPO/logs/job-refined-watch.log</string>
  <key>StandardErrorPath</key>
  <string>$REPO/logs/job-refined-watch-error.log</string>
</dict>
</plist>
EOF

plutil -lint "$PLIST"
launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl enable "gui/$(id -u)/$LABEL"
launchctl kickstart "gui/$(id -u)/$LABEL"

echo "installed $LABEL"
echo "status: launchctl print gui/$(id -u)/$LABEL | head -40"
echo "logs:   tail -f $REPO/logs/job-refined-watch.log"
