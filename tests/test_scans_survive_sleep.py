"""WO-5 item 6: a check's four minutes are four minutes on the wall, asleep or not.

Scan 66 on the author's own board held on for 8,200 seconds -- two hours and
seventeen minutes -- against a budget of 240. Inside it one Craigslist read
took 5,972 seconds against the 75 a source may have, requests "timed out"
after 524 seconds against an eight-second read timeout, and seven sources were
recorded as failing, each a strike towards pausing it, because the Mac was
asleep: pmset logged Idle Sleep at 18:00:10 and then only brief DarkWakes.

The scan was already timed on a monotonic clock, and that was the bug. On a
Mac, Python's time.monotonic is mach_absolute_time, which stops while the
computer sleeps -- it had counted 46.7 of that Mac's 92.8 hours since boot --
and every wait the scan made, a queue's timeout or a thread's join, counted
the same awake-only seconds. The scan woke with most of its budget unspent and
went on spending it, a few seconds per DarkWake.

Here the computer sleeps in the middle of a scan by moving the scan's clock
and not the awake one, which is what closing the lid does to the two
(``Clocks``).
"""

from __future__ import annotations

import math
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta

import httpx
import pytest

from sf_housing import scanner as scanner_module
from sf_housing.database import Repository
from sf_housing.freshness import source_is_in_backoff
from sf_housing.models import ListingCandidate
from sf_housing.preferences import Preferences
from sf_housing.scanner import (
    LANE_THREAD_NAME,
    SLEPT_MESSAGE,
    SLEPT_SCAN_MESSAGE,
    SOURCE_HARD_CEILING_SECONDS,
    Scanner,
)
from sf_housing.scheduling import manual_scans_today, scheduled_scan_due, slept_through_lately
from sf_housing.sources import PartialReadError, SourceError

TWO_HOURS = 2 * 60 * 60.0
# What the app scans with (sf_housing/settings.py): four minutes for a whole
# check and eight seconds for a read.
BUDGET = 240.0
READ_TIMEOUT = 8.0
# Longer than any wait these allow, so a scan that sits out a stalled read
# instead of walking away from it fails rather than passing late.
STALL = 10.0
# Long enough for the scan to have settled into waiting on something, and well
# inside the one-second slices it waits in.
SETTLE = 0.2
# How long each page of a walking source takes to come back while the
# computer is awake, and the moment of real time it takes as well, so the scan
# is waiting on the read as it would be on a real site.
PAGE_SECONDS = 10.0
REAL_PAGE_SECONDS = 0.005


class Clocks:
    """The two clocks a scan reads, each with a hand the test can move.

    ``awake`` is this process's own time -- ``time.monotonic``, which on a Mac
    stops while it sleeps -- plus whatever a stand-in site has been made to
    take. ``scan`` is that and the time the computer has slept: the clock every
    budget and ceiling of a scan is spent on.
    """

    def __init__(self) -> None:
        self.taken = 0.0
        self.slept = 0.0

    def awake(self) -> float:
        return time.monotonic() + self.taken

    def scan(self) -> float:
        return self.awake() + self.slept

    def take(self, seconds: float) -> None:
        """A site taking this long to answer, with the computer awake throughout."""
        self.taken += seconds

    def sleep(self, seconds: float) -> None:
        """Close the lid for this long."""
        self.slept += seconds


def room(platform: str, source_id: str) -> ListingCandidate:
    return ListingCandidate(
        platform=platform,
        source_id=source_id,
        title="Sunny private room in a house near Golden Gate Park",
        original_url=f"https://example.test/{platform}/{source_id}",
        price=1500,
        neighborhood="NOPA",
        summary="Flexible lease, communal garden, 4 roommates.",
        metadata={"property_type": "house", "rooms_in_property": "4"},
    )


class Stub:
    """A source that does whatever the test hands it, then returns its homes."""

    mode = "automatic"
    manual_reason = None
    detail_budget = 0

    def __init__(
        self, platform: str, *, search: Callable[[httpx.Client], object] | None = None, listings=()
    ) -> None:
        self.platform = platform
        self.search_url = f"https://example.test/{platform}"
        self._search = search
        self._listings = list(listings)
        self.searches = 0

    def search(self, client, preferences):
        self.searches += 1
        if self._search is not None:
            self._search(client)
        return list(self._listings)

    def enrich(self, client, listing):
        return listing


class LaneStub(Stub):
    runs_in_own_lane = True


class Walker(Stub):
    """A paginated source shaped like the repository's own: page after page,
    keeping what it has read when a later page is refused (the PARTIAL_WALK
    note in sf_housing/sources.py) and failing only when the first one is."""

    def __init__(self, platform: str, *, pages: int = 50) -> None:
        super().__init__(platform)
        self.pages = pages
        self.refused: list[SourceError] = []
        self.finished = threading.Event()

    def search(self, client, preferences):
        self.searches += 1
        homes: list[ListingCandidate] = []
        try:
            for page in range(1, self.pages + 1):
                try:
                    client.get(self.search_url, params={"page": page})
                except SourceError as refused:
                    if not homes:
                        raise
                    self.refused.append(refused)
                    return self.stop_short(homes, refused)
                homes.append(room(self.platform, str(page)))
            return homes
        finally:
            self.finished.set()

    def stop_short(self, homes: list[ListingCandidate], refused: SourceError) -> list[ListingCandidate]:
        return homes


class ZillowWalker(Walker):
    """Zillow's shape. It blocks by address, so a refusal part way through has
    to reach the backoff, and the pages it did read travel with the failure."""

    def stop_short(self, homes: list[ListingCandidate], refused: SourceError) -> list[ListingCandidate]:
        raise PartialReadError(refused, homes) from refused


class LaneWalker(Walker):
    runs_in_own_lane = True


def scanner_for(
    repository: Repository, preferences: Preferences, sources: list, clocks: Clocks, **options
) -> Scanner:
    """A scanner timed by ``clocks`` and otherwise set up as the app sets one up."""
    settings = {"timeout_seconds": READ_TIMEOUT, "max_scan_seconds": BUDGET, **options}
    return Scanner(
        repository,
        lambda: preferences,
        sources,
        detail_delay_seconds=0,
        clock=clocks.scan,
        awake_clock=clocks.awake,
        **settings,
    )


def serve(monkeypatch: pytest.MonkeyPatch, site: Callable[[httpx.Request], httpx.Response]) -> None:
    """Answer every request the scan's clients send with ``site``. The clients
    are otherwise exactly as the scan builds them, the hook each of them runs
    before every request included."""
    real = httpx.Client

    def client(*args, **kwargs):
        return real(*args, transport=httpx.MockTransport(site), **kwargs)

    monkeypatch.setattr(scanner_module.httpx, "Client", client)


def source_rows(repository: Repository, run_id: int) -> dict[str, tuple[str, str | None]]:
    with repository.connection() as connection:
        return {
            row["platform"]: (row["status"], row["message"])
            for row in connection.execute(
                "SELECT platform, status, message FROM source_runs WHERE scan_run_id = ?", (run_id,)
            )
        }


def homes_from(repository: Repository, platform: str) -> int:
    with repository.connection() as connection:
        return connection.execute("SELECT COUNT(*) FROM listings WHERE platform = ?", (platform,)).fetchone()[0]


def lane_alive() -> bool:
    return any(thread.name == LANE_THREAD_NAME for thread in threading.enumerate())


@pytest.fixture
def stalled() -> Iterator[threading.Event]:
    """What a read that never comes back waits on, let go when the test ends."""
    release = threading.Event()
    yield release
    release.set()


# --------------------------------------------------------------------------
# the clock a scan is timed by
# --------------------------------------------------------------------------


def seconds_since_boot() -> float:
    """What the Mac itself says: now, less the moment it booted."""
    answer = subprocess.run(
        ["/usr/sbin/sysctl", "-n", "kern.boottime"], capture_output=True, text=True, check=True
    ).stdout
    # { sec = 1789449021, usec = 555742 } Mon Sep 14 22:10:21 2026
    seconds, micros = (int(re.search(rf"\b{name}\s*=\s*(\d+)", answer).group(1)) for name in ("sec", "usec"))
    return time.time() - (seconds + micros / 1_000_000)


@pytest.mark.skipif(sys.platform != "darwin", reason="reads the Mac's own boot time")
def test_the_scan_clock_counts_the_time_this_computer_has_slept(
    repository: Repository, preferences: Preferences
) -> None:
    """Every budget and ceiling a scan has is spent on this clock, and the one
    it had by default was Python's own, which stops while a Mac sleeps."""
    scanner = Scanner(repository, lambda: preferences, [])

    assert scanner._clock() == pytest.approx(seconds_since_boot(), abs=60)
    if seconds_since_boot() - time.monotonic() > 60:
        # This Mac has slept since it booted, so Python's clock is behind by
        # that much -- and is not the one the scan is timed by.
        assert abs(scanner._clock() - time.monotonic()) > 60


# --------------------------------------------------------------------------
# sleeping through a scan
# --------------------------------------------------------------------------


def test_an_answer_handed_back_as_the_time_ran_out_is_kept(
    repository: Repository, preferences: Preferences, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read that finished in the very moment its time was up -- the last page
    back as the clock passed the end -- was thrown away: the wait looked at the
    clock before it looked at the answer already waiting for it, and recorded
    a source that had read six pages as not reached, with nothing stored."""
    clocks = Clocks()
    scanner = scanner_for(repository, preferences, [], clocks)

    class DoneBeforeAnybodyWaits:
        """A worker that has finished before the scan so much as looks."""

        def __init__(self, target, name=None, daemon=None) -> None:
            self.target = target

        def start(self) -> None:
            self.target()

    def the_whole_allowance_and_a_little_more() -> list[str]:
        clocks.take(30.0)
        return ["the homes it read"]

    monkeypatch.setattr(scanner_module.threading, "Thread", DoneBeforeAnybodyWaits)
    limit = scanner_module._FetchLimit(clocks.scan() + 20.0)

    answer = scanner._within_ceiling(
        "search-Slow", limit, the_whole_allowance_and_a_little_more, timed_out="stopped answering", out_of_time=None
    )

    assert answer == ["the homes it read"]


def test_a_scan_the_computer_sleeps_through_stops_on_waking(
    repository: Repository, preferences: Preferences, stalled: threading.Event
) -> None:
    """Scan 66. The lid closed while a source was being read; on waking the
    scan went on waiting for it, on a clock that had not counted the night,
    and seven sources were recorded as failing for a sleep none of them caused."""
    clocks = Clocks()

    def lid_closes(client) -> None:
        time.sleep(SETTLE)  # the scan is waiting on the read by now
        clocks.sleep(TWO_HOURS)
        stalled.wait(STALL)  # and the read never comes back

    after = [Stub("Next", listings=[room("Next", "1")]), Stub("Then", listings=[room("Then", "1")])]
    scanner = scanner_for(repository, preferences, [Stub("Asleep", search=lid_closes), *after], clocks)

    started = time.monotonic()
    outcome = scanner.run_scan("scheduled")
    waited = time.monotonic() - started

    assert waited < 5, f"the scan held on for {waited:.1f}s after the computer woke"
    assert source_rows(repository, outcome.run_id) == {
        platform: ("skipped", SLEPT_MESSAGE) for platform in ("Asleep", "Next", "Then")
    }
    assert [source.searches for source in after] == [0, 0], "a source was read after the scan's time had gone"
    assert (outcome.status, outcome.sources_failed) == ("interrupted", 0)


def test_a_detail_page_the_computer_sleeps_through_is_not_waited_on(
    repository: Repository, preferences: Preferences, stalled: threading.Event
) -> None:
    """The same wait one level down. Running out of time there is no page's
    fault, so it is no detail-page warning either, and the homes the search
    returned are all kept."""
    clocks = Clocks()
    asked: list[str] = []

    class Detailed(Stub):
        detail_budget = 3

        def enrich(self, client, listing):
            asked.append(listing.source_id)
            time.sleep(SETTLE)  # the scan is waiting on the page by now
            clocks.sleep(TWO_HOURS)
            stalled.wait(STALL)
            return listing

    source = Detailed("Detailed", listings=[room("Detailed", str(number)) for number in (1, 2, 3)])
    scanner = scanner_for(repository, preferences, [source], clocks)

    started = time.monotonic()
    outcome = scanner.run_scan("scheduled")
    waited = time.monotonic() - started

    assert waited < 5, f"the scan waited {waited:.1f}s on a detail page after the computer woke"
    assert asked == ["1"], "a detail page was asked for after the computer slept"
    status, message = source_rows(repository, outcome.run_id)["Detailed"]
    assert status == "success", message
    assert "detail-page warning" not in (message or "")
    assert (outcome.sources_failed, outcome.listings_added) == (0, 3)


def test_a_lane_the_computer_slept_past_is_left_without_counting_it_failed(
    repository: Repository, preferences: Preferences, stalled: threading.Event
) -> None:
    """The scan waits on its lane with a join, which counted awake time too, and
    a lane it gave up on was recorded as failing. This lane is stuck between
    its reads rather than in one, where no source's ceiling reaches, so it is
    the scan's wait on the lane that has to notice the night."""
    clocks = Clocks()
    joining = threading.Event()

    class Stuck(LaneStub):
        @property
        def detail_budget(self):
            # Read after the search, outside every ceiling the scan has.
            joining.wait(STALL)
            time.sleep(SETTLE)  # the scan is waiting on the lane by now
            clocks.sleep(TWO_HOURS)
            stalled.wait(STALL)
            return 0

    behind = LaneStub("LaneNext", listings=[room("LaneNext", "1")])
    sources = [Stuck("Lane", listings=[room("Lane", "1")]), behind, Stub("Main", listings=[room("Main", "1")])]
    scanner = scanner_for(repository, preferences, sources, clocks)
    join_lane = scanner._join_lane

    def waiting_on_the_lane(*args, **kwargs):
        joining.set()
        return join_lane(*args, **kwargs)

    scanner._join_lane = waiting_on_the_lane
    try:
        started = time.monotonic()
        outcome = scanner.run_scan("scheduled")
        waited = time.monotonic() - started
        rows = source_rows(repository, outcome.run_id)
        panel = scanner.progress
    finally:
        stalled.set()
        give_up = time.monotonic() + 15
        while lane_alive():
            assert time.monotonic() < give_up, "the lane never ended"
            time.sleep(0.01)

    assert waited < 5, f"the scan waited {waited:.1f}s on its lane after the computer woke"
    assert rows["Lane"] == ("skipped", SLEPT_MESSAGE)
    assert rows["Main"][0] == "success"
    assert (outcome.status, outcome.sources_failed, panel["sources_failed"]) == ("interrupted", 0, 0)
    # Come unstuck, the lane went no further: the check it belonged to was over.
    assert behind.searches == 0
    # And the source it never reached says so, rather than showing what the
    # check before this one said about it.
    assert rows["LaneNext"] == ("skipped", SLEPT_MESSAGE)
    assert source_rows(repository, outcome.run_id)["LaneNext"] == rows["LaneNext"], (
        "the lane started another source after its check was over"
    )


def test_every_wait_on_a_thread_counts_the_time_the_computer_slept(
    repository: Repository, preferences: Preferences, stalled: threading.Event
) -> None:
    """A thread's join counts awake time only, like a queue's timeout: thirty
    seconds begun as the lid closed could last the night."""
    clocks = Clocks()
    scanner = scanner_for(repository, preferences, [], clocks)

    def reading() -> None:
        time.sleep(SETTLE)  # the scan is waiting on it by now
        clocks.sleep(60 * 60.0)
        stalled.wait(STALL)

    thread = threading.Thread(target=reading, daemon=True)
    thread.start()
    started = time.monotonic()
    scanner._join_within(thread, 30)
    waited = time.monotonic() - started

    # Noticed within a slice of the wait, a second, of waking; slack for a busy machine.
    assert waited < 2.0, f"a thirty-second wait that slept an hour went on for {waited:.1f}s after waking"
    assert thread.is_alive(), "the wait ended because the thread did, which proves nothing"


def test_a_wait_on_a_thread_still_lasts_its_time_while_the_computer_is_awake(
    repository: Repository, preferences: Preferences, stalled: threading.Event
) -> None:
    clocks = Clocks()
    scanner = scanner_for(repository, preferences, [], clocks)
    thread = threading.Thread(target=stalled.wait, args=(STALL,), daemon=True)
    thread.start()

    started = time.monotonic()
    scanner._join_within(thread, 0.3)
    waited = time.monotonic() - started

    assert 0.25 <= waited < 1.5, f"a 0.3s wait took {waited:.2f}s"
    assert thread.is_alive()


# --------------------------------------------------------------------------
# asking a site for nothing once the time is up
# --------------------------------------------------------------------------


@pytest.mark.parametrize("walker_type", [Walker, LaneWalker], ids=["main-line", "own-lane"])
def test_no_request_is_sent_once_a_sources_time_is_up(
    repository: Repository, preferences: Preferences, monkeypatch: pytest.MonkeyPatch, walker_type
) -> None:
    """The ceiling only ever stopped the scan waiting. The walk went on asking
    for pages for as long as the walk itself allowed -- an abandoned Zillow
    read on through eight rent bands of thirty pages. Now each request is
    refused once the time of the call making it is up."""
    clocks = Clocks()
    sent: list[float] = []

    def site(request: httpx.Request) -> httpx.Response:
        sent.append(clocks.scan())
        time.sleep(REAL_PAGE_SECONDS)
        clocks.take(PAGE_SECONDS)
        return httpx.Response(200, text="<html></html>")

    serve(monkeypatch, site)
    walker = walker_type("Walker", pages=50)
    # A lane is only started beside something on the main line.
    sources = [walker, Stub("Main")] if walker_type is LaneWalker else [walker]
    scanner = scanner_for(repository, preferences, sources, clocks)

    scanner.run_scan("scheduled")

    fits = math.ceil(SOURCE_HARD_CEILING_SECONDS / PAGE_SECONDS)
    assert len(sent) == fits, (
        f"{len(sent)} pages were asked for; {fits} fit in the {SOURCE_HARD_CEILING_SECONDS:.0f}s a source may take"
    )
    assert len(walker.refused) == 1, "the walk ended without its next page being refused"


def test_a_source_the_scan_walked_away_from_asks_its_site_for_nothing_more(
    repository: Repository, preferences: Preferences, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Walked away from, a stalled read did not stop: when its page finally
    came back it went on to the next, and the next, through the scan's own
    client, while the scan was reading the sources after it."""
    clocks = Clocks()
    sent: list[str] = []
    came_back = threading.Event()

    def site(request: httpx.Request) -> httpx.Response:
        sent.append(str(request.url))
        if len(sent) == 1:
            # The first page trickles in for longer than a source may take.
            clocks.take(SOURCE_HARD_CEILING_SECONDS + PAGE_SECONDS)
            came_back.wait(STALL)
        return httpx.Response(200, text="<html></html>")

    serve(monkeypatch, site)
    walker = Walker("Abandoned", pages=50)
    meanwhile: dict[str, bool] = {}

    def next_source(client) -> None:
        came_back.set()  # the stalled page answers while the scan reads this source
        meanwhile["walker_stopped"] = walker.finished.wait(STALL)

    scanner = scanner_for(repository, preferences, [walker, Stub("Next", search=next_source)], clocks)

    outcome = scanner.run_scan("scheduled")

    assert len(sent) == 1, f"the read walked away from asked for {len(sent) - 1} more page(s)"
    assert meanwhile["walker_stopped"] and len(walker.refused) == 1, "the walk did not stop at its next page"
    rows = source_rows(repository, outcome.run_id)
    # Awake, and past its own ceiling: the stall it always was.
    assert rows["Abandoned"][0] == "error" and "stopped answering" in rows["Abandoned"][1]
    assert rows["Next"][0] == "success"


def requests_at_the_end_of_a_scan(
    repository: Repository,
    preferences: Preferences,
    monkeypatch: pytest.MonkeyPatch,
    stalled: threading.Event,
    *,
    lid_closes: bool,
) -> list[str]:
    """Which sites a scan's closing count of HotPads asked, the lid closing
    part way through the scan or not."""
    clocks = Clocks()
    asked: list[str] = []

    def site(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.host)
        return httpx.Response(200, text="")

    serve(monkeypatch, site)

    def read(client) -> None:
        if lid_closes:
            clocks.sleep(TWO_HOURS)
            stalled.wait(STALL)

    scanner_for(repository, preferences, [Stub("Only", search=read)], clocks).run_scan("scheduled")
    return asked


@pytest.mark.coverage_pass
def test_the_count_a_scan_ends_with_is_not_asked_for_once_the_computer_slept_past_its_time(
    repository: Repository, preferences: Preferences, monkeypatch: pytest.MonkeyPatch, stalled: threading.Event
) -> None:
    """The count is taken on the scan's own thread and client, after every
    source. Two hours past the scan's time it is a request to a site that
    blocks by address, on behalf of a check that is long over."""
    assert requests_at_the_end_of_a_scan(repository, preferences, monkeypatch, stalled, lid_closes=True) == []


@pytest.mark.coverage_pass
def test_the_count_a_scan_ends_with_is_still_taken_while_the_computer_stays_awake(
    repository: Repository, preferences: Preferences, monkeypatch: pytest.MonkeyPatch, stalled: threading.Event
) -> None:
    assert requests_at_the_end_of_a_scan(repository, preferences, monkeypatch, stalled, lid_closes=False) == [
        "hotpads.com"
    ]


# --------------------------------------------------------------------------
# running out of time is not failing
# --------------------------------------------------------------------------


@pytest.mark.parametrize("walker_type", [Walker, ZillowWalker], ids=["keeps-what-it-read", "partial-read-error"])
@pytest.mark.parametrize("cut", ["time-limit", "slept"])
def test_a_read_the_scan_ran_out_of_time_for_keeps_what_it_read_and_rechecks_nothing(
    repository: Repository, preferences: Preferences, monkeypatch: pytest.MonkeyPatch, walker_type, cut: str
) -> None:
    """Cut short by the check's time -- its budget, or the computer asleep --
    a walk hands back the pages it read. They are stored as any read's are,
    the run says why it stopped, and nothing failed. The recheck pass is
    skipped: a home a half-finished walk never reached is no sign it has gone."""
    clocks = Clocks()
    sent: list[str] = []

    def site(request: httpx.Request) -> httpx.Response:
        # Nothing moves until the scan is waiting on the walk, as it would be
        # on any real site, so what it hands back is what the scan receives.
        time.sleep(REAL_PAGE_SECONDS if sent else SETTLE)
        sent.append(str(request.url))
        if cut == "slept" and len(sent) == 3:
            clocks.sleep(TWO_HOURS)
        else:
            clocks.take(PAGE_SECONDS)
        return httpx.Response(200, text="<html></html>")

    serve(monkeypatch, site)
    # A minute to go by the time the walk starts, against the 75 seconds the
    # source could otherwise take.
    budget = 60.0 if cut == "time-limit" else BUDGET
    walker = walker_type("Walker", pages=50)
    scanner = scanner_for(
        repository, preferences, [Stub("Quick", listings=[room("Quick", "1")]), walker], clocks,
        max_scan_seconds=budget,
    )
    rechecked: list[str] = []
    recheck = scanner._recheck_absent

    def spy(client, source, preferences, **kwargs):
        rechecked.append(source.platform)
        return recheck(client, source, preferences, **kwargs)

    scanner._recheck_absent = spy

    outcome = scanner.run_scan("scheduled")

    status, message = source_rows(repository, outcome.run_id)["Walker"]
    assert status == "success", message
    reason = SLEPT_MESSAGE if cut == "slept" else "60-second time limit"
    assert reason in (message or ""), f"the run said {message!r}, not why it stopped"
    assert len(sent) == (3 if cut == "slept" else math.ceil(budget / PAGE_SECONDS))
    assert homes_from(repository, "Walker") == len(sent), "the pages read before the time ran out were not all kept"
    assert rechecked == ["Quick"], "the walk cut short was rechecked"
    assert outcome.sources_failed == 0


def test_a_source_the_scan_ran_out_of_time_for_is_not_counted_as_failing(
    repository: Repository, preferences: Preferences, stalled: threading.Event
) -> None:
    """Still reading when the check's time ran out, a source was recorded as
    failing, and two of those in a row paused it for six hours (the backoff in
    sf_housing/freshness.py) for nothing the site did."""
    clocks = Clocks()

    def slow(client) -> None:
        clocks.take(5)  # still reading when the check's four seconds are up
        stalled.wait(STALL)

    source = Stub("Slow", search=slow)
    scanner = scanner_for(repository, preferences, [source], clocks, max_scan_seconds=4, timeout_seconds=1)

    for _ in range(2):
        outcome = scanner.run_scan("scheduled")
        status, message = source_rows(repository, outcome.run_id)["Slow"]
        assert status == "skipped", message
        assert "4-second time limit" in message
        assert (outcome.status, outcome.sources_failed) == ("completed", 0)

    assert source_is_in_backoff(repository, source) is None


def test_a_source_that_stops_answering_while_the_computer_is_awake_is_still_a_failure(
    repository: Repository, preferences: Preferences, stalled: threading.Event, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only time running out -- the check's, or the night's -- is excused. A
    site that stops answering inside its own ceiling with the computer awake
    is the stall it always was. On the clocks the app really uses."""
    monkeypatch.setattr(scanner_module, "SOURCE_HARD_CEILING_SECONDS", 1.0)
    source = Stub("Stalled", search=lambda client: stalled.wait(STALL))
    scanner = Scanner(
        repository, lambda: preferences, [source], detail_delay_seconds=0,
        timeout_seconds=READ_TIMEOUT, max_scan_seconds=BUDGET,
    )

    outcome = scanner.run_scan("scheduled")

    status, message = source_rows(repository, outcome.run_id)["Stalled"]
    assert status == "error"
    assert "stopped answering" in message
    assert outcome.sources_failed == 1


def test_a_check_the_computer_slept_through_is_owed_again_and_not_spent(
    repository: Repository, preferences: Preferences, stalled: threading.Event
) -> None:
    """Review of WO-5: a check the lid closed on stopped on waking and was
    filed completed, so the heartbeat took its slot as served and nothing
    read the sources it skipped until the next slot, up to sixteen hours on;
    and a check asked for by hand was spent. Filed as interrupted, the slot is
    owed again -- not in the next moment of darkness, but within the hour."""
    clocks = Clocks()

    def lid_closes(client) -> None:
        time.sleep(SETTLE)
        clocks.sleep(TWO_HOURS)
        stalled.wait(STALL)

    scanner = scanner_for(
        repository, preferences, [Stub("Asleep", search=lid_closes), Stub("Next", listings=[room("Next", "1")])], clocks
    )

    outcome = scanner.run_scan("manual")

    assert (outcome.status, outcome.sources_failed) == ("interrupted", 0)
    recent = repository.recent_scans(20)
    assert recent[0]["message"] == SLEPT_SCAN_MESSAGE
    assert scheduled_scan_due(recent), "the slot it ran for is still owed a check"
    assert manual_scans_today(recent) == 0, "a check the sleep cut short is not the day's check by hand"
    stopped = datetime.fromisoformat(recent[0]["finished_at"])
    assert slept_through_lately(recent, now=stopped + timedelta(minutes=59)), "not again in the next dark minute"
    assert not slept_through_lately(recent, now=stopped + timedelta(minutes=61)), "but within the hour"


def test_a_sleep_after_the_last_source_leaves_the_check_complete(
    repository: Repository, preferences: Preferences
) -> None:
    """Only what a sleep left unread makes a check interrupted: the lid closed
    while the scan tidied up after its last source cost it nothing, and owes
    nothing."""
    clocks = Clocks()
    scanner = scanner_for(repository, preferences, [Stub("Quick", listings=[room("Quick", "1")])], clocks)
    tidy = scanner._retire_old_listings

    def the_lid_closes_while_it_tidies() -> None:
        clocks.sleep(TWO_HOURS)
        tidy()

    scanner._retire_old_listings = the_lid_closes_while_it_tidies

    outcome = scanner.run_scan("scheduled")

    assert (outcome.status, outcome.sources_failed) == ("completed", 0)
    assert not scheduled_scan_due(repository.recent_scans(20))


@pytest.mark.parametrize("shape", ["plain", "partial"])
def test_a_request_the_sleep_cut_off_is_not_counted_against_the_site(
    repository: Repository, preferences: Preferences, shape: str
) -> None:
    """Review of WO-5: the lid closes with a request out; on waking the network
    is gone and the read fails at once, before the scan's wait has looked at
    the clock. That was filed as the site failing -- a strike towards pausing
    it, and two such nights paused it. It is the sleep's doing, as a read the
    scan walked away from is, and what was read before it is kept."""
    clocks = Clocks()

    def the_connection_dies_in_the_night(client) -> None:
        time.sleep(SETTLE)
        clocks.sleep(TWO_HOURS)
        failure = httpx.ConnectError("[Errno 51] Network is unreachable")
        if shape == "partial":
            raise PartialReadError(failure, [room("Asleep", "1")]) from failure
        raise failure

    source = Stub("Asleep", search=the_connection_dies_in_the_night)
    scanner = scanner_for(repository, preferences, [source], clocks)

    for _ in range(2):
        outcome = scanner.run_scan("scheduled")
        status, message = source_rows(repository, outcome.run_id)["Asleep"]
        assert outcome.sources_failed == 0, (status, message)
        assert SLEPT_MESSAGE in (message or ""), (status, message)

    assert source_is_in_backoff(repository, source) is None
    if shape == "partial":
        assert homes_from(repository, "Asleep") == 1, "the page read before the night is kept"


def test_a_request_refused_for_time_reads_without_its_class_name() -> None:
    """Pre-launch audit: the refusal WO-5 added was named ScanTimeUp, and a
    Zillow walk stopped by it stores its cause as "ScanTimeUp: Stopped before
    asking ...". The panel strips the class names errors are given, which
    end in Error, so that one reached the System status panel and Support."""
    from sf_housing.freshness import _first_sentence
    from sf_housing.sources import ScanTimeUpError

    refused = ScanTimeUpError("Stopped before asking www.zillow.com again: the time allowed for this source had run out.")

    said = _first_sentence(f"{type(refused).__name__}: {refused}", "Zillow")

    assert said.startswith("Stopped before asking www.zillow.com again"), said
