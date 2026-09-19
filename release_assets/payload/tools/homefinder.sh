#!/bin/bash
# homefinder -- open and look after SF Home Finder from a terminal.
#
# Installed to ~/.local/bin/homefinder. Everything here delegates to the tools
# beside the app itself, so this stays a way in rather than a second
# implementation of anything.
set -euo pipefail

APP_ROOT="${SF_HOUSING_APP_ROOT:-$HOME/Library/Application Support/SF Housing Monitor}"
PORT="${SF_HOUSING_PORT:-8000}"
URL="http://127.0.0.1:$PORT/"
TOOLS="$APP_ROOT/tools"
LABEL="com.sfhousing.monitor"
# Where this copy's login service would live. install.sh works it out exactly
# this way -- defaulting inside the app folder when the install was told to keep
# off the account's LaunchAgents -- so reading it any other way here would be
# looking somewhere the install never wrote.
if [ "${SF_HOUSING_NO_LAUNCH_AGENT:-0}" = "1" ]; then
  DEFAULT_LAUNCH_AGENTS_DIR="$APP_ROOT/LaunchAgents"
else
  DEFAULT_LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
fi
LAUNCH_AGENTS_DIR="${SF_HOUSING_LAUNCH_AGENTS_DIR:-$DEFAULT_LAUNCH_AGENTS_DIR}"
PLIST_PATH="$LAUNCH_AGENTS_DIR/$LABEL.plist"
# The app carries its own Python, so reading a JSON field never depends on what
# the machine happens to have.
PY="$APP_ROOT/current/bin/python"

say() { printf '%s\n' "$*"; }
# Anything that went wrong goes to stderr, so a person piping or logging this
# still sees it and a script can tell the two apart.
err() { printf '%s\n' "$*" >&2; }
health() { /usr/bin/curl -fsS --max-time 4 "${URL}health" 2>/dev/null || true; }

INSTALL_LINE="curl -fsSL https://github.com/2millerhenry/sf-home-finder/raw/HEAD/install.sh | bash"

# One quiet line, only when there is something to say. Nothing here asks GitHub
# anything: the app itself checks on its own schedule and stores the answer, so
# this reads what is already known and costs a local request. A machine that is
# offline, or told not to check, simply has nothing to report and says nothing.
announce_update() {
  body="$(health)"
  [ -n "$body" ] || return 0
  [ "$(printf '%s' "$body" | field update.available || true)" = "True" ] || return 0
  latest="$(printf '%s' "$body" | field update.latest || true)"
  [ -n "$latest" ] || return 0
  say ""
  say "  $latest is available. Run: homefinder update"
}

# The command lives in ~/.local/bin, outside the app, so it can outlive the app
# it points at -- a copy restored from a backup, a half-finished uninstall, a
# machine the folder was never on. Saying "it may still be starting" to somebody
# who has no app at all sends them to wait for something that is never coming.
installed() { [ -x "$TOOLS/open.sh" ] || [ -x "$PY" ]; }
require_installed() {
  installed && return 0
  err "SF Home Finder is not installed on this Mac."
  err ""
  err "  Install it with:"
  err ""
  err "    $INSTALL_LINE"
  err ""
  err "  (Looked in $APP_ROOT)"
  exit 1
}

field() {
  [ -x "$PY" ] || return 1
  "$PY" -c 'import json,sys
try: d = json.load(sys.stdin)
except Exception: sys.exit(1)
for key in sys.argv[1].split("."):
    d = d.get(key) if isinstance(d, dict) else None
print("" if d is None else d)' "$1" 2>/dev/null
}

# A copy that is being validated, or a second install on the same Mac, has no
# login service of its own: install.sh skips registering one precisely so a
# throwaway install cannot reach the real account's. launchd knows one label per
# account, so anything here that reaches for that label reaches the installation
# somebody actually uses.
isolated() {
  if [ "${SF_HOUSING_NO_LAUNCH_AGENT:-0}" = "1" ]; then
    return 0
  fi
  [ "$LAUNCH_AGENTS_DIR" != "$HOME/Library/LaunchAgents" ]
}

# Restart the copy this command belongs to, the same way install.sh starts one.
restart_isolated() {
  if [ -f "$APP_ROOT/service.pid" ]; then
    pid="$(/bin/cat "$APP_ROOT/service.pid" 2>/dev/null || true)"
    # The same guard the uninstaller uses: a truncated or half-written file
    # must never be handed to kill.
    case "$pid" in
      ''|*[!0-9]*) ;;
      *) /bin/kill "$pid" >/dev/null 2>&1 || true ;;
    esac
  fi
  # open.sh starts nothing while the port still answers, so starting again
  # before the old process has let go is a restart that quietly did not happen
  # -- and leaves the old code serving with nothing to say it did.
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    [ -n "$(health)" ] || break
    /bin/sleep 1
  done
  "$TOOLS/open.sh" --no-browser
}

usage() {
  cat <<USAGE
homefinder -- your San Francisco housing search

  homefinder           open your dashboard
  homefinder update    get the newest version, keeping everything
  homefinder help      show this

Rarely needed:

  homefinder status    is it running, and when does it check next
  homefinder check     look for new homes now (once a day)
  homefinder logs      what it has been doing lately
  homefinder restart   restart it, keeping everything
  homefinder repair    fix it without losing your deal or your homes
  homefinder uninstall remove it (your homes are kept unless you say otherwise)

It runs on its own at $URL. You do not have to start it.
USAGE
}

case "${1:-open}" in
  open|"")
    require_installed
    # open.sh starts the service if it is not running and reports honestly if
    # it cannot. Nothing is forwarded to it: it takes flags of its own, and
    # handing it the word "open" only works by luck today.
    #
    # Run rather than exec'd. exec replaces this shell, so anything worth
    # saying afterwards can only be said before -- and a note about a newer
    # version printed ahead of the app opening reads like part of opening it.
    status=0
    "$TOOLS/open.sh" || status=$?
    [ "$status" -eq 0 ] && announce_update
    exit "$status"
    ;;

  status)
    require_installed
    body="$(health)"
    if [ -z "$body" ]; then
      err "Installed, but not answering at $URL"
      err "  It may still be starting -- give it a minute, then try again."
      err "  If it stays quiet: homefinder restart, then homefinder repair."
      exit 1
    fi
    version="$(printf '%s' "$body" | field version || true)"
    running="$(printf '%s' "$body" | field scan_running || true)"
    state="$(printf '%s' "$body" | field scheduled_checking.state || true)"
    summary="$(printf '%s' "$body" | field scheduled_checking.summary || true)"
    say "Running${version:+ $version} at $URL"
    [ -n "$summary" ] && say "  $summary"
    [ "$running" = "True" ] && say "  Checking right now."
    [ -n "$state" ] && [ "$state" != "current" ] && say "  Schedule: $state"
    # These three lines are each an `&&` chain that reports "there was nothing
    # to print" as a non-zero status, and the last one is the last thing the
    # script runs -- so a perfectly healthy app, whose schedule state IS
    # "current" and so prints no extra line, exited 1 and read as a failure to
    # anything checking the exit code. Say plainly that reaching here is fine.
    exit 0
    ;;

  check|scan)
    require_installed
    # The app refuses a POST that does not come from its own origin, which is
    # what stops a web page you happen to have open from telling your dashboard
    # to do things. This request genuinely is from the local dashboard's origin,
    # so it says so; the browser protection is untouched either way.
    #
    # -f is deliberately absent: a refusal here is an answer worth reading
    # rather than a failure, and with it curl would exit before the reply could
    # be looked at.
    location="$(/usr/bin/curl -sS -o /dev/null -D - --max-time 20 \
      -H "Origin: http://127.0.0.1:$PORT" \
      -X POST -d "return_to=/" "${URL}scan" 2>/dev/null \
      | /usr/bin/sed -n 's/^[Ll]ocation: //p' | tr -d '\r' || true)"
    # A refusal comes back as a redirect carrying its reason: "message" when it
    # is a normal no (already running, or today's check is spent) and "error"
    # when something has to be done first. Both are worth repeating verbatim --
    # they are already written for a person.
    case "$location" in
      *scan=starting*) say "Checking now. Watch it at $URL" ;;
      *message=*|*error=*)
        note="${location#*=}"
        case "$location" in
          *message=*) note="${location#*message=}" ;;
          *error=*) note="${location#*error=}" ;;
        esac
        note="${note%%&*}"; note="${note//+/ }"
        printf '%b\n' "${note//%/\\x}"
        ;;
      "") say "Could not reach it at $URL. Is it running? Try 'homefinder status'." ; exit 1 ;;
      *) say "Asked it to check. Watch it at $URL" ;;
    esac
    ;;

  logs)
    lines="${2:-40}"
    # Checked here rather than left to tail, which answers a typo with
    # "illegal offset -- abc", a sentence about tail and not about anything the
    # person did.
    case "$lines" in
      ''|*[!0-9]*) err "homefinder logs takes a number of lines, for example: homefinder logs 50"; exit 1 ;;
    esac
    log="$APP_ROOT/logs/service.log"
    if [ ! -f "$log" ]; then
      installed || require_installed
      err "Nothing logged yet. It writes to $log once it has run."
      exit 1
    fi
    exec /usr/bin/tail -n "$lines" "$log"
    ;;

  restart)
    require_installed
    # Every other part of this reads the copy it was installed beside. Restart
    # did not: it kickstarted the shared label whatever copy asked, so an
    # isolated one on 8011 bounced the live app on 8000 while it was serving.
    if isolated; then
      [ -x "$TOOLS/open.sh" ] || { err "The tool that starts it is missing from $TOOLS. Try 'homefinder repair'."; exit 1; }
      say "Restarting the copy in $APP_ROOT."
      restart_isolated
    elif [ -f "$PLIST_PATH" ]; then
      /bin/launchctl kickstart -k "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
      say "Restarting. Give it a few seconds, then: homefinder status"
    else
      say "No login service is installed. Try 'homefinder repair'."
      exit 1
    fi
    ;;

  update)
    require_installed
    body="$(health)"
    # Installing restarts the app. Doing that underneath a running check
    # abandons it part way and spends the day's manual check on nothing.
    if [ "$(printf '%s' "$body" | field scan_running || true)" = "True" ]; then
      err "A check for new homes is running right now."
      err "  Updating restarts the app, which would interrupt it."
      err "  Try again in a few minutes: homefinder status shows when it finishes."
      exit 1
    fi
    before="$(printf '%s' "$body" | field version || true)"
    say "Updating SF Home Finder${before:+ from $before}."
    say "Your homes, your deal and your notes are kept."
    say ""
    # The same published command a new install uses. It finds the newest
    # release, checks every file in it against a checksum and installs over the
    # top, so nothing here has to know what the newest version is or how to
    # fetch it -- there is one installer, and this is it.
    /bin/bash -c "$INSTALL_LINE"
    # The installer hands a normal install back to launchd, which starts it on
    # the new files. An isolated copy has no launchd to be handed to, and the
    # installer's own open.sh starts nothing while the old process is still
    # answering -- so the files were upgraded underneath a process that went on
    # serving the version it started with, and the check below then reported "it
    # was already the newest version" about an upgrade that had happened.
    if isolated; then
      restart_isolated >/dev/null 2>&1 || true
    fi
    # Saying "done" is not the same as being done. The installer restarts the
    # service, and a restart that quietly did not happen leaves somebody
    # running the very version they just replaced, told it worked. So the
    # answer comes from the app itself: whatever is serving now is what this
    # reports, and if that is still the old one it says so instead.
    after=""
    for _ in 1 2 3 4 5 6 7 8 9 10 11 12; do
      after="$(health | field version || true)"
      [ -n "$after" ] && [ "$after" != "$before" ] && break
      /bin/sleep 1
    done
    say ""
    if [ -z "$after" ]; then
      err "Updated, but it is not answering at $URL yet."
      err "  Give it a minute, then: homefinder status"
      exit 1
    elif [ -n "$before" ] && [ "$after" = "$before" ]; then
      say "Still running $after -- it was already the newest version."
      say "If you expected a change, restart it: homefinder restart"
    else
      say "Now running $after."
    fi
    ;;

  repair)
    require_installed
    [ -x "$TOOLS/repair.sh" ] || { err "Repair is missing. Run the installer again:"; err "  $INSTALL_LINE"; exit 1; }
    exec "$TOOLS/repair.sh"
    ;;

  uninstall)
    require_installed
    [ -x "$TOOLS/uninstall.sh" ] || { err "Uninstall is missing from $TOOLS."; exit 1; }
    exec "$TOOLS/uninstall.sh"
    ;;

  -h|--help|help) usage ;;
  -v|--version|version)
    require_installed
    version="$(health | field version || true)"
    say "${version:-unknown}"
    ;;
  *)
    err "homefinder: there is no '$1' command."
    err ""
    usage >&2
    exit 1
    ;;
esac
