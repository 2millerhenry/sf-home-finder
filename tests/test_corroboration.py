"""What other sources say: about this home, and about its building.

A reader on the listing page is deciding one thing: is this worth going to
look at. A single source cannot answer that alone, but the two questions other
sources can help with must be kept apart. The same flat on another site says
what that site charges for it. Another flat in the same building says only
that the building is real.

The old "Also listed elsewhere" matched by street address alone, so it offered
a different flat's rent in one building as "the figure to check" for this one
(WO-3 todo 6). These tests hold both halves: copies of this home, with their
rents, and other sources at the address, with none.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sf_housing.database import Repository
from sf_housing.models import ListingCandidate
from sf_housing.preferences import parse_preferences
from sf_housing.scoring import score_listing
from tests.conftest import TEST_PREFERENCES


@pytest.fixture
def repository(tmp_path: Path) -> Repository:
    instance = Repository(tmp_path / "housing.sqlite3")
    instance.initialize()
    return instance


PREFERENCES = parse_preferences(TEST_PREFERENCES)


def store(repository: Repository, *, platform: str, address: str | None, price: int | None,
          slug: str, title: str = "A building", status: str = "active",
          url: str | None = None, summary: str | None = None) -> int:
    listing = ListingCandidate(
        platform=platform,
        source_id=slug,
        title=title,
        original_url=url or f"https://example.test/{platform.lower().replace('.', '')}/{slug}",
        price=price,
        neighborhood="Potrero Hill",
        listing_type="Apartment",
        summary=summary or f"{title} on {platform}. Entire one bedroom apartment, 1 bed 1 bath.",
        metadata={"address": address} if address else {},
        housing_kind="whole_unit",
        unit_type="one_bedroom",
    )
    listing_id, _ = repository.upsert_listing(listing, score_listing(listing, PREFERENCES))
    if status != "active":
        repository.set_listing_status(listing_id, status)
    return listing_id


def unit_page(repository: Repository, platform: str, address: str, price: int, slug: str, **kwargs) -> int:
    """A card for one flat, from a site whose pages are one flat each."""
    urls = {
        "Zillow": f"https://www.zillow.com/homedetails/{slug}_zpid/",
        "AvalonBay": f"https://www.avaloncommunities.com/unit/{slug}",
    }
    return store(repository, platform=platform, address=address, price=price, slug=slug,
                 url=urls[platform], title=address, **kwargs)


# --------------------------------------------------------------------------
# the building: sources, never rents
# --------------------------------------------------------------------------


def test_another_flat_in_the_building_is_named_as_a_source_and_its_rent_is_not_offered(
    repository: Repository,
) -> None:
    """The regression. Two sites describing two different homes at one address
    were presented as one home, with the other's rent as this one's figure."""
    redfin = store(repository, platform="Redfin", address="800 Indiana St", price=None, slug="a")
    store(repository, platform="Rent.com", address="800 Indiana Street", price=4350, slug="b",
          title="Avalon Dogpatch")

    found = repository.elsewhere(redfin)

    assert found["building_sources"] == ["Rent.com"]
    assert found["copies"] == []


def test_the_building_match_survives_how_each_site_writes_an_address(repository: Repository) -> None:
    """"800 Indiana St" and "800 Indiana Street Unit 4" are one building."""
    subject = store(repository, platform="Redfin", address="800 Indiana St", price=None, slug="a")
    store(repository, platform="Rent.com", address="800 Indiana Street Unit 4", price=4350, slug="b")

    assert repository.elsewhere(subject)["building_sources"] == ["Rent.com"]


def test_a_different_building_is_not_corroboration(repository: Repository) -> None:
    subject = store(repository, platform="Redfin", address="800 Indiana St", price=None, slug="a")
    store(repository, platform="Rent.com", address="802 Indiana St", price=4350, slug="b")
    store(repository, platform="Zumper", address="800 Tennessee St", price=4100, slug="c")

    assert repository.elsewhere(subject) == {"copies": [], "building_sources": []}


def test_a_source_repeating_itself_is_not_a_second_opinion(repository: Repository) -> None:
    """Two cards from one site are that site listing two homes in a building,
    not two sources agreeing that the building exists."""
    subject = store(repository, platform="Redfin", address="800 Indiana St", price=None, slug="a")
    store(repository, platform="Redfin", address="800 Indiana St #2", price=4290, slug="b")

    assert repository.elsewhere(subject)["building_sources"] == []


def test_a_listing_never_corroborates_itself(repository: Repository) -> None:
    subject = store(repository, platform="Redfin", address="800 Indiana St", price=4290, slug="a")
    assert repository.elsewhere(subject) == {"copies": [], "building_sources": []}


def test_a_home_already_passed_on_is_not_offered_as_evidence(repository: Repository) -> None:
    subject = store(repository, platform="Redfin", address="800 Indiana St", price=None, slug="a")
    store(repository, platform="Rent.com", address="800 Indiana St", price=4350, slug="b",
          status="dismissed")

    assert repository.elsewhere(subject)["building_sources"] == []


def test_a_listing_with_no_address_asks_nothing(repository: Repository) -> None:
    subject = store(repository, platform="Craigslist", address=None, price=1800, slug="a")
    store(repository, platform="Rent.com", address="800 Indiana St", price=4350, slug="b")

    assert repository.elsewhere(subject) == {"copies": [], "building_sources": []}


def test_a_missing_listing_is_not_an_error(repository: Repository) -> None:
    assert repository.elsewhere(99999) == {"copies": [], "building_sources": []}


# --------------------------------------------------------------------------
# this home: copies, with their rents
# --------------------------------------------------------------------------


def test_the_same_flat_on_another_site_is_a_copy_with_its_own_rent(repository: Repository) -> None:
    zillow = unit_page(repository, "Zillow", "295 Buchanan St #105", 4150, "1001")
    avalon = unit_page(repository, "AvalonBay", "295 Buchanan Street, Unit 105", 4195, "b105")

    found = repository.elsewhere(zillow)

    assert [(copy["id"], copy["price"]) for copy in found["copies"]] == [(avalon, 4195)]
    # Named once, as the same home -- not again as "another home here".
    assert found["building_sources"] == []


def test_another_flat_at_one_address_is_never_a_copy(repository: Repository) -> None:
    """606 and 608 in one building are two homes, whatever the street says."""
    zillow = unit_page(repository, "Zillow", "1405 Franklin St #305", 4495, "2001")
    unit_page(repository, "AvalonBay", "1405 Franklin St #607", 4495, "b607")

    found = repository.elsewhere(zillow)

    assert found["copies"] == []
    assert found["building_sources"] == ["AvalonBay"]


# --------------------------------------------------------------------------
# what the reader sees
# --------------------------------------------------------------------------


def _page(tmp_path: Path, build) -> str:
    from fastapi.testclient import TestClient

    from sf_housing.app import create_app
    from tests.test_dashboard import app_settings

    settings = app_settings(tmp_path)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    live = Repository(settings.database_path)
    live.initialize()
    subject = build(live)
    with TestClient(application) as client:
        page = client.get(f"/listings/{subject}")
    assert page.status_code == 200
    return page.text


def test_the_page_names_the_building_s_other_sources_without_their_rent(tmp_path: Path) -> None:
    def build(live: Repository) -> int:
        subject = store(live, platform="Redfin", address="777 Tennessee St", price=None, slug="a",
                        title="777 Tennessee St")
        store(live, platform="Rent.com", address="777 Tennessee Street", price=4290, slug="b",
              title="Potrero 1010")
        return subject

    text = _page(tmp_path, build)

    assert "Other homes at this address" in text
    assert "1 other source (Rent.com) lists homes at this address" in text
    # A different flat's rent is not this one's figure to check.
    assert "$4,290" not in text
    assert "figure to check" not in text


def test_the_page_shows_this_home_on_other_sites_with_both_rents(tmp_path: Path) -> None:
    def build(live: Repository) -> int:
        subject = unit_page(live, "Zillow", "295 Buchanan St #105", 4150, "1001")
        unit_page(live, "AvalonBay", "295 Buchanan St Unit 105", 4195, "b105")
        return subject

    text = _page(tmp_path, build)

    assert "This home elsewhere" in text
    assert "$4,195/month" in text
    assert "The sites do not agree on the rent." in text


def test_the_page_says_nothing_when_no_other_source_has_it(tmp_path: Path) -> None:
    text = _page(
        tmp_path,
        lambda live: store(live, platform="Redfin", address="777 Tennessee St", price=4290, slug="a"),
    )

    assert "This home elsewhere" not in text
    assert "Other homes at this address" not in text


def test_the_page_counts_sources_and_not_rows(tmp_path: Path) -> None:
    """One site listing eight flats in a building is one source, not eight.

    A real board showed "30 other sources list this address" for 55 9th St,
    where twenty-nine of the thirty rows were Movoto and Zillow naming
    individual units. The app reads eighteen sites in total, so the sentence
    could not have been true of any address.
    """
    def build(live: Repository) -> int:
        subject = store(live, platform="Redfin", address="55 9th St", price=3930, slug="a")
        for unit, price in (("205", 3930), ("316", 3180), ("714", 3995)):
            store(live, platform="Movoto", address=f"55 9th St #{unit}", price=price,
                  slug=f"m{unit}", title=f"55 9th St #{unit}")
        return subject

    text = _page(tmp_path, build)

    assert "1 other source (Movoto) lists homes at this address" in text
    assert "3 other sources" not in text
