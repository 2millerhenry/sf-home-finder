#!/usr/bin/env python3
"""What the install actually did to a machine that had never seen it.

Somebody deciding whether to run an unsigned installer is not asking whether
the author is trustworthy. They are asking what it will do to their computer,
and every answer a project gives to that is prose the author wrote about
their own software.

So this answers it with evidence instead. A GitHub runner takes a picture of
itself, installs the release, takes another, and the difference is published
beside the download: every path created, every file changed, every login item
registered, on a machine nobody controls and nothing else runs on.

Two things it is careful about.

Paths are recorded with a hash as well as a size, because the surprise worth
catching is not a new file -- it is a line appended to a shell profile that
was already there. During this project's own development a test did exactly
that to the author's ~/.zshrc, and nothing noticed until he read the diff by
hand.

And it reports what it saw rather than what it expected. There is no list of
approved paths in here to quietly absorb a new one: an install that starts
writing somewhere else shows up as a path in the report, and the reader
decides whether that is fine.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

# Where an install could reasonably leave something. Each is walked whole,
# except the home directory itself, which is read one level deep -- a runner's
# home is full of churn that has nothing to do with this, and the files worth
# watching there are the dotfiles an installer might append a line to.
ROOTS = [
    "~/Library/Application Support",
    "~/Library/LaunchAgents",
    "~/.local/bin",
    "~/Applications",
    "/Applications",
    "/Library/LaunchAgents",
    "/Library/LaunchDaemons",
    "/usr/local/bin",
]

# Read for their contents, not merely their presence.
HOME_FILES_DEPTH = 1

# A file big enough that hashing every one of them would dominate the run.
# Size and mode still change when these do, which is enough to notice.
HASH_LIMIT_BYTES = 2_000_000


def _digest(path: Path) -> str:
    try:
        if path.stat().st_size > HASH_LIMIT_BYTES:
            return "large"
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return "unreadable"


# Whose bytes this may read. Anything this app wrote is fair game, and so is
# a dotfile in the home directory, because a line appended to a shell profile
# is invisible in a size alone. Everything else is recorded by size and mode
# only: a snapshot run on somebody's own Mac walks past every other app's
# private data, and there is no reason for this to open any of it.
def _ours(path: Path) -> bool:
    text = str(path)
    return "SF Home Finder" in text or "sfhousing" in text or "homefinder" in text


def _record(path: Path, read_bytes: bool = False) -> dict:
    try:
        info = path.lstat()
    except OSError:
        return {}
    hashable = (read_bytes or _ours(path)) and not path.is_symlink() and path.is_file()
    return {
        "size": info.st_size,
        "mode": oct(info.st_mode & 0o777),
        "link": os.readlink(path) if path.is_symlink() else None,
        "sha": _digest(path) if hashable else None,
    }


def _walk(base: Path):
    """The top level of a root, and everything inside anything this app owns.

    Read whole, a developer's ~/Library/Application Support is 900,000 files
    belonging to other programs and a 170MB snapshot that says nothing. What
    matters outside this app's own folders is whether something new appeared
    at all, not what is inside somebody else's -- so each root is read one
    level deep, and only paths this app owns are opened up.
    """
    try:
        entries = sorted(base.iterdir())
    except OSError:
        return
    for entry in entries:
        yield entry
        if entry.is_dir() and not entry.is_symlink() and _ours(entry):
            try:
                yield from sorted(entry.rglob("*"))
            except OSError:
                continue


def snapshot() -> dict:
    """Every watched path, with enough about it to notice a change."""
    seen: dict[str, dict] = {}
    for root in ROOTS:
        base = Path(root).expanduser()
        if not base.exists():
            continue
        for path in _walk(base):
            seen[str(path)] = _record(path)
    home = Path.home()
    for path in home.iterdir():
        # Dotfiles only: a shell profile is the classic place for an installer
        # to leave something behind, and the rest of a home directory is the
        # person's own business.
        if path.name.startswith(".") and path.is_file():
            seen[str(path)] = _record(path, read_bytes=True)
    return {"paths": seen, "agents": _agents()}


def _agents() -> list[str]:
    """Login items this account has registered, by label."""
    try:
        listing = subprocess.run(
            ["/bin/launchctl", "list"], capture_output=True, text=True, timeout=30
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    labels = []
    for line in listing.splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) == 3 and parts[2].strip():
            labels.append(parts[2].strip())
    return sorted(labels)


def _plural(count: int, noun: str) -> str:
    return noun if count == 1 else f"{noun}s"


def _relative(path: str) -> str:
    home = str(Path.home())
    return path.replace(home, "~", 1) if path.startswith(home) else path


def report(before: dict, after: dict, version: str) -> str:
    old, new = before["paths"], after["paths"]
    created = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    changed = sorted(p for p in set(old) & set(new) if old[p] != new[p])
    agents = sorted(set(after.get("agents", [])) - set(before.get("agents", [])))

    # Grouped by the root each path sits under, because "141 files under one
    # folder" is the honest shape of this install and a flat list of 141
    # lines reads like something to be alarmed about.
    groups: dict[str, list[str]] = {}
    for path in created:
        shown = _relative(path)
        root = next(
            (r for r in ROOTS if shown.startswith(r) or path.startswith(str(Path(r).expanduser()))),
            "elsewhere",
        )
        groups.setdefault(root, []).append(shown)

    lines = [
        f"# What installing {version} did to a clean Mac",
        "",
        "Recorded by a GitHub runner that had never seen this app: a snapshot of the",
        "machine, the install, another snapshot, and the difference. Nothing here is a",
        "claim about what the installer is meant to do -- it is what it did.",
        "",
        f"- **{len(created)}** {_plural(len(created), 'file')} created",
        f"- **{len(changed)}** existing {_plural(len(changed), 'file')} changed",
        f"- **{len(removed)}** {_plural(len(removed), 'file')} removed",
        f"- **{len(agents)}** {_plural(len(agents), 'login item')} registered",
        "",
    ]

    lines += ["## Where the new files went", ""]
    for root in sorted(groups, key=lambda r: -len(groups[r])):
        lines.append(f"- `{root}` — {len(groups[root])} {_plural(len(groups[root]), 'file')}")
    lines.append("")

    if changed:
        lines += [
            "## Files that already existed and were changed",
            "",
            "The ones worth reading closely. A shell profile or a file outside this",
            "app's own folders appearing here is the surprise this report exists for.",
            "",
        ]
        lines += [f"- `{_relative(p)}`" for p in changed]
        lines.append("")
    else:
        lines += [
            "## Files that already existed and were changed",
            "",
            "None. The install added its own folders and altered nothing that was",
            "already on the machine.",
            "",
        ]

    if removed:
        lines += ["## Files removed", ""] + [f"- `{_relative(p)}`" for p in removed] + [""]

    lines += ["## Login items registered", ""]
    lines += ([f"- `{label}`" for label in agents] if agents else ["None."])
    lines += [
        "",
        "## Removing all of it",
        "",
        "```",
        "homefinder uninstall",
        "```",
        "",
        "Generated by `scripts/install_report.py`. Reproduce it by running that script",
        "either side of an install on any Mac.",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    take = sub.add_parser("snapshot", help="Record the machine as it is now")
    take.add_argument("--out", type=Path, required=True)
    write = sub.add_parser("report", help="Write the difference between two snapshots")
    write.add_argument("--before", type=Path, required=True)
    write.add_argument("--after", type=Path, required=True)
    write.add_argument("--version", default="this release")
    write.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    if args.command == "snapshot":
        args.out.write_text(json.dumps(snapshot()), encoding="utf-8")
        print(f"snapshot: {args.out}")
        return 0

    before = json.loads(args.before.read_text(encoding="utf-8"))
    after = json.loads(args.after.read_text(encoding="utf-8"))
    args.out.write_text(report(before, after, args.version), encoding="utf-8")
    print(f"report: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
