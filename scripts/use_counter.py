#!/usr/bin/env python3
"""Point the install command at the counter, or back at GitHub.

The install URL appears in two places that have to agree -- the command people
paste from the README, and the comment at the top of install.sh that repeats
it -- and a counter only counts what people actually run. Editing both by hand
is how they drift, so this does both or neither.

Reversible on purpose. ``--revert`` puts the GitHub URL back everywhere, which
is the fix if the worker ever becomes a problem: the old command has no
dependency on it.

    python3 scripts/use_counter.py https://sf-home-finder-install.you.workers.dev
    python3 scripts/use_counter.py --revert
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
REPO = "2millerhenry/sf-home-finder"
GITHUB_URL = f"https://github.com/{REPO}/raw/HEAD/install.sh"

# Any install URL, whichever of the two it currently is. Anchored to the start
# of a line -- optionally behind a shell comment marker, which is how
# install.sh repeats it -- so it rewrites the command people run and nothing
# else. The README also quotes this command inline, in prose, to show how to
# install without being counted; that one names GitHub deliberately and a
# looser pattern turns it into a sentence that contradicts itself.
INSTALL_COMMAND = re.compile(
    r"(?m)^(#?[ \t]*curl -fsSL )(https://\S+?install\.sh)( \| bash)"
)

TARGETS = ("README.md", "install.sh")


def normalise(raw: str) -> str:
    """The full install URL for a worker base, checked before anything is written."""
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError(f"not an https URL: {raw}")
    base = f"https://{parsed.netloc}{parsed.path.rstrip('/')}"
    return base if base.endswith("/install.sh") else f"{base}/install.sh"


def retarget(url: str) -> list[tuple[str, int]]:
    """Rewrite every install command to ``url``. Returns what changed."""
    changed: list[tuple[str, int]] = []
    for name in TARGETS:
        path = ROOT / name
        before = path.read_text(encoding="utf-8")
        after, count = INSTALL_COMMAND.subn(rf"\g<1>{url}\g<3>", before)
        if count and after != before:
            path.write_text(after, encoding="utf-8")
        changed.append((name, count))
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("url", nargs="?", help="the deployed worker's base URL")
    parser.add_argument(
        "--revert",
        action="store_true",
        help="point the install command back at GitHub",
    )
    args = parser.parse_args(argv)

    if args.revert:
        url = GITHUB_URL
    elif args.url:
        try:
            url = normalise(args.url)
        except ValueError as error:
            print(f"Stopped: {error}", file=sys.stderr)
            return 1
    else:
        parser.error("give a worker URL, or --revert")

    changed = retarget(url)
    for name, count in changed:
        print(f"  {name}: {count} install command{'' if count == 1 else 's'} -> {url}")

    if not any(count for _, count in changed):
        print("\nNothing matched. Was the install command edited by hand?", file=sys.stderr)
        return 1

    if not args.revert:
        base = url[: -len("/install.sh")]
        print(
            "\nTo read the numbers, with the stats token from the deploy step:\n"
            f"  export SF_INSTALL_COUNTER_URL={base}\n"
            "  export SF_INSTALL_COUNTER_TOKEN=<your stats token>\n"
            "  uv run python scripts/installs.py"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
