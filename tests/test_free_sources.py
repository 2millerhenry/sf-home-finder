"""The two zero-setup sources added so a new install sees more than Craigslist.

Both fixtures are real captured payloads, trimmed: the SF portal one keeps four
rentals plus one ownership record, and the Apartment List one keeps the page's
real schema.org block.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from sf_housing.classification import ROOM, UNKNOWN, WHOLE_UNIT
from sf_housing.preferences import Preferences, parse_preferences
from sf_housing.sources import ApartmentListSource, SFHousingPortalSource, SourceError
from tests.conftest import TEST_PREFERENCES


FIXTURES = Path(__file__).parent / "fixtures"

# Every case here reads a real capture whose lotteries have since closed.
pytestmark = pytest.mark.usefixtures("portal_lotteries_open")


class FakeResponse:
    def __init__(self, payload: object = None, text: str = "", status: int = 200):
        self._payload = payload
        self.text = text
        self.status = status
        # What the shared refusal check reads, as a real response carries it.
        self.status_code = status

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload

    def raise_for_status(self):
        if self.status >= 400:
            raise AssertionError(f"unexpected status {self.status}")


class FakeClient:
    def __init__(self, response: FakeResponse):
        self.response = response
        self.requested: list[str] = []

    def get(self, url, **kwargs):
        self.requested.append(url)
        return self.response


@pytest.fixture
def preferences() -> Preferences:
    return parse_preferences(TEST_PREFERENCES)


def portal_payload() -> dict:
    return json.loads((FIXTURES / "sf_portal_listings.json").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# SF Housing Portal (DAHLIA)
# --------------------------------------------------------------------------


def test_portal_returns_rentals_with_real_rents(preferences: Preferences) -> None:
    source = SFHousingPortalSource()
    client = FakeClient(FakeResponse(payload=portal_payload()))

    listings = source.search(client, preferences)

    assert listings, "the fixture contains rentals, so the parser must return some"
    assert client.requested == [SFHousingPortalSource.api_url]
    for listing in listings:
        assert listing.platform == "SF Housing Portal"
        assert listing.price and listing.price > 0
        assert listing.original_url.startswith("https://housing.sfgov.org/listings/")
        assert listing.metadata["below_market_rate"] is True


def test_portal_skips_ownership_records(preferences: Preferences) -> None:
    """Resale and new-sale rows are homes to buy, not rent."""
    payload = portal_payload()
    tenures = {str(r.get("Tenure")) for r in payload["listings"]}
    assert any("ale" in t for t in tenures), "fixture must contain an ownership row to prove the filter"

    listings = SFHousingPortalSource().search(FakeClient(FakeResponse(payload=payload)), preferences)

    sale_ids = {r["Id"] for r in payload["listings"] if "rental" not in str(r.get("Tenure", "")).casefold()}
    for listing in listings:
        assert listing.source_id.split(":")[0] not in sale_ids


def test_portal_skips_listings_outside_san_francisco(preferences: Preferences) -> None:
    payload = portal_payload()
    for record in payload["listings"]:
        record["Building_City"] = "Daly City"

    assert SFHousingPortalSource().search(FakeClient(FakeResponse(payload=payload)), preferences) == []


def test_portal_carries_a_timestamp_so_the_first_scan_can_bound_it(preferences: Preferences) -> None:
    """This is one of the few sources that publishes a usable date."""
    from datetime import datetime

    listings = SFHousingPortalSource().search(
        FakeClient(FakeResponse(payload=portal_payload())), preferences
    )

    stamped = [l for l in listings if l.metadata.get("listing_timestamp")]
    assert stamped, "the portal publishes LastModifiedDate; it must survive into metadata"
    for listing in stamped:
        # Must be parseable by Scanner._within_initial_window.
        datetime.fromisoformat(str(listing.metadata["listing_timestamp"]))


def test_portal_maps_its_own_unit_vocabulary(preferences: Preferences) -> None:
    listings = SFHousingPortalSource().search(
        FakeClient(FakeResponse(payload=portal_payload())), preferences
    )
    by_type = {str(l.metadata.get("sf_portal_unit_type")).casefold(): l for l in listings}

    if "studio" in by_type:
        assert by_type["studio"].metadata["unit_type"] == "studio"
        assert by_type["studio"].housing_kind == WHOLE_UNIT
    if "1 br" in by_type:
        assert by_type["1 br"].metadata["unit_type"] == "1 bedroom"
    if "sro" in by_type:
        # A single-room-occupancy is a room, not a home to split.
        assert by_type["sro"].housing_kind == ROOM
        assert "unit_type" not in by_type["sro"].metadata


def test_portal_keeps_the_cheapest_rent_per_unit_type() -> None:
    """A building can publish one unit type in both buckets; keep the better rent."""
    payload = {
        "listings": [
            {
                "Id": "a1",
                "Name": "Test Commons",
                "Tenure": "Re-rental",
                "Building_City": "San Francisco",
                "Building_Street_Address": "1 Test St",
                "unitSummaries": {
                    "general": [{"unitType": "1 BR", "minMonthlyRent": 2400.0, "totalUnits": 2}],
                    "reserved": [{"unitType": "1 BR", "minMonthlyRent": 1500.0, "totalUnits": 1}],
                },
            }
        ]
    }
    listings = SFHousingPortalSource().search(
        FakeClient(FakeResponse(payload=payload)), parse_preferences(TEST_PREFERENCES)
    )

    assert len(listings) == 1, "one unit type must not become two rows"
    assert listings[0].price == 1500


def test_portal_ignores_units_without_a_rent() -> None:
    payload = {
        "listings": [
            {
                "Id": "a1",
                "Name": "No Rent Published",
                "Tenure": "New rental",
                "Building_City": "San Francisco",
                "unitSummaries": {"general": [{"unitType": "2 BR", "minMonthlyRent": None}]},
            }
        ]
    }

    assert SFHousingPortalSource().search(
        FakeClient(FakeResponse(payload=payload)), parse_preferences(TEST_PREFERENCES)
    ) == []


def test_portal_reports_a_changed_api_instead_of_returning_nothing(preferences: Preferences) -> None:
    """Silence would look identical to 'no homes available'."""
    with pytest.raises(SourceError, match="listings array"):
        SFHousingPortalSource().search(FakeClient(FakeResponse(payload={"data": []})), preferences)

    with pytest.raises(SourceError, match="non-JSON"):
        SFHousingPortalSource().search(FakeClient(FakeResponse(payload=None)), preferences)


def test_portal_accepts_an_empty_but_valid_response(preferences: Preferences) -> None:
    """No open lotteries is a real answer, not a failure."""
    assert SFHousingPortalSource().search(FakeClient(FakeResponse(payload={"listings": []})), preferences) == []


# --------------------------------------------------------------------------
# Apartment List
# --------------------------------------------------------------------------


def apartment_list_document() -> str:
    return (FIXTURES / "apartment_list_search.html").read_text(encoding="utf-8")


def test_apartment_list_reads_buildings_and_starting_rent(preferences: Preferences) -> None:
    source = ApartmentListSource()
    client = FakeClient(FakeResponse(text=apartment_list_document()))

    listings = source.search(client, preferences)

    assert listings
    for listing in listings:
        assert listing.platform == "Apartment List"
        assert listing.original_url.startswith("https://www.apartmentlist.com/")
        assert listing.metadata["building_listing"] is True


def test_apartment_list_knows_the_workflow_but_not_yet_the_size(preferences: Preferences) -> None:
    """Apartment List rents whole apartments, never a room in someone's home.

    So the workflow is known from the search feed, while the bedroom count -
    which decides *which* shortlist a building belongs in - stays unknown until
    the building's own page is read.
    """
    listings = ApartmentListSource().search(
        FakeClient(FakeResponse(text=apartment_list_document())), preferences
    )

    assert listings
    for listing in listings:
        assert listing.housing_kind == WHOLE_UNIT
        assert "bedrooms" not in listing.metadata
        assert "unconfirmed" in listing.summary


def test_apartment_list_reports_a_format_change(preferences: Preferences) -> None:
    with pytest.raises(SourceError, match="structured building data"):
        ApartmentListSource().search(
            FakeClient(FakeResponse(text="<html><body>nothing here</body></html>")), preferences
        )


def test_apartment_list_survives_malformed_structured_data(preferences: Preferences) -> None:
    document = (
        '<html><head>'
        '<script type="application/ld+json">{not json at all}</script>'
        '<script type="application/ld+json">'
        '{"@type":"Product","name":"Real Building","url":"https://www.apartmentlist.com/ca/x",'
        '"offers":{"@type":"AggregateOffer","lowPrice":2100,"highPrice":3400}}'
        "</script></head></html>"
    )

    listings = ApartmentListSource().search(FakeClient(FakeResponse(text=document)), parse_preferences(TEST_PREFERENCES))

    assert len(listings) == 1
    assert listings[0].price == 2100
    assert listings[0].metadata["price_high"] == 3400
    assert "$2,100 to $3,400" in listings[0].summary


def test_apartment_list_skips_entries_without_a_usable_url() -> None:
    document = (
        '<html><head><script type="application/ld+json">'
        '[{"@type":"Product","name":"No URL"},'
        '{"@type":"Product","name":"Insecure","url":"http://example.test/x"},'
        '{"@type":"Product","name":"Good","url":"https://www.apartmentlist.com/ca/good"}]'
        "</script></head></html>"
    )

    listings = ApartmentListSource().search(
        FakeClient(FakeResponse(text=document)), parse_preferences(TEST_PREFERENCES)
    )

    assert [l.title for l in listings] == ["Good"]


# --------------------------------------------------------------------------
# wiring
# --------------------------------------------------------------------------


def test_both_sources_are_automatic_and_in_the_default_set() -> None:
    from sf_housing.sources import default_sources

    platforms = {s.platform: s for s in default_sources()}

    for name in ("SF Housing Portal", "Apartment List"):
        assert name in platforms, f"{name} must work on a fresh install with no setup"
        assert platforms[name].mode == "automatic"
        assert platforms[name].manual_reason is None


def test_two_unit_types_in_one_building_survive_storage(tmp_path: Path) -> None:
    """Regression: the portal serves one page per building, not per unit.

    With a shared URL the repository's canonical-URL dedup collapsed every unit
    type in a building into a single row, so a cheap studio vanished behind the
    two-bedroom that happened to be written last.
    """
    from sf_housing.database import Repository, canonicalize_url
    from sf_housing.models import ScoreResult

    payload = {
        "listings": [
            {
                "Id": "b7",
                "Name": "Garden Court",
                "Tenure": "Re-rental",
                "Building_City": "San Francisco",
                "Building_Street_Address": "1637 15th St",
                "unitSummaries": {
                    "general": [
                        {"unitType": "Studio", "minMonthlyRent": 1626.0, "totalUnits": 3},
                        {"unitType": "1 BR", "minMonthlyRent": 1847.0, "totalUnits": 3},
                    ]
                },
            }
        ]
    }

    listings = SFHousingPortalSource().search(
        FakeClient(FakeResponse(payload=payload)), parse_preferences(TEST_PREFERENCES)
    )
    assert len(listings) == 2

    canonical = {canonicalize_url(l.original_url) for l in listings}
    assert len(canonical) == 2, "each unit type needs a distinct canonical URL"

    repository = Repository(tmp_path / "housing.sqlite3")
    repository.initialize()
    for listing in listings:
        repository.upsert_listing(listing, ScoreResult(70, ["fits"], "", {}))

    import sqlite3

    stored = sqlite3.connect(tmp_path / "housing.sqlite3").execute(
        "SELECT price FROM listings ORDER BY price"
    ).fetchall()
    assert [row[0] for row in stored] == [1626, 1847], "both homes must survive the write"


# --------------------------------------------------------------------------
# Zumper
# --------------------------------------------------------------------------


def zumper_search() -> str:
    return (FIXTURES / "zumper_search.html").read_text(encoding="utf-8")


def zumper_building() -> str:
    return (FIXTURES / "zumper_building.html").read_text(encoding="utf-8")


def test_zumper_reads_the_published_search_feed(preferences: Preferences) -> None:
    from sf_housing.sources import ZumperSource

    listings = ZumperSource().search(FakeClient(FakeResponse(text=zumper_search())), preferences)

    assert listings
    for listing in listings:
        assert listing.platform == "Zumper"
        assert listing.original_url.startswith("https://www.zumper.com/")
        assert listing.housing_kind == WHOLE_UNIT


def test_zumper_carries_the_posting_date(preferences: Preferences) -> None:
    """Zumper is the only free non-government source that dates its listings."""
    from datetime import date

    from sf_housing.sources import ZumperSource

    listings = ZumperSource().search(FakeClient(FakeResponse(text=zumper_search())), preferences)

    stamped = [l for l in listings if l.metadata.get("listing_timestamp")]
    assert stamped, "datePosted must survive into metadata or the first-run window cannot use it"
    for listing in stamped:
        date.fromisoformat(str(listing.metadata["listing_timestamp"]))


def test_zumper_bedroom_count_drives_the_home_size(preferences: Preferences) -> None:
    from sf_housing.classification import STUDIO, classify_listing
    from sf_housing.sources import ZumperSource

    listings = ZumperSource().search(FakeClient(FakeResponse(text=zumper_search())), preferences)
    studios = [l for l in listings if str(l.metadata.get("bedrooms")) == "0"]

    assert studios, "the fixture keeps a zero-bedroom entry"
    assert classify_listing(studios[0]).unit_type == STUDIO


def test_zumper_amenities_reach_the_text_the_scorer_reads(preferences: Preferences) -> None:
    """Amenities are matched from listing text, not from a metadata key."""
    from sf_housing.sources import ZumperSource

    listings = ZumperSource().search(FakeClient(FakeResponse(text=zumper_search())), preferences)
    with_amenities = [l for l in listings if l.metadata.get("zumper_amenities")]

    assert with_amenities
    listing = with_amenities[0]
    assert "Amenities:" in listing.summary
    assert str(listing.metadata["zumper_amenities"][0]) in listing.summary


def test_zumper_says_when_rent_is_unknown_instead_of_inventing_one(preferences: Preferences) -> None:
    from sf_housing.sources import ZumperSource

    listings = ZumperSource().search(FakeClient(FakeResponse(text=zumper_search())), preferences)
    unpriced = [l for l in listings if l.price is None]

    assert unpriced, "most buildings publish no rent on the search page"
    for listing in unpriced:
        assert "unconfirmed" in listing.summary


def test_zumper_enrich_recovers_the_rent_from_the_building_page(preferences: Preferences) -> None:
    from sf_housing.sources import ZumperSource

    source = ZumperSource()
    listings = source.search(FakeClient(FakeResponse(text=zumper_search())), preferences)
    unpriced = next(l for l in listings if l.price is None)

    client = FakeClient(FakeResponse(text=zumper_building()))
    enriched = source.enrich(client, unpriced)

    assert enriched.price and enriched.price > 0
    assert "unconfirmed" not in enriched.summary
    assert enriched.metadata["zumper_price_source"] == "building page"


def test_zumper_enrich_does_not_refetch_an_already_priced_listing(preferences: Preferences) -> None:
    from sf_housing.sources import ZumperSource

    source = ZumperSource()
    listings = source.search(FakeClient(FakeResponse(text=zumper_search())), preferences)
    priced = next(l for l in listings if l.price)

    client = FakeClient(FakeResponse(text=zumper_building()))
    assert source.enrich(client, priced) is priced
    assert client.requested == [], "a listing that already has a rent must cost no request"


def test_zumper_drops_results_outside_san_francisco(preferences: Preferences) -> None:
    import json as _json

    from sf_housing.sources import ZumperSource

    from bs4 import BeautifulSoup

    node = BeautifulSoup(zumper_search(), "html.parser").select_one('script[type="application/ld+json"]')
    payload = _json.loads(node.string)
    for entry in payload["mainEntity"]["itemListElement"]:
        entry["item"]["about"]["address"]["addressLocality"] = "Oakland"
    moved = f'<html><head><script type="application/ld+json">{_json.dumps(payload)}</script></head></html>'

    assert ZumperSource().search(FakeClient(FakeResponse(text=moved)), preferences) == []


def test_zumper_reports_a_format_change_rather_than_going_quiet(preferences: Preferences) -> None:
    from sf_housing.sources import ZumperSource

    with pytest.raises(SourceError, match="structured search results"):
        ZumperSource().search(
            FakeClient(FakeResponse(text="<html><body>redesigned</body></html>")), preferences
        )


def apartment_list_building() -> str:
    return (FIXTURES / "apartment_list_building.html").read_text(encoding="utf-8")


def test_apartment_list_enrich_completes_the_building(preferences: Preferences) -> None:
    """The search feed alone cannot place a building in a shortlist; the page can."""
    source = ApartmentListSource()
    listing = source.search(FakeClient(FakeResponse(text=apartment_list_document())), preferences)[0]
    assert "bedrooms" not in listing.metadata

    enriched = source.enrich(FakeClient(FakeResponse(text=apartment_list_building())), listing)

    assert "bedrooms" in enriched.metadata, "the bedroom count is what decides the shortlist"
    assert enriched.building_units, "the 50-unit rule needs a real count"
    assert enriched.metadata.get("address")
    assert "unconfirmed" not in enriched.summary


def test_apartment_list_enrich_picks_the_cheapest_available_home(preferences: Preferences) -> None:
    """A building's cheapest unit decides whether it is worth opening at all."""
    source = ApartmentListSource()
    listing = source.search(FakeClient(FakeResponse(text=apartment_list_document())), preferences)[0]

    enriched = source.enrich(FakeClient(FakeResponse(text=apartment_list_building())), listing)

    document = apartment_list_building()
    prices = [int(p) for p in re.findall(r'"lowPrice":\s*(\d+)', document)]
    assert enriched.price == min(prices), "the cheapest published rent must win"
    assert "Cheapest available home" in enriched.summary


def test_apartment_list_enrich_names_the_other_unit_types(preferences: Preferences) -> None:
    source = ApartmentListSource()
    listing = source.search(FakeClient(FakeResponse(text=apartment_list_document())), preferences)[0]

    enriched = source.enrich(FakeClient(FakeResponse(text=apartment_list_building())), listing)

    assert "also lists" in enriched.summary, "a building's other sizes must not be hidden"


def test_apartment_list_enriched_listing_reaches_a_real_shortlist(preferences: Preferences) -> None:
    """The whole point: an enriched building must classify into a workflow."""
    from sf_housing.classification import classify_listing

    source = ApartmentListSource()
    listing = source.search(FakeClient(FakeResponse(text=apartment_list_document())), preferences)[0]
    enriched = source.enrich(FakeClient(FakeResponse(text=apartment_list_building())), listing)

    classified = classify_listing(enriched)
    assert classified.housing_kind == WHOLE_UNIT
    assert classified.unit_type in {"studio", "one_bedroom", "two_bedroom", "three_bedroom"}


def test_apartment_list_enrich_survives_a_page_with_nothing_useful(preferences: Preferences) -> None:
    """A redesigned building page must degrade, not crash the whole scan."""
    source = ApartmentListSource()
    listing = source.search(FakeClient(FakeResponse(text=apartment_list_document())), preferences)[0]

    enriched = source.enrich(FakeClient(FakeResponse(text="<html><body>nothing</body></html>")), listing)

    assert enriched.price == listing.price
    assert enriched.building_units is None
    assert "bedrooms" not in enriched.metadata


@pytest.mark.parametrize(
    "value,expected",
    [("  spaced   out ", "spaced out"), (None, ""), (False, ""), (0, ""), (True, "True"), (48, "48")],
)
def test_clean_text_survives_non_string_structured_data(value: object, expected: str) -> None:
    """A page publishing `true` where others publish "Yes" cost 16 of 20 detail fetches."""
    from sf_housing.sources import _clean_text

    assert _clean_text(value) == expected


@pytest.mark.parametrize(
    "value,expected",
    [(True, True), (False, False), ("Yes", True), ("No", False), ("yes", True), (None, None), ("", None)],
)
def test_pets_allowed_reads_both_shapes(value: object, expected: bool | None) -> None:
    from sf_housing.sources import _pets_allowed

    assert _pets_allowed(value) is expected


def test_apartment_list_enrich_handles_a_boolean_pets_flag(preferences: Preferences) -> None:
    """Regression for the crash that lost most Apartment List detail fetches."""
    source = ApartmentListSource()
    listing = source.search(FakeClient(FakeResponse(text=apartment_list_document())), preferences)[0]
    page = (
        '<html><head><script type="application/ld+json">'
        '{"@type":["RealEstateListing","ApartmentComplex"],"name":"B",'
        '"petsAllowed":true,"address":{"streetAddress":"1 Test St"},'
        '"numberOfAvailableAccommodationUnits":{"value":12}}'
        "</script></head></html>"
    )

    enriched = source.enrich(FakeClient(FakeResponse(text=page)), listing)

    assert enriched.building_units == 12
    assert "Pets allowed." in enriched.summary


def test_apartment_list_classifies_a_building_that_publishes_no_unit_rents(
    preferences: Preferences,
) -> None:
    """Smaller buildings list their unit mix with no prices; still classify them."""
    source = ApartmentListSource()
    listing = source.search(FakeClient(FakeResponse(text=apartment_list_document())), preferences)[0]
    page = (
        '<html><head><script type="application/ld+json">'
        '[{"@type":["RealEstateListing","ApartmentComplex"],"name":"Small Building"},'
        '{"@type":["Apartment","Residence"],"name":"Junior 1 Bedroom","numberOfBedrooms":1},'
        '{"@type":["Apartment","Residence"],"name":"Efficiency","numberOfBedrooms":0}]'
        "</script></head></html>"
    )

    enriched = source.enrich(FakeClient(FakeResponse(text=page)), listing)

    assert enriched.metadata["bedrooms"] == 0, "the smallest home should stand in"
    assert enriched.price == listing.price, "the building's own starting rent must survive"
    assert "publishes no rent per home" in enriched.summary


# --------------------------------------------------------------------------
# neighbourhood recovery
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.zumper.com/apartment-buildings/1/blueground-san-francisco-soma-san-francisco-ca", "SoMa"),
        ("https://www.zumper.com/apartment-buildings/2/potrero-1010-potrero-hill-san-francisco-ca", "Potrero Hill"),
        ("https://www.zumper.com/apartment-buildings/4/x-bernal-heights-san-francisco-ca", "Bernal Heights"),
        ("https://www.zumper.com/apartment-buildings/5/plain-building-san-francisco-ca", None),
    ],
)
def test_a_listing_url_can_name_its_neighbourhood(url: str, expected: str | None) -> None:
    """Zumper's structured data only ever says "San Francisco"; the slug says more."""
    from sf_housing.sources import sf_area_from_slug

    assert sf_area_from_slug(url) == expected


def test_the_longest_matching_area_wins() -> None:
    """"Mission Bay" must never be filed as "Mission"; they are different searches."""
    from sf_housing.sources import sf_area_from_slug

    assert sf_area_from_slug("https://www.zumper.com/x/azure-mission-bay-san-francisco-ca") == "Mission Bay"


def test_zumper_listings_carry_the_area_from_their_url(preferences: Preferences) -> None:
    from sf_housing.sources import ZumperSource

    listings = ZumperSource().search(FakeClient(FakeResponse(text=zumper_search())), preferences)

    assert any(l.neighborhood for l in listings), "the fixture URLs name real neighbourhoods"


def test_apartment_list_takes_the_area_from_the_building_coordinates(preferences: Preferences) -> None:
    source = ApartmentListSource()
    listing = source.search(FakeClient(FakeResponse(text=apartment_list_document())), preferences)[0]
    page = (
        '<html><head><script type="application/ld+json">'
        '{"@type":["RealEstateListing","ApartmentComplex"],"name":"B",'
        '"geo":{"@type":"GeoCoordinates","latitude":37.7625,"longitude":-122.3985},'
        '"address":{"streetAddress":"1 Test St"}}'
        "</script></head></html>"
    )

    enriched = source.enrich(FakeClient(FakeResponse(text=page)), listing)

    assert enriched.neighborhood, "a published map pin inside a target area should resolve"


@pytest.mark.parametrize(
    "zip_code,expected",
    [
        ("94158", "Mission Bay"),
        ("94123", "Marina"),
        ("94104", "Financial District"),
        ("94110", None),   # Mission and Bernal Heights both
        ("94114", None),   # Castro and Noe Valley both
        ("", None),
        ("not a zip", None),
    ],
)
def test_only_unambiguous_zips_name_a_neighbourhood(zip_code: str, expected: str | None) -> None:
    """Area carries the most weight in the score, so a wrong one costs more than none."""
    from sf_housing.sources import sf_area_from_zip

    assert sf_area_from_zip(zip_code) == expected


def test_the_result_cap_takes_the_whole_craigslist_page() -> None:
    """Craigslist's search page carries about 220 results and ignores an offset,
    so a 120 cap threw away roughly a hundred homes per search -- on a live
    scan, 197 listings instead of 290, and 108 rooms instead of 201. Nobody
    chose that number; it was a default nobody had revisited."""
    from sf_housing.deal_profile import legacy_view, DealProfile

    profile = DealProfile.from_dict(
        {
            "state": "active",
            "enabled_paths": ["private_room"],
            "budgets": {"private_room": {"maximum_monthly": 2500}},
            "geography": {"anywhere_in_sf": True},
        }
    )
    view = legacy_view(profile, {})

    assert view["sources"]["max_results_per_source"] >= 220, (
        "the cap has to clear one full Craigslist page"
    )


def test_a_stored_cap_is_still_respected() -> None:
    """Raising the default must not override someone who set their own."""
    from sf_housing.preferences import parse_preferences

    import yaml

    document = {
        "profile_version": 1,
        "profile": {
            "state": "active",
            "enabled_paths": ["private_room"],
            "budgets": {"private_room": {"maximum_monthly": 2500}},
            "geography": {"anywhere_in_sf": True},
        },
        "technical": {"sources": {"max_results_per_source": 40}},
    }
    preferences = parse_preferences(yaml.safe_dump(document))

    assert preferences.section("sources")["max_results_per_source"] == 40
