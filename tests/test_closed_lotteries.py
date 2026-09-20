"""A lottery whose applications have closed must never be on the shortlist.

The city's portal is the best source this app has -- real addresses, real
below-market rents, no scraping -- and it goes on publishing a listing for
months after the day applications were due. The board read the deadline and
printed it, and nothing else: on the author's own board 156 portal homes had a
deadline already behind them, all of them still counted as active, and one of
them sat on the shortlist at 92 with a raffle that had closed. In the owner's
words, "lots of good cheap DAHLIA listings but the raffle was 2 months ago".

The other half of every case here is the rule that outranks it: a deadline
nobody stated, or one this cannot read, removes nothing. Open waitlists
publish no deadline at all, and a home that might still be real is given the
benefit of the doubt.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from sf_housing.database import Repository
from sf_housing.models import ListingCandidate, ScoreResult
from sf_housing.preferences import Preferences
from sf_housing.scanner import Scanner, _merged_metadata
from sf_housing.sources import (
    SFHousingPortalSource,
    application_deadline_passed,
    today_in_san_francisco,
)

TODAY = date(2026, 9, 19)


# --------------------------------------------------------------------------
# The rule itself
# --------------------------------------------------------------------------


def stamp(day: str) -> str:
    """A deadline spelled the way the portal spells it."""
    return f"{day}T00:00:00.000+0000"


def test_a_deadline_on_the_day_it_is_read_has_not_passed() -> None:
    """The portal's windows close at 5pm Pacific, and it publishes only the day.

    Read as "the day is over the moment it begins", somebody filing on the
    last day the city gave them would find the home already gone from their
    shortlist.
    """
    assert application_deadline_passed(stamp("2026-09-19"), today=TODAY) is False
    assert application_deadline_passed(stamp("2026-09-18"), today=TODAY) is True
    assert application_deadline_passed(stamp("2026-09-20"), today=TODAY) is False


def test_the_last_day_lasts_until_the_end_of_it_in_san_francisco() -> None:
    """Judged in UTC, the last day of every lottery ended at 5pm Pacific.

    From 5pm Pacific onwards the UTC date is already tomorrow, which is
    exactly the evening somebody would be rushing an application in.
    """
    evening = datetime(2026, 9, 20, 1, 30, tzinfo=UTC)  # 6:30pm on the 19th in SF

    assert evening.date() == date(2026, 9, 20), "the premise: UTC has already turned over"
    assert today_in_san_francisco(evening) == TODAY
    assert (
        application_deadline_passed(stamp("2026-09-19"), today=today_in_san_francisco(evening))
        is False
    )


def test_a_deadline_written_in_the_portals_own_timezone_is_read_as_the_day_it_names() -> None:
    """Converting the whole stamp to Pacific would close every lottery a day early.

    The portal writes midnight with a +0000 offset for a day it means
    locally; that instant is 5pm the previous afternoon in San Francisco.
    """
    assert application_deadline_passed(stamp("2026-09-19"), today=TODAY) is False


def test_a_lottery_that_states_no_deadline_at_all_is_never_closed() -> None:
    """Open waitlists publish none, and absence of evidence never removes a home."""
    for nothing in (None, "", "   "):
        assert application_deadline_passed(nothing, today=TODAY) is False


def test_a_deadline_this_cannot_read_is_given_the_benefit_of_the_doubt() -> None:
    """A date in a shape nobody anticipated is not proof that anything closed."""
    for unreadable in ("soon", "2026-9-1", "Rolling", "0000", 12345):
        assert application_deadline_passed(unreadable, today=TODAY) is False


# --------------------------------------------------------------------------
# The portal feed: a closed lottery never becomes a candidate
# --------------------------------------------------------------------------


class Response:
    def __init__(self, records: list[dict]):
        self.records = records

    def json(self) -> dict:
        return {"listings": self.records}

    def raise_for_status(self) -> None:
        return None


class Client:
    def __init__(self, records: list[dict]):
        self.records = records

    def get(self, url, **kwargs):
        return Response(self.records)


def record(identifier: str, due: str | None) -> dict:
    entry = {
        "Id": identifier,
        "Name": f"The {identifier}",
        "Tenure": "Re-rental",
        "Building_City": "San Francisco",
        "Building_Street_Address": "1303 Larkin St",
        "unitSummaries": {"general": [{"unitType": "1 BR", "minMonthlyRent": 1125.0, "totalUnits": 3}]},
    }
    if due is not None:
        entry["Application_Due_Date"] = due
    return entry


@pytest.fixture(autouse=True)
def frozen_today(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hold the calendar still, so these cases still mean what they say next year."""
    monkeypatch.setattr("sf_housing.sources.today_in_san_francisco", lambda now=None: TODAY)


def portal(records: list[dict], preferences: Preferences):
    return SFHousingPortalSource().search(Client(records), preferences)


def test_a_lottery_whose_deadline_has_passed_never_becomes_a_candidate(
    preferences: Preferences,
) -> None:
    """A cheap below-market rent nobody can apply for is the most convincing ghost.

    The portal kept publishing these and the source kept turning every one of
    them into a candidate, printing the dead deadline in the summary as if it
    were a feature.
    """
    listings = portal([record("Fitzgerald", stamp("2026-07-16"))], preferences)

    assert listings == []


def test_every_unit_type_of_a_closed_lottery_goes_with_it(preferences: Preferences) -> None:
    """One portal record becomes one candidate per unit type; the deadline is the record's."""
    closed = record("Fitzgerald", stamp("2026-07-16"))
    closed["unitSummaries"]["general"].append({"unitType": "2 BR", "minMonthlyRent": 1400.0})

    assert portal([closed], preferences) == []


def test_a_lottery_whose_deadline_is_today_is_still_a_candidate(preferences: Preferences) -> None:
    """The portal states a day, not an hour; a date alone must not cost somebody their last day."""
    listings = portal([record("Larkin", stamp("2026-09-19"))], preferences)

    assert [listing.metadata["application_due_date"][:10] for listing in listings] == ["2026-09-19"]


def test_an_open_waitlist_with_no_deadline_is_still_a_candidate(preferences: Preferences) -> None:
    """Dropping these would be the false positive the whole principle forbids."""
    listings = portal([record("Waitlist", None)], preferences)

    assert len(listings) == 1
    assert listings[0].metadata["application_due_date"] is None


def test_an_open_lottery_says_out_loud_that_it_is_open(preferences: Preferences) -> None:
    """Silence here would leave a home hidden for ever once the city extended its deadline.

    The sweep marks a home gone when its stored deadline passes, and stored
    metadata outlives the card it came from. Without the portal restating that
    this listing is open, an extended deadline would arrive on a row that was
    still marked gone and the home would never come back.
    """
    listings = portal([record("Larkin", stamp("2026-09-20"))], preferences)

    assert listings and all(listing.metadata["verified_inactive"] is False for listing in listings)


def test_an_extended_deadline_lifts_the_mark_the_sweep_left(preferences: Preferences) -> None:
    """A lottery the city re-opens must come back, not stay buried by yesterday's verdict."""
    stored = {"application_due_date": stamp("2026-07-16"), "verified_inactive": True}
    fresh = portal([record("Larkin", stamp("2026-11-01"))], preferences)[0]

    merged = _merged_metadata(stored, fresh.metadata)

    assert merged["verified_inactive"] is False
    assert merged["application_due_date"][:10] == "2026-11-01"


# --------------------------------------------------------------------------
# The board: homes stored before any of this leave the shortlist too
# --------------------------------------------------------------------------


@pytest.fixture
def repository(tmp_path: Path) -> Repository:
    instance = Repository(tmp_path / "housing.sqlite3")
    instance.initialize()
    return instance


def stored_home(repository: Repository, slug: str, due: str | None, *, score: int = 92) -> int:
    listing = ListingCandidate(
        platform="SF Housing Portal",
        source_id=f"{slug}:1 br",
        title=f"The {slug} - 1 BR",
        original_url=f"https://housing.sfgov.org/listings/{slug}?unit=1-br",
        price=1125,
        neighborhood="Mission",
        listing_type="1 BR",
        summary="Below-market-rate 1 BR through the San Francisco housing portal.",
        metadata={"below_market_rate": True, "application_due_date": due},
        housing_kind="whole_unit",
        unit_type="one_bedroom",
    )
    listing_id, _ = repository.upsert_listing(
        listing, ScoreResult(score, ["Stored"], "", {}, eligibility="eligible")
    )
    return listing_id


def row(repository: Repository, listing_id: int) -> dict:
    with repository.connection() as connection:
        found = connection.execute(
            "SELECT score, eligibility, status, concern, metadata_json FROM listings WHERE id = ?",
            (listing_id,),
        ).fetchone()
    return dict(found)


def sweep(repository: Repository, today: date = TODAY) -> int:
    scanner = Scanner(repository, lambda: None, [], detail_delay_seconds=0.0)
    return scanner._close_expired_lotteries(today=today)


def test_a_stored_home_whose_deadline_has_passed_leaves_the_shortlist(
    repository: Repository,
) -> None:
    """Refusing the feed only stops them being added again; 156 were already stored.

    A source dropping a home proves nothing and must never remove it, so
    nothing would ever have taken these off the page. A deadline the source
    stated itself, now behind us, is not absence -- it is the source saying
    nobody can apply.
    """
    closed = stored_home(repository, "Fitzgerald", stamp("2026-07-16"))

    assert sweep(repository) == 1

    after = row(repository, closed)
    assert after["score"] <= 49
    assert after["eligibility"] == "ineligible"
    assert "2026-07-16" in after["concern"]


def test_a_closed_home_is_still_on_the_board_to_be_searched(repository: Repository) -> None:
    """Nothing is ever deleted: removed means it leaves the shortlist, not the app."""
    closed = stored_home(repository, "Fitzgerald", stamp("2026-07-16"))
    sweep(repository)

    assert row(repository, closed)["status"] == "active"
    assert [item["id"] for item in repository.query_listings(60, housing_kind="", view="all")] == [closed]
    assert repository.query_listings(60, housing_kind="", view="active") == []


def test_a_home_whose_deadline_is_today_keeps_its_place(repository: Repository) -> None:
    """The sweep runs twice a day; running at noon must not end somebody's last day."""
    today = stored_home(repository, "Larkin", stamp("2026-09-19"))

    assert sweep(repository) == 0

    assert row(repository, today)["score"] == 92
    assert [item["id"] for item in repository.query_listings(60, housing_kind="", view="active")] == [today]


def test_a_home_that_states_no_deadline_is_left_exactly_where_it_is(
    repository: Repository,
) -> None:
    """Most of the board states none at all; a sweep that touched them would empty it."""
    silent = stored_home(repository, "Craigslist-ish", None)

    assert sweep(repository) == 0

    assert row(repository, silent)["score"] == 92


def test_a_home_the_user_starred_keeps_its_star_when_its_lottery_closes(
    repository: Repository,
) -> None:
    """A star is the user's own work and the only irreplaceable data in the app."""
    starred = stored_home(repository, "Fitzgerald", stamp("2026-07-16"))
    repository.set_listing_status(starred, "saved")

    assert sweep(repository) == 1

    after = row(repository, starred)
    assert after["status"] == "saved"
    assert after["score"] <= 49


def test_a_home_already_known_gone_is_not_given_a_second_verdict(
    repository: Repository,
) -> None:
    """The reason a home is gone is the first proof of it, not the latest sweep's wording."""
    closed = stored_home(repository, "Fitzgerald", stamp("2026-07-16"))
    sweep(repository)
    before = row(repository, closed)

    assert sweep(repository) == 0
    assert row(repository, closed) == before


def test_a_row_whose_metadata_is_not_json_cannot_cost_the_sweep(
    repository: Repository,
) -> None:
    """One damaged row must not leave every closed lottery on the page."""
    damaged = stored_home(repository, "Damaged", stamp("2026-07-16"))
    closed = stored_home(repository, "Fitzgerald", stamp("2026-07-16"))
    with repository.connection() as connection:
        connection.execute("UPDATE listings SET metadata_json = ? WHERE id = ?", ("{not json", damaged))
        connection.commit()

    assert sweep(repository) == 1

    assert row(repository, closed)["eligibility"] == "ineligible"


# --------------------------------------------------------------------------
# Wiring: the sweep is part of a real check
# --------------------------------------------------------------------------


class QuietSource:
    platform = "Good"
    mode = "automatic"
    search_url = "https://example.test/good"
    manual_reason = None
    detail_budget = 0

    def search(self, client, preferences):
        return []

    def enrich(self, client, listing):
        return listing


def test_a_real_check_closes_the_lotteries_that_have_expired_since_the_last_one(
    repository: Repository, preferences: Preferences
) -> None:
    """A sweep nothing calls is a sweep that never runs.

    The whole point of the second half: the board fixes itself on the next
    check, without anybody having to ask it to.
    """
    closed = stored_home(repository, "Fitzgerald", stamp("2020-01-01"))
    scanner = Scanner(repository, lambda: preferences, [QuietSource()], detail_delay_seconds=0)

    scanner.run_scan("test")

    assert row(repository, closed)["eligibility"] == "ineligible"


def test_a_failing_sweep_never_fails_a_check(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shortlist being a day out of date is not worth losing a whole scan over."""
    import sqlite3

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(repository, "stated_application_deadlines", broken)
    scanner = Scanner(repository, lambda: None, [], detail_delay_seconds=0.0)

    assert scanner._close_expired_lotteries() == 0
