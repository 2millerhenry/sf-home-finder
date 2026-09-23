#!/bin/bash
set -euo pipefail

RELEASE_ROOT="${1:-$(cd "$(dirname "$0")/../.." && pwd)}"
PAYLOAD_DIR="$RELEASE_ROOT/payload"
VERSION="0.5.21"
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
WHEEL_FILE="$PAYLOAD_DIR/sf_home_finder-0.5.21-py3-none-any.whl"

say() { printf '%s\n' "$*"; }
fail() { say "Installation stopped: $*"; exit 1; }

# One line per step: a spinner while it runs, a tick and how long it took when
# it is done. What the step printed is held back unless it fails -- uv names
# all forty-seven libraries it installs, which is a wall of text nobody reads
# and which buried the one line that matters on the one run that went wrong.
#
# Plain lines when stdout is not a terminal, because a spinner written to a
# file is several hundred carriage returns: CI reads this, and so does anybody
# piping the install to a log to send on.
SPIN=(⠋ ⠙ ⠹ ⠸ ⠼ ⠴ ⠦ ⠧ ⠇ ⠏)
STEP_PID=""
STEP_LOG=""
step() {
  local label=$1 limit=$2; shift 2
  local started rc=0 i=0 elapsed=0 percent=
  : >"$STEP_LOG"
  started=$(/bin/date +%s)
  if [ -t 1 ]; then
    "$@" >"$STEP_LOG" 2>&1 &
    STEP_PID=$!
    while /bin/kill -0 "$STEP_PID" 2>/dev/null; do
      elapsed=$(( $(/bin/date +%s) - started ))
      if [ "$elapsed" -ge "$limit" ]; then
        /bin/kill "$STEP_PID" 2>/dev/null || true
        rc=124
        break
      fi
      # A step that knows how far along it is says so; one that does not
      # shows the time it has taken, which is the only honest thing left.
      percent=$(/usr/bin/tail -3 "$STEP_LOG" 2>/dev/null | /usr/bin/sed -n 's/^PROGRESS \([0-9]*\)$/\1/p' | /usr/bin/tail -1)
      if [ -n "$percent" ]; then
        printf '\r\033[K  %-34s %s  %3s%%  %ss' "$label" "${SPIN[$((i % 10))]}" "$percent" "$elapsed"
      else
        printf '\r\033[K  %-34s %s  %ss' "$label" "${SPIN[$((i % 10))]}" "$elapsed"
      fi
      i=$((i + 1))
      /bin/sleep 0.12
    done
    if [ "$rc" = 0 ]; then wait "$STEP_PID" 2>/dev/null || rc=$?; else wait "$STEP_PID" 2>/dev/null || true; fi
    STEP_PID=""
    elapsed=$(( $(/bin/date +%s) - started ))
    if [ "$rc" = 0 ]; then
      printf '\r\033[K  %-34s ✓  %ss\n' "$label" "$elapsed"
    else
      printf '\r\033[K  %-34s ✗  %ss\n' "$label" "$elapsed"
    fi
  else
    say "  $label..."
    "$@" >"$STEP_LOG" 2>&1 || rc=$?
  fi
  if [ "$rc" != 0 ] && [ -s "$STEP_LOG" ]; then /usr/bin/grep -v '^PROGRESS ' "$STEP_LOG" | /usr/bin/tail -6 || true; fi
  return "$rc"
}

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
STEP_LOG="$STAGE/step.log"
cleanup() { /bin/rm -rf "$STAGE"; }
trap cleanup EXIT
# Ctrl-C during a step would otherwise leave the step running and the cursor
# mid-line.
trap 'if [ -n "$STEP_PID" ]; then /bin/kill "$STEP_PID" 2>/dev/null || true; fi; printf "\n"; exit 130' INT

export UV_CACHE_DIR="$APP_ROOT/cache"
export UV_PYTHON_INSTALL_DIR="$APP_ROOT/python"

# Two downloads used to happen behind one unchanging line, which is most of
# why the install felt stalled: a private Python, then every library it needs.
# uv draws perfectly good progress bars and they were being suppressed, so the
# slowest part of the install was also the only part with nothing to watch.
# The steps are numbered for the same reason -- four quiet commands read as
# one hang, four announced ones read as progress.
#
# Every one of them says what to do when it fails, because these are the only
# steps that need the internet and so the only ones that routinely do. Left to
# `set -e` alone the script exited silently -- `uv --quiet` prints nothing at
# all on a failed download, so a dropped connection ended the install with the
# last progress line on screen and no error, no next step and no app. The
# Windows installer has always said this (Invoke-Uv in windows/payload
# install.ps1); this is the same sentence.
UV_FAILED="the private runtime download did not finish. Check your internet connection, then run this again."
say ""
step "Downloading a private Python" 1800 \
  "$UV_BIN" python install "$PYTHON_VERSION" --install-dir "$UV_PYTHON_INSTALL_DIR" --no-bin || fail "$UV_FAILED"
step "Setting up its own environment" 600 \
  "$UV_BIN" venv "$STAGE/runtime" --python "$PYTHON_VERSION" --managed-python --no-project --quiet || fail "$UV_FAILED"
# --compile-bytecode so the libraries are compiled here, at the priority a
# person is watching, rather than by the login service on its first import at
# the background priority launchd gives it.
step "Downloading the libraries it needs" 1800 \
  "$UV_BIN" pip sync "$LOCK_FILE" --python "$STAGE/runtime/bin/python" --strict --compile-bytecode || fail "$UV_FAILED"
step "Installing SF Home Finder" 600 \
  "$UV_BIN" pip install "$WHEEL_FILE" --python "$STAGE/runtime/bin/python" --no-deps --quiet --compile-bytecode || fail "$UV_FAILED"
"$STAGE/runtime/bin/python" -I -c 'import sf_housing; assert sf_housing.__version__ == "0.5.21"' ||
  fail "the installed app did not pass its version check. Download the ZIP again."

# A safety copy of the housing database before anything is replaced, and the
# backups folder kept to a bounded size -- by sf_housing/backups.py, the same
# rules both Repairs and the Windows installer run. It used to be a one-line
# copy with the old runtime's Python: skipped whenever that runtime was the
# thing broken, which is exactly when people reinstall, and never pruned, so
# every install added a full-size copy for good.
#
# With the new runtime's Python, which has just been proved to work, and while
# the old app is still running: the copy reads through the write-ahead log, so
# it is safe beside a running app, and a copy that cannot be taken stops the
# install here with the old app untouched and still serving.
if [ -f "$DATA_DIR/housing.sqlite3" ]; then
  step "Backing up your homes" 900 \
    "$STAGE/runtime/bin/python" -I -m sf_housing.backups protect "$APP_ROOT" ||
    fail "your housing data could not be backed up first, so nothing was changed. The reason is above; free some disk space if it says the disk is full, then run this again."
fi

# Its own step, for the same reason starting it is one: launchd sends the
# running app a signal and waits for it to go, which on an app mid-scan is
# seconds, and spent between two steps they are seconds with a finished tick
# on screen and nothing moving. Never fatal -- a copy that will not stop is
# the port check's problem, further up, and it has already passed.
stop_the_old_copy() {
  /bin/launchctl bootout "gui/$(id -u)" "$PLIST_PATH" >/dev/null 2>&1 || true
}
if [ "${SF_HOUSING_NO_LAUNCH_AGENT:-0}" != "1" ] && [ -f "$PLIST_PATH" ]; then
  step "Stopping the copy you have" 120 stop_the_old_copy || true
fi

# Start-up skips re-ranking when the board carries a mark saying this code and
# this deal already scored it. That mark is maintained by the version that
# wrote it, and a version that predates it cannot maintain anything: install an
# older release, let it re-score the board under its own rules, come back, and
# the mark left by the newer one is still sitting there matching. Retracting it
# on every install is the only point either version is guaranteed to run, and
# it costs one re-score per install against one per launch, which is the whole
# trade.
#
# Here, after the old service has been told to stop, rather than before: a
# running app mid-scan holds the database's write lock, the delete would wait
# out SQLite's five-second default and fail, and `|| true` would swallow it --
# silently skipping the one step that makes a downgrade safe. The timeout is
# for a process still dying when this runs. Written with the new runtime's own
# Python, which has just been proved to import and has not been moved yet, run
# isolated so no PYTHONPATH can break it. Never fatal: a mark left in place
# matters only after a downgrade and back, and failing an install over it would
# cost more than it saves.
if [ -f "$DATA_DIR/housing.sqlite3" ]; then
  "$STAGE/runtime/bin/python" -I -c 'import sqlite3,sys
connection = sqlite3.connect(sys.argv[1], timeout=30)
connection.execute("DELETE FROM scoring_state WHERE name = ?", ("rescore_fingerprint",))
connection.commit()
connection.close()' "$DATA_DIR/housing.sqlite3" >/dev/null 2>&1 || true
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

# The app runs under launchd, which starts it with the plist's environment and
# nothing else -- a shell profile that exports SF_HOUSING_NO_UPDATE_CHECK never
# reaches it, so the switch has to be written into the service itself. Kept
# across upgrades by reading the plist that is already there: an upgrade is run
# without the variable set far more often than not, and silently turning the
# update check back on is exactly the surprise this switch exists to avoid.
NO_UPDATE_CHECK_ENTRY=""
if [ "${SF_HOUSING_NO_UPDATE_CHECK:-0}" = "1" ] ||
  /usr/bin/grep -q "SF_HOUSING_NO_UPDATE_CHECK" "$PLIST_PATH" 2>/dev/null; then
  NO_UPDATE_CHECK_ENTRY="<key>SF_HOUSING_NO_UPDATE_CHECK</key><string>1</string>"
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
  <dict><key>SF_HOUSING_DATA_DIR</key><string>$DATA_DIR</string>$NO_UPDATE_CHECK_ENTRY</dict>
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

# The pass the step above made owed, run here rather than by the service.
#
# Measured on a real 9,615-home board: a newly installed runtime's first
# start takes about fifty seconds, nearly all of it macOS vetting libraries
# it has not seen before and Python compiling them. Inside the login service
# that same work took seventy-three seconds at the background priority
# launchd gives it, and six minutes in the upgrade this fixes, on a Mac busy
# with the install itself. Run here it is one visible step, and the service
# then starts in three and a half seconds.
#
# Never fatal. If it cannot run, the service does the same work on its first
# start, exactly as every release before this one did.
step "Getting it ready to start" 1800 \
  /usr/bin/env SF_HOUSING_DATA_DIR="$DATA_DIR" "$RUNTIME_TARGET/bin/python" -I -m sf_housing prepare || true

# Waiting for the first answer. The heavy work is done by now, so a healthy
# start is seconds; the allowance is generous because a Mac busy with Spotlight
# indexing a brand-new runtime can still make it slow, and saying "it did not
# work" to somebody whose app is seconds away is the failure that matters here.
FIRST_ANSWER_SECONDS=180
answers() {
  local deadline=$(( $(/bin/date +%s) + FIRST_ANSWER_SECONDS )) health
  while [ "$(/bin/date +%s)" -lt "$deadline" ]; do
    health="$(/usr/bin/curl -fsS --max-time 2 "http://127.0.0.1:$PORT/health" 2>/dev/null || true)"
    case "$health" in
      *'"app":"sf-home-finder"'*|*'"app": "sf-home-finder"'*|*'"ok":true'*|*'"ok": true'*) return 0 ;;
    esac
    /bin/sleep 1
  done
  return 1
}

# Handing the app to launchd and waiting for its first answer are one step,
# because they are one wait. Stopping and starting a login service takes a
# few seconds of its own, and done outside a step it spent them with a
# finished tick on screen and nothing moving -- which reads as an installer
# that has hung, at the moment somebody is watching it hardest. Every second
# of the install now sits inside a step that is counting.
SERVICE_REFUSED=3
start_it() {
  if [ "${SF_HOUSING_NO_LAUNCH_AGENT:-0}" = "1" ]; then
    say "LaunchAgent installation skipped for isolated validation."
    SF_HOUSING_APP_ROOT="$APP_ROOT" SF_HOUSING_PORT="$PORT" \
      SF_HOUSING_LAUNCH_AGENTS_DIR="$LAUNCH_AGENTS_DIR" "$TOOLS_DIR/open.sh" --no-browser ||
      return "$SERVICE_REFUSED"
  else
    /bin/launchctl bootout "gui/$(id -u)" "$PLIST_PATH" >/dev/null 2>&1 || true
    /bin/launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH" || return "$SERVICE_REFUSED"
    /bin/launchctl enable "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
    /bin/launchctl kickstart -k "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
  fi
  answers
}

# The refusal is told apart from the wait running out: launchd declining the
# service is the installer's fault and stops it, while an app that has not
# answered yet is the advice below, which has always been the friendlier of
# the two and is usually right.
HEALTHY=0
step "Starting it up" "$(( FIRST_ANSWER_SECONDS + 60 ))" start_it
case $? in
  0) HEALTHY=1 ;;
  "$SERVICE_REFUSED") fail "macOS could not start the login service. Run Repair and use the Desktop log if it repeats." ;;
esac

if [ "$HEALTHY" != 1 ]; then
  # The install itself finished: the runtime is in place and the login service
  # is registered. Only the first response is late. Saying "installation
  # stopped" here sent people to Repair for an app that was seconds from
  # answering, so this says what is actually true and leaves the previous
  # runtime in place as the way back.
  say ""
  say "Installed, but it has not answered yet. It is probably still starting."
  say ""
  say "  Give it a minute, then open:  http://127.0.0.1:$PORT/   (⌘-click it)"
  say "  Still nothing? Run:           $CLI_PATH repair"
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
