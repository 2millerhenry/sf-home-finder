#!/bin/bash
# Make the homefinder command findable, in every shell that will look for it.
#
# usage: put-command-on-path.sh <directory holding the command>
#
# The command lives in ~/.local/bin, which a stock macOS shell does not search.
# A line in one profile is not enough: an interactive zsh reads .zshrc, a zsh
# script or an ssh command reads .zprofile, a login bash reads .bash_profile, a
# non-login bash reads .bashrc, and fish reads none of them and does not
# understand their syntax. Two people typed homefinder after installing and
# were told it did not exist, so this writes to every file their own shell
# reads, once, and then asks a fresh shell whether it can actually find the
# command rather than telling them it can.
#
# Prints what it changed and what a new shell finds. Exit 0 when the command
# will be found, 1 when it will not.
set -uo pipefail

CLI_DIR="${1:?usage: put-command-on-path.sh <directory>}"
COMMAND_NAME="homefinder"
MARKER="# Added by SF Home Finder so the $COMMAND_NAME command can be found."
LOGIN_SHELL="${SHELL:-/bin/sh}"

say() { printf '%s\n' "$*"; }

add_posix_line() {
  # One line, once, to a file the shell reads. Created if it is not there:
  # a Mac with no .zshrc is the common case, not the odd one.
  target="$1"
  /bin/mkdir -p "$(/usr/bin/dirname "$target")" 2>/dev/null || return 1
  [ -e "$target" ] || : > "$target" 2>/dev/null || return 1
  if /usr/bin/grep -qF "$CLI_DIR" "$target" 2>/dev/null; then
    return 0
  fi
  printf '\n%s\n%s\n' "$MARKER" "export PATH=\"$CLI_DIR:\$PATH\"" >> "$target" || return 1
  say "  added it to $(/usr/bin/basename "$target")"
}

add_fish_line() {
  # fish has its own syntax and its own idea of PATH; a POSIX export line here
  # is a syntax error on every prompt, which is worse than a missing command.
  target="$HOME/.config/fish/config.fish"
  /bin/mkdir -p "$(/usr/bin/dirname "$target")" 2>/dev/null || return 1
  [ -e "$target" ] || : > "$target" 2>/dev/null || return 1
  if /usr/bin/grep -qF "$CLI_DIR" "$target" 2>/dev/null; then
    return 0
  fi
  printf '\n%s\nfish_add_path %s\n' "$MARKER" "$CLI_DIR" >> "$target" || return 1
  say "  added it to config.fish"
}

case "$LOGIN_SHELL" in
  */fish)
    add_fish_line
    ;;
  */bash)
    add_posix_line "$HOME/.bash_profile"
    add_posix_line "$HOME/.bashrc"
    ;;
  */zsh)
    add_posix_line "$HOME/.zshrc"
    add_posix_line "$HOME/.zprofile"
    ;;
  *)
    # An unknown shell still reads this one more often than not.
    add_posix_line "$HOME/.profile"
    ;;
esac

# Now ask, rather than assume. The shell is started the way a terminal starts
# it -- login and interactive -- so it reads the files just written.
if [ ! -x "$LOGIN_SHELL" ]; then
  say "  cannot check $LOGIN_SHELL from here; open a new terminal and type $COMMAND_NAME"
  exit 0
fi
case "$LOGIN_SHELL" in
  */fish) FOUND="$("$LOGIN_SHELL" -lc "type -a $COMMAND_NAME" 2>/dev/null)" ;;
  *)      FOUND="$("$LOGIN_SHELL" -lic "type -a $COMMAND_NAME" 2>/dev/null)" ;;
esac
RESOLVED="$(printf '%s\n' "$FOUND" | /usr/bin/grep -o '/[^ ]*'"$COMMAND_NAME" | /usr/bin/head -20)"

if [ -z "$RESOLVED" ]; then
  say "  a new terminal still cannot find $COMMAND_NAME; run it as $CLI_DIR/$COMMAND_NAME"
  exit 1
fi

FIRST="$(printf '%s\n' "$RESOLVED" | /usr/bin/head -1)"
say "  a new terminal finds: $FIRST"
# Another copy earlier on PATH answers instead, quietly running a different
# installation than the one just put in place. Naming it is the whole fix: it
# lives outside this app and is not ours to delete.
printf '%s\n' "$RESOLVED" | while read -r other; do
  [ -n "$other" ] || continue
  if [ "$other" != "$CLI_DIR/$COMMAND_NAME" ]; then
    say "  note: another $COMMAND_NAME is also on your PATH: $other"
  fi
done
exit 0
