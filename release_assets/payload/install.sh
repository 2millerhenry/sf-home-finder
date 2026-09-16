#!/bin/bash
set -euo pipefail

RELEASE_ROOT="${1:-$(cd "$(dirname "$0")/../.." && pwd)}"
PAYLOAD_DIR="$RELEASE_ROOT/payload"
VERSION="0.5.6"
PYTHON_VERSION="3.12.10"
PORT="${SF_HOUSING_PORT:-8000}"
APP_ROOT="${SF_HOUSING_APP_ROOT:-$HOME/Library/Application Support/SF Housing Monitor}"
# Isolated validation must not touch the real account's login services. Writing
# the plist to ~/Library/LaunchAgents regardless of this flag meant installing a
# second copy repointed the first one's login service at a temporary directory,
# and the damage only appeared at the next login.
if [ "${SF_HOUSING_NO_LAUNCH_AGENT:-0}" = "1" ]; then
  DEFAULT_LAUNCH_AGENTS_DIR="$APP_ROOT/LaunchAgents"
else
  DEFAULT_LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
fi
LAUNCH_AGENTS_DIR="${SF_HOUSING_LAUNCH_AGENTS_DIR:-$DEFAULT_LAUNCH_AGENTS_DIR}"
LABEL="com.sfhousing.monitor"
PLIST_PATH="$LAUNCH_AGENTS_DIR/$LABEL.plist"
DATA_DIR="$APP_ROOT/data"
LOG_DIR="$APP_ROOT/logs"
TOOLS_DIR="$APP_ROOT/tools"
RUNTIMES_DIR="$APP_ROOT/runtimes"
RELEASES_DIR="$APP_ROOT/releases"
UV_BIN="$PAYLOAD_DIR/uv"
LOCK_FILE="$PAYLOAD_DIR/requirements.lock"
WHEEL_FILE="$PAYLOAD_DIR/sf_home_finder-0.5.6-py3-none-any.whl"

say() { printf '%s\n' "$*"; }
fail() { say "Installation stopped: $*"; exit 1; }

if [ "$(uname -s)" != "Darwin" ]; then
  fail "this release supports macOS only."
fi
if [ "$(uname -m)" != "arm64" ]; then
  fail "this build supports Apple Silicon Macs only. Ask for an Intel build instead of forcing this one."
fi
# A ZIP downloaded through a browser marks every extracted file with
# com.apple.quarantine, so macOS would otherwise re-prompt for Open, Repair,
# Verify and Uninstall one at a time. The user has already approved this
# release by opening the installer, so clear the flag once for the whole
# folder. This is never fatal: an unsigned build still installs without it.
/usr/bin/xattr -dr com.apple.quarantine "$RELEASE_ROOT" >/dev/null 2>&1 || true

for required in "$UV_BIN" "$LOCK_FILE" "$WHEEL_FILE" "$PAYLOAD_DIR/checksums.sha256"; do
  [ -f "$required" ] || fail "the release is incomplete ($(basename "$required") is missing). Download the ZIP again."
done
(cd "$PAYLOAD_DIR" && /usr/bin/shasum -a 256 -c checksums.sha256 >/dev/null) || fail "release verification failed. Download the ZIP again."

if /usr/sbin/lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  HEALTH="$(/usr/bin/curl -fsS --max-time 2 "http://127.0.0.1:$PORT/health" 2>/dev/null || true)"
  case "$HEALTH" in
    *'"app":"sf-home-finder"'*|*'"app": "sf-home-finder"'*|*'"ok":true'*|*'"ok": true'*) ;;
    *) fail "port $PORT is already used by another app. Close that app, then run Install again. Nothing was stopped." ;;
  esac
fi

say "Installing SF Home Finder $VERSION..."
/bin/mkdir -p "$DATA_DIR/config" "$LOG_DIR" "$TOOLS_DIR" "$RUNTIMES_DIR" "$RELEASES_DIR" "$LAUNCH_AGENTS_DIR"
/bin/chmod 700 "$APP_ROOT" "$DATA_DIR" "$LOG_DIR"

STAGE="$(/usr/bin/mktemp -d "$APP_ROOT/.install.XXXXXX")"
cleanup() { /bin/rm -rf "$STAGE"; }
trap cleanup EXIT

say "Preparing the private Python runtime (the first install needs internet access)..."
export UV_CACHE_DIR="$APP_ROOT/cache"
export UV_PYTHON_INSTALL_DIR="$APP_ROOT/python"
"$UV_BIN" python install "$PYTHON_VERSION" --install-dir "$UV_PYTHON_INSTALL_DIR" --no-bin --no-progress --quiet
"$UV_BIN" venv "$STAGE/runtime" --python "$PYTHON_VERSION" --managed-python --no-project --quiet
"$UV_BIN" pip sync "$LOCK_FILE" --python "$STAGE/runtime/bin/python" --strict --no-progress --quiet
"$UV_BIN" pip install "$WHEEL_FILE" --python "$STAGE/runtime/bin/python" --no-deps --no-progress --quiet
"$STAGE/runtime/bin/python" -I -c 'import sf_housing; assert sf_housing.__version__ == "0.5.6"'

if [ -f "$DATA_DIR/housing.sqlite3" ] && [ -x "$APP_ROOT/current/bin/python" ]; then
  /bin/mkdir -p "$APP_ROOT/backups"
  STAMP="$(/bin/date +%Y%m%d-%H%M%S)"
  "$APP_ROOT/current/bin/python" -c 'import sqlite3,sys; source=sqlite3.connect(sys.argv[1]); target=sqlite3.connect(sys.argv[2]); source.backup(target); target.close(); source.close()' "$DATA_DIR/housing.sqlite3" "$APP_ROOT/backups/housing-$STAMP.sqlite3"
fi
if [ "${SF_HOUSING_NO_LAUNCH_AGENT:-0}" != "1" ] && [ -f "$PLIST_PATH" ]; then
  /bin/launchctl bootout "gui/$(id -u)" "$PLIST_PATH" >/dev/null 2>&1 || true
fi

RUNTIME_TARGET="$RUNTIMES_DIR/$VERSION"
if [ -e "$RUNTIME_TARGET" ]; then
  /bin/mv "$RUNTIME_TARGET" "$STAGE/previous-runtime"
fi
/bin/mv "$STAGE/runtime" "$RUNTIME_TARGET"
/bin/ln -sfn "$RUNTIME_TARGET" "$APP_ROOT/current"

/bin/mkdir -p "$RELEASES_DIR/$VERSION"
/bin/cp "$WHEEL_FILE" "$LOCK_FILE" "$RELEASES_DIR/$VERSION/"
/bin/cp "$PAYLOAD_DIR/tools/"*.sh "$TOOLS_DIR/"
/bin/chmod 700 "$TOOLS_DIR/"*.sh

# A terminal command, for anybody who would rather type than hunt for a
# bookmark -- and the only way in for somebody who installed with the one-line
# command and so has no folder of shortcuts. ~/.local/bin because it needs no
# administrator: the whole install stays password-free.
# Isolated validation must not touch the real account's command, for the same
# reason it must not touch its login services: ~/.local/bin is shared, so a
# throwaway install writing there -- or a throwaway uninstall removing it --
# reaches straight into the installation somebody actually uses.
if [ "${SF_HOUSING_NO_LAUNCH_AGENT:-0}" = "1" ]; then
  CLI_DIR="$APP_ROOT/bin"
else
  CLI_DIR="$HOME/.local/bin"
fi
CLI_PATH="$CLI_DIR/homefinder"
/bin/mkdir -p "$CLI_DIR"
# 0.4.3 shipped this under the wrong name -- the app has always been Home
# Finder. Anybody who installed that has a housefinder sitting in the same
# directory, and leaving it there means two commands where one is stale.
if [ "${SF_HOUSING_NO_LAUNCH_AGENT:-0}" != "1" ] && [ -f "$CLI_DIR/housefinder" ]; then
  /bin/rm -f "$CLI_DIR/housefinder"
fi
# Written beside the command and renamed over it, never copied onto it. cp
# truncates and rewrites the same file, and bash reads a script incrementally
# as it runs -- so an update started by typing `homefinder update` would be
# rewriting the very file it was still reading, and would carry on executing
# whatever bytes landed at the offset it had reached. Renaming leaves the
# running command holding the old file until it finishes.
CLI_STAGED="$CLI_DIR/.homefinder.$$.tmp"
if /bin/cp "$TOOLS_DIR/homefinder.sh" "$CLI_STAGED" 2>/dev/null &&
   /bin/chmod 755 "$CLI_STAGED" 2>/dev/null &&
   /bin/mv -f "$CLI_STAGED" "$CLI_PATH" 2>/dev/null; then
  CLI_READY=1
  case ":$PATH:" in
    *":$CLI_DIR:"*) CLI_ON_PATH=1 ;;
    *) CLI_ON_PATH=0 ;;
  esac
else
  /bin/rm -f "$CLI_STAGED" 2>/dev/null || true
  CLI_READY=0
  CLI_ON_PATH=0
fi
if [ -d "$PAYLOAD_DIR/furnished-finder-bridge" ]; then
  /bin/rm -rf "$APP_ROOT/furnished-finder-bridge.new"
  /bin/cp -R "$PAYLOAD_DIR/furnished-finder-bridge" "$APP_ROOT/furnished-finder-bridge.new"
  /bin/rm -rf "$APP_ROOT/furnished-finder-bridge"
  /bin/mv "$APP_ROOT/furnished-finder-bridge.new" "$APP_ROOT/furnished-finder-bridge"
fi

if [ -f "$PAYLOAD_DIR/gmail-client-secret.json" ] && [ ! -f "$DATA_DIR/gmail-client-secret.json" ]; then
  /bin/cp "$PAYLOAD_DIR/gmail-client-secret.json" "$DATA_DIR/gmail-client-secret.json"
  /bin/chmod 600 "$DATA_DIR/gmail-client-secret.json"
fi

PLIST_TMP="$STAGE/$LABEL.plist"
/bin/cat > "$PLIST_TMP" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$APP_ROOT/current/bin/python</string>
    <string>-m</string><string>sf_housing</string><string>serve</string>
    <string>--host</string><string>127.0.0.1</string>
    <string>--port</string><string>$PORT</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict><key>SF_HOUSING_DATA_DIR</key><string>$DATA_DIR</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$LOG_DIR/service.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/service-error.log</string>
  <key>ProcessType</key><string>Background</string>
</dict>
</plist>
PLIST
/usr/bin/plutil -lint "$PLIST_TMP" >/dev/null
/bin/cp "$PLIST_TMP" "$PLIST_PATH"
/bin/chmod 600 "$PLIST_PATH"

if [ "${SF_HOUSING_NO_LAUNCH_AGENT:-0}" = "1" ]; then
  say "LaunchAgent installation skipped for isolated validation."
  SF_HOUSING_APP_ROOT="$APP_ROOT" SF_HOUSING_PORT="$PORT" \
    SF_HOUSING_LAUNCH_AGENTS_DIR="$LAUNCH_AGENTS_DIR" "$TOOLS_DIR/open.sh" --no-browser
else
  /bin/launchctl bootout "gui/$(id -u)" "$PLIST_PATH" >/dev/null 2>&1 || true
  /bin/launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH" || fail "macOS could not start the login service. Run Repair and use the Desktop log if it repeats."
  /bin/launchctl enable "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
  /bin/launchctl kickstart -k "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
fi

say "Starting it up..."
# launchd throttles a restart to ThrottleInterval, ten seconds, and a first
# start on a full board spends about fifteen more re-ranking what is already
# stored. Two throttled attempts and that is a minute gone, which is why this
# waits well past what a healthy start needs rather than the 45 seconds it used
# to: the old window was tight enough that a normal install could miss it.
HEALTHY=0
for attempt in $(/usr/bin/seq 1 150); do
  HEALTH="$(/usr/bin/curl -fsS --max-time 2 "http://127.0.0.1:$PORT/health" 2>/dev/null || true)"
  case "$HEALTH" in
    *'"app":"sf-home-finder"'*|*'"app": "sf-home-finder"'*|*'"ok":true'*|*'"ok": true'*) HEALTHY=1; break ;;
  esac
  /bin/sleep 1
done

if [ "$HEALTHY" != 1 ]; then
  # The install itself finished: the runtime is in place and the login service
  # is registered. Only the first response is late. Saying "installation
  # stopped" here sent people to Repair for an app that was seconds from
  # answering, so this says what is actually true and leaves the previous
  # runtime in place as the way back.
  say ""
  say "Installed, but it has not answered yet. It is probably still starting."
  say ""
  say "  Wait a minute, then open:  http://127.0.0.1:$PORT/"
  say "  Still nothing? Double-click: Repair SF Home Finder"
  say "  What went wrong is logged in: $LOG_DIR/service-error.log"
  say ""
  say "Your profile and history are safe in: $DATA_DIR"
  exit 0
fi

# Deliberately last, and only past the health check above: until the new
# runtime has actually served a request, the old one is the way back.
if [ -x "$TOOLS_DIR/reclaim.sh" ]; then
  SF_HOUSING_APP_ROOT="$APP_ROOT" SF_HOUSING_RECLAIM_QUIET=1 "$TOOLS_DIR/reclaim.sh" || true
fi

# Somebody who installed with the one-line command has no folder of .command
# files to go back to, so the way back has to be said out loud rather than
# assumed. It is always the same: the login service keeps it running, so the
# address works whenever they want it.
say ""
say "Done. SF Home Finder is running."
say ""
say "  Open it any time:   http://127.0.0.1:$PORT/     <- bookmark this"
if [ "${CLI_READY:-0}" = 1 ] && [ "${CLI_ON_PATH:-0}" = 1 ]; then
  say "  Or from a terminal: homefinder            (homefinder help for more)"
fi
say "  It checks for you:  10:00 and 18:00, every day, on its own"
say "  Your data lives in: $DATA_DIR"
say ""
if [ "${CLI_READY:-0}" = 1 ] && [ "${CLI_ON_PATH:-0}" != 1 ]; then
  # Telling somebody to run a line is not the same as it getting run. The first
  # person to install this on another Mac typed homefinder, got "command not
  # found", and never saw the step -- so this does it for them.
  #
  # Into the file their own shell actually reads. The message used to name
  # ~/.zshrc unconditionally, which on a bash login shell is a file nothing
  # opens, and that is exactly the machine it failed on.
  case "${SHELL:-}" in
    */bash) PROFILE="$HOME/.bash_profile" ;;
    */fish) PROFILE="" ;;
    *)      PROFILE="$HOME/.zshrc" ;;
  esac
  PATH_LINE='export PATH="$HOME/.local/bin:$PATH"'
  if [ -n "$PROFILE" ] && ! /usr/bin/grep -qF '.local/bin' "$PROFILE" 2>/dev/null; then
    /bin/mkdir -p "$(/usr/bin/dirname "$PROFILE")"
    printf '\n# Added by SF Home Finder so the homefinder command can be found.\n%s\n' \
      "$PATH_LINE" >> "$PROFILE"
  fi
  # A line in a profile is read by the next shell, not this one. Saying "open a
  # new Terminal window" is a step somebody has to remember minutes later, and
  # the first two people to install this typed homefinder in the window they
  # had just used, got "command not found", and stopped. So both ways that work
  # right now are on screen, and the profile line only has to matter later.
  say "The 'homefinder' command lives in $CLI_DIR, which this Terminal window"
  say "does not know about yet."
  say ""
  # The profile above is the file this shell reads first. The others it may
  # read, fish's own syntax, a stale homefinder earlier on the PATH, and a
  # check that a new shell can really find the command rather than a promise
  # that it will, are one job with its own tool -- which Repair can run too,
  # and which skips any file that already knows the way.
  if [ -f "$PAYLOAD_DIR/tools/put-command-on-path.sh" ]; then
    /bin/bash "$PAYLOAD_DIR/tools/put-command-on-path.sh" "$CLI_DIR" || true
    say ""
  fi
  if [ -n "$PROFILE" ]; then
    say "  In this window:     source $PROFILE"
    say "  Or run it directly: $CLI_DIR/homefinder"
    say "  Any new window already knows it."
  else
    say "  Run it directly:    $CLI_DIR/homefinder"
    say "  Or put $CLI_DIR on your PATH."
  fi
  say ""
fi
if [ "${SF_HOUSING_NO_BROWSER:-0}" != "1" ]; then
  /usr/bin/open "http://127.0.0.1:$PORT/"
fi
