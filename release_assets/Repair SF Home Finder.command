#!/bin/bash
set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_ROOT="${SF_HOUSING_APP_ROOT:-$HOME/Library/Application Support/SF Housing Monitor}"
# This release's own Repair first, never whatever an older install left in
# tools/: on a Mac that installed 0.5.6 or earlier, that is the script that
# restored nothing and deleted every backup older than thirty days.
if [ -x "$SCRIPT_DIR/payload/tools/repair.sh" ]; then
  SF_HOUSING_APP_ROOT="$APP_ROOT" SF_HOUSING_RELEASE_ROOT="$SCRIPT_DIR" "$SCRIPT_DIR/payload/tools/repair.sh"
elif [ -x "$APP_ROOT/tools/repair.sh" ]; then
  SF_HOUSING_APP_ROOT="$APP_ROOT" SF_HOUSING_RELEASE_ROOT="$SCRIPT_DIR" "$APP_ROOT/tools/repair.sh"
else
  "$SCRIPT_DIR/payload/install.sh" "$SCRIPT_DIR"
fi
