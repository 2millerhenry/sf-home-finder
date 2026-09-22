"""Movoto: individual homes rather than buildings.

Almost every other automatic source publishes buildings, with a rent that
belongs to whichever home in one is cheapest. Movoto publishes the homes -- own
rent, own unit number, own bedroom count -- which is why a third of the
addresses read from it belonged to no other source.

The fixture is real captured records, trimmed, chosen for the behaviours that
are easy to get wrong: a home whose unit number is written in both fields, one
with no bedroom count at all, one filed under a neighbourhood the app also
ranks and one filed under a name it does not, and a whole house among the
condos. Two records are there to be rejected: a real Oakland home, and one
bent to FOR_SALE with a sale price, which is the single check standing between
this source and a million-dollar "rent".
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

from sf_housing.classification import WHOLE_UNIT
from sf_housing.preferences import Preferences, parse_preferences
from sf_housing.sources import (
    MovotoSource,
    SourceError,
    _mentions_unit,
    default_sources,
)
from tests.conftest import TEST_PREFERENCES


FIXTURES = Path(__file__).parent / "fixtures"

HARRISON = "nfhl5y84oqab"   # unit written in both fields, beds + area
GUERRERO = "ms0nibh2v4ab"   # no bedroom count
CHANNEL = "64099438"        # neighbourhood the app ranks
NINETEENTH = "zht1k74v2vab" # neighbourhood the app does not rank
HOWTH = "46797521"          # a whole house
SALE = "sale-1"             # FOR_SALE, must never be read as a rent


def search_page() -> str:
    return (FIXTURES / "movoto_search.html").read_text(encoding="utf-8")


class FakeResponse:
    def __init__(self, text: str = "", status: int = 200):
        self.text = text
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"unexpected status {self.status_code}")


class FakeClient:
    """Serves each queued page once, then repeats the last one."""

    def __init__(self, *pages: FakeResponse):
        self.pages = list(pages) or [FakeResponse("<html></html>")]
        self.requested: list[str] = []
        self.headers: dict[str, str] = {}

    def get(self, url, **kwargs):
        self.requested.append(url)
        self.headers = kwargs.get("headers") or {}
        return self.pages[min(len(self.requested) - 1, len(self.pages) - 1)]


def profile(*paths: str, maximum: int = 12000) -> Preferences:
    budgets = {
        path: {"maximum_monthly": maximum, "minimum_monthly": 100, "occupants": 3}
        for path in paths
    }
    return parse_preferences(
        yaml.safe_dump(
            {
                "profile_version": 1,
                "profile": {
                    "state": "active",
                    "enabled_paths": list(paths),
                    "budgets": budgets,
                    "geography": {"anywhere_in_sf": True},
                },
            }
        )
    )


@pytest.fixture
def preferences() -> Preferences:
    return parse_preferences(TEST_PREFERENCES)


def found(preferences: Preferences, page: str | None = None):
    return MovotoSource().search(FakeClient(FakeResponse(page or search_page())), preferences)


def by_id(listings):
    return {item.source_id: item for item in listings}


def doctored(mls: str, **changes) -> str:
    """The captured page with one home's fields changed."""
    page = search_page()
    payload = json.loads(
        re.search(r'<script id="__INITIAL_STATE__"[^>]*>(.*?)</script>', page, re.S).group(1)
    )
    target = next(x for x in payload["pageData"]["listings"] if x["mlsNumber"] == mls)
    for key, value in changes.items():
        if key == "geo":
            target["geo"].update(value)
        else:
            target[key] = value
    return (
        '<html><body><script id="__INITIAL_STATE__">'
        + json.dumps(payload)
        + "</script></body></html>"
    )


# --------------------------------------------------------------------------
# the check that matters most
# --------------------------------------------------------------------------


def test_a_home_for_sale_is_never_read_as_a_home_to_let(preferences) -> None:
    """A sale and a let are the same record with a different status, and
    `listPrice` is a sale price on one and a monthly rent on the other. The
    fixture carries a real record bent to FOR_SALE at $1,298,000; read as a
    rent it would clear no budget ever set and would be the most expensive
    thing in the pool."""
    stored = by_id(found(preferences))
    assert SALE not in stored
    assert all(item.price is None or item.price < 100_000 for item in stored.values())


def test_the_status_is_what_is_checked_not_the_url(preferences) -> None:
    """Reading "this came from the rentals page, so it is a rental" is what
    makes a source silently wrong the day the page changes."""
    page = doctored(HARRISON, houseRealStatus="FOR_SALE", listPrice=1_450_000)
    assert HARRISON not in by_id(found(preferences, page))


@pytest.mark.parametrize("changes", [{"isRentals": False}, {"isSold": True}], ids=["not-a-rental", "sold"])
def test_a_home_that_is_not_actually_lettable_is_dropped(changes, preferences) -> None:
    assert HARRISON not in by_id(found(preferences, doctored(HARRISON, **changes)))


def test_a_home_in_another_city_is_left_on_the_page(preferences) -> None:
    """The fixture carries a real Oakland home from Movoto's own Oakland page."""
    for item in found(preferences):
        assert "oakland" not in (item.metadata.get("address") or "").casefold()
    assert len(found(preferences)) == 5


# --------------------------------------------------------------------------
# what a home is
# --------------------------------------------------------------------------


def test_every_lettable_san_francisco_home_is_read(preferences) -> None:
    assert set(by_id(found(preferences))) == {HARRISON, GUERRERO, CHANNEL, NINETEENTH, HOWTH}


def test_the_rent_is_the_homes_own(preferences) -> None:
    """Not a building's starting rate stood in for it, which is what every
    other building source here has to do."""
    stored = by_id(found(preferences))
    assert stored[HARRISON].price == 5700
    assert stored[HOWTH].price == 6000
    assert "Asking $5,700 a month." in (stored[HARRISON].summary or "")


def test_the_bedroom_count_is_the_homes_own(preferences) -> None:
    stored = by_id(found(preferences))
    assert stored[HARRISON].metadata["bedrooms"] == 2
    assert stored[HOWTH].metadata["bedrooms"] == 3


def test_a_home_with_no_bedroom_count_says_so_rather_than_guessing(preferences) -> None:
    """About one home in seven publishes none. Defaulted to zero it becomes a
    studio, and a studio is a thing a deal can be scored against."""
    listing = by_id(found(preferences))[GUERRERO]
    assert "bedrooms" not in listing.metadata
    assert "does not state how many bedrooms" in (listing.summary or "")


def test_homes_are_whole_homes(preferences) -> None:
    assert {item.housing_kind for item in found(preferences)} == {WHOLE_UNIT}


def test_the_property_type_is_carried_through(preferences) -> None:
    stored = by_id(found(preferences))
    assert stored[HOWTH].listing_type == "Single Family House"
    assert stored[HARRISON].listing_type == "Condo"


# --------------------------------------------------------------------------
# the unit number
# --------------------------------------------------------------------------


def test_a_unit_number_is_not_printed_twice(preferences) -> None:
    """Movoto writes it in the address and in subPremise, spelled differently:
    "1140 Harrison St #323" arrives with a subPremise of "APT 323". Appended
    unconditionally the title reads "1140 Harrison St #323 APT 323"."""
    stored = by_id(found(preferences))
    assert stored[HARRISON].title == "1140 Harrison St #323"
    assert stored[CHANNEL].title == "110 Channel St # 416"
    assert stored[NINETEENTH].title == "3711 19th Ave #VI434"


def test_a_unit_number_the_address_lacks_is_added(preferences) -> None:
    """Two homes at one address are two listings, and without the unit they
    are one row overwriting the other."""
    page = doctored(HOWTH, geo={"subPremise": "APT 5"})
    assert by_id(found(preferences, page))[HOWTH].title == "273 Howth St APT 5"


def test_the_unit_is_kept_out_of_the_address_corroboration_matches_on(preferences) -> None:
    """Another source can confirm the building. It cannot confirm the flat."""
    listing = by_id(found(preferences))[HARRISON]
    assert listing.metadata["address"] == "1140 Harrison St #323"
    assert listing.metadata["unit"] == "APT 323"


@pytest.mark.parametrize(
    "address,unit,already",
    [
        ("1140 Harrison St #323", "APT 323", True),
        ("110 Channel St # 416", "416", True),
        ("3711 19th Ave #VI434", "APT VI434", True),
        ("55 Page St", "UNIT 12", False),
        ("55 Page St 12", "UNIT 12", True),
        ("273 Howth St", "", False),
    ],
)
def test_the_unit_comparison_ignores_how_it_is_spelled(address, unit, already) -> None:
    assert _mentions_unit(address, unit) is already


# --------------------------------------------------------------------------
# where a home is
# --------------------------------------------------------------------------


def test_movotos_own_neighbourhood_is_used_where_the_app_ranks_it(preferences) -> None:
    """Stated by the source beats inferred from the street.

    On the captured records the two agree, which proves nothing about which
    one wins, so the stated name is moved to a different ranked neighbourhood
    here. A street table is a guess about where an address sits; the site
    listing the home is saying where it is."""
    assert by_id(found(preferences))[CHANNEL].neighborhood == "Mission Bay"

    page = doctored(CHANNEL, geo={"neighborhoodName": "Bernal Heights"})
    assert by_id(found(preferences, page))[CHANNEL].neighborhood == "Bernal Heights"


def test_a_stated_neighbourhood_the_app_cannot_rank_never_wins(preferences) -> None:
    """It is reported in the summary, and the listing is still labelled from
    its street, because an unrankable label is worse than an inferred one."""
    page = doctored(CHANNEL, geo={"neighborhoodName": "Somewhere Nobody Ranks"})
    listing = by_id(found(preferences, page))[CHANNEL]
    assert listing.neighborhood == "Mission Bay"
    assert "Movoto files it under Somewhere Nobody Ranks." in (listing.summary or "")


def test_a_rent_published_as_a_flag_is_not_a_rent_of_one_dollar(preferences) -> None:
    """`True` is an int in Python and `int(True)` is 1, so a boolean in the
    price field becomes a home asking a dollar a month -- which would outrank
    everything else in the pool."""
    listing = by_id(found(preferences, doctored(HARRISON, listPrice=True)))[HARRISON]
    assert listing.price is None
    assert "Asking $1 a month" not in (listing.summary or "")


def test_a_home_with_no_rent_is_kept_without_one(preferences) -> None:
    """Movoto priced every home read here, but a missing rent is a missing
    rent, not a reason to drop the home or to invent a figure."""
    listing = by_id(found(preferences, doctored(HARRISON, listPrice=None)))[HARRISON]
    assert listing.price is None
    assert "Asking" not in (listing.summary or "")
    assert "1140 Harrison St" in (listing.summary or "")


def test_a_neighbourhood_the_app_cannot_rank_falls_back_to_the_address(preferences) -> None:
    """"Western South of Market" is a real place and not one this deal can
    rank, so the listing is labelled from its street instead and Movoto's own
    name is reported rather than dropped."""
    listing = by_id(found(preferences))[HARRISON]
    assert listing.neighborhood == "SoMa"
    assert "Movoto files it under Western South of Market." in (listing.summary or "")


def test_a_neighbourhood_that_only_differs_in_spelling_is_not_reported_twice(
    preferences,
) -> None:
    """"Movoto files it under Parkmerced" against a listing already labelled
    Park Merced is one fact spelled two ways."""
    listing = by_id(found(preferences))[NINETEENTH]
    assert listing.neighborhood == "Park Merced"
    assert "files it under" not in (listing.summary or "")


def test_every_home_publishes_the_address_corroboration_matches_on(preferences) -> None:
    from sf_housing.location import parse_street_address

    for listing in found(preferences):
        assert parse_street_address(listing.metadata["address"]) is not None


# --------------------------------------------------------------------------
# links and identity
# --------------------------------------------------------------------------


def test_the_link_is_absolute_and_on_movoto(preferences) -> None:
    for listing in found(preferences):
        assert listing.original_url.startswith("https://www.movoto.com/")


def test_a_link_pointing_at_another_host_is_refused(preferences) -> None:
    assert HARRISON not in by_id(found(preferences, doctored(HARRISON, path="https://evil.example/x")))


def test_a_home_with_no_link_is_dropped(preferences) -> None:
    assert HARRISON not in by_id(found(preferences, doctored(HARRISON, path="")))


def test_the_stored_id_is_movotos_own_not_the_naming_path(preferences) -> None:
    """The path carries the street and the unit, so keyed on that a re-listing
    at a corrected address orphans the stored row instead of updating it."""
    listing = by_id(found(preferences))[HARRISON]
    assert listing.source_id == HARRISON
    assert "/" not in listing.source_id


# --------------------------------------------------------------------------
# paging and walls
# --------------------------------------------------------------------------


def test_paging_stops_when_the_site_serves_page_one_again(preferences) -> None:
    """Page forty holds the last fourteen homes; pages forty-five and fifty
    both came back as page one, home for home."""
    client = FakeClient(FakeResponse(search_page()))
    MovotoSource().search(client, preferences)
    assert len(client.requested) == 2
    assert client.requested[0] == "https://www.movoto.com/san-francisco-ca/rentals/"
    assert client.requested[1] == "https://www.movoto.com/san-francisco-ca/rentals/p-2/"


def test_a_second_page_of_new_homes_is_read(preferences) -> None:
    other = search_page().replace(HARRISON, "second-page-1").replace(GUERRERO, "second-page-2")
    client = FakeClient(FakeResponse(search_page()), FakeResponse(other))
    assert {"second-page-1", "second-page-2"} <= set(by_id(MovotoSource().search(client, preferences)))


def test_the_per_source_cap_is_honoured() -> None:
    preferences = parse_preferences(
        yaml.safe_dump({**yaml.safe_load(TEST_PREFERENCES), "sources": {"max_results_per_source": 2}})
    )
    assert len(MovotoSource().search(FakeClient(FakeResponse(search_page())), preferences)) == 2


@pytest.mark.parametrize(
    "response",
    [
        FakeResponse("", 202),
        FakeResponse("", 403),
        FakeResponse("", 429),
        FakeResponse("   ", 200),
        FakeResponse("<html><body>Please verify you are a human</body></html>", 200),
        FakeResponse("<html><title>Just a moment...</title></html>", 200),
    ],
    ids=["202-empty", "403", "429", "blank-200", "captcha", "cloudflare"],
)
def test_a_wall_is_never_read_as_an_empty_result(response, preferences) -> None:
    with pytest.raises(SourceError):
        MovotoSource().search(FakeClient(response), preferences)


def test_being_rate_limited_is_reported_as_rate_limiting(preferences) -> None:
    with pytest.raises(SourceError) as error:
        MovotoSource().search(FakeClient(FakeResponse("", 429)), preferences)
    assert "rate-limiting rather than broken" in str(error.value)


@pytest.mark.parametrize(
    "payload_text",
    [
        "{not json",
        "[1,2,3]",
        '{"pageData": {}}',
        '{"pageData": []}',
        '{"pageData": {"listings": "nope"}}',
    ],
    ids=["malformed", "not-an-object", "no-listings", "pageData-is-a-list", "listings-wrong-type"],
)
def test_a_payload_this_no_longer_understands_is_reported_not_crashed(
    payload_text, preferences
) -> None:
    page = f'<html><body><script id="__INITIAL_STATE__">{payload_text}</script></body></html>'
    with pytest.raises(SourceError) as error:
        MovotoSource().search(FakeClient(FakeResponse(page)), preferences)
    assert "format may have changed" in str(error.value)


def test_junk_records_inside_a_good_payload_are_skipped_not_fatal(preferences) -> None:
    page = search_page()
    payload = json.loads(
        re.search(r'<script id="__INITIAL_STATE__"[^>]*>(.*?)</script>', page, re.S).group(1)
    )
    payload["pageData"]["listings"] = [None, "nonsense", {}, *payload["pageData"]["listings"]]
    doctored_page = (
        '<html><body><script id="__INITIAL_STATE__">' + json.dumps(payload) + "</script></body></html>"
    )
    assert len(found(preferences, doctored_page)) == 5


def test_a_page_of_only_out_of_town_homes_is_read_not_raised(preferences) -> None:
    """Homes were read; every one was elsewhere. An empty result, not a broken
    page, and it must not put a working source into backoff."""
    page = search_page().replace('"San Francisco"', '"Oakland"')
    assert MovotoSource().search(FakeClient(FakeResponse(page)), preferences) == []


# --------------------------------------------------------------------------
# registration and cost
# --------------------------------------------------------------------------


def test_movoto_ships_whether_or_not_an_inbox_is_connected() -> None:
    class FakeMailbox:
        credential = None

        def configured(self):
            return False

    for label, sources in (
        ("no inbox", default_sources()),
        ("inbox connected", default_sources(FakeMailbox())),
    ):
        assert "Movoto" in [item.platform for item in sources], label


def test_movoto_needs_no_detail_fetch() -> None:
    assert MovotoSource.detail_budget == 0
    assert not hasattr(MovotoSource, "enrich")


def test_the_source_keeps_no_per_deal_state() -> None:
    assert vars(MovotoSource()) == {}


def test_the_request_borrows_a_browser_name(preferences) -> None:
    client = FakeClient(FakeResponse(search_page()))
    MovotoSource().search(client, preferences)
    assert "Chrome" in client.headers.get("User-Agent", "")


def test_movoto_runs_before_the_sources_that_fetch_a_page_per_building(
    repository, preferences
) -> None:
    from sf_housing.scanner import Scanner
    from sf_housing.sources import ApartmentListSource, RentComSource

    scanner = Scanner(
        repository,
        lambda: preferences,
        [RentComSource(), ApartmentListSource(), MovotoSource()],
        detail_delay_seconds=0,
    )
    order = [item.platform for item in scanner._eligible_sources("scheduled")]
    assert order.index("Movoto") < order.index("Apartment List")
    assert order.index("Movoto") < order.index("Rent.com")


def test_a_floor_area_answers_the_bedroom_count_movoto_leaves_out() -> None:
    """One Movoto home in seven publishes no bedroom count, and those homes
    were the whole of the owner's "Other homes" tab. Movoto does publish their
    floor area, and on its own stated homes in this city the sizes separate
    cleanly: the uncounted ones sit at a median 454 sq ft with three-quarters
    under 509, while one-bedrooms start at 557. That is an answer."""
    from sf_housing.sources import _bedrooms_from_floor_area

    assert _bedrooms_from_floor_area(454) == 0, "the median uncounted home is a studio"
    assert _bedrooms_from_floor_area(509) == 0, "and so is the top of that quartile"
    assert _bedrooms_from_floor_area(648) == 1, "the median one-bedroom"
    assert _bedrooms_from_floor_area(1000) == 2
    assert _bedrooms_from_floor_area(1391) == 3

    # Silence wherever the area cannot mean a bedroom count.
    assert _bedrooms_from_floor_area(None) is None
    assert _bedrooms_from_floor_area("1,200 sq ft") is None
    assert _bedrooms_from_floor_area(0) is None
    assert _bedrooms_from_floor_area(4000) is None, "a house, not a bedroom count"
    assert _bedrooms_from_floor_area(True) is None, "a bool is not an area"


def test_a_stated_bedroom_count_is_never_overridden_by_an_estimate(preferences) -> None:
    """The estimate exists for homes Movoto declines to count. A home whose
    count it does publish is believed over any floor area, and must not be
    marked as estimated."""
    page = doctored(HARRISON, bed=2, sqftTotal=430)
    home = by_id(found(preferences, page))[HARRISON]

    assert home.metadata.get("bedrooms") == 2, "the stated count wins over 430 sq ft"
    assert "bedrooms_basis" not in home.metadata


def test_an_uncounted_home_is_read_from_its_floor_area_and_says_so(preferences) -> None:
    # Movoto publishes "bed": 0 as a stated studio; the homes this is for
    # carry no bed field at all.
    page = doctored(HARRISON, bed=None, sqftTotal=430)
    home = by_id(found(preferences, page))[HARRISON]

    assert home.metadata.get("bedrooms") == 0, "430 sq ft is a studio"
    assert home.metadata.get("bedrooms_basis") == "floor_area", "and is marked an estimate"
    assert "floor area puts this at" in (home.summary or ""), "and the row says how it knows"
