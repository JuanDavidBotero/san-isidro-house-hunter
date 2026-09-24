#!/usr/bin/env bash
# Install (or remove) the daily launchd schedule for the San Isidro radar.
#
#   ./install-schedule.sh              install at the default 08:00 local time
#   ./install-schedule.sh --at 7:30    install at a specific local time
#   ./install-schedule.sh --uninstall  remove the schedule
#   ./install-schedule.sh --status     show whether it is loaded and when it last ran
#   ./install-schedule.sh --run-now    trigger a run immediately (for testing)
#
# Why launchd and not cron: launchd runs a job that was missed while the Mac was
# asleep or powered off, as soon as it wakes. cron silently skips it. For a daily
# radar on a laptop that is the difference between working and quietly not working.

set -euo pipefail
cd "$(dirname "$0")"

REPO="$(pwd)"
LABEL="com.juanbotero.san-isidro-radar"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
HOUR=8
MINUTE=0
ACTION="install"

while [ $# -gt 0 ]; do
  case "$1" in
    --at)
      IFS=':' read -r HOUR MINUTE <<< "${2:-}"
      MINUTE="${MINUTE:-0}"
      [ -n "$HOUR" ] || { echo "error: --at needs HH or HH:MM" >&2; exit 2; }
      shift 2 ;;
    --uninstall) ACTION="uninstall"; shift ;;
    --status)    ACTION="status";    shift ;;
    --run-now)   ACTION="run-now";   shift ;;
    *) echo "error: unknown argument '$1'" >&2; exit 2 ;;
  esac
done

case "$ACTION" in
  uninstall)
    launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || launchctl unload "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
    echo "Removed the daily schedule. History in data/ is untouched."
    exit 0 ;;
  status)
    if launchctl print "gui/$(id -u)/${LABEL}" >/dev/null 2>&1; then
      echo "Schedule is LOADED."
      launchctl print "gui/$(id -u)/${LABEL}" | grep -E "last exit code|runs|state" || true
    else
      echo "Schedule is NOT loaded. Install it with ./install-schedule.sh"
    fi
    echo
    echo "Recent log (logs/radar.log):"
    tail -n 20 logs/radar.log 2>/dev/null || echo "  (no log yet)"
    exit 0 ;;
  run-now)
    launchctl kickstart -p "gui/$(id -u)/${LABEL}" 2>/dev/null \
      || launchctl start "${LABEL}" 2>/dev/null \
      || { echo "error: schedule is not loaded; install it first" >&2; exit 1; }
    echo "Triggered a run. Follow it with: tail -f logs/radar.log"
    exit 0 ;;
esac

# --- install ---------------------------------------------------------------

if [ ! -f .env ]; then
  echo "warning: no .env file. The scheduled job inherits almost no environment," >&2
  echo "         so without .env it will have no credentials and will fail." >&2
  echo "         cp .env.example .env && chmod 600 .env" >&2
  echo >&2
fi

mkdir -p logs
chmod +x run.sh

cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${LABEL}</string>

  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>${REPO}/run.sh</string>
  </array>

  <key>WorkingDirectory</key>
  <string>${REPO}</string>

  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key><integer>${HOUR}</integer>
    <key>Minute</key><integer>${MINUTE}</integer>
  </dict>

  <!-- Run a missed job once the Mac wakes, instead of skipping the day. -->
  <key>RunAtLoad</key>
  <false/>

  <key>StandardOutPath</key>
  <string>${REPO}/logs/radar.log</string>
  <key>StandardErrorPath</key>
  <string>${REPO}/logs/radar.log</string>

  <!-- A launchd job starts with a minimal PATH. Homebrew python lives in
       /opt/homebrew/bin, which is not on the default PATH, and run.sh probes for a
       usable interpreter along it. -->
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>

  <key>ProcessType</key>
  <string>Background</string>
</dict>
</plist>
PLIST_EOF

# bootout first so re-running this script reloads cleanly instead of erroring.
launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || launchctl load "$PLIST"

printf 'Installed: the radar runs daily at %02d:%02d local time.\n' "$HOUR" "$MINUTE"
echo
echo "  Verify:   ./install-schedule.sh --status"
echo "  Test now: ./install-schedule.sh --run-now"
echo "  Logs:     tail -f logs/radar.log"
echo "  Remove:   ./install-schedule.sh --uninstall"
echo
echo "Note: this uses your Mac's LOCAL time zone, not Argentina time. In Amsterdam"
echo "08:00 local is 03:00 or 04:00 in Buenos Aires, which is fine for a daily sweep"
echo "of overnight listings. Re-run with --at to change it."
