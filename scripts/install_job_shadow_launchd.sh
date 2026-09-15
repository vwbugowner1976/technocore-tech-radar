#!/bin/bash
set -euo pipefail

LABEL="com.vwbugowner.technoscout-jobshadow"
REPO="${HOME}/technocore-tech-radar"
PYTHON="${REPO}/.venv/bin/python"
PLIST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
LOG_DIR="${REPO}/logs"

if [[ ! -x "${PYTHON}" ]]; then
  echo "missing virtualenv python: ${PYTHON}" >&2
  exit 1
fi
if [[ ! -f "${REPO}/job_shadow_runner.py" ]]; then
  echo "missing ${REPO}/job_shadow_runner.py; run git pull first" >&2
  exit 1
fi

mkdir -p "${HOME}/Library/LaunchAgents" "${LOG_DIR}"

cat > "${PLIST}" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${PYTHON}</string>
    <string>${REPO}/job_shadow_runner.py</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${REPO}</string>
  <key>RunAtLoad</key>
  <true/>
  <key>StartInterval</key>
  <integer>60</integer>
  <key>StandardOutPath</key>
  <string>${LOG_DIR}/job-shadow.log</string>
  <key>StandardErrorPath</key>
  <string>${LOG_DIR}/job-shadow-error.log</string>
  <key>ProcessType</key>
  <string>Background</string>
</dict>
</plist>
PLIST

plutil -lint "${PLIST}"
launchctl bootout "gui/$(id -u)" "${PLIST}" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "${PLIST}"
launchctl enable "gui/$(id -u)/${LABEL}"
launchctl kickstart -k "gui/$(id -u)/${LABEL}"

echo "installed ${LABEL}"
echo "status: launchctl print gui/$(id -u)/${LABEL} | head -40"
echo "jobs:   cd ${REPO} && .venv/bin/python job_shadow.py status --limit 20"
echo "logs:   tail -f ${LOG_DIR}/job-shadow.log"
