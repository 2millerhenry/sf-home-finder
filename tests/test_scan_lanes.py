"""Sources that answer only for themselves, read alongside the rest of a scan.

A scan read every source strictly one after another, so the wait was the sum
of all of them: 223 seconds on a real board, of which the sources themselves
accounted for 221. Craigslist alone was 25 of the 29 seconds of this install's
very first search, and it is served from its own addresses.

It is not the only one. Zillow Group answers for Zillow, Trulia and HotPads,
and CoStar for Rent.com, ApartmentGuide and Apartments.com -- those two lines
of traffic stay strictly in turn -- while the sites that answer for nobody but
themselves are read on a lane beside them. Every site still sees one request
at a time, at the pace it always saw.

These pin what makes that safe as well as quicker. Only a source that declares
it gets a lane, never one that merely shares Craigslist's name. The main line
is charged with the lane's time in everything it decides by the clock --
whether to skip a source, how long to search, how many detail pages, how long
to recheck -- so no source is given more time than a sequential scan would
have given it: more time is more requests, and more requests is the thing this
must never do. Progress never counts backwards or stands still. The lane is
finished before the scan adds up what it found, and reads on a client of its
own set up exactly like the scan's. A lane that outstays its bounded join is
never started a second time. And SF_HOUSING_SEQUENTIAL_SCAN=1 puts everything
back exactly as it was.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import httpx
import pytest

from sf_housing import scanner as scanner_module
from sf_housing.database import Repository
from sf_housing.models import ListingCandidate
from sf_housing.preferences import Preferences
from sf_housing.scanner import Scanner


LANE_THREAD = getattr(scanner_module, "LANE_THREAD_NAME", "scan-lane")
SEQUENTIAL = "SF_HOUSING_SEQUENTIAL_SCAN"


@pytest.fixture(autouse=True)
def no_coverage_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every full scan ends by asking HotPads how many homes it is holding.
    Nothing in this file is about that count, and left in, every test here
    sent a real request to a site that blocks by address -- and spent network
    time inside the tests that time the lane. The one test that watches when
    coverage runs replaces it on its own scanner."""
    monkeypatch.setattr(Scanner, "_measure_missing_coverage", lambda self, client, preferences: None)


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

    def __init__(self, platform: str, *, search=None, listings=()) -> None:
        self.platform = platform
        self.search_url = f"https://example.test/{platform}"
        self._search = search
        self._listings = list(listings)

    def search(self, client, preferences):
        if self._search is not None:
            self._search(client)
        return list(self._listings)

    def enrich(self, client, listing):
        return listing


class LaneStub(Stub):
    runs_in_own_lane = True


def lane_alive() -> bool:
    return any(thread.name == LANE_THREAD for thread in threading.enumerate())


def source_rows(repository: Repository, run_id: int) -> dict[str, dict]:
    with repository.connection() as connection:
        return {
            row["platform"]: dict(row)
            for row in connection.execute(
                "SELECT platform, status, message FROM source_runs WHERE scan_run_id = ?",
                (run_id,),
            )
        }


def fresh(tmp_path: Path, name: str) -> Repository:
    repository = Repository(tmp_path / name / "housing.sqlite3")
    repository.initialize()
    return repository


# --- The lane exists, and only where it has been asked for ------------------


def test_the_lane_source_is_read_at_the_same_time_as_the_rest(
    repository: Repository, preferences: Preferences
) -> None:
    """The whole point. Each of these can only finish once the other has
    started, which is possible only if they overlap. Read one after the other,
    the first waits out the barrier and fails, and the second finds it broken.
    """
    barrier = threading.Barrier(2, timeout=5)

    def meet(name):
        def run(client):
            barrier.wait()
        return run

    lane = LaneStub("Lane", search=meet("Lane"), listings=[room("Lane", "1")])
    main = Stub("Main", search=meet("Main"), listings=[room("Main", "1")])
    scanner = Scanner(repository, lambda: preferences, [lane, main], detail_delay_seconds=0)

    outcome = scanner.run_scan("manual")

    assert outcome.sources_failed == 0, "the two sources never overlapped"
    assert outcome.status == "completed"
    # Both lanes' work is in the scan's totals, not just the main lane's.
    assert outcome.listings_seen == 2
    assert outcome.listings_added == 2
    with repository.connection() as connection:
        stored = connection.execute(
            "SELECT listings_seen, listings_added FROM scan_runs WHERE id = ?",
            (outcome.run_id,),
        ).fetchone()
    assert (stored["listings_seen"], stored["listings_added"]) == (2, 2)


def test_the_scan_takes_about_as_long_as_its_slowest_lane(
    repository: Repository, preferences: Preferences
) -> None:
    """What the overlap buys. Two sources of a second each took two seconds in
    turn; read together they take one. The bound is generous on purpose: this
    is here to catch the lanes quietly running in turn again, not to time a
    runner."""
    lane = LaneStub("Lane", search=lambda client: time.sleep(1.0))
    main = Stub("Main", search=lambda client: time.sleep(1.0))
    scanner = Scanner(repository, lambda: preferences, [lane, main], detail_delay_seconds=0)

    started = time.monotonic()
    outcome = scanner.run_scan("manual")
    elapsed = time.monotonic() - started

    assert outcome.status == "completed"
    assert elapsed < 1.6, f"took {elapsed:.2f}s -- the lanes ran one after the other"


@pytest.mark.parametrize(
    "impostor",
    [
        pytest.param(type("Named", (Stub,), {}), id="named-craigslist"),
        pytest.param(type("Loose", (Stub,), {"runs_in_own_lane": "yes"}), id="truthy-string"),
        pytest.param(type("One", (Stub,), {"runs_in_own_lane": 1}), id="truthy-one"),
    ],
)
def test_nothing_but_an_explicit_true_gives_a_source_a_lane(
    repository: Repository, preferences: Preferences, impostor
) -> None:
    """Thirteen test files, and any helper yet to be written, use a stand-in
    called Craigslist. Choosing the lane by name would thread every one of them
    without anybody asking, and a loosely truthy attribute is not a decision
    either. Read strictly in turn, neither source can meet the other."""
    barrier = threading.Barrier(2, timeout=0.5)
    named = impostor("Craigslist", search=lambda client: barrier.wait())
    other = Stub("Other", search=lambda client: barrier.wait())
    scanner = Scanner(repository, lambda: preferences, [named, other], detail_delay_seconds=0)

    outcome = scanner.run_scan("manual")

    assert outcome.sources_failed == 2, "a source that did not opt in was given a lane"


def test_the_lane_holds_exactly_the_sources_somebody_argued_for() -> None:
    """Which sources are read beside the rest is a ban-safety decision rather
    than an optimisation: each is here because its owner sees nobody else's
    requests, which tests/test_traffic_groups.py checks for real. Pinning the
    roster by name means a source joining the lane is a decision somebody made,
    not something that happened."""
    from sf_housing.scanner import reads_on_its_own_lane
    from sf_housing.sources import default_sources

    expected = {
        "Craigslist",               # its own servers, its own address space
        "Listings Project",
        "Abacus (small buildings)",  # appfolio.com, read in turn with AppFolio
        "SpareRoom",
        "SF Housing Portal",         # the city's own site
        "Apartment List",
        "Zumper",
        "Uloop",
        "UDR",
        "AppFolio",
        "AvalonBay",
        "RentSFNow",
        "Movoto",
    }
    laned = {source.platform for source in default_sources() if reads_on_its_own_lane(source)}

    assert laned == expected


def test_the_sequential_switch_puts_every_source_back_in_line(
    repository: Repository, preferences: Preferences, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The way back, if a lane ever turns out to cost something nobody saw. It
    has to mean exactly today's behaviour: no overlap, and the same order."""
    monkeypatch.setenv(SEQUENTIAL, "1")
    barrier = threading.Barrier(2, timeout=0.5)
    order: list[str] = []
    lock = threading.Lock()

    def meet(name):
        def run(client):
            with lock:
                order.append(name)
            barrier.wait()
        return run

    lane = LaneStub("Lane", search=meet("Lane"))
    main = Stub("Main", search=meet("Main"))
    scanner = Scanner(repository, lambda: preferences, [lane, main], detail_delay_seconds=0)

    outcome = scanner.run_scan("manual")

    assert outcome.sources_failed == 2, "the switch did not stop the lane"
    assert order == [source.platform for source in scanner._eligible_sources("manual")]


def test_a_scan_aimed_at_chosen_sources_stays_in_one_line(
    repository: Repository, preferences: Preferences
) -> None:
    """A scoped scan is somebody setting a connector up from the setup page.
    It is one deliberate read, and nothing about it needs to be quicker."""
    barrier = threading.Barrier(2, timeout=0.5)
    lane = LaneStub("Lane", search=lambda client: barrier.wait())
    main = Stub("Main", search=lambda client: barrier.wait())
    scanner = Scanner(repository, lambda: preferences, [], detail_delay_seconds=0)

    outcome = scanner.run_scan("manual", sources=[lane, main])

    assert outcome.sources_failed == 2, "a scoped scan was split into lanes"


def test_a_scan_with_nothing_else_to_run_does_not_start_a_lane(
    repository: Repository, preferences: Preferences
) -> None:
    """A lane beside nothing saves nothing, and is one more thread to reason
    about when something goes wrong."""
    threads: list[str] = []
    lane = LaneStub(
        "Lane",
        search=lambda client: threads.extend(t.name for t in threading.enumerate()),
        listings=[room("Lane", "1")],
    )
    scanner = Scanner(repository, lambda: preferences, [lane], detail_delay_seconds=0)

    outcome = scanner.run_scan("manual")

    assert outcome.status == "completed"
    assert outcome.listings_added == 1
    assert LANE_THREAD not in threads


# --- The rule that keeps it from asking any site for more ------------------


def measure_recheck_allowance(
    repository: Repository, preferences: Preferences, *, lane_seconds: float
) -> tuple[dict[str, tuple[float, float]], float, dict[str, tuple[float, float]]]:
    """How much recheck time each source was handed and when it asked, when
    the lane finished, and each source's search and recheck deadlines."""
    handed: dict[str, tuple[float, float]] = {}
    ended: dict[str, float] = {}
    deadlines: dict[str, list[float]] = {}

    def lane_search(client):
        time.sleep(lane_seconds)
        ended["at"] = time.monotonic()

    lane = LaneStub("Lane", search=lane_search)
    main = Stub("Main")
    scanner = Scanner(repository, lambda: preferences, [lane, main], detail_delay_seconds=0)
    original = scanner._recheck_absent
    original_search = scanner._search_within_ceiling

    def spy(client, source, preferences, **kwargs):
        now = scanner._clock()
        handed[source.platform] = (kwargs["deadline"] - now, now)
        deadlines.setdefault(source.platform, []).append(kwargs["deadline"])
        return original(client, source, preferences, **kwargs)

    def spy_search(source, client, preferences, trigger, *, deadline):
        deadlines.setdefault(source.platform, []).insert(0, deadline)
        return original_search(source, client, preferences, trigger, deadline=deadline)

    scanner._recheck_absent = spy
    scanner._search_within_ceiling = spy_search
    scanner.run_scan("manual")
    return handed, ended["at"], {platform: tuple(pair) for platform, pair in deadlines.items()}


def test_no_source_is_handed_more_recheck_time_than_a_sequential_scan_gives_it(
    tmp_path: Path, preferences: Preferences, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The load-bearing rule. Recheck reads are limited by time, so more time is
    more requests. With Craigslist out of the queue every later source reaches
    its turn sooner, sees more of the scan left, and would ask its site for more
    than it does today -- Rent.com among them, on CloudFront beside Redfin,
    which already turns the app away.

    Measured where it bites: the main lane rechecking while Craigslist is still
    reading. Shifting by Craigslist's elapsed time alone is not enough there,
    because elapsed is still short of what it will finally take, so the shift
    is floored at how long Craigslist usually takes.
    """
    lane_seconds = 0.5
    monkeypatch.setenv(SEQUENTIAL, "1")
    sequential, _, sequential_deadlines = measure_recheck_allowance(
        fresh(tmp_path, "sequential"), preferences, lane_seconds=lane_seconds
    )
    monkeypatch.delenv(SEQUENTIAL)
    laned, lane_ended, laned_deadlines = measure_recheck_allowance(
        fresh(tmp_path, "laned"), preferences, lane_seconds=lane_seconds
    )

    assert laned["Main"][1] < lane_ended, "Main never overlapped the lane, so this proves nothing"
    tolerance = 0.05
    assert laned["Main"][0] <= sequential["Main"][0] + tolerance, (
        f"the main lane was handed {laned['Main'][0]:.3f}s of recheck time against "
        f"{sequential['Main'][0]:.3f}s in turn"
    )
    # Craigslist's own allowance is what it always was: it was first in line
    # and it still starts when the scan does, so its recheck is handed the
    # scan's real deadline -- the one its search ran to -- exactly as in turn.
    # Asserted on the deadline itself rather than on seconds measured in two
    # separate scans, which a slower machine made differ by a tenth.
    for deadlines in (sequential_deadlines, laned_deadlines):
        searched, rechecked = deadlines["Lane"]
        assert rechecked == searched, "Craigslist's own recheck was charged"


def test_once_the_lane_is_over_the_shift_is_exactly_its_time(
    repository: Repository, preferences: Preferences
) -> None:
    """After Craigslist finishes, the main lane is charged precisely what it
    took -- the time a source would have had if Craigslist had gone ahead of
    it. The lane itself is never charged: its search and its recheck both run to
    the scan's real deadline."""

    def wait_for_lane(client):
        give_up = time.monotonic() + 10
        while lane_alive():
            assert time.monotonic() < give_up, "the lane never finished"
            time.sleep(0.01)

    lane = LaneStub("Lane")
    main = Stub("Main", search=wait_for_lane)
    scanner = Scanner(repository, lambda: preferences, [lane, main], detail_delay_seconds=0)
    searched: dict[str, float] = {}
    rechecked: dict[str, float] = {}
    original_search = scanner._search_within_ceiling
    original_recheck = scanner._recheck_absent

    def spy_search(source, client, preferences, trigger, *, deadline):
        searched[source.platform] = deadline
        return original_search(source, client, preferences, trigger, deadline=deadline)

    def spy_recheck(client, source, preferences, **kwargs):
        rechecked[source.platform] = kwargs["deadline"]
        return original_recheck(client, source, preferences, **kwargs)

    scanner._search_within_ceiling = spy_search
    scanner._recheck_absent = spy_recheck

    scanner.run_scan("manual")

    deadline = searched["Lane"]
    assert searched["Main"] < deadline, "the main line searched to the real deadline"
    assert rechecked["Lane"] == deadline, "Craigslist's own recheck was shifted"
    assert scanner.last_lane_seconds is not None and scanner.last_lane_seconds > 0
    assert rechecked["Main"] == pytest.approx(deadline - scanner.last_lane_seconds, abs=1e-9)


def test_a_sequential_scan_hands_every_source_the_real_deadline(
    repository: Repository, preferences: Preferences, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(SEQUENTIAL, "1")
    lane = LaneStub("Lane")
    main = Stub("Main")
    scanner = Scanner(repository, lambda: preferences, [lane, main], detail_delay_seconds=0)
    searched: dict[str, float] = {}
    rechecked: dict[str, float] = {}
    original_search = scanner._search_within_ceiling
    original_recheck = scanner._recheck_absent

    def spy_search(source, client, preferences, trigger, *, deadline):
        searched[source.platform] = deadline
        return original_search(source, client, preferences, trigger, deadline=deadline)

    def spy_recheck(client, source, preferences, **kwargs):
        rechecked[source.platform] = kwargs["deadline"]
        return original_recheck(client, source, preferences, **kwargs)

    scanner._search_within_ceiling = spy_search
    scanner._recheck_absent = spy_recheck

    scanner.run_scan("manual")

    assert rechecked == searched


# --- What a person watching the progress bar sees ---------------------------


def test_progress_counts_up_and_names_every_source_in_flight(
    repository: Repository, preferences: Preferences
) -> None:
    """Completion was recorded as a source's position in the list, which is only
    a count while sources finish in order. Craigslist is near the front and now
    finishes whenever it finishes, so a position would move the count backwards
    in front of whoever was watching it."""
    lane_in, mains_done = threading.Event(), threading.Event()
    box: dict[str, object] = {}

    def lane_search(client):
        lane_in.set()
        assert mains_done.wait(10)

    def main_search(client):
        assert lane_in.wait(10)
        box["both"] = box["scanner"].progress

    lane = LaneStub("Lane", search=lane_search)
    main = Stub("Main", search=main_search)
    tail = Stub("Tail", search=lambda client: mains_done.set())
    scanner = Scanner(repository, lambda: preferences, [lane, main, tail], detail_delay_seconds=0)
    box["scanner"] = scanner
    counts: list[int] = []
    percents: list[int] = []
    headings: list[object] = []
    original = scanner._finish_source_progress

    def recording(*args, **kwargs):
        original(*args, **kwargs)
        snapshot = scanner.progress
        counts.append(int(snapshot["sources_completed"]))
        percents.append(int(snapshot["percent"]))
        headings.append(snapshot["current_source"])

    scanner._finish_source_progress = recording

    outcome = scanner.run_scan("manual")

    assert outcome.status == "completed"
    assert counts == [1, 2, 3], f"completion went {counts}"
    assert percents == sorted(percents), f"the bar went {percents}"
    order = [s.platform for s in scanner._eligible_sources("manual") if s.platform in {"Lane", "Main"}]
    assert box["both"]["current_source"] == " and ".join(order)
    # Main finishes first, with the lane still reading: the heading goes on
    # naming the lane rather than going blank.
    assert headings[0] == "Lane", f"with the lane still reading, the heading said {headings[0]!r}"
    final = scanner.progress
    assert final["sources_completed"] == 3
    assert final["current_source"] is None
    assert final["percent"] == 100


# --- Joining, failure, and the paths that end a source early ---------------


def test_the_lane_is_finished_before_the_scan_adds_up_what_it_found(
    repository: Repository, preferences: Preferences
) -> None:
    """The scan adds up its totals and measures missing coverage only once the
    lane is done, so Craigslist's homes and failures belong to the scan they
    were read in. The lane reads on a client of its own, so the scan closing its
    client can never cut Craigslist off part way."""
    release = threading.Event()
    seen: dict[str, bool] = {}

    def lane_search(client):
        assert release.wait(10)
        seen["client_closed_during_lane"] = client.is_closed

    lane = LaneStub("Lane", search=lane_search)
    main = Stub("Main")
    scanner = Scanner(repository, lambda: preferences, [lane, main], detail_delay_seconds=0)

    def coverage(client, preferences):
        seen["lane_alive_at_coverage"] = lane_alive()

    scanner._measure_missing_coverage = coverage
    timer = threading.Timer(0.3, release.set)
    timer.start()
    try:
        outcome = scanner.run_scan("manual")
    finally:
        timer.cancel()
        release.set()

    assert seen["lane_alive_at_coverage"] is False, "coverage ran while the lane was still reading"
    assert seen["client_closed_during_lane"] is False
    assert outcome.status == "completed"


def test_a_lane_that_crashes_is_recorded_and_the_rest_of_the_scan_is_kept(
    repository: Repository, preferences: Preferences
) -> None:
    """A failure inside a source's search is already recorded against it. One
    outside it -- in the bookkeeping before the search -- would otherwise end a
    background thread silently, leave its row saying "running" for ever, and
    leave its name on the progress panel."""
    lane = LaneStub("Lane", listings=[room("Lane", "1")])
    main = Stub("Main", listings=[room("Main", "1")])
    scanner = Scanner(repository, lambda: preferences, [lane, main], detail_delay_seconds=0)
    original = scanner._seconds_until_readable

    def readable(source, trigger):
        if source.platform == "Lane":
            raise RuntimeError("the lane fell over outside its search")
        return original(source, trigger)

    scanner._seconds_until_readable = readable

    outcome = scanner.run_scan("manual")

    assert outcome.status == "completed_with_errors"
    assert outcome.sources_failed == 1
    assert outcome.listings_added == 1, "the main lane's homes were lost to the lane's crash"
    rows = source_rows(repository, outcome.run_id)
    assert rows["Lane"]["status"] == "error", "the crashed lane's row was left open"
    assert "RuntimeError" in (rows["Lane"]["message"] or "")
    # Keyed by platform, the rows above would hide a second Lane row behind the
    # first -- the stuck one this exists to prevent.
    with repository.connection() as connection:
        lane_rows = [
            row["status"]
            for row in connection.execute(
                "SELECT status FROM source_runs WHERE scan_run_id = ? AND platform = 'Lane'",
                (outcome.run_id,),
            )
        ]
    assert lane_rows == ["error"], f"the crashed lane left rows {lane_rows}"
    assert rows["Main"]["status"] == "success"
    final = scanner.progress
    assert final["sources_completed"] == 2
    assert final["current_source"] is None
    assert not lane_alive()


@pytest.mark.parametrize(
    "setup, expected",
    [
        pytest.param(
            {"source": type("Manual", (LaneStub,), {"mode": "manual", "manual_reason": "Needs a person."})},
            "manual",
            id="not-automatic",
        ),
        pytest.param({"max_scan_seconds": 0.0}, "skipped", id="out-of-time"),
    ],
)
def test_a_lane_source_that_ends_early_still_closes_its_row_and_its_progress(
    repository: Repository, preferences: Preferences, setup, expected
) -> None:
    """Every early way out of a source used to be a `continue` in the loop. In
    a method they are returns, and each must still write the row and retire the
    source from the bar, or the bar sticks short of the end."""
    lane_type = setup.get("source", LaneStub)
    lane = lane_type("Lane")
    main = Stub("Main")
    options = {"detail_delay_seconds": 0}
    if "max_scan_seconds" in setup:
        options["max_scan_seconds"] = setup["max_scan_seconds"]
    scanner = Scanner(repository, lambda: preferences, [lane, main], **options)

    outcome = scanner.run_scan("manual")

    rows = source_rows(repository, outcome.run_id)
    assert rows["Lane"]["status"] == expected
    final = scanner.progress
    assert final["sources_completed"] == 2
    assert final["current_source"] is None
    assert not lane_alive()


def test_the_first_search_still_marks_a_lane_source_as_set_up(
    repository: Repository, preferences: Preferences
) -> None:
    """The very first search is the one this exists to speed up, and it is also
    the one that records which sources have been read at least once."""
    lane = LaneStub("Lane", listings=[room("Lane", "1")])
    main = Stub("Main", listings=[room("Main", "1")])
    scanner = Scanner(repository, lambda: preferences, [lane, main], detail_delay_seconds=0)

    outcome = scanner.run_scan("initial_discovery")

    assert outcome.status == "completed"
    assert repository.source_initialized(scanner._source_key(lane))
    assert repository.source_initialized(scanner._source_key(main))


def test_a_lane_left_behind_is_never_started_a_second_time(
    repository: Repository, preferences: Preferences, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The join is bounded, so a lane stuck somewhere nothing bounds is walked
    away from instead of waited on for ever. Still reading Craigslist when the
    next scan began, a second lane would double the requests to the one site
    all of this is careful not to annoy -- and a stale lane finishing late must
    not write its numbers into somebody else's scan.
    """
    stuck = threading.Event()
    searches: list[int] = []

    class Stuck(LaneStub):
        @property
        def detail_budget(self):
            # Read outside every ceiling the scan has, which is the point.
            stuck.wait(15)
            return 0

    # With a home of its own, so a stale lane finishing late has something to
    # try to count into the next scan.
    lane = Stuck("Lane", search=lambda client: searches.append(1), listings=[room("Lane", "1")])
    main = Stub("Main", listings=[room("Main", "1")])
    scanner = Scanner(
        repository,
        lambda: preferences,
        [lane, main],
        detail_delay_seconds=0,
        timeout_seconds=0.01,
        max_scan_seconds=0.5,
    )
    monkeypatch.setattr(scanner, "_lane_join_grace", lambda trigger: 0.0)

    try:
        started = time.monotonic()
        first = scanner.run_scan("manual")
        assert time.monotonic() - started < 5, "the scan waited on a stuck lane"
        assert first.sources_failed == 1
        assert lane_alive(), "this test needs the lane to still be stuck"
        # The scan that walked away settles the lane in its own record.
        left = scanner.progress
        assert left["sources_completed"] == 2
        assert left["sources_failed"] == 1
        assert left["current_source"] is None
        first_rows = source_rows(repository, first.run_id)
        assert first_rows["Lane"]["status"] == "error"
        assert first_rows["Lane"]["message"] == scanner_module.LEFT_BEHIND_MESSAGE

        second = scanner.run_scan("manual")
        assert searches == [1], "a second Craigslist lane was started beside the first"
        assert source_rows(repository, second.run_id)["Lane"]["status"] == "skipped"
        settled = scanner.progress
    finally:
        stuck.set()

    give_up = time.monotonic() + 15
    while lane_alive():
        assert time.monotonic() < give_up, "the stale lane never ended"
        time.sleep(0.01)
    after = scanner.progress
    for key in ("sources_completed", "listings_seen", "listings_added", "sources_failed", "current_source"):
        assert after[key] == settled[key], f"the stale lane changed the next scan's {key}"


# --- Found by adversarial review -------------------------------------------


@pytest.mark.parametrize("sequential", [True, False], ids=["in-turn", "beside-a-lane"])
def test_a_source_skipped_for_time_in_turn_is_skipped_beside_a_lane_too(
    repository: Repository, preferences: Preferences, monkeypatch: pytest.MonkeyPatch, sequential: bool
) -> None:
    """The critical case the first version missed. Only the recheck pass was
    charged with the lane's time; the search, the detail pages and the decision
    to skip a source for time were not. On a scan up against its limit -- seven
    of the last seventy-two real ones -- the main line reached sources the
    sequential scan would have skipped, and read them: requests a sequential
    scan never sent, on exactly the scans that were already long."""
    if sequential:
        monkeypatch.setenv(SEQUENTIAL, "1")
    read: list[str] = []
    lane = LaneStub("Lane")
    main = Stub("Main", search=lambda client: read.append("Main"))
    scanner = Scanner(
        repository,
        lambda: preferences,
        [lane, main],
        detail_delay_seconds=0,
        timeout_seconds=0.2,
        max_scan_seconds=0.7,
    )
    original = scanner._seconds_until_readable

    def readable(source, trigger):
        # Craigslist taking most of the scan, somewhere no ceiling cuts it short.
        if source.platform == "Lane":
            time.sleep(0.6)
        return original(source, trigger)

    scanner._seconds_until_readable = readable

    outcome = scanner.run_scan("manual")

    assert source_rows(repository, outcome.run_id)["Main"]["status"] == "skipped"
    assert read == [], "a source a sequential scan skips for time was read"


def test_pressing_check_during_a_scan_leaves_that_scans_panel_alone(
    repository: Repository, preferences: Preferences
) -> None:
    """A refusal is decided before the scan lock is asked for, so a Check
    refused for today's budget while a scan was running reset that scan's panel.
    Recorded as positions, the next source used to put the count right again;
    counted up it never recovered, and a lane's own updates were dropped for the
    rest of the scan -- a finished scan left saying it had not finished."""
    release = threading.Event()
    calls: list[int] = []

    def allowed(*args, **kwargs):
        calls.append(1)
        return len(calls) == 1

    lane = LaneStub("Lane", search=lambda client: release.wait(10))
    main = Stub("Main")
    scanner = Scanner(
        repository, lambda: preferences, [lane, main], detail_delay_seconds=0, scan_allowed=allowed
    )
    assert scanner.start_scan("manual") is True
    try:
        give_up = time.monotonic() + 10
        while scanner.progress["sources_completed"] < 1:
            assert time.monotonic() < give_up, "the main line never finished its source"
            time.sleep(0.01)
        before = scanner.progress

        refused = scanner.run_scan("manual")

        during = scanner.progress
    finally:
        release.set()
    assert scanner.wait_until_idle(15)
    assert refused.status == "budget_reached"
    assert during["running"] is True, "the refusal marked the running scan as over"
    assert during["sources_completed"] == before["sources_completed"]
    final = scanner.progress
    assert final["sources_completed"] == 2
    assert final["current_source"] is None
    assert final["percent"] == 100


def test_the_panel_counts_every_home_a_failing_source_had_already_read(
    repository: Repository, preferences: Preferences
) -> None:
    """A home is counted the moment it is read, and the panel used to be put
    back in line with that count only when the source finished -- so a source
    that failed part way through a home left the panel one short of what the
    scan recorded."""
    main = Stub("Main", listings=[room("Main", "1"), room("Main", "2")])
    scanner = Scanner(repository, lambda: preferences, [main], detail_delay_seconds=0)
    original = repository.find_listing
    reads: list[int] = []

    def find_listing(*args, **kwargs):
        reads.append(1)
        if len(reads) == 2:
            raise RuntimeError("the database hiccupped on the second home")
        return original(*args, **kwargs)

    repository.find_listing = find_listing

    outcome = scanner.run_scan("manual")

    assert outcome.listings_seen == 2
    assert scanner.progress["listings_seen"] == outcome.listings_seen


def test_the_bar_keeps_moving_while_the_scan_waits_on_the_lane(
    repository: Repository, preferences: Preferences
) -> None:
    """Once the main line has nothing left to read, its estimate for a source
    in flight is nothing, and the lane's source is only counted when it
    finishes -- so a scan waiting on Craigslist at the end showed a bar and a
    time left that stood still until it was done."""
    release = threading.Event()
    lane = LaneStub("Lane", search=lambda client: release.wait(10))
    main = Stub("Main")
    scanner = Scanner(repository, lambda: preferences, [lane, main], detail_delay_seconds=0)
    assert scanner.start_scan("manual") is True
    try:
        give_up = time.monotonic() + 10
        while scanner.progress["sources_completed"] < 1:
            assert time.monotonic() < give_up, "the main line never finished its source"
            time.sleep(0.01)
        time.sleep(0.2)  # the main line has reached the join by now
        first = scanner.progress
        time.sleep(1.0)
        later = scanner.progress
    finally:
        release.set()
    assert scanner.wait_until_idle(15)
    assert later["percent"] > first["percent"], (
        f"the bar stood at {first['percent']}% and then {later['percent']}%"
    )


def test_a_lane_left_behind_counts_as_failed_even_if_it_finishes_before_the_totals(
    repository: Repository, preferences: Preferences, monkeypatch: pytest.MonkeyPatch
) -> None:
    """How many lane sources were left behind was worked out when the totals
    were added up, from what the lane had finished by then. A lane that finally
    finished in the gap after being left took its failure out of the record,
    and the scan reported that nothing had gone wrong."""
    stuck = threading.Event()

    class Stuck(LaneStub):
        @property
        def detail_budget(self):
            stuck.wait(15)
            return 0

    lane = Stuck("Lane", listings=[room("Lane", "1")])
    main = Stub("Main")
    scanner = Scanner(
        repository,
        lambda: preferences,
        [lane, main],
        detail_delay_seconds=0,
        timeout_seconds=0.01,
        max_scan_seconds=0.5,
    )
    monkeypatch.setattr(scanner, "_lane_join_grace", lambda trigger: 0.0)
    original = repository.finish_source_run

    def finish_source_run(row, status, *args, **kwargs):
        result = original(row, status, *args, **kwargs)
        if kwargs.get("message") == scanner_module.LEFT_BEHIND_MESSAGE:
            # The lane comes unstuck and finishes between being left and the
            # scan adding up what happened.
            stuck.set()
            give_up = time.monotonic() + 15
            while lane_alive():
                assert time.monotonic() < give_up, "the lane never finished"
                time.sleep(0.01)
        return result

    repository.finish_source_run = finish_source_run
    try:
        outcome = scanner.run_scan("manual")
    finally:
        stuck.set()

    assert outcome.sources_failed == 1, "the late finish took the lane's failure with it"
    assert outcome.status == "completed_with_errors"
    # And the lane, finishing after it was left, wrote nothing into this scan's
    # panel: the record, the panel and the log all say the same thing.
    final = scanner.progress
    assert final["sources_failed"] == outcome.sources_failed
    assert final["sources_completed"] == 2
    assert final["listings_seen"] == outcome.listings_seen
    assert final["listings_added"] == outcome.listings_added


def test_a_lane_failure_that_cannot_be_written_down_counts_once(
    repository: Repository, preferences: Preferences
) -> None:
    """A source's own error handling counts the failure and then writes its
    row. If writing the row fails too, that second failure escapes to the lane,
    which counted the source again -- one broken source reported as two."""

    def explode(client):
        raise RuntimeError("the search failed")

    lane = LaneStub("Lane", search=explode)
    main = Stub("Main")
    scanner = Scanner(repository, lambda: preferences, [lane, main], detail_delay_seconds=0)
    original = repository.finish_source_run

    def finish_source_run(row, status, *args, **kwargs):
        if status == "error":
            raise sqlite3.OperationalError("database is locked")
        return original(row, status, *args, **kwargs)

    repository.finish_source_run = finish_source_run

    outcome = scanner.run_scan("manual")

    assert outcome.sources_failed == 1


def test_a_main_line_source_that_ends_the_scan_leaves_the_panel_at_once(
    repository: Repository, preferences: Preferences
) -> None:
    """A main-line source whose failure ends the scan stayed named on the panel
    beside the lane for as long as the scan waited on the lane -- "Checking
    Craigslist and Zillow" about a source nobody was reading any more."""
    release, main_failed = threading.Event(), threading.Event()
    lane = LaneStub("Lane", search=lambda client: release.wait(10))
    main = Stub("Main")
    scanner = Scanner(repository, lambda: preferences, [lane, main], detail_delay_seconds=0)
    original = scanner._seconds_until_readable

    def readable(source, trigger):
        if source.platform == "Main":
            main_failed.set()
            raise RuntimeError("the main line fell over outside its search")
        return original(source, trigger)

    scanner._seconds_until_readable = readable
    assert scanner.start_scan("manual") is True
    try:
        assert main_failed.wait(10), "the main line never reached its source"
        give_up = time.monotonic() + 5
        while scanner.progress["current_source"] != "Lane":
            assert time.monotonic() < give_up, (
                f"the panel still says {scanner.progress['current_source']!r}"
            )
            time.sleep(0.01)
    finally:
        release.set()
    assert scanner.wait_until_idle(15)


def test_stopping_the_process_does_not_wait_for_the_lane(
    repository: Repository, preferences: Preferences
) -> None:
    """The lane was waited for in a finally, which runs for Ctrl+C too, so
    stopping a scan from a terminal hung for up to the rest of the scan plus
    over two minutes. The lane is a daemon thread and goes with the process."""
    release = threading.Event()
    lane = LaneStub("Lane", search=lambda client: release.wait(8))
    main = Stub("Main")
    scanner = Scanner(repository, lambda: preferences, [lane, main], detail_delay_seconds=0)
    original = scanner._seconds_until_readable

    def readable(source, trigger):
        if source.platform == "Main":
            raise SystemExit("stopped from the terminal")
        return original(source, trigger)

    scanner._seconds_until_readable = readable
    started = time.monotonic()
    try:
        with pytest.raises(SystemExit):
            scanner.run_scan("manual")
        waited = time.monotonic() - started
    finally:
        release.set()
    give_up = time.monotonic() + 10
    while lane_alive():
        assert time.monotonic() < give_up, "the lane never ended"
        time.sleep(0.01)

    assert waited < 4, f"stopping the scan waited {waited:.1f}s for the lane"


def test_the_lane_talks_to_its_site_exactly_as_the_scan_always_did(
    repository: Repository, preferences: Preferences, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its own client, so the scan's pool is exactly the four connections it
    always kept. Sharing that pool meant a lane took one of the slots the scan
    keeps for reads it has walked away from; widening the pool to make room let
    a fifth connection reach a site already holding four. Same headers, timeout
    and limits, and cookies are kept per site, so Craigslist sees what it always
    saw."""
    made: list[dict] = []
    real = httpx.Client

    def recording(*args, **kwargs):
        made.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(scanner_module.httpx, "Client", recording)
    lane = LaneStub("Lane")
    main = Stub("Main")

    Scanner(repository, lambda: preferences, [lane, main], detail_delay_seconds=0).run_scan("manual")

    assert len(made) == 2, f"expected the scan's client and the lane's, got {len(made)}"
    for kwargs in made:
        assert kwargs["limits"].max_connections == 4
        assert kwargs["limits"].max_keepalive_connections == 2
        assert kwargs["follow_redirects"] is True
    assert made[0]["headers"] == made[1]["headers"]
    assert made[0]["timeout"] == made[1]["timeout"]


def test_the_progress_notes_no_longer_say_sources_are_read_one_at_a_time() -> None:
    """Craigslist is read beside the others now. What stays true, and what the
    note is really about, is that each site is only ever asked one page at a
    time."""
    script = (Path(__file__).resolve().parent.parent / "sf_housing" / "static" / "scan-progress.js").read_text()

    assert "Sources are read one at a time" not in script
    assert "one page at a time" in script


def test_the_lane_never_moves_the_main_lines_estimate(
    repository: Repository, preferences: Preferences, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the main line owns the bar's estimate for the source in flight. A
    lane source that set it as it started, or cleared it as it finished, would
    take a main-line source's partial progress off the bar -- which then moves
    backwards in front of whoever is watching."""
    main_started, lane_reading = threading.Event(), threading.Event()
    estimates: dict[str, tuple[object, object]] = {}

    def estimate() -> tuple[object, object]:
        with scanner._progress_lock:
            state = scanner._progress_state
            return state["weight_current"], state["current_started_monotonic"]

    def main_search(client):
        assert lane_reading.wait(10)
        estimates["lane_started"] = estimate()
        give_up = time.monotonic() + 10
        while lane_alive():
            assert time.monotonic() < give_up, "the lane never finished"
            time.sleep(0.01)
        estimates["lane_finished"] = estimate()

    lane = LaneStub("Lane", search=lambda client: lane_reading.set())
    main = Stub("Main", search=main_search)
    scanner = Scanner(repository, lambda: preferences, [lane, main], detail_delay_seconds=0)
    # Different weights, so the estimate says whose it is.
    monkeypatch.setattr(scanner, "_source_weights", lambda sources: {"Lane": 7.0, "Main": 3.0})
    original = scanner._start_source_progress

    def in_order(source, source_index, weight, *, lane):
        # The lane's source goes on the panel only after the main line's has,
        # so a lane that overwrote the estimate would be seen doing it.
        if lane is not None:
            assert main_started.wait(10)
        original(source, source_index, weight, lane=lane)
        if lane is None:
            estimates["main_started"] = estimate()
            main_started.set()

    monkeypatch.setattr(scanner, "_start_source_progress", in_order)

    outcome = scanner.run_scan("manual")

    assert outcome.status == "completed"
    assert estimates["main_started"][0] == 3.0
    assert estimates["lane_started"] == estimates["main_started"], "the lane's start took over the estimate"
    assert estimates["lane_finished"] == estimates["main_started"], "the lane's finish cleared the estimate"


def test_a_scan_that_fails_on_the_main_line_still_counts_what_the_lane_stored(
    repository: Repository, preferences: Preferences
) -> None:
    """A scan's failure path adds up its totals too, after the lane has been
    joined. Left out there, a scan that fell over on one main-line source would
    record none of the Craigslist homes it had already stored."""
    lane = LaneStub("Lane", listings=[room("Lane", "1")])
    main = Stub("Main")
    scanner = Scanner(repository, lambda: preferences, [lane, main], detail_delay_seconds=0)
    original = scanner._seconds_until_readable

    def readable(source, trigger):
        if source.platform == "Main":
            raise RuntimeError("the main line fell over outside its search")
        return original(source, trigger)

    scanner._seconds_until_readable = readable

    outcome = scanner.run_scan("manual")

    assert outcome.status == "failed"
    assert (outcome.listings_seen, outcome.listings_added) == (1, 1)
    with repository.connection() as connection:
        stored = connection.execute(
            "SELECT status, listings_seen, listings_added FROM scan_runs WHERE id = ?",
            (outcome.run_id,),
        ).fetchone()
    assert (stored["status"], stored["listings_seen"], stored["listings_added"]) == ("failed", 1, 1)


def test_a_lane_that_cannot_open_its_client_still_records_its_source(
    repository: Repository, preferences: Preferences, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lane opens a client of its own, before any of the source's own
    handling has run. Without a handler around that, a client that could not
    be opened would end the thread silently: no row saying what happened to
    Craigslist, no failure in the scan, and a bar one source short of the end."""
    real = httpx.Client

    def refusing(*args, **kwargs):
        if threading.current_thread().name == LANE_THREAD:
            raise OSError("too many open files")
        return real(*args, **kwargs)

    monkeypatch.setattr(scanner_module.httpx, "Client", refusing)
    lane = LaneStub("Lane", listings=[room("Lane", "1")])
    main = Stub("Main", listings=[room("Main", "1")])
    scanner = Scanner(repository, lambda: preferences, [lane, main], detail_delay_seconds=0)

    outcome = scanner.run_scan("manual")

    assert outcome.status == "completed_with_errors"
    assert outcome.sources_failed == 1
    assert outcome.listings_added == 1, "the main line's homes were lost with the lane"
    rows = source_rows(repository, outcome.run_id)
    assert rows["Lane"]["status"] == "error"
    assert "too many open files" in (rows["Lane"]["message"] or "")
    final = scanner.progress
    assert final["sources_completed"] == 2
    assert final["current_source"] is None


@pytest.mark.parametrize("sequential", [True, False], ids=["in-turn", "beside-a-lane"])
def test_a_lane_quicker_than_usual_never_costs_a_source_its_turn(
    repository: Repository, preferences: Preferences, monkeypatch: pytest.MonkeyPatch, sequential: bool
) -> None:
    """Found by the review's critic. While the lane is reading, the main line is
    charged the lane's usual time, and a usual time far above this run's -- a
    few stalled Craigslist runs are enough -- skipped the next source for time.
    A skip is instant, so every source after it was reached at once and skipped
    too: the rest of a scan gone, under a message about the time limit, on a
    scan that read in turn finishes with time to spare."""
    if sequential:
        monkeypatch.setenv(SEQUENTIAL, "1")
    read: list[str] = []
    lock = threading.Lock()

    def reading(name):
        def run(client):
            with lock:
                read.append(name)
            time.sleep(0.05)
        return run

    lane = LaneStub("Lane")
    mains = [Stub(f"Main{index}", search=reading(f"Main{index}")) for index in range(3)]
    scanner = Scanner(
        repository,
        lambda: preferences,
        [lane, *mains],
        detail_delay_seconds=0,
        timeout_seconds=0.1,
        max_scan_seconds=2.0,
    )
    # Craigslist usually takes nearly the whole scan; this time it takes 0.3s,
    # leaving over a second to spare however slow the machine running this is.
    monkeypatch.setattr(
        scanner,
        "_source_weights",
        lambda sources: {source.platform: 1.95 if source.platform == "Lane" else 0.05 for source in sources},
    )
    original = scanner._seconds_until_readable

    def readable(source, trigger):
        if source.platform == "Lane":
            time.sleep(0.3)
        return original(source, trigger)

    scanner._seconds_until_readable = readable

    outcome = scanner.run_scan("manual")

    rows = source_rows(repository, outcome.run_id)
    skipped = sorted(platform for platform, row in rows.items() if row["status"] == "skipped")
    assert skipped == [], f"skipped for time: {skipped}"
    assert sorted(read) == ["Main0", "Main1", "Main2"]
