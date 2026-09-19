"""What a scan writes onto a stored home, and what it must not.

WO-3 todo 5: the price was refreshed on every scan but the summary was frozen
at first sight, so 1,414 of 8,254 active listings quoted a rent in their text
that differed from the price beside it ("$7,195/mo" above "$7,495 a month").
And the recheck pass, which reads a stored home's page again, wrote whatever
the page now showed onto the row -- a page re-let to another flat rewrote the
flat the user had starred.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from sf_housing.database import Repository
from sf_housing.models import ListingCandidate
from sf_housing.scanner import Scanner, _merged_metadata, _merged_summary


class OneCardSource:
    platform = "Good"
    mode = "automatic"
    search_url = "https://example.test/good"
    manual_reason = None
    detail_budget = 0

    def __init__(self, listing: ListingCandidate):
        self.listing = listing

    def search(self, client, preferences):
        return [self.listing]

    def enrich(self, client, listing):
        return listing


def room(summary: str, *, price: int = 1500, address: str | None = None) -> ListingCandidate:
    return ListingCandidate(
        platform="Good",
        source_id="1",
        title="Sunny private room in a house near Golden Gate Park",
        original_url="https://example.test/listing/1",
        price=price,
        neighborhood="NOPA",
        summary=summary,
        metadata={"property_type": "house", "rooms_in_property": "4", **({"address": address} if address else {})},
    )


def stored(repository: Repository) -> dict:
    with repository.connection() as connection:
        row = connection.execute("SELECT * FROM listings").fetchone()
    return dict(row)


def test_a_summary_that_is_the_same_sentence_with_new_numbers_is_refreshed(repository, preferences) -> None:
    Scanner(repository, lambda: preferences, [OneCardSource(room("Room for $1,500 a month, flexible lease."))],
            detail_delay_seconds=0.0).run_scan("test")
    Scanner(repository, lambda: preferences,
            [OneCardSource(room("Room for $1,450 a month, flexible lease.", price=1450))],
            detail_delay_seconds=0.0).run_scan("test")

    row = stored(repository)
    assert row["price"] == 1450
    assert row["summary"] == "Room for $1,450 a month, flexible lease."


def test_a_richer_stored_summary_is_not_replaced_by_a_thin_card(repository, preferences) -> None:
    """Detail pages say more than search cards; that has not changed."""
    detailed = "A sunny private room in a four-bedroom house, shared garden, quiet street, flexible lease."
    Scanner(repository, lambda: preferences, [OneCardSource(room(detailed))], detail_delay_seconds=0.0).run_scan("test")
    Scanner(repository, lambda: preferences, [OneCardSource(room("Room available."))],
            detail_delay_seconds=0.0).run_scan("test")
    assert stored(repository)["summary"] == detailed


def test_the_summary_rule() -> None:
    assert _merged_summary("1 bed from $2,895", "1 bed from $2,795", "T") == "1 bed from $2,795"
    assert _merged_summary("Asking $12,500 a month.", "Asking $9,950 a month.", "T") == "Asking $9,950 a month.", (
        "the same sentence with a shorter number is still the site's new number"
    )
    assert _merged_summary("1 bed from $3,200", "2 beds from $4,700", "T") == "2 beds from $4,700", (
        "a rewritten card no shorter than the old text is the site's current word"
    )
    assert _merged_summary("T", "A fresh description", "T") == "A fresh description", "a title is no summary"
    assert _merged_summary(None, "A fresh description", "T") == "A fresh description"
    assert _merged_summary("A long detail page text", "Card text", "T") == "A long detail page text"
    assert _merged_summary("Stored", None, "T") == "Stored"


def test_the_browser_import_keeps_its_own_card_s_words() -> None:
    """Its cards are the whole card the user's browser read, and on that path
    the fresh summary and type always won."""
    existing = {"price": 3000, "neighborhood": "Mission", "summary": "Charming 1BR, $3,000/month, available Sept.",
                "listing_type": "Apartment", "title": "T"}
    fresh = room("Now $3,400.", price=3400)
    fresh = replace(fresh, listing_type="Condo")
    merged = Scanner._merge_stored(fresh, existing, {}, fresh_first=True)
    assert (merged.summary, merged.listing_type) == ("Now $3,400.", "Condo")


def test_an_address_the_card_reads_thinner_keeps_the_stored_one() -> None:
    """The unit is what says which flat this is; a card that drops it is a
    thinner card, not a different home."""
    stored_metadata = {"address": "295 Buchanan St #105"}
    assert _merged_metadata(stored_metadata, {"address": "295 Buchanan St"})["address"] == "295 Buchanan St #105"
    assert _merged_metadata(stored_metadata, {"address": "Contact for address"})["address"] == "295 Buchanan St #105"
    assert _merged_metadata(stored_metadata, {"address": "295 Buchanan St #106"})["address"] == "295 Buchanan St #106"
    assert _merged_metadata(stored_metadata, {"price_note": "x"})["address"] == "295 Buchanan St #105"


def test_every_scan_ends_by_ageing_out_old_homes(repository, preferences) -> None:
    Scanner(repository, lambda: preferences, [OneCardSource(room("Room for $1,500 a month."))],
            detail_delay_seconds=0.0).run_scan("test")
    old = (datetime.now(UTC) - timedelta(days=30)).isoformat(timespec="seconds")
    with repository.connection() as connection:
        connection.execute("UPDATE listings SET first_found = ?", (old,))
        connection.commit()

    outcome = Scanner(repository, lambda: preferences, [], detail_delay_seconds=0.0).run_scan("test")

    assert outcome.status == "completed"
    row = stored(repository)
    assert (row["status"], row["status_reason"]) == ("dismissed", "aged")


def test_a_scan_whose_archive_fails_still_completes(repository, preferences, monkeypatch) -> None:
    def broken(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(repository, "retire_old_listings", broken)
    outcome = Scanner(repository, lambda: preferences, [OneCardSource(room("Room for $1,500."))],
                      detail_delay_seconds=0.0).run_scan("test")
    assert outcome.status == "completed"
    assert outcome.listings_added == 1


# --------------------------------------------------------------------------
# the recheck reads a stored home's page again
# --------------------------------------------------------------------------


class RelettingSource:
    """A source whose page for a stored home now shows another flat."""

    platform = "Zillow"
    mode = "automatic"
    search_url = "https://example.test/zillow"
    manual_reason = None
    detail_budget = 0
    recheck_budget = 5

    def __init__(self, address: str, price: int):
        self.address, self.price = address, price

    def search(self, client, preferences):
        return []

    def enrich(self, client, listing):
        return replace(listing, price=self.price, title=self.address,
                       metadata={**listing.metadata, "address": self.address})


def _starred_unit(repository: Repository, url: str) -> int:
    from sf_housing.models import ScoreResult

    listing = ListingCandidate(
        platform="Zillow", source_id="555", title="405 Laguna St #2", original_url=url, price=3200,
        neighborhood="Hayes Valley", summary="Entire one bedroom apartment.",
        metadata={"address": "405 Laguna St #2"}, housing_kind="whole_unit", unit_type="one_bedroom",
    )
    listing_id, _ = repository.upsert_listing(listing, ScoreResult(90, ["Stored"], "", {}, eligibility="eligible"))
    repository.set_listing_status(listing_id, "saved")
    return listing_id


def _recheck(repository: Repository, preferences, source) -> int:
    scanner = Scanner(repository, lambda: preferences, [source], detail_delay_seconds=0.0)
    return scanner._recheck_absent(object(), source, preferences, seen_source_ids=set(),
                                   deadline=scanner._clock() + 600)


def test_a_unit_page_now_showing_another_flat_marks_the_home_gone_and_rewrites_nothing(
    repository, preferences
) -> None:
    listing_id = _starred_unit(repository, "https://www.zillow.com/homedetails/x/555_zpid/")

    _recheck(repository, preferences, RelettingSource("409 Laguna St #4C", 7200))

    row = stored(repository)
    assert (row["title"], row["price"], row["status"]) == ("405 Laguna St #2", 3200, "saved")
    metadata = json.loads(row["metadata_json"])
    assert metadata["address"] == "405 Laguna St #2"
    assert metadata["verified_inactive"] is True


def test_a_building_page_featuring_another_unit_says_nothing_about_this_one(repository, preferences) -> None:
    _starred_unit(repository, "https://www.zillow.com/apartments/x/?zid=37.77--122.41")
    with repository.connection() as connection:
        connection.execute("UPDATE listings SET source_id = '37.77--122.41'")
        connection.commit()

    checked = _recheck(repository, preferences, RelettingSource("409 Laguna St #4C", 7200))

    row = stored(repository)
    metadata = json.loads(row["metadata_json"])
    assert checked == 0, "nothing was confirmed"
    assert "verified_inactive" not in metadata
    assert (row["title"], row["price"]) == ("405 Laguna St #2", 3200)


def test_a_starred_unit_whose_page_drops_its_unit_is_neither_rewritten_nor_called_gone(
    repository, preferences
) -> None:
    """Dropping the unit proves nothing either way: it is left as it was."""
    _starred_unit(repository, "https://www.zillow.com/homedetails/x/555_zpid/")

    checked = _recheck(repository, preferences, RelettingSource("405 Laguna St", 5400))

    row = stored(repository)
    metadata = json.loads(row["metadata_json"])
    assert checked == 0
    assert "verified_inactive" not in metadata
    assert (row["title"], row["price"], metadata["address"]) == ("405 Laguna St #2", 3200, "405 Laguna St #2")


def test_a_building_card_nobody_owns_is_still_refreshed_by_its_page(repository, preferences) -> None:
    """The guard is for the user's homes and for proof of a move; a building
    card's featured unit rotating is the card being current."""
    listing_id = _starred_unit(repository, "https://www.zillow.com/apartments/x/?zid=37.77--122.41")
    repository.set_listing_status(listing_id, "active")
    with repository.connection() as connection:
        connection.execute("UPDATE listings SET source_id = '37.77--122.41', status_reason = NULL")
        connection.commit()

    checked = _recheck(repository, preferences, RelettingSource("405 Laguna St #7", 3300))

    assert checked == 1
    assert json.loads(stored(repository)["metadata_json"])["address"] == "405 Laguna St #7"


def test_an_alert_copy_with_no_address_is_not_rewritten_by_a_page_that_names_one(repository, preferences) -> None:
    """The same page vouches for nothing once it names an address the row
    never had: the page may have been re-let to another flat."""
    from sf_housing.models import ScoreResult

    listing = ListingCandidate(
        platform="Zillow", source_id="555", title="A Zillow alert", original_url="https://www.zillow.com/homedetails/555_zpid/",
        price=3200, neighborhood="Hayes Valley", summary="Entire one bedroom apartment.", metadata={},
        housing_kind="whole_unit", unit_type="one_bedroom",
    )
    listing_id, _ = repository.upsert_listing(listing, ScoreResult(90, ["Stored"], "", {}, eligibility="eligible"))
    repository.set_listing_status(listing_id, "saved")

    checked = _recheck(repository, preferences, RelettingSource("295 Buchanan St #402", 7200))

    assert checked == 0
    assert "address" not in json.loads(stored(repository)["metadata_json"])


def test_a_description_quoting_another_rent_says_so_on_the_listing_page(tmp_path) -> None:
    from fastapi.testclient import TestClient

    from sf_housing.app import create_app
    from sf_housing.database import quoted_rent_mismatch
    from sf_housing.models import ScoreResult
    from tests.test_dashboard import app_settings

    assert quoted_rent_mismatch("Asking $7,495 a month. Available now.", 7195) == "$7,495"
    assert quoted_rent_mismatch("Asking $7,195 a month.", 7195) is None
    assert quoted_rent_mismatch("Deposit $500, parking $250/mo.", 7195) is None, "not a rent"
    assert quoted_rent_mismatch("Units from $3,100/mo and $3,400/mo.", 3400) is None
    # A range is read as a range: on the real board Trulia's "Advertised at
    # $4,640 - $5,895/mo" was flagged against its own $4,640.
    assert quoted_rent_mismatch("Advertised at $4,640 - $5,895/mo.", 4640) is None
    assert quoted_rent_mismatch("Advertised at $4,640 - $5,895/mo.", 6100) == "$4,640\u2013$5,895"
    # Money that is not the rent.
    assert quoted_rent_mismatch("Two bedrooms, $1,500/month per person.", 3000) is None
    assert quoted_rent_mismatch("Parking is $300/month, storage $150 a month.", 3000) is None
    assert quoted_rent_mismatch("Parking is available for an additional $600/month.", 3200) is None
    assert quoted_rent_mismatch("$600/month for garage parking.", 3200) is None
    assert quoted_rent_mismatch("Each roommate pays $2,100/month.", 4200) is None
    assert quoted_rent_mismatch("Two bedrooms, $2,100/mo per tenant.", 4200) is None
    # A room's own rent, stated per room, still agrees with it: on the real
    # board a Craigslist room at $1,075 ("$1,075 per month each, and 1 room
    # downstairs for $925") was flagged with the other room's rent.
    assert quoted_rent_mismatch(
        "2 rooms upstairs for $1,075 per month each, and 1 room downstairs for $925 per month", 1075
    ) is None
    assert quoted_rent_mismatch(
        "Rooms at $1,075/month each. The whole three-bedroom flat, with a garden and a view of the park, "
        "is $3,200 a month.", 1075
    ) is None
    # A concession's net-effective price is not a different rent.
    assert quoted_rent_mismatch("6 weeks free: net effective $2,650/mo on a 13-month lease.", 2895) is None
    # A building's card lists what its floorplans cost, not the card's rent.
    assert quoted_rent_mismatch("Its 3-bedroom homes start at $8,692 a month.", 4390, "building") is None

    settings = app_settings(tmp_path)
    live = Repository(settings.database_path)
    live.initialize()
    listing = ListingCandidate(
        platform="Movoto", source_id="m1", title="325 Octavia St", original_url="https://www.movoto.com/san-francisco-ca/m1/for-rent/",
        price=7195, neighborhood="Hayes Valley", summary="Listed as a 2-bedroom. Asking $7,495 a month.",
        metadata={"address": "325 Octavia St"}, housing_kind="whole_unit", unit_type="two_bedroom",
    )
    listing_id, _ = live.upsert_listing(listing, ScoreResult(80, ["Stored"], "", {}, eligibility="eligible"))
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    with TestClient(application) as client:
        page = client.get(f"/listings/{listing_id}").text
    assert "This description quotes $7,495 a month; the listing states $7,195." in page
