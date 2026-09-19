#!/bin/bash
set -euo pipefail

APP_ROOT="${SF_HOUSING_APP_ROOT:-$HOME/Library/Application Support/SF Housing Monitor}"
PORT="${SF_HOUSING_PORT:-8000}"
LABEL="com.sfhousing.monitor"
PLIST_PATH="${SF_HOUSING_LAUNCH_AGENTS_DIR:-$HOME/Library/LaunchAgents}/$LABEL.plist"
URL="http://127.0.0.1:$PORT/"

health() {
  RESPONSE="$(/usr/bin/curl -fsS --max-time 2 "${URL}health" 2>/dev/null || true)"
  case "$RESPONSE" in
    *'"app":"sf-home-finder"'*|*'"app": "sf-home-finder"'*|*'"ok":true'*|*'"ok": true'*) return 0 ;;
    *) return 1 ;;
  esac
}

if ! health; then
  if /usr/sbin/lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    MESSAGE="Port $PORT is being used by another app. SF Home Finder did not stop it. Close that app, then use Open again."
    printf '%s\n' "$MESSAGE"
    [ "${SF_HOUSING_NO_BROWSER:-0}" = "1" ] || /usr/bin/osascript -e "display dialog \"$MESSAGE\" buttons {\"OK\"} default button \"OK\" with icon caution"
    exit 1
  fi
  if [ "${SF_HOUSING_NO_LAUNCH_AGENT:-0}" = "1" ]; then
    [ -x "$APP_ROOT/current/bin/python" ] || { printf 'The app runtime is missing. Run Repair.\n'; exit 1; }
    # Keep isolated release validation honest: the child process must know it
    # was intentionally started without a macOS LaunchAgent. In normal use the
    # value is 0 and diagnostics verify the real login service as before.
    SF_HOUSING_DATA_DIR="$APP_ROOT/data" SF_HOUSING_NO_LAUNCH_AGENT="${SF_HOUSING_NO_LAUNCH_AGENT:-0}" /usr/bin/nohup "$APP_ROOT/current/bin/python" -m sf_housing serve --host 127.0.0.1 --port "$PORT" >>"$APP_ROOT/logs/service.log" 2>>"$APP_ROOT/logs/service-error.log" &
    printf '%s\n' "$!" > "$APP_ROOT/service.pid"
  else
    [ -f "$PLIST_PATH" ] || { printf 'The login service is missing. Run Repair.\n'; exit 1; }
    /bin/launchctl kickstart -k "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
  fi
  # The same window the installer waits, and for the same reason: a first
  # start re-ranks every stored home before it answers anything, which on a
  # board of 8,583 took 42 seconds. Thirty was short enough that a healthy app
  # on a full board was declared dead and the reader was sent to Repair.
  for attempt in $(/usr/bin/seq 1 150); do
    health && break
    /bin/sleep 1
  done
fi

health || {
  printf 'It has not answered yet and is probably still starting on a full board.\n'
  printf '  Wait a minute, then open: %s\n' "$URL"
  printf '  Still nothing? Double-click Repair SF Home Finder, or read %s/logs.\n' "$APP_ROOT"
  exit 1
}
if [ "${1:-}" != "--no-browser" ] && [ "${SF_HOUSING_NO_BROWSER:-0}" != "1" ]; then
  /usr/bin/open "$URL"
fi
printf 'SF Home Finder is ready at %s\n' "$URL"
