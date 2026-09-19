"""D1: a home nobody acted on leaves the shortlist three weeks after it arrived.

Archived, never deleted: it moves to the Archive tab marked "aged out", and a
home that is starred, noted, or that the user passed or restored is never
touched -- those are the user's own work and the only irreplaceable data in
the app. The live board was 15 days old with no stars and no notes when this
was written, so every case here builds its own rows and dates.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sf_housing.database import Repository
from sf_housing.models import ListingCandidate, ScoreResult

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)


def at(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat(timespec="seconds")


@pytest.fixture
def repository(tmp_path: Path) -> Repository:
    instance = Repository(tmp_path / "housing.sqlite3")
    instance.initialize()
    return instance


def home(repository: Repository, slug: str, *, found: str | float, address: str | None = None,
         platform: str = "Zillow") -> int:
    url = {
        "Zillow": f"https://www.zillow.com/homedetails/x/{slug}_zpid/",
        "Movoto": f"https://www.movoto.com/san-francisco-ca/{slug}/for-rent/",
    }[platform]
    listing = ListingCandidate(
        platform=platform, source_id=slug, title=address or f"Home {slug}", original_url=url,
        price=3000, neighborhood="Mission", listing_type="Apartment",
        summary="Entire one bedroom apartment, 1 bed 1 bath.",
        metadata={"address": address} if address else {}, housing_kind="whole_unit", unit_type="one_bedroom",
    )
    listing_id, _ = repository.upsert_listing(listing, ScoreResult(80, ["Stored"], "", {}, eligibility="eligible"))
    stamp = at(found) if isinstance(found, (int, float)) else found
    with repository.connection() as connection:
        connection.execute("UPDATE listings SET first_found = ? WHERE id = ?", (stamp, listing_id))
        connection.commit()
    return listing_id


def state(repository: Repository, listing_id: int) -> tuple[str, str | None]:
    with repository.connection() as connection:
        row = connection.execute("SELECT status, status_reason FROM listings WHERE id = ?", (listing_id,)).fetchone()
    return row["status"], row["status_reason"]


def archive(repository: Repository, **kwargs) -> int:
    return repository.archive_stale_listings(now=NOW.isoformat(), **kwargs)


def test_a_home_22_days_on_the_board_is_aged_out_and_one_20_days_old_is_not(repository: Repository) -> None:
    old = home(repository, "1", found=22)
    young = home(repository, "2", found=20)

    assert archive(repository) == 1

    assert state(repository, old) == ("dismissed", "aged")
    assert state(repository, young) == ("active", None)


def test_a_starred_home_100_days_old_survives(repository: Repository) -> None:
    starred = home(repository, "1", found=100)
    repository.set_listing_status(starred, "saved")
    assert archive(repository) == 0
    assert state(repository, starred) == ("saved", "user")


def test_a_noted_home_survives(repository: Repository) -> None:
    noted = home(repository, "1", found=100)
    repository.set_listing_note(noted, "Asked about parking, waiting to hear")
    assert archive(repository) == 0
    assert state(repository, noted) == ("active", None)


def test_a_home_the_user_restored_stays_restored(repository: Repository) -> None:
    restored = home(repository, "1", found=40)
    assert archive(repository) == 1
    repository.set_listing_status(restored, "active")

    assert archive(repository) == 0
    assert state(repository, restored) == ("active", "user")


def test_a_home_the_user_passed_stays_passed_by_the_user(repository: Repository) -> None:
    """Otherwise the Passed tab would lose it to the archive's reason."""
    passed = home(repository, "1", found=40)
    repository.set_listing_status(passed, "dismissed")
    assert archive(repository) == 0
    assert state(repository, passed) == ("dismissed", "user")


def test_the_count_returned_is_the_rows_changed(repository: Repository) -> None:
    for index in range(5):
        home(repository, f"old{index}", found=30 + index)
    home(repository, "young", found=1)
    kept = home(repository, "kept", found=50)
    repository.set_listing_status(kept, "saved")

    changed = archive(repository)

    with repository.connection() as connection:
        aged = connection.execute("SELECT COUNT(*) FROM listings WHERE status_reason = 'aged'").fetchone()[0]
    assert changed == aged == 5
    assert archive(repository) == 0, "a second run has nothing left to do"


@pytest.mark.parametrize("stamp", ["", "not a date", "2459580.5", "17", "09/01/2026"])
def test_a_first_found_that_is_not_a_date_is_left_alone(repository: Repository, stamp: str) -> None:
    """SQLite reads a bare number as a Julian day -- "17" is 4,700 BC -- so a
    malformed stamp would otherwise look ancient and be archived."""
    odd = home(repository, "1", found=stamp)
    assert archive(repository) == 0
    assert state(repository, odd) == ("active", None)


def test_a_clock_that_jumped_backwards_archives_nothing(repository: Repository) -> None:
    """Homes found "in the future" have a negative age, which is no age."""
    future = home(repository, "1", found=-30)
    assert archive(repository) == 0
    assert state(repository, future) == ("active", None)


def test_each_listing_ages_by_its_own_first_found_date(repository: Repository) -> None:
    """D1 as decided: a listing is archived three weeks after it first appeared.
    A copy a site started listing today has not sat on anybody's shortlist --
    aged by the home's oldest copy, it was archived in the very scan that
    found it, even when it was the first copy that fitted the deal."""
    zillow = home(repository, "1", found=30, address="295 Buchanan St #105")
    movoto = home(repository, "m105", found=1, address="295 Buchanan St #105", platform="Movoto")

    assert archive(repository) == 1
    assert state(repository, zillow) == ("dismissed", "aged")
    assert state(repository, movoto) == ("active", None)
    shown = repository.query_listings(housing_kind="whole_unit", minimum_score=60)
    assert [row["id"] for row in shown] == [movoto], "the home is still on the shortlist, once"


def test_a_note_on_any_copy_keeps_the_whole_home(repository: Repository) -> None:
    zillow = home(repository, "1", found=30, address="295 Buchanan St #105")
    movoto = home(repository, "m105", found=30, address="295 Buchanan St #105", platform="Movoto")
    repository.set_listing_note(movoto, "Tour Saturday")
    assert archive(repository) == 0
    assert state(repository, zillow) == ("active", None)


def test_a_note_on_an_aged_out_home_brings_it_back(repository: Repository) -> None:
    aged = home(repository, "1", found=30)
    archive(repository)
    repository.set_listing_note(aged, "Actually, worth a call")
    assert state(repository, aged) == ("active", None)
    assert archive(repository) == 0, "and the note keeps it"


def test_the_window_is_a_parameter_with_a_floor(repository: Repository) -> None:
    home(repository, "1", found=10)
    assert archive(repository, keep_days=7) == 1
    with pytest.raises(ValueError):
        archive(repository, keep_days=0)


def test_aged_out_homes_are_in_the_archive_not_in_passed(repository: Repository) -> None:
    aged = home(repository, "1", found=30)
    passed = home(repository, "2", found=1)
    repository.set_listing_status(passed, "dismissed")
    archive(repository)

    in_passed = [item["id"] for item in repository.query_listings(view="dismissed", housing_kind="")]
    in_archive = [item["id"] for item in repository.query_listings(view="all", housing_kind="")]

    assert in_passed == [passed]
    assert aged in in_archive
    assert aged not in [item["id"] for item in repository.query_listings(view="active", housing_kind="", minimum_score=0)]


def test_a_star_landing_while_the_archive_runs_is_never_lost(repository: Repository) -> None:
    """The archive is one statement: a star lands before it (and the home is
    kept) or after it (and the star wins). Either way the home ends starred."""
    homes = [home(repository, f"h{index}", found=40) for index in range(200)]
    starred = homes[123]
    barrier = threading.Barrier(2)

    def star() -> None:
        barrier.wait()
        repository.set_listing_status(starred, "saved")

    thread = threading.Thread(target=star)
    thread.start()
    barrier.wait()
    archive(repository)
    thread.join()
    assert state(repository, starred) == ("saved", "user")


def test_a_failing_archive_never_fails_a_scan(repository: Repository, monkeypatch: pytest.MonkeyPatch) -> None:
    from sf_housing.scanner import Scanner

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(repository, "retire_old_listings", broken)
    scanner = Scanner(repository, lambda: None, [], detail_delay_seconds=0.0)
    assert scanner._retire_old_listings() == 0


def test_a_malformed_date_on_one_copy_does_not_make_the_home_ancient(repository: Repository) -> None:
    """"17" read as a Julian day is 4,700 BC: as the home's oldest copy it
    would age out the copy found yesterday."""
    odd = home(repository, "1", found="17", address="295 Buchanan St #105")
    fresh = home(repository, "m105", found=1, address="295 Buchanan St #105", platform="Movoto")
    assert archive(repository) == 0
    assert state(repository, fresh) == state(repository, odd) == ("active", None)
