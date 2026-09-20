"""WO-5 item 3: what held the shortlist's homes back is worked out once per
change to the listings, not once per page.

``exclusion_summary`` -- the "N more homes were collected and held back by
your deal" lines under the shortlist -- was 54% of the dashboard's render time
on a real board: 764 ms a page, 352 ms with it stubbed out. Every load read
every held-back home's stored verdict again, about 4,360 ``json.loads`` over
3,789 rows and 5.3 MB of JSON per request, to print what the load before had
printed. It, the filters' choices and whether a tab holds anything are now
remembered until a listing changes, and what says one changed is a counter the
board keeps itself, by triggers, so a scan in another process moves it too.

Serving an answer the listings no longer give is the failure that matters. So
every way a listing changes is tried here, and every remembered answer is held
to one worked out by a Repository that has never been asked anything.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import threading
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from operator import methodcaller
from pathlib import Path
from typing import Callable

import pytest
from fastapi.testclient import TestClient

from sf_housing import database
from sf_housing.app import _rent_table, create_app
from sf_housing.database import Repository
from sf_housing.models import ListingCandidate, ScoreResult
from sf_housing.rent_estimate import WINDOW_DAYS, RentTable
from sf_housing.settings import Settings
from tests.conftest import TEST_PREFERENCES

URLS = {
    "Zillow": "https://www.zillow.com/homedetails/x/{}_zpid/",
    "Movoto": "https://www.movoto.com/san-francisco-ca/{}/for-rent/",
    "Redfin": "https://www.redfin.com/CA/San-Francisco/x/unit-1/home/{}",
}
SUMMARIES = {
    "room": "Private room in a shared flat.",
    "studio": "Entire studio apartment.",
    "one_bedroom": "Entire one bedroom apartment.",
    None: "Entire apartment, whole place to yourself.",
}


def listing(platform: str, slug: str, address: str, *, neighborhood: str = "Mission", price: int | None = 1900,
            housing_kind: str = "whole_unit", unit_type: str | None = "one_bedroom") -> ListingCandidate:
    return ListingCandidate(
        platform=platform, source_id=slug, title=address, original_url=URLS[platform].format(slug),
        price=price, neighborhood=neighborhood, listing_type="Apartment",
        summary=SUMMARIES["room" if housing_kind == "room" else unit_type],
        metadata={"address": address}, housing_kind=housing_kind, unit_type=unit_type,
    )


def verdict(score: int, *, failing: str | None = None, reason: str = "") -> ScoreResult:
    """A stored verdict rather than a scored one, so each board is exactly what
    its test says. ``failing`` names the hard check that rules the home out,
    kept the way scoring keeps it: the JSON the summary has to read."""
    if failing is None:
        return ScoreResult(score, ["Stored"], "", {}, eligibility="eligible")
    check = {"status": "fail", "check": failing, "reason": reason}
    return ScoreResult(
        score, ["Stored"], "", {"hard_constraints": [check]}, eligibility="ineligible", eligibility_reasons=[reason]
    )


def put(repository: Repository, home: ListingCandidate, result: ScoreResult) -> int:
    listing_id, _ = repository.upsert_listing(home, result)
    return listing_id


def days_ago(days: float) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")


def another_program(repository: Repository, sql: str, *parameters: object) -> None:
    """Write as a scan run from the command line would: its own connection to
    the file, knowing nothing of this process or of what it remembers."""
    connection = sqlite3.connect(repository.path)
    try:
        connection.execute(sql, parameters)
        connection.commit()
    finally:
        connection.close()


# The question the test deal's one-bedroom tab asks: its cut-off, its tab.
ONE_BEDROOM_TAB = (60, "whole_unit", ("one_bedroom",))
# What the test deal's own tabs cover, for the tab that holds everything else.
DEAL_TABS = (True, ("studio", "one_bedroom", "two_bedroom", "three_bedroom"))


def thin_shortlist(repository: Repository) -> dict[str, int]:
    """Two one-bedrooms on the shortlist and four the deal held back: one below
    the cut-off, two outside its areas and one over its budget."""
    return {
        "mission": put(repository, listing("Zillow", "1", "10 Oak St #1"), verdict(85)),
        "nopa": put(repository, listing("Movoto", "m2", "11 Oak St #1", neighborhood="NOPA"), verdict(80)),
        "low": put(repository, listing("Zillow", "3", "12 Oak St #1", neighborhood="Bernal Heights"), verdict(40)),
        "tenderloin": put(
            repository, listing("Zillow", "4", "13 Oak St #1", neighborhood="Tenderloin"),
            verdict(45, failing="area", reason="Tenderloin is outside your target neighborhoods."),
        ),
        "sunset": put(
            repository, listing("Zillow", "5", "14 Oak St #1", neighborhood="Outer Sunset"),
            verdict(45, failing="area", reason="Outer Sunset is outside your target neighborhoods."),
        ),
        "dear": put(
            repository, listing("Zillow", "6", "15 Oak St #1", price=3200),
            verdict(49, failing="price", reason="The monthly price exceeds this path's maximum."),
        ),
    }


# What the one-bedroom tab says ``thin_shortlist`` held back.
HELD_BACK = [
    {"reason": "outside your areas", "count": 2},
    {"reason": "below your 60 match cut-off", "count": 1},
    {"reason": "over your budget", "count": 1},
]

# Everything the dashboard asks the board that is now remembered: what the
# tab held back and why, the filters' choices on the shortlist and in the
# Archive, and whether the starred list, the passed list and the tab for
# every other home hold anything.
QUESTIONS = (
    methodcaller("exclusion_summary", *ONE_BEDROOM_TAB),
    methodcaller("filter_options", *ONE_BEDROOM_TAB, DEAL_TABS),
    # The Archive's choices, which is what this has always meant to ask. It
    # used to say so by passing a cut-off of zero, back when that was also
    # what dropped the status filter; the tab is now named, so the question
    # survives the choices being drawn from the view.
    methodcaller("filter_options", 0, "", (), DEAL_TABS, "all"),
    methodcaller("has_homes", 60, "saved", ""),
    methodcaller("has_homes", 60, "dismissed", ""),
    methodcaller("has_homes", 60, "active", "other", DEAL_TABS),
)


def answers(repository: Repository) -> list[object]:
    return [question(repository) for question in QUESTIONS]


def answers_afresh(repository: Repository) -> list[object]:
    """Each answer from its own Repository that has never been asked anything:
    one Repository for all of them could remember a wrong answer too."""
    return [question(Repository(repository.path)) for question in QUESTIONS]


@pytest.fixture
def parsed(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, str]]:
    """Every JSON document parsed from here on, and the thread that parsed it.

    Reading the held-back homes' stored verdicts is the work the summary
    repeated on every page, so counting what is parsed counts that work.
    ``database.json`` is the json module itself: every caller is seen.
    """
    seen: list[tuple[int, str]] = []
    real = json.loads

    def counted(document, *args, **kwargs):
        seen.append((threading.get_ident(), document))
        return real(document, *args, **kwargs)

    monkeypatch.setattr(database.json, "loads", counted)
    return seen


def parsed_here(parsed: list[tuple[int, str]]) -> list[str]:
    """What this thread parsed, so that no other thread's work is counted."""
    here = threading.get_ident()
    return [document for thread, document in parsed if thread == here]


# --------------------------------------------------------------------------
# worked out once, and again when a listing changes
# --------------------------------------------------------------------------


def test_the_shortlist_does_not_recount_what_was_held_back_until_a_listing_changes(
    repository: Repository, parsed: list[tuple[int, str]]
) -> None:
    homes = thin_shortlist(repository)
    assert repository.exclusion_summary(*ONE_BEDROOM_TAB) == HELD_BACK

    parsed.clear()
    assert repository.exclusion_summary(*ONE_BEDROOM_TAB) == HELD_BACK
    assert parsed_here(parsed) == [], "the held-back homes' verdicts were read again with nothing changed"

    repository.set_listing_status(homes["tenderloin"], "dismissed")
    parsed.clear()
    assert repository.exclusion_summary(*ONE_BEDROOM_TAB) == [
        {"reason": "below your 60 match cut-off", "count": 1},
        {"reason": "outside your areas", "count": 1},
        {"reason": "over your budget", "count": 1},
    ]
    assert parsed_here(parsed), "a home was passed and what was held back was not counted again"


@pytest.fixture
def app_and_repository(tmp_path: Path):
    data_dir = tmp_path / "data"
    preferences_path = tmp_path / "preferences.yaml"
    preferences_path.write_text(TEST_PREFERENCES, encoding="utf-8")
    settings = Settings(data_dir=data_dir, preferences_path=preferences_path,
                        database_path=data_dir / "housing.sqlite3", log_path=data_dir / "test.log")
    repository = Repository(settings.database_path)
    repository.initialize()
    return create_app(settings=settings, sources=[], enable_scheduler=False), repository


def held_back_lines(page: str) -> list[str]:
    return re.findall(r"<li><b>\d+</b> [^<]+</li>", page)


def stored_verdicts(repository: Repository, *listing_ids: int) -> set[str]:
    with repository.connection() as connection:
        return {
            str(row[0])
            for row in connection.execute(
                f"SELECT score_details_json FROM listings WHERE id IN ({','.join('?' for _ in listing_ids)})",
                listing_ids,
            )
        }


def test_reloading_the_shortlist_reads_no_held_back_home_again_until_a_listing_changes(
    app_and_repository, parsed: list[tuple[int, str]]
) -> None:
    """The page as a person reloads it: the app's own Repository, answering
    from the one connection a request gets, on the server's worker threads --
    so what is counted is only the held-back homes' own verdicts."""
    application, repository = app_and_repository
    homes = thin_shortlist(repository)
    verdicts = stored_verdicts(repository, homes["tenderloin"], homes["sunset"], homes["dear"])
    tab = "/?housing=one_bedroom&view=active"

    with TestClient(application) as client:
        first = client.get(tab).text
        parsed.clear()
        again = client.get(tab).text
        reread = [document for _, document in parsed if document in verdicts]
        # Through this test's Repository, which to the app's is another writer.
        repository.set_listing_status(homes["tenderloin"], "dismissed")
        parsed.clear()
        after = client.get(tab).text
        recounted = [document for _, document in parsed if document in verdicts]

    assert "<li><b>2</b> outside your areas</li>" in first
    assert held_back_lines(again) == held_back_lines(first)
    assert reread == [], "reloading the page read the held-back homes again"
    assert "<li><b>1</b> outside your areas</li>" in after
    assert recounted, "a home was passed and the page did not count again"


# --------------------------------------------------------------------------
# every way a listing changes is a change
# --------------------------------------------------------------------------


def found_long_ago(repository: Repository, listing_id: int, days: float = 30) -> None:
    another_program(repository, "UPDATE listings SET first_found = ? WHERE id = ?", days_ago(days), listing_id)


def long_gone(repository: Repository) -> int:
    """A home the archive aged out that no site has listed for 200 days: what
    retention exists to delete."""
    gone = put(repository, listing("Redfin", "r9", "30 Oak St #1", neighborhood="Outer Richmond"), verdict(30))
    another_program(
        repository,
        "UPDATE listings SET status = 'dismissed', status_reason = 'aged', first_found = ?, last_seen = ? "
        "WHERE id = ?",
        days_ago(200), days_ago(200), gone,
    )
    # Its site searched since, and the home not among what it returned.
    another_program(
        repository, "INSERT INTO scan_runs (trigger, status, started_at) VALUES ('test', 'completed', ?)", days_ago(0)
    )
    another_program(
        repository,
        "INSERT INTO source_runs (scan_run_id, platform, status, started_at) "
        "VALUES ((SELECT MAX(id) FROM scan_runs), 'Redfin', 'success', ?)",
        days_ago(0),
    )
    return gone


# Each sets up what it needs on ``thin_shortlist`` and returns the change.


def a_new_listing_arrives(repository: Repository, homes: dict[str, int]) -> Callable[[], object]:
    # Of no stated size, so the tab for every other home now holds something.
    arrival = listing("Redfin", "r7", "16 Oak St #1", neighborhood="Inner Richmond", unit_type=None)
    return lambda: put(repository, arrival, verdict(90))


def a_listing_is_found_again_below_the_cut_off(repository: Repository, homes: dict[str, int]) -> Callable[[], object]:
    return lambda: put(repository, listing("Zillow", "1", "10 Oak St #1"), verdict(40))


def a_home_is_starred(repository: Repository, homes: dict[str, int]) -> Callable[[], object]:
    return lambda: repository.set_listing_status(homes["mission"], "saved")


def a_star_is_taken_off(repository: Repository, homes: dict[str, int]) -> Callable[[], object]:
    repository.set_listing_status(homes["mission"], "saved")
    return lambda: repository.set_listing_status(homes["mission"], "active")


def a_home_is_passed(repository: Repository, homes: dict[str, int]) -> Callable[[], object]:
    return lambda: repository.set_listing_status(homes["mission"], "dismissed")


def a_passed_home_is_restored(repository: Repository, homes: dict[str, int]) -> Callable[[], object]:
    repository.set_listing_status(homes["low"], "dismissed")
    return lambda: repository.set_listing_status(homes["low"], "active")


def a_note_brings_an_aged_out_home_back(repository: Repository, homes: dict[str, int]) -> Callable[[], object]:
    found_long_ago(repository, homes["mission"])
    assert repository.archive_stale_listings() == 1
    return lambda: repository.set_listing_note(homes["mission"], "Worth a call after all")


def a_home_is_rescored(repository: Repository, homes: dict[str, int]) -> Callable[[], object]:
    return lambda: repository.update_score(homes["mission"], verdict(40))


def the_archive_ages_a_home_out(repository: Repository, homes: dict[str, int]) -> Callable[[], object]:
    found_long_ago(repository, homes["mission"])
    return repository.archive_stale_listings


def retention_deletes_a_home_nobody_kept(repository: Repository, homes: dict[str, int]) -> Callable[[], object]:
    long_gone(repository)
    return repository.prune_old_listings


def the_end_of_scan_sweep_ages_out_and_deletes(repository: Repository, homes: dict[str, int]) -> Callable[[], object]:
    found_long_ago(repository, homes["mission"])
    long_gone(repository)
    return repository.retire_old_listings


def the_homes_are_keyed_again(repository: Repository, homes: dict[str, int]) -> Callable[[], object]:
    # Spelled as a source spells it; keying the board again respells it.
    another_program(repository, "UPDATE listings SET neighborhood = 'nopa' WHERE id = ?", homes["nopa"])

    def key_again() -> None:
        with repository.connection() as connection:
            repository.refresh_identities(connection)

    return key_again


def another_program_adds_a_listing(repository: Repository, homes: dict[str, int]) -> Callable[[], object]:
    # A bare insert, with none of the keying an upsert does after it: the
    # insert alone has to count.
    url = URLS["Redfin"].format("r8")
    return lambda: another_program(
        repository,
        "INSERT INTO listings (platform, source_id, canonical_url, original_url, title, price, neighborhood, "
        "housing_kind, unit_type, concern, score, first_found, last_seen) "
        "VALUES ('Redfin', 'r8', ?, ?, '17 Oak St #1', 1900, 'Inner Richmond', 'whole_unit', NULL, '', 90, ?, ?)",
        url.casefold(), url, days_ago(0), days_ago(0),
    )


def another_program_rescores_a_listing(repository: Repository, homes: dict[str, int]) -> Callable[[], object]:
    return lambda: another_program(repository, "UPDATE listings SET score = 30 WHERE id = ?", homes["mission"])


def another_program_deletes_a_listing(repository: Repository, homes: dict[str, int]) -> Callable[[], object]:
    return lambda: another_program(repository, "DELETE FROM listings WHERE id = ?", homes["low"])


CHANGES = [
    a_new_listing_arrives,
    a_listing_is_found_again_below_the_cut_off,
    a_home_is_starred,
    a_star_is_taken_off,
    a_home_is_passed,
    a_passed_home_is_restored,
    a_note_brings_an_aged_out_home_back,
    a_home_is_rescored,
    the_archive_ages_a_home_out,
    retention_deletes_a_home_nobody_kept,
    the_end_of_scan_sweep_ages_out_and_deletes,
    the_homes_are_keyed_again,
    another_program_adds_a_listing,
    another_program_rescores_a_listing,
    another_program_deletes_a_listing,
]


@pytest.mark.parametrize("change", CHANGES, ids=[change.__name__ for change in CHANGES])
def test_every_way_a_listing_changes_is_a_change(repository: Repository, change) -> None:
    make_the_change = change(repository, thin_shortlist(repository))
    version = repository.board_version()
    before = answers(repository)

    make_the_change()

    after = answers(repository)
    assert after == answers_afresh(repository), "an answer remembered from before the change was served after it"
    assert after != before, "this change moves no answer, so it proves nothing about what is remembered"
    assert repository.board_version() != version


def opened(repository: Repository, listing_id: int) -> bool:
    with repository.connection() as connection:
        row = connection.execute("SELECT opened_at FROM listings WHERE id = ?", (listing_id,)).fetchone()
    return row["opened_at"] is not None


@pytest.mark.parametrize("open_it", ["mark_listing_opened", "open_listing_url"])
def test_opening_a_listing_is_not_a_change(
    repository: Repository, parsed: list[tuple[int, str]], open_it: str
) -> None:
    """The one write the counter leaves out: nothing remembered says which
    homes were opened, and reading a home is no reason to count the board
    again for the next page."""
    homes = thin_shortlist(repository)
    remembered = repository.exclusion_summary(*ONE_BEDROOM_TAB)
    version = repository.board_version()

    getattr(repository, open_it)(homes["tenderloin"])

    assert opened(repository, homes["tenderloin"]), "the open was not recorded, so this proves nothing"
    assert repository.board_version() == version
    parsed.clear()
    assert repository.exclusion_summary(*ONE_BEDROOM_TAB) == remembered
    assert parsed_here(parsed) == [], "opening a home cost the next page its memory"


def test_a_write_that_is_rolled_back_is_not_a_change(repository: Repository, parsed: list[tuple[int, str]]) -> None:
    """The counter lives in the file and moves inside the writer's own
    transaction, so a write that never happened never moved it."""
    homes = thin_shortlist(repository)
    remembered = repository.exclusion_summary(*ONE_BEDROOM_TAB)
    version = repository.board_version()

    connection = sqlite3.connect(repository.path, isolation_level=None)
    try:
        connection.execute("BEGIN")
        # Committed, this would put the home on the shortlist.
        connection.execute("UPDATE listings SET score = 90 WHERE id = ?", (homes["low"],))
        connection.execute("ROLLBACK")
    finally:
        connection.close()

    assert repository.board_version() == version
    parsed.clear()
    assert repository.exclusion_summary(*ONE_BEDROOM_TAB) == remembered == HELD_BACK
    assert parsed_here(parsed) == [], "a write that was rolled back cost the next page its memory"


# --------------------------------------------------------------------------
# what is remembered is the answer, and only its own answer
# --------------------------------------------------------------------------


def test_what_a_caller_does_with_an_answer_cannot_change_the_next_one(repository: Repository) -> None:
    thin_shortlist(repository)
    summary_afresh = Repository(repository.path).exclusion_summary(*ONE_BEDROOM_TAB)
    choices_afresh = Repository(repository.path).filter_options(*ONE_BEDROOM_TAB)
    summary = repository.exclusion_summary(*ONE_BEDROOM_TAB)
    choices = repository.filter_options(*ONE_BEDROOM_TAB)

    # A page decorating what it was given, in place: first the answer that was
    # worked out, then one that was remembered.
    for _ in range(2):
        summary[0]["count"] += 100
        summary.append({"reason": "made up", "count": 1})
        choices[0].append("Atlantis")
        choices[1].clear()

        summary = repository.exclusion_summary(*ONE_BEDROOM_TAB)
        choices = repository.filter_options(*ONE_BEDROOM_TAB)

        assert summary == summary_afresh
        assert choices == choices_afresh


def every_kind_of_home(repository: Repository) -> dict[str, int]:
    """Rooms and whole homes of each size, on and off the shortlist, starred
    and passed: enough for every pair of questions below to differ."""
    homes = {
        "room": put(repository, listing("Zillow", "1", "1 Page St", housing_kind="room", unit_type=None,
                                        neighborhood="NOPA"), verdict(85)),
        "room_70": put(repository, listing("Zillow", "2", "2 Page St", housing_kind="room", unit_type=None,
                                           neighborhood="Inner Richmond"), verdict(70)),
        "passed_room": put(repository, listing("Zillow", "3", "3 Page St", housing_kind="room", unit_type=None,
                                               neighborhood="Castro"), verdict(75)),
        "one_bed": put(repository, listing("Zillow", "4", "4 Oak St #1"), verdict(90)),
        "one_bed_70": put(repository, listing("Zillow", "5", "5 Oak St #1", neighborhood="Bernal Heights"), verdict(70)),
        "one_bed_out": put(repository, listing("Zillow", "6", "6 Oak St #1", neighborhood="Tenderloin"),
                           verdict(45, failing="area", reason="Tenderloin is outside your target neighborhoods.")),
        "passed_one_bed": put(repository, listing("Movoto", "m7", "7 Oak St #1", neighborhood="Castro"), verdict(75)),
        "studio_low": put(repository, listing("Zillow", "8", "8 Oak St #1", unit_type="studio",
                                              neighborhood="Outer Sunset"), verdict(40)),
        "studio_dear": put(repository, listing("Zillow", "9", "9 Oak St #1", unit_type="studio", price=3200),
                           verdict(49, failing="price", reason="The monthly price exceeds this path's maximum.")),
        "sizeless": put(repository, listing("Redfin", "r10", "10 Oak St #1", unit_type=None,
                                            neighborhood="Hayes Valley"), verdict(88)),
    }
    repository.set_listing_status(homes["one_bed"], "saved")
    repository.set_listing_status(homes["passed_room"], "dismissed")
    repository.set_listing_status(homes["passed_one_bed"], "dismissed")
    return homes


# Two questions differing in one thing each, asked in turn.
ROOMS_HAVE_A_TAB = (True, ("studio", "one_bedroom"))
ROOMS_HAVE_NO_TAB = (False, ("studio", "one_bedroom"))
PAIRS = {
    "held_back_at_two_cut_offs": (
        methodcaller("exclusion_summary", 60, "whole_unit", ("one_bedroom",)),
        methodcaller("exclusion_summary", 80, "whole_unit", ("one_bedroom",)),
    ),
    "held_back_on_two_tabs": (
        methodcaller("exclusion_summary", 60, "room"),
        methodcaller("exclusion_summary", 60, "whole_unit"),
    ),
    "held_back_at_two_sizes": (
        methodcaller("exclusion_summary", 60, "whole_unit", ("one_bedroom",)),
        methodcaller("exclusion_summary", 60, "whole_unit", ("studio",)),
    ),
    "held_back_at_two_lengths": (
        methodcaller("exclusion_summary", 60, "whole_unit", (), limit=1),
        methodcaller("exclusion_summary", 60, "whole_unit", ()),
    ),
    # The tab a filter is asked on, which is part of what it answers: the
    # shortlist's areas and Near matches' are different lists, and serving one
    # for the other is the crossing this pair exists to catch. It replaced a
    # pair of cut-offs, which stopped telling the two apart once the choices
    # started coming from the view rather than from a score alone.
    "choices_on_two_views": (
        methodcaller("filter_options", 60, "whole_unit", ("one_bedroom",), DEAL_TABS, "active"),
        methodcaller("filter_options", 60, "whole_unit", ("one_bedroom",), DEAL_TABS, "near_matches"),
    ),
    "choices_on_two_tabs": (
        methodcaller("filter_options", 60, "room", (), DEAL_TABS),
        methodcaller("filter_options", 60, "whole_unit", (), DEAL_TABS),
    ),
    "choices_at_two_sizes": (
        methodcaller("filter_options", 60, "whole_unit", ("one_bedroom",), DEAL_TABS),
        methodcaller("filter_options", 60, "whole_unit", ("studio",), DEAL_TABS),
    ),
    "choices_for_two_deals_other_tabs": (
        methodcaller("filter_options", 60, "other", (), ROOMS_HAVE_A_TAB),
        methodcaller("filter_options", 60, "other", (), ROOMS_HAVE_NO_TAB),
    ),
    "a_tab_in_two_views": (
        methodcaller("has_homes", 60, "saved", "room"),
        methodcaller("has_homes", 60, "active", "room"),
    ),
    "a_tab_at_two_cut_offs": (
        methodcaller("has_homes", 60, "active", "room"),
        methodcaller("has_homes", 90, "active", "room"),
    ),
    "two_tabs_in_one_view": (
        methodcaller("has_homes", 60, "saved", "room"),
        methodcaller("has_homes", 60, "saved", "whole_unit"),
    ),
    "two_deals_other_tabs": (
        methodcaller("has_homes", 60, "dismissed", "other", (True, ("one_bedroom",))),
        methodcaller("has_homes", 60, "dismissed", "other", (False, ("one_bedroom",))),
    ),
}


@pytest.mark.parametrize("first, second", list(PAIRS.values()), ids=list(PAIRS))
def test_two_questions_asked_in_turn_each_get_their_own_answer(
    repository: Repository, first: methodcaller, second: methodcaller
) -> None:
    every_kind_of_home(repository)
    truth = {question: question(Repository(repository.path)) for question in (first, second)}
    assert truth[first] != truth[second], "one answer for both, so this pair cannot show them crossing"

    for _ in range(2):
        assert first(repository) == truth[first], first
        assert second(repository) == truth[second], second


# --------------------------------------------------------------------------
# the counter itself
# --------------------------------------------------------------------------


def forget_the_counter(repository: Repository) -> None:
    """Leave the board as no version with the counter has opened it: no
    triggers on the listings, no table to count in."""
    connection = sqlite3.connect(repository.path)
    try:
        triggers = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'listings'"
        ).fetchall()
        for (name,) in triggers:
            connection.execute(f'DROP TRIGGER "{name}"')
        connection.execute("DROP TABLE IF EXISTS listings_version")
        connection.commit()
    finally:
        connection.close()


def test_a_board_without_the_counter_answers_everything_and_remembers_nothing(
    repository: Repository, parsed: list[tuple[int, str]]
) -> None:
    """Nothing could say when such a board changed, so nothing about it may be
    remembered -- and it must still answer, not fail."""
    homes = thin_shortlist(repository)
    remembered = answers(repository)
    forget_the_counter(repository)
    another_program(repository, "UPDATE listings SET score = 30 WHERE id = ?", homes["mission"])

    assert answers(repository) == answers_afresh(repository) != remembered
    for _ in range(2):
        parsed.clear()
        repository.exclusion_summary(*ONE_BEDROOM_TAB)
        assert parsed_here(parsed), "remembered on a board that cannot say when it changes"
    assert repository.board_version() is None
    assert _rent_table(repository) is not _rent_table(repository)

    # The next start puts the counter back, and it counts again.
    reopened = Repository(repository.path)
    reopened.initialize()
    version = reopened.board_version()
    assert version is not None
    reopened.set_listing_status(homes["low"], "dismissed")
    assert reopened.board_version() != version


def test_a_counter_made_again_from_nothing_never_revives_an_old_answer(repository: Repository) -> None:
    """Without the counter nothing may be vouched for -- including what was
    filed before it went. A counter made again from nothing climbs back
    through numbers already filed, and an answer kept under one of them would
    be handed out for a board it no longer describes."""
    homes = thin_shortlist(repository)
    version = repository.board_version()
    stale = answers(repository)
    forget_the_counter(repository)
    another_program(repository, "UPDATE listings SET score = 30 WHERE id = ?", homes["mission"])
    answers(repository)  # asked while the board can say nothing
    another_program(
        repository,
        "CREATE TABLE listings_version (id INTEGER PRIMARY KEY CHECK (id = 1), value INTEGER NOT NULL)",
    )
    another_program(repository, "INSERT INTO listings_version (id, value) VALUES (1, ?)", version)

    assert answers(repository) == answers_afresh(repository) != stale


def test_a_cut_off_that_is_not_a_whole_number_is_asked_and_filed_as_one(repository: Repository) -> None:
    """Answers are filed under the whole-number cut-off. Worked out with the
    raw one, 60.5 filed an empty answer under 60, handed to the next page that
    asked about 60."""
    # The one home on the board that the two cut-offs disagree about.
    put(repository, listing("Zillow", "1", "10 Oak St #1"), verdict(60))
    for question in ("filter_options", "has_homes"):
        arguments = {
            "filter_options": lambda minimum: (minimum, "whole_unit", ("one_bedroom",), DEAL_TABS),
            "has_homes": lambda minimum: (minimum, "active", "whole_unit", DEAL_TABS),
        }[question]
        getattr(repository, question)(*arguments(60.5))
        assert getattr(repository, question)(*arguments(60)) == getattr(Repository(repository.path), question)(
            *arguments(60)
        ), question


def test_a_column_added_later_is_counted_without_anybody_remembering_to(repository: Repository) -> None:
    """The update trigger names the columns it counts. A column added by a
    later version must be counted from the start that adds it, or a change to
    it alone would leave every remembered answer in place."""
    thin_shortlist(repository)
    another_program(repository, "ALTER TABLE listings ADD COLUMN extra TEXT")
    repository.initialize()  # what the start that added it does next
    version = repository.board_version()

    another_program(repository, "UPDATE listings SET extra = 'x'")

    assert repository.board_version() != version
    with repository.connection() as connection:
        columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(listings)")}
        trigger = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = 'listings_version_update'"
        ).fetchone()
    assert trigger is not None, "no trigger counts updates"
    named = re.search(r"AFTER UPDATE OF (.+?) ON listings", str(trigger[0]))
    assert named is not None, trigger[0]
    counted = [column.strip() for column in named.group(1).split(",")]
    assert sorted(counted) == sorted(columns - {"id", "opened_at"})


# --------------------------------------------------------------------------
# the rent medians the shortlist ranks unpriced homes by
# --------------------------------------------------------------------------


def rents_in_the_mission(repository: Repository) -> list[int]:
    """Eight one-bedrooms, the fewest a median is drawn from, at $3,000 to $3,700."""
    return [
        put(repository, listing("Zillow", f"v{index}", f"{20 + index} Valencia St #1", price=3000 + 100 * index),
            verdict(85))
        for index in range(8)
    ]


def the_day_is(monkeypatch: pytest.MonkeyPatch, moment: datetime) -> None:
    """Fix the day the app keys the medians by, so that midnight passing
    between two calls cannot decide a test."""

    class Fixed(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment.astimezone(tz)

    monkeypatch.setattr("sf_housing.app.datetime", Fixed)


def test_the_rent_medians_are_remembered_until_a_listing_changes(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    homes = rents_in_the_mission(repository)
    the_day_is(monkeypatch, datetime.now(UTC))
    table = _rent_table(repository)
    assert _rent_table(repository) is table, "nothing changed, and the medians were worked out again"

    # A rent corrected in place: no new row, and no search saw anything.
    another_program(repository, "UPDATE listings SET price = price + 1000 WHERE id = ?", homes[0])

    changed = _rent_table(repository)
    assert changed is not table, "a rent changed and the old medians were kept"
    assert changed == RentTable.from_observations(repository.rent_observations(WINDOW_DAYS))
    assert changed.estimate("Mission", "one_bedroom") != table.estimate("Mission", "one_bedroom")


def test_the_rent_medians_are_worked_out_again_on_a_new_day(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    """They are over the last sixty days, which move with the calendar as well
    as with the listings."""
    rents_in_the_mission(repository)
    moment = datetime.now(UTC)
    the_day_is(monkeypatch, moment)
    today = _rent_table(repository)
    assert _rent_table(repository) is today

    the_day_is(monkeypatch, moment + timedelta(days=1))

    tomorrow = _rent_table(repository)
    assert tomorrow is not today, "a day passed and yesterday's medians were kept"
    assert _rent_table(repository) is tomorrow


# --------------------------------------------------------------------------
# writers, threads and size
# --------------------------------------------------------------------------


def test_a_change_landing_while_an_answer_is_worked_out_is_never_hidden_by_it(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The counter is read before the answer is worked out, so an answer can
    only be filed under a version older than what it saw. Read after, a scan
    committing in between would file the old answer under the new version,
    and it would be shown until the next change."""
    homes = thin_shortlist(repository)
    real = json.loads
    landed: list[bool] = []

    def a_scan_commits_meanwhile(document, *args, **kwargs):
        # The held-back rows have been read; the answer is being worked out.
        # The scan finds one of them inside the deal after all.
        if not landed:
            landed.append(True)
            another_program(
                repository, "UPDATE listings SET score = 90, eligibility = 'eligible' WHERE id = ?", homes["sunset"]
            )
        return real(document, *args, **kwargs)

    monkeypatch.setattr(database.json, "loads", a_scan_commits_meanwhile)
    repository.exclusion_summary(*ONE_BEDROOM_TAB)
    monkeypatch.setattr(database.json, "loads", real)

    assert landed, "the write never landed while the answer was worked out, so this proves nothing"
    assert repository.exclusion_summary(*ONE_BEDROOM_TAB) == Repository(repository.path).exclusion_summary(
        *ONE_BEDROOM_TAB
    )


def test_the_memory_holds_a_bounded_number_of_answers(
    repository: Repository, parsed: list[tuple[int, str]]
) -> None:
    """A board is asked a handful of questions between changes. One asked
    endlessly many must not grow the memory without end."""
    thin_shortlist(repository)
    repository.exclusion_summary(0, "whole_unit", ("one_bedroom",))
    for cut_off in range(1, Repository.REMEMBERED_ANSWERS + 1):
        repository.exclusion_summary(cut_off, "whole_unit", ("one_bedroom",))

    parsed.clear()
    repository.exclusion_summary(0, "whole_unit", ("one_bedroom",))

    assert parsed_here(parsed), "the first answer was still held after more answers than the memory keeps"


def test_pages_answered_on_many_threads_while_a_scan_writes_end_with_the_board_as_it_is(
    repository: Repository,
) -> None:
    """Pages on the server's worker threads, some answering from one reused
    connection as the dashboard does, while a scan writes. None may fail, and
    once the writes stop every answer is the board's.

    Python lets another thread in every 5 ms, and the memory is looked at and
    refiled in microseconds, so at that pace a missing lock almost never
    shows. Switching every microsecond, taking the lock out showed as
    "dictionary changed size during iteration" in 16 of 16 runs alone and 15
    of 16 with three more copies of it running at once; with the lock, every
    run passed.
    """
    homes = thin_shortlist(repository)
    failures: list[BaseException] = []
    scanned = threading.Event()

    def page(reusing: bool) -> None:
        try:
            while not scanned.is_set():
                with repository.reusing_one_connection() if reusing else nullcontext():
                    answers(repository)
                    # Twenty questions more than a page asks, so the memory the
                    # threads share is big enough to be caught mid-look.
                    for cut_off in range(50, 70):
                        repository.exclusion_summary(cut_off, "whole_unit", ("one_bedroom",))
        except BaseException as exc:  # noqa: BLE001 - reported below, with the rest
            failures.append(exc)

    def scan() -> None:
        try:
            for turn in range(40):
                repository.set_listing_status(homes["low"], "dismissed" if turn % 2 == 0 else "active")
                another_program(repository, "UPDATE listings SET score = ? WHERE id = ?", 40 + turn, homes["sunset"])
        except BaseException as exc:  # noqa: BLE001
            failures.append(exc)
        finally:
            scanned.set()

    threads = [threading.Thread(target=page, args=(index % 2 == 0,)) for index in range(8)]
    threads.append(threading.Thread(target=scan))
    interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
    finally:
        sys.setswitchinterval(interval)

    assert not any(thread.is_alive() for thread in threads), "a page or the scan never finished"
    assert failures == []
    assert answers(repository) == answers_afresh(repository)
    with repository.reusing_one_connection():
        assert answers(repository) == answers_afresh(repository)
