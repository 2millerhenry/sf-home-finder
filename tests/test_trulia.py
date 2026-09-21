"""Trulia: buildings read from one search payload, priced by a range.

Trulia publishes one card per building with the address in separate fields and
a rent written as ``"$3,834 - $4,002/mo"``. Where a building lets more than one
size the bottom of that range belongs to the smallest home in it, which is the
rule most of this file is about.

It also refuses unattended requests hard, and the refusal escalates: read a few
dozen times in a couple of minutes it returns 403 to everything for the best
part of an hour. Nothing here retries, and neither does the source.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from sf_housing.classification import ROOM as ROOM_KIND, WHOLE_UNIT
from sf_housing.preferences import Preferences, parse_preferences
from sf_housing.sources import (
    SourceError,
    TruliaSource,
    _numeric_span,
    _starting_rate_note,
    default_sources,
)
from tests.conftest import TEST_PREFERENCES


FIXTURES = Path(__file__).parent / "fixtures"


def search_page() -> str:
    return (FIXTURES / "trulia_search.html").read_text(encoding="utf-8")


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


# --------------------------------------------------------------------------
# reading a bedroom range published as two numbers
# --------------------------------------------------------------------------


def test_a_bedroom_range_needs_both_ends_to_mean_anything() -> None:
    """Trulia states min and max separately. Formatted into a string for the
    text-based span reader, a card with a max and no min renders as "None 2",
    from which that reader takes the only number it can see and reports a
    two-bedroom floor for a building whose smallest home nobody stated."""
    assert _numeric_span(0, 2) == (0, 2)
    assert _numeric_span(1, 1) == (1, 1)
    assert _numeric_span(None, 2) is None
    assert _numeric_span(2, None) is None
    assert _numeric_span(None, None) is None


def test_a_bedroom_range_arriving_backwards_is_still_read_low_to_high() -> None:
    assert _numeric_span(3, 1) == (1, 3)


def test_a_boolean_is_not_a_bedroom_count() -> None:
    """`True` is an int in Python, and `int(True)` is 1: a flag in either
    field would silently become a one-bedroom."""
    assert _numeric_span(True, 2) is None
    assert _numeric_span(1, False) is None


def test_a_float_bedroom_count_is_read_as_a_whole_number() -> None:
    assert _numeric_span(2.0, 3.0) == (2, 3)


def test_the_starting_rate_note_says_whose_rent_it_is() -> None:
    assert _starting_rate_note(3675, 0, 2) == (
        "Rents here start at $3,675 a month for a studio; "
        "the 2-bedroom rent is not published."
    )


# --------------------------------------------------------------------------
# registration and cost
# --------------------------------------------------------------------------


def test_trulia_ships_whether_or_not_an_inbox_is_connected() -> None:
    """Switched on once one real page had been read and parsed, which is what
    the fixture at the bottom of this file is. Before that it was written,
    tested and deliberately not registered: a parser that has only ever seen
    cards somebody built for it has not been shown to work."""
    class FakeMailbox:
        credential = None

        def configured(self):
            return False

    for label, sources in (
        ("no inbox", default_sources()),
        ("inbox connected", default_sources(FakeMailbox())),
    ):
        assert "Trulia" in [item.platform for item in sources], label


def test_trulia_is_ready_to_switch_on() -> None:
    """The half that is finished: it conforms, it is priced into the scan
    order, and it has a mark of its own on the alerts page."""
    import re

    for attribute in ("platform", "mode", "search_url", "manual_reason", "detail_budget"):
        assert hasattr(TruliaSource, attribute)
    assert TruliaSource.mode == "automatic"

    scanner_source = Path("sf_housing/scanner.py").read_text()
    assert '"Trulia": 46' in scanner_source

    alerts = Path("sf_housing/templates/alerts.html").read_text()
    marks = re.search(r"\{% set source_marks = \{(.*?)\} %\}", alerts, re.S).group(1)
    assert "'Trulia':" in marks


def test_trulia_needs_no_detail_fetch() -> None:
    """Everything worth scoring is on the search page, so no listing can be
    stranded by a second request that never lands -- which matters more here
    than anywhere, because the second request is the one most likely to 403."""
    assert TruliaSource.detail_budget == 0
    assert not hasattr(TruliaSource, "enrich")


def test_only_two_pages_are_ever_read() -> None:
    """Every extra page is another chance to be turned away for an hour."""
    assert TruliaSource.max_pages == 2


def test_trulia_runs_before_the_sources_that_fetch_a_page_per_building(
    repository, preferences
) -> None:
    from sf_housing.scanner import Scanner
    from sf_housing.sources import ApartmentListSource, RentComSource

    scanner = Scanner(
        repository,
        lambda: preferences,
        [RentComSource(), ApartmentListSource(), TruliaSource()],
        detail_delay_seconds=0,
    )
    order = [item.platform for item in scanner._eligible_sources("scheduled")]
    assert order.index("Trulia") < order.index("Apartment List")
    assert order.index("Trulia") < order.index("Rent.com")


# --------------------------------------------------------------------------
# walls
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "response",
    [
        FakeResponse("", 403),
        FakeResponse("", 429),
        FakeResponse("", 202),
        FakeResponse("   ", 200),
        FakeResponse("<html><body>Please verify you are a human</body></html>", 200),
        FakeResponse("<html><title>Just a moment...</title></html>", 200),
    ],
    ids=["403", "429", "202-empty", "blank-200", "captcha", "cloudflare"],
)
def test_a_wall_is_never_read_as_an_empty_result(response, preferences) -> None:
    with pytest.raises(SourceError):
        TruliaSource().search(FakeClient(response), preferences)


def test_the_403_is_reported_as_rate_limiting_not_breakage(preferences) -> None:
    """Trulia's usual refusal. Reported as breakage it would put a working
    source into backoff for a wall that clears on its own."""
    with pytest.raises(SourceError) as error:
        TruliaSource().search(FakeClient(FakeResponse("", 403)), preferences)
    assert "rate-limiting rather than broken" in str(error.value)
    assert "next check tries again" in str(error.value)


def test_being_turned_away_is_never_retried_inside_one_scan(preferences) -> None:
    """Retrying inside a scan is what turns an occasional refusal into a
    sustained one: twelve rapid retries bought a block lasting the best part
    of an hour."""
    client = FakeClient(FakeResponse("", 403))
    with pytest.raises(SourceError):
        TruliaSource().search(client, preferences)
    assert len(client.requested) == 1


def test_the_request_states_a_language(preferences) -> None:
    """Trulia answers 403 to a browser string that names no language."""
    client = FakeClient(FakeResponse("", 403))
    with pytest.raises(SourceError):
        TruliaSource().search(client, preferences)
    assert client.headers.get("Accept-Language") == "en-US,en;q=0.9"


def test_no_bedroom_filter_is_ever_put_in_the_url(preferences) -> None:
    """Trulia spells one `/2p_beds/`, and asked for it alongside a city it
    answered 403 to every attempt while the unfiltered page kept working."""
    client = FakeClient(FakeResponse("", 403))
    with pytest.raises(SourceError):
        TruliaSource().search(client, profile("two_bedroom"))
    assert not any("beds" in url for url in client.requested)


def test_the_source_keeps_no_per_deal_state() -> None:
    """The bedroom floor a deal asked for is threaded through as an argument.
    Parked on the instance it would be read by whichever deal ran next."""
    assert vars(TruliaSource()) == {}


@pytest.mark.parametrize(
    "payload_text",
    [
        "{not json",
        "[1,2,3]",
        '{"props": {}}',
        '{"props": {"searchData": {"homes": "nope"}}}',
        '{"props": []}',
        '{"props": {"searchData": "nope"}}',
        '{"props": {"searchData": null}}',
    ],
    ids=[
        "malformed", "not-an-object", "no-searchData", "homes-wrong-type",
        "props-is-a-list", "searchData-is-a-string", "searchData-is-null",
    ],
)
def test_a_payload_this_no_longer_understands_is_reported_not_crashed(
    payload_text, preferences
) -> None:
    """A shape change must read as "the format may have changed". Walked with
    `.get("props", {}).get(...)`, a level arriving as a list raises
    AttributeError from inside the parser instead, and the scan log gets a
    class name in place of a sentence."""
    page = f'<html><body><script id="__NEXT_DATA__">{payload_text}</script></body></html>'
    with pytest.raises(SourceError) as error:
        TruliaSource().search(FakeClient(FakeResponse(page)), preferences)
    assert "format may have changed" in str(error.value)


# --------------------------------------------------------------------------
# guards, on deliberately constructed cards
#
# These payloads are written here rather than captured: each one is a single
# card bent into the shape a guard exists for. The captured fixture above is
# what proves the parser reads a real page; this is what proves each guard
# fires. Field names and their shapes are the ones the live payload uses.
# --------------------------------------------------------------------------


def card(**overrides) -> dict:
    """One Trulia card, shaped like the live ones."""
    base = {
        "__typename": "HOME_RentalCommunity",
        "url": "/building/prism-1028-market-st-san-francisco-ca-94102-2753090638",
        "homeUrl": None,
        "typedHomeId": "2753090638_BUILDING_ID",
        "providerListingId": "abc123",
        "location": {
            "city": "San Francisco", "stateCode": "CA", "zipCode": "94102",
            "streetAddress": "1028 Market St",
            "fullLocation": "1028 Market St, San Francisco, CA 94102",
        },
        "price": {"formattedPrice": "$3,675 - $4,973/mo"},
        "bedrooms": {"formattedValue": "Studio-2 Beds", "min": 0, "max": 2},
        "bathrooms": {"min": 1, "max": 2},
        "floorSpace": {"formattedDimension": "400-900 sqft"},
        "tags": [{"formattedName": "SPECIAL OFFER", "level": "HIGHLIGHTED"}],
    }
    base.update(overrides)
    return base


def page_of(*cards: dict) -> str:
    payload = {"props": {"searchData": {"totalHomes": 1375, "homes": list(cards)}}}
    return f'<html><body><script id="__NEXT_DATA__">{json.dumps(payload)}</script></body></html>'


def read(page: str, preferences: Preferences):
    return TruliaSource().search(FakeClient(FakeResponse(page)), preferences)


def test_a_card_in_another_city_is_left_on_the_page(preferences) -> None:
    oakland = card(location={"city": "Oakland", "zipCode": "94607", "streetAddress": "1 Broadway"},
                   typedHomeId="8888_BUILDING_ID")
    assert [item.source_id for item in read(page_of(card(), oakland), preferences)] == [
        "2753090638_BUILDING_ID"
    ]


def test_the_link_is_made_absolute(preferences) -> None:
    """`url` is a path and `homeUrl` -- the field whose name suggests
    otherwise -- is null on every live card."""
    listing = read(page_of(card()), preferences)[0]
    assert listing.original_url == (
        "https://www.trulia.com/building/prism-1028-market-st-san-francisco-ca-94102-2753090638"
    )


def test_a_link_pointing_at_another_host_is_refused(preferences) -> None:
    """urljoin honours an absolute URL in the path field, so a payload that
    carried one would be stored and opened as somebody else's page."""
    assert read(page_of(card(url="https://evil.example/phish")), preferences) == []


def test_a_card_without_a_link_is_dropped(preferences) -> None:
    assert read(page_of(card(url=None)), preferences) == []


def test_the_stored_id_is_trulias_own_not_the_naming_slug(preferences) -> None:
    listing = read(page_of(card()), preferences)[0]
    assert listing.source_id == "2753090638_BUILDING_ID"
    assert "prism" not in listing.source_id.lower()


def test_a_card_with_no_stable_id_falls_back_to_the_provider_id(preferences) -> None:
    listing = read(page_of(card(typedHomeId=None)), preferences)[0]
    assert listing.source_id == "abc123"


def test_one_card_with_no_id_is_skipped_and_the_rest_are_kept(preferences) -> None:
    nameless = card(typedHomeId=None, providerListingId=None, url="/building/x-1")
    kept = read(page_of(nameless, card()), preferences)
    assert [item.source_id for item in kept] == ["2753090638_BUILDING_ID"]


def test_a_page_where_no_card_has_an_id_is_a_format_change_not_an_empty_result(
    preferences,
) -> None:
    """One card without an id is a bad card. Every card without one means the
    field this source keys on has gone, and recording that as "found nothing
    today" would hide it for as long as it lasted."""
    nameless = card(typedHomeId=None, providerListingId=None)
    with pytest.raises(SourceError) as error:
        read(page_of(nameless), preferences)
    assert "format may have changed" in str(error.value)


def test_a_larger_home_is_never_quoted_the_smallest_homes_rent() -> None:
    """The bottom of "$3,675 - $4,973/mo" belongs to the studio. Printed
    against the two-bedroom it reads as a two-bedroom going for a studio's
    rent."""
    listing = read(page_of(card()), profile("two_bedroom"))[0]
    assert listing.price is None
    assert listing.metadata["price_from"] == 3675
    assert "Rents here start at $3,675 a month for a studio" in (listing.summary or "")
    assert "the 2-bedroom rent is not published" in (listing.summary or "")
    assert "the a " not in (listing.summary or "")


def test_the_smallest_home_keeps_the_rent_that_belongs_to_it() -> None:
    listing = read(page_of(card()), profile("studio"))[0]
    assert listing.price == 3675
    assert "price_from" not in listing.metadata


def test_a_single_size_building_keeps_the_bottom_of_its_own_spread() -> None:
    """A range on a building that only lets one-bedrooms is a spread across
    its own units, so its bottom really is what a one-bedroom starts at."""
    one_bed = card(bedrooms={"formattedValue": "1 Bed", "min": 1, "max": 1},
                   price={"formattedPrice": "$3,834 - $4,002/mo"})
    listing = read(page_of(one_bed), profile("one_bedroom"))[0]
    assert listing.price == 3834
    assert "price_from" not in listing.metadata
    assert "Advertised at $3,834 - $4,002/mo." in (listing.summary or "")


def test_a_building_too_small_for_the_deal_is_dropped() -> None:
    """A building's range is what it lets, so its largest home decides."""
    one_bed = card(bedrooms={"formattedValue": "1 Bed", "min": 1, "max": 1})
    assert read(page_of(one_bed), profile("three_bedroom")) == []


def test_a_card_with_no_rent_is_kept_and_says_so(preferences) -> None:
    listing = read(page_of(card(price={"formattedPrice": "Contact for price"})), preferences)[0]
    assert listing.price is None
    assert "publishes no rent" in (listing.summary or "")


def test_a_room_for_rent_is_not_scored_as_a_whole_home(preferences) -> None:
    """A lodger's room reaching a whole-home deal is the failure here."""
    room = card(__typename="HOME_RoomForRent")
    assert read(page_of(room), preferences)[0].housing_kind == ROOM_KIND
    assert read(page_of(card()), preferences)[0].housing_kind == WHOLE_UNIT
    assert read(page_of(room), preferences)[0].listing_type == "Room in a home"


def test_a_bedroom_range_with_one_end_missing_is_not_guessed_at(preferences) -> None:
    """Read as a single number, a card with a max and no min reports a floor
    the page never stated."""
    listing = read(page_of(card(bedrooms={"min": None, "max": 2})), preferences)[0]
    assert "bedrooms" not in listing.metadata
    assert listing.price == 3675, "with no usable range the published rent stands as it is"


def test_the_address_corroboration_matches_on_is_published(preferences) -> None:
    from sf_housing.location import parse_street_address

    listing = read(page_of(card()), preferences)[0]
    assert listing.metadata["address"] == "1028 Market St"
    assert parse_street_address(listing.metadata["address"]) is not None
    assert listing.neighborhood == "SoMa"


def test_paging_stops_when_the_site_serves_page_one_again(preferences) -> None:
    """Page sixty came back as page one, card for card."""
    client = FakeClient(FakeResponse(page_of(card())))
    TruliaSource().search(client, preferences)
    assert client.requested == [
        "https://www.trulia.com/for_rent/San_Francisco,CA/",
        "https://www.trulia.com/for_rent/San_Francisco,CA/2_p/",
    ]


def test_a_page_of_only_out_of_town_cards_is_read_not_raised(preferences) -> None:
    """Cards were read; every one was elsewhere. That is an empty result, not
    a broken page, and must not put a working source into backoff."""
    oakland = card(location={"city": "Oakland", "zipCode": "94607", "streetAddress": "1 Broadway"})
    assert read(page_of(oakland), preferences) == []


def test_junk_cards_inside_a_good_payload_are_skipped_not_fatal(preferences) -> None:
    payload = {"props": {"searchData": {"homes": [None, "nonsense", {}, card()]}}}
    page = f'<html><body><script id="__NEXT_DATA__">{json.dumps(payload)}</script></body></html>'
    assert len(read(page, preferences)) == 1


def test_the_per_source_cap_is_honoured() -> None:
    preferences = parse_preferences(
        yaml.safe_dump({**yaml.safe_load(TEST_PREFERENCES), "sources": {"max_results_per_source": 1}})
    )
    page = page_of(card(), card(typedHomeId="2_BUILDING_ID", url="/building/b-2"))
    assert len(TruliaSource().search(FakeClient(FakeResponse(page)), preferences)) == 1


def test_a_rent_has_to_carry_a_currency_marker_to_count(preferences) -> None:
    """`formattedPrice` is landlord-facing copy. Read without insisting on a
    dollar sign, "Call for 2 bedroom pricing" becomes a rent of $2, which
    clears every budget ever set."""
    listing = read(page_of(card(price={"formattedPrice": "Call for 2 bedroom pricing"})), preferences)[0]
    assert listing.price is None
    assert "publishes no rent" in (listing.summary or "")


def test_trulias_own_labels_reach_the_reader(preferences) -> None:
    """"SPECIAL OFFER" is a concession worth seeing on a card."""
    summary = read(page_of(card()), preferences)[0].summary or ""
    assert "Trulia tags it special offer." in summary


def test_a_card_with_no_labels_says_nothing_about_them(preferences) -> None:
    assert "Trulia tags it" not in (read(page_of(card(tags=[])), preferences)[0].summary or "")


def test_trulias_placeholder_unit_number_is_not_shown_to_anybody(preferences) -> None:
    """32767 is the largest signed 16-bit integer, and Trulia publishes it
    where a home has no unit: "1825 Mission St #32767" arrived on a *building*
    card, which cannot have a unit number at all. One address in the 120 read
    carried it; every other suffix was an ordinary flat number, so it is
    stripped by value rather than by guessing which numbers are real."""
    listing = read(page_of(card(location={
        "city": "San Francisco", "zipCode": "94103",
        "streetAddress": "1825 Mission St #32767",
        "fullLocation": "1825 Mission St #32767, San Francisco, CA 94103"})), preferences)[0]
    assert listing.title == "1825 Mission St"
    assert "32767" not in (listing.summary or "")
    assert listing.metadata["address"] == "1825 Mission St"


def test_a_real_unit_number_is_left_alone(preferences) -> None:
    listing = read(page_of(card(location={
        "city": "San Francisco", "zipCode": "94103",
        "streetAddress": "205 9th St #28",
        "fullLocation": "205 9th St #28, San Francisco, CA 94103"})), preferences)[0]
    assert listing.title == "205 9th St #28"


# --------------------------------------------------------------------------
# the real captured page
#
# The constructed cards above prove each guard fires. These prove the parser
# reads a page Trulia actually served: five real records captured from its
# San Francisco and Oakland searches, chosen for one behaviour each.
# --------------------------------------------------------------------------


MISSION = "1001488048_BUILDING_ID"   # one bedroom count, rent published as a range
PRISM = "2753090638_BUILDING_ID"     # studio through 2-bed, so the range's floor is the studio's
ROOM = "401673398_ZPID"              # a room for rent among the buildings
STUDIO_BLDG = "2757916840_BUILDING_ID"
OAKLAND = "2747960547_BUILDING_ID"   # a real Oakland building on a real Oakland page


def real(preferences: Preferences):
    return {item.source_id: item for item in
            TruliaSource().search(FakeClient(FakeResponse(search_page())), preferences)}


def test_the_captured_page_reads_into_its_san_francisco_homes(preferences) -> None:
    assert set(real(preferences)) == {MISSION, PRISM, ROOM, STUDIO_BLDG}


def test_the_oakland_building_on_the_captured_page_is_dropped(preferences) -> None:
    assert OAKLAND not in real(preferences)


def test_a_real_range_price_is_kept_for_a_single_size_building(preferences) -> None:
    """"$3,834 - $4,002/mo" on a building letting only one-bedrooms is a
    spread across its own units, so its floor is what a one-bedroom starts
    at."""
    listing = real(preferences)[MISSION]
    assert listing.price == 3834
    assert "price_from" not in listing.metadata
    assert listing.metadata["bedrooms_low"] == listing.metadata["bedrooms_high"] == 1


def test_a_real_multi_size_building_withholds_the_larger_homes_rent() -> None:
    listings = {i.source_id: i for i in TruliaSource().search(
        FakeClient(FakeResponse(search_page())), profile("one_bedroom"))}
    listing = listings[PRISM]
    assert listing.price is None
    assert listing.metadata["price_from"] == 3675
    assert listing.metadata["bedrooms_low"] == 0 and listing.metadata["bedrooms_high"] == 2
    assert "the 1-bedroom rent is not published" in (listing.summary or "")


def test_the_real_room_for_rent_is_not_scored_as_a_whole_home(preferences) -> None:
    stored = real(preferences)
    assert stored[ROOM].housing_kind == ROOM_KIND
    assert stored[ROOM].listing_type == "Room in a home"
    assert stored[MISSION].housing_kind == WHOLE_UNIT


def test_every_real_link_is_absolute_and_on_trulia(preferences) -> None:
    """`url` is a path and `homeUrl` is null on all 120 captured records."""
    for listing in real(preferences).values():
        assert listing.original_url.startswith("https://www.trulia.com/")


def test_every_real_home_publishes_a_usable_address_and_area(preferences) -> None:
    from sf_housing.location import parse_street_address

    for listing in real(preferences).values():
        assert parse_street_address(listing.metadata["address"]) is not None
        assert listing.neighborhood, f"{listing.title} resolved to no neighbourhood"


def test_a_page_that_repeats_the_last_one_ends_the_walk(preferences) -> None:
    """Past its last real page Trulia serves page one again: page sixty came
    back card for card. At the shipped two-page depth that costs one wasted
    request, which is why the guard has to be asserted rather than inferred --
    read deeper it would re-read the same homes until the budget ran out."""
    source = TruliaSource()
    source.max_pages = 6
    client = FakeClient(FakeResponse(page_of(card())))
    listings = source.search(client, preferences)

    assert len(listings) == 1
    assert len(client.requested) == 2, "the repeat should stop the walk, not six pages of it"


def test_the_nightly_sweep_reads_far_deeper_than_a_waiting_scan(preferences) -> None:
    """Two pages when somebody is waiting, the whole city once a day. Read
    deeply at 10:00 this source spends its one chance and returns nothing for
    an hour; read shallowly forever it returns 80 of 1,365 homes."""
    from sf_housing.sources import DEEP_SWEEP_TRIGGER, _pages_for_trigger

    source = TruliaSource()
    assert _pages_for_trigger(source, "scheduled") == 2
    assert _pages_for_trigger(source, DEEP_SWEEP_TRIGGER) == 35

    shallow = FakeClient(FakeResponse(page_of(card())))
    source.search_for_trigger(shallow, preferences, "scheduled")
    assert len(shallow.requested) == 2

    deep = FakeClient(*[
        FakeResponse(page_of(card(typedHomeId=f"{n}_BUILDING_ID", url=f"/building/b-{n}")))
        for n in range(40)
    ])
    source.search_for_trigger(deep, preferences, DEEP_SWEEP_TRIGGER)
    assert len(deep.requested) == 35, "the sweep stopped short of its own depth"


def test_a_plain_search_is_always_the_shallow_one(preferences) -> None:
    """The protocol's own entry point cannot know the trigger, so it must not
    guess deep: anything calling it gets the read that does not get refused.

    Served distinct pages on purpose -- given one page repeated, the walk stops
    at the second either way and the test proves nothing about its depth."""
    client = FakeClient(*[
        FakeResponse(page_of(card(typedHomeId=f"{n}_BUILDING_ID", url=f"/building/b-{n}")))
        for n in range(40)
    ])
    TruliaSource().search(client, preferences)
    assert len(client.requested) == 2


def test_a_sweep_is_never_shallower_than_a_normal_run() -> None:
    """`deep_max_pages` is a floor to raise, not a number to obey. Set below
    `max_pages` by mistake it would make the nightly sweep -- the one run whose
    whole purpose is depth -- the shallowest read of the day."""
    from sf_housing.sources import DEEP_SWEEP_TRIGGER, _pages_for_trigger

    class Backwards:
        max_pages = 10
        deep_max_pages = 3

    assert _pages_for_trigger(Backwards(), DEEP_SWEEP_TRIGGER) == 10

    class Unpaged:
        max_pages = 4

    assert _pages_for_trigger(Unpaged(), DEEP_SWEEP_TRIGGER) == 4
    assert _pages_for_trigger(Unpaged(), "scheduled") == 4


# --------------------------------------------------------------------------
# the bathroom count, stated the same way the bedrooms are
# --------------------------------------------------------------------------


def test_a_home_carries_the_bathroom_count_trulia_states(preferences) -> None:
    """99 Trulia homes on the owner's board, not one with a bathroom count,
    while the card said "1 Bath" in the same shape it states the bedrooms.

    Three of the four homes on the captured page say it plainly; the fourth
    covers a range and is left blank, which is the same refusal the bedroom
    reader beside it makes.
    """
    found = TruliaSource().search(FakeClient(FakeResponse(search_page())), preferences)
    counted = [home for home in found if home.metadata.get("bathrooms") is not None]

    assert counted, "no Trulia home carried a bathroom count"
    assert all(0 < float(home.metadata["bathrooms"]) <= 12 for home in counted)


def test_a_building_whose_bathroom_range_is_open_states_nothing(preferences) -> None:
    """The half that matters. A building letting one-bath and two-bath homes
    says neither about the one on the card, so nothing is written rather than
    whichever end of the range would look better on the shortlist."""
    from sf_housing.sources import _stated_bathrooms

    assert _stated_bathrooms({"bathrooms": {"formattedValue": "1 Bath", "min": 1, "max": 1}}) == 1.0
    assert _stated_bathrooms({"bathrooms": {"formattedValue": "1-2 Baths", "min": 1, "max": 2}}) is None
