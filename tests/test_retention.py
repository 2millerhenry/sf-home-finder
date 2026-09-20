"""WO-5 item 4: nothing was ever deleted, so a year of listings was a year of rows.

The real board grows by ~572 listings and ~2.5MB a day, and a synthetic year
held 213,408 rows in 952MB, every one of them read by the pages one way or
another. A retention sweep now ends every scan, in one pass and one
transaction with WO-3's 21-day archive (D1): a row the archive aged out, that
no search has returned and nothing has confirmed for 120 days, is deleted --
once it has been in the Archive that long too, and its own site has been
searched successfully since it was last seen -- and nothing else ever is. A home any copy of which is starred, noted, passed
or restored is the user's own work, the only thing on the board that cannot
be fetched again, and survives however old it is; so does a row whose dates
are not dates. The live board is younger than 120 days, so every case here
builds its own rows and dates.
"""

from __future__ import annotations

import json
import random
import re
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

import httpx
import pytest

from sf_housing.database import Repository
from sf_housing.models import ListingCandidate, ScoreResult
from sf_housing.scanner import Scanner

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
# A date given as days before NOW, or -- for a date that is not one -- the
# text stored as it stands.
Stamp = float | str
AS_SEEN = object()
BUCHANAN = "295 Buchanan St #105"


def at(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat(timespec="seconds")


def stamp(value: Stamp) -> str:
    return at(value) if isinstance(value, (int, float)) else value


def days_before_the_real_clock(days: float) -> str:
    """For a scan, which sweeps at the moment it ends rather than at NOW."""
    return (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")


def card(slug: str, *, platform: str = "Zillow", address: str | None = None) -> ListingCandidate:
    url = {
        "Zillow": f"https://www.zillow.com/homedetails/x/{slug}_zpid/",
        "Movoto": f"https://www.movoto.com/san-francisco-ca/{slug}/for-rent/",
    }[platform]
    return ListingCandidate(
        platform=platform, source_id=slug, title=address or f"Home {slug}", original_url=url,
        price=1900, neighborhood="Mission", listing_type="Apartment",
        summary="Entire one bedroom apartment, 1 bed 1 bath.",
        metadata={"address": address} if address else {}, housing_kind="whole_unit", unit_type="one_bedroom",
    )


def put(repository: Repository, listing: ListingCandidate, **kwargs) -> int:
    # A stored verdict rather than a scored one: these tests are about which
    # rows are kept, and the test deal is for rooms.
    listing_id, _ = repository.upsert_listing(
        listing, ScoreResult(80, ["Stored"], "", {}, eligibility="eligible"), **kwargs
    )
    return listing_id


def home(repository: Repository, slug: str, *, found: Stamp, seen: Stamp | None = None,
         confirmed: Stamp | None | object = AS_SEEN, platform: str = "Zillow", address: str | None = None,
         **columns: object) -> int:
    """A stored home as the months left it.

    First found ``found`` days ago, and last returned by a search ``seen``
    days ago -- by default, when it was found. A scan stamps a confirmation
    whenever a search returns a home, so it was last confirmed then too
    unless ``confirmed`` says otherwise; None is a home nothing ever
    confirmed. ``columns`` (status, status_reason, note, home_key,
    metadata_json) are written as given, last.
    """
    listing_id = put(repository, card(slug, platform=platform, address=address))
    seen = found if seen is None else seen
    confirmed = seen if confirmed is AS_SEEN else confirmed
    with repository.connection() as connection:
        connection.execute(
            "UPDATE listings SET first_found = ?, last_seen = ? WHERE id = ?",
            (stamp(found), stamp(seen), listing_id),
        )
        if confirmed is not None:
            connection.execute(
                "UPDATE listings SET metadata_json = json_set(metadata_json, '$.last_verified_at', ?) WHERE id = ?",
                (stamp(confirmed), listing_id),
            )
        for name, value in columns.items():
            connection.execute(f"UPDATE listings SET {name} = ? WHERE id = ?", (value, listing_id))
        connection.commit()
    return listing_id


def aged_out(repository: Repository, slug: str, *, unseen: Stamp = 200, found: Stamp | None = None, **kwargs) -> int:
    """A home the 21-day archive aged out, that no search has returned for ``unseen`` days."""
    kwargs.setdefault("status", "dismissed")
    kwargs.setdefault("status_reason", "aged")
    return home(repository, slug, found=unseen if found is None else found, seen=unseen, **kwargs)


def backlog(repository: Repository, count: int) -> list[int]:
    """``count`` separate homes the archive aged out, none listed for 200 days."""
    ids = [put(repository, card(f"b{index}")) for index in range(count)]
    with repository.connection() as connection:
        connection.executemany(
            "UPDATE listings SET first_found = ?, last_seen = ?, status = 'dismissed', status_reason = 'aged' "
            "WHERE id = ?",
            [(at(200), at(200), listing_id) for listing_id in ids],
        )
        connection.commit()
    return ids


def remaining(repository: Repository) -> set[int]:
    with repository.connection() as connection:
        return {int(row[0]) for row in connection.execute("SELECT id FROM listings")}


def state(repository: Repository, listing_id: int) -> tuple[str, str | None]:
    with repository.connection() as connection:
        row = connection.execute("SELECT status, status_reason FROM listings WHERE id = ?", (listing_id,)).fetchone()
    return row["status"], row["status_reason"]


def prune(repository: Repository, **kwargs) -> int:
    return repository.prune_old_listings(now=NOW.isoformat(), **kwargs)


def retire(repository: Repository, **kwargs) -> tuple[int, int]:
    return repository.retire_old_listings(now=NOW.isoformat(), **kwargs)


def later(repository: Repository, *, days: float) -> tuple[int, int]:
    """The end-of-scan sweep ``days`` after NOW."""
    return repository.retire_old_listings(now=(NOW + timedelta(days=days)).isoformat(timespec="seconds"))


def searched(repository: Repository, platform: str, *, moment: str, status: str = "success") -> None:
    """A search of ``platform`` on record, started at ``moment``, that ended as ``status``."""
    with repository.connection() as connection:
        scan = connection.execute(
            "INSERT INTO scan_runs (trigger, status, started_at) VALUES ('test', 'completed', ?)", (moment,)
        ).lastrowid
        connection.execute(
            "INSERT INTO source_runs (scan_run_id, platform, status, started_at) VALUES (?, ?, ?, ?)",
            (scan, platform, status, moment),
        )
        connection.commit()


def nothing_was_searched(repository: Repository) -> None:
    with repository.connection() as connection:
        connection.execute("DELETE FROM source_runs")
        connection.commit()


@pytest.fixture(autouse=True)
def every_site_was_searched_a_moment_ago(repository: Repository) -> None:
    """As on any board a scan has run on: both sites here were searched, and
    answered, a minute before NOW. What decides each case below is then the
    rule it is about; the cases about a site nobody could ask say so."""
    for platform in ("Zillow", "Movoto"):
        searched(repository, platform, moment=at(1 / 1440))


class ZillowSearch:
    """Zillow as a scan asks it: answering, with none of the old homes, or
    not reached at all."""

    platform = "Zillow"
    mode = "automatic"
    search_url = "https://www.zillow.com/san-francisco-ca/rentals/"
    manual_reason = None
    detail_budget = 0

    def __init__(self, *, reachable: bool) -> None:
        self.reachable = reachable

    def search(self, client, preferences):
        if not self.reachable:
            raise httpx.ConnectError("[Errno 8] nodename nor servname provided, or not known")
        return []

    def enrich(self, client, listing):
        return listing


def the_disk_fills_up(repository: Repository, *, with_rows_left: int | None = None) -> None:
    """From now on the board refuses to delete a row -- or, given
    ``with_rows_left``, once only that many are left -- as a full disk would,
    part way through a sweep."""
    when = "" if with_rows_left is None else f"WHEN (SELECT COUNT(*) FROM listings) <= {int(with_rows_left)} "
    with repository.connection() as connection:
        connection.execute(
            f"CREATE TRIGGER disk_full BEFORE DELETE ON listings {when}"
            "BEGIN SELECT RAISE(ABORT, 'database or disk is full'); END"
        )
        connection.commit()


@pytest.fixture(params=["prune_old_listings", "retire_old_listings"])
def sweep(request: pytest.FixtureRequest, repository: Repository):
    """Retention either way in: on its own, or at the end of a scan with the
    archive. Returns how many rows it deleted."""
    if request.param == "prune_old_listings":
        return lambda: prune(repository)
    return lambda: retire(repository)[1]


# --------------------------------------------------------------------------
# what is old enough to go
# --------------------------------------------------------------------------


def test_an_aged_out_home_no_site_has_listed_for_121_days_is_deleted_and_one_unseen_for_119_is_kept(
    repository: Repository,
) -> None:
    aged_out(repository, "1", found=150, unseen=121)
    kept = aged_out(repository, "2", found=150, unseen=119)

    assert prune(repository) == 1
    assert remaining(repository) == {kept}


@pytest.mark.parametrize("date", ["found", "unseen", "confirmed"])
def test_each_of_the_three_dates_is_held_to_120_days_to_the_hour(repository: Repository, date: str) -> None:
    aged_out(repository, "1", **{"found": 200, "unseen": 200, "confirmed": 200, date: 120 + 1 / 24})
    an_hour_short = aged_out(repository, "2", **{"found": 200, "unseen": 200, "confirmed": 200, date: 120 - 1 / 24})

    assert prune(repository) == 1
    assert remaining(repository) == {an_hour_short}


def test_a_home_nothing_ever_confirmed_is_judged_by_when_a_search_last_returned_it(repository: Repository) -> None:
    """Rows stored before scans stamped a confirmation carry none."""
    aged_out(repository, "1", unseen=121, confirmed=None)
    kept = aged_out(repository, "2", unseen=119, confirmed=None)

    assert prune(repository) == 1
    assert remaining(repository) == {kept}


def test_a_home_a_search_still_returns_is_never_deleted_however_long_ago_it_was_found(
    repository: Repository,
) -> None:
    """Measured from first-found alone, a big building's card was deleted while
    every scan still returned it, and came back as a new home on the
    shortlist -- every 120 days, for as long as the site listed it. The
    browser import refreshes last_seen and leaves the last scan's
    confirmation as it was, so last_seen has to keep the home on its own."""
    still_listed = aged_out(repository, "1", found=300, unseen=1, confirmed=300)
    aged_out(repository, "2", found=300, unseen=200)

    assert prune(repository) == 1
    assert remaining(repository) == {still_listed}


def test_a_recent_confirmation_keeps_a_home_no_search_has_returned(repository: Repository) -> None:
    """Searches stopped returning it long ago, but its own page was read ten
    days ago and was still up."""
    confirmed = aged_out(repository, "1", unseen=200, confirmed=10)
    aged_out(repository, "2", unseen=200)

    assert prune(repository) == 1
    assert remaining(repository) == {confirmed}


@pytest.mark.parametrize("date", ["found", "unseen", "confirmed"])
@pytest.mark.parametrize(
    "value",
    ["", "17", "not a date", "09/01/2026", "2026-13-45T00:00:00+00:00", -30, -200],
    ids=["empty", "bare-number", "words", "month-first", "no-such-month", "30-days-ahead", "200-days-ahead"],
)
def test_a_date_that_is_not_a_date_or_lies_in_the_future_keeps_the_home(
    repository: Repository, date: str, value: Stamp
) -> None:
    """SQLite reads a bare "17" as a Julian day -- 4,700 BC -- so a malformed
    stamp would make a home look ancient; a date in the future is a clock
    that jumped, and has no age at all, however far ahead it is. Either way
    the row stays until its dates can be read."""
    odd = aged_out(repository, "1", **{"found": 200, "unseen": 200, "confirmed": 200, date: value})
    aged_out(repository, "2", unseen=200)

    assert prune(repository) == 1
    assert remaining(repository) == {odd}


def test_a_stamp_written_with_another_offset_is_judged_by_the_moment_it_names(repository: Repository) -> None:
    """Every writer here stamps UTC, but a date means the moment it names. Nine
    hours ahead, 20:00 on 21 May is 11:00 UTC: 120 days and an hour before
    NOW, though its text reads later than the cut-off. Eight hours behind,
    05:00 is 13:00 UTC, an hour short, though its text reads earlier."""
    aged_out(repository, "1", found=200, unseen="2026-05-21T20:00:00+09:00", confirmed=200)
    an_hour_short = aged_out(repository, "2", found=200, unseen="2026-05-21T05:00:00-08:00", confirmed=200)

    assert prune(repository) == 1
    assert remaining(repository) == {an_hour_short}


def test_a_home_whose_stored_details_are_not_json_is_kept_and_does_not_stop_the_sweep(
    repository: Repository,
) -> None:
    """Reading a confirmation out of text that is not JSON fails the whole
    statement, so one damaged row would otherwise end retention for the board."""
    damaged = aged_out(repository, "1", unseen=200, metadata_json="{not json")
    aged_out(repository, "2", unseen=200)

    assert prune(repository) == 1
    assert remaining(repository) == {damaged}


# --------------------------------------------------------------------------
# the user's own work is never deleted
# --------------------------------------------------------------------------


def two_copies(repository: Repository, **movoto: object) -> tuple[int, int]:
    """Zillow's and Movoto's copies of one flat, both aged out and unseen for
    200 days; ``movoto`` is what was done to Movoto's."""
    zillow = aged_out(repository, "1", address=BUCHANAN)
    other = aged_out(repository, "m105", platform="Movoto", address=BUCHANAN, **movoto)
    with repository.connection() as connection:
        keys = {row[0] for row in connection.execute(
            "SELECT home_key FROM listings WHERE id IN (?, ?)", (zillow, other)
        )}
    assert len(keys) == 1 and keys != {""}, "the two copies must be one home for this to test anything"
    return zillow, other


@pytest.mark.parametrize(
    "decision",
    [
        {"status": "saved", "status_reason": "user"},
        {"status": "saved", "status_reason": "aged"},
        {"note": "Tour Saturday"},
        {"note": "   "},
        {"status": "dismissed", "status_reason": "user"},
        {"status": "active", "status_reason": "user"},
        {"status": "dismissed", "status_reason": None},
    ],
    ids=["starred", "starred-whatever-reason-is-recorded", "noted", "noted-with-whitespace-only", "passed",
         "restored", "passed-before-reasons-were-kept"],
)
def test_no_copy_of_a_home_the_user_decided_about_is_deleted(repository: Repository, decision: dict) -> None:
    """A star is a star whatever reason sits beside it; each of the others is
    the only thing keeping the home in its case."""
    zillow, movoto = two_copies(repository, **decision)
    aged_out(repository, "2", unseen=200)

    assert prune(repository) == 1
    assert remaining(repository) == {zillow, movoto}


@pytest.mark.parametrize("act", ["star", "pass", "restore", "note"])
def test_whatever_the_user_does_to_an_aged_out_home_keeps_it_from_retention(repository: Repository, act: str) -> None:
    """Through the calls the page makes, so retention agrees with what D1
    records for each of them."""
    zillow, movoto = two_copies(repository)
    {
        "star": lambda: repository.set_listing_status(movoto, "saved"),
        "pass": lambda: repository.set_listing_status(movoto, "dismissed"),
        "restore": lambda: repository.set_listing_status(movoto, "active"),
        "note": lambda: repository.set_listing_note(movoto, "Worth a call about parking"),
    }[act]()
    aged_out(repository, "2", unseen=200)

    assert retire(repository) == (0, 1)
    assert remaining(repository) == {zillow, movoto}


@pytest.mark.parametrize(
    "own",
    [
        {"status": "active", "status_reason": None},
        {"status": "active", "status_reason": "aged"},
        {"status": "saved", "status_reason": "user"},
        {"status": "saved", "status_reason": None},
        {"status": "dismissed", "status_reason": "user"},
        {"status": "dismissed", "status_reason": None},
        {"status": "active", "status_reason": "user"},
        {"note": "Parking is extra"},
        {"note": "  "},
    ],
    ids=["never-aged", "on-the-board-whatever-reason-is-recorded", "starred", "starred-before-reasons-were-kept",
         "passed", "passed-before-reasons-were-kept", "restored", "noted", "noted-with-whitespace-only"],
)
def test_only_a_row_the_archive_aged_out_is_ever_deleted(repository: Repository, own: dict) -> None:
    """Aged out is dismissed by the archive: a row on the board is never
    deleted, whatever reason is recorded beside it."""
    row = home(repository, "1", found=300, **{"status": "dismissed", "status_reason": "aged", **own})
    aged_out(repository, "2", unseen=200)

    assert prune(repository) == 1
    assert remaining(repository) == {row}


def test_a_home_the_archive_could_not_age_is_not_deleted_by_the_sweep_either(repository: Repository) -> None:
    """Its first-found date is not a date, so the archive left it on the board;
    retention must not reach round the archive and delete it anyway."""
    odd = home(repository, "1", found="17", seen=300)
    aged_out(repository, "2", unseen=200)

    assert retire(repository) == (0, 1)
    assert remaining(repository) == {odd}
    assert state(repository, odd) == ("active", None)


@pytest.mark.parametrize("key", ["", None], ids=["empty", "null"])
def test_a_row_with_no_home_key_is_a_home_of_its_own(repository: Repository, key: str | None) -> None:
    """No key means nothing but the row itself is known to be that home: a
    star on one keyless row must not keep every keyless row on the board."""
    starred = aged_out(repository, "1", unseen=200, home_key=key, status="saved", status_reason="user")
    aged_out(repository, "2", unseen=200, home_key=key)

    assert prune(repository) == 1
    assert remaining(repository) == {starred}


def test_the_retention_window_is_a_parameter_that_cannot_be_shorter_than_the_archive(
    repository: Repository,
) -> None:
    thirty = aged_out(repository, "1", unseen=30)
    refused = (
        lambda: prune(repository, keep_days=20),
        lambda: retire(repository, keep_days=20),
        lambda: retire(repository, archive_days=40, keep_days=30),
        lambda: retire(repository, archive_days=0),
    )
    for sweep_too_eagerly in refused:
        with pytest.raises(ValueError):
            sweep_too_eagerly()

    assert remaining(repository) == {thirty}, "a refused sweep does nothing"
    assert prune(repository, keep_days=21) == 1


# --------------------------------------------------------------------------
# one pass, one transaction, and the scan that runs it
# --------------------------------------------------------------------------


def test_the_sweep_ages_out_and_deletes_in_one_pass(repository: Repository) -> None:
    ordinary = home(repository, "1", found=30, seen=0)
    resting = aged_out(repository, "2", unseen=60)
    aged_out(repository, "3", unseen=200)

    assert retire(repository) == (1, 1)

    assert state(repository, ordinary) == ("dismissed", "aged")
    assert remaining(repository) == {ordinary, resting}
    assert retire(repository) == (0, 0), "a second run has nothing left to do"


def test_a_home_no_sweep_reached_for_months_is_aged_out_now_and_deleted_a_season_later(
    repository: Repository,
) -> None:
    """The app was not run for half a year. The first sweep ages the home out,
    into the Archive where it can still be seen and restored; no site having
    listed it all that while, the sweep 120 days on deletes it -- not the same
    pass, which is how a home let go of by a click was deleted unseen."""
    listing_id = home(repository, "1", found=200)

    assert retire(repository) == (1, 0)
    assert state(repository, listing_id) == ("dismissed", "aged")
    assert later(repository, days=119) == (0, 0)
    assert later(repository, days=121) == (0, 1)
    assert remaining(repository) == set()


@pytest.mark.parametrize(
    "act", ["the star comes off", "the note is cleared", "restored, starred, and the star comes off"]
)
def test_a_home_let_go_of_after_months_unlisted_waits_in_the_archive_before_it_goes(
    repository: Repository, act: str
) -> None:
    """Review of WO-5: a home starred or noted months ago that no site lists
    any more, let go of with one click -- maybe a mis-click -- was aged out
    and deleted by the same sweep, and never reached the Archive: nothing
    was left to restore, and "Restore brings it back for good" was undone by
    a star put on and taken off. It now waits in the Archive like any other."""
    listing_id = aged_out(repository, "1", unseen=200)
    if act == "the star comes off":
        repository.set_listing_status(listing_id, "saved")
        repository.set_listing_status(listing_id, "active")
    elif act == "the note is cleared":
        repository.set_listing_note(listing_id, "Landlord said call back in spring")
        repository.set_listing_note(listing_id, "")
    else:
        repository.set_listing_status(listing_id, "active")
        repository.set_listing_status(listing_id, "saved")
        repository.set_listing_status(listing_id, "active")
    assert state(repository, listing_id) == ("active", None), "an ordinary home again"

    assert retire(repository) == (1, 0)
    assert state(repository, listing_id) == ("dismissed", "aged"), "in the Archive, where Restore reaches it"
    assert later(repository, days=119) == (0, 0)
    assert later(repository, days=121) == (0, 1)


@pytest.mark.parametrize(
    "searches",
    [
        pytest.param([], id="never searched"),
        pytest.param([("error", 1, "Zillow")], id="its search failed"),
        pytest.param(
            [("backoff", 3, "Zillow"), ("skipped", 2, "Zillow"), ("not_needed", 1, "Zillow")],
            id="passed over",
        ),
        pytest.param([("success", 201, "Zillow")], id="searched only before it was last seen"),
        pytest.param([("success", 1, "Movoto")], id="another site searched"),
    ],
)
def test_a_home_is_not_called_unlisted_by_a_site_nobody_asked(
    repository: Repository, searches: list[tuple[str, float, str]]
) -> None:
    """Review of WO-5: "no site lists it" was read from dates alone, so a
    check that reached no site -- offline after a month away, or a site
    blocking the app for a season -- deleted every old home that site might
    still list, and each came back as new when the site answered again."""
    nothing_was_searched(repository)
    listing_id = aged_out(repository, "1", unseen=200)
    for status, days_ago, platform in searches:
        searched(repository, platform, moment=at(days_ago), status=status)

    assert prune(repository) == 0
    assert remaining(repository) == {listing_id}

    searched(repository, "Zillow", moment=at(1))
    assert prune(repository) == 1, "and once its site answers without it, it goes"


def test_an_offline_check_after_months_away_keeps_the_board_and_the_next_one_tidies_it(
    repository: Repository, preferences
) -> None:
    """The laptop was shut for half a year; the catch-up check runs before the
    Wi-Fi is back, and reaches nothing. It deleted every home nobody starred.
    Now nothing goes until the site has answered."""
    nothing_was_searched(repository)
    old = {aged_out(repository, str(index), unseen=days_before_the_real_clock(200)) for index in range(3)}
    offline = Scanner(repository, lambda: preferences, [ZillowSearch(reachable=False)], detail_delay_seconds=0.0)

    offline.run_scan("startup_catchup")

    assert remaining(repository) == old, "a check that reached no site deleted nothing"

    Scanner(repository, lambda: preferences, [ZillowSearch(reachable=True)], detail_delay_seconds=0.0).run_scan(
        "scheduled"
    )

    assert remaining(repository) == set(), "Zillow answered, without them"


def test_a_delete_that_fails_leaves_the_archive_undone_too(repository: Repository) -> None:
    """One transaction for both, as WO-3 asked: a sweep that fails leaves
    nothing half done, and the next scan runs the whole of it again."""
    ordinary = home(repository, "1", found=30, seen=0)
    doomed = aged_out(repository, "2", unseen=200)
    the_disk_fills_up(repository)

    with pytest.raises(sqlite3.DatabaseError):
        retire(repository)

    assert state(repository, ordinary) == ("active", None)
    assert remaining(repository) == {ordinary, doomed}


def test_every_scan_ends_by_deleting_what_retention_no_longer_keeps(repository: Repository, preferences) -> None:
    aged_out(repository, "1", unseen=days_before_the_real_clock(200))
    resting = aged_out(repository, "2", unseen=days_before_the_real_clock(60))

    outcome = Scanner(repository, lambda: preferences, [], detail_delay_seconds=0.0).run_scan("test")

    assert outcome.status == "completed"
    assert remaining(repository) == {resting}


def test_a_browser_import_ends_by_deleting_them_too(repository: Repository, preferences) -> None:
    aged_out(repository, "1", unseen=days_before_the_real_clock(200))
    resting = aged_out(repository, "2", unseen=days_before_the_real_clock(60))

    outcome = Scanner(repository, lambda: preferences, [], detail_delay_seconds=0.0).import_candidates(
        "Zillow", "https://www.zillow.com/san-francisco-ca/rentals/", []
    )

    assert outcome.status == "completed"
    assert remaining(repository) == {resting}


def test_a_deleted_home_a_site_lists_again_comes_back_as_a_new_one(repository: Repository) -> None:
    """New to the board, so new to the shortlist, with three weeks ahead of it."""
    old = aged_out(repository, "1", address=BUCHANAN, unseen=200)
    assert prune(repository) == 1

    back = put(repository, card("1", address=BUCHANAN), seen_at=at(0))

    assert back > old, "a new row, not the old one brought back"
    assert state(repository, back) == ("active", None)
    with repository.connection() as connection:
        assert connection.execute("SELECT first_found FROM listings WHERE id = ?", (back,)).fetchone()[0] == at(0)
    shortlist = repository.query_listings(minimum_score=60, housing_kind="whole_unit")
    assert [row["id"] for row in shortlist] == [back]


def everything_but_the_listings(repository: Repository) -> dict[str, list[tuple]]:
    """Every other table, row for row. ``listings_version`` counts changes to
    the listings and is meant to move."""
    with repository.connection() as connection:
        names = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT IN ('listings', 'listings_version')"
            )
        ]
        return {
            name: [tuple(row) for row in connection.execute(f'SELECT * FROM "{name}" ORDER BY rowid')]
            for name in names
        }


def test_the_sweep_deletes_listings_and_touches_nothing_else(repository: Repository) -> None:
    run = repository.begin_scan("test")
    source_run = repository.begin_source_run(run, "Zillow", "https://www.zillow.com/san-francisco-ca/rentals/")
    repository.finish_source_run(source_run, "success", seen=2, added=2)
    repository.finish_scan(run, "completed", seen=2, added=2)
    repository.mark_source_initialized("zillow", "success")
    repository.record_source_coverage("HotPads", 1234)
    with repository.connection() as connection:
        connection.execute("INSERT INTO connector_states (connector_key, state) VALUES ('gmail', 'connected')")
        connection.commit()
    aged_out(repository, "1", unseen=200)
    kept = aged_out(repository, "2", unseen=60)
    before = everything_but_the_listings(repository)
    assert all(before[name] for name in ("scan_runs", "source_runs", "source_initializations",
                                          "source_coverage", "connector_states")), before

    assert retire(repository) == (0, 1)

    assert remaining(repository) == {kept}
    assert everything_but_the_listings(repository) == before
    with repository.connection() as connection:
        assert [tuple(row) for row in connection.execute("PRAGMA integrity_check")] == [("ok",)]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_no_remembered_answer_still_counts_a_home_the_sweep_deleted(repository: Repository) -> None:
    """The Archive's site filter, and whether it holds anything at all, are
    remembered until the listings change -- and a deletion is a change, or
    the Archive would offer a site whose only home had gone."""
    aged_out(repository, "m1", platform="Movoto", unseen=200)
    # Named, as ``has_homes`` beside it always has been: an aged-out home is
    # in the Archive and nowhere else, and a filter asked about the shortlist
    # is right to say it has no sites at all.
    assert repository.filter_options(0, "", view="all")[1] == ["Movoto"]
    assert repository.has_homes(0, "all", "")

    assert prune(repository) == 1

    assert repository.filter_options(0, "", view="all")[1] == []
    assert not repository.has_homes(0, "all", "")


# --------------------------------------------------------------------------
# a star racing the sweep
# --------------------------------------------------------------------------


def test_a_star_landing_while_the_sweep_runs_lands_whole_or_is_refused(repository: Repository) -> None:
    """Deciding and deleting are one statement under the write lock. A star
    lands wholly before the sweep, and the home keeps every copy; or wholly
    after it, and finds nothing, which the page answers with a 404. Never on
    a home the sweep then deletes, and never on half of one."""
    for index in range(100):
        address = f"{100 + 2 * index} Oak St #1"
        put(repository, card(f"z{index}", address=address))
        put(repository, card(f"m{index}", platform="Movoto", address=address))
    with repository.connection() as connection:
        connection.execute(
            "UPDATE listings SET first_found = ?, last_seen = ?, status = 'dismissed', status_reason = 'aged'",
            (at(200), at(200)),
        )
        connection.commit()
        target = [row[0] for row in connection.execute(
            "SELECT id FROM listings WHERE home_key = (SELECT home_key FROM listings WHERE source_id = 'z61')"
        )]
    assert len(target) == 2, "the home starred has two copies"
    barrier = threading.Barrier(2)
    landed: list[bool] = []

    def star() -> None:
        barrier.wait()
        landed.append(repository.set_listing_status(target[1], "saved"))

    thread = threading.Thread(target=star)
    thread.start()
    barrier.wait()
    retire(repository)
    thread.join()

    with repository.connection() as connection:
        left = {row["id"]: (row["status"], row["status_reason"])
                for row in connection.execute("SELECT id, status, status_reason FROM listings")}
    if landed == [True]:
        assert left == {listing_id: ("saved", "user") for listing_id in target}
    else:
        assert landed == [False] and left == {}


def test_a_star_landing_between_two_batches_of_a_backlog_keeps_its_home(
    repository: Repository, sweep, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A backlog is decided a batch at a time, never once for all of it: a
    star that lands after one batch keeps its home out of every later one."""
    monkeypatch.setattr(Repository, "PRUNE_BATCH", 10)
    homes = backlog(repository, 30)
    starred = homes[-1]
    # A click on the Archive page landing just as the first batch's last row goes.
    with repository.connection() as connection:
        connection.execute(
            f"CREATE TRIGGER a_star_lands AFTER DELETE ON listings WHEN OLD.id = {homes[9]} "
            f"BEGIN UPDATE listings SET status = 'saved', status_reason = 'user' WHERE id = {starred}; END"
        )
        connection.commit()

    assert sweep() == 29
    assert remaining(repository) == {starred}
    assert state(repository, starred) == ("saved", "user")


# --------------------------------------------------------------------------
# a backlog, a batch at a time
# --------------------------------------------------------------------------


def test_a_backlog_is_deleted_a_batch_at_a_time_until_none_is_left(
    repository: Repository, sweep, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A first sweep of a year is 130,000 rows: in one transaction, the write
    lock held for longer than a star waits for it."""
    monkeypatch.setattr(Repository, "PRUNE_BATCH", 10)
    backlog(repository, 55)

    assert sweep() == 55
    assert remaining(repository) == set()


def test_a_backlog_left_when_its_time_is_up_is_carried_on_by_the_next_sweep(
    repository: Repository, sweep, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Repository, "PRUNE_BATCH", 10)
    monkeypatch.setattr(Repository, "PRUNE_SECONDS", 0.0)
    homes = backlog(repository, 55)

    assert sweep() == 10
    assert remaining(repository) == set(homes[10:])
    assert sweep() == 10
    assert remaining(repository) == set(homes[20:])


def test_every_batch_of_a_backlog_is_its_own_transaction(
    repository: Repository, sweep, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure part way through -- a full disk, a busy board -- keeps every
    batch already done, for the next sweep to carry on from."""
    monkeypatch.setattr(Repository, "PRUNE_BATCH", 10)
    homes = backlog(repository, 55)
    the_disk_fills_up(repository, with_rows_left=35)  # the third batch fails on its first row

    with pytest.raises(sqlite3.DatabaseError):
        sweep()

    assert remaining(repository) == set(homes[20:])


# --------------------------------------------------------------------------
# a year of listings, against the rules read a second way
# --------------------------------------------------------------------------


YEAR_SECONDS = 365 * 86400
ODD_STAMPS = ["", "17", "not a date", "09/01/2026", "2026-13-45T00:00:00+00:00", at(-30)]
DATED = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def a_year_of_listings(repository: Repository, rng: random.Random, count: int) -> list[dict]:
    """``count`` rows first found across a year, in the shape a year leaves.

    Homes of one to three copies, some known by no key; each copy found on
    its own day, returned by searches for a few weeks and then not at all,
    and confirmed when last returned -- or never, earlier, since, or on a
    date that is not one. Most copies past three weeks aged out, a few the
    archive never reached. The user's own work is laid over rows of every
    age a copy at a time, and several times thicker than on any real board
    so that every carve-out is met many times over: stars, notes (some only
    whitespace), passes, restores, and passes from before reasons were kept.
    One row in a hundred has a date that is not a date, one in two hundred
    details that are not JSON.
    """
    rows: list[dict] = []
    number = 0

    def seconds_ago(seconds: int) -> str:
        # Never exactly on a line the rules draw, which floating point could
        # put on either side.
        if seconds % 86400 == 0:
            seconds += 1
        return (NOW - timedelta(seconds=seconds)).isoformat(timespec="seconds")

    while len(rows) < count:
        number += 1
        copies = rng.choices((1, 2, 3), weights=(70, 20, 10))[0]
        key = f"home:{number}" if copies > 1 else rng.choices((f"home:{number}", "", None), weights=(6, 3, 1))[0]
        for _ in range(copies):
            found = rng.randrange(0, YEAR_SECONDS)
            seen = max(0, found - int(rng.expovariate(1 / (20 * 86400))))
            confirmed = rng.choices(
                ("seen", "never", "earlier", "since", "odd"), weights=(80, 8, 5, 4, 3)
            )[0]
            metadata: dict = {"address": f"{number} Oak St"}
            if confirmed == "seen":
                metadata["last_verified_at"] = seconds_ago(seen)
            elif confirmed == "earlier":
                metadata["last_verified_at"] = seconds_ago(seen + rng.randrange(0, 60 * 86400))
            elif confirmed == "since":
                metadata["last_verified_at"] = seconds_ago(rng.randrange(0, seen + 1))
            elif confirmed == "odd":
                metadata["last_verified_at"] = rng.choice(ODD_STAMPS)
            if found >= 21 * 86400:
                status, reason = ("dismissed", "aged") if rng.random() < 0.93 else ("active", None)
            else:
                status, reason = "active", None
            decided = rng.random()
            if decided < 0.02:
                status, reason = "saved", "user"
            elif decided < 0.05:
                status, reason = "dismissed", "user"
            elif decided < 0.06:
                status, reason = "active", "user"
            elif decided < 0.07:
                status, reason = "dismissed", None
            row = {
                "id": len(rows) + 1,
                "source_id": f"y{len(rows) + 1}",
                "url": f"https://www.zillow.com/homedetails/x/y{len(rows) + 1}_zpid/",
                "first_found": seconds_ago(found),
                "last_seen": seconds_ago(seen),
                "status": status,
                "status_reason": reason,
                "note": rng.choice(("Tour Saturday", "  ")) if rng.random() < 0.015 else "",
                "home_key": key,
                "metadata_json": json.dumps(metadata),
            }
            if rng.random() < 0.01:
                row[rng.choice(("first_found", "last_seen"))] = rng.choice(ODD_STAMPS)
            if rng.random() < 0.005:
                row["metadata_json"] = "{not json"
            rows.append(row)
    rows = rows[:count]
    with repository.connection() as connection:
        connection.executemany(
            """INSERT INTO listings (id, platform, source_id, canonical_url, title, concern, score, housing_kind,
                   unit_type, first_found, last_seen, original_url, status, status_reason, note, home_key,
                   metadata_json)
               VALUES (:id, 'Zillow', :source_id, :url, 'A home', '', 70, 'whole_unit', 'one_bedroom',
                   :first_found, :last_seen, :url, :status, :status_reason, :note, :home_key, :metadata_json)""",
            rows,
        )
        connection.commit()
    return rows


def age_in_days(value: object) -> float | None:
    """How old a stored stamp is at NOW; None for one that is not a date."""
    if not isinstance(value, str) or not DATED.match(value):
        return None
    try:
        return (NOW - datetime.fromisoformat(value)).total_seconds() / 86400
    except ValueError:
        return None


class Verdict(NamedTuple):
    aged: set[int]
    deleted: set[int]
    left: dict[int, tuple[str, str | None]]
    # Rows old enough on every date, kept only because a copy of their home
    # is the user's.
    spared: set[int]


def what_the_rules_say(rows: list[dict]) -> Verdict:
    """D1 and retention read plainly, in Python.

    D1 first: an ordinary home past its three weeks leaves the shortlist,
    unless a copy of it is starred, noted, or passed or restored by the user.
    Then retention: a row the archive aged out goes when all three of its
    dates are 120 days old, unless a copy of its home is any of those, or a
    pass from before reasons were kept -- and not in the pass that aged it,
    which starts its time in the Archive. (Every row here is Zillow's, which
    was searched a moment before the sweep.)
    """
    def home_of(row: dict) -> str:
        return row["home_key"] or f"row:{row['id']}"

    def old(value: object, days: int) -> bool:
        age = age_in_days(value)
        return age is not None and age >= days

    kept_by_the_archive = {
        home_of(row) for row in rows
        if row["status"] == "saved" or row["note"] != "" or row["status_reason"] == "user"
    }
    aged = {
        row["id"] for row in rows
        if row["status"] == "active" and row["status_reason"] is None and row["note"] == ""
        and old(row["first_found"], 21) and home_of(row) not in kept_by_the_archive
    }
    after = {
        row["id"]: ("dismissed", "aged") if row["id"] in aged else (row["status"], row["status_reason"])
        for row in rows
    }
    owned = {
        home_of(row) for row in rows
        if after[row["id"]][0] == "saved" or row["note"] != "" or after[row["id"]][1] == "user"
        or (after[row["id"]][0] != "active" and after[row["id"]][1] != "aged")
    }
    deleted: set[int] = set()
    spared: set[int] = set()
    for row in rows:
        try:
            metadata = json.loads(row["metadata_json"])
        except ValueError:
            continue
        confirmed = metadata.get("last_verified_at")
        if (
            after[row["id"]] == ("dismissed", "aged") and row["id"] not in aged and row["note"] == ""
            and old(row["first_found"], 120) and old(row["last_seen"], 120)
            and (confirmed is None or old(confirmed, 120))
        ):
            (spared if home_of(row) in owned else deleted).add(row["id"])
    left = {listing_id: after[listing_id] for listing_id in after if listing_id not in deleted}
    return Verdict(aged, deleted, left, spared)


def test_a_year_of_listings_loses_exactly_what_the_rules_say_and_nothing_the_user_owns(
    repository: Repository,
) -> None:
    """The cases above met one at a time; here all at once, over the shape of
    a year -- homes whose copies disagree, keys shared and missing, a backlog
    several batches long -- and checked row for row against the same rules
    written a second way."""
    rows = a_year_of_listings(repository, random.Random(20260918), 9000)
    rules = what_the_rules_say(rows)
    user_owned = {
        row["id"] for row in rows
        if row["status"] == "saved" or row["note"] != "" or row["status_reason"] == "user"
    }
    # The mix has to meet what it claims to, or agreeing proves nothing.
    assert len(rules.deleted) > 2 * Repository.PRUNE_BATCH, "a backlog several batches long"
    assert len(rules.aged) > 100, "homes the archive never reached"
    assert len(rules.spared) > 100, "old rows kept only for a copy the user owns"
    assert len(user_owned) > 400

    assert retire(repository) == (len(rules.aged), len(rules.deleted))

    with repository.connection() as connection:
        board = {
            row["id"]: (row["status"], row["status_reason"])
            for row in connection.execute("SELECT id, status, status_reason FROM listings")
        }
    assert board == rules.left
    assert user_owned | rules.spared <= set(board), "nothing the user starred, noted, passed or restored is gone"
