"""The four failures a real renter will actually meet.

Recovery used to be proven for exactly one of these: a source raising during a
scan. The rest were inferred from code. Each test here forces the real failure
and checks the user is left with a true statement and a next step.
"""

from __future__ import annotations

import pathlib
import sqlite3
import sys
import threading
import time

import httpx
import pytest

from sf_housing.connectors import connector_state_for_error
from sf_housing.database import DatabaseUnreadableError, Repository
from sf_housing.imap_alerts import ImapAlertError, ImapAlertMailbox
from sf_housing.models import ListingCandidate, ScoreResult
from sf_housing.preferences import parse_preferences
from sf_housing.scanner import Scanner
from sf_housing.sources import SourceError
from tests.conftest import TEST_PREFERENCES

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from test_imap_alerts import FakeIMAP  # noqa: E402


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


def room(source_id: str, platform: str = "Craigslist") -> ListingCandidate:
    return ListingCandidate(
        platform=platform,
        source_id=source_id,
        title="Sunny private room in NOPA",
        original_url=f"https://sfbay.craigslist.org/x/{source_id}.html",
        price=1500,
        neighborhood="NOPA",
        listing_type="Room/share",
        summary="Private room in a shared home, flexible lease.",
    )


class WorkingSource:
    platform = "Craigslist"
    mode = "automatic"
    search_url = "https://example.test/a"
    manual_reason = None
    detail_budget = 0

    def search(self, client, preferences):
        return [room("a1"), room("a2")]

    def enrich(self, client, listing):
        return listing


class RaisingSource:
    mode = "automatic"
    search_url = "https://example.test/b"
    manual_reason = None
    detail_budget = 0

    def __init__(self, platform: str, error: Exception):
        self.platform = platform
        self.error = error

    def search(self, client, preferences):
        raise self.error

    def enrich(self, client, listing):
        return listing


def scanner_for(tmp_path: pathlib.Path, sources):
    repository = Repository(tmp_path / "housing.sqlite3")
    repository.initialize()
    preferences = parse_preferences(TEST_PREFERENCES)
    return repository, Scanner(repository, lambda: preferences, sources)


# --------------------------------------------------------------------------
# 1. installing a second copy must not break the first
# --------------------------------------------------------------------------


def test_isolated_install_keeps_its_login_service_inside_the_app_root() -> None:
    """Writing to ~/Library/LaunchAgents in isolated mode repointed a working
    install's login service at a temporary directory, and the damage only showed
    up at the next login."""
    installer = (REPO_ROOT / "release_assets" / "payload" / "install.sh").read_text(encoding="utf-8")

    assert 'DEFAULT_LAUNCH_AGENTS_DIR="$APP_ROOT/LaunchAgents"' in installer
    assert 'DEFAULT_LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"' in installer
    # The default must be chosen by the isolation flag, not applied unconditionally.
    flag = installer.index('if [ "${SF_HOUSING_NO_LAUNCH_AGENT:-0}" = "1" ]')
    assignment = installer.index('LAUNCH_AGENTS_DIR="${SF_HOUSING_LAUNCH_AGENTS_DIR:-$DEFAULT_LAUNCH_AGENTS_DIR}"')
    assert flag < assignment
    # The isolated run must be told where the plist went, so the Ready Check
    # validates the one that was actually written.
    assert 'SF_HOUSING_LAUNCH_AGENTS_DIR="$LAUNCH_AGENTS_DIR" "$TOOLS_DIR/open.sh"' in installer


# --------------------------------------------------------------------------
# 2. a revoked app password
# --------------------------------------------------------------------------


def test_a_refused_password_asks_the_user_to_reconnect(tmp_path: pathlib.Path) -> None:
    """"Refused" and "the search was refused" need opposite advice, so the
    credential path states which one it is instead of leaving it to text."""
    mailbox = ImapAlertMailbox(
        tmp_path / "imap-credential.json",
        connector=lambda host, **kw: FakeIMAP(host, **kw, login_error=True),
    )
    mailbox.save_credential("someone@gmail.com", "abcd efgh ijkl mnop")

    with pytest.raises(ImapAlertError) as raised:
        mailbox.messages("from:(zillow.com) newer_than:90d")

    assert raised.value.connector_state == "authorization_expired"
    # Without the typed state the message alone reads as a generic problem.
    assert connector_state_for_error(str(raised.value)) == "degraded"


def test_a_revoked_credential_mid_scan_moves_providers_to_reconnect(
    tmp_path: pathlib.Path,
) -> None:
    repository, scanner = scanner_for(
        tmp_path,
        [
            WorkingSource(),
            RaisingSource(
                "Zillow",
                ImapAlertError("password refused", connector_state="authorization_expired"),
            ),
        ],
    )
    # The failing source has to look like a connector-backed one.
    scanner.sources[1].connector_key = "gmail"
    scanner.sources[1].connector_state_key = "gmail:zillow"

    outcome = scanner.run_scan("scheduled")

    assert outcome.status == "completed_with_errors"
    state = repository.connector_state("gmail:zillow")
    assert state.state == "authorization_expired", "the user must be told to reconnect"
    # The free sources are unaffected and the user's data is untouched.
    assert outcome.listings_added == 2


def test_a_network_failure_is_not_mistaken_for_a_bad_password() -> None:
    """Both say "refused"; only one means reconnect."""
    assert connector_state_for_error("Could not reach imap.gmail.com. Check the connection.") == "degraded"


# --------------------------------------------------------------------------
# 3. losing the network mid-scan
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectError("[Errno 8] nodename nor servname provided"),
        httpx.ReadTimeout("timed out"),
        httpx.RemoteProtocolError("server disconnected"),
    ],
)
def test_losing_the_network_keeps_what_already_worked(
    tmp_path: pathlib.Path, error: Exception
) -> None:
    repository, scanner = scanner_for(
        tmp_path, [WorkingSource(), RaisingSource("SpareRoom", error)]
    )

    outcome = scanner.run_scan("scheduled")

    assert outcome.status == "completed_with_errors"
    assert outcome.sources_failed == 1
    assert outcome.listings_added == 2, "results already collected must survive"
    with repository.connection() as connection:
        runs = {r["platform"]: r for r in connection.execute("SELECT * FROM source_runs")}
    assert runs["Craigslist"]["status"] == "success"
    assert runs["SpareRoom"]["status"] == "error"
    assert type(error).__name__ in runs["SpareRoom"]["message"], "name the real reason"


def test_every_source_failing_is_still_an_honest_finished_scan(tmp_path: pathlib.Path) -> None:
    """An entirely offline machine must not look like a successful empty search."""
    repository, scanner = scanner_for(
        tmp_path,
        [
            RaisingSource("Craigslist", httpx.ConnectError("offline")),
            RaisingSource("SpareRoom", httpx.ConnectError("offline")),
        ],
    )

    outcome = scanner.run_scan("scheduled")

    assert outcome.sources_failed == 2
    assert outcome.status == "completed_with_errors"
    assert outcome.status != "completed", "no sources succeeded, so this is not a clean run"


# --------------------------------------------------------------------------
# 4. a database that cannot be read
# --------------------------------------------------------------------------


def test_an_unreadable_database_names_itself_and_the_recovery(tmp_path: pathlib.Path) -> None:
    corrupt = tmp_path / "housing.sqlite3"
    corrupt.write_bytes(b"this is not a database")

    with pytest.raises(DatabaseUnreadableError) as raised:
        Repository(corrupt).initialize()

    message = str(raised.value)
    assert str(corrupt) in message, "say which file"
    assert "Repair" in message, "say what to do"
    assert "not lost" in message and "Do not delete" in message


def test_a_corrupt_database_is_never_replaced_automatically(tmp_path: pathlib.Path) -> None:
    """It holds every star, note and first-found date the user has built up."""
    corrupt = tmp_path / "housing.sqlite3"
    original = b"this is not a database"
    corrupt.write_bytes(original)

    with pytest.raises(DatabaseUnreadableError):
        Repository(corrupt).initialize()

    assert corrupt.read_bytes() == original, "the file must be left exactly as found"
    assert not list(tmp_path.glob("*.corrupt*")), "nothing may be moved aside either"


def test_a_healthy_database_still_initialises_normally(tmp_path: pathlib.Path) -> None:
    """The new guard must not change the working path."""
    repository = Repository(tmp_path / "housing.sqlite3")
    repository.initialize()
    repository.upsert_listing(room("ok"), ScoreResult(80, ["Fits"], "", {}))

    assert len(repository.query_listings(0, view="all")) == 1
    repository.initialize()  # idempotent
    assert len(repository.query_listings(0, view="all")) == 1


def test_the_startup_failure_reaches_the_service_log(tmp_path: pathlib.Path, caplog) -> None:
    """Repair and the install log surface the service log, not a traceback."""
    import logging

    from sf_housing.app import create_app
    from sf_housing.settings import Settings

    data = tmp_path / "data"
    data.mkdir()
    (data / "housing.sqlite3").write_bytes(b"not a database")
    preferences = tmp_path / "preferences.yaml"
    preferences.write_text(TEST_PREFERENCES, encoding="utf-8")
    settings = Settings(
        data_dir=data,
        preferences_path=preferences,
        database_path=data / "housing.sqlite3",
        log_path=data / "test.log",
    )

    with caplog.at_level(logging.ERROR, logger="sf_housing.app"):
        with pytest.raises(DatabaseUnreadableError):
            create_app(settings=settings, sources=[], enable_scheduler=False)

    assert any("Repair" in record.message for record in caplog.records)


def test_a_database_that_breaks_after_startup_is_reported_by_the_ready_check(
    tmp_path: pathlib.Path,
) -> None:
    """Corruption found while running is a diagnostic, not a crash."""
    from sf_housing.diagnostics import _application_checks
    from sf_housing.settings import Settings

    database = tmp_path / "housing.sqlite3"
    Repository(database).initialize()
    database.write_bytes(b"corrupted after the app started")
    settings = Settings(
        data_dir=tmp_path,
        preferences_path=tmp_path / "preferences.yaml",
        database_path=database,
        log_path=tmp_path / "t.log",
    )

    checks = {
        c.key: c
        for c in _application_checks(
            settings,
            Repository(database),
            request_host="127.0.0.1",
            request_port=8000,
            app_version="test",
        )
    }

    assert checks["database"].status == "blocked"
    assert "Repair" in checks["database"].action


# --------------------------------------------------------------------------
# 5. the app is stopped while a check is running
# --------------------------------------------------------------------------


def interrupted_scan(repository: Repository) -> int:
    """The rows a process killed mid-check leaves behind."""
    run_id = repository.begin_scan("scheduled")
    repository.begin_source_run(run_id, "Craigslist", "https://sfbay.craigslist.org/search/roo")
    return run_id


def scan_row(repository: Repository, run_id: int) -> dict:
    with repository.connection() as connection:
        row = connection.execute("SELECT * FROM scan_runs WHERE id = ?", (run_id,)).fetchone()
    return dict(row)


def test_a_check_cut_off_by_a_quit_is_settled_when_the_app_comes_back(
    tmp_path: pathlib.Path,
) -> None:
    """Quitting mid-check left a row saying a check was running, and nothing
    ever cleared it. The Ready Check then told people to reopen the app, and
    reopening the app changed nothing."""
    repository, scanner = scanner_for(tmp_path, [WorkingSource()])
    run_id = interrupted_scan(repository)

    assert scanner.recover_interrupted_scans() == 1

    row = scan_row(repository, run_id)
    assert row["status"] == "interrupted"
    assert row["finished_at"], "a check that is over has to have an end"


def test_the_source_left_mid_fetch_is_settled_too(tmp_path: pathlib.Path) -> None:
    """The Sources page reads source runs, not scan runs. Settling only the
    parent leaves that page showing a source still fetching."""
    repository, scanner = scanner_for(tmp_path, [WorkingSource()])
    interrupted_scan(repository)

    scanner.recover_interrupted_scans()

    with repository.connection() as connection:
        rows = [dict(r) for r in connection.execute("SELECT * FROM source_runs")]
    assert [r["status"] for r in rows] == ["interrupted"]
    assert all(r["finished_at"] for r in rows)


def test_a_check_that_really_finished_is_left_exactly_as_it_was(
    tmp_path: pathlib.Path,
) -> None:
    """Recovery rewrites history. It may only rewrite the part that is false."""
    repository, scanner = scanner_for(tmp_path, [WorkingSource()])
    scanner.run_scan("scheduled")
    before = scan_row(repository, 1)

    assert scanner.recover_interrupted_scans() == 0
    assert scan_row(repository, 1) == before


def test_a_check_that_is_genuinely_running_is_never_declared_dead(
    tmp_path: pathlib.Path,
) -> None:
    """Recovery is only allowed to speak while it holds the lock a live check
    would be holding. Without that rule a scheduled check running at startup
    would be marked interrupted underneath itself."""
    repository, scanner = scanner_for(tmp_path, [WorkingSource()])
    run_id = interrupted_scan(repository)

    settled: list[int] = []
    other = Scanner(repository, scanner.preference_loader, [WorkingSource()])
    assert other._acquire_scan_locks(), "a lock nobody holds has to be available"
    try:
        settled.append(scanner.recover_interrupted_scans())
    finally:
        other._release_scan_locks()

    assert settled == [0]
    assert scan_row(repository, run_id)["status"] == "running"


def test_the_ready_check_stops_asking_for_repair_once_the_app_restarts(
    tmp_path: pathlib.Path,
) -> None:
    """The point of all of this. The Ready Check said an interrupted check
    needed Repair; reopening the app is the recovery, so after it the check
    has to stop asking."""
    from datetime import UTC, datetime, timedelta

    from sf_housing.diagnostics import _scan_check

    repository, scanner = scanner_for(tmp_path, [WorkingSource()])
    run_id = interrupted_scan(repository)
    with repository.connection() as connection:
        connection.execute(
            "UPDATE scan_runs SET started_at = ? WHERE id = ?",
            ((datetime.now(UTC) - timedelta(hours=2)).isoformat(), run_id),
        )
        connection.commit()

    now = datetime.now(UTC)
    before = _scan_check(repository, scanner, now)
    assert before.status == "attention", "the stuck row is what the check is for"
    assert "Repair" in before.action

    scanner.recover_interrupted_scans()

    assert _scan_check(repository, scanner, now).status != "attention"


def test_reopening_the_app_is_what_actually_settles_the_record(
    tmp_path: pathlib.Path,
) -> None:
    """The path a person really takes. Recovery nothing calls is not recovery,
    and the Ready Check tells them reopening the app is the fix."""
    from sf_housing.app import create_app
    from sf_housing.settings import Settings

    database = tmp_path / "housing.sqlite3"
    repository = Repository(database)
    repository.initialize()
    run_id = interrupted_scan(repository)

    preferences_path = tmp_path / "preferences.yaml"
    preferences_path.write_text(TEST_PREFERENCES, encoding="utf-8")
    create_app(
        settings=Settings(
            data_dir=tmp_path,
            preferences_path=preferences_path,
            database_path=database,
            log_path=tmp_path / "t.log",
        ),
        sources=[],
        enable_scheduler=False,
    )

    assert scan_row(repository, run_id)["status"] == "interrupted"


# --------------------------------------------------------------------------
# 6. a source whose detail pages stop answering
# --------------------------------------------------------------------------


class StallingDetailSource:
    """Searches fine, then never finishes a detail page.

    The real shape of the failure: an HTTP read timeout bounds each chunk of a
    response rather than the whole of it, so a server that trickles bytes holds
    the connection open for as long as it likes.
    """

    mode = "automatic"
    search_url = "https://example.test/stall"
    manual_reason = None
    detail_budget = 3

    def __init__(self, platform: str = "Craigslist") -> None:
        self.platform = platform
        self.released = threading.Event()
        self.enrich_calls = 0

    def search(self, client, preferences):
        return [room("s1"), room("s2"), room("s3")]

    def enrich(self, client, listing):
        self.enrich_calls += 1
        self.released.wait(12)  # far past any ceiling a test sets
        return listing


class LateSource:
    """Runs after the stalling one, and is what starvation actually costs."""

    mode = "automatic"
    search_url = "https://example.test/late"
    manual_reason = None
    detail_budget = 0

    def __init__(self, platform: str = "Zillow") -> None:
        self.platform = platform

    def search(self, client, preferences):
        return [room("late1", self.platform), room("late2", self.platform)]

    def enrich(self, client, listing):
        return listing


def test_a_detail_page_that_never_answers_does_not_hold_the_scan(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """The ceiling was written to cover a source's whole turn, but only ever
    wrapped the search. One Craigslist check spent 593 seconds inside detail
    pages and every source behind it was skipped with nothing collected."""
    from sf_housing import scanner as scanner_module

    monkeypatch.setattr(scanner_module, "DETAIL_HARD_CEILING_SECONDS", 0.4)
    stalling = StallingDetailSource()
    repository, scanner = scanner_for(tmp_path, [stalling, LateSource()])

    try:
        started = time.monotonic()
        outcome = scanner.run_scan("scheduled")
        elapsed = time.monotonic() - started
    finally:
        stalling.released.set()

    assert stalling.enrich_calls, "the detail fetch has to have been attempted"
    assert elapsed < 9, f"a stalled detail page held the scan for {elapsed:.1f}s"
    assert outcome.status in {"completed", "completed_with_errors"}


def test_the_sources_behind_a_stalled_one_still_run(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """This is what the overrun actually cost: not one slow source, but every
    source queued behind it collecting nothing."""
    from sf_housing import scanner as scanner_module

    monkeypatch.setattr(scanner_module, "DETAIL_HARD_CEILING_SECONDS", 0.4)
    stalling = StallingDetailSource()
    repository, scanner = scanner_for(tmp_path, [stalling, LateSource()])

    try:
        scanner.run_scan("scheduled")
    finally:
        stalling.released.set()

    with repository.connection() as connection:
        platforms = {
            row["platform"] for row in connection.execute("SELECT platform FROM listings")
        }
    assert "Zillow" in platforms, "the source behind the stalled one was skipped"


def test_a_stalled_detail_page_never_loses_the_search_result(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """A detail page is an enrichment of a home already found. Giving up on the
    detail must cost the detail, never the home."""
    from sf_housing import scanner as scanner_module

    monkeypatch.setattr(scanner_module, "DETAIL_HARD_CEILING_SECONDS", 0.4)
    stalling = StallingDetailSource()
    repository, scanner = scanner_for(tmp_path, [stalling, LateSource()])

    try:
        scanner.run_scan("scheduled")
    finally:
        stalling.released.set()

    with repository.connection() as connection:
        kept = [
            row["source_id"]
            for row in connection.execute(
                "SELECT source_id FROM listings WHERE platform = 'Craigslist'"
            )
        ]
    assert sorted(kept) == ["s1", "s2", "s3"], "the searched homes have to survive"


def test_the_detail_ceiling_never_outlasts_what_the_phase_has_left(
    tmp_path: pathlib.Path,
) -> None:
    """Each call is bounded on its own, but a run of them must not add up to an
    overrun either, so the ceiling also stops at the phase's own limit."""
    from sf_housing.scanner import DETAIL_HARD_CEILING_SECONDS

    repository, scanner = scanner_for(tmp_path, [WorkingSource()])
    asked: list[float] = []

    class Recorder:
        platform = "Craigslist"

        def enrich(self, client, listing):
            raise AssertionError("never reached")

    # A clock that stands still, so each call's limit reads as its ceiling
    # exactly; limits are on the scanner's clock, never on time.monotonic.
    scanner._clock = lambda: 1000.0
    scanner._within_ceiling = lambda label, limit, run, *, timed_out, out_of_time: asked.append(limit.end - 1000.0)

    near = DETAIL_HARD_CEILING_SECONDS / 3
    scanner._enrich_within_ceiling(
        Recorder(), None, room("x"), limit=1000.0 + near
    )
    scanner._enrich_within_ceiling(
        Recorder(), None, room("x"), limit=1000.0 + DETAIL_HARD_CEILING_SECONDS * 5
    )

    assert asked[0] < DETAIL_HARD_CEILING_SECONDS, "a near limit has to shorten the ceiling"
    assert asked[0] <= near + 1e-6, f"ceiling {asked[0]} outran the phase limit {near}"
    assert asked[1] == DETAIL_HARD_CEILING_SECONDS, "a distant limit leaves a whole page's worth"


class StallingRecheckSource:
    """Finds nothing new, and stalls on the rechecks of what it used to list.

    detail_budget is zero, so the only place this source can call ``enrich`` is
    the recheck of a home missing from its search. That is what makes the test
    below aim at the recheck path and nothing else.
    """

    mode = "automatic"
    search_url = "https://example.test/recheck"
    manual_reason = None
    detail_budget = 0

    def __init__(self, platform: str = "Craigslist") -> None:
        self.platform = platform
        self.released = threading.Event()
        self.enrich_calls = 0

    def search(self, client, preferences):
        return [room("still-here", self.platform)]

    def enrich(self, client, listing):
        self.enrich_calls += 1
        self.released.wait(12)
        return listing


def test_a_stalled_recheck_does_not_hold_the_scan_either(
    tmp_path: pathlib.Path, monkeypatch
) -> None:
    """The recheck walks homes a source has stopped listing, one detail page
    each. It reads the clock before every one, but the read itself was
    unbounded, so a page that never answered spent the whole scan there."""
    from sf_housing import scanner as scanner_module

    monkeypatch.setattr(scanner_module, "DETAIL_HARD_CEILING_SECONDS", 0.4)
    stalling = StallingRecheckSource()
    repository, scanner = scanner_for(tmp_path, [stalling])
    # A shortlisted home the search no longer returns is exactly what a recheck
    # goes and looks at.
    for index in range(3):
        repository.upsert_listing(
            room(f"gone{index}"), ScoreResult(90, ["fits"], "check", {})
        )

    try:
        started = time.monotonic()
        scanner.run_scan("scheduled")
        elapsed = time.monotonic() - started
    finally:
        stalling.released.set()

    assert stalling.enrich_calls, "the recheck has to have been attempted"
    assert elapsed < 9, f"a stalled recheck held the scan for {elapsed:.1f}s"


def test_one_detail_page_is_never_worth_more_than_a_whole_source(tmp_path: pathlib.Path) -> None:
    """The ceilings are only meaningful relative to each other and to the scan.
    A single page allowed as long as a source's entire turn, or as long as the
    scan itself, is not a ceiling."""
    from sf_housing.scanner import (
        DETAIL_HARD_CEILING_SECONDS,
        SOURCE_HARD_CEILING_SECONDS,
    )
    from sf_housing.settings import Settings

    assert 0 < DETAIL_HARD_CEILING_SECONDS < SOURCE_HARD_CEILING_SECONDS
    assert DETAIL_HARD_CEILING_SECONDS < Settings.from_environment().scan_max_seconds / 4


# --------------------------------------------------------------------------
# 7. a source that asks not to be read again so soon
# --------------------------------------------------------------------------


class CountingSource:
    """Records how many times a scan actually read it."""

    platform = "Zillow"
    mode = "automatic"
    search_url = "https://example.test/zillow"
    manual_reason = None
    detail_budget = 0
    min_seconds_between_reads = 900

    def __init__(self) -> None:
        self.reads = 0

    def search(self, client, preferences):
        self.reads += 1
        return [room("z1", "Zillow"), room("z2", "Zillow")]

    def enrich(self, client, listing):
        return listing


def test_pressing_check_again_does_not_read_the_source_again(
    tmp_path: pathlib.Path,
) -> None:
    """Twenty-four pages a press, pressed while waiting, is how an address
    earns a block that outlasts the afternoon. Nothing is lost by declining:
    the homes from a minute ago are already in the pool."""
    source = CountingSource()
    repository, scanner = scanner_for(tmp_path, [source])

    for _ in range(5):
        scanner.run_scan("manual")

    assert source.reads == 1, f"read {source.reads} times in five presses"


def test_the_homes_from_the_first_read_are_still_there(tmp_path: pathlib.Path) -> None:
    """Declining to re-read must not look like a source that found nothing."""
    source = CountingSource()
    repository, scanner = scanner_for(tmp_path, [source])
    scanner.run_scan("manual")
    scanner.run_scan("manual")

    with repository.connection() as connection:
        kept = [r["source_id"] for r in connection.execute("SELECT source_id FROM listings")]
    assert sorted(kept) == ["z1", "z2"]


def test_the_skip_says_why_rather_than_looking_like_a_failure(
    tmp_path: pathlib.Path,
) -> None:
    source = CountingSource()
    repository, scanner = scanner_for(tmp_path, [source])
    scanner.run_scan("manual")
    scanner.run_scan("manual")

    with repository.connection() as connection:
        runs = [dict(r) for r in connection.execute("SELECT * FROM source_runs ORDER BY id")]
    assert runs[-1]["status"] == "skipped", runs[-1]["status"]
    assert "already collected" in (runs[-1]["message"] or "")


def test_the_nightly_sweep_is_never_held_back_by_the_floor(
    tmp_path: pathlib.Path,
) -> None:
    """It runs once a day, which is its own rate limit, and it is the only run
    that reads deeply enough to be worth protecting from a manual check that
    happened to land minutes earlier."""
    from sf_housing.scanner import DEEP_SWEEP_TRIGGER

    source = CountingSource()
    repository, scanner = scanner_for(tmp_path, [source])
    scanner.run_scan("manual")
    scanner.run_scan(DEEP_SWEEP_TRIGGER)

    assert source.reads == 2, "the sweep was held back by a check minutes earlier"


def test_a_source_without_a_floor_is_read_every_time(tmp_path: pathlib.Path) -> None:
    """The floor is opt-in. Most of these are cheap to read and nobody has
    ever objected to being asked."""
    source = CountingSource()
    source.min_seconds_between_reads = 0
    repository, scanner = scanner_for(tmp_path, [source])

    scanner.run_scan("manual")
    scanner.run_scan("manual")

    assert source.reads == 2


def test_the_floor_holds_however_many_times_somebody_presses(
    tmp_path: pathlib.Path,
) -> None:
    """Every declined check records a skipped run, and those pile up fast when
    somebody keeps pressing. Reading a fixed window of recent runs let them
    push the last real attempt out of sight, so the floor lapsed after a
    handful of presses -- exactly when it was working hardest."""
    source = CountingSource()
    repository, scanner = scanner_for(tmp_path, [source])

    for _ in range(25):
        scanner.run_scan("manual")

    assert source.reads == 1, f"the floor lapsed after some presses: {source.reads} reads"


class RefusingSource(CountingSource):
    """A source that is currently turning us away, as Zillow was."""

    def search(self, client, preferences):
        self.reads += 1
        raise SourceError("Zillow turned away an unattended request (HTTP 403).")


def test_a_source_that_is_refusing_us_is_asked_no_more_often_than_one_that_is_not(
    tmp_path: pathlib.Path,
) -> None:
    """The floor has to be measured from the last time it was asked, not the
    last time it answered. Measured from the last success, a source that is
    refusing every request has no recent success -- so the floor never applies
    and every press hammers the block that is already in place. That is the
    case the floor exists for."""
    source = RefusingSource()
    repository, scanner = scanner_for(tmp_path, [source])

    for _ in range(6):
        scanner.run_scan("manual")

    assert source.reads == 1, f"a refusing source was asked {source.reads} times"
    # And it was refused, not broken: the stand-in once raised a name nobody
    # had imported, so what this exercised was a NameError, not a refusal.
    with repository.connection() as connection:
        first = connection.execute("SELECT status, message FROM source_runs ORDER BY id LIMIT 1").fetchone()
    assert first["status"] == "error" and first["message"].startswith("SourceError:"), (
        f"the stand-in did not refuse the way a source does: {first['message']!r}"
    )


def test_a_brand_new_install_reads_the_source_straight_away(
    tmp_path: pathlib.Path,
) -> None:
    """The floor asks when this source was last read. On a fresh install the
    answer is never, and a floor that mistook that for "just now" would leave
    somebody's first check collecting nothing at all from it."""
    source = CountingSource()
    repository, scanner = scanner_for(tmp_path, [source])

    assert scanner._seconds_until_readable(source, "manual") == 0.0
    scanner.run_scan("manual")

    assert source.reads == 1, "a new install read nothing from a floored source"
    with repository.connection() as connection:
        kept = [r["source_id"] for r in connection.execute("SELECT source_id FROM listings")]
    assert sorted(kept) == ["z1", "z2"]


# --------------------------------------------------------------------------
# 7. the database rots in the middle rather than at the front
# --------------------------------------------------------------------------


def half_corrupt(source: pathlib.Path, destination: pathlib.Path) -> pathlib.Path:
    """A database whose header still reads but whose pages do not.

    This is what a bad sector, a full disk or a killed write actually leaves
    behind: SQLite opens the file and answers PRAGMAs, and only fails when a
    query walks into the damaged page.
    """
    repository = Repository(destination)
    repository.initialize()
    for index in range(400):
        repository.upsert_listing(room(f"c{index}"), ScoreResult(70, ["Fits"], "", {}))
    with sqlite3.connect(destination) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    body = bytearray(destination.read_bytes())
    # Leave page 1 -- the header and the schema -- intact, wreck the rest.
    body[8192 : 8192 + 4096] = b"\x00" * 4096
    destination.write_bytes(bytes(body))
    return destination


def test_a_database_damaged_past_its_header_is_caught_at_startup(
    tmp_path: pathlib.Path,
) -> None:
    """Corruption below page one used to open cleanly and then throw
    ``database disk image is malformed`` out of whatever query ran first, which
    reached the reader as a bare 500 with no file named and no way back. The
    check that catches it has to run where the readable message already is."""
    database = half_corrupt(tmp_path / "seed.sqlite3", tmp_path / "housing.sqlite3")

    with pytest.raises(DatabaseUnreadableError) as raised:
        Repository(database).initialize()

    assert str(database) in str(raised.value), "say which file"
    assert "Repair" in str(raised.value), "say what to do"
    assert database.exists(), "the damaged file is evidence; it is never removed"


def test_a_healthy_database_is_not_called_damaged(tmp_path: pathlib.Path) -> None:
    """The guard runs on every start, so a false positive would lock a working
    install out of its own data."""
    repository = Repository(tmp_path / "housing.sqlite3")
    repository.initialize()
    repository.upsert_listing(room("ok"), ScoreResult(80, ["Fits"], "", {}))

    repository.initialize()

    assert len(repository.query_listings(0, view="all")) == 1


# --------------------------------------------------------------------------
# 8. Repair does what the app tells the reader it does
# --------------------------------------------------------------------------


def repair_install(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    """An app root with Repair in it, as a real install has it.

    Repair runs the release's own backup rules with a Python the machine
    already has, so this gives it both: an installed runtime's ``python`` and a
    wheel in the release carrying ``sf_housing.backups`` -- built from the
    package in this repo, so these tests run the code that ships. The installer
    Repair calls afterwards is a stub; what is tested here is what Repair does
    to the data, which must not depend on the reinstall succeeding.
    """
    import shutil
    import zipfile

    app_root = tmp_path / "app"
    (app_root / "data").mkdir(parents=True)
    (app_root / "tools").mkdir(parents=True)
    (app_root / "current" / "bin").mkdir(parents=True)
    (app_root / "current" / "bin" / "python").symlink_to(sys.executable)
    shutil.copy(REPO_ROOT / "release_assets" / "payload" / "tools" / "repair.sh", app_root / "tools")
    release = tmp_path / "release"
    (release / "payload").mkdir(parents=True)
    with zipfile.ZipFile(release / "payload" / "sf_home_finder-0.0.0-py3-none-any.whl", "w") as wheel:
        for name in ("__init__.py", "backups.py"):
            wheel.write(REPO_ROOT / "sf_housing" / name, f"sf_housing/{name}")
    installer = release / "payload" / "install.sh"
    installer.write_text("#!/bin/bash\necho stub installer ran\n", encoding="utf-8")
    installer.chmod(0o755)
    return app_root, release


def run_repair(app_root: pathlib.Path, release: pathlib.Path, **extra_env: str):
    import subprocess

    # A tripwire, not a formality. `launchctl bootout <plist>` stops whatever
    # service carries the label *inside* that plist, so a test that created one
    # here would stop the real app on the machine running the tests. Repair
    # finds the plist under HOME, which is this temporary directory.
    plist = app_root.parent / "Library" / "LaunchAgents" / "com.sfhousing.monitor.plist"
    assert not plist.exists(), "a test must never give Repair a login service to stop"
    # From outside this repo, as a person runs it, so its own sf_housing can
    # never answer for the release's.
    return subprocess.run(
        [str(app_root / "tools" / "repair.sh")],
        capture_output=True,
        text=True,
        cwd=app_root.parent,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(app_root.parent),
            "SF_HOUSING_APP_ROOT": str(app_root),
            "SF_HOUSING_RELEASE_ROOT": str(release),
            **extra_env,
        },
    )


@pytest.mark.skipif(sys.platform == "win32", reason="the shell tools are macOS")
def test_repair_puts_the_last_good_backup_back(tmp_path: pathlib.Path) -> None:
    """The unreadable-database message and the service log both promise that
    Repair restores the most recent backup. Repair only ever made one: a reader
    who followed the instruction got the same dead app and no way to find out
    why."""
    app_root, release = repair_install(tmp_path)
    live = app_root / "data" / "housing.sqlite3"
    good = Repository(tmp_path / "good.sqlite3")
    good.initialize()
    good.upsert_listing(room("saved-home"), ScoreResult(90, ["Fits"], "", {}))
    (app_root / "backups").mkdir()
    backup = app_root / "backups" / "housing-20260101-000000.sqlite3"
    backup.write_bytes((tmp_path / "good.sqlite3").read_bytes())
    live.write_bytes(b"this is not a database")

    result = run_repair(app_root, release)

    assert result.returncode == 0, result.stderr
    restored = Repository(live)
    restored.initialize()
    assert [c.source_id for _, c in restored.all_candidates()] == ["saved-home"]
    assert list((app_root / "backups").glob("housing-unreadable-*.sqlite3")), (
        "the unreadable file is kept as evidence, never deleted"
    )


# What Repair was up to 0.5.6, and still is in the tools/ of every Mac that
# installed one of those: a copy by ``cp``, every backup older than thirty days
# deleted, nothing restored, then the reinstall.
OLDER_REPAIR = """#!/bin/bash
set -u
APP_ROOT="${SF_HOUSING_APP_ROOT:-$HOME/Library/Application Support/SF Housing Monitor}"
find "$APP_ROOT/backups" -name 'housing-*.sqlite3' -mtime +30 -delete
"$SF_HOUSING_RELEASE_ROOT/payload/install.sh" "$SF_HOUSING_RELEASE_ROOT"
"""


@pytest.mark.skipif(sys.platform == "win32", reason="the shell tools are macOS")
def test_the_repair_a_new_release_ships_is_the_one_that_runs_on_a_mac_an_older_one_installed(
    tmp_path: pathlib.Path,
) -> None:
    """Pre-launch audit: double-clicking Repair in the new release folder ran
    whatever repair.sh the installed app had in tools/ -- on a Mac that ever
    installed 0.5.6 or earlier, the script that restores nothing and deletes
    every backup older than thirty days. The person whose board would not open
    followed the instructions and lost the copy that held their homes."""
    import os
    import shutil
    import time

    app_root, release = repair_install(tmp_path)
    (app_root / "tools" / "repair.sh").write_text(OLDER_REPAIR, encoding="utf-8")
    (app_root / "tools" / "repair.sh").chmod(0o755)
    (release / "payload" / "tools").mkdir()
    shutil.copy(REPO_ROOT / "release_assets" / "payload" / "tools" / "repair.sh", release / "payload" / "tools")
    wrapper = release / "Repair SF Home Finder.command"
    shutil.copy(REPO_ROOT / "release_assets" / "Repair SF Home Finder.command", wrapper)
    wrapper.chmod(0o755)
    (release / "payload" / "tools" / "repair.sh").chmod(0o755)
    good = Repository(tmp_path / "good.sqlite3")
    good.initialize()
    good.upsert_listing(room("saved-home"), ScoreResult(90, ["Fits"], "", {}))
    (app_root / "backups").mkdir()
    backup = app_root / "backups" / "housing-20260801-000000.sqlite3"
    backup.write_bytes((tmp_path / "good.sqlite3").read_bytes())
    six_weeks_ago = time.time() - 45 * 86400
    os.utime(backup, (six_weeks_ago, six_weeks_ago))
    live = app_root / "data" / "housing.sqlite3"
    live.write_bytes(b"this is not a database")
    plist = app_root.parent / "Library" / "LaunchAgents" / "com.sfhousing.monitor.plist"
    assert not plist.exists(), "a test must never give Repair a login service to stop"

    import subprocess

    result = subprocess.run(
        [str(wrapper)],
        capture_output=True,
        text=True,
        cwd=release,
        env={"PATH": "/usr/bin:/bin", "HOME": str(app_root.parent), "SF_HOUSING_APP_ROOT": str(app_root)},
    )

    assert result.returncode == 0, result.stderr
    assert backup.exists(), "Repair deleted the only good backup"
    restored = Repository(live)
    restored.initialize()
    assert [c.source_id for _, c in restored.all_candidates()] == ["saved-home"]


@pytest.mark.skipif(sys.platform == "win32", reason="the shell tools are macOS")
def test_repair_leaves_a_working_database_alone(tmp_path: pathlib.Path) -> None:
    """Repair is what people run when anything at all is wrong. Restoring a
    backup over a healthy database would throw away every home found since it."""
    app_root, release = repair_install(tmp_path)
    live = app_root / "data" / "housing.sqlite3"
    repository = Repository(live)
    repository.initialize()
    repository.upsert_listing(room("found-today"), ScoreResult(90, ["Fits"], "", {}))
    (app_root / "backups").mkdir()
    stale = Repository(tmp_path / "stale.sqlite3")
    stale.initialize()
    (app_root / "backups" / "housing-20260101-000000.sqlite3").write_bytes(
        (tmp_path / "stale.sqlite3").read_bytes()
    )

    result = run_repair(app_root, release)

    assert result.returncode == 0, result.stderr
    assert [c.source_id for _, c in Repository(live).all_candidates()] == ["found-today"]


@pytest.mark.skipif(sys.platform == "win32", reason="the shell tools are macOS")
def test_repair_never_prunes_away_the_only_good_backup(tmp_path: pathlib.Path) -> None:
    """Backups were deleted by age. A database that went bad more than a month
    after the last upgrade meant Repair deleted the last readable copy of the
    user's homes and put a copy of the corrupt one in its place."""
    import os
    import time

    app_root, release = repair_install(tmp_path)
    live = app_root / "data" / "housing.sqlite3"
    good = Repository(tmp_path / "good.sqlite3")
    good.initialize()
    good.upsert_listing(room("saved-home"), ScoreResult(90, ["Fits"], "", {}))
    (app_root / "backups").mkdir()
    backup = app_root / "backups" / "housing-20260101-000000.sqlite3"
    backup.write_bytes((tmp_path / "good.sqlite3").read_bytes())
    ancient = time.time() - 90 * 86400
    os.utime(backup, (ancient, ancient))
    live.write_bytes(b"this is not a database")

    run_repair(app_root, release)

    assert backup.exists(), "the only readable copy of the user's homes was deleted"


@pytest.mark.skipif(sys.platform == "win32", reason="the shell tools are macOS")
def test_open_waits_as_long_for_a_first_answer_as_the_installer_does() -> None:
    """A first start re-ranks every stored home before it serves anything: 42
    seconds on a board of 8,583, and it grows with the board. Open waited 30
    and then told the reader the dashboard did not start and to run Repair,
    which restarts it and starts the same wait over."""
    tools = REPO_ROOT / "release_assets" / "payload" / "tools"
    opener = (tools / "open.sh").read_text(encoding="utf-8")
    installer = (REPO_ROOT / "release_assets" / "payload" / "install.sh").read_text(encoding="utf-8")

    def window(script: str) -> int:
        import re

        # Open counts attempts; the installer names its allowance, because it
        # waits inside a helper that draws a spinner rather than in a bare
        # loop. Both are seconds, and both have to be found: a pattern that
        # matches neither would let this pass by comparing nothing.
        found = [
            int(match)
            for pattern in (r"/usr/bin/seq 1 (\d+)", r"FIRST_ANSWER_SECONDS=(\d+)")
            for match in re.findall(pattern, script)
        ]
        assert found, "no wait window found in this script"
        return max(found)

    assert window(opener) >= window(installer), "Open gives up before the app is up"
    assert "did not start" not in opener, "a slow start is not a failed start"


@pytest.mark.skipif(sys.platform == "win32", reason="the shell tools are macOS")
def test_a_restored_board_carries_its_own_scoring_mark(tmp_path: pathlib.Path) -> None:
    """Restoring a backup must not inherit the live board's claim about its scores.

    The mark that says "already scored by this code with these answers" lives
    inside the database, in the same transaction as the scores. That is what
    makes a restore safe without Repair having to remember anything: the
    backup brings whatever mark it was taken with, so a backup from before a
    change of deal cannot present itself as current.
    """
    live = Repository(tmp_path / "live.sqlite3")
    live.initialize()
    with live.connection() as connection:
        live.set_scoring_mark(connection, "the live board's mark")
        connection.commit()

    backup = Repository(tmp_path / "backup.sqlite3")
    backup.initialize()
    with backup.connection() as connection:
        backup.set_scoring_mark(connection, "an older mark, from an older deal")
        connection.commit()

    # What Repair does: the backup's bytes become the live file.
    (tmp_path / "live.sqlite3").write_bytes((tmp_path / "backup.sqlite3").read_bytes())

    assert Repository(tmp_path / "live.sqlite3").scoring_mark() == (
        "an older mark, from an older deal"
    ), "the restored board answered with the mark of the board it replaced"


def test_a_board_older_than_the_mark_asks_to_be_scored(tmp_path: pathlib.Path) -> None:
    """A database from before this table existed knows nothing, so it is owed a pass."""
    old_board = tmp_path / "old.sqlite3"
    connection = sqlite3.connect(old_board)
    connection.execute("CREATE TABLE listings (id INTEGER PRIMARY KEY)")
    connection.commit()
    connection.close()

    assert Repository(old_board).scoring_mark() is None



def _damaged(path: pathlib.Path) -> None:
    path.write_bytes(b"this is not a database")


def _good_backup(app_root: pathlib.Path, stamp: str, source_id: str) -> pathlib.Path:
    folder = app_root / "backups"
    folder.mkdir(exist_ok=True)
    board = Repository(folder.parent / f"staging-{stamp}.sqlite3")
    board.initialize()
    board.upsert_listing(room(source_id), ScoreResult(90, ["Fits"], "", {}))
    target = folder / f"housing-{stamp}.sqlite3"
    target.write_bytes(board.path.read_bytes())
    board.path.unlink()
    return target


@pytest.mark.skipif(sys.platform == "win32", reason="the shell tools are macOS")
def test_repair_clicked_thirty_times_on_a_board_that_keeps_breaking_still_restores(
    tmp_path: pathlib.Path,
) -> None:
    """The regression that motivated rebuilding Repair, through the script itself.

    Every Repair snapshots the live file, damaged or not. The old rule kept the
    twenty newest copies of any kind, so a board that kept breaking filled the
    folder with copies of its own damage and pruned the one good backup before
    the restore went looking. The thirtieth click must restore it like the first.
    """
    app_root, release = repair_install(tmp_path)
    live = app_root / "data" / "housing.sqlite3"
    good = _good_backup(app_root, "20260101-000000", "the-home-from-before")

    for click in range(30):
        _damaged(live)
        result = run_repair(app_root, release)
        assert result.returncode == 0, f"click {click + 1}: {result.stdout}{result.stderr}"
        assert [c.source_id for _, c in Repository(live).all_candidates()] == ["the-home-from-before"], (
            f"click {click + 1} did not put the homes back"
        )
    assert good.exists(), "the only good backup was pruned"


@pytest.mark.skipif(sys.platform == "win32", reason="the shell tools are macOS")
def test_repair_keeps_the_backups_folder_bounded(tmp_path: pathlib.Path) -> None:
    from sf_housing.backups import KEEP_GOOD

    app_root, release = repair_install(tmp_path)
    live = app_root / "data" / "housing.sqlite3"
    board = Repository(live)
    board.initialize()
    board.upsert_listing(room("today"), ScoreResult(90, ["Fits"], "", {}))
    for day in range(1, 29):
        _good_backup(app_root, f"202608{day:02d}-120000", f"home-{day}")

    assert run_repair(app_root, release).returncode == 0
    assert len(list((app_root / "backups").glob("housing-*.sqlite3"))) == KEEP_GOOD


@pytest.mark.skipif(sys.platform == "win32", reason="the shell tools are macOS")
def test_repair_applies_this_release_s_rules_not_the_installed_app_s(tmp_path: pathlib.Path) -> None:
    """Repairing an older install must run the rules in the release being run.

    0.5.6 ships no ``sf_housing.backups`` at all, so a Repair that imported the
    installed package would fail on exactly the machines that need it. The
    installed runtime here is a real virtual environment holding an
    ``sf_housing`` without that module -- a faithful 0.5.6 -- so only the
    release's wheel can supply the rules.
    """
    import subprocess

    app_root, release = repair_install(tmp_path)
    current = app_root / "current"
    (current / "bin" / "python").unlink()
    (current / "bin").rmdir()
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(current)], check=True)
    site = next(current.glob("lib/python*/site-packages"))
    (site / "sf_housing").mkdir()
    (site / "sf_housing" / "__init__.py").write_text('__version__ = "0.5.6"\n', encoding="utf-8")
    # From outside this repo, so its own sf_housing cannot answer instead.
    old = subprocess.run(
        [str(current / "bin" / "python"), "-c", "import sf_housing.backups"],
        capture_output=True, text=True, cwd=tmp_path,
    )
    assert old.returncode != 0, "the fixture must look like 0.5.6, with no backups module installed"

    live = app_root / "data" / "housing.sqlite3"
    _good_backup(app_root, "20260901-000000", "saved-home")
    _damaged(live)

    result = run_repair(app_root, release)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Restored the backup" in result.stdout, result.stdout
    assert [c.source_id for _, c in Repository(live).all_candidates()] == ["saved-home"]


@pytest.mark.skipif(sys.platform == "win32", reason="the shell tools are macOS")
def test_repair_is_not_thrown_by_a_python_setting_in_the_environment(tmp_path: pathlib.Path) -> None:
    """A PYTHONPATH left by other software reaches every Python that reads the
    environment: here its ``sitecustomize`` ends any such Python at start-up,
    and its ``sf_housing.backups`` would claim success without doing anything.
    Repair runs its Pythons isolated, so neither gets a say."""
    app_root, release = repair_install(tmp_path)
    decoy = tmp_path / "decoy"
    (decoy / "sf_housing").mkdir(parents=True)
    (decoy / "sitecustomize.py").write_text("raise SystemExit(99)\n", encoding="utf-8")
    (decoy / "sf_housing" / "__init__.py").write_text("", encoding="utf-8")
    (decoy / "sf_housing" / "backups.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    live = app_root / "data" / "housing.sqlite3"
    _good_backup(app_root, "20260901-000000", "saved-home")
    _damaged(live)

    result = run_repair(app_root, release, PYTHONPATH=str(decoy))

    assert result.returncode == 0, result.stdout + result.stderr
    assert [c.source_id for _, c in Repository(live).all_candidates()] == ["saved-home"]


@pytest.mark.skipif(sys.platform == "win32", reason="the shell tools are macOS")
def test_repair_says_so_when_checking_the_data_fails(tmp_path: pathlib.Path) -> None:
    """A crash in the rules is not "your data is fine". It is said, the data is
    left alone, and the app files are still repaired."""
    import zipfile

    app_root, release = repair_install(tmp_path)
    wheel = next((release / "payload").glob("sf_home_finder-*.whl"))
    with zipfile.ZipFile(wheel, "w") as broken:
        broken.writestr("sf_housing/__init__.py", "")
        broken.writestr("sf_housing/backups.py", "def main(argv):\n    raise RuntimeError('boom')\n")
    live = app_root / "data" / "housing.sqlite3"
    _damaged(live)
    before = live.read_bytes()

    result = run_repair(app_root, release)

    assert "stopped with an error" in result.stdout, result.stdout + result.stderr
    assert live.read_bytes() == before
    assert "stub installer ran" in result.stdout


def test_repair_stops_the_app_before_it_touches_the_data() -> None:
    """A restore replaces the database file, and a running app could go on
    writing to the one being replaced. launchctl cannot be run from a test
    without reaching the real service, so this half is read: the login service
    is stopped inside the step that waits for the process, that step comes
    before the rules run, and every way out starts the service again."""
    script = (REPO_ROOT / "release_assets" / "payload" / "tools" / "repair.sh").read_text(encoding="utf-8")
    stop = script[script.index("stop_app() {"):]
    assert stop.index("service stop") < stop.index("app_running"), "it does not wait after asking launchd"
    body = script[script.index('if [ -f "$APP_ROOT/data/housing.sqlite3" ]; then'):]
    assert body.index("stop_app") < body.index("sf_housing.backups import main"), "the data is touched with the app running"
    no_copy = body[body.index('if [ "$STATUS" -eq 2 ]; then'):]
    assert no_copy.index("service start") < no_copy.index("exit 2"), "a Repair that stops early leaves the app off"
    failed = body[body.index('if ! "$INSTALLER"'):]
    assert failed.index("service start") < failed.index("exit 1"), "a failed reinstall leaves the app off"


@pytest.mark.skipif(sys.platform == "win32", reason="the shell tools are macOS")
def test_repair_waits_for_the_app_itself_to_stop_before_touching_the_data(tmp_path: pathlib.Path) -> None:
    """``launchctl bootout`` can return before the process is gone, and an app
    started some other way has no login service to stop at all. Here the app
    is a stand-in with the real app's command line; it notes what the database
    looked like at the moment it was told to stop, which must be before the
    restore -- and it must be gone by the time the data is touched."""
    import hashlib
    import subprocess
    import textwrap
    import time

    app_root, release = repair_install(tmp_path)
    live = app_root / "data" / "housing.sqlite3"
    _good_backup(app_root, "20260901-000000", "saved-home")
    _damaged(live)
    damaged = hashlib.sha256(live.read_bytes()).hexdigest()
    seen, ready = tmp_path / "seen-at-stop", tmp_path / "ready"
    stand_in = tmp_path / "stand_in.py"
    stand_in.write_text(textwrap.dedent(f"""
        import hashlib, pathlib, signal, sys, time
        def stop(*_):
            data = pathlib.Path({str(live)!r}).read_bytes()
            pathlib.Path({str(seen)!r}).write_text(hashlib.sha256(data).hexdigest())
            sys.exit(0)
        signal.signal(signal.SIGTERM, stop)
        pathlib.Path({str(ready)!r}).write_text("up")
        while True:
            time.sleep(0.1)
    """), encoding="utf-8")
    command = f'{app_root}/current/bin/python -m sf_housing serve'
    app = subprocess.Popen(["/bin/bash", "-c", f'exec -a "{command}" "{sys.executable}" "{stand_in}"'])
    try:
        for _ in range(100):
            if ready.exists():
                break
            time.sleep(0.05)
        assert ready.exists(), "the stand-in never started"

        result = run_repair(app_root, release)

        assert app.wait(timeout=10) == 0, "the app was not stopped"
    finally:
        if app.poll() is None:
            app.kill()
    assert seen.read_text() == damaged, "the data was changed before the app had stopped"
    assert result.returncode == 0, result.stdout + result.stderr
    assert [c.source_id for _, c in Repository(live).all_candidates()] == ["saved-home"]


@pytest.mark.skipif(sys.platform == "win32", reason="the shell tools are macOS")
def test_repair_with_no_way_to_run_its_rules_leaves_the_data_exactly_as_it_is(
    tmp_path: pathlib.Path,
) -> None:
    """Failing toward the data: nothing cruder is let near it instead."""
    app_root, release = repair_install(tmp_path)
    (app_root / "current" / "bin" / "python").unlink()
    live = app_root / "data" / "housing.sqlite3"
    _good_backup(app_root, "20260901-000000", "saved-home")
    _damaged(live)
    before = live.read_bytes()

    result = run_repair(app_root, release)

    assert live.read_bytes() == before, "the database was changed with no way to check it"
    assert "left exactly as it is" in result.stdout
    assert "stub installer ran" in result.stdout, "the app files were not repaired either"


@pytest.mark.skipif(sys.platform == "win32", reason="the shell tools are macOS")
def test_repair_that_cannot_take_a_safety_copy_goes_no_further(tmp_path: pathlib.Path) -> None:
    """No copy, no restore and no reinstall -- only the message, and nothing changed."""
    app_root, release = repair_install(tmp_path)
    live = app_root / "data" / "housing.sqlite3"
    board = Repository(live)
    board.initialize()
    board.upsert_listing(room("today"), ScoreResult(90, ["Fits"], "", {}))
    (app_root / "backups").write_text("a file where the backups folder should be")
    before = live.read_bytes()

    result = run_repair(app_root, release)

    assert result.returncode == 2, result.stdout + result.stderr
    assert "Nothing was changed" in result.stdout
    assert "stub installer ran" not in result.stdout, "it went on to reinstall with no copy taken"
    assert live.read_bytes() == before


@pytest.mark.skipif(sys.platform == "win32", reason="the shell tools are macOS")
def test_repair_on_a_healthy_board_keeps_what_is_still_in_its_write_ahead_log(
    tmp_path: pathlib.Path,
) -> None:
    """A snapshot taken by Repair must include writes not yet checkpointed."""
    import sqlite3

    app_root, release = repair_install(tmp_path)
    live = app_root / "data" / "housing.sqlite3"
    board = Repository(live)
    board.initialize()
    board.upsert_listing(room("checkpointed"), ScoreResult(90, ["Fits"], "", {}))
    writer = sqlite3.connect(live)
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("UPDATE listings SET note = 'written a moment ago'")
    writer.commit()
    try:
        assert run_repair(app_root, release).returncode == 0
    finally:
        writer.close()

    snapshots = sorted((app_root / "backups").glob("housing-*.sqlite3"))
    copy = sqlite3.connect(snapshots[-1])
    try:
        notes = [row[0] for row in copy.execute("SELECT note FROM listings")]
    finally:
        copy.close()
    assert notes == ["written a moment ago"], "the safety copy lost what was only in the log"
