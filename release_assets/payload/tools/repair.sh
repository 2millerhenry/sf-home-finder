#!/bin/bash
set -euo pipefail

APP_ROOT="${SF_HOUSING_APP_ROOT:-$HOME/Library/Application Support/SF Housing Monitor}"
RELEASE_ROOT="$(cd "$(dirname "$0")/../.." && pwd 2>/dev/null || true)"

if [ -x "$RELEASE_ROOT/payload/install.sh" ]; then
  INSTALLER="$RELEASE_ROOT/payload/install.sh"
elif [ -n "${SF_HOUSING_RELEASE_ROOT:-}" ] && [ -x "$SF_HOUSING_RELEASE_ROOT/payload/install.sh" ]; then
  RELEASE_ROOT="$SF_HOUSING_RELEASE_ROOT"
  INSTALLER="$SF_HOUSING_RELEASE_ROOT/payload/install.sh"
else
  printf 'Repair needs the downloaded SF Home Finder folder. Open it and double-click Repair there.\n'
  exit 1
fi

# Found the way install.sh finds it, so Repair stops the service the install
# started rather than one it has to guess at.
if [ "${SF_HOUSING_NO_LAUNCH_AGENT:-0}" = "1" ]; then
  DEFAULT_LAUNCH_AGENTS_DIR="$APP_ROOT/LaunchAgents"
else
  DEFAULT_LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
fi
PLIST_PATH="${SF_HOUSING_LAUNCH_AGENTS_DIR:-$DEFAULT_LAUNCH_AGENTS_DIR}/com.sfhousing.monitor.plist"

service() {
  [ "${SF_HOUSING_NO_LAUNCH_AGENT:-0}" = "1" ] && return 0
  [ -f "$PLIST_PATH" ] || return 0
  case "$1" in
    stop) /bin/launchctl bootout "gui/$(id -u)" "$PLIST_PATH" >/dev/null 2>&1 || true ;;
    start) /bin/launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH" >/dev/null 2>&1 || true ;;
  esac
}

# The app's own process, by the command the login service starts it with --
# this install's, never another copy's. Escaped, because pgrep reads a pattern
# and a home folder may have a + or a bracket in its name.
APP_PROCESS="$(printf '%s' "$APP_ROOT/current/bin/python -m sf_housing serve" | /usr/bin/sed 's/[][\.*^$(){}?+|]/\\&/g')"
app_running() { /usr/bin/pgrep -f "$APP_PROCESS" >/dev/null 2>&1; }

# bootout asks launchd to stop the app and can return before the process has
# gone -- and a restore under a process still writing could lose what it
# writes. So this waits for the process itself, ends it as the Windows Repair
# does if it will not go, and says so if even that fails. Also covers an app
# started some other way, with no login service to stop.
stop_app() {
  service stop
  for attempt in $(/usr/bin/seq 1 40); do
    app_running || return 0
    case "$attempt" in
      1) /usr/bin/pkill -TERM -f "$APP_PROCESS" >/dev/null 2>&1 || true ;;
      20) /usr/bin/pkill -KILL -f "$APP_PROCESS" >/dev/null 2>&1 || true ;;
    esac
    /bin/sleep 0.5
  done
  ! app_running
}

# The data comes first, and it does not wait on the reinstall. Somebody running
# Repair may have no internet, and getting their homes back must not depend on
# downloading a runtime. The rules are the ones in this release's own wheel,
# run by a Python this machine already has: the installed app may be the thing
# that is broken, and repairing an older install must apply these rules rather
# than the ones it shipped with. The rules themselves -- what is copied, what is
# restored, what is pruned -- live in sf_housing/backups.py, the same code the
# Windows Repair and both installers run, and are tested there.
if [ -f "$APP_ROOT/data/housing.sqlite3" ]; then
  PYTHON=""
  for candidate in "$APP_ROOT/current/bin/python" "$APP_ROOT"/python/cpython-*/bin/python3; do
    if [ -x "$candidate" ] && "$candidate" -I -c 'import sqlite3' >/dev/null 2>&1; then
      PYTHON="$candidate"
      break
    fi
  done
  WHEEL="$(/bin/ls "$RELEASE_ROOT"/payload/sf_home_finder-*.whl 2>/dev/null | /usr/bin/head -1 || true)"

  if [ -z "$PYTHON" ] || [ -z "$WHEEL" ]; then
    # Failing toward the data: without a way to run the rules, the database is
    # left exactly as it is rather than touched by something cruder.
    printf 'Could not check your housing data on this machine, so it was left exactly as it is.\n'
  elif ! stop_app; then
    # Stopped first, always: a restore replaces the database file, and a
    # running app could go on writing to the one being replaced.
    printf 'SF Home Finder would not stop, so your housing data was left exactly as it is.\n'
  else
    STATUS=0
    # Isolated (-I), so nothing in the environment or the current folder can
    # stand in for the release's package, and the wheel is put first on the
    # path by hand. The Windows Repair runs this same line.
    "$PYTHON" -I -c 'import sys; sys.path.insert(0, sys.argv[1]); from sf_housing.backups import main; sys.exit(main(sys.argv[2:]))' \
      "$WHEEL" repair "$APP_ROOT" || STATUS=$?
    if [ "$STATUS" -eq 2 ]; then
      # No safety copy could be taken, so nothing else may go ahead: not a
      # restore, not a reinstall that would rewrite the app around the data.
      service start
      exit 2
    fi
    # 3 means the database could not be read and there was nothing good to
    # restore. Nothing was deleted; the reinstall below still fixes the app.
    if [ "$STATUS" -ne 0 ] && [ "$STATUS" -ne 3 ]; then
      # Anything else is the rules themselves failing, and silence would read
      # as "your data is fine".
      printf 'Checking your housing data stopped with an error (shown above); nothing more was done to it.\n'
    fi
  fi
fi

printf 'Repairing application files. Your profile, listings, and connectors will be preserved.\n'
if ! "$INSTALLER" "$(cd "$RELEASE_ROOT" && pwd)"; then
  # Repair stopped the service above; a reinstall that failed before starting
  # it again must not leave the app off until the next login.
  service start
  exit 1
fi
