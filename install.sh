#!/bin/bash
# Install SF Home Finder:
#
#   curl -fsSL https://sf-home-finder-install.sfhomefinder.workers.dev/install.sh | bash
#
# Downloads the current release and runs the installer inside it -- the same one
# the ZIP contains, which checks every file against a checksum before using it.
#
# It never asks for your password, because nothing here needs an administrator;
# anything claiming otherwise is not this. It works in a temporary folder that is
# removed however it ends, and it touches nothing outside the app's own folder.
#
# There is no Gatekeeper prompt this way, which is a real difference rather than
# a trick: macOS marks what a *browser* downloads, and curl is not a browser. The
# bytes are identical to the ZIP either way.
set -euo pipefail

REPO="2millerhenry/sf-home-finder"
fail() { printf '\nStopped: %s\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = Darwin ] || fail "this app is macOS only."
[ "$(uname -m)" = arm64 ] || fail "this needs an Apple Silicon Mac (M1 or later)."

WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT

echo "Finding the latest release..."
URL="$(curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" |
  sed -n 's/.*"\(https[^"]*macOS-arm64\.zip\)".*/\1/p' | head -1)"
[ -n "$URL" ] || fail "could not find the download. See https://github.com/$REPO/releases"

echo "Downloading..."
curl -fL --progress-bar "$URL" -o "$WORK/r.zip" || fail "the download did not finish. Check your connection and run it again."
/usr/bin/unzip -q "$WORK/r.zip" -d "$WORK/x" || fail "the download was incomplete. Run it again."

# Found by what it contains rather than by what it is called, so a release
# renamed between versions still installs.
ROOT="$(find "$WORK/x" -maxdepth 3 -type f -path '*/payload/install.sh' | head -1)"
ROOT="${ROOT%/payload/install.sh}"
[ -f "${ROOT:-}/payload/install.sh" ] || fail "that is not a release. Try the ZIP instead: https://github.com/$REPO/releases/latest"

echo
# What comes next is two downloads behind one line of output -- a private
# Python, then the libraries it needs -- and on a slow connection that is a
# minute or more of a cursor not moving. Saying so costs nothing and is the
# difference between somebody waiting and somebody pressing Ctrl-C.
echo "Next it downloads a private Python and the libraries it needs, about"
echo "20 MB in total. Measured at twenty seconds on a quick connection."
echo

# Run it rather than exec it. exec replaces this shell, which would mean the
# EXIT trap above never fires and roughly 60MB of download and unpacked release
# stayed in the temp folder after every install.
status=0
/bin/bash "$ROOT/payload/install.sh" "$ROOT" || status=$?
exit "$status"
