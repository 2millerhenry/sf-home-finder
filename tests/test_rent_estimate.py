"""D3: a rent to rank by when the listing states none -- and nothing else.

Three hard rules from the decision: the estimate moves ranking only, never
eligibility or a budget filter; it is never shown, exported or stored; and
neighbourhood spellings are made one first, since "Tenderloin" (192 rows) and
"tenderloin" (157) split one sample in two.
"""

from __future__ import annotations

import csv
import io
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sf_housing.app import create_app
from sf_housing.database import Repository
from sf_housing.location import canonical_neighborhood
from sf_housing.models import ListingCandidate, ScoreResult
from sf_housing.rent_estimate import MINIMUM_SAMPLE, RentTable, size_of
from sf_housing.settings import Settings
from tests.conftest import TEST_PREFERENCES


# --------------------------------------------------------------------------
# the table
# --------------------------------------------------------------------------


def test_the_estimate_is_the_median_so_one_absurd_rent_does_not_move_it() -> None:
    """Financial District one-beds include a $349 row that wrecks an average."""
    rents = [349, 3800, 3850, 3893, 3900, 3950, 4000, 4100, 4200]
    table = RentTable.from_observations(("Financial District", "one_bedroom", rent) for rent in rents)
    assert table.estimate("Financial District", "one_bedroom") == 3900


def test_an_even_sample_breaks_the_tie_towards_the_dearer_rent() -> None:
    rents = [2000, 2100, 2200, 2300, 2400, 2500, 2600, 2700]
    table = RentTable.from_observations(("Tenderloin", "studio", rent) for rent in rents)
    assert table.estimate("Tenderloin", "studio") == 2400


def test_too_few_homes_in_an_area_falls_back_to_the_city_then_to_nothing() -> None:
    observations = [("Nob Hill", "one_bedroom", 3900)] * (MINIMUM_SAMPLE - 1)
    observations += [("Mission", "one_bedroom", 3000)] * MINIMUM_SAMPLE
    table = RentTable.from_observations(observations)
    assert table.estimate("Mission", "one_bedroom") == 3000
    # Nob Hill has seven: not enough alone, so the city's one-bedroom median.
    assert table.estimate("Nob Hill", "one_bedroom") == 3000
    assert table.estimate("Nob Hill", "four_bedroom") is None
    assert table.estimate("Nob Hill", None) is None


def test_one_area_under_two_spellings_is_one_sample() -> None:
    observations = [("Tenderloin", "studio", 2100)] * 4 + [("tenderloin", "studio", 2100)] * 4
    table = RentTable.from_observations(observations)
    assert table.estimate("TENDERLOIN", "studio") == 2100
    assert ("tenderloin", "studio") in table.by_area


def test_size_of() -> None:
    assert size_of("room", "one_bedroom") == "room"
    assert size_of("whole_unit", "studio") == "studio"
    assert size_of("whole_unit", None) is None
    assert size_of("unknown", "studio") is None


@pytest.mark.parametrize(
    "stored, expected",
    [
        ("tenderloin", "Tenderloin"),
        ("  Nob   hill ", "Nob Hill"),
        ("haight ashbury", "Haight-Ashbury"),
        ("Hayes Valley, CA", "Hayes Valley"),
        ("city of san francisco", "San Francisco"),
        ("SoMa", "SoMa"),
        ("SOMA / south beach", "SOMA / south beach"),
        ("", ""),
        (None, None),
    ],
)
def test_one_spelling_for_one_area(stored, expected) -> None:
    assert canonical_neighborhood(stored) == expected


# --------------------------------------------------------------------------
# on the board
# --------------------------------------------------------------------------


def room(slug: str, neighborhood: str, price: int | None, *, score: int = 70) -> tuple[ListingCandidate, ScoreResult]:
    listing = ListingCandidate(
        platform="Craigslist", source_id=slug, title=f"Private room {slug}",
        original_url=f"https://sfbay.craigslist.org/roo/d/{slug}.html", price=price,
        neighborhood=neighborhood, listing_type="Room/share",
        summary="A private room in a shared house, flexible lease, 4 roommates, communal garden.",
        metadata={"property_type": "house", "rooms_in_property": "4"},
    )
    return listing, ScoreResult(score, ["Stored"], "", {}, eligibility="eligible")


def test_one_home_listed_on_many_sites_is_one_observation_at_its_highest_rent(tmp_path: Path) -> None:
    repository = Repository(tmp_path / "housing.sqlite3")
    repository.initialize()
    for platform, slug, price in (("Zillow", "1", 3000), ("Movoto", "m1", 3400)):
        listing = ListingCandidate(
            platform=platform, source_id=slug, title="x",
            original_url=(f"https://www.zillow.com/homedetails/x/{slug}_zpid/" if platform == "Zillow"
                          else f"https://www.movoto.com/san-francisco-ca/{slug}/for-rent/"),
            price=price, neighborhood="tenderloin", summary="Entire one bedroom apartment.",
            metadata={"address": "295 Buchanan St #105"}, housing_kind="whole_unit", unit_type="one_bedroom",
        )
        repository.upsert_listing(listing, ScoreResult(80, [], "", {}, eligibility="eligible"))
    assert repository.rent_observations(60) == [("Tenderloin", "one_bedroom", 3400)]


@pytest.fixture
def board(tmp_path: Path):
    data_dir = tmp_path / "data"
    preferences_path = tmp_path / "preferences.yaml"
    preferences_path.write_text(TEST_PREFERENCES, encoding="utf-8")
    settings = Settings(data_dir=data_dir, preferences_path=preferences_path,
                        database_path=data_dir / "housing.sqlite3", log_path=data_dir / "test.log")
    repository = Repository(settings.database_path)
    repository.initialize()
    # What rooms cost: far over this deal's $2,000 in NOPA, one of its
    # preferred areas, and well inside it in Bernal Heights, which it only
    # accepts. Passed, so none of them is on the page to confuse what it shows.
    for index in range(MINIMUM_SAMPLE):
        for area, rent in (("NOPA", 3456), ("Bernal Heights", 1234)):
            listing, result = room(f"{area}{index}", area, rent)
            listing_id, _ = repository.upsert_listing(listing, result)
            repository.set_listing_status(listing_id, "dismissed")
    # Stored first and in the better area, so on its own score -- with rent
    # unknown for both -- the dear one ranks first.
    dear, _ = repository.upsert_listing(*room("dear-area", "NOPA", None))
    cheap, _ = repository.upsert_listing(*room("cheap-area", "Bernal Heights", None))
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    return application, repository, cheap, dear


def _rows(repository: Repository) -> list[dict]:
    with sqlite3.connect(repository.path) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute("SELECT * FROM listings ORDER BY id")]


def test_an_unpriced_home_is_ranked_by_what_its_area_usually_costs(board) -> None:
    application, repository, cheap, dear = board
    with TestClient(application) as client:
        page = client.get("/?housing=room&view=active&sort=score").text
    assert page.index("Private room cheap-area") < page.index("Private room dear-area")
    stored = {item["id"]: item["score"] for item in repository.query_listings(housing_kind="room", minimum_score=0)}
    assert stored[dear] > stored[cheap], "without the estimate the order would be the other way"
    assert "placed by what similar homes nearby rent for" in page
    assert 'aria-sort="descending"' not in page, "the rows are no longer in plain score order"


def test_the_estimate_is_never_shown_exported_or_stored(board) -> None:
    application, repository, cheap, dear = board
    before = _rows(repository)
    with TestClient(application) as client:
        page = client.get("/?housing=room&view=active&sort=score").text
        sheet = client.get("/listings.csv?housing=room&view=active&sort=score").text
        detail = client.get(f"/listings/{cheap}").text
    for text in (page, sheet, detail):
        assert "1,234" not in text and "1234" not in text
        assert "3,456" not in text and "3456" not in text
    table = {row["Address"]: row["Price"] for row in csv.DictReader(io.StringIO(sheet))}
    assert table["Private room cheap-area"] == table["Private room dear-area"] == ""
    after = _rows(repository)
    assert [(row["id"], row["price"], row["score"], row["eligibility"]) for row in after] == [
        (row["id"], row["price"], row["score"], row["eligibility"]) for row in before
    ]


def test_the_estimate_never_changes_who_is_on_the_shortlist_or_the_count(board) -> None:
    application, repository, cheap, dear = board
    with TestClient(application) as client:
        ranked = client.get("/?housing=room&view=active&sort=score").text
        by_price = client.get("/?housing=room&view=active&sort=price").text
    for text in (ranked, by_price):
        assert "Private room cheap-area" in text and "Private room dear-area" in text
    counts = repository.shortlist_counts([60], kinds=["room"])
    assert counts[60] == 2


def test_other_orders_and_views_are_left_alone(board) -> None:
    application, repository, cheap, dear = board
    with TestClient(application) as client:
        page = client.get("/?housing=room&view=active&sort=newest").text
    assert "placed by what similar homes nearby rent for" not in page


def test_a_home_another_site_prices_is_never_given_an_estimate(tmp_path: Path) -> None:
    """A note on the unpriced copy makes it the copy the home is shown by; the
    estimate must still see that another site states the rent."""
    from sf_housing.rent_estimate import ranking_order

    repository = Repository(tmp_path / "housing.sqlite3")
    repository.initialize()
    for platform, sid, url, price in (
        ("Zillow", "1", "https://www.zillow.com/homedetails/x/1_zpid/", 3400),
        ("Redfin", "r1", "https://www.redfin.com/CA/San-Francisco/x/unit-5/home/r1", None),
    ):
        listing = ListingCandidate(
            platform=platform, source_id=sid, title="100 Valencia St #5", original_url=url, price=price,
            neighborhood="Mission", summary="Entire studio apartment, whole place to yourself.",
            metadata={"address": "100 Valencia St #5"}, housing_kind="whole_unit", unit_type="studio",
        )
        listing_id, _ = repository.upsert_listing(listing, ScoreResult(88, ["Stored"], "", {}, eligibility="eligible"))
    repository.set_listing_note(listing_id, "Redfin has no rent, Zillow says $3,400")

    rows = repository.query_listings(minimum_score=0, housing_kind="whole_unit")
    assert [(row["price"], row["home_price"]) for row in rows] == [(None, 3400)]

    table = RentTable.from_observations([("Mission", "studio", 1000)] * MINIMUM_SAMPLE)
    _, estimated = ranking_order(rows, repository.home_candidates([rows[0]["id"]]), table,
                                 lambda listing: ScoreResult(100, [], "", {}))
    assert not estimated


def test_an_estimate_never_lifts_a_home_another_copy_holds_down() -> None:
    """One site's listing puts the home outside the deal; at the estimated rent
    that copy still scores low, and the home is placed by it -- exactly as a
    stated rent would place it."""
    from sf_housing.rent_estimate import ranking_order

    fits = ListingCandidate(platform="Zillow", source_id="1", title="x", original_url="https://x.test/1",
                            price=None, neighborhood="Mission", housing_kind="whole_unit", unit_type="studio")
    outside = ListingCandidate(platform="Movoto", source_id="m1", title="x", original_url="https://x.test/m1",
                               price=None, neighborhood="Mission", housing_kind="whole_unit", unit_type="studio",
                               building_units=120)
    held = {"id": 1, "price": None, "home_price": None, "score": 80, "home_score": 45}
    rival = {"id": 2, "price": 2000, "home_price": 2000, "score": 60, "home_score": 60}
    table = RentTable.from_observations([("Mission", "studio", 1000)] * MINIMUM_SAMPLE)

    def scored(listing):
        return ScoreResult(40 if listing.building_units else 95, [], "", {})

    order, estimated = ranking_order([held, rival], {1: [fits, outside]}, table, scored)

    assert estimated and [item["id"] for item in order] == [2, 1]


def test_an_estimate_is_not_pinned_to_a_lower_stored_score_of_a_copy_that_fits() -> None:
    """Both copies suit the deal; the home is placed by what they score at the
    estimated rent, not held at the lower score one had with no rent at all."""
    from sf_housing.rent_estimate import ranking_order

    a = ListingCandidate(platform="Zillow", source_id="1", title="x", original_url="https://x.test/1",
                         price=None, neighborhood="Mission", housing_kind="whole_unit", unit_type="studio")
    b = ListingCandidate(platform="Movoto", source_id="m1", title="x", original_url="https://x.test/m1",
                         price=None, neighborhood="Mission", housing_kind="whole_unit", unit_type="studio")
    home = {"id": 1, "price": None, "home_price": None, "score": 88, "home_score": 80}
    rival = {"id": 2, "price": 2000, "home_price": 2000, "score": 86, "home_score": 86}
    table = RentTable.from_observations([("Mission", "studio", 1000)] * MINIMUM_SAMPLE)

    order, _ = ranking_order([home, rival], {1: [a, b]}, table, lambda listing: ScoreResult(92, [], "", {}))

    assert [item["id"] for item in order] == [1, 2]
