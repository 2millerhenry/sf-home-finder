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
# a trick: macOS marks what a *browser* downloads, and curl is not a browser.
# This takes the smaller .tar.xz where the ZIP page takes the ZIP; the folder
# that comes out of either one is the same folder.
set -euo pipefail

REPO="2millerhenry/sf-home-finder"
fail() { printf '\nStopped: %s\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = Darwin ] || fail "this app is macOS only."
[ "$(uname -m)" = arm64 ] || fail "this needs an Apple Silicon Mac (M1 or later)."

WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT

echo "Finding the latest release..."
RELEASE="$(curl -fsSL "https://api.github.com/repos/$REPO/releases/latest")" ||
  fail "could not reach GitHub. Check your connection and run it again."

# The .tar.xz first: it holds the same folder as the ZIP and is a third
# smaller, because most of the release is a large binary that deflate barely
# compresses. The ZIP whenever a release has no tarball, which is every
# release published before this line was written -- so the fallback is the
# path that runs until the next one ships, not a nicety.
URL="$(printf '%s' "$RELEASE" |
  sed -n 's/.*"\(https[^"]*macOS-arm64\.tar\.xz\)".*/\1/p' | head -1)"
FORMAT=tar
ARCHIVE="$WORK/r.tar.xz"
if [ -z "$URL" ]; then
  URL="$(printf '%s' "$RELEASE" |
    sed -n 's/.*"\(https[^"]*macOS-arm64\.zip\)".*/\1/p' | head -1)"
  FORMAT=zip
  ARCHIVE="$WORK/r.zip"
fi
[ -n "$URL" ] || fail "could not find the download. See https://github.com/$REPO/releases"

echo "Downloading..."
curl -fL --progress-bar "$URL" -o "$ARCHIVE" || fail "the download did not finish. Check your connection and run it again."
mkdir -p "$WORK/x"
# Both of these ship with macOS, so preferring the smaller archive costs
# nobody a dependency. A truncated download fails here, which is what the
# message below is about.
case "$FORMAT" in
  tar) /usr/bin/tar -xJf "$ARCHIVE" -C "$WORK/x" ;;
  zip) /usr/bin/unzip -q "$ARCHIVE" -d "$WORK/x" ;;
esac || fail "the download was incomplete. Run it again."

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
