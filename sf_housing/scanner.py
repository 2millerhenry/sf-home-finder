from __future__ import annotations

import logging
import json
import os
import queue
import re
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta

import httpx

from .classification import classify_listing
from .connectors import GMAIL_PROVIDERS, aggregate_gmail_status, connector_state_for_error
from .coverage import coverage_is_fresh, fetch_count, missing_coverage_targets
from .database import Repository, utc_now
from .listing_identity import UNIT_GRAIN, facts_for_listing, same_home
from .location import split_street_address
from .filelock import release as release_file_lock, try_acquire as try_acquire_file_lock
from .freshness import source_is_in_backoff, source_key as watchdog_source_key
from .models import ListingCandidate, ScanOutcome
from .preferences import Preferences, setting_int
from .rent_estimate import (
    FAR_BELOW_MARKET_SHARE,
    WINDOW_DAYS as RENT_WINDOW_DAYS,
    RentTable,
    size_of,
)
from .rescore_marker import current_fingerprint
from .scoring import implausible_rent, score_listing
from .sources import (
    MONITOR_HEADERS,
    ListingSource,
    PartialReadError,
    ScanTimeUpError,
    SourceError,
    application_deadline_passed,
    card_proves_listed,
    covers_inventory,
    facebook_coordinate_neighborhood,
    sf_area_from_address,
    visible_sf_area_hint,
)


LOGGER = logging.getLogger(__name__)

# Which runs the app started by itself. Two places used to spell this set out
# separately -- which sources a run may touch, and whether a failing source is
# allowed its cooldown -- so adding a third kind of automatic run meant finding
# both or silently getting a lesser scan than the one being caught up.
AUTOMATIC_TRIGGERS = frozenset({"scheduled", "startup_catchup", "catch_up", "deep_sweep"})

# The nightly run that reads the sources which cannot be read deeply every time.
# Most sources are simply read in full on every scan: measured end to end that
# costs about 150 seconds twice a day, which is nothing. Two are different.
# Trulia and Redfin answer 403 and 202 once they have had enough, and a refused
# source returns nothing at all -- so reading either of them to the bottom is
# worth doing once a day at an hour nobody is waiting, and not worth doing at
# 10:00 when a shallow read that works is better than a deep one that is
# turned away.
DEEP_SWEEP_TRIGGER = "deep_sweep"
# A connector test is a person asking, but it still has to reach the sources a
# schedule would, or the thing they are testing is not the thing that runs.
FULL_SOURCE_TRIGGERS = AUTOMATIC_TRIGGERS | {"connector_test"}

# Rechecking is bounded by time, not by a count. A fixed six-per-source meant a
# sixty-home shortlist took days to cycle, so "no stale results" was a direction
# rather than a promise. The scan finishes well inside its allowance, and the
# leftover time is spent confirming homes instead of being given back.
#
# Every source still gets a fair share of what is left, so one slow source
# cannot spend the whole allowance and starve the rest, and each is guaranteed a
# small floor so it always makes progress even when the share is thin.
# What a source is owed each scan regardless of the share it works out to.
#
# Three was too few to matter. A real install had 90 shortlisted Craigslist
# homes waiting to be re-confirmed and drained them at roughly that rate, so
# the median home on the shortlist had last been looked at 52 hours earlier
# and the oldest 127 -- long enough for one to be flagged, pulled, and still
# sitting there looking live when somebody clicked it.
RECHECK_FLOOR_PER_SOURCE = 20

# ...but the floor is a slice of time, not a licence to spend the whole scan.
# A count on its own is only a bound for a source that answers quickly: a slow
# one doing twenty reads at a tenth of a second apiece would eat a short scan
# outright and the sources behind it would never run. So the floor holds until
# either its count or this share of what is left is used, whichever comes
# first, and the share is generous enough to drain a real backlog while still
# leaving three quarters of the time for everyone else.
RECHECK_FLOOR_TIME_SHARE = 0.25
# The longest any one source may hold the scan. An HTTP read timeout bounds
# each chunk of a response, not the whole of it, so a server that trickles
# bytes keeps its connection open for as long as it likes: AvalonBay took 200
# seconds over a single request that normally takes two, and the sixteen
# sources queued behind it were all skipped to keep the scan inside its limit.
# Set above the slowest healthy source rather than near it -- Craigslist reads
# several searches and their detail pages and wants about a minute -- so this
# only ever catches a source that has stopped behaving.
SOURCE_HARD_CEILING_SECONDS = 75.0
# The same idea on the nightly sweep, where a source is asked several narrower
# questions instead of one wide one and honestly needs longer: Zillow's eight
# rent bands take about a minute against the seventy-five a waiting scan
# allows. Cutting it there would abandon the whole source and return nothing,
# which is the one outcome worse than a slow one. Still a ceiling, so a source
# that stops answering at 3am cannot hold the sweep until morning.
DEEP_SOURCE_CEILING_SECONDS = 300.0
# The same stall one level down. The ceiling above was written to cover a
# source's whole turn -- "several searches and their detail pages" -- but it
# only ever wrapped the search, so a single detail page that trickled bytes
# still held the scan: Craigslist spent 593 seconds on one check and all
# twenty-three sources behind it were skipped with nothing collected. One
# detail page is a single request, so anything approaching this is a stall.
DETAIL_HARD_CEILING_SECONDS = 30.0

RECHECK_HARD_CEILING = 250

# Scans run at 10:00 and 18:00, so the gaps are eight hours and sixteen. The
# window has to be shorter than the shorter gap, or a home that goes quiet is
# passed over for a whole cycle and can reach a full day unconfirmed. At seven
# hours every scan re-examines anything the previous one did not confirm, which
# puts the worst case at sixteen hours rather than twenty-four.
RECHECK_AFTER = timedelta(hours=7)

# How many suspiciously cheap homes one source may have opened in one scan.
#
# A rent below the floor for a home its size costs the home its place at the
# top of the shortlist until somebody has read its own page (see
# ``database.CHEAP_RENT_CONFIRMED_FOR``), and this is the pass that does the
# reading. It has to be tight, because these are homes a search is still
# returning -- there is no silence here to justify a crawl. Measured on the
# owner's real board: thirteen of the 140 homes on the shortlist are below
# their floor and far enough below what their area asks to be worth a look.
# Twice that, so a board with more of them still finishes in one scan, and
# the queue rotates oldest-read first so nothing is starved when it does not.
CHEAP_RENT_PAGES_PER_SCAN = 25

# ...and the most of what is left of the scan the pass may hold. The count
# alone bounds a source that answers quickly; a slow one doing twenty-five
# reads would spend a short scan outright and the sources behind it would
# never run. This pass goes before the recheck of absent homes, because these
# are the homes about to be recommended, so it must not be able to spend the
# recheck's time as well as its own.
CHEAP_RENT_TIME_SHARE = 0.15

# What the reader is told. A home nobody has confirmed for a day is not a home
# the app can vouch for, whatever its score says.
CONFIRMATION_STALE_AFTER = timedelta(hours=24)

# How far back the one-off `initial_discovery` scan reaches when a profile
# first goes active, so a new install opens on real listings instead of an
# empty dashboard. Listings with no parseable timestamp are kept regardless.
INITIAL_DISCOVERY_WINDOW = timedelta(days=7)


def _remaining_label(seconds: int | None, *, running: bool) -> str:
    """How much longer, in words somebody can act on.

    Rounded hard on purpose. A scan is not predictable to the second, and
    "about a minute left" that turns out to be seventy seconds reads as honest
    where "63s left" counting unevenly reads as broken.

    The one slot the panel has for this. A first scan has no history to
    estimate from and used to get its own separate line saying "may take up to
    two minutes", which then sat beside the real estimate on every scan after
    -- the same thing said twice, one of them stale.
    """
    if not running:
        return ""
    if seconds is None:
        return "a couple of minutes, probably"
    if seconds <= 10:
        return "finishing up"
    if seconds < 60:
        return "about half a minute left"
    minutes = round(seconds / 60)
    return f"about {minutes} minute{'' if minutes == 1 else 's'} left"


def _rechecking_sources_remaining(active_sources: list[ListingSource], source_index: int) -> int:
    """How many sources from here on can actually spend the recheck allowance.

    The allowance is divided by the sources still to come, so that one of them
    cannot take it all. Dividing by *every* remaining source counted the ten
    with no ``enrich`` method and no queue, which reserved shares that were
    never spent and left the source with the deepest backlog on the thinnest
    slice.
    """
    remaining = active_sources[max(0, source_index - 1):]
    countable = sum(
        1
        for source in remaining
        if hasattr(source, "enrich")
        and int(getattr(source, "recheck_budget", RECHECK_HARD_CEILING)) > 0
    )
    return max(1, countable)


def _search_covered_inventory(
    source: ListingSource,
    preferences: Preferences,
    found: int,
    *,
    cut_short: bool,
) -> bool:
    """Did this search look everywhere this source keeps its San Francisco homes?

    The question the rule about absence turns on, and the reason it is asked
    here rather than in SQL: only the run knows how it ended. Three things
    have to hold, and any one of them failing means the homes this search did
    not return were never looked for.

    The source has to say its search covers its inventory at all
    (``sources.covers_inventory``), which most of them cannot: a site that
    serves a thousand of the two and a half thousand homes it claims, or that
    is asked a question narrowed by the deal, answers about a slice.

    The read has to have finished. A walk stopped by a refusal, or by the
    check's own time limit, is a partial read of a source that can cover --
    and a partial read filed as a complete one is exactly the shape of this
    bug.

    And it has to have come in under the ceiling on how many homes a source
    may contribute. At the ceiling the search stopped counting rather than ran
    out, so whatever was past it is unread.
    """
    if cut_short or not covers_inventory(source):
        return False
    maximum = setting_int(preferences.section("sources").get("max_results_per_source"), 250)
    return found < maximum


# The background line a source that opts in is read on. Named, so a thread
# dump or a test can tell it from the short-lived threads every search and
# detail page already runs on.
LANE_THREAD_NAME = "scan-lane"
# Set to "1" to read every source strictly in turn again, exactly as before
# lanes existed: the way back if a lane ever turns out to cost something.
SEQUENTIAL_SCAN_VARIABLE = "SF_HOUSING_SEQUENTIAL_SCAN"
# The margin on top of the bounds a lane can legitimately overrun its deadline
# by. See Scanner._lane_join_grace.
LANE_JOIN_SLACK_SECONDS = 30.0
STALE_LANE_MESSAGE = (
    "Still being read by the previous check, so it was not read a second time "
    "alongside it. The next check reads it as usual."
)
LEFT_BEHIND_MESSAGE = (
    "Still reading when this check reached its time limit, so it was left to "
    "finish on its own and its homes were not counted in this check."
)
INTERRUPTED_LANE_MESSAGE = (
    "Still reading when this check was stopped, so its homes were not counted "
    "in this check."
)
# What a source reads when a check stops for it, when the reason is this
# computer going to sleep rather than anything the site did.
SLEPT_MESSAGE = (
    "This computer went to sleep during the check, so the check stopped here "
    "rather than carry on for hours. The next check reads it."
)

# What the check itself reads when that left sources it never read. Filed as
# interrupted, not completed: the slot it ran for is still owed a check (see
# ``scheduling.catch_up_if_due``), and a check asked for by hand is not spent.
SLEPT_SCAN_MESSAGE = (
    "This computer went to sleep during the check, so it stopped rather than carry on for hours. "
    "What it read is kept; the sources it had not read are read by a check soon after the computer wakes."
)


def scan_clock() -> Callable[[], float]:
    """A clock that never goes backwards and keeps counting while the computer sleeps.

    Every budget and ceiling of a scan is spent on it. Python's own
    ``time.monotonic`` stops while a Mac sleeps (it is mach_absolute_time;
    Linux's CLOCK_MONOTONIC stops too): it had counted 46.7 of one laptop's
    92.8 hours since boot. A scan timed by it resumed after the lid opened
    with most of its four minutes still to spend, and in the Mac's brief
    background wakes it went on spending them -- one check held on for two
    hours and seventeen minutes, a single Craigslist read for ninety-nine
    minutes, and a request "timed out" after 524 seconds against a limit of
    eight. Linux's CLOCK_BOOTTIME and macOS's CLOCK_MONOTONIC count the sleep;
    on Windows ``time.monotonic`` already does.
    """
    boottime = getattr(time, "CLOCK_BOOTTIME", None)
    if boottime is not None:
        return lambda: time.clock_gettime(boottime)
    if sys.platform == "darwin":
        return lambda: time.clock_gettime(time.CLOCK_MONOTONIC)
    return time.monotonic


def awake_only_clock() -> Callable[[], float]:
    """A clock that stops while the computer sleeps: where it falls behind
    ``scan_clock`` the computer slept, which a check then says instead of
    blaming a site. ``time.monotonic`` is that clock on macOS and Linux; on
    Windows it counts sleep too, so the interrupt time Windows keeps without
    its sleep is read instead (untested here: no Windows machine), and a
    failure to read it leaves only the telling-apart undone.
    """
    if sys.platform == "win32":
        try:
            import ctypes

            query = ctypes.windll.kernel32.QueryUnbiasedInterruptTime

            def unbiased() -> float:
                ticks = ctypes.c_ulonglong()
                if not query(ctypes.byref(ticks)):
                    raise OSError("QueryUnbiasedInterruptTime failed")
                return ticks.value / 10_000_000

            unbiased()
            return unbiased
        except Exception:
            pass
    return time.monotonic


# How long any wait on a source or a lane sleeps before looking at the scan's
# clock again. Python's own timeouts are counted on the clock that stops while
# the computer sleeps, so a wait of seventy-five seconds begun just before the
# lid closed could last hours; in slices, a wait notices within a second of
# waking that its time is up.
WAIT_SLICE_SECONDS = 1.0
# How far the scan's clock must run ahead of the awake one before a check says
# the computer slept. The two tick together while it is awake.
SLEEP_NOTICE_SECONDS = 5.0


@dataclass
class _FetchLimit:
    """When one call to a source must stop asking its site for anything.

    Set for the thread the call runs on and read by the scanner's HTTP clients
    before every request, redirects included (``Scanner._refuse_past_the_limit``),
    so a source walked away from stops at its next page instead of reading on
    for as long as its own walk allows. ``tripped`` says a request was refused.
    """

    end: float
    tripped: bool = False
    # Once the time is up: what the source should say about it, and whether
    # that is time running out (never a failure) or the source stalling.
    stopped: str | None = None
    for_time: bool = False


# The limit of the call running on this thread, if any.
_FETCH_LIMIT = threading.local()


class _OutOfTime(Exception):
    """A source the check stopped for time -- its own budget, or the computer
    sleeping -- rather than one that failed. Recorded as not reached, never as
    a failure: a failure counts towards pausing the source (freshness), and
    running out of time or battery is nothing the site did."""


def reads_on_its_own_lane(source: ListingSource) -> bool:
    """Whether this source may be read beside the rest of a scan.

    Only a source whose ``runs_in_own_lane`` is the value True -- not merely
    something truthy -- and never one chosen by name: thirteen test files use a
    stand-in called Craigslist, and choosing by name would thread every one of
    them without anybody asking. What makes a source eligible is that its owner
    sees nobody else's requests; sf_housing.sources.traffic_group says who that
    owner is, and tests/test_traffic_groups.py refuses to let one owner be read
    on both lines at once.
    """
    return getattr(source, "runs_in_own_lane", False) is True


def _masked(text: str | None) -> str:
    """A summary with its numbers taken out, to tell a new sentence from new numbers."""
    return re.sub(r"\d[\d,.]*", "#", re.sub(r"\s+", " ", str(text or "")).strip()).casefold()


def _merged_summary(stored: str | None, fresh: str | None, title: str | None) -> str | None:
    """The fresh summary unless it is plainly thinner than the one kept.

    A detail page says more than a search card, so a longer stored text
    survives a short card. Anything else -- the same sentence with new
    numbers, a rewritten card no shorter than the old one, a stored "summary"
    that was only the title -- is the site's current word and replaces it.
    """
    if not fresh:
        return stored
    kept = str(stored or "").strip()
    if not kept or kept == str(title or "").strip() or _masked(kept) == _masked(fresh):
        return fresh
    if len(fresh.strip()) >= len(kept):
        return fresh
    return stored


def _merged_metadata(stored: dict[str, Any], fresh: dict[str, Any]) -> dict[str, Any]:
    merged = {**stored, **fresh}
    kept = stored.get("address")
    if isinstance(kept, str) and kept.strip() and "address" in fresh:
        before = split_street_address(kept)
        after = split_street_address(str(fresh.get("address") or ""))
        if before is not None and (
            after is None
            or (
                (after.number, after.street) == (before.number, before.street)
                and before.unit
                and not after.unit
            )
        ):
            merged["address"] = kept
    return merged


@dataclass
class _SourceTally:
    """What one line of a scan collected, counted by that line alone."""

    seen: int = 0
    added: int = 0
    updated: int = 0
    failed: int = 0
    # The row of the source this line is reading, while it is open, so a
    # failure that escapes the source's own handling can still close it.
    open_source_run_id: int | None = None
    finished_progress: set[int] = field(default_factory=set)


class _Lane:
    """One background line of a scan, and how long it has been reading.

    Everything the main line decides by the clock is charged with this lane's
    time: whether a source is skipped for time, how long its search may run,
    whether another detail page fits, and how long it may recheck. A source
    reached sooner than it would have been in turn sees more of the scan left,
    and each of those decisions spends requests, so without the charge the
    scans already running long would send more than they do today. Charging
    only the recheck pass was the first version; on a scan up against its time
    limit -- seven of the last seventy-two real ones -- it read sources a
    sequential scan would have skipped.

    Once the lane is over, the charge is exactly what it took: the time a
    source would have had if the lane's source had gone ahead of it. While it
    is still reading, the time elapsed is still short of what it will take, so
    elapsed alone would hand the main line time sequential order never gave
    it. The charge is therefore never less than how long the lane's sources
    usually take.

    A usual time can be too high as well: a few stalled runs are enough. Where
    that would decide a skip, the main line waits on the lane instead (see
    Scanner._wait_if_the_lane_decides_a_skip), because a skip is instant and
    every source after it would be reached at once and skipped too. Where it
    only sizes a source's detail pages or recheck share, they come out a little
    smaller than in turn -- fewer requests, never more.

    One case cannot be made to match, because it turns on how long the lane
    will take: a lane source stalling far past its usual time. In turn, a
    stalled Craigslist once cost a whole scan -- 593 seconds, and every source
    behind it skipped. Beside a lane those sources are read instead. Each gets
    only the read it gets on any ordinary scan, so no site sees more than it
    does on a normal day; that scan simply stops being lost.
    """

    def __init__(
        self,
        clock: Callable[[], float],
        sources: list[tuple[int, ListingSource]],
        *,
        expected_seconds: float,
        generation: int,
    ) -> None:
        self._clock = clock
        self.sources = list(sources)
        self.expected_seconds = max(0.0, float(expected_seconds))
        self.generation: int | None = generation
        self.tally = _SourceTally()
        self.thread: threading.Thread | None = None
        self.joined = False
        self.abandoned = False
        # How many of its sources the scan gave up on, counted at the moment it
        # gave up. The lane is still running then, and a count taken later
        # could already include a source that finished in between -- which
        # would take its failure out of the scan's record.
        self.left_behind = 0
        self._lock = threading.Lock()
        self._started: float | None = None
        self._finished: float | None = None

    def mark_started(self) -> None:
        with self._lock:
            self._started = self._clock()

    def mark_finished(self) -> None:
        with self._lock:
            self._finished = self._clock()

    def started_at(self) -> float | None:
        with self._lock:
            return self._started

    def seconds(self) -> float | None:
        with self._lock:
            if self._started is None or self._finished is None:
                return None
            return self._finished - self._started

    def shift(self) -> float:
        with self._lock:
            if self._started is None:
                return self.expected_seconds
            if self._finished is not None:
                return self._finished - self._started
            return max(self._clock() - self._started, self.expected_seconds)


class Scanner:
    def __init__(
        self,
        repository: Repository,
        preference_loader: Callable[[], Preferences],
        sources: list[ListingSource],
        timeout_seconds: float = 15.0,
        detail_delay_seconds: float = 0.25,
        max_scan_seconds: float = 110.0,
        deep_scan_max_seconds: float | None = None,
        scan_allowed: Callable[[str, list[ListingSource] | None], bool] | None = None,
        clock: Callable[[], float] | None = None,
        awake_clock: Callable[[], float] | None = None,
    ):
        self.repository = repository
        self.preference_loader = preference_loader
        self.sources = sources
        self.timeout_seconds = timeout_seconds
        self.detail_delay_seconds = detail_delay_seconds
        self.max_scan_seconds = max_scan_seconds
        # A sweep that inherits the interactive budget is not a sweep: it would
        # spend four minutes and skip the sources it exists to read deeply.
        self.deep_scan_max_seconds = deep_scan_max_seconds or max_scan_seconds
        # Injected rather than imported: the daily budget is decided against the
        # schedule, and scheduling imports this module. Left out, a Scanner is
        # unbudgeted -- which is what the tests that are about scanning itself
        # want, and what the app never does.
        self.scan_allowed = scan_allowed
        # Taken rather than read, so a test can decide what a second costs.
        # Every budget here is spent in real seconds, the ones that pass while
        # the computer sleeps too (``scan_clock``), which is right in production
        # and useless in a test: how much work a machine turns a second into
        # varies enough that the same scan confirmed sixty homes here and
        # forty-four on a CI runner. Tests that are about how the allowance is
        # divided hand in a clock they advance themselves, and measure the
        # arithmetic instead of the hardware.
        self._clock = clock or scan_clock()
        # The seconds this process was awake for. Where it falls behind the
        # scan's clock the computer slept, which a check then says, rather than
        # blaming a site; and it bounds every wait too, so a test clock that
        # never moves cannot leave one waiting for ever.
        self._awake_clock = awake_clock or awake_only_clock()
        self._scan_lock = threading.Lock()
        self._process_lock_handle = None
        # Set whenever no scan is in flight, so a caller can block on the end of
        # one instead of asking over and over whether it is over yet. Starts
        # set: a scanner that has never run is idle.
        self._scan_finished = threading.Event()
        self._scan_finished.set()
        self._progress_lock = threading.Lock()
        # The sources being read right now, keyed by the object and holding
        # their position in the scan, so the panel can name every one of them
        # when a lane has a source in flight beside the main line.
        self._in_flight_sources: dict[int, tuple[int, str]] = {}
        # Bumped whenever a scan's progress starts or ends. A lane keeps the
        # value it began under and its updates are dropped once that changes,
        # so a lane left behind can never write into another scan's numbers.
        self._progress_generation = 0
        self._lane_thread: threading.Thread | None = None
        # How long the most recent lane took to read, for the log and for
        # anybody checking the saving is real.
        self.last_lane_seconds: float | None = None
        # What homes of each size cost in each neighbourhood, read once at the
        # start of every scan for the pass that decides whose cheap listing is
        # worth opening. Written before any lane starts and only read after,
        # so the two lines share it without sharing a lock. None before the
        # first scan, and after one that could not read it -- which that pass
        # treats as knowing nothing about any area rather than as an answer.
        self._scan_rent_table: RentTable | None = None
        self._progress_state: dict[str, object] = {
            "status": "idle",
            "running": False,
            "run_id": None,
            "trigger": None,
            "started_monotonic": None,
            "elapsed_seconds": 0,
            "sources_total": 0,
            "sources_completed": 0,
            "current_source": None,
            "listings_seen": 0,
            "listings_added": 0,
            "sources_failed": 0,
            # What the bar is really measuring. Counting sources, Craigslist
            # and Listings Project are a twenty-third each, so the bar shows
            # nothing for the 75 seconds the first one takes and then jumps
            # four points. Weighted by how long each source usually takes, it
            # moves at the rate the scan is actually progressing.
            "weight_total": 0.0,
            "weight_done": 0.0,
            "weight_current": 0.0,
            "current_started_monotonic": None,
        }

    @property
    def is_running(self) -> bool:
        return self._scan_lock.locked()

    @property
    def progress(self) -> dict[str, object]:
        """Return a JSON-safe snapshot of the active or most recent scan."""
        with self._progress_lock:
            snapshot = dict(self._progress_state)
        started = snapshot.pop("started_monotonic", None)
        if snapshot["running"] and isinstance(started, (int, float)):
            snapshot["elapsed_seconds"] = max(0, int(self._clock() - started))
        weight_total = float(snapshot.pop("weight_total", 0.0) or 0.0)
        weight_done = float(snapshot.pop("weight_done", 0.0) or 0.0)
        weight_current = float(snapshot.pop("weight_current", 0.0) or 0.0)
        current_started = snapshot.pop("current_started_monotonic", None)
        completed = int(snapshot["sources_completed"])
        total = int(snapshot["sources_total"])
        if snapshot["status"] in {"completed", "completed_with_errors"}:
            percent = 100
        elif weight_total > 0:
            # The source in flight counts for the share of itself it has had
            # time to do, so the bar keeps moving through a slow one instead
            # of waiting for it to finish. Capped just under its own weight:
            # a source running longer than usual must not borrow from the
            # next one and show progress that has not happened.
            running = 0.0
            if snapshot["running"] and weight_current > 0 and isinstance(current_started, (int, float)):
                spent = max(0.0, self._clock() - current_started)
                running = min(spent / weight_current, 0.95) * weight_current
            percent = round(((weight_done + running) / weight_total) * 100)
        elif total:
            percent = round((completed / total) * 100)
        else:
            percent = 0
        snapshot["percent"] = max(0, min(100, percent))
        # The weights are seconds each source really took on its own recent
        # runs, so what is left of them is already the answer to "how much
        # longer" -- no second guess to keep in step with the first. Only
        # offered while a scan is running and only once the weights are known;
        # a number invented for the first ever scan would be worse than none.
        remaining: int | None = None
        if snapshot["running"] and weight_total > 0:
            remaining = max(0, round(weight_total - weight_done - running))
        snapshot["seconds_remaining"] = remaining
        # Worded here rather than in the page and again in the script that
        # updates it: the first paint is server-rendered and every one after is
        # not, so two copies of this phrasing would disagree for the half
        # second before the first poll, and then quietly forever.
        snapshot["remaining_label"] = _remaining_label(remaining, running=bool(snapshot["running"]))
        return snapshot

    def _eligible_sources(self, trigger: str) -> list[ListingSource]:
        include_scheduled_only = trigger in FULL_SOURCE_TRIGGERS
        eligible = [
            source
            for source in self.sources
            if (
                include_scheduled_only
                or not getattr(source, "scheduled_only", False)
                or (trigger == "manual" and getattr(source, "manual_scan_enabled", False))
            )
        ]
        if trigger == "initial_discovery":
            eligible = [
                source
                for source in eligible
                if not self.repository.source_initialized(self._source_key(source))
            ]
        # Browser-backed third-party helpers can take tens of seconds even when
        # healthy. Keep the fast direct and inbox sources first so a delayed
        # Facebook helper can never prevent Craigslist, SpareRoom, or saved
        # search alerts from running inside the shared time budget.
        priority = {
            "Craigslist": 10,
            # One public JSON call, so it costs almost nothing to run early.
            "SF Housing Portal": 12,
            "Listings Project": 15,
            "Abacus (small buildings)": 20,
            # A handful of small JSON posts and no detail reads.
            "RentSFNow": 22,
            "AvalonBay": 24,
            "AppFolio": 26,
            "UDR": 27,
            "SpareRoom": 25,
            # A student board: one small page per scan, no detail reads.
            "Uloop": 28,
            "HotPads": 35,
            "Apartments.com": 40,
            "Zumper": 45,
            # Six search pages of about 700KB, no detail reads, and the most
            # reliable of the large portals -- six of six fetches answered
            # while it was being measured, including to an honest User-Agent.
            "Zillow": 44,
            # Two search pages and no detail reads, and the source most
            # likely to be turned away, so it is asked early while the
            # scan still has time to record the refusal.
            "Trulia": 46,
            "Redfin": 47,
            # Four search pages and no detail reads, so it costs about what
            # Redfin does and runs well before the sources that fetch a page
            # per building.
            "ApartmentGuide": 48,
            # Six search pages and no detail reads, and the deepest
            # inventory of the direct sources, so it is worth the pages.
            "Movoto": 49,
            "Roomies": 50,
            # A search page plus a detail page per building, like Rent.com.
            "Apartment List": 52,
            # Heaviest of the direct sources: a search page plus a detail page
            # per building, so it runs after the ones that read a single page.
            "Rent.com": 55,
            "Furnished Finder": 60,
            "Facebook Marketplace": 70,
            "Facebook Groups": 80,
        }
        return sorted(eligible, key=lambda source: (priority.get(source.platform, 60), source.platform.casefold()))

    @staticmethod
    def _source_key(source: ListingSource) -> str:
        return watchdog_source_key(source)

    @staticmethod
    def _not_needed(source: ListingSource, preferences: Preferences) -> str | None:
        """Why this deal has no use for a source, or None to read it as usual.

        The source answers for itself (``not_needed_for``), because only it
        knows what it lists. An answer that fails costs nothing: that source is
        read exactly as it would have been before it could be asked.
        """
        ask = getattr(source, "not_needed_for", None)
        if not callable(ask):
            return None
        try:
            reason = ask(preferences)
        except Exception:
            LOGGER.warning(
                "%s could not say whether this deal needs it, so it is read", source.platform,
                exc_info=True,
            )
            return None
        return str(reason) if reason else None

    @staticmethod
    def _provider(source: ListingSource) -> str:
        return str(
            getattr(source, "last_provider", getattr(source, "provider", source.__class__.__name__))
        )

    @staticmethod
    def _connector_key(source: ListingSource) -> str | None:
        value = getattr(source, "connector_key", None)
        return str(value) if value else None

    @staticmethod
    def _connector_state_key(source: ListingSource) -> str | None:
        value = getattr(source, "connector_state_key", None)
        if value:
            return str(value)
        return Scanner._connector_key(source)

    def refresh_gmail_connector_state(self) -> None:
        provider_states = {
            key: state
            for key, _ in GMAIL_PROVIDERS
            if (state := self.repository.connector_state(key)) is not None
        }
        state, message, observed, providers = aggregate_gmail_status(provider_states)
        self.repository.set_connector_state(
            "gmail",
            state,
            message=message,
            observed_items=observed,
            metadata={"providers": providers},
            configured=True,
            attempted=state != "configured_unverified",
            succeeded=state in {"working", "working_zero", "waiting_first_alert"},
        )

    @staticmethod
    def _within_initial_window(
        listing: ListingCandidate,
        *,
        now: datetime | None = None,
    ) -> bool:
        raw = listing.metadata.get("listing_timestamp")
        if not isinstance(raw, str) or not raw.strip():
            return True
        try:
            published = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        except ValueError:
            return True
        if published.tzinfo is None:
            published = published.replace(tzinfo=UTC)
        return published >= (now or datetime.now(UTC)) - INITIAL_DISCOVERY_WINDOW

    def _measure_missing_coverage(
        self, client: httpx.Client, preferences: Preferences
    ) -> None:
        """Record how many homes each disconnected source is holding.

        "Waiting for first alert" never says what the missing setup costs, so
        the chore has no stated payoff. A real number gives it one -- and only a
        real one: everything here answers None rather than guess, and a count
        that cannot be taken simply is not stored, leaving the page to show its
        invitation without one.

        Every failure is swallowed. This runs inside a scan whose job is
        collecting homes, and must never be the reason one fails.
        """
        try:
            targets = missing_coverage_targets(self.repository, preferences)
        except Exception:
            LOGGER.warning("Could not work out which sources to measure", exc_info=True)
            return
        try:
            stored = self.repository.source_coverage()
        except Exception:
            stored = {}
        for platform, url in targets:
            existing = stored.get(platform) or {}
            if coverage_is_fresh(existing.get("taken_at")):
                continue
            try:
                count = fetch_count(client, platform, url)
            except Exception:
                # A silent failure here would look identical to a source that
                # simply cannot be counted, which is the one thing that must
                # stay distinguishable.
                LOGGER.warning("Could not count %s", platform, exc_info=True)
                continue
            if count is None:
                LOGGER.info("%s could not be counted; showing no figure for it", platform)
                continue
            try:
                self.repository.record_source_coverage(platform, count)
                LOGGER.info("%s is holding %s listings", platform, count)
            except Exception:
                LOGGER.warning("Could not store the %s count", platform, exc_info=True)

    def _search_within_ceiling(
        self,
        source: ListingSource,
        client: httpx.Client,
        preferences: Preferences,
        trigger: str,
        *,
        deadline: float,
        budget: float,
    ) -> tuple[list[ListingCandidate], str | None]:
        """Search a source, and stop waiting on it if it stops answering.

        A stalled source cannot be interrupted from outside: it is sitting in a
        socket read, and the read timeout applies to each chunk rather than to
        the whole response, so a server sending one byte at a time holds the
        connection for as long as it likes. Left alone that is not one slow
        source, it is every source after it -- one 200-second stall skipped
        sixteen of them in a single scan.

        So the search runs on a worker thread the scan can walk away from. The
        stalled source loses its own results and reports why; nothing else
        loses anything. The thread is a daemon and shares the client, which
        httpx supports, so an abandoned read finishes into a queue nobody is
        listening to and the interpreter can still exit -- and asks its site
        for nothing more once its time is up (see ``_FetchLimit``).

        Returns the listings, and why the read stopped early if it did: a
        source that keeps what it has read when a later page is refused keeps
        it when the time runs out too, and says so.
        """
        def run() -> tuple[str, Any]:
            trigger_search = getattr(source, "search_for_trigger", None)
            return (
                trigger_search(client, preferences, trigger)
                if callable(trigger_search)
                else source.search(client, preferences)
            )

        # Never longer than the scan has left, and never so short that a
        # healthy source is cut off by a ceiling meant for a broken one.
        hard = self._source_ceiling_for(trigger)
        remaining = deadline - self._clock()
        ceiling = min(hard, max(self.timeout_seconds, remaining))
        limit = _FetchLimit(self._clock() + ceiling)
        try:
            listings = self._within_ceiling(
                f"search-{source.platform}",
                limit,
                run,
                timed_out=(
                    f"{source.platform} stopped answering partway through and was left after "
                    f"{int(ceiling)} seconds, so the rest of this scan could still run. "
                    "The next check tries it again."
                ),
                # Set by what the scan had left, not by the source's own
                # ceiling: still reading then is the check running out of time.
                out_of_time=(
                    f"Stopped part way to keep this scan within the {int(budget)}-second "
                    "time limit. The next check reads it again."
                    if ceiling < hard
                    else None
                ),
            )
        except PartialReadError as partial:
            # Cut for time, a partial read is stored and said, not failed; cut
            # at its own ceiling it is the stall it always was.
            partial.out_of_time = limit.stopped if limit.for_time else None
            raise
        return listings, limit.stopped

    def _within_ceiling(
        self,
        label: str,
        limit: _FetchLimit,
        run: Callable[[], Any],
        *,
        timed_out: str,
        out_of_time: str | None,
    ) -> Any:
        """Run one call the scan is allowed to walk away from, until ``limit.end``.

        The thread is a daemon and shares the client, which httpx supports, so
        an abandoned read finishes into a queue nobody is listening to and the
        interpreter can still exit. The call's limit goes with it: once it is
        up, the thread's next request is refused before it is sent.

        Waited on in slices against the scan's clock, which counts the time
        the computer sleeps, never on Python's own timeout, which does not: a
        wait begun just before the lid closed otherwise went on for hours.

        The time can run out three ways, each said as what it is: the computer
        slept (``SLEPT_MESSAGE``); the scan had less time left than the source
        is allowed (``out_of_time``); or, awake and inside its own ceiling, the
        source stopped answering (``timed_out``) -- the only failure of the
        three, and the only one that counts towards pausing it.
        """
        started, awake = self._clock(), self._awake_clock()
        span = limit.end - started
        outcome: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)

        def worker() -> None:
            _FETCH_LIMIT.current = limit
            try:
                outcome.put(("value", run()))
            except BaseException as error:  # reported on the scan's thread
                outcome.put(("error", error))

        threading.Thread(target=worker, name=label, daemon=True).start()
        while True:
            # The awake clock bounds it as well: it never runs ahead of the
            # scan's, and a test clock that stands still cannot wait for ever.
            remaining = min(limit.end - self._clock(), awake + span - self._awake_clock())
            if remaining <= 0:
                try:
                    # Handed back just as the time ran out: it is still an answer.
                    kind, payload = outcome.get_nowait()
                    break
                except queue.Empty:
                    pass
                limit.tripped = True
                raise self._stop(limit, started, awake, timed_out=timed_out, out_of_time=out_of_time) from None
            try:
                kind, payload = outcome.get(timeout=min(remaining, WAIT_SLICE_SECONDS))
                break
            except queue.Empty:
                continue
        if limit.tripped or (kind == "error" and self._slept_since(started, awake)):
            # A request was refused for time on the way, or the computer slept
            # while it was out -- a request the sleep cut off (the network gone,
            # a read reset on waking) is no more the site's doing: whatever
            # came back came back short, and the reason goes with it.
            stopped = self._stop(limit, started, awake, timed_out=timed_out, out_of_time=out_of_time)
            if kind == "error" and not isinstance(payload, PartialReadError):
                raise stopped from payload
        if kind == "error":
            raise payload
        return payload

    def _stop(
        self, limit: _FetchLimit, started: float, awake: float, *, timed_out: str, out_of_time: str | None
    ) -> Exception:
        """Record on ``limit`` why its call ran out of time, and return that as an error."""
        stopped = self._why_stopped(started, awake, timed_out=timed_out, out_of_time=out_of_time)
        limit.for_time = isinstance(stopped, _OutOfTime)
        limit.stopped = (
            str(stopped)
            if limit.for_time
            else "Stopped at the time one source may take; what it read before then is kept."
        )
        return stopped

    def _why_stopped(
        self, started: float, awake: float, *, timed_out: str, out_of_time: str | None
    ) -> Exception:
        """Why a call begun at ``started`` (``awake`` on the awake clock) ran out of time."""
        if self._slept_since(started, awake):
            return _OutOfTime(SLEPT_MESSAGE)
        if out_of_time is not None:
            return _OutOfTime(out_of_time)
        return SourceError(timed_out)

    def _slept_since(self, started: float, awake: float) -> bool:
        """Whether the computer slept since ``started`` (``awake`` on the awake clock)."""
        return (self._clock() - started) - (self._awake_clock() - awake) > SLEEP_NOTICE_SECONDS

    def _refuse_past_the_limit(self, request: httpx.Request) -> None:
        """Send nothing for a call whose time is up (see ``_FetchLimit``).

        The request hook of both of a scan's clients, run before every request
        and every redirect on the thread making it. The check that walked away
        from a source only stopped waiting; this is what stops the source --
        an abandoned walk read on for up to eight rent bands of thirty pages.
        """
        limit = getattr(_FETCH_LIMIT, "current", None)
        if limit is not None and self._clock() >= limit.end:
            limit.tripped = True
            raise ScanTimeUpError(
                f"Stopped before asking {request.url.host} again: the time allowed for this source had run out."
            )

    @contextmanager
    def _asking_until(self, end: float) -> Iterator[None]:
        """Hold this thread's own requests to ``end`` while inside, then put back
        whatever limit it had -- the scan runs on the scheduler's threads, which
        go on to other jobs."""
        before = getattr(_FETCH_LIMIT, "current", None)
        _FETCH_LIMIT.current = _FetchLimit(end)
        try:
            yield
        finally:
            _FETCH_LIMIT.current = before

    def _join_within(self, thread: threading.Thread, seconds: float) -> None:
        """``thread.join(seconds)``, with the seconds counted on the scan's clock."""
        started, awake = self._clock(), self._awake_clock()
        while thread.is_alive():
            remaining = min(started + seconds - self._clock(), awake + seconds - self._awake_clock())
            if remaining <= 0:
                return
            thread.join(min(remaining, WAIT_SLICE_SECONDS))

    def _enrich_within_ceiling(
        self,
        source: ListingSource,
        client: httpx.Client,
        candidate: ListingCandidate,
        *,
        limit: float,
    ) -> ListingCandidate:
        """Fetch one detail page the scan is allowed to walk away from.

        Every caller already checks the clock before asking, but the ask itself
        was unbounded, so one page that never finished spent the whole budget
        anyway. Bounded by whichever comes first: what a single page can
        reasonably want, or what this phase has left -- so no run of detail
        pages can add up to an overrun either.
        """
        ceiling = max(1.0, min(DETAIL_HARD_CEILING_SECONDS, limit - self._clock()))
        return self._within_ceiling(
            f"detail-{source.platform}",
            _FetchLimit(self._clock() + ceiling),
            lambda: source.enrich(client, candidate),
            timed_out=(
                f"{source.platform} stopped answering for {candidate.original_url} and was "
                f"left after {int(ceiling)} seconds so the scan could go on."
            ),
            out_of_time=(
                "This check's time ran out while the page was read."
                if ceiling < DETAIL_HARD_CEILING_SECONDS
                else None
            ),
        )

    def _recheck_absent(
        self,
        client: httpx.Client,
        source: ListingSource,
        preferences: Preferences,
        *,
        seen_source_ids: set[str],
        deadline: float,
        sources_remaining: int = 1,
    ) -> int:
        """Re-confirm shortlisted homes this source has stopped returning.

        Bounded by time rather than by a count, so the shortlist is covered in a
        day instead of a week. The page is what decides: a source that says the
        post is gone already sets ``verified_inactive`` and scoring already
        refuses it. Everything else is left exactly as it was -- a fetch that
        fails proves nothing, and neither does silence.

        ``sources_remaining`` includes this source, so the share it may spend is
        what is left divided by how many sources still have to run. One slow
        source therefore cannot spend the whole allowance, and the floor below
        keeps a thin share from meaning no progress at all.
        """
        if not hasattr(source, "enrich"):
            return 0
        now = self._clock()
        available = deadline - now - self.timeout_seconds
        if available <= 0:
            return 0
        share = available / max(1, int(sources_remaining))
        recheck_deadline = now + share
        floor = max(0, int(getattr(source, "recheck_floor", RECHECK_FLOOR_PER_SOURCE)))
        ceiling = max(0, int(getattr(source, "recheck_budget", RECHECK_HARD_CEILING)))
        if ceiling <= 0:
            return 0

        try:
            candidates = self.repository.shortlisted_absent_from_search(
                source.platform,
                # A source whose cards do not prove a home is still listed has
                # nothing to exclude: every one of its homes is a candidate,
                # returned by this search or not. See card_proves_listed.
                seen_source_ids if card_proves_listed(source) else set(),
                preferences.minimum_score,
                limit=ceiling,
                recheck_after=RECHECK_AFTER,
            )
        except Exception:  # a recheck must never cost the scan its results
            LOGGER.warning("%s recheck lookup failed", source.platform, exc_info=True)
            return 0

        # The floor's own deadline, so that being owed twenty reads is never the
        # same as being owed the rest of the scan.
        floor_deadline = now + max(0.0, available * RECHECK_FLOOR_TIME_SHARE)
        checked = 0
        for listing_id, candidate in candidates:
            # The floor is what a source is owed regardless of its share; past
            # that it stops at its share, and never past the scan's own deadline.
            limit = (
                min(deadline, floor_deadline)
                if checked < floor
                else min(recheck_deadline, deadline)
            )
            if self._clock() + self.timeout_seconds > limit:
                break
            try:
                confirmed = self._confirm_from_page(
                    client, source, preferences, listing_id, candidate, limit=limit
                )
            except SourceError as refusal:
                LOGGER.info("%s stopped reading pages after a refusal: %s", source.platform, refusal)
                break
            if confirmed:
                checked += 1
                if self.detail_delay_seconds:
                    time.sleep(self.detail_delay_seconds)
        return checked

    def _confirm_from_page(
        self,
        client: httpx.Client,
        source: ListingSource,
        preferences: Preferences,
        listing_id: int,
        candidate: ListingCandidate,
        *,
        limit: float,
    ) -> bool:
        """Read one home's own page and store what it said. True if anything was.

        The one place in the app that turns a page read into a row, shared by
        the two passes that ask for one: the recheck of homes a search has
        stopped returning, and the confirmation of homes priced below anything
        their size lets for. They differ only in which homes they pick and
        what they may spend; what a page proves is the same either way, and
        writing it twice would let the two drift.

        False, changing nothing, for every way of not getting an answer -- so
        the home is tried again next scan rather than counted as checked.
        """
        try:
            refreshed = self._enrich_within_ceiling(source, client, candidate, limit=limit)
        except SourceError:
            # A refusal is the site saying stop, and the page after it would
            # be refused too. Worse, on Zillow it is the same bot protection
            # that then starts refusing the search -- which costs every home
            # the source finds, to learn nothing about the few being read.
            # So it reaches the caller, which ends this source's pass.
            raise
        except Exception as exc:
            # Unreachable is not gone. Leave the home exactly as it was, and
            # leave its confirmation dates alone so it is tried again rather
            # than counted as checked.
            LOGGER.info(
                "%s could not recheck %s: %s", source.platform, candidate.original_url, exc
            )
            return False
        if refreshed is candidate:
            # The source handed back the very object it was given, so it
            # went nowhere: eight sources ship ``enrich`` as ``return
            # listing``, and Zumper's returns before any request whenever
            # the rent is already known. Having an ``enrich`` attribute was
            # read as being able to confirm, so those homes were stamped
            # confirmed without a single request -- a SpareRoom room no
            # search had returned since 5 September was printed as "Last
            # confirmed 17 September" on the author's own board, and a home
            # taken down in between would have read as current right up to
            # the email. Nothing was read, so nothing is confirmed; the
            # home keeps its real date and says nobody has checked it.
            return False
        before = facts_for_listing(candidate)
        after = facts_for_listing(classify_listing(refreshed))
        moved = not same_home(before, after, strict=False)
        if moved or (
            self.repository.is_owned(listing_id)
            # The same page vouches for nothing once it names an address
            # the row never had: an alert-email copy has none, and the
            # page may have been re-let to another flat since.
            and not same_home(before, after, strict=True, same_url=not after.address_raw)
        ):
            # The page now describes a different home, or -- for a home
            # the user starred or noted -- no longer shows it is the same
            # one. None of it may be written onto this row: a star on 405
            # Laguna must not wake up on 409. Only when both are pages
            # for one flat, and the page names another, is the flat that
            # was here known to be gone from it; anything less proves
            # nothing and the row is left exactly as it was.
            if not moved or before.grain != UNIT_GRAIN or after.grain != UNIT_GRAIN:
                return False
            refreshed = replace(
                candidate, metadata={**candidate.metadata, "verified_inactive": True}
            )
        metadata = dict(refreshed.metadata)
        metadata["last_verified_at"] = utc_now()
        # And separately: the page itself was read, which a search returning
        # the home is not. Only this answers whether a rent nobody believes
        # belongs to a listing that is still up, so it is written only when
        # the page actually showed the home -- a page saying the post is gone
        # has proved the opposite and is stored as that instead.
        if metadata.get("verified_inactive") is not True:
            metadata["page_verified_at"] = metadata["last_verified_at"]
        refreshed = replace(classify_listing(refreshed), metadata=metadata)
        try:
            self.repository.update_score(
                listing_id, score_listing(refreshed, preferences), listing=refreshed
            )
        except Exception:
            LOGGER.warning("%s recheck could not be stored", source.platform, exc_info=True)
            return False
        return True

    def _read_what_homes_cost(self) -> RentTable | None:
        """Median rents by area and size, for the whole of one scan.

        One read of the board rather than one per source: the medians run over
        sixty days and cannot move inside a scan, and every source's cheap-rent
        pass asks the same table. None when the board cannot be read, which
        that pass treats as knowing nothing about any area -- so it falls back
        to the hard floor and opens the page, because being wrong that way
        costs a request and being wrong the other way leaves a home nobody
        ever looks at.
        """
        try:
            return RentTable.from_observations(
                self.repository.rent_observations(RENT_WINDOW_DAYS)
            )
        except Exception:  # never let this cost a scan its results
            LOGGER.warning("Could not read what homes normally cost", exc_info=True)
            return None

    def _confirm_cheap_homes(
        self,
        client: httpx.Client,
        source: ListingSource,
        preferences: Preferences,
        *,
        deadline: float,
    ) -> int:
        """Open the listing page of a home too cheap to recommend unread.

        A rent below the floor for a home that size is a question the board
        answers by ranking the home below the ones that raise none. This is
        what lets it be answered the other way: the page is read, and a page
        that still shows the home puts it straight back where its score says
        it belongs. A page that says the post is gone proves it gone, through
        the same ``verified_inactive`` that has always been the only thing
        allowed to take a home off the shortlist -- and everything else,
        a refusal, a timeout, an unreachable host, leaves the home exactly as
        it was, still on the shortlist and still saying nobody has checked.

        Narrow on purpose. Only homes already good enough for the shortlist,
        only those the scorer marked below their floor, and of those only the
        ones also far below what their own area and size normally cost
        (``FAR_BELOW_MARKET_SHARE``) -- thirteen homes on the owner's board.
        Never lets a page read cost the scan its results.
        """
        if not hasattr(source, "enrich"):
            return 0
        now = self._clock()
        available = deadline - now - self.timeout_seconds
        if available <= 0:
            return 0
        # One read's worth, and then a share of what is left after it.
        # ``available`` already has that one read set aside, so this is never
        # past the deadline the calling line works to -- and a share too thin
        # for a single request still leaves room for one, which matters: a
        # pass that can never make one request is the same as no pass at all,
        # and these homes are few enough that a handful of scans clears them.
        limit = now + self.timeout_seconds + available * CHEAP_RENT_TIME_SHARE
        try:
            candidates = self.repository.cheap_homes_to_confirm(
                source.platform,
                preferences.minimum_score,
                limit=CHEAP_RENT_PAGES_PER_SCAN,
                confirm_after=RECHECK_AFTER,
            )
        except Exception:  # confirming a rent must never cost the scan its results
            LOGGER.warning("%s cheap-rent lookup failed", source.platform, exc_info=True)
            return 0

        table = self._scan_rent_table
        checked = 0
        for listing_id, candidate in candidates:
            if self._clock() + self.timeout_seconds > limit:
                break
            median = (
                table.estimate(
                    candidate.neighborhood,
                    size_of(candidate.housing_kind, candidate.unit_type),
                )
                if table is not None
                else None
            )
            # An area that is simply cheap is not a rent to doubt. Two things
            # have to be true before the median is allowed to say so.
            #
            # The board must know one: a neighbourhood and size it has too few
            # homes of has no median at all, and nothing known about the area
            # leaves the hard floor -- a statement of the same kind -- as the
            # evidence, so the page is read. Being wrong that way costs one
            # request; being wrong the other way leaves a home that may be
            # real at the bottom of the shortlist with nobody going to look.
            #
            # And the median must be a figure the app would believe as a rent
            # in the first place. One below the floor is not evidence that
            # homes here are cheap; it is the same doubt over again, counted
            # from the same rows, and a board that had collected fifty fake
            # $1,400 three-bedrooms would otherwise have them vouch for each
            # other and none of them ever be opened.
            if (
                median is not None
                and candidate.price is not None
                and int(candidate.price) > median * FAR_BELOW_MARKET_SHARE
                and not implausible_rent(replace(candidate, price=median))
            ):
                continue
            try:
                confirmed = self._confirm_from_page(
                    client, source, preferences, listing_id, candidate, limit=limit
                )
            except SourceError as refusal:
                LOGGER.info("%s stopped reading pages after a refusal: %s", source.platform, refusal)
                break
            if confirmed:
                checked += 1
                if self.detail_delay_seconds:
                    time.sleep(self.detail_delay_seconds)
        return checked

    def _retire_old_listings(self) -> int:
        """Age out homes past their three weeks, and delete aged-out homes no
        site has listed for 120 days; never let either cost a scan or an import.
        Returns how many were aged out."""
        try:
            archived, deleted = self.repository.retire_old_listings()
        except Exception:
            LOGGER.warning("Could not age out or retire old homes", exc_info=True)
            # The two share a transaction; a deletion that keeps failing must
            # not stop the shortlist ageing too.
            try:
                return self.repository.archive_stale_listings()
            except Exception:
                LOGGER.warning("Could not archive homes past their three weeks", exc_info=True)
                return 0
        if deleted:
            LOGGER.info(
                "Deleted %s aged-out listing(s) no site has listed for %s days",
                deleted,
                self.repository.RETAIN_UNSEEN_DAYS,
            )
        return archived

    def _close_expired_lotteries(self, today: date | None = None) -> int:
        """Take homes whose stated deadline to apply by has passed off the page.

        The other half of refusing a closed lottery. The city's portal skips
        them at the door now, but a board scanned before it learned to is
        still holding them -- 156 of them on the author's own, one of them
        second on the shortlist at 92 with a deadline two months behind it --
        and nothing would ever have removed them, because a source quietly
        dropping a home proves nothing and must never remove one.

        Every source is asked, not only the portal: the question is whether a
        deadline this board stores has passed, and any source that starts
        stating one gets the same answer. Never lets the sweep cost a scan.
        """
        try:
            stated = self.repository.stated_application_deadlines()
            closed = {
                listing_id: due
                for listing_id, due in stated.items()
                if application_deadline_passed(due, today=today)
            }
            moved = self.repository.close_expired_applications(closed)
        except Exception:
            LOGGER.warning("Could not close homes whose deadline to apply has passed", exc_info=True)
            return 0
        if moved:
            LOGGER.info("Closed %s home(s) whose deadline to apply by has passed", moved)
        return moved

    @staticmethod
    def _merge_stored(
        listing: ListingCandidate,
        existing: Any,
        stored_metadata: dict[str, Any],
        *,
        fresh_first: bool = False,
    ) -> ListingCandidate:
        """Keep richer stored fields when a thin search card would overwrite them.

        Detail pages carry more than search cards, so a later scan must not erase
        what an earlier enrichment learned. Used by the scan and by the browser
        import alike, so one home is merged one way whichever route it came by.

        Two things the stored copy must not win, though. A summary that is the
        same sentence with different numbers in it ("1 bed from $2,895" last
        week, "$2,795" now) is the site saying the numbers changed; keeping the
        stored one froze the old rent in the text beside the new rent in the
        price column. And an address the card cannot read, or reads without the
        unit the stored one had, is a thinner card rather than a different
        home, so the stored address stays -- it is what says which flat this is.

        ``fresh_first`` is the browser import's rule: a card the user's own
        browser read is the whole card, so its summary and type win whenever it
        states them, as they always did on that path.
        """
        if fresh_first:
            summary = listing.summary or existing["summary"]
            listing_type = listing.listing_type or existing["listing_type"]
        else:
            summary = _merged_summary(existing["summary"], listing.summary, existing["title"])
            listing_type = (
                existing["listing_type"]
                if existing["listing_type"] not in (None, "", "Room/share")
                else listing.listing_type
            )
        return replace(
            listing,
            price=listing.price if listing.price is not None else existing["price"],
            neighborhood=listing.neighborhood or existing["neighborhood"],
            summary=summary,
            listing_type=listing_type,
            metadata=_merged_metadata(stored_metadata, listing.metadata),
        )

    def _begin_progress(self, trigger: str, sources: list[ListingSource] | None = None) -> None:
        self._scan_finished.clear()
        with self._progress_lock:
            # Whatever a lane still running from before this began tries to
            # write, from here on it is dropped.
            self._progress_generation += 1
            self._in_flight_sources.clear()
            self._progress_state.update(
                {
                    "status": "running",
                    "running": True,
                    "run_id": None,
                    "trigger": trigger,
                    "started_monotonic": self._clock(),
                    "elapsed_seconds": 0,
                    "sources_total": len(sources) if sources is not None else len(self._eligible_sources(trigger)),
                    "sources_completed": 0,
                    "current_source": None,
                    "listings_seen": 0,
                    "listings_added": 0,
                    "sources_failed": 0,
                }
            )

    def _source_weights(self, sources: list[ListingSource]) -> dict[str, float]:
        """How long each source in this scan is expected to take.

        Read from what each one actually did on its own recent runs. A source
        nobody has timed yet is given the middle of what is known rather than
        nothing, so an unmeasured source does not silently weigh zero and let
        the bar reach 100% with work still to do.
        """
        try:
            measured = self.repository.typical_source_seconds()
        except Exception:  # progress must never cost a scan its results
            LOGGER.warning("could not read source durations for progress", exc_info=True)
            measured = {}
        known = sorted(measured.values())
        fallback = known[len(known) // 2] if known else 5.0
        return {
            source.platform: float(measured.get(source.platform, fallback))
            for source in sources
        }

    def _start_source_progress(
        self, source: ListingSource, source_index: int, weight: float, *, lane: _Lane | None
    ) -> None:
        """Put a source on the panel while it is being read.

        Only the main line drives the bar's estimate for the source in flight.
        A lane's source is named, and its weight added when it finishes, but it
        does not reset the main line's estimate as it goes.
        """
        with self._progress_lock:
            if lane is not None and lane.generation != self._progress_generation:
                return
            self._in_flight_sources[id(source)] = (source_index, source.platform)
            values: dict[str, object] = {"current_source": self._in_flight_label()}
            if lane is None:
                values["weight_current"] = weight
                values["current_started_monotonic"] = self._clock()
            self._progress_state.update(values)

    def _in_flight_label(self) -> str | None:
        """Every source being read right now, in the order the scan lists them.

        Called with the progress lock held. Ordered by position rather than by
        which of two threads got there first, so the heading does not swap its
        words round from one refresh to the next.
        """
        names = [platform for _, platform in sorted(self._in_flight_sources.values())]
        if not names:
            return None
        if len(names) == 1:
            return names[0]
        return ", ".join(names[:-1]) + " and " + names[-1]

    def _finish_source_progress(
        self,
        source: ListingSource,
        weight: float,
        *,
        lane: _Lane | None = None,
        tally: _SourceTally | None = None,
    ) -> None:
        """Retire a source from the bar, however it ended.

        Called for a skip and a failure as well as a success: a source that
        stops without its weight being retired leaves the bar stuck for the
        rest of the scan, which is the failure this replaced.

        Completion is counted up, never set to the source's position. A
        position is only a count while sources finish in order, and a source
        read on its own lane finishes whenever it finishes -- the first in the
        list, done last, would have moved the count backwards.
        """
        if tally is not None:
            tally.open_source_run_id = None
            tally.finished_progress.add(id(source))
        with self._progress_lock:
            if lane is not None and lane.generation != self._progress_generation:
                return
            self._in_flight_sources.pop(id(source), None)
            done = float(self._progress_state.get("weight_done", 0.0) or 0.0)
            completed = int(self._progress_state.get("sources_completed", 0) or 0)
            values: dict[str, object] = {
                "sources_completed": completed + 1,
                "current_source": self._in_flight_label(),
                "weight_done": done + max(0.0, float(weight)),
            }
            if lane is None:
                values["weight_current"] = 0.0
                values["current_started_monotonic"] = None
            self._progress_state.update(values)

    def _add_progress(self, lane: _Lane | None, **deltas: int) -> None:
        """Count something up on the panel from either line.

        Counted up rather than set, because each line only knows its own
        totals: setting what one line had seen would hide what the other had.
        """
        with self._progress_lock:
            if lane is not None and lane.generation != self._progress_generation:
                return
            for key, amount in deltas.items():
                self._progress_state[key] = int(self._progress_state.get(key, 0) or 0) + int(amount)

    def _drop_from_panel(self, source: ListingSource) -> None:
        """Take a source off the panel without counting it as finished."""
        with self._progress_lock:
            if self._in_flight_sources.pop(id(source), None) is not None:
                self._progress_state["current_source"] = self._in_flight_label()

    def _update_progress(self, **values: object) -> None:
        with self._progress_lock:
            self._progress_state.update(values)

    def _finish_progress(self, status: str) -> None:
        with self._progress_lock:
            # Whatever a lane still running from before this began tries to
            # write, from here on it is dropped.
            self._progress_generation += 1
            self._in_flight_sources.clear()
            started = self._progress_state.get("started_monotonic")
            elapsed = int(self._clock() - started) if isinstance(started, (int, float)) else 0
            self._progress_state.update(
                {
                    "status": status,
                    "running": False,
                    "current_source": None,
                    "elapsed_seconds": max(0, elapsed),
                }
            )
        # Last, so anybody woken by this reads the finished state rather than
        # the one it is in the middle of replacing.
        self._scan_finished.set()

    def wait_until_idle(self, timeout: float = 30.0) -> bool:
        """Block until no scan is in flight. False if it timed out instead.

        For callers that have to see the end of a scan they did not run inline
        -- chiefly tests, which otherwise poll ``is_running`` against a fixed
        wall-clock deadline. A deadline long enough today is only long enough
        until the thing being waited on gets slower, and a test that fails that
        way blames the code rather than the clock.

        The event on its own is not quite the answer: a run sets it inside
        ``_finish_progress``, which happens a moment before the worker lets go
        of the scan locks. Taking the lock here and dropping it again is what
        makes "idle" mean the run is entirely over rather than one instruction
        away from it, and it costs nothing on the paths that were refused
        before any lock was taken.
        """
        if not self._scan_finished.wait(timeout):
            return False
        if not self._scan_lock.acquire(timeout=max(0.0, timeout)):
            return False
        self._scan_lock.release()
        return True

    def _within_daily_budget(self, trigger: str, sources: list[ListingSource] | None) -> bool:
        """Whether this run may go out to the sources at all today.

        A scan aimed at one source is a connector being tested, not the app
        going out on its own account: it is one read of one site, asked for by
        somebody sitting in front of the setup page, and refusing it would
        break setting the source up at all. Only a full sweep is charged.
        """
        if self.scan_allowed is None or sources is not None:
            return True
        return bool(self.scan_allowed(trigger, sources))

    def start_scan(self, trigger: str = "manual", sources: list[ListingSource] | None = None) -> bool:
        """Start a scan in a daemon thread after acquiring the overlap lock.

        Acquiring before the HTTP response is sent lets the dashboard reliably show
        that a user-requested scan is in progress, instead of briefly appearing idle.
        """
        if not self.preference_loader().profile_active:
            self._report_refusal(trigger, sources, "profile_required")
            return False
        if not self._within_daily_budget(trigger, sources):
            self._report_refusal(trigger, sources, "budget_reached")
            LOGGER.info("Refused %s scan: today's source budget is spent", trigger)
            return False
        if not self._acquire_scan_locks():
            return False
        self._begin_progress(trigger, sources)
        thread = threading.Thread(
            target=self._run_locked_scan,
            args=(trigger, sources),
            name=f"sf-housing-{trigger}-scan",
            daemon=True,
        )
        try:
            thread.start()
        except Exception:
            self._finish_progress("failed")
            self._release_scan_locks()
            raise
        return True

    def _report_refusal(
        self, trigger: str, sources: list[ListingSource] | None, status: str
    ) -> None:
        """Show a refused check on the panel -- unless a scan is already running.

        A refusal is decided before the scan lock is asked for, so a Check
        refused while a scan was running used to reset that scan's panel. When
        completion was recorded as a position, the next source put the count
        right again; counted up it never recovered, and a lane's updates were
        dropped for the rest of the scan. The scan that is running owns the
        panel until it ends.
        """
        if self.is_running:
            return
        self._begin_progress(trigger, sources or [])
        self._finish_progress(status)

    def run_scan(self, trigger: str = "manual", sources: list[ListingSource] | None = None) -> ScanOutcome:
        if not self.preference_loader().profile_active:
            self._report_refusal(trigger, sources, "profile_required")
            return ScanOutcome(0, "profile_required")
        if not self._within_daily_budget(trigger, sources):
            self._report_refusal(trigger, sources, "budget_reached")
            LOGGER.info("Refused %s scan: today's source budget is spent", trigger)
            return ScanOutcome(0, "budget_reached")
        if not self._acquire_scan_locks():
            run_id = self.repository.begin_scan(trigger)
            self.repository.finish_scan(run_id, "skipped", message="Another scan is already running.")
            LOGGER.warning("Skipped %s scan because another scan is running", trigger)
            return ScanOutcome(run_id, "skipped", already_running=True)

        self._begin_progress(trigger, sources)
        return self._run_locked_scan(trigger, sources)

    def import_candidates(
        self,
        platform: str,
        search_url: str,
        listings: list[ListingCandidate],
        *,
        trigger: str = "browser_bridge",
    ) -> ScanOutcome:
        """Score and store browser-collected cards under the normal scan lock.

        A browser bridge is deliberately treated as another source run, not as a
        side channel to SQLite.  That makes its status visible in the dashboard
        and prevents it from racing an ordinary scheduled scan.
        """
        if not self.preference_loader().profile_active:
            return ScanOutcome(0, "profile_required")
        if not self._acquire_scan_locks():
            return ScanOutcome(0, "skipped", already_running=True)

        self._begin_progress(trigger, [])
        self._update_progress(sources_total=1, current_source=platform)
        run_id: int | None = None
        source_run_id: int | None = None
        seen = added = updated = 0
        final_status = "failed"
        try:
            run_id = self.repository.begin_scan(trigger)
            self._update_progress(run_id=run_id)
            source_run_id = self.repository.begin_source_run(
                run_id,
                platform,
                search_url,
                provider="browser_bridge",
                source_key=f"browser_bridge:{platform}",
            )
            preferences = self.preference_loader()

            settled: list[int] = []
            for listing in listings:
                seen += 1
                existing = self.repository.find_listing(
                    listing.platform, listing.source_id, listing.original_url, listing
                )
                if existing is not None:
                    stored_metadata = json.loads(existing["metadata_json"] or "{}")
                    listing = self._merge_stored(listing, existing, stored_metadata, fresh_first=True)
                listing = classify_listing(listing)
                result = score_listing(listing, preferences)
                matched_neighborhood = result.details.get("neighborhood", {}).get("match_label")
                if matched_neighborhood:
                    listing = replace(listing, neighborhood=str(matched_neighborhood))
                _, created = self.repository.upsert_listing(listing, result, settled=settled)
                if created:
                    added += 1
                else:
                    updated += 1
                self._update_progress(listings_seen=seen, listings_added=added)
            self._score_what_was_settled(settled, preferences)

            self.repository.finish_source_run(source_run_id, "success", seen=seen, added=added)
            self._retire_old_listings()
            self._close_expired_lotteries()
            self.repository.finish_scan(
                run_id,
                "completed",
                seen=seen,
                added=added,
                updated=updated,
            )
            self._update_progress(sources_completed=1, current_source=None)
            final_status = "completed"
            LOGGER.info("Imported %s browser cards from %s: %s new", seen, platform, added)
            return ScanOutcome(
                run_id,
                final_status,
                listings_seen=seen,
                listings_added=added,
                listings_updated=updated,
            )
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            if source_run_id is not None:
                self.repository.finish_source_run(
                    source_run_id, "error", seen=seen, added=added, message=message[:1000]
                )
            if run_id is not None:
                self.repository.finish_scan(
                    run_id,
                    "completed_with_errors",
                    seen=seen,
                    added=added,
                    updated=updated,
                    failed=1,
                    message=message[:1000],
                )
            self._update_progress(sources_completed=1, current_source=None, sources_failed=1)
            final_status = "completed_with_errors"
            LOGGER.exception("Browser bridge import from %s failed", platform)
            return ScanOutcome(
                run_id or 0,
                "completed_with_errors",
                listings_seen=seen,
                listings_added=added,
                listings_updated=updated,
                sources_failed=1,
            )
        finally:
            self._finish_progress(final_status)
            self._release_scan_locks()

    def recover_interrupted_scans(self) -> int:
        """Settle any check a stopped process left recorded as running.

        A check only ever runs while its owner holds scan.lock, so taking that
        lock is proof that none is running in any process -- which makes every
        row still marked running the work of something that died. Doing this at
        startup is what makes the Ready Check's own advice, that reopening the
        app clears an interrupted check, actually true.
        """
        if not self._acquire_scan_locks():
            return 0
        try:
            return self.repository.abandon_interrupted_scans()
        finally:
            self._release_scan_locks()

    @staticmethod
    def _source_ceiling_for(trigger: str) -> float:
        """The longest any one source may hold this kind of run.

        On the sweep a source is asked several narrower questions instead of
        one wide one -- Zillow's eight rent bands measured about a minute
        against the seventy-five seconds a waiting scan allows. Cutting it
        there abandons the source and returns nothing, which is the one outcome
        worse than a slow one.
        """
        if trigger == DEEP_SWEEP_TRIGGER:
            return DEEP_SOURCE_CEILING_SECONDS
        return SOURCE_HARD_CEILING_SECONDS

    def _seconds_until_readable(self, source: ListingSource, trigger: str) -> float:
        """How long this source still wants left alone. Zero means read it.

        Every scan re-read every source in full, and a person pressing Check
        for new homes got a full read each time. Zillow blocks by address for
        six hours or more, and it does so on rate: about ten requests a minute
        sustained earned one here. Twenty-four pages a press, pressed while
        waiting, is the shape of that -- so a source may declare a floor, and
        pressing again inside it costs nothing rather than another read.

        The nightly sweep is exempt. It runs once a day, which is its own rate
        limit, and it is the only run that reads deeply enough to be worth
        protecting from a manual check that happened to land minutes earlier.
        """
        if trigger == DEEP_SWEEP_TRIGGER:
            return 0.0
        floor = float(getattr(source, "min_seconds_between_reads", 0) or 0)
        if floor <= 0:
            return 0.0
        # The last time it was asked, not the last time it answered: during a
        # block the failures are what has to be spaced out.
        stamp = self.repository.last_source_attempt(
            source_key=self._source_key(source), platform=source.platform
        )
        if not stamp:
            return 0.0
        try:
            asked = datetime.fromisoformat(stamp)
        except ValueError:
            return 0.0
        if asked.tzinfo is None:
            asked = asked.replace(tzinfo=UTC)
        return max(0.0, floor - (datetime.now(UTC) - asked).total_seconds())

    def _budget_for(self, trigger: str) -> float:
        """How long this scan may take.

        Four minutes is what somebody watching a spinner will sit through.
        Nobody is watching the nightly sweep, and its whole job is the depth
        that four minutes cannot reach -- inheriting the interactive budget
        would have it spend the time on the first few sources and skip the ones
        it exists for.
        """
        if trigger == DEEP_SWEEP_TRIGGER:
            return self.deep_scan_max_seconds
        return self.max_scan_seconds

    def _acquire_scan_locks(self) -> bool:
        if not self._scan_lock.acquire(blocking=False):
            return False
        lock_path = self.repository.path.parent / "scan.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+", encoding="utf-8")
        try:
            acquired = try_acquire_file_lock(handle)
        except OSError:
            handle.close()
            self._scan_lock.release()
            raise
        if not acquired:
            handle.close()
            self._scan_lock.release()
            return False
        self._process_lock_handle = handle
        return True

    def _release_scan_locks(self) -> None:
        handle = self._process_lock_handle
        self._process_lock_handle = None
        if handle is not None:
            release_file_lock(handle)
            handle.close()
        self._scan_lock.release()

    def _run_locked_scan(self, trigger: str, sources: list[ListingSource] | None = None) -> ScanOutcome:
        """Run a scan while the caller holds ``_scan_lock``."""
        run_id: int | None = None
        budget = self._budget_for(trigger)
        began, began_awake = self._clock(), self._awake_clock()
        deadline = began + budget

        def asleep() -> bool:
            """Whether the computer has slept since this scan began: the scan's
            clock runs on through a sleep, and the awake one does not."""
            return self._slept_since(began, began_awake)

        scan_started_at = utc_now()
        # Each line counts into its own tally, and the lane's is only added in
        # once the lane has been joined, so no count is read while another
        # thread is still writing it.
        main_tally = _SourceTally()
        lane: _Lane | None = None
        self.last_lane_seconds = None
        final_status = "failed"
        try:
            run_id = self.repository.begin_scan(trigger)
            self._update_progress(run_id=run_id)
            LOGGER.info("Starting %s scan (run %s)", trigger, run_id)
            preferences = self.preference_loader()
            self._scan_rent_table = self._read_what_homes_cost()
            headers = dict(MONITOR_HEADERS)
            timeout = httpx.Timeout(self.timeout_seconds, connect=min(5.0, self.timeout_seconds))
            active_sources = sources if sources is not None else self._eligible_sources(trigger)
            weights = self._source_weights(active_sources)
            laned = self._lane_candidates(active_sources, scoped=sources is not None)
            # A lane from an earlier scan that outstayed its join may still be
            # reading. Starting a second one beside it would ask the same site
            # for everything twice, so this scan leaves that source alone.
            lane_blocked = bool(laned) and self._lane_thread is not None and self._lane_thread.is_alive()
            limits = httpx.Limits(max_connections=4, max_keepalive_connections=2)
            with httpx.Client(
                headers=headers,
                timeout=timeout,
                limits=limits,
                follow_redirects=True,
                event_hooks={"request": [self._refuse_past_the_limit]},
            ) as client, self._asking_until(deadline):
                self._update_progress(
                    weight_total=sum(weights.values()), weight_done=0.0
                )
                lane_ids = {id(source) for _, source in laned}
                if lane_blocked:
                    for _, source in laned:
                        self._refuse_source(run_id, source, weights.get(source.platform, 0.0))
                elif laned:
                    lane = self._start_lane(
                        laned,
                        headers=headers,
                        timeout=timeout,
                        preferences=preferences,
                        trigger=trigger,
                        deadline=deadline,
                        budget=budget,
                        run_id=run_id,
                        scan_started_at=scan_started_at,
                        active_sources=active_sources,
                        weights=weights,
                        asleep=asleep,
                    )
                main_line_deadline = self._main_line_deadline(deadline, lane)
                try:
                    for source_index, source in enumerate(active_sources, start=1):
                        if id(source) in lane_ids:
                            continue
                        if lane is not None:
                            self._wait_if_the_lane_decides_a_skip(lane, deadline, weights)
                        try:
                            self._scan_source(
                                source,
                                source_index,
                                client=client,
                                preferences=preferences,
                                trigger=trigger,
                                deadline=main_line_deadline(),
                                budget=budget,
                                run_id=run_id,
                                scan_started_at=scan_started_at,
                                active_sources=active_sources,
                                weights=weights,
                                tally=main_tally,
                                lane=None,
                                recheck_deadline=main_line_deadline,
                                asleep=asleep,
                            )
                        except BaseException:
                            # A source that ends the scan is no longer being
                            # read, and must not sit on the panel beside the
                            # lane while the scan waits for the lane.
                            self._drop_from_panel(source)
                            raise
                except (KeyboardInterrupt, SystemExit):
                    # Somebody stopped the process. The lane is a daemon and goes
                    # with it; waiting up to the rest of the scan for it would
                    # make Ctrl+C take minutes.
                    if lane is not None:
                        self._abandon_lane(lane, weights, message=INTERRUPTED_LANE_MESSAGE)
                    raise
                finally:
                    # Whether the main line finished or raised, the lane is waited
                    # for before the scan adds up its totals or measures anything
                    # else, so its homes and its failures belong to this scan.
                    if lane is not None and not lane.abandoned:
                        self._join_lane(
                            lane, run_id=run_id, trigger=trigger, deadline=deadline, weights=weights, asleep=asleep
                        )

                # Ask the disconnected sources how much they are holding. Inside
                # the client block on purpose: one line further out and the
                # client is closed, which fails every request with "Cannot send
                # a request, as the client has been closed" and looks exactly
                # like a source that cannot be counted.
                # A passenger on the scan, not part of it: it runs after every
                # source, cannot change the outcome and cannot fail it. Any
                # trigger may ask; how often these sites are really contacted is
                # bounded by how stale the stored count is.
                self._measure_missing_coverage(client, preferences)

            # Another passenger: homes first found more than three weeks ago
            # that nobody starred, noted or decided about leave the page, and
            # those no site has listed for four months leave the board.
            self._retire_old_listings()
            # And a third: a home nobody can apply for any more is not a home
            # the shortlist may go on recommending.
            self._close_expired_lotteries()
            total_seen, total_added, total_updated, sources_failed = self._scan_totals(main_tally, lane)
            # Read from what the sources recorded rather than from the clocks:
            # a sleep after the last source (during the sweep, say) left
            # nothing unread, and owes nothing.
            slept_through = self.repository.source_runs_say(run_id, SLEPT_MESSAGE)
            completed_status = (
                "interrupted" if slept_through else "completed_with_errors" if sources_failed else "completed"
            )
            self.repository.finish_scan(
                run_id,
                completed_status,
                seen=total_seen,
                added=total_added,
                updated=total_updated,
                failed=sources_failed,
                message=SLEPT_SCAN_MESSAGE if slept_through else None,
            )
            final_status = completed_status
            LOGGER.info(
                "Finished scan %s: %s seen, %s new, %s source errors",
                run_id,
                total_seen,
                total_added,
                sources_failed,
            )
            return ScanOutcome(
                run_id,
                final_status,
                listings_seen=total_seen,
                listings_added=total_added,
                listings_updated=total_updated,
                sources_failed=sources_failed,
            )
        except Exception as exc:
            total_seen, total_added, total_updated, sources_failed = self._scan_totals(main_tally, lane)
            if run_id is not None:
                self.repository.finish_scan(
                    run_id,
                    "failed",
                    seen=total_seen,
                    added=total_added,
                    updated=total_updated,
                    failed=sources_failed,
                    message=f"{type(exc).__name__}: {exc}"[:1000],
                )
            LOGGER.exception("Scan %s failed before all sources could run", run_id)
            return ScanOutcome(
                run_id or 0,
                "failed",
                listings_seen=total_seen,
                listings_added=total_added,
                listings_updated=total_updated,
                sources_failed=sources_failed,
            )
        finally:
            self._finish_progress(final_status)
            self._release_scan_locks()

    def _lane_candidates(
        self, active_sources: list[ListingSource], *, scoped: bool
    ) -> list[tuple[int, ListingSource]]:
        """The sources this scan reads on a lane of their own, with their positions.

        Only a source whose ``runs_in_own_lane`` is the value True -- not merely
        something truthy -- and never one picked by name: test stand-ins and
        helpers are called Craigslist too. A scan aimed at chosen sources is
        one deliberate read and stays in one line, a lane beside nothing saves
        nothing, and SF_HOUSING_SEQUENTIAL_SCAN=1 turns lanes off entirely.
        """
        if scoped or os.environ.get(SEQUENTIAL_SCAN_VARIABLE) == "1":
            return []
        indexed = list(enumerate(active_sources, start=1))
        chosen = [(index, source) for index, source in indexed if reads_on_its_own_lane(source)]
        if not chosen or len(chosen) == len(indexed):
            return []
        return chosen

    def _lane_join_grace(self, trigger: str) -> float:
        """How long past the deadline a lane is waited for before it is left.

        A lane can overrun the deadline by at most one search and one detail
        page, both already bounded, plus scoring and storing what they
        returned. This is those bounds and a margin, not a guess at how long
        any particular source takes.
        """
        return self._source_ceiling_for(trigger) + DETAIL_HARD_CEILING_SECONDS + LANE_JOIN_SLACK_SECONDS

    @staticmethod
    def _main_line_deadline(deadline: float, lane: _Lane | None) -> Callable[[], float]:
        """The deadline the main line works to, worked out afresh each time it is asked.

        See _Lane for why the main line is charged with the lane's time. Asked
        as each source starts, and again as its recheck pass starts, so a
        source begun while the lane is still reading, and rechecking after it
        has finished, is charged what the lane has really taken by then.
        """
        if lane is None:
            return lambda: deadline
        return lambda: deadline - lane.shift()

    def _wait_if_the_lane_decides_a_skip(
        self, lane: _Lane, deadline: float, weights: dict[str, float]
    ) -> None:
        """Wait on the lane when only the time it has yet to take would skip a source.

        While the lane is reading, the main line is charged its usual time,
        which is a guess at this run. A guess too high skipped the next source
        for time, and a skip is instant, so every source after it was reached
        at once and skipped as well -- the rest of a scan gone, on a scan that
        read in turn finishes with time to spare.

        A skip is already certain when even the time the lane has actually
        taken leaves no room, and impossible when its usual time does: only in
        between does it turn on how long the lane will take, and only there
        does the main line wait. The clock and the lane's time both move while
        it waits, so after half the gap the skip is certain if the lane has not
        finished. Nothing is read while it waits.
        """
        started = lane.started_at()
        if started is None or lane.thread is None or not lane.thread.is_alive():
            return
        now = self._clock()
        elapsed = now - started
        reach = now + self.timeout_seconds
        if reach <= deadline - max(elapsed, lane.expected_seconds):
            return
        if reach > deadline - elapsed:
            return
        self._hand_bar_to_lane(lane, weights)
        self._join_within(lane.thread, ((deadline - elapsed) - reach) / 2)

    def _start_lane(
        self, laned: list[tuple[int, ListingSource]], **scan: Any
    ) -> _Lane:
        with self._progress_lock:
            generation = self._progress_generation
        weights: dict[str, float] = scan["weights"]
        lane = _Lane(
            self._clock,
            laned,
            expected_seconds=sum(weights.get(source.platform, 0.0) for _, source in laned),
            generation=generation,
        )
        thread = threading.Thread(
            target=self._run_lane, args=(lane,), kwargs=scan, name=LANE_THREAD_NAME, daemon=True
        )
        lane.thread = thread
        self._lane_thread = thread
        # Started from the scan's own thread, before the main line reads
        # anything, so the lane's clock and the main line's agree on when the
        # lane began.
        lane.mark_started()
        thread.start()
        return lane

    def _run_lane(
        self,
        lane: _Lane,
        *,
        headers: dict[str, str],
        timeout: httpx.Timeout,
        preferences: Preferences,
        trigger: str,
        deadline: float,
        budget: float,
        run_id: int,
        scan_started_at: str,
        active_sources: list[ListingSource],
        weights: dict[str, float],
        asleep: Callable[[], bool] = lambda: False,
    ) -> None:
        try:
            # Its own client, with the scan's headers, timeout and limits. On the
            # scan's client a lane took one of the four connections the scan
            # keeps for reads it has walked away from, and widening that pool to
            # make room let a fifth connection reach a site already holding
            # four. Cookies are kept per site, so Craigslist's requests are
            # exactly what they were -- and the scan closing its own client can
            # never cut the lane off part way.
            with httpx.Client(
                headers=headers,
                timeout=timeout,
                limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
                follow_redirects=True,
                event_hooks={"request": [self._refuse_past_the_limit]},
            ) as client, self._asking_until(deadline):
                for source_index, source in lane.sources:
                    if lane.abandoned:
                        # The scan has already settled every source this lane
                        # had not finished; starting one now would only write
                        # into a check that is over.
                        break
                    failed_before = lane.tally.failed
                    try:
                        self._scan_source(
                            source,
                            source_index,
                            client=client,
                            preferences=preferences,
                            trigger=trigger,
                            deadline=deadline,
                            budget=budget,
                            run_id=run_id,
                            scan_started_at=scan_started_at,
                            active_sources=active_sources,
                            weights=weights,
                            tally=lane.tally,
                            lane=lane,
                            # The lane's source keeps exactly the time it had at
                            # the front of the queue, which is where it was.
                            recheck_deadline=lambda: deadline,
                            asleep=asleep,
                        )
                    except Exception as error:
                        self._lane_source_failed(
                            lane,
                            source,
                            run_id=run_id,
                            weight=weights.get(source.platform, 0.0),
                            error=error,
                            already_counted=lane.tally.failed > failed_before,
                        )
        except Exception as error:
            # The lane could not so much as open its client. Every source it had
            # not settled is recorded as failed, rather than simply missing from
            # the scan with nothing to say why.
            for _, source in lane.sources:
                if id(source) not in lane.tally.finished_progress:
                    self._lane_source_failed(
                        lane, source, run_id=run_id, weight=weights.get(source.platform, 0.0), error=error
                    )
        finally:
            lane.mark_finished()

    def _lane_source_failed(
        self,
        lane: _Lane,
        source: ListingSource,
        *,
        run_id: int,
        weight: float,
        error: Exception,
        already_counted: bool = False,
    ) -> None:
        """Record a failure that escaped the source's own error handling.

        A failure inside a search is already recorded against the source. One
        outside it would otherwise end the lane's thread silently, leave the
        source's row saying "running" for ever and its name on the panel. On
        the main line the same failure ends the scan, as it always has; a lane
        is not allowed to take the main line's results down with it.
        """
        LOGGER.exception("%s failed on its own lane; the rest of the scan carries on", source.platform)
        # The source's own handling may already have counted this failure
        # before a second one escaped -- a row it could not write, say. Counted
        # again, one broken source would read as two.
        if not already_counted:
            lane.tally.failed += 1
            self._add_progress(lane, sources_failed=1)
        message = f"{type(error).__name__}: {error}"[:1000]
        try:
            row = lane.tally.open_source_run_id
            if row is None:
                row = self.repository.begin_source_run(
                    run_id,
                    source.platform,
                    source.search_url,
                    self._provider(source),
                    self._source_key(source),
                )
            self.repository.finish_source_run(
                row,
                "error",
                message=message,
                provider=self._provider(source),
                source_key=self._source_key(source),
            )
        except Exception:
            LOGGER.warning("Could not record why %s failed", source.platform, exc_info=True)
        if id(source) not in lane.tally.finished_progress:
            self._finish_source_progress(source, weight, lane=lane, tally=lane.tally)

    def _refuse_source(self, run_id: int, source: ListingSource, weight: float) -> None:
        """Record a lane source this scan declined to read, and retire it."""
        row = self.repository.begin_source_run(
            run_id,
            source.platform,
            source.search_url,
            self._provider(source),
            self._source_key(source),
        )
        self.repository.finish_source_run(
            row,
            "skipped",
            message=STALE_LANE_MESSAGE,
            provider=self._provider(source),
            source_key=self._source_key(source),
        )
        self._finish_source_progress(source, weight)

    def _join_lane(
        self,
        lane: _Lane,
        *,
        run_id: int,
        trigger: str,
        deadline: float,
        weights: dict[str, float],
        asleep: Callable[[], bool] = lambda: False,
    ) -> None:
        """Wait for the lane, for as long as it can legitimately take and no longer."""
        assert lane.thread is not None
        self._hand_bar_to_lane(lane, weights)
        try:
            self._join_within(lane.thread, max(0.0, deadline - self._clock()) + self._lane_join_grace(trigger))
            if not lane.thread.is_alive():
                lane.joined = True
                self.last_lane_seconds = lane.seconds()
                LOGGER.info(
                    "%s read on its own lane in %.1f seconds",
                    ", ".join(source.platform for _, source in lane.sources),
                    self.last_lane_seconds or 0.0,
                )
                return
            if asleep():
                # Past its time because the computer slept, not because a site
                # held it: said so, and counted against nobody.
                LOGGER.warning(
                    "%s was still reading when the computer slept past this scan's time limit",
                    ", ".join(source.platform for _, source in lane.sources),
                )
                self._abandon_lane(
                    lane, weights, message=SLEPT_MESSAGE, failed=False, run_id=run_id, not_reached=SLEPT_MESSAGE
                )
                return
            LOGGER.error(
                "%s was still reading past this scan's time limit and was left to finish on its own",
                ", ".join(source.platform for _, source in lane.sources),
            )
            self._abandon_lane(
                lane,
                weights,
                message=LEFT_BEHIND_MESSAGE,
                run_id=run_id,
                not_reached=(
                    f"Skipped to keep this scan within the {int(self._budget_for(trigger))}-second time limit."
                ),
            )
        finally:
            # Nothing is in flight any more: the lane's weight has been added by
            # its own finish, or retired just now.
            self._update_progress(weight_current=0.0, current_started_monotonic=None)

    def _hand_bar_to_lane(self, lane: _Lane, weights: dict[str, float]) -> None:
        """Keep the bar and the time left moving while the scan waits on the lane.

        The main line owns the estimate for the source in flight, and once it
        has nothing left to read that estimate is nothing -- so a scan waiting
        on Craigslist at the end showed a bar and a time left that stood still
        until Craigslist was done.
        """
        started = lane.started_at()
        with self._progress_lock:
            if started is None or lane.generation != self._progress_generation:
                return
            waiting = sum(
                weights.get(source.platform, 0.0)
                for _, source in lane.sources
                if id(source) not in lane.tally.finished_progress
            )
            if waiting > 0:
                self._progress_state.update(
                    {"weight_current": waiting, "current_started_monotonic": started}
                )

    def _abandon_lane(
        self,
        lane: _Lane,
        weights: dict[str, float],
        *,
        message: str,
        failed: bool = True,
        run_id: int | None = None,
        not_reached: str | None = None,
    ) -> None:
        """Stop waiting on a lane, and settle its sources in this scan's record.

        As failures, unless ``failed`` is False: a lane left because the
        computer slept is not reached, which nothing counts against a site.
        With ``not_reached``, each source the lane never started is written
        down as not reached, for that reason: a lane left behind starts
        nothing more, and without a row of its own a source goes on showing
        what the check before this one said about it.
        """
        lane.abandoned = True
        with self._progress_lock:
            # From here on nothing the lane does reaches this scan's numbers, or
            # any later scan's.
            lane.generation = None
        unfinished = [source for _, source in lane.sources if id(source) not in lane.tally.finished_progress]
        lane.left_behind = len(unfinished) if failed else 0
        for source in unfinished:
            self._finish_source_progress(source, weights.get(source.platform, 0.0))
        if failed:
            self._add_progress(None, sources_failed=len(unfinished))
        row = lane.tally.open_source_run_id
        if row is not None and unfinished:
            try:
                self.repository.finish_source_run(
                    row,
                    "error" if failed else "skipped",
                    message=message,
                    provider=self._provider(unfinished[0]),
                    source_key=self._source_key(unfinished[0]),
                )
            except Exception:
                LOGGER.warning("Could not record that the lane was left behind", exc_info=True)
        if run_id is not None and not_reached:
            for source in unfinished[1:] if row is not None else unfinished:
                try:
                    started = self.repository.begin_source_run(
                        run_id, source.platform, source.search_url, self._provider(source), self._source_key(source)
                    )
                    self.repository.finish_source_run(
                        started,
                        "skipped",
                        message=not_reached,
                        provider=self._provider(source),
                        source_key=self._source_key(source),
                    )
                except Exception:
                    LOGGER.warning("Could not record that %s was not reached", source.platform, exc_info=True)

    @staticmethod
    def _scan_totals(main_tally: _SourceTally, lane: _Lane | None) -> tuple[int, int, int, int]:
        """The scan's totals from its lines' own counts.

        A lane that was joined is added in whole. One that was left behind may
        still be writing, so none of its counts are read; each source it had
        not finished when it was left counts as a failure of this scan -- the
        same number the panel and the log gave, whatever the lane does after.
        """
        tallies = [main_tally]
        left_behind = 0
        if lane is not None:
            if lane.joined:
                tallies.append(lane.tally)
            elif lane.abandoned:
                left_behind = lane.left_behind
        return (
            sum(tally.seen for tally in tallies),
            sum(tally.added for tally in tallies),
            sum(tally.updated for tally in tallies),
            sum(tally.failed for tally in tallies) + left_behind,
        )

    def _scan_source(
        self,
        source: ListingSource,
        source_index: int,
        *,
        client: httpx.Client,
        preferences: Preferences,
        trigger: str,
        deadline: float,
        budget: float,
        run_id: int,
        scan_started_at: str,
        active_sources: list[ListingSource],
        weights: dict[str, float],
        tally: _SourceTally,
        lane: _Lane | None,
        recheck_deadline: Callable[[], float],
        asleep: Callable[[], bool] = lambda: False,
    ) -> None:
        """Read one source from start to finish, on whichever line calls it.

        The body the scan loop always had, moved rather than rewritten. Every
        early way out that was a ``continue`` is a ``return``; the counts go
        into the calling line's own ``tally`` instead of the scan's running
        totals, so no count is ever shared between threads; and the deadline
        handed to the recheck pass is asked for again when that pass starts,
        from ``recheck_deadline``. ``deadline`` is whatever the calling line
        works to: the scan's own on a lane, and on the main line the scan's
        less the lane's time (see _Lane).
        """
        self._start_source_progress(
            source, source_index, weights.get(source.platform, 0.0), lane=lane
        )
        source_key = self._source_key(source)
        source_run_id = self.repository.begin_source_run(
            run_id,
            source.platform,
            source.search_url,
            self._provider(source),
            source_key,
        )
        tally.open_source_run_id = source_run_id
        if source.mode != "automatic":
            self.repository.finish_source_run(
                source_run_id,
                source.mode,
                message=source.manual_reason,
                source_key=self._source_key(source),
            )
            self._finish_source_progress(
                source, weights.get(source.platform, 0.0), lane=lane, tally=tally
            )
            return
        # A source this deal can make no use of is not asked at all, and its run
        # says why in its own words. SpareRoom used to decide this inside its
        # search and answer with an empty list, so every scan filed a successful
        # check of it that had found nothing -- "working, no matches, a valid
        # result" -- for a deal with no private room in it, when no request had
        # left the machine. That also ran the recheck pass behind it.
        reason = self._not_needed(source, preferences)
        if reason:
            self.repository.finish_source_run(
                source_run_id,
                "not_needed",
                message=reason,
                provider=self._provider(source),
                source_key=self._source_key(source),
            )
            self._finish_source_progress(
                source, weights.get(source.platform, 0.0), lane=lane, tally=tally
            )
            return
        # Some sources ask not to be read again so soon, and this
        # applies to a check somebody pressed as much as to a
        # scheduled one: pressing again while waiting is precisely
        # how an address gets blocked. Nothing is lost by declining
        # -- the homes from minutes ago are already in the pool.
        wait = self._seconds_until_readable(source, trigger)
        if wait > 0:
            self.repository.finish_source_run(
                source_run_id,
                "skipped",
                message=(
                    f"Checked less than {int(wait // 60) + 1} minute(s) ago. "
                    "Reading it again this soon is what gets a source to refuse us, "
                    "and its homes are already collected."
                ),
                provider=self._provider(source),
                source_key=self._source_key(source),
            )
            self._finish_source_progress(
                source, weights.get(source.platform, 0.0), lane=lane, tally=tally
            )
            return
        # A source that failed repeatedly gets a short, persisted
        # automatic cooldown.  This avoids hammering a broken
        # external page after wake/restart, while an explicit user
        # check remains an intentional one-time retry.
        if trigger in AUTOMATIC_TRIGGERS:
            backoff = source_is_in_backoff(self.repository, source)
            if backoff is not None:
                self.repository.finish_source_run(
                    source_run_id,
                    "backoff",
                    message=backoff.action,
                    provider=self._provider(source),
                    source_key=self._source_key(source),
                )
                LOGGER.warning(
                    "%s source deferred during automatic backoff after %s failures",
                    source.platform,
                    backoff.failure_streak,
                )
                self._finish_source_progress(
                    source, weights.get(source.platform, 0.0), lane=lane, tally=tally
                )
                return
        if self._clock() + self.timeout_seconds > deadline:
            self.repository.finish_source_run(
                source_run_id,
                "skipped",
                message=(
                    SLEPT_MESSAGE
                    if asleep()
                    else f"Skipped to keep this scan within the {int(budget)}-second time limit."
                ),
                source_key=self._source_key(source),
            )
            self._finish_source_progress(
                source, weights.get(source.platform, 0.0), lane=lane, tally=tally
            )
            return

        source_seen = source_added = source_updated = detail_failures = 0
        source_fetched = source_parsed = source_classified = source_deduplicated = 0
        source_hard_filtered = source_active = source_archived = 0
        cut_short: PartialReadError | None = None
        stopped: str | None = None
        try:
            try:
                listings, stopped = self._search_within_ceiling(
                    source, client, preferences, trigger, deadline=deadline, budget=budget
                )
            except PartialReadError as partial:
                # The read stopped part way, holding homes it had already read.
                # They are stored below exactly as a finished read's would be,
                # and the failure is recorded once they are -- unless what
                # stopped it was the check's time, which is no failure at all.
                listings, cut_short = partial.listings, partial
                stopped = getattr(partial, "out_of_time", None)
                if stopped:
                    # The check ran out of its own time, which is nothing the
                    # site did, so this is not filed as a failure. It is still
                    # a part-read source: ``stopped`` is what keeps it from
                    # being quoted as a search that covered anything, below.
                    cut_short = None
            source_fetched = len(listings)
            source_parsed = len(listings)
            if trigger == "initial_discovery":
                listings = [
                    listing for listing in listings if self._within_initial_window(listing)
                ]
            detail_budget = max(0, int(source.detail_budget))

            # Pass 1: resolve stored state and score provisionally,
            # without touching the network. Spending the detail budget
            # in arrival order meant the first ten results consumed it
            # regardless of quality, so most homes a user actually sees
            # never got the posting date that lives on a detail page.
            prepared: list[dict[str, Any]] = []
            for listing in listings:
                source_seen += 1
                tally.seen += 1
                # On the panel the moment it is counted here, keeping the bar moving
                # while results are read -- and so a source that fails part way
                # through a home cannot leave the panel one short of the record.
                self._add_progress(lane, listings_seen=1)
                existing = self.repository.find_listing(
                    listing.platform, listing.source_id, listing.original_url, listing
                )
                if existing is not None:
                    source_deduplicated += 1
                stored_metadata = (
                    json.loads(existing["metadata_json"] or "{}") if existing is not None else {}
                )
                needs_details = (
                    existing is None
                    or not existing["summary"]
                    or existing["summary"] == existing["title"]
                    or (
                        listing.platform == "Craigslist"
                        and listing.housing_kind == "whole_unit"
                        and stored_metadata.get("craigslist_detail_checked") is not True
                    )
                    # The general form of the clause above: a source
                    # whose card is only a pointer says so, and keeps
                    # saying so until a detail page answers. Without
                    # it one rate-limited fetch strands the home with
                    # whatever thin card it arrived on.
                    or stored_metadata.get("detail_pending") is True
                )
                # Scored against a merged copy so an already-enriched
                # listing is ranked on what is actually known about it.
                # The raw card is what gets enriched, exactly as before.
                preview = (
                    self._merge_stored(listing, existing, stored_metadata)
                    if existing is not None
                    else listing
                )
                # The provisional score exists to rank the queue for
                # the detail budget, and nothing else reads it. Nine
                # of the sources here have no detail budget at all,
                # so for them this was scoring every listing twice
                # and throwing one away -- 250 regex searches per
                # listing, 1,959 listings, for a number never used.
                provisional = 0
                if detail_budget:
                    try:
                        provisional = score_listing(
                            classify_listing(preview), preferences
                        ).score
                    except Exception:  # a thin card must never be lost to scoring
                        provisional = 0
                prepared.append(
                    {
                        "listing": listing,
                        "existing": existing,
                        "stored_metadata": stored_metadata,
                        "needs_details": needs_details,
                        "provisional": provisional,
                        "enriched": False,
                    }
                )

            # Pass 2: spend the budget on the best candidates that still
            # need a detail page. Ties keep document order, which for a
            # date-sorted source means the newer listing wins.
            chosen = sorted(
                (index for index, item in enumerate(prepared) if item["needs_details"]),
                key=lambda index: (-prepared[index]["provisional"], index),
            )[:detail_budget]
            for index in sorted(chosen):
                if self._clock() + self.timeout_seconds > deadline:
                    break
                item = prepared[index]
                try:
                    item["listing"] = self._enrich_within_ceiling(
                        source, client, item["listing"], limit=deadline
                    )
                    item["enriched"] = True
                except _OutOfTime:
                    # The check's time is up: no more pages, and no page's fault.
                    break
                except Exception as exc:  # one expired/broken detail must not lose the search result
                    detail_failures += 1
                    LOGGER.warning(
                        "%s detail fetch failed for %s: %s",
                        source.platform,
                        item["listing"].original_url,
                        exc,
                    )
                if self.detail_delay_seconds:
                    time.sleep(self.detail_delay_seconds)

            # Pass 4 is below; pass 3 first, so a home this search
            # did return is up to date before anything is rechecked.
            # Pass 3: classify, score and store every listing.
            settled: list[int] = []
            for item in prepared:
                listing = item["listing"]
                existing = item["existing"]
                if not item["enriched"] and existing is not None:
                    listing = self._merge_stored(listing, existing, item["stored_metadata"])
                listing = classify_listing(listing)
                source_classified += 1
                result = score_listing(listing, preferences)
                if result.eligibility == "ineligible":
                    source_hard_filtered += 1
                if (
                    result.eligibility != "ineligible"
                    and result.score >= preferences.minimum_score
                ):
                    source_active += 1
                else:
                    source_archived += 1
                matched_neighborhood = result.details.get("neighborhood", {}).get("match_label")
                if matched_neighborhood:
                    listing = replace(listing, neighborhood=str(matched_neighborhood))
                # A search that returns a home is the source saying it
                # still lists it, which is a confirmation and a
                # stronger one than re-reading a single page. Stamping
                # it here is what keeps the recheck pass aimed only at
                # the homes nobody has heard about.
                #
                # Unless the card is not the source's current answer.
                # Zillow goes on returning homes its own pages call off
                # the market, so taking its card as a confirmation wrote
                # "checked today" onto homes nobody had looked at, and
                # put them behind the seven-hour bar that keeps the
                # recheck off homes already seen to -- the queue that
                # would have caught them. A page this scan really read
                # still counts, for any source. See card_proves_listed.
                confirmed: dict[str, str] = {}
                if card_proves_listed(source) or item["enriched"]:
                    confirmed["last_verified_at"] = scan_started_at
                # A detail page this scan actually read is the stronger
                # confirmation, and the only one that answers whether a rent
                # nobody believes belongs to a listing still up. Written here
                # so a cheap home the scan has just opened is not opened a
                # second time by the pass below for the same answer.
                if item["enriched"] and listing.metadata.get("verified_inactive") is not True:
                    confirmed["page_verified_at"] = scan_started_at
                listing = replace(
                    listing,
                    metadata={**listing.metadata, **confirmed},
                )
                _, created = self.repository.upsert_listing(listing, result, settled=settled)
                if created:
                    source_added += 1
                    tally.added += 1
                    self._add_progress(lane, listings_added=1)
                else:
                    source_updated += 1
                    tally.updated += 1
            self._score_what_was_settled(settled, preferences)
            if cut_short is not None:
                # Everything it read is stored; now it is recorded as the
                # failure it was, with those counts, so the backoff still hears
                # a source that keeps turning us away. It skips pass 4: the
                # homes a search that stopped half way did not return are not
                # evidence of anything.
                raise cut_short.cause
            # Pass 4: confirm the shortlisted homes whose rent is below
            # anything their size lets for, before they are recommended.
            # Before the recheck below, because these are homes the search
            # did return: they are about to appear near the top of the
            # shortlist, and there are few of them.
            confirmed_cheap = 0 if stopped else self._confirm_cheap_homes(
                client, source, preferences, deadline=recheck_deadline()
            )
            # Pass 5: go and look at the shortlisted homes this
            # search stopped returning. A listing is enriched once
            # and never revisited, so a room verified live on Monday
            # and taken down on Wednesday stayed on the shortlist
            # looking as current as one posted this morning. Absence
            # from one page is not proof, so this checks the page
            # rather than inferring anything from the silence, and
            # only runs where the search itself succeeded -- in full: a
            # read the check's time cut short returned only what it got to.
            rechecked = 0 if stopped else self._recheck_absent(
                client,
                source,
                preferences,
                seen_source_ids={item["listing"].source_id for item in prepared},
                deadline=recheck_deadline(),
                # Only the sources that can actually recheck. Ten of
                # the twenty-four have no enrich at all, so counting
                # them reserved a share none of them could ever
                # spend and handed the smallest slice to Craigslist,
                # which runs third and carries the deepest queue.
                sources_remaining=_rechecking_sources_remaining(
                    active_sources, source_index
                ),
            )
            message = (
                f"Completed with {detail_failures} detail-page warning(s)."
                if detail_failures
                else getattr(source, "empty_result_message", None)
                if source_seen == 0
                else None
            )
            if rechecked:
                note = f"Rechecked {rechecked} home(s) this search no longer lists."
                message = f"{message} {note}" if message else note
            if confirmed_cheap:
                note = f"Opened {confirmed_cheap} listing(s) priced below what their size lets for."
                message = f"{message} {note}" if message else note
            if stopped:
                message = f"{message} {stopped}" if message else stopped
            self.repository.finish_source_run(
                source_run_id,
                "success",
                seen=source_seen,
                added=source_added,
                message=message,
                provider=self._provider(source),
                source_key=self._source_key(source),
                updated=source_updated,
                fetched=source_fetched,
                parsed=source_parsed,
                classified=source_classified,
                deduplicated=source_deduplicated,
                hard_filtered=source_hard_filtered,
                active=source_active,
                archived=source_archived,
                # Against what the search actually fetched, before the
                # initial-discovery window thins it: the ceiling is on how
                # many homes the source may hand over, and a read that hit it
                # stopped short whatever was kept afterwards.
                covered=_search_covered_inventory(
                    source, preferences, source_fetched, cut_short=bool(stopped)
                ),
            )
            if trigger == "initial_discovery":
                self.repository.mark_source_initialized(source_key, "success", message)
            connector_key = self._connector_key(source)
            connector_state_key = self._connector_state_key(source)
            if connector_key and connector_state_key:
                previous = self.repository.connector_state(connector_state_key)
                observed = (previous.observed_items if previous else 0) + source_seen
                alerts_seen = max(0, int(getattr(source, "last_alert_count", 0)))
                if connector_key == "gmail":
                    state = (
                        "working"
                        if source_seen > 0
                        else "degraded"
                        if alerts_seen > 0
                        else "waiting_first_alert"
                    )
                    state_message = (
                        f"Imported {source_seen} {source.platform} listing{'' if source_seen == 1 else 's'} from a saved-search alert."
                        if source_seen
                        else (
                            getattr(source, "empty_result_message", None)
                            or f"{source.platform} alerts were found, but no supported listing was parsed."
                        )
                        if alerts_seen
                        else f"No {source.platform} saved-search alert has arrived yet."
                    )
                else:
                    state = "working" if observed > 0 else "working_zero"
                    state_message = (
                        f"{source.platform} returned {source_seen} listing{'' if source_seen == 1 else 's'}."
                        if source_seen
                        else f"{source.platform} checked successfully with no matching listings."
                    )
                self.repository.set_connector_state(
                    connector_state_key,
                    state,
                    message=state_message,
                    observed_items=observed,
                    attempted=True,
                    succeeded=state in {"working", "working_zero", "waiting_first_alert"},
                )
                if connector_key == "gmail":
                    self.refresh_gmail_connector_state()
            LOGGER.info(
                "%s scan succeeded: %s seen, %s new",
                source.platform,
                source_seen,
                source_added,
            )
        except _OutOfTime as cut:
            # Out of time -- the check's own, or the computer asleep -- before
            # the search brought anything back. Not reached, as a source passed
            # over for time is, and never a failure: failures count towards
            # pausing a source, and this is nothing the site did.
            self.repository.finish_source_run(
                source_run_id,
                "skipped",
                seen=source_seen,
                added=source_added,
                message=str(cut),
                provider=self._provider(source),
                source_key=self._source_key(source),
            )
            LOGGER.warning("%s was not read to the end: %s", source.platform, cut)
        except Exception as exc:
            tally.failed += 1
            self._add_progress(lane, sources_failed=1)
            message = f"{type(exc).__name__}: {exc}"
            # A SourceError is already written as a sentence for the person
            # reading the card. Prefixing it with the exception's class name
            # turned "Apify rejected the API token" into something that looks
            # like a crash report; the diagnostic form stays on the run record.
            human = str(exc) if isinstance(exc, SourceError) else message
            self.repository.finish_source_run(
                source_run_id,
                "error",
                seen=source_seen,
                added=source_added,
                message=message[:1000],
                provider=self._provider(source),
                source_key=self._source_key(source),
                updated=source_updated,
                fetched=source_fetched,
                parsed=source_parsed,
                classified=source_classified,
                deduplicated=source_deduplicated,
                hard_filtered=source_hard_filtered,
                active=source_active,
                archived=source_archived,
            )
            if trigger == "initial_discovery":
                self.repository.mark_source_initialized(source_key, "error", message[:1000])
            connector_key = self._connector_key(source)
            connector_state_key = self._connector_state_key(source)
            if connector_key and connector_state_key:
                self.repository.set_connector_state(
                    connector_state_key,
                    # A source that knows what went wrong outranks a
                    # guess made from its message text.
                    getattr(exc, "connector_state", None)
                    or connector_state_for_error(message),
                    message=human[:1000],
                    observed_items=source_seen,
                    attempted=True,
                )
                if connector_key == "gmail":
                    self.refresh_gmail_connector_state()
            LOGGER.exception("%s source failed without stopping other sources", source.platform)
        self._finish_source_progress(
            source, weights.get(source.platform, 0.0), lane=lane, tally=tally
        )


    def rescore_all(self, preferences: Preferences | None = None) -> int:
        active_preferences = preferences or self.preference_loader()
        if not active_preferences.profile_active:
            # No mark is left: a draft deal has no scores worth vouching for,
            # and recording one would let the pass owed after it is finished be
            # skipped.
            return 0
        # The homes still in play; a home the archive has aged out keeps the
        # scores it left the shortlist with until it is restored (see
        # ``Repository.live_candidates`` and ``rescore_home``).
        candidates = self.repository.live_candidates()
        # One connection for the whole board. A connection per listing costs an
        # fsync on every close, which on 870 homes was half a minute of disk
        # sync while start-up waited on it and the dashboard was unreachable.
        # Only claim the board when nothing else is writing to it. A scan
        # already in flight loaded its answers when it started, so it goes on
        # storing scores from the deal this pass has just replaced -- the board
        # ends up part new and part old, and a mark covering all of it would
        # make that permanent by skipping the pass that would have fixed it.
        # ``None`` clears any existing mark, so the next start scores again.
        #
        # The lane is asked about separately because it outlives the lock: the
        # main line releases the scan lock while the lane is still storing, so
        # ``is_running`` alone left a window where a deal saved at just the
        # wrong moment claimed a board the lane was about to write old scores
        # into.
        mark = None if self._board_has_another_writer() else current_fingerprint(active_preferences)
        with self.repository.connection() as connection:
            scored = self._rescore(candidates, active_preferences, connection, mark=mark)
        LOGGER.info("Rescored %s stored listings", scored)
        return scored

    def rescore_home(self, listing_id: int, preferences: Preferences | None = None) -> int:
        """Score every copy of one home against the deal in hand. Returns rows scored.

        ``rescore_all`` leaves a home the archive aged out with the scores it
        had then. Restoring, starring, passing or noting one puts it back in
        front of the reader, so it is scored first -- through the same pass,
        so exactly as every other home was. The board's mark is put back as
        it was: this home now agrees with it, and a mark that was already out
        of date stays out of date, so the next start still scores the rest.
        """
        active_preferences = preferences or self.preference_loader()
        if not active_preferences.profile_active:
            return 0
        candidates = self.repository.home_rows(listing_id)
        if not candidates:
            return 0
        mark = self.repository.scoring_mark()
        with self.repository.connection() as connection:
            return self._rescore(candidates, active_preferences, connection, mark=mark)

    def _score_what_was_settled(self, settled: list[int], preferences: Preferences) -> None:
        """Score the copies a scan's writes brought into a home the user decided about.

        A copy found later arrives starred or passed, and so does an older
        copy a new card joins to the home (``Repository.upsert_listing``).
        One the archive had aged out still holds the verdict of the deal it
        left the shortlist with -- ``rescore_all`` scores only the homes in
        play -- and as the user's own copy it is the one shown. So it is
        scored now, against the deal this scan holds, as ``rescore_home``
        scores a home the user restores.
        """
        if not settled or not preferences.profile_active:
            return
        candidates = sorted(self.repository.candidates(sorted(set(settled))).items())
        if not candidates:
            return
        mark = self.repository.scoring_mark()
        with self.repository.connection() as connection:
            self._rescore(candidates, preferences, connection, mark=mark)

    def _board_has_another_writer(self) -> bool:
        """Is anything other than this pass still storing listings?

        Three questions, because a writer can be in three places. The main line
        holds the scan lock, which ``is_running`` reports. Its lane is a
        separate thread that keeps storing after that lock is released, so a
        lane alone is a live writer while ``is_running`` is already False. And
        a scan in another process -- ``python -m sf_housing scan``, or a second
        server on the same data directory -- holds only the cross-process file
        lock, which is what that file exists for.

        Anything this cannot establish counts as a writer: the cost is one
        slower start, and the alternative is claiming a board somebody else
        may be writing old scores into.
        """
        if self.is_running:
            return True
        lane = self._lane_thread
        if lane is not None and lane.is_alive():
            return True
        return self._scan_lock_held_elsewhere()

    def _scan_lock_held_elsewhere(self) -> bool:
        """Does another process hold the cross-process scan lock right now?

        Asked by trying for it without waiting and letting go at once, so a
        scan that starts a moment later is not turned away. Such a scan loads
        the deal as it stands then -- already the new one -- so the only writer
        that matters is one holding the lock at this instant.
        """
        lock_path = self.repository.path.parent / "scan.lock"
        if not lock_path.exists():
            return False
        try:
            handle = lock_path.open("a+", encoding="utf-8")
        except OSError:
            return True
        try:
            acquired = try_acquire_file_lock(handle)
        except OSError:
            return True
        else:
            if acquired:
                release_file_lock(handle)
            return not acquired
        finally:
            handle.close()

    def _rescore(self, candidates, active_preferences, connection, mark: str | None = None) -> int:
        # Retracted before the first row moves. Listings commit one at a time
        # -- ``Repository._update_score`` ends with its own commit -- so a pass
        # is not one transaction, and a kill part way through leaves the board
        # half rewritten. Whatever mark it carried described the board as it
        # was, so it must not outlive the first change to it.
        self.repository.set_scoring_mark(connection, None)
        connection.commit()
        batch = list(candidates)
        scored: set[int] = set()
        while batch:
            for listing_id, listing in batch:
                visible_area: str | None = None
                if listing.platform == "Facebook Marketplace":
                    mapped_area = facebook_coordinate_neighborhood(listing.neighborhood)
                    if mapped_area:
                        listing = replace(listing, neighborhood=mapped_area)
                elif listing.platform == "Furnished Finder" and not listing.neighborhood:
                    # Backfill only an explicitly written area from cards already
                    # stored locally. Do not request detail pages or guess map pins.
                    visible_area = visible_sf_area_hint(
                        " ".join(filter(None, [listing.title, listing.summary, listing.listing_type]))
                    )
                    if visible_area:
                        listing = replace(listing, neighborhood=visible_area)
                if not listing.neighborhood:
                    # A home's area is worked out from its address when the
                    # card is first read, so one stored before the street
                    # table could place its block keeps a blank area for ever.
                    # On the owner's board that was 44 of the 98 homes on the
                    # shortlist -- 36 of them Zillow's -- every one carrying a
                    # street address the table can name. The address is
                    # already here, so this asks nobody for anything; it just
                    # stops a home's area depending on the day it was found.
                    from_address = sf_area_from_address(str(listing.metadata.get("address") or ""))
                    if from_address:
                        visible_area = from_address
                        listing = replace(listing, neighborhood=from_address)
                listing = classify_listing(listing)
                result = score_listing(listing, active_preferences)
                matched_neighborhood = result.details.get("neighborhood", {}).get("match_label")
                self.repository.update_score(
                    listing_id,
                    result,
                    neighborhood=(
                        str(matched_neighborhood)
                        if matched_neighborhood
                        else visible_area
                    ),
                    listing=listing,
                    connection=connection,
                    rekey=False,
                )
            scored.update(listing_id for listing_id, _ in batch)
            # Which rows are one home can change with a rescore -- a size read
            # differently, a whole home told from a room -- so the keys are
            # recomputed, once, for every row this pass wrote and their twins.
            # A twin this pass did not score may just have been keyed into a
            # home it did, where an older deal's score would hold the home down:
            # it is scored too, and keyed with the rest, until none is left.
            keyed = self.repository.refresh_identities(
                connection, among=[listing_id for listing_id, _ in batch]
            )
            batch = sorted(self.repository.candidates(sorted(keyed - scored)).items())
        # And recorded only now, with every listing written. Between the
        # retraction above and this line the board carries no claim at all,
        # which is exactly what it deserves while it is being rewritten.
        self.repository.set_scoring_mark(connection, mark)
        connection.commit()
        return len(scored)
