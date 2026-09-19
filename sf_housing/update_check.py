"""Whether a newer release exists. Nothing about who is asking.

This app collects nothing and its searches never leave the machine, which is
most of why anybody should trust it. Asking a public page whether a newer
version exists is the one outbound request it makes on its own behalf, so it is
worth being exact about what that costs: a GET to the GitHub releases API, with
no query, no body, no identifier, and not even the installed version -- the
comparison happens here, after the answer comes back. GitHub learns that some
IP address read a public page, which is what it learns when anybody opens the
releases page in a browser. ``SF_HOUSING_NO_UPDATE_CHECK=1`` turns it off
entirely and everything downstream then behaves as though no answer exists.

The reason it exists at all: there is no other way to fix anything. Nobody
reports a crash here, because nothing is reported anywhere. Without this, a bug
shipped is a bug that stays on every machine that has it, and the only people
who would ever learn of a fix are the ones who happen to revisit a web page.

Nothing here ever runs while a page is rendering. A scheduled job refreshes the
stored answer, and everything that displays it reads what was stored, so a slow
or unreachable GitHub can never hold up the dashboard.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import httpx

from . import RELEASE_REPO, __version__


CACHE_NAME = "update-check.json"

# Once a day. The answer changes a few times a year, and GitHub allows sixty
# unauthenticated requests an hour from one address -- a budget shared with
# anything else on the machine that talks to it, so spending one a day on this
# leaves it effectively untouched.
CHECK_INTERVAL = timedelta(hours=24)

# Long enough for a slow connection, short enough that a scheduled job never
# sits on a socket. Nothing waits on this, so there is no reason to be patient.
REQUEST_TIMEOUT_SECONDS = 6.0

# Releases are tagged v0.5.0. Anything with a suffix -- v0.6.0-rc1, v1.0.0b2 --
# is deliberately refused rather than parsed: a pre-release is not something to
# put in front of somebody who only wants their housing search to keep working.
_RELEASE_TAG = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")

DISABLE_VARIABLE = "SF_HOUSING_NO_UPDATE_CHECK"


def checking_disabled() -> bool:
    """Whether the person running this has asked not to be checked up on."""
    return os.environ.get(DISABLE_VARIABLE) == "1"


def release_series(tag: str | None) -> tuple[int, int, int] | None:
    """The three numbers in a release tag, or None if it is not one."""
    match = _RELEASE_TAG.match(str(tag or "").strip())
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def is_newer(candidate: str | None, installed: str | None = None) -> bool:
    """Whether ``candidate`` is a later release than ``installed``.

    Compared as three numbers, never as text. "0.10.0" sorts before "0.9.0" as
    a string, so a text comparison works for exactly nine minor releases and
    then quietly stops offering upgrades forever.
    """
    later = release_series(candidate)
    if later is None:
        return False
    current = release_series(installed if installed is not None else __version__)
    if current is None:
        # An installed version this cannot parse is a developer build. Offering
        # to "upgrade" it to the newest tag would replace the thing being
        # worked on with a release.
        return False
    return later > current


@dataclass(frozen=True, slots=True)
class UpdateStatus:
    """What was last learned about releases, and when."""

    checked_at: datetime
    latest: str | None = None
    available: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "available": self.available,
            "latest": self.latest,
            "checked_at": self.checked_at.astimezone(timezone.utc).isoformat(),
        }


def cache_path(data_dir: Path) -> Path:
    # Beside the database rather than in the runtime directory, which is
    # replaced wholesale on every upgrade -- the one moment the stored answer
    # is most worth keeping.
    return Path(data_dir) / CACHE_NAME


def read_status(data_dir: Path) -> UpdateStatus | None:
    """The last stored answer, or None if there is not a usable one.

    Everything that displays an update goes through here, and it neither
    reaches the network nor raises: a missing, empty, truncated or hand-edited
    file is simply an absence of news.
    """
    if checking_disabled():
        return None
    try:
        raw = json.loads(cache_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    try:
        checked_at = datetime.fromisoformat(str(raw.get("checked_at", "")))
    except ValueError:
        return None
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=timezone.utc)
    latest = raw.get("latest")
    latest = str(latest) if isinstance(latest, str) else None
    # Recomputed rather than trusted. The stored flag was true for the version
    # installed when it was written, and an upgrade leaves that file in place --
    # so a cache written before the upgrade would go on advertising the release
    # the machine is now running.
    return UpdateStatus(checked_at=checked_at, latest=latest, available=is_newer(latest))


def write_status(data_dir: Path, status: UpdateStatus) -> None:
    """Store an answer, all at once or not at all.

    Written beside the target and renamed over it, because a process killed
    mid-write would otherwise leave a half-written file that every later read
    has to treat as damage.
    """
    target = cache_path(data_dir)
    temporary = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    try:
        # Inside the guard with everything else. Creating the directory can
        # fail on its own -- a read-only home, or a plain file sitting where
        # the data directory should be -- and that raised straight out of a
        # scheduled job, which kills the job rather than the news.
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(
            json.dumps(status.as_dict(), ensure_ascii=False), encoding="utf-8"
        )
        temporary.replace(target)
    except OSError:
        # A read-only or full disk is not worth failing a scheduled job over.
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def fetch_latest_tag(timeout: float = REQUEST_TIMEOUT_SECONDS) -> str | None:
    """Ask GitHub for the newest release tag, or None if it did not say.

    Every failure is the same answer -- offline, rate limited, a repository
    that has no releases yet, a proxy returning a login page. None of them are
    news, and none of them are worth showing anybody.
    """
    url = f"https://api.github.com/repos/{RELEASE_REPO}/releases/latest"
    try:
        response = httpx.get(
            url,
            timeout=timeout,
            follow_redirects=True,
            headers={
                "Accept": "application/vnd.github+json",
                # Named so that if this ever does become a burden on GitHub
                # they can see what it is rather than having to guess. The
                # version is deliberately left out: GitHub refuses a request
                # with no User-Agent at all, but naming the release would tell
                # them which version this machine is running, which is the one
                # thing this is promised never to say.
                "User-Agent": "sf-home-finder",
            },
        )
        if response.status_code != 200:
            return None
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    tag = payload.get("tag_name")
    return str(tag) if isinstance(tag, str) else None


def refresh_status(
    data_dir: Path,
    *,
    fetch: Callable[[], str | None] = fetch_latest_tag,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    interval: timedelta = CHECK_INTERVAL,
    force: bool = False,
) -> UpdateStatus | None:
    """Ask again if it is time to, and store what comes back.

    Returns the status now in force, which is the stored one when the answer is
    still fresh enough. Never raises: this runs on a scheduler where an
    exception is a log line nobody reads.
    """
    if checking_disabled():
        return None
    moment = now()
    existing = read_status(data_dir)
    if existing is not None and not force:
        if moment - existing.checked_at < interval:
            return existing
    try:
        tag = fetch()
    except Exception:  # noqa: BLE001 - a background check may never take the app down
        tag = None
    if tag is None:
        # Keep the last good answer rather than replacing it with silence, and
        # keep its timestamp too, so a machine that is offline for a week
        # retries daily instead of only once.
        return existing
    status = UpdateStatus(checked_at=moment, latest=tag, available=is_newer(tag))
    write_status(data_dir, status)
    return status
