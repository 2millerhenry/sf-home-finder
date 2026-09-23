"""When this app is allowed to ask for anything, and what it remembers of asking.

The footer's "Say thanks" is always there and demands nothing: a reader who
never wants to think about money can use this app for years and never be
interrupted by the subject. This module is about the other kind of ask -- a
card that arrives over the board on the app's own initiative -- and the only
thing that makes that acceptable is how rarely it is allowed to happen.

Two asks in the life of an install, and then silence for good:

* the first three days after the app arrived, so nobody is asked to fund
  something they have not yet decided to keep,
* the second a week after the first, and only if the first went unanswered.

Going to Ko-fi ends it immediately, whether or not anything is given. Somebody
who opened the page has answered the question, and asking a second time would
be asking them to answer it twice.

Two more refusals live in the app rather than here, because they are about the
moment rather than the count: never over a running scan, and never while the
Ready Check says something is broken. An app asking for money on a morning it
is not working is asking for a toll.

The counting is kept in a file beside the database rather than in the browser.
This app runs on one computer and its data directory travels -- a restored
backup, a machine move, an upgrade that replaces the whole runtime folder --
so the history of what has been asked travels with it. In ``localStorage`` it
would be forgotten every time somebody cleared their browser, and an ask that
comes back whenever a cache is cleared is the kind people learn to resent.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path


STATE_NAME = "donation-prompt.json"

# Long enough to have opened the app on a second day and seen it find something;
# short enough that the answer is still about this app rather than a vague
# memory of it. Both windows are counted from the moment they start, not from
# midnight: somebody who installed this on Friday evening is not asked on Monday
# morning because three dates have turned over.
FIRST_ASK_AFTER = timedelta(days=3)
SECOND_ASK_AFTER = timedelta(days=7)

# Not a tunable. Two is the whole promise this module makes, and a third ask
# would make a liar of the first two.
ASK_LIMIT = 2

# Any journey to the donation page, given or not. See the module docstring.
THANKED = "thanks"


@dataclass(frozen=True, slots=True)
class PromptState:
    """What has been asked of this install, and when."""

    first_seen_at: datetime | None = None
    asked_at: datetime | None = None
    asks: int = 0
    answer: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "first_seen_at": _iso(self.first_seen_at),
            "asked_at": _iso(self.asked_at),
            "asks": self.asks,
            "answer": self.answer,
        }


def _iso(moment: datetime | None) -> str | None:
    return moment.astimezone(UTC).isoformat() if moment is not None else None


def _moment(raw: object, *, ceiling: datetime) -> datetime | None:
    """One stored timestamp, or None if it is not one this app can use.

    Clamped to ``ceiling`` rather than trusted. A stamp in the future is not a
    date anything here can have happened on -- a machine whose clock was wrong
    when the file was written, then corrected -- and left alone it would hold
    the first ask until that date arrived, which for a clock set to 2031 means
    never. Clamping costs an ask that lands three days after the clock was
    fixed; trusting it costs the feature.
    """
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return min(parsed.astimezone(UTC), ceiling)


def state_path(data_dir: Path) -> Path:
    # Beside the database and the update cache, not in the runtime directory,
    # which is replaced wholesale on every upgrade -- the one moment a record
    # of "this person has already been asked" is most worth keeping.
    return Path(data_dir) / STATE_NAME


def read_state(data_dir: Path, *, now: datetime | None = None) -> PromptState:
    """What is known about asking this install, which may be nothing.

    Never raises. A missing, empty, truncated or hand-edited file means the
    same thing as a fresh install: nothing has been asked yet. The failure that
    direction costs an ask somebody may have already had; treating damage as
    "already asked twice" would silently retire the feature on a machine that
    had never shown the card at all.
    """
    ceiling = now or datetime.now(UTC)
    try:
        raw = json.loads(state_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return PromptState()
    if not isinstance(raw, dict):
        return PromptState()
    asks = raw.get("asks")
    answer = raw.get("answer")
    return PromptState(
        first_seen_at=_moment(raw.get("first_seen_at"), ceiling=ceiling),
        asked_at=_moment(raw.get("asked_at"), ceiling=ceiling),
        asks=asks if isinstance(asks, int) and asks >= 0 else 0,
        answer=answer if isinstance(answer, str) else "",
    )


def write_state(data_dir: Path, state: PromptState) -> bool:
    """Store the state, all at once or not at all. True if it landed.

    Written beside the target and renamed over it, because a process killed
    mid-write would otherwise leave a half-written file that every later read
    has to treat as damage -- and damage here reads as "never asked", which
    would put the card back in front of somebody who had already answered it.

    The caller is told whether it landed, because one caller cares: the route
    that is about to show the card. An ask it could not record is an ask that
    would be made again on the next page load, so it does not make it.
    """
    target = state_path(data_dir)
    temporary = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(state.as_dict(), ensure_ascii=False), encoding="utf-8")
        temporary.replace(target)
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    return True


def installed_at(data_dir: Path) -> datetime:
    """When this install arrived, as well as the filesystem can say.

    The data directory's own creation, which is made once and then only written
    into. ``st_birthtime`` is the real answer on macOS; Windows puts creation in
    ``st_ctime``; Linux has neither, and its ``st_ctime`` on a directory moves
    every time a file is added to it. That last case reads as "installed just
    now", which delays the first ask rather than bringing it forward, and it is
    only ever read once -- :func:`resolve_first_seen` writes the answer down --
    so on a fresh install the drift is a few seconds of a three-day window.
    """
    try:
        stamp = Path(data_dir).stat()
    except OSError:
        return datetime.now(UTC)
    created = getattr(stamp, "st_birthtime", None) or stamp.st_ctime
    try:
        return datetime.fromtimestamp(float(created), UTC)
    except (OSError, OverflowError, ValueError):
        return datetime.now(UTC)


def resolve_first_seen(data_dir: Path, state: PromptState) -> PromptState:
    """Fix the clock this install's first ask is counted from, once and for all.

    Settled on the first read and written down, so every later read gets the
    same answer no matter what the filesystem has done to the directory since.
    Deriving it rather than starting the count now is what lets an install that
    has been running for months be asked on the day this ships, instead of
    being treated as brand new and told to wait three more days.
    """
    if state.first_seen_at is not None:
        return state
    settled = replace(state, first_seen_at=installed_at(data_dir))
    write_state(data_dir, settled)
    return settled


def ask_is_due(state: PromptState, *, now: datetime) -> bool:
    """Whether the card has earned the right to appear, on the count alone."""
    if state.answer or state.asks >= ASK_LIMIT:
        return False
    if state.first_seen_at is None:
        # Nothing to count from. resolve_first_seen settles this before the
        # question is ever asked in earnest; reaching here means the file could
        # not be written, and an ask nobody can record is an ask that would
        # repeat on every page load.
        return False
    if state.asks == 0:
        return now - state.first_seen_at >= FIRST_ASK_AFTER
    if state.asked_at is None:
        return False
    return now - state.asked_at >= SECOND_ASK_AFTER


def record_ask(state: PromptState, *, now: datetime) -> PromptState:
    """The state after the card has been put in front of somebody."""
    return replace(state, asked_at=now, asks=state.asks + 1)


def record_thanks(state: PromptState) -> PromptState:
    """The state after somebody went to the donation page. This is the end of it."""
    return replace(state, answer=THANKED)
