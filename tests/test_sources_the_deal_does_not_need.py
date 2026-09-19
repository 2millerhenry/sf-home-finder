"""A source the deal cannot use is not asked, and says so rather than "no matches".

SpareRoom lists private rooms and nothing else. For a deal with no private room
in it, its search answered with an empty list without asking anybody, and every
scan then recorded a successful check that had found nothing: the dashboard read
"No matches", Support read "working, no matches -- a valid result, not a
failure", and the Ready Check counted it among the sources that work. The
author's own board holds sixty-three of those runs, from 4 September on, and not
one request behind any of them.

Abacus did the same for a deal of rooms only, and wrote its "skipped" message
over the one it gives for a real empty board -- and never put it back.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from sf_housing.app import create_app
from sf_housing.database import Repository
from sf_housing.diagnostics import _source_freshness_checks
from sf_housing.freshness import evaluate_source_freshness
from sf_housing.scanner import Scanner
from sf_housing.settings import Settings
from sf_housing.sources import AbacusSource, SpareRoomSource
from tests.test_sources import ABACUS_HTML, SPAREROOM_HTML, client_for
from tests.test_zillow import profile


NO_VACANCIES = (
    '<div class="listings__no-vacancies">There are no available properties at this time.</div>'
)


class SpareRoomHere(SpareRoomSource):
    """The real SpareRoom reader, answered by a page held here rather than the site."""

    def __init__(self) -> None:
        self.asked = 0

    def search(self, client, preferences):
        self.asked += 1
        with client_for(SPAREROOM_HTML) as local:
            return super().search(local, preferences)


class AbacusHere(AbacusSource):
    """The real Abacus reader, answered by a page held here rather than the site."""

    def __init__(self, page: str) -> None:
        self.page = page
        self.asked = 0

    def search(self, client, preferences):
        self.asked += 1
        with client_for(self.page) as local:
            return super().search(local, preferences)


def latest_run(repository: Repository, platform: str) -> dict:
    return next(run for run in repository.latest_source_runs() if run["platform"] == platform)


def test_spareroom_is_not_asked_for_a_deal_with_no_private_room(repository: Repository) -> None:
    source = SpareRoomHere()
    scanner = Scanner(repository, lambda: profile("one_bedroom"), [source], detail_delay_seconds=0)

    outcome = scanner.run_scan("scheduled")

    assert source.asked == 0, "SpareRoom was asked for a deal that cannot use it"
    assert outcome.sources_failed == 0
    run = latest_run(repository, "SpareRoom")
    assert run["status"] == "not_needed"
    assert run["message"] == (
        "Private rooms are not in Your deal, and SpareRoom lists nothing else, so it was not asked."
    )

    health = evaluate_source_freshness(repository, source)
    assert health.status == "not_needed"
    assert health.label == "SpareRoom is not needed for your deal"
    assert health.short_label == "Not needed for your deal"
    assert health.explanation == run["message"]
    assert "valid result" not in health.explanation
    assert health.listings_seen == 0
    assert health.needs_attention is False

    (check,) = _source_freshness_checks(repository, [source], datetime.now(UTC))
    assert check.status == "not_applicable", "counted among the sources that work"

    scan = repository.recent_scans(1)[0]
    assert scan["sources_skipped"] == 0, "the check did not run out of time for it"
    assert scan["sources_reached"] == 0, "no site was asked anything"


def test_spareroom_is_read_as_before_once_the_deal_has_a_private_room(
    repository: Repository,
) -> None:
    """The skip belongs to the deal and not to the source: with a private room
    in the deal SpareRoom is read as it always was, and says what it found."""
    deal = {"now": profile("one_bedroom")}
    source = SpareRoomHere()
    scanner = Scanner(repository, lambda: deal["now"], [source], detail_delay_seconds=0)
    scanner.run_scan("scheduled")
    deal["now"] = profile("private_room")

    scanner.run_scan("scheduled")

    assert source.asked == 1
    run = latest_run(repository, "SpareRoom")
    assert (run["status"], run["listings_seen"]) == ("success", 1)
    assert evaluate_source_freshness(repository, source).status == "working"


def test_abacus_is_not_asked_for_a_deal_of_rooms_only(repository: Repository) -> None:
    source = AbacusHere(ABACUS_HTML)
    scanner = Scanner(repository, lambda: profile("private_room"), [source], detail_delay_seconds=0)

    scanner.run_scan("scheduled")

    assert source.asked == 0
    run = latest_run(repository, "Abacus (small buildings)")
    assert run["status"] == "not_needed"
    assert run["message"].startswith("Whole homes are not in Your deal")


def test_abacus_having_nothing_to_let_is_never_reported_as_a_skip(repository: Repository) -> None:
    """The skip was written over the source's own empty-board message and never
    put back, so after one rooms-only deal a real empty board read "Skipped
    because entire homes are not enabled in Your deal" -- about a deal that had
    them, on a check that had really been made."""
    deal = {"now": profile("private_room")}
    source = AbacusHere(NO_VACANCIES)
    scanner = Scanner(repository, lambda: deal["now"], [source], detail_delay_seconds=0)
    scanner.run_scan("scheduled")
    deal["now"] = profile("two_bedroom")

    scanner.run_scan("scheduled")

    run = latest_run(repository, "Abacus (small buildings)")
    assert (run["status"], run["message"]) == (
        "success",
        "Abacus currently reports no available rental properties.",
    )
    assert evaluate_source_freshness(repository, source).status == "working_zero"


def test_a_source_that_cannot_answer_whether_it_is_needed_is_read_as_before(
    repository: Repository,
) -> None:
    """The question is new, and a source that answers it badly must cost no
    more than a source that was never asked: it is read, and the scan goes on."""

    class Confused(SpareRoomHere):
        def not_needed_for(self, preferences):
            raise KeyError("enabled_paths")

    source = Confused()
    scanner = Scanner(repository, lambda: profile("private_room"), [source], detail_delay_seconds=0)

    outcome = scanner.run_scan("scheduled")

    assert source.asked == 1
    assert outcome.sources_failed == 0
    assert latest_run(repository, "SpareRoom")["status"] == "success"


def test_asking_a_source_whether_the_deal_needs_it_changes_nothing_about_it() -> None:
    """The answer used to be kept on the source, which is how it outlived the
    deal it was about. Asked of either, for any deal, it leaves nothing behind."""
    for source in (SpareRoomSource(), AbacusSource()):
        for deal in (profile("private_room"), profile("one_bedroom"), profile("studio", "private_room")):
            source.not_needed_for(deal)
        assert vars(source) == {}, vars(source)


def test_the_phantom_checks_already_on_a_board_are_refiled_as_what_they_were(tmp_path: Path) -> None:
    """Every board the old code ran on holds these rows -- SpareRoom "checked"
    twice a day and found nothing, some of them rechecking homes they never
    read. Left alone, the newest would be quoted as SpareRoom's last good
    result the first time it failed after the deal gained a private room.
    A real empty board, on the same source or another, is left exactly alone."""
    from sf_housing.freshness import source_key

    path = tmp_path / "housing.sqlite3"
    repository = Repository(path)
    repository.initialize()
    written = [
        # A real check, older than the phantoms, and one that happens to
        # mention the deal: only the exact words the skip used are refiled.
        (SpareRoomSource(), "No rooms were listed in Your deal."),
        (SpareRoomSource(), "Skipped because private rooms are not enabled in Your deal."),
        (
            SpareRoomSource(),
            "Skipped because private rooms are not enabled in Your deal. "
            "Rechecked 1 home(s) this search no longer lists.",
        ),
        (AbacusSource(), "Skipped because entire homes are not enabled in Your deal."),
        (AbacusSource(), "Abacus currently reports no available rental properties."),
    ]
    scan = repository.begin_scan("scheduled")
    for source, message in written:
        run = repository.begin_source_run(
            scan, source.platform, source.search_url, source_key=source_key(source)
        )
        repository.finish_source_run(run, "success", seen=0, message=message)
    repository.finish_scan(scan, "completed")

    for _ in range(2):  # the next start, and the one after it
        Repository(path).initialize()

    with repository.connection() as connection:
        rows = [
            (row["platform"], row["status"], row["message"])
            for row in connection.execute("SELECT * FROM source_runs ORDER BY id")
        ]
    assert rows == [
        ("SpareRoom", "success", "No rooms were listed in Your deal."),
        ("SpareRoom", "not_needed", "Skipped because private rooms are not enabled in Your deal."),
        ("SpareRoom", "not_needed", "Skipped because private rooms are not enabled in Your deal."),
        ("Abacus (small buildings)", "not_needed", "Skipped because entire homes are not enabled in Your deal."),
        ("Abacus (small buildings)", "success", "Abacus currently reports no available rental properties."),
    ]
    spareroom = SpareRoomSource()
    last_good = repository.last_successful_source_run(source_key=source_key(spareroom), platform="SpareRoom")
    assert last_good is not None and last_good["message"] == "No rooms were listed in Your deal.", (
        "a phantom check is still standing in as SpareRoom's last good result"
    )
    assert evaluate_source_freshness(repository, spareroom).status == "not_needed"


def one_bedroom_settings(tmp_path: Path) -> Settings:
    data = tmp_path / "data"
    settings = Settings(
        data_dir=data,
        preferences_path=data / "config" / "preferences.yaml",
        database_path=data / "housing.sqlite3",
        log_path=data / "test.log",
    )
    settings.preferences_path.parent.mkdir(parents=True)
    settings.preferences_path.write_text(
        yaml.safe_dump(
            {
                "profile_version": 1,
                "profile": {
                    "state": "active",
                    "enabled_paths": ["one_bedroom"],
                    "budgets": {"one_bedroom": {"maximum_monthly": 3000, "minimum_monthly": 100}},
                    "geography": {"anywhere_in_sf": True},
                },
            }
        ),
        encoding="utf-8",
    )
    return settings


def test_the_pages_say_not_needed_where_they_said_no_matches(tmp_path: Path) -> None:
    """Both pages the reader checks a source on, rendered after a real scan
    with the deal the author has: one-bedrooms, no private room."""
    application = create_app(
        settings=one_bedroom_settings(tmp_path), sources=[SpareRoomHere()], enable_scheduler=False
    )
    application.state.scanner.run_scan("scheduled")

    with TestClient(application) as client:
        dashboard = client.get("/")
        support = client.get("/support")

    assert dashboard.status_code == 200 and support.status_code == 200
    roster = dashboard.text[dashboard.text.index('class="source-roster"') :]
    roster = roster[: roster.index("</ul>")]
    assert "SpareRoom" in roster and "Not needed for your deal" in roster, roster
    assert "No matches" not in roster

    page = support.text
    working = page[page.index("Nothing to do about these") : page.index("None of these stop the app working")]
    optional = page[page.index("None of these stop the app working") :]
    assert "SpareRoom" not in working, "still counted among the sources that work"
    assert "SpareRoom is not needed for your deal" in optional
    assert "valid result" not in page
