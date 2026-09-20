"""ApartmentGuide: a whole building read from one page, with no detail fetch.

The fixtures are real captured records, trimmed. Four San Francisco buildings
chosen for the behaviours that are easy to get wrong -- a building with homes
free today that *also* publishes a future date, a building letting two sizes, a
building with nothing free and two future dates -- plus one real Oakland record,
so the locality guard is proved against a page that genuinely contains one
rather than against a page that never could.

``apartmentguide_padded.html`` is the same page with one San Francisco building
named in ``expandedSearchIds``. No San Francisco query was observed to pad, so
that id is set deliberately: ApartmentGuide ships Rent.com's payload and
Rent.com pads, and the guard has to hold the day it starts.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

from sf_housing.classification import WHOLE_UNIT
from sf_housing.location import sf_area_from_address
from sf_housing.preferences import Preferences, parse_preferences
from sf_housing.sources import (
    ApartmentGuideSource,
    SourceError,
    _other_bedroom_sizes,
    _representative_bedroom,
    default_sources,
)
from tests.conftest import TEST_PREFERENCES


FIXTURES = Path(__file__).parent / "fixtures"

ISLE_HOUSE = "6858923"        # 2-bed, 11 homes free now, and one plan dated Oct 2026
GEARY = "5977562"             # studio + 1-bed
MT_SUTRO = "5891014"          # studio only, nothing free, no date
DOGPATCH = "6257544"          # nothing free at all, two future dates
OAKLAND = "5916906"           # a real Oakland building on a San Francisco page


def search_page() -> str:
    return (FIXTURES / "apartmentguide_search.html").read_text(encoding="utf-8")


def padded_page() -> str:
    return (FIXTURES / "apartmentguide_padded.html").read_text(encoding="utf-8")


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

    def get(self, url, **kwargs):
        self.requested.append(url)
        self.headers = kwargs.get("headers") or {}
        return self.pages[min(len(self.requested) - 1, len(self.pages) - 1)]


def profile(*paths: str, maximum: int = 9000) -> Preferences:
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
    return ApartmentGuideSource().search(FakeClient(FakeResponse(page or search_page())), preferences)


def by_id(listings):
    return {item.source_id: item for item in listings}


# --------------------------------------------------------------------------
# what the page is read into
# --------------------------------------------------------------------------


def test_every_san_francisco_building_on_the_page_is_read(preferences) -> None:
    assert set(by_id(found(preferences))) == {ISLE_HOUSE, GEARY, MT_SUTRO, DOGPATCH}


def test_a_building_in_another_city_is_left_on_the_page(preferences) -> None:
    """The one guard that has to hold when a city-scoped feed stops being one.
    An Oakland building scored against San Francisco rents looks like a find."""
    assert OAKLAND not in by_id(found(preferences))


def test_the_padded_building_is_dropped_by_id_not_by_address(preferences) -> None:
    """Named padding is dropped even though its address really is in San
    Francisco, which is the case an address test could never catch."""
    padded = by_id(found(preferences, padded_page()))
    assert DOGPATCH not in padded
    assert {ISLE_HOUSE, GEARY, MT_SUTRO} == set(padded)


def test_the_link_is_absolute_and_built_from_the_published_path(preferences) -> None:
    listing = by_id(found(preferences))[ISLE_HOUSE]
    assert listing.original_url == (
        "https://www.apartmentguide.com/a/Isle-House-San-Francisco-CA-6858923/"
    )


def test_the_stored_id_is_apartmentguides_own_not_the_naming_slug(preferences) -> None:
    """The path carries the building's name. Keyed on that, a rename orphans
    the stored row and adds a duplicate instead of updating it."""
    listing = by_id(found(preferences))[ISLE_HOUSE]
    assert listing.source_id == ISLE_HOUSE
    assert "isle" not in listing.source_id.lower()


def test_buildings_are_whole_homes(preferences) -> None:
    assert {item.housing_kind for item in found(preferences)} == {WHOLE_UNIT}


# --------------------------------------------------------------------------
# the join
# --------------------------------------------------------------------------


def test_the_rent_table_is_joined_by_id_and_never_by_position() -> None:
    """filterMatchResults is deliberately stored in a different order than
    listings. Paired by index this prints one building's rent on another."""
    page = search_page()
    payload = json.loads(re.search(r'id="__NEXT_DATA__"[^>]*>(\{.*?\})</script>', page, re.S).group(1))
    search = payload["props"]["pageProps"]["pageData"]["location"]["listingSearch"]
    assert [row["listingId"] for row in search["filterMatchResults"]] != [
        str(item["id"]) for item in search["listings"]
    ], "fixture no longer proves anything; reorder filterMatchResults"

    listings = by_id(ApartmentGuideSource().search(FakeClient(FakeResponse(page)), profile("two_bedroom")))
    assert listings[ISLE_HOUSE].price == 5832


def test_a_building_missing_from_the_rent_table_still_reads(preferences) -> None:
    """The listing carries its own bedCountData. Losing the join costs the
    upper end of the range, never the building."""
    page = search_page()
    payload = json.loads(re.search(r'id="__NEXT_DATA__"[^>]*>(\{.*?\})</script>', page, re.S).group(1))
    payload["props"]["pageProps"]["pageData"]["location"]["listingSearch"]["filterMatchResults"] = []
    stripped = f'<html><body><script id="__NEXT_DATA__">{json.dumps(payload)}</script></body></html>'

    listing = by_id(found(preferences, stripped))[ISLE_HOUSE]
    assert listing.price == 5832
    assert "homes are free right now" not in (listing.summary or "")


# --------------------------------------------------------------------------
# what a home costs
# --------------------------------------------------------------------------


def test_the_rent_is_the_one_for_the_size_the_deal_asked_for() -> None:
    """925 Geary lets a studio at $1,845 and a one-bedroom at $2,740. A deal
    that wants a one-bedroom must not be quoted the studio's rent."""
    assert by_id(ApartmentGuideSource().search(
        FakeClient(FakeResponse(search_page())), profile("one_bedroom")
    ))[GEARY].price == 2740
    assert by_id(ApartmentGuideSource().search(
        FakeClient(FakeResponse(search_page())), profile("studio")
    ))[GEARY].price == 1845


def test_a_building_without_the_wanted_size_keeps_its_own_cheapest_home() -> None:
    """Scored as the mismatch it is rather than dropped or silently repriced."""
    listing = by_id(ApartmentGuideSource().search(
        FakeClient(FakeResponse(search_page())), profile("four_bedroom")
    ))[GEARY]
    assert listing.price == 1845
    assert listing.metadata["bedrooms"] == 0


def test_the_other_sizes_never_repeat_the_size_already_quoted() -> None:
    """"Its studio homes start at $1,845. This building also lets a studio
    from $1,845" is one fact said twice, and reads as two different studios."""
    summary = by_id(ApartmentGuideSource().search(
        FakeClient(FakeResponse(search_page())), profile("studio")
    ))[GEARY].summary or ""
    assert "also lets a 1-bedroom from $2,740" in summary
    assert summary.count("$1,845") == 1


def test_a_concession_is_reported_and_never_priced_in(preferences) -> None:
    """Six weeks free changes what a year costs and nothing about the rent.
    Folded into price it would put a building under a budget it does not meet."""
    listing = by_id(found(preferences))[ISLE_HOUSE]
    assert listing.price == 5832
    assert "6 weeks free" in listing.metadata["concession"]
    assert "Offer: Up to 6 weeks free" in (listing.summary or "")


def test_landlord_copy_cannot_run_into_the_next_sentence(preferences) -> None:
    """The concession arrives however it was typed, full stop or not."""
    summary = by_id(found(preferences))[ISLE_HOUSE].summary or ""
    assert "*Restrictions May Apply. Managed by" in summary


# --------------------------------------------------------------------------
# when somebody could actually move in
# --------------------------------------------------------------------------


def test_a_building_with_homes_free_today_publishes_no_future_move_in_date(preferences) -> None:
    """ApartmentGuide dates a plan only when it is *not* lettable yet. Isle
    House has 11 homes free now and one plan dated October 2026; reading that
    date as this building's would hand scoring a move-in months later than
    the truth."""
    listing = by_id(found(preferences))[ISLE_HOUSE]
    assert "available_on" not in listing.metadata
    assert "11 homes are free right now" in (listing.summary or "")


def test_a_building_with_nothing_free_publishes_its_earliest_opening(preferences) -> None:
    """Two future dates on the page; the earlier one is when somebody could
    actually move in."""
    listing = by_id(found(preferences))[DOGPATCH]
    assert listing.metadata["available_on"] == "October 2, 2026"
    assert "Nothing is free today" in (listing.summary or "")


def test_the_move_in_date_is_written_the_way_scoring_reads_it(preferences) -> None:
    """An ISO string under a key of its own is a fact nothing consults, and
    the availability criterion stays Unknown for the source that published it."""
    from sf_housing.scoring import _available_on

    listing = by_id(found(preferences))[DOGPATCH]
    assert _available_on(listing) is not None


def test_a_building_that_publishes_no_dates_at_all_claims_none(preferences) -> None:
    assert "available_on" not in by_id(found(preferences))[MT_SUTRO].metadata


# --------------------------------------------------------------------------
# what must never become a posting date
# --------------------------------------------------------------------------


def test_the_record_refresh_stamp_is_not_a_posting_date(preferences) -> None:
    """updatedAt is when ApartmentGuide last touched its own row. Stored as
    listing_timestamp it becomes published_at, is rewritten every scan, renders
    as "Posted today" and pins every building to the top of the newest sort."""
    listing = by_id(found(preferences))[ISLE_HOUSE]
    assert listing.metadata["record_updated"].startswith("2026-")
    assert "listing_timestamp" not in listing.metadata


def test_homes_available_is_never_recorded_as_the_size_of_the_building(preferences) -> None:
    """ApartmentGuide publishes nothing that is the building's own size, so
    building_units stays unset rather than being guessed from what is free."""
    listing = by_id(found(preferences))[ISLE_HOUSE]
    assert listing.metadata["homes_available"] == 11
    assert listing.building_units is None
    assert "11 units" not in (listing.summary or "")


# --------------------------------------------------------------------------
# paging
# --------------------------------------------------------------------------


def test_paging_stops_when_the_site_serves_page_one_again(preferences) -> None:
    """Past its last real page ApartmentGuide re-serves page one with its own
    total still reading 476. Believed, the walk re-reads the same buildings
    until the budget runs out."""
    client = FakeClient(FakeResponse(search_page()))
    listings = ApartmentGuideSource().search(client, preferences)
    assert len(client.requested) == 2, "one page of new buildings, one that repeats it"
    assert len(listings) == 4


def test_a_second_page_of_new_buildings_is_read(preferences) -> None:
    page = search_page()
    other = page.replace(ISLE_HOUSE, "9000001").replace(GEARY, "9000002")
    client = FakeClient(FakeResponse(page), FakeResponse(other))
    listings = ApartmentGuideSource().search(client, preferences)
    assert {"9000001", "9000002"} <= set(by_id(listings))
    assert client.requested[1] == (
        "https://www.apartmentguide.com/apartments/California/San-Francisco/?page=2"
    )


def test_the_first_page_is_asked_for_without_a_page_parameter(preferences) -> None:
    client = FakeClient(FakeResponse(search_page()))
    ApartmentGuideSource().search(client, preferences)
    assert client.requested[0] == (
        "https://www.apartmentguide.com/apartments/California/San-Francisco/"
    )


def test_no_bedroom_or_rent_filter_is_ever_put_in_the_url(preferences) -> None:
    """ApartmentGuide accepts /N-bedrooms/ and ?maxPrice= and ignores both:
    asked for four-bedrooms under $2,000 it returned the same 476-building
    first page. A filter here would advertise a narrowing that never happened."""
    client = FakeClient(FakeResponse(search_page()))
    ApartmentGuideSource().search(client, profile("four_bedroom", maximum=2000))
    assert not any("bedroom" in url or "Price" in url for url in client.requested)


def test_the_per_source_cap_is_honoured() -> None:
    preferences = parse_preferences(
        yaml.safe_dump({**yaml.safe_load(TEST_PREFERENCES), "sources": {"max_results_per_source": 2}})
    )
    assert len(ApartmentGuideSource().search(FakeClient(FakeResponse(search_page())), preferences)) == 2


# --------------------------------------------------------------------------
# walls
# --------------------------------------------------------------------------


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
    """Counted as zero listings, a wall records "this source found nothing
    today" at exactly the moment the source stopped talking to us."""
    with pytest.raises(SourceError):
        ApartmentGuideSource().search(FakeClient(response), preferences)


def test_being_rate_limited_is_reported_as_rate_limiting(preferences) -> None:
    with pytest.raises(SourceError) as error:
        ApartmentGuideSource().search(FakeClient(FakeResponse("", 429)), preferences)
    assert "rate-limiting rather than broken" in str(error.value)


def test_a_page_whose_shape_changed_says_so(preferences) -> None:
    with pytest.raises(SourceError) as error:
        ApartmentGuideSource().search(
            FakeClient(FakeResponse("<html><body>hello</body></html>")), preferences
        )
    assert "format may have changed" in str(error.value)


def test_a_page_of_only_out_of_town_buildings_is_read_not_raised(preferences) -> None:
    """Cards were read; every one was elsewhere. That is an empty result, not
    a broken page, and must not put a working source into backoff."""
    page = search_page().replace('"San Francisco"', '"Oakland"')
    assert ApartmentGuideSource().search(FakeClient(FakeResponse(page)), preferences) == []


# --------------------------------------------------------------------------
# shared helpers, and registration
# --------------------------------------------------------------------------


def test_the_shared_rent_picker_prefers_a_priced_size_over_an_unpriced_one() -> None:
    """An unpriced studio must never displace the priced one-bedroom asked for."""
    rows = [{"beds": 0, "prices": {}}, {"beds": 1, "prices": {"low": 2740}}]
    assert _representative_bedroom(rows, {0, 1}) == (1, 2740)


def test_the_shared_rent_picker_ignores_junk_rows() -> None:
    assert _representative_bedroom([None, "x", {"beds": True}, {}], {1}) == (None, None)
    assert _representative_bedroom(None, {1}) == (None, None)


def test_the_shared_size_list_drops_the_size_already_quoted() -> None:
    rows = [{"beds": 0, "prices": {"low": 1845}}, {"beds": 1, "prices": {"low": 2740}}]
    assert _other_bedroom_sizes(rows, 0) == ["a 1-bedroom from $2,740"]
    assert _other_bedroom_sizes(rows, None) == ["a studio from $1,845", "a 1-bedroom from $2,740"]


def test_apartmentguide_ships_in_the_production_source_list() -> None:
    platforms = [source.platform for source in default_sources()]
    assert "ApartmentGuide" in platforms


def test_apartmentguide_needs_no_detail_fetch() -> None:
    """Everything worth scoring is on the search page, so no listing here can
    be stranded by a rate-limited second request."""
    assert ApartmentGuideSource.detail_budget == 0
    assert not hasattr(ApartmentGuideSource, "enrich")


def test_the_availability_predicate_believes_a_status_without_a_count() -> None:
    """ApartmentGuide says a plan is lettable two ways and uses both: 123 of
    the plans read here were AVAILABLE_WITH_COUNT and 235
    AVAILABLE_WITHOUT_COUNT. Reading the count alone calls two thirds of the
    lettable stock unavailable, which is what makes a building with homes free
    today advertise a move-in date months out.

    Asserted directly on the predicate rather than through a fixture: across
    300 real San Francisco buildings no record mixes a countless-but-available
    plan with a dated one, so no captured page can tell the two readings apart.
    The predicate is still answering the wrong question without this.
    """
    lettable = ApartmentGuideSource._lettable_now
    assert lettable({"availabilityStatusCode": "AVAILABLE_WITHOUT_COUNT"}) is True
    assert lettable({"availableCount": 3}) is True
    assert lettable({"availabilityStatusCode": "AVAILABLE_WITH_COUNT", "availableCount": 0}) is True
    assert lettable({"availabilityStatusCode": "UNAVAILABLE_WITH_FUTURE_MOVE_DATE", "availableCount": 0}) is False
    assert lettable({}) is False
    # A boolean is not a count. `True > 0` is true in Python and would make
    # every plan carrying a flag here read as lettable.
    assert lettable({"availableCount": True}) is False


def test_the_browser_headers_state_a_language() -> None:
    """Trulia refuses a request that names no language with the same 403 it
    gives the honest User-Agent. The header is set once for every source that
    borrows a browser's name, so it is asserted where it is sent."""
    client = FakeClient(FakeResponse(search_page()))
    ApartmentGuideSource().search(client, parse_preferences(TEST_PREFERENCES))
    assert client.headers.get("Accept-Language") == "en-US,en;q=0.9"
    assert "Chrome" in client.headers.get("User-Agent", "")


def test_apartmentguide_ships_whether_or_not_an_inbox_is_connected() -> None:
    """default_sources has two return paths. A source added to one of them
    only is a source that vanishes the moment somebody connects their email."""
    class FakeMailbox:
        def __init__(self):
            self.credential = None

        def configured(self):
            return False

    for label, sources in (
        ("no inbox", default_sources()),
        ("inbox connected", default_sources(FakeMailbox())),
    ):
        assert "ApartmentGuide" in [item.platform for item in sources], label


def test_apartmentguide_runs_before_the_sources_that_fetch_a_page_per_building(
    repository, preferences
) -> None:
    """It reads search pages only, so it must not queue behind Rent.com and
    Apartment List, which fetch a detail page for every building they find.
    Asserted through the scanner's real ordering, not a copy of its table."""
    from sf_housing.scanner import Scanner
    from sf_housing.sources import ApartmentListSource, RentComSource

    sources = [RentComSource(), ApartmentListSource(), ApartmentGuideSource()]
    scanner = Scanner(repository, lambda: preferences, sources, detail_delay_seconds=0)
    order = [item.platform for item in scanner._eligible_sources("scheduled")]

    assert order.index("ApartmentGuide") < order.index("Apartment List")
    assert order.index("ApartmentGuide") < order.index("Rent.com")


def test_every_building_publishes_the_address_corroboration_matches_on(preferences) -> None:
    """Cross-source corroboration joins on the street address in metadata.
    A source that omits it is invisible to every other source at the same
    building, which is exactly where ApartmentGuide is most useful: it prices
    buildings Redfin lists without a rent."""
    from sf_housing.location import parse_street_address

    for listing in found(preferences):
        address = listing.metadata.get("address")
        assert address, f"{listing.title} publishes no address"
        assert parse_street_address(address) is not None, f"{address!r} does not parse"


@pytest.mark.parametrize(
    "payload",
    [
        "{not json at all",
        "[1, 2, 3]",
        '{"props": {}}',
        '{"props": {"pageProps": {"pageData": {"location": {}}}}}',
        '{"props": {"pageProps": {"pageData": {"location": {"listingSearch": []}}}}}',
        '{"props": []}',
        '{"props": {"pageProps": "nope"}}',
        '{"props": {"pageProps": {"pageData": {"location": null}}}}',
    ],
    ids=[
        "malformed", "not-an-object", "no-pageData", "no-listingSearch", "wrong-type",
        "props-is-a-list", "pageProps-is-a-string", "location-is-null",
    ],
)
def test_a_payload_this_no_longer_understands_is_reported_not_crashed(payload, preferences) -> None:
    """A shape change must read as "the format may have changed", not as a
    traceback that reaches the scan log as a class name."""
    page = f'<html><body><script id="__NEXT_DATA__">{payload}</script></body></html>'
    with pytest.raises(SourceError) as error:
        ApartmentGuideSource().search(FakeClient(FakeResponse(page)), preferences)
    assert "format may have changed" in str(error.value)


def test_junk_records_inside_a_good_payload_are_skipped_not_fatal(preferences) -> None:
    """One malformed building must not cost the other forty-nine."""
    page = search_page()
    payload = json.loads(re.search(r'id="__NEXT_DATA__"[^>]*>(\{.*?\})</script>', page, re.S).group(1))
    search = payload["props"]["pageProps"]["pageData"]["location"]["listingSearch"]
    search["listings"] = [None, "nonsense", {}, {"id": "x"}, *search["listings"]]
    search["filterMatchResults"] = [None, 7, *search["filterMatchResults"]]
    doctored = f'<html><body><script id="__NEXT_DATA__">{json.dumps(payload)}</script></body></html>'

    listings = ApartmentGuideSource().search(FakeClient(FakeResponse(doctored)), preferences)
    assert set(by_id(listings)) == {ISLE_HOUSE, GEARY, MT_SUTRO, DOGPATCH}


def test_a_link_pointing_at_another_host_is_refused(preferences) -> None:
    """urljoin honours an absolute URL in the path field, so a payload that
    carried one would be stored and opened as somebody else's page."""
    page = search_page().replace(
        '"/a/Isle-House-San-Francisco-CA-6858923/"',
        '"https://evil.example/phish"',
    )
    assert ISLE_HOUSE not in by_id(found(preferences, page))


def test_one_instance_serving_two_deals_never_mixes_them_up() -> None:
    """The bedroom count a deal asked for is threaded through as an argument,
    never parked on the instance. Stored on `self` between search and use it
    can be read by the next deal, which is the failure Rent.com documents at
    length because it has to."""
    source = ApartmentGuideSource()
    studio = by_id(source.search(FakeClient(FakeResponse(search_page())), profile("studio")))
    one_bed = by_id(source.search(FakeClient(FakeResponse(search_page())), profile("one_bedroom")))
    again = by_id(source.search(FakeClient(FakeResponse(search_page())), profile("studio")))

    assert studio[GEARY].price == 1845
    assert one_bed[GEARY].price == 2740
    assert again[GEARY].price == 1845, "the second deal changed what the first one sees"
    # The one thing this source does keep is whether the walk it has just
    # made reached the end of the results, which is a fact about the read and
    # not about the deal, and is written afresh at the top of every search.
    # Named here rather than waved through by a looser check, so the next
    # thing parked on the instance still fails this test.
    assert set(vars(source)) == {"search_covers_inventory"}, "this source keeps per-deal state"


def test_walking_the_payload_never_raises_on_a_shape_it_did_not_expect() -> None:
    """`payload.get("a", {}).get("b", {})` reads safely only while every level
    is a mapping. One level arriving as a list raises AttributeError from
    inside the parser, and the scan log gets a class name instead of a
    sentence a reader can act on."""
    from sf_housing.sources import _nested_mapping

    assert _nested_mapping({"a": {"b": {"c": 1}}}, "a", "b") == {"c": 1}
    assert _nested_mapping({"a": []}, "a", "b") == {}
    assert _nested_mapping({"a": "text"}, "a", "b") == {}
    assert _nested_mapping({"a": {"b": None}}, "a", "b") == {}
    assert _nested_mapping([1, 2, 3], "a") == {}
    assert _nested_mapping(None, "a") == {}
    assert _nested_mapping({"a": {"b": 7}}, "a", "b") == {}


def doctored(*, matched: dict | None = None, **changes) -> str:
    """The captured page with one building's fields changed.

    Every San Francisco building read here was on-market and unrestricted, so
    those states have to be induced. They are not decoration: scoring reads
    `verified_inactive` in four places, and a building that has gone but never
    says so looks exactly like a dropped connection and keeps its place on the
    shortlist for good.

    `matched` changes the same building's row in filterMatchResults, which is
    where the rents are read from in preference to the listing's own copy.
    """
    page = search_page()
    payload = json.loads(re.search(r'id="__NEXT_DATA__"[^>]*>(\{.*?\})</script>', page, re.S).group(1))
    search = payload["props"]["pageProps"]["pageData"]["location"]["listingSearch"]
    next(item for item in search["listings"] if str(item["id"]) == ISLE_HOUSE).update(changes)
    if matched is not None:
        next(row for row in search["filterMatchResults"] if row["listingId"] == ISLE_HOUSE).update(matched)
    return f'<html><body><script id="__NEXT_DATA__">{json.dumps(payload)}</script></body></html>'


def test_a_building_taken_off_the_market_says_so(preferences) -> None:
    listing = by_id(found(preferences, doctored(offMarket=True)))[ISLE_HOUSE]
    assert listing.metadata["verified_inactive"] is True
    assert "taken this building off the market" in (listing.summary or "")


def test_a_building_still_letting_is_never_marked_inactive(preferences) -> None:
    listing = by_id(found(preferences))[ISLE_HOUSE]
    assert "verified_inactive" not in listing.metadata


def test_an_income_restricted_building_says_so(preferences) -> None:
    """Published as a list, so `is True` could never fire on it."""
    listing = by_id(found(preferences, doctored(incomeRestrictions=[{"maxIncome": 90000}])))[ISLE_HOUSE]
    assert listing.metadata["below_market_rate"] is True
    assert "income restricted" in (listing.summary or "")


def test_an_unrestricted_building_is_not_marked_below_market(preferences) -> None:
    assert "below_market_rate" not in by_id(found(preferences))[ISLE_HOUSE].metadata


def test_a_building_that_publishes_no_rent_for_its_size_says_that_too(preferences) -> None:
    page = doctored(
        bedCountData=[{"beds": 2, "prices": {}}],
        matched={"bedCountData": [{"beds": 2, "prices": {}}]},
    )
    listing = by_id(found(preferences, page))[ISLE_HOUSE]
    assert listing.price is None
    assert listing.metadata["bedrooms"] == 2
    assert "at a rent it does not publish" in (listing.summary or "")


def test_the_rent_table_wins_over_the_listings_own_copy(preferences) -> None:
    """filterMatchResults is the richer of the two, so it is read first. This
    is what makes the join worth doing at all."""
    page = doctored(
        bedCountData=[{"beds": 2, "prices": {"low": 1}}],
        matched={"bedCountData": [{"beds": 2, "prices": {"low": 4242}}]},
    )
    assert by_id(found(preferences, page))[ISLE_HOUSE].price == 4242


def test_the_title_is_the_building_not_whoever_manages_it(preferences) -> None:
    """The title is what a reader scans a shortlist by. "The Bozzuto Group"
    on four different buildings tells them nothing about any of them."""
    listing = by_id(found(preferences))[ISLE_HOUSE]
    assert listing.title == "Isle House"
    assert listing.metadata["managed_by"] == "The Bozzuto Group"


def test_a_building_with_no_name_falls_back_to_its_street(preferences) -> None:
    listing = by_id(found(preferences, doctored(name=None)))[ISLE_HOUSE]
    assert listing.title == "39 Bruton St"


def test_the_neighbourhood_is_resolved_from_the_address(preferences) -> None:
    """Out-of-area is a hard fail in scoring, so a listing that resolves to
    nothing is a listing that can never be ranked properly.

    2235 3rd St used to come back as Potrero Hill, because the city's coarse
    neighbourhood layer has no Dogpatch; the finer one does. The address is
    read before the building's name here, so the answer is the table's own and
    not the word "Dogpatch" in "Brick + Iron Lofts at Dogpatch".
    """
    listings = by_id(found(preferences))
    assert listings[GEARY].neighborhood == "Tenderloin"
    assert listings[MT_SUTRO].neighborhood == "Inner Sunset"
    assert listings[DOGPATCH].neighborhood == "Dogpatch"
    assert sf_area_from_address("2235 3rd St") == "Dogpatch"


def test_the_map_pin_resolves_a_neighbourhood_the_address_cannot(preferences) -> None:
    """Three fallbacks in order: the street, then the map pin, then the
    postcode. Dropping the later two costs the buildings on streets the table
    does not carry."""
    page = doctored(address="Nowhere At All", location={
        "lat": 37.7599, "lng": -122.4148, "city": "San Francisco", "zip": "94110"})
    assert by_id(found(preferences, page))[ISLE_HOUSE].neighborhood is not None


def test_the_floor_area_is_kept_as_well_as_said(preferences) -> None:
    """Five sources store `floor_area` alongside the sentence. Nothing reads
    the key today; it is kept because a stored fact a reader can already see
    on the page should be queryable too, and because dropping it here alone
    would leave one source shaped unlike the other four."""
    listing = by_id(found(preferences))[ISLE_HOUSE]
    assert listing.metadata["floor_area"] == "1034–1483 Sqft"
    assert "Floor area 1034–1483 Sqft." in (listing.summary or "")


def test_a_building_with_one_home_left_says_so_in_english() -> None:
    """"1 homes are free right now" is what the obvious version prints, and a
    building down to its last home is the one a reader most wants to trust."""
    from sf_housing.sources import _homes_free_note

    assert _homes_free_note(1) == "1 home is free right now."
    assert _homes_free_note(2) == "2 homes are free right now."
    assert "unit" not in _homes_free_note(11), "the building-size reader takes 'N units' for the building"


def test_the_count_of_free_homes_is_rendered_for_real(preferences) -> None:
    assert "11 homes are free right now." in (by_id(found(preferences))[ISLE_HOUSE].summary or "")
