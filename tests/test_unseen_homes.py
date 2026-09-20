"""How long since a source last actually returned a home decides where it sits.

A home the app cannot see any more is not a home the app knows is gone. The
whole of this file is that distinction: absence demotes and then moves a home
to Near matches, where it is still ranked, still filtered and still one click
from its own listing; only proof -- a page we fetched that said so -- removes
it from the shortlist outright, and nothing at all deletes it.

The clock is the source's own. A site that is blocking us, timing out, or
returning a page it no longer parses records no successful search, so its
homes stop ageing until it answers again. One outage emptying the shortlist is
the worst false positive available here.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sf_housing.database import (
    UNSEEN_DEMOTE_AFTER,
    UNSEEN_SHORTLIST_AFTER,
    Repository,
    near_miss_distance,
)


CUT_OFF = 60

# Fixed, because none of this is measured against the wall clock: a home's age
# is the gap between when its source last searched and when that search last
# returned it, and both are stamps on the board.
NOW = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)

DEMOTE_HOURS = UNSEEN_DEMOTE_AFTER.total_seconds() / 3600
SHORTLIST_HOURS = UNSEEN_SHORTLIST_AFTER.total_seconds() / 3600


def at(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).isoformat(timespec="seconds")


@pytest.fixture
def repository(tmp_path: Path) -> Repository:
    instance = Repository(tmp_path / "housing.sqlite3")
    instance.initialize()
    return instance


def home(
    repository: Repository,
    source_id: str,
    *,
    seen: float,
    score: int = 80,
    platform: str = "Zillow",
    status: str = "active",
    note: str = "",
    metadata: dict[str, object] | None = None,
    home_key: str = "",
    eligibility: str = "eligible",
    housing_kind: str = "whole_unit",
    unit_type: str | None = "one_bedroom",
) -> int:
    """One stored home, last returned by a search ``seen`` hours before ``NOW``."""
    with repository.connection() as connection:
        cursor = connection.execute(
            """INSERT INTO listings (platform, source_id, title, original_url, canonical_url,
                   price, housing_kind, unit_type, concern, score, eligibility, status, note,
                   metadata_json, home_key, first_found, last_seen)
               VALUES (?, ?, ?, ?, ?, 1800, ?, ?, '', ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                platform,
                source_id,
                f"Flat {source_id}",
                f"https://example.test/{platform}/{source_id}",
                f"https://example.test/{platform}/{source_id}",
                housing_kind,
                unit_type,
                score,
                eligibility,
                status,
                note,
                json.dumps(metadata or {}),
                home_key,
                at(24 * 10),
                at(seen),
            ),
        )
        connection.commit()
        return int(cursor.lastrowid)


def searched(
    repository: Repository,
    platform: str = "Zillow",
    *,
    hours_ago: float = 0,
    status: str = "success",
    seen: int = 40,
) -> None:
    """A search of ``platform`` on record: when it ran, how it ended, what it read."""
    with repository.connection() as connection:
        scan = connection.execute(
            "INSERT INTO scan_runs (trigger, status, started_at) VALUES ('test', 'completed', ?)",
            (at(hours_ago),),
        ).lastrowid
        connection.execute(
            """INSERT INTO source_runs (scan_run_id, platform, status, started_at, listings_seen)
               VALUES (?, ?, ?, ?, ?)""",
            (scan, platform, status, at(hours_ago), seen),
        )
        connection.commit()


def shortlist(repository: Repository) -> list[str]:
    return [row["source_id"] for row in _rows(repository, "active")]


def near_matches(repository: Repository) -> list[str]:
    return [row["source_id"] for row in _rows(repository, "near_matches")]


def _rows(repository: Repository, view: str, sort: str = "score") -> list[dict[str, object]]:
    return repository.query_listings(
        minimum_score=CUT_OFF,
        sort=sort,
        housing_kind="whole_unit",
        unit_types=("one_bedroom",),
        view=view,
    )


# --------------------------------------------------------------------------
# the two thresholds
# --------------------------------------------------------------------------


def test_the_two_thresholds_are_the_ones_that_were_decided() -> None:
    """Thirty-six hours and three days, written down where a change is visible.

    Every other test here is measured against these constants, so on their own
    they would let either number be moved without a single test noticing.
    Tuning them is a decision about the product and is meant to change this
    line with them; drifting into them by accident is not.
    """
    assert UNSEEN_DEMOTE_AFTER == timedelta(hours=36)
    assert UNSEEN_SHORTLIST_AFTER == timedelta(days=3)


def test_a_home_unseen_for_36_hours_ranks_below_every_home_still_showing_up(
    repository: Repository,
) -> None:
    """The top of the shortlist was homes nobody could go and see.

    A home that has stopped appearing keeps its score, and score is what the
    recommended order is made of, so the best-scoring absence outranked every
    home the sources were still returning that morning.
    """
    home(repository, "quiet", seen=DEMOTE_HOURS + 1, score=95)
    home(repository, "showing", seen=1, score=70)
    searched(repository)

    assert shortlist(repository) == ["showing", "quiet"]


def test_a_home_just_inside_36_hours_keeps_its_place_at_the_top(
    repository: Repository,
) -> None:
    """A single missed check must not cost a home its ranking.

    The threshold is three chances at twice a day, not one: a scan that ran
    late, or a site that skipped the home once, is not a home going away.
    """
    home(repository, "quiet", seen=DEMOTE_HOURS - 1, score=95)
    home(repository, "showing", seen=1, score=70)
    searched(repository)

    assert shortlist(repository) == ["quiet", "showing"]


def test_a_home_unseen_for_three_days_leaves_the_shortlist_for_near_matches(
    repository: Repository,
) -> None:
    """A shortlist of homes that are no longer listed is worth nothing.

    Off the shortlist and into Near matches, not out of the board: absence is
    not proof, so the home stays where somebody can still look for themselves.
    """
    home(repository, "gone_quiet", seen=SHORTLIST_HOURS + 1, score=92)
    home(repository, "showing", seen=1, score=70)
    searched(repository)

    assert shortlist(repository) == ["showing"]
    assert near_matches(repository) == ["gone_quiet"]


def test_a_home_just_inside_three_days_is_still_on_the_shortlist(
    repository: Repository,
) -> None:
    """The line has to be the line, or tuning either number means nothing."""
    home(repository, "gone_quiet", seen=SHORTLIST_HOURS - 1, score=92)
    searched(repository)

    assert shortlist(repository) == ["gone_quiet"]


def test_a_home_that_left_the_shortlist_is_still_stored_and_still_searchable(
    repository: Repository,
) -> None:
    """Nothing is ever deleted, and "removed" must not mean "hidden".

    The Archive tab holds every home the app has collected. A rule about
    absence that emptied it too would take away the one place a reader can
    check the app's own guess.
    """
    listing_id = home(repository, "gone_quiet", seen=SHORTLIST_HOURS + 48, score=92)
    searched(repository)

    assert [row["source_id"] for row in _rows(repository, "all")] == ["gone_quiet"]
    with repository.connection() as connection:
        row = connection.execute(
            "SELECT status, score FROM listings WHERE id = ?", (listing_id,)
        ).fetchone()
    assert (row["status"], row["score"]) == ("active", 92)


# --------------------------------------------------------------------------
# absence has to be evidenced
# --------------------------------------------------------------------------


def test_a_source_that_has_not_searched_successfully_since_costs_its_homes_nothing(
    repository: Repository,
) -> None:
    """One outage must never empty the shortlist.

    Redfin on the owner's board had failed every check for nine days. Measured
    against the wall clock its 279 homes were all long unseen, when the only
    thing anybody knew about them was that nobody had asked.
    """
    home(repository, "unasked", seen=SHORTLIST_HOURS + 48, score=92)
    # Its last good search predates the home; every check since has failed.
    searched(repository, hours_ago=SHORTLIST_HOURS + 72)
    searched(repository, hours_ago=1, status="error")
    searched(repository, hours_ago=0, status="backoff")

    assert shortlist(repository) == ["unasked"]
    assert near_matches(repository) == []


def test_a_search_that_read_nothing_costs_its_homes_nothing(
    repository: Repository,
) -> None:
    """A site changing its markup looks exactly like a site with no homes.

    It succeeds, it raises nothing, and it parses zero listings out of the
    page. Counted as a search that could have returned these homes, the next
    such change would quietly drain the shortlist and blame the homes for it.
    """
    home(repository, "unread", seen=SHORTLIST_HOURS + 48, score=92)
    searched(repository, hours_ago=0, seen=0)

    assert shortlist(repository) == ["unread"]


def test_a_home_whose_source_has_never_searched_keeps_its_place(
    repository: Repository,
) -> None:
    """Absence has to be evidenced before it costs anything.

    A home imported from an alert email, or stored by a source whose history
    the board has lost, has no search to be absent from.
    """
    home(repository, "orphan", seen=SHORTLIST_HOURS + 240, score=92)

    assert shortlist(repository) == ["orphan"]


def test_another_sites_search_does_not_age_this_sites_home(
    repository: Repository,
) -> None:
    """Only the home's own source can say it stopped appearing.

    Sources are read on their own schedules and several are read by hand, so
    borrowing the newest search on the board would age every home to whichever
    site ran last.
    """
    home(repository, "quiet_zillow", seen=SHORTLIST_HOURS + 48, score=92, platform="Zillow")
    searched(repository, "Craigslist", hours_ago=0)

    assert shortlist(repository) == ["quiet_zillow"]


def test_a_page_that_confirmed_the_home_since_restarts_the_clock(
    repository: Repository,
) -> None:
    """Opening a home's own page and finding it is the strongest sighting there is.

    It is written as ``last_verified_at`` rather than as ``last_seen``, and a
    rule that read only the searches would go on ageing a home the app had
    just confirmed by hand.
    """
    home(
        repository,
        "checked",
        seen=SHORTLIST_HOURS + 48,
        score=92,
        metadata={"last_verified_at": at(2)},
    )
    searched(repository)

    assert shortlist(repository) == ["checked"]


def test_an_old_page_check_does_not_age_a_home_the_searches_still_return(
    repository: Repository,
) -> None:
    """Whichever sighting is later wins, not whichever kind is more direct.

    Preferring the page stamp would age a home the moment somebody opened it,
    and then go on ageing it however many searches returned it afterwards.
    """
    home(
        repository,
        "fresh",
        seen=1,
        score=92,
        metadata={"last_verified_at": at(SHORTLIST_HOURS + 48)},
    )
    searched(repository)

    assert shortlist(repository) == ["fresh"]


def test_one_site_going_quiet_does_not_move_a_home_another_still_lists(
    repository: Repository,
) -> None:
    """A home is on the shortlist while any site is still showing it.

    Two sites list one flat; one drops it and the other advertises it daily.
    Judging the home by the copy that went quiet would hide a home that is
    demonstrably still for rent.
    """
    home(repository, "quiet_copy", seen=SHORTLIST_HOURS + 48, score=92, home_key="flat-1")
    home(repository, "live_copy", seen=1, score=92, platform="Movoto", home_key="flat-1")
    searched(repository, "Zillow")
    searched(repository, "Movoto")

    assert len(shortlist(repository)) == 1, "one home, however many sites list it"
    assert near_matches(repository) == []


def test_one_site_going_quiet_does_not_demote_a_home_another_showed_this_morning(
    repository: Repository,
) -> None:
    """The freshest copy speaks for the home, not the stalest.

    Both copies are still on the shortlist here -- one two days old, one an
    hour -- so the choice between them is the whole of the rule. Reading the
    stalest would sink a home that was on its other site this morning below
    homes nobody has seen since.
    """
    home(repository, "quiet_copy", seen=DEMOTE_HOURS + 4, score=92, home_key="flat-1")
    home(repository, "live_copy", seen=1, score=92, platform="Movoto", home_key="flat-1")
    home(repository, "other", seen=1, score=70, platform="Craigslist")
    searched(repository, "Zillow")
    searched(repository, "Movoto")
    searched(repository, "Craigslist")

    assert shortlist(repository) == ["live_copy", "other"]


# --------------------------------------------------------------------------
# what absence may never do
# --------------------------------------------------------------------------


def test_reading_the_shortlist_never_writes_the_proof_that_a_home_is_gone(
    repository: Repository,
) -> None:
    """``verified_inactive`` means a page said so, and must keep meaning that.

    Folding absence into the same flag would make every later reader of it --
    the scorer, the Archive, the copy the page shows a home by -- state a
    proof the app does not have.
    """
    home(repository, "gone_quiet", seen=SHORTLIST_HOURS + 240, score=92)
    searched(repository)

    near_matches(repository)

    with repository.connection() as connection:
        stored = connection.execute("SELECT metadata_json, availability_state FROM listings").fetchall()
    assert [json.loads(row["metadata_json"]) for row in stored] == [{}]
    assert [row["availability_state"] for row in stored] == ["unknown"]


def test_a_starred_home_stays_on_the_shortlist_however_long_it_is_unseen(
    repository: Repository,
) -> None:
    """A star is the user's own work and the only thing here that cannot be refetched.

    Somebody who starred a home has decided to chase it, and decides for
    themselves when to give up on it.
    """
    home(repository, "starred", seen=SHORTLIST_HOURS + 240, score=92, status="saved")
    searched(repository)

    assert shortlist(repository) == ["starred"]
    assert near_matches(repository) == []


def test_a_noted_home_stays_on_the_shortlist_however_long_it_is_unseen(
    repository: Repository,
) -> None:
    """A note is the user's own writing, and means they are working on this home."""
    home(repository, "noted", seen=SHORTLIST_HOURS + 240, score=92, note="Emailed the agent")
    searched(repository)

    assert shortlist(repository) == ["noted"]


# --------------------------------------------------------------------------
# proof, which is a different thing
# --------------------------------------------------------------------------


def test_a_home_proved_gone_leaves_the_shortlist_before_anything_rescores_it(
    repository: Repository,
) -> None:
    """Scoring caps a proved-gone home at 49, but only next time it is scored.

    Between the page saying so and the next rescore -- a restart away, or a
    board that has not changed enough to trigger one -- the home kept whatever
    score it had and sat on the shortlist with it.
    """
    home(repository, "proved_gone", seen=1, score=92, metadata={"verified_inactive": True})
    home(repository, "showing", seen=1, score=70)
    searched(repository)

    assert shortlist(repository) == ["showing"]


def test_a_home_proved_gone_is_not_offered_as_a_near_match(
    repository: Repository,
) -> None:
    """Near matches is for homes that might still be real.

    A home whose own page said it is gone has the Archive. Putting proof in
    beside absence would bury the homes worth a second look under the ones
    that are not -- and a home proved gone has almost always stopped appearing
    in the searches too, so the two arrive together.
    """
    home(repository, "proved_gone_and_quiet", seen=SHORTLIST_HOURS + 48, score=92,
         metadata={"verified_inactive": True})
    home(repository, "proved_gone_today", seen=1, score=92, metadata={"verified_inactive": True})
    searched(repository)

    assert near_matches(repository) == []


# --------------------------------------------------------------------------
# what the page says about it
# --------------------------------------------------------------------------


def test_a_home_that_went_quiet_sorts_among_the_near_matches_by_how_long_ago(
    repository: Repository,
) -> None:
    """Sorted by how nearly each home matched, absence had no measure at all.

    So a home that scored 92 until yesterday sorted below every home the deal
    had turned down, when it is the likeliest of them to still be worth a call.
    """
    just_gone = {"unseen_days": UNSEEN_SHORTLIST_AFTER.total_seconds() / 86400 + 0.5, "score": 92}
    long_gone = {"unseen_days": UNSEEN_SHORTLIST_AFTER.total_seconds() / 86400 * 4, "score": 92}
    nine_points_short = {"score": CUT_OFF - 9}

    order = sorted(
        (long_gone, nine_points_short, just_gone),
        key=lambda item: near_miss_distance(item, CUT_OFF),
    )

    assert order == [just_gone, nine_points_short, long_gone]


def test_a_home_still_showing_up_is_not_measured_as_a_near_miss_by_absence(
    repository: Repository,
) -> None:
    """The third measure must not quietly reorder the two that were there."""
    assert near_miss_distance({"score": CUT_OFF - 1, "unseen_days": 0.0}, CUT_OFF) == pytest.approx(0.1)


def test_the_page_says_a_home_went_quiet_rather_than_counting_points_it_did_not_lose(
    repository: Repository,
) -> None:
    """Near matches explains every row by how far it missed.

    A home short of nothing at all -- it cleared the cut-off and the budget,
    and its source stopped returning it -- was explained as "0 points below
    your cut-off", which is both false and unanswerable.
    """
    home(repository, "gone_quiet", seen=SHORTLIST_HOURS + 24, score=92)
    searched(repository)

    row = _rows(repository, "near_matches")[0]

    assert row["unseen_too_long"] is True
    assert row["unseen_days"] == pytest.approx(4.0, abs=0.01)


def test_a_home_the_sources_still_return_is_not_marked_as_having_gone_quiet(
    repository: Repository,
) -> None:
    """Every row carries the field, so a false one would mislabel the page."""
    home(repository, "showing", seen=1, score=92)
    searched(repository)

    row = _rows(repository, "active")[0]

    assert row["unseen_too_long"] is False
    # The hour between the search and the sighting, not a rounded-off nothing:
    # the number the page prints has to be the one the rules are read from.
    assert row["unseen_days"] == pytest.approx(1 / 24, abs=0.001)


# --------------------------------------------------------------------------
# the numbers beside the shortlist have to agree with it
# --------------------------------------------------------------------------


def test_the_cut_off_slider_counts_a_home_that_went_quiet_as_off_the_shortlist(
    repository: Repository,
) -> None:
    """The number under the slider is a promise about what the page will show.

    It is built from its own predicate rather than the view's, so every rule
    the shortlist gains has to reach it too or the slider reads high.
    """
    home(repository, "gone_quiet", seen=SHORTLIST_HOURS + 24, score=92)
    home(repository, "showing", seen=1, score=92)
    searched(repository)

    counts = repository.shortlist_counts([CUT_OFF], kinds=("whole_unit",))

    assert counts[CUT_OFF] == len(shortlist(repository)) == 1


def test_the_cut_off_slider_counts_a_home_proved_gone_as_off_the_shortlist(
    repository: Repository,
) -> None:
    """Same promise, for the other thing that takes a home off the shortlist."""
    home(repository, "proved_gone", seen=1, score=92, metadata={"verified_inactive": True})
    home(repository, "showing", seen=1, score=92)
    searched(repository)

    counts = repository.shortlist_counts([CUT_OFF], kinds=("whole_unit",))

    assert counts[CUT_OFF] == len(shortlist(repository)) == 1


def test_a_thin_shortlist_names_absence_rather_than_blaming_the_deal(
    repository: Repository,
) -> None:
    """The one explanation of a thin shortlist a reader gets named every cause but this.

    A deal that turned nothing down, and homes missing from the page anyway,
    read as a broken scraper -- which is exactly what the note exists to
    prevent somebody concluding.
    """
    home(repository, "gone_quiet", seen=SHORTLIST_HOURS + 24, score=92)
    home(repository, "showing", seen=1, score=92)
    searched(repository)

    reasons = repository.exclusion_summary(CUT_OFF, "whole_unit", ("one_bedroom",))

    assert [item["reason"] for item in reasons] == ["not listed by their own source for 3 days or more"]
    assert [item["count"] for item in reasons] == [1]


# --------------------------------------------------------------------------
# the page somebody actually opens
# --------------------------------------------------------------------------


def dashboard(tmp_path: Path, build, view: str = "near_matches", sort: str = "closeness") -> str:
    """The real page, built the way the app builds it, over a board ``build`` fills."""
    from fastapi.testclient import TestClient

    from sf_housing.app import create_app
    from sf_housing.settings import Settings
    from tests.conftest import TEST_PREFERENCES

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    preferences_path = tmp_path / "preferences.yaml"
    preferences_path.write_text(TEST_PREFERENCES, encoding="utf-8")
    settings = Settings(
        data_dir=data_dir,
        preferences_path=preferences_path,
        database_path=data_dir / "housing.sqlite3",
        log_path=data_dir / "test.log",
    )
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    build(application.state.repository)
    with TestClient(application) as client:
        return client.get(f"/?housing=room&view={view}&sort={sort}").text


def test_near_matches_tells_the_reader_the_source_stopped_listing_the_home(
    tmp_path: Path,
) -> None:
    """Every row in Near matches is explained by how far it missed.

    A home short of nothing -- it cleared the cut-off and the budget, and its
    source simply stopped returning it -- was explained as "0 points below
    your cut-off": false, and no help at all to somebody deciding whether to
    ring the number on the listing.
    """

    def board(repository: Repository) -> None:
        home(repository, "gone_quiet", seen=SHORTLIST_HOURS + 24, score=92,
             housing_kind="room", unit_type=None)
        searched(repository)

    page = dashboard(tmp_path, board)

    assert "Not listed by Zillow for 4 days" in page
    assert "points below your cut-off" not in page


def test_the_shortlist_stops_offering_a_home_its_source_has_dropped(
    tmp_path: Path,
) -> None:
    """The whole point, end to end: what somebody is shown first.

    A shortlist that keeps offering homes that are no longer listed teaches
    the reader to distrust all of it, which costs far more than the one home.
    """

    def board(repository: Repository) -> None:
        home(repository, "gone_quiet", seen=SHORTLIST_HOURS + 24, score=92,
             housing_kind="room", unit_type=None)
        home(repository, "showing", seen=1, score=70, housing_kind="room", unit_type=None)
        searched(repository)

    shortlist_page = dashboard(tmp_path, board, view="active", sort="score")

    assert "Flat showing" in shortlist_page
    assert "Flat gone_quiet" not in shortlist_page
