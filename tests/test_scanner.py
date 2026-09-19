from __future__ import annotations

import threading
import pathlib
import tempfile
import time
from dataclasses import replace

from sf_housing.database import Repository
from sf_housing.gmail_alerts import GmailAlertError
from sf_housing.models import ListingCandidate
from sf_housing.preferences import Preferences
from sf_housing.scanner import Scanner


class GoodSource:
    platform = "Good"
    mode = "automatic"
    search_url = "https://example.test/good"
    manual_reason = None
    detail_budget = 0

    def search(self, client, preferences):
        return [
            ListingCandidate(
                platform=self.platform,
                source_id="1",
                title="Sunny private room in a house near Golden Gate Park",
                original_url="https://example.test/listing/1",
                price=1500,
                neighborhood="NOPA",
                summary="Flexible lease, communal garden, 4 roommates.",
                metadata={"property_type": "house", "rooms_in_property": "4"},
            )
        ]

    def enrich(self, client, listing):
        return listing


class BrokenSource:
    platform = "Broken"
    mode = "automatic"
    search_url = "https://example.test/broken"
    manual_reason = None
    detail_budget = 0

    def search(self, client, preferences):
        raise RuntimeError("upstream unavailable")

    def enrich(self, client, listing):
        return listing


class GmailProviderSource(GoodSource):
    platform = "Zillow"
    connector_key = "gmail"
    connector_state_key = "gmail:zillow"
    provider = "gmail"
    last_alert_count = 1


class EmptyGmailProviderSource(GmailProviderSource):
    last_alert_count = 0

    def search(self, client, preferences):
        return []


class OpaqueGmailProviderSource(EmptyGmailProviderSource):
    last_alert_count = 1
    empty_result_message = "An alert arrived without a supported direct listing link."


class ExpiredGmailProviderSource(GmailProviderSource):
    def search(self, client, preferences):
        raise GmailAlertError("Gmail authorization is invalid or expired. Reconnect it from Sources.")


class TimedOutGmailProviderSource(GmailProviderSource):
    def search(self, client, preferences):
        raise GmailAlertError("Gmail alert search timed out or failed.")


class BlockingSource(GoodSource):
    platform = "Blocking"

    def __init__(self, started: threading.Event, release: threading.Event):
        self.started = started
        self.release = release

    def search(self, client, preferences):
        self.started.set()
        assert self.release.wait(timeout=3)
        return super().search(client, preferences)


class IncrementalDetailSource(GoodSource):
    platform = "Incremental"
    detail_budget = 1

    def search(self, client, preferences):
        self.detail_budget = 1
        return [
            ListingCandidate(
                platform=self.platform,
                source_id=str(number),
                title=f"Room {number}",
                original_url=f"https://example.test/incremental/{number}",
                price=1500,
                neighborhood="NOPA",
                summary=f"Room {number}",
            )
            for number in (1, 2)
        ]

    def enrich(self, client, listing):
        return replace(
            listing,
            summary=f"{listing.title} full detail with a sunny private room in a house.",
            metadata={"enriched": True},
        )


class TriggerAwareSource(GoodSource):
    platform = "Trigger aware"

    def __init__(self):
        self.trigger = None

    def search_for_trigger(self, client, preferences, trigger):
        self.trigger = trigger
        return self.search(client, preferences)


def test_one_source_failure_does_not_stop_other_sources(
    repository: Repository, preferences: Preferences
) -> None:
    scanner = Scanner(
        repository,
        lambda: preferences,
        [BrokenSource(), GoodSource()],
        detail_delay_seconds=0,
    )

    outcome = scanner.run_scan("test")

    assert outcome.status == "completed_with_errors"
    assert outcome.sources_failed == 1
    assert outcome.listings_added == 1
    assert len(repository.query_listings(0, view="all")) == 1
    statuses = {item["platform"]: item for item in repository.latest_source_runs()}
    assert statuses["Broken"]["status"] == "error"
    assert "upstream unavailable" in statuses["Broken"]["message"]
    assert statuses["Good"]["status"] == "success"


def test_scan_passes_trigger_to_sources_that_support_trigger_specific_depth(
    repository: Repository, preferences: Preferences
) -> None:
    source = TriggerAwareSource()
    scanner = Scanner(repository, lambda: preferences, [source], detail_delay_seconds=0)

    outcome = scanner.run_scan("manual")

    assert outcome.status == "completed"
    assert source.trigger == "manual"


def test_gmail_provider_success_updates_provider_and_aggregate_state(repository, preferences) -> None:
    scanner = Scanner(repository, lambda: preferences, [GmailProviderSource()], detail_delay_seconds=0)

    outcome = scanner.run_scan("gmail_test")

    provider = repository.connector_state("gmail:zillow")
    aggregate = repository.connector_state("gmail")
    assert outcome.status == "completed"
    assert provider is not None and provider.state == "working"
    assert aggregate is not None and aggregate.state == "working"
    assert repository.query_listings(0, view="all")


def test_gmail_provider_distinguishes_waiting_from_unparseable_alert(repository, preferences) -> None:
    waiting = Scanner(
        repository, lambda: preferences, [EmptyGmailProviderSource()], detail_delay_seconds=0
    )
    waiting.run_scan("gmail_test")
    assert repository.connector_state("gmail:zillow").state == "waiting_first_alert"

    opaque = Scanner(
        repository, lambda: preferences, [OpaqueGmailProviderSource()], detail_delay_seconds=0
    )
    opaque.run_scan("gmail_test")
    state = repository.connector_state("gmail:zillow")
    assert state is not None and state.state == "degraded"
    assert "without a supported" in state.message


def test_expired_gmail_does_not_stop_a_later_public_source(repository, preferences) -> None:
    scanner = Scanner(
        repository,
        lambda: preferences,
        [ExpiredGmailProviderSource(), GoodSource()],
        detail_delay_seconds=0,
    )

    outcome = scanner.run_scan("manual")

    assert outcome.status == "completed_with_errors"
    assert repository.connector_state("gmail:zillow").state == "authorization_expired"
    assert repository.connector_state("gmail").state == "authorization_expired"
    statuses = {run["platform"]: run["status"] for run in repository.latest_source_runs()}
    assert statuses["Zillow"] == "error"
    assert statuses["Good"] == "success"


def test_gmail_timeout_is_provider_degraded_and_public_source_still_runs(
    repository, preferences
) -> None:
    scanner = Scanner(
        repository,
        lambda: preferences,
        [TimedOutGmailProviderSource(), GoodSource()],
        detail_delay_seconds=0,
    )

    outcome = scanner.run_scan("gmail_test")

    assert outcome.status == "completed_with_errors"
    assert repository.connector_state("gmail:zillow").state == "degraded"
    statuses = {run["platform"]: run["status"] for run in repository.latest_source_runs()}
    assert statuses == {"Good": "success", "Zillow": "error"}


def test_scheduled_scan_runs_fast_sources_before_slow_browser_helpers(
    repository: Repository, preferences: Preferences
) -> None:
    def source(
        platform: str,
        scheduled_only: bool = False,
        manual_scan_enabled: bool = False,
    ):
        return type(
            "Source",
            (),
            {
                "platform": platform,
                "scheduled_only": scheduled_only,
                "manual_scan_enabled": manual_scan_enabled,
                "mode": "automatic",
                "search_url": "https://example.test",
                "manual_reason": None,
                "detail_budget": 0,
            },
        )()

    scanner = Scanner(
        repository,
        lambda: preferences,
        [
            source("Facebook Marketplace", True, True),
            source("Facebook Groups", True, True),
            source("Furnished Finder", True),
            source("Craigslist"),
            source("SpareRoom"),
            source("Zillow"),
        ],
    )

    assert [item.platform for item in scanner._eligible_sources("scheduled")] == [
        "Craigslist",
        "SpareRoom",
        "Zillow",
        "Furnished Finder",
        "Facebook Marketplace",
        "Facebook Groups",
    ]

    assert [item.platform for item in scanner._eligible_sources("manual")] == [
        "Craigslist",
        "SpareRoom",
        "Zillow",
        "Facebook Marketplace",
        "Facebook Groups",
    ]


def test_a_one_off_source_list_does_not_run_every_configured_source(
    repository: Repository, preferences: Preferences
) -> None:
    scanner = Scanner(repository, lambda: preferences, [BrokenSource(), GoodSource()], detail_delay_seconds=0)

    outcome = scanner.run_scan("backfill", sources=[GoodSource()])

    assert outcome.status == "completed"
    assert outcome.sources_failed == 0
    assert outcome.listings_added == 1
    assert "Broken" not in {item["platform"] for item in repository.latest_source_runs()}


def test_overlapping_scans_are_skipped(
    repository: Repository, preferences: Preferences
) -> None:
    started, release = threading.Event(), threading.Event()
    scanner = Scanner(
        repository,
        lambda: preferences,
        [BlockingSource(started, release)],
        detail_delay_seconds=0,
    )
    thread = threading.Thread(target=scanner.run_scan, args=("scheduled",))
    thread.start()
    assert started.wait(timeout=2)

    overlapping = scanner.run_scan("manual")
    release.set()
    thread.join(timeout=3)

    assert overlapping.status == "skipped"
    assert overlapping.already_running is True
    assert not thread.is_alive()


def test_start_scan_marks_running_before_returning(
    repository: Repository, preferences: Preferences
) -> None:
    started, release = threading.Event(), threading.Event()
    scanner = Scanner(
        repository,
        lambda: preferences,
        [BlockingSource(started, release)],
        detail_delay_seconds=0,
    )

    assert scanner.start_scan("manual") is True
    assert scanner.is_running is True
    assert started.wait(timeout=2)
    assert scanner.start_scan("manual") is False

    release.set()


def test_waiting_for_a_scan_does_not_give_up_when_the_machine_is_busy(
    repository: Repository, preferences: Preferences
) -> None:
    """The scan is waited on, not timed.

    The suite used to poll ``is_running`` until three seconds had passed and
    then assert on whatever it found. Three seconds is plenty on an idle
    machine -- 188ms measured -- but this runs in a process full of other
    tests' scan threads, and under that contention the same scan took 9.6
    seconds with eight busy threads and 28 with twenty-four. The deadline was
    not measuring the scanner; it was measuring the machine.
    """
    started, release = threading.Event(), threading.Event()
    scanner = Scanner(
        repository,
        lambda: preferences,
        [BlockingSource(started, release)],
        detail_delay_seconds=0,
    )

    assert scanner.wait_until_idle(timeout=0) is True, "an idle scanner is idle"
    assert scanner.start_scan("manual") is True
    assert started.wait(timeout=30)
    assert scanner.wait_until_idle(timeout=0.05) is False, "a running scan is not idle"

    release.set()
    assert scanner.wait_until_idle() is True
    assert scanner.is_running is False
    assert scanner.progress["status"] == "completed"


def test_scan_progress_reports_real_source_and_completion(
    repository: Repository, preferences: Preferences
) -> None:
    """The progress panel has to describe the scan that is actually running.

    Waited on rather than polled against a deadline. This asked whether the
    scan was over every ten milliseconds until three seconds had passed, and
    then asserted on whatever it found -- so on a machine busy enough to take
    longer it read the half-finished state and blamed the scanner for it. The
    suite got slower, and the failure that followed was about the clock.
    """
    started, release = threading.Event(), threading.Event()
    scanner = Scanner(
        repository,
        lambda: preferences,
        [BlockingSource(started, release), GoodSource()],
        detail_delay_seconds=0,
    )

    assert scanner.start_scan("manual") is True
    # A failsafe, not a pace: the wait returns the moment the source is entered.
    assert started.wait(timeout=30), "the scan never reached the first source"
    active = scanner.progress
    assert active["running"] is True
    assert active["current_source"] == "Blocking"
    assert active["sources_total"] == 2
    assert active["sources_completed"] == 0
    assert active["percent"] == 0

    release.set()
    assert scanner.wait_until_idle(), "the scan never finished"

    finished = scanner.progress
    assert finished["running"] is False
    assert finished["status"] == "completed"
    assert finished["sources_completed"] == 2
    assert finished["listings_seen"] == 2
    assert finished["percent"] == 100


def test_detail_enrichment_is_preserved_and_advances_on_later_scans(
    repository: Repository, preferences: Preferences
) -> None:
    scanner = Scanner(
        repository,
        lambda: preferences,
        [IncrementalDetailSource()],
        detail_delay_seconds=0,
    )

    scanner.run_scan("first")
    after_first = {item["source_id"]: item for item in repository.query_listings(0, view="all")}
    assert "full detail" in after_first["1"]["summary"]
    assert after_first["2"]["summary"] == "Room 2"

    scanner.run_scan("second")
    after_second = {item["source_id"]: item for item in repository.query_listings(0, view="all")}
    assert "full detail" in after_second["1"]["summary"]
    assert "full detail" in after_second["2"]["summary"]
    assert after_second["1"]["metadata"]["enriched"] is True


def test_a_source_that_stops_answering_does_not_take_the_scan_with_it(
    repository: Repository, preferences: Preferences, monkeypatch
) -> None:
    """An HTTP read timeout bounds each chunk of a response, not the whole of
    it, so a server that trickles bytes holds its connection for as long as it
    likes. One did: AvalonBay took 200 seconds over a single request that
    normally takes two, and the sixteen sources queued behind it were skipped
    to keep the scan inside its limit. A stall has to cost one source, never
    the rest of them."""
    import sf_housing.scanner as scanner_module

    monkeypatch.setattr(scanner_module, "SOURCE_HARD_CEILING_SECONDS", 1.0)

    class Stalled:
        platform = "Stalled"
        mode = "automatic"
        search_url = "https://example.test"
        manual_reason = None
        detail_budget = 0

        def search(self, client, preferences):
            time.sleep(30)
            return []

    started = time.monotonic()
    scanner = Scanner(
        repository, lambda: preferences, [Stalled(), GoodSource()], detail_delay_seconds=0
    )
    outcome = scanner.run_scan("test")
    elapsed = time.monotonic() - started

    assert elapsed < 20, "the scan waited out the stall instead of walking away"
    assert outcome.listings_added == 1, "the healthy source lost its results too"
    assert outcome.sources_failed == 1
    statuses = {item["platform"]: item for item in repository.latest_source_runs()}
    assert statuses["Good"]["status"] == "success"
    assert statuses["Stalled"]["status"] == "error"
    assert "stopped answering" in statuses["Stalled"]["message"]


def test_a_healthy_source_is_never_cut_off_by_the_stall_ceiling(
    repository: Repository, preferences: Preferences
) -> None:
    """The ceiling sits well above the slowest healthy source. Set near one
    instead, it would start failing Craigslist, which reads several searches
    and their detail pages and wants about a minute."""
    from sf_housing.scanner import SOURCE_HARD_CEILING_SECONDS

    assert SOURCE_HARD_CEILING_SECONDS >= 70


def test_a_source_that_raises_still_reports_its_own_error(
    repository: Repository, preferences: Preferences
) -> None:
    """Running the search on a worker thread must not swallow or reshape what
    it raised: the message a reader sees is the source's own."""
    scanner = Scanner(
        repository, lambda: preferences, [BrokenSource(), GoodSource()], detail_delay_seconds=0
    )
    outcome = scanner.run_scan("test")

    assert outcome.listings_added == 1
    statuses = {item["platform"]: item for item in repository.latest_source_runs()}
    assert "upstream unavailable" in statuses["Broken"]["message"]


def test_the_stall_ceiling_never_outlasts_the_scan_itself(
    repository: Repository, preferences: Preferences
) -> None:
    """A scan with ten seconds left must not wait seventy-five for a stalled
    source. The ceiling is whichever is smaller, floored at one request's
    timeout so a healthy source is never cut off mid-fetch."""

    class Stalled:
        platform = "Stalled"
        mode = "automatic"
        search_url = "https://example.test"
        manual_reason = None
        detail_budget = 0

        def search(self, client, preferences):
            time.sleep(30)
            return []

    started = time.monotonic()
    scanner = Scanner(
        repository,
        lambda: preferences,
        [Stalled()],
        detail_delay_seconds=0,
        timeout_seconds=1.0,
        max_scan_seconds=4.0,
    )
    scanner.run_scan("test")
    elapsed = time.monotonic() - started

    assert elapsed < 15, "the scan's own budget did not bound the wait"
    statuses = {item["platform"]: item for item in repository.latest_source_runs()}
    # Cut by what the scan had left, not by its own seventy-five seconds, it
    # is a source the check ran out of time for -- not a failure. Two failures
    # pause a source for six hours, and a site cannot be blamed for a limit
    # it never reached.
    assert statuses["Stalled"]["status"] == "skipped"
    assert "4-second time limit" in statuses["Stalled"]["message"]


def test_a_source_with_no_detail_budget_is_not_scored_twice(
    repository: Repository, preferences: Preferences, monkeypatch
) -> None:
    """The provisional score ranks the queue for the detail budget, and
    nothing else reads it. Nine sources here have no detail budget, so for
    them this was scoring every listing twice and throwing one away -- 250
    regex searches per listing, for a number never used. It cost Movoto a
    third of its time."""
    import sf_housing.scanner as scanner_module

    calls = {"n": 0}
    real = scanner_module.score_listing

    def counted(listing, prefs):
        calls["n"] += 1
        return real(listing, prefs)

    monkeypatch.setattr(scanner_module, "score_listing", counted)
    scanner = Scanner(repository, lambda: preferences, [GoodSource()], detail_delay_seconds=0)
    outcome = scanner.run_scan("test")

    assert outcome.listings_added == 1
    assert calls["n"] == 1, "a source with no detail budget scored its listing twice"


def test_a_source_with_a_detail_budget_still_ranks_its_queue(
    repository: Repository, preferences: Preferences
) -> None:
    """Skipping the provisional score must not skip it where it is the thing
    deciding which homes get a detail page. Spent in arrival order instead,
    the first few results consume the budget regardless of quality."""

    class Budgeted:
        platform = "Budgeted"
        mode = "automatic"
        search_url = "https://example.test"
        manual_reason = None
        detail_budget = 1

        def __init__(self):
            self.enriched: list[str] = []

        def search(self, client, preferences):
            return [
                ListingCandidate(
                    platform="Budgeted",
                    source_id=str(index),
                    title=title,
                    original_url=f"https://example.test/{index}",
                    price=price,
                    neighborhood="Mission",
                    summary=title,
                    metadata={"address": f"{index} Valencia St"},
                )
                for index, (title, price) in enumerate([("A dud", 9000), ("A good one", 1500)])
            ]

        def enrich(self, client, listing):
            self.enriched.append(listing.source_id)
            return listing

    source = Budgeted()
    Scanner(repository, lambda: preferences, [source], detail_delay_seconds=0).run_scan("test")

    assert source.enriched == ["1"], "the budget went to the worse home"


# --------------------------------------------------------------------------
# the progress bar somebody is actually watching
# --------------------------------------------------------------------------


class SlowSource:
    """A source that takes a known, noticeable amount of time."""

    mode = "automatic"
    search_url = "https://example.test"
    manual_reason = None
    detail_budget = 0

    def __init__(self, platform: str, seconds: float):
        self.platform = platform
        self.seconds = seconds

    def search(self, client, preferences):
        time.sleep(self.seconds)
        return []


def test_the_bar_measures_time_rather_than_counting_sources(
    repository: Repository, preferences: Preferences, monkeypatch
) -> None:
    """Counting sources, a 75-second Craigslist and a 1-second Listings
    Project are a twenty-third each: the bar shows nothing for the first
    minute of a scan and then jumps. Weighted by how long each source usually
    takes, it moves at the rate the scan is actually progressing.

    Durations are stubbed rather than produced by sleeping: source runs are
    timestamped to the second, so a test source can only ever measure zero or
    one, and what it measures depends on how busy the machine is."""
    monkeypatch.setattr(
        repository, "typical_source_seconds", lambda: {"Slow": 75.0, "Quick": 1.0}
    )
    sources = [SlowSource("Slow", 0.0), SlowSource("Quick", 0.0)]
    scanner = Scanner(repository, lambda: preferences, sources, detail_delay_seconds=0)

    weights = scanner._source_weights(sources)

    assert weights == {"Slow": 75.0, "Quick": 1.0}
    # The slow one is most of the bar, not half of it.
    assert weights["Slow"] / sum(weights.values()) > 0.9


def test_progress_survives_a_database_that_will_not_answer(
    repository: Repository, preferences: Preferences, monkeypatch
) -> None:
    """Weighting is a nicety; results are not. A failure reading durations
    must cost the bar its accuracy and never cost the scan its listings."""
    def boom():
        raise RuntimeError("no")

    monkeypatch.setattr(repository, "typical_source_seconds", boom)
    scanner = Scanner(repository, lambda: preferences, [GoodSource()], detail_delay_seconds=0)

    outcome = scanner.run_scan("test")

    assert outcome.listings_added == 1
    assert scanner.progress["percent"] == 100


def test_an_unmeasured_source_never_weighs_nothing(
    repository: Repository, preferences: Preferences
) -> None:
    """A source with no history weighing zero lets the bar reach 100% with
    that source's work still to do."""
    scanner = Scanner(repository, lambda: preferences, [GoodSource()], detail_delay_seconds=0)
    weights = scanner._source_weights([SlowSource("NeverSeen", 0.0)])

    assert weights["NeverSeen"] > 0


def test_a_source_that_ends_any_way_at_all_stops_holding_up_the_bar(
    repository: Repository, preferences: Preferences
) -> None:
    """However a source ends -- done, failed, skipped, abandoned -- its share
    has to come off the bar, or the bar stalls there for the rest of the scan.

    Checked mid-scan on purpose: a finished scan reports 100% whatever the
    weights say, so asserting it at the end proves nothing about this."""
    scanner = Scanner(repository, lambda: preferences, [GoodSource()], detail_delay_seconds=0)
    scanner._update_progress(
        status="running",
        running=True,
        sources_total=4,
        weight_total=100.0,
        weight_done=0.0,
        weight_current=25.0,
        # The scanner's own clock: it counts the time the computer sleeps,
        # and time.monotonic does not, so the two do not share a zero.
        current_started_monotonic=scanner._clock(),
    )
    assert int(scanner.progress["percent"]) == 0

    scanner._finish_source_progress(1, 25.0)

    assert int(scanner.progress["percent"]) == 25, "a finished source did not leave the bar"

    scanner._finish_source_progress(2, 25.0)

    assert int(scanner.progress["percent"]) == 50


def test_a_finished_scan_always_reads_as_finished(
    repository: Repository, preferences: Preferences
) -> None:
    """Whatever the weights ended up saying, a scan that is over is 100%."""
    scanner = Scanner(
        repository, lambda: preferences, [BrokenSource(), GoodSource()], detail_delay_seconds=0
    )
    scanner.run_scan("test")

    assert scanner.progress["percent"] == 100
    assert scanner.progress["running"] is False


def test_the_bar_moves_while_a_single_slow_source_is_still_running(
    repository: Repository, preferences: Preferences
) -> None:
    """The whole point of weighting. Partway through a lone slow source the
    bar has to be somewhere between nothing and everything, rather than stuck
    at zero until it finishes.

    Driven through the progress state rather than by racing a real scan on a
    timer: under load a sleeping thread reads whatever it happens to read, and
    a test that passes on an idle machine and fails on a busy one is telling
    you about the machine."""
    scanner = Scanner(repository, lambda: preferences, [GoodSource()], detail_delay_seconds=0)
    scanner._update_progress(
        status="running",
        running=True,
        sources_total=2,
        sources_completed=0,
        weight_total=100.0,
        weight_done=0.0,
        weight_current=40.0,
        # Ten of this source's forty seconds have gone.
        current_started_monotonic=scanner._clock() - 10.0,
    )

    percent = int(scanner.progress["percent"])

    assert 0 < percent < 100, "the bar did not move inside a running source"
    assert percent == 10, f"a quarter of a 40%-weighted source should read 10%, got {percent}"


def test_a_source_running_long_cannot_show_progress_that_has_not_happened(
    repository: Repository, preferences: Preferences
) -> None:
    """Interpolating inside the running source is capped just under its own
    weight: a source taking three times its usual must not borrow the next
    one's share and march the bar past what has actually been done."""
    scanner = Scanner(repository, lambda: preferences, [GoodSource()], detail_delay_seconds=0)
    scanner._update_progress(
        status="running",
        running=True,
        sources_total=2,
        sources_completed=0,
        weight_total=100.0,
        weight_done=0.0,
        weight_current=40.0,
        # Three times as long as this source usually takes.
        current_started_monotonic=scanner._clock() - 120.0,
    )

    percent = int(scanner.progress["percent"])

    assert percent <= 40, (
        f"the bar read {percent}% while only the first source, worth 40%, had run"
    )
    assert percent >= 35, "a source well past its estimate should read near its full share"


def test_a_sweep_is_not_held_to_the_budget_a_person_is_waiting_on() -> None:
    """Four minutes is what somebody watching a spinner will sit through. A
    sweep inherits that and is no longer a sweep: it spends the four minutes on
    the first few sources and skips the ones it exists to read deeply."""
    from sf_housing.settings import Settings

    settings = Settings.from_environment()

    assert settings.deep_scan_max_seconds > settings.scan_max_seconds


def test_only_the_sweep_gets_the_longer_budget(tmp_path) -> None:
    """A setting nothing reads is a setting that does nothing, and a longer
    budget handed to an interactive scan is a four-minute promise broken."""
    from sf_housing.scanner import DEEP_SWEEP_TRIGGER, Scanner
    from sf_housing.database import Repository
    from sf_housing.preferences import parse_preferences
    from tests.conftest import TEST_PREFERENCES

    repository = Repository(tmp_path / "housing.sqlite3")
    repository.initialize()
    scanner = Scanner(
        repository,
        lambda: parse_preferences(TEST_PREFERENCES),
        [],
        max_scan_seconds=240.0,
        deep_scan_max_seconds=900.0,
    )

    assert scanner._budget_for(DEEP_SWEEP_TRIGGER) == 900.0
    for waited_on in ("manual", "scheduled", "catch_up", "startup_catchup"):
        assert scanner._budget_for(waited_on) == 240.0, waited_on


def test_a_scanner_given_no_sweep_budget_keeps_the_one_it_has(tmp_path) -> None:
    """Every existing caller constructs a Scanner without it."""
    from sf_housing.scanner import DEEP_SWEEP_TRIGGER, Scanner
    from sf_housing.database import Repository
    from sf_housing.preferences import parse_preferences
    from tests.conftest import TEST_PREFERENCES

    repository = Repository(tmp_path / "housing.sqlite3")
    repository.initialize()
    scanner = Scanner(
        repository, lambda: parse_preferences(TEST_PREFERENCES), [], max_scan_seconds=99.0
    )

    assert scanner._budget_for(DEEP_SWEEP_TRIGGER) == 99.0


def test_a_sliced_source_is_not_cut_off_at_a_waiting_scan_s_ceiling() -> None:
    """On the sweep a source is asked several narrower questions instead of
    one wide one: Zillow's eight rent bands measured about a minute against
    the seventy-five seconds a waiting scan allows. Cutting it there abandons
    the source and returns nothing, which is worse than slow."""
    from sf_housing.scanner import (
        DEEP_SOURCE_CEILING_SECONDS,
        SOURCE_HARD_CEILING_SECONDS,
    )
    from sf_housing.settings import Settings

    from sf_housing.scanner import DEEP_SWEEP_TRIGGER, Scanner

    assert DEEP_SOURCE_CEILING_SECONDS > SOURCE_HARD_CEILING_SECONDS
    # And still a ceiling: it has to fit inside the sweep's own budget.
    assert DEEP_SOURCE_CEILING_SECONDS < Settings.from_environment().deep_scan_max_seconds
    # The constants are only worth having if the run actually asks for them.
    assert Scanner._source_ceiling_for(DEEP_SWEEP_TRIGGER) == DEEP_SOURCE_CEILING_SECONDS
    for waited_on in ("manual", "scheduled", "catch_up", "startup_catchup"):
        assert Scanner._source_ceiling_for(waited_on) == SOURCE_HARD_CEILING_SECONDS, waited_on
