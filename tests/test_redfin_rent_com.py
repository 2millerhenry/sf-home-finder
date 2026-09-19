"""The two portals that answer an unattended request and publish whole
San Francisco buildings as structured data.

Both fixtures are real captured payloads, trimmed. The Rent.com search fixture
is deliberately the *contaminated* one: asked for three-bedrooms under a rent
ceiling, Rent.com returned ten cards of which one was in San Francisco and the
other nine were in Oakland, Alameda, Berkeley and Tiburon. Keeping that page as
the fixture is what makes the filters here provable rather than decorative.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import httpx
import pytest
import yaml

from sf_housing.classification import ROOM, WHOLE_UNIT, classify_listing
from sf_housing.preferences import Preferences, parse_preferences
from sf_housing.sources import (
    RedfinSource,
    RentComSource,
    SourceError,
    _bedroom_span,
    _offer_price,
    default_sources,
)
from tests.conftest import TEST_PREFERENCES


FIXTURES = Path(__file__).parent / "fixtures"


def redfin_page() -> str:
    return (FIXTURES / "redfin_search.html").read_text(encoding="utf-8")


def rent_search_page() -> str:
    return (FIXTURES / "rent_com_search.html").read_text(encoding="utf-8")


def rent_building_page() -> str:
    return (FIXTURES / "rent_com_building.html").read_text(encoding="utf-8")


def rent_clean_page() -> str:
    """A real unpadded San Francisco search, whose first card is the building
    the detail fixture belongs to. Enrichment is checked against a matching
    pair because the source refuses a page served for a different building."""
    return (FIXTURES / "rent_com_search_clean.html").read_text(encoding="utf-8")


def gateway_card(preferences: Preferences, source: RentComSource | None = None):
    """The card for the building the detail fixture belongs to.

    The source is threaded through because ``search`` is what tells ``enrich``
    which bedroom count the deal asked for; enriching on a second instance
    silently falls back to the cheapest home in the building.
    """
    listings = (source or RentComSource()).search(
        FakeClient(FakeResponse(rent_clean_page())), preferences
    )
    return next(item for item in listings if item.source_id == "lc6377711")


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
        return self.pages[min(len(self.requested) - 1, len(self.pages) - 1)]


def profile(*paths: str, maximum: int = 3000) -> Preferences:
    """A deal enabling exactly the given whole-home paths."""
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
# shared guards
# --------------------------------------------------------------------------


@pytest.mark.parametrize("source", [RedfinSource(), RentComSource()])
@pytest.mark.parametrize(
    "response",
    [
        FakeResponse("", 202),
        FakeResponse("   ", 200),
        FakeResponse("<html><body>Please verify you are a human</body></html>", 200),
        FakeResponse("<html><title>Just a moment...</title></html>", 200),
    ],
    ids=["202-empty", "blank-200", "captcha", "cloudflare"],
)
def test_a_wall_is_never_read_as_an_empty_result(source, response, preferences) -> None:
    """Rate-limited, both answer HTTP 202 with an empty body. Counted as zero
    listings that would record "this source found nothing" at exactly the
    moment the source stopped talking to us."""
    with pytest.raises(SourceError):
        source.search(FakeClient(response), preferences)


def test_the_202_message_names_the_status_it_saw(preferences) -> None:
    with pytest.raises(SourceError) as caught:
        RedfinSource().search(FakeClient(FakeResponse("", 202)), preferences)
    assert "202" in str(caught.value)


def test_a_refusal_part_way_through_keeps_the_pages_already_read() -> None:
    """Redfin delivered nothing for nine days while its pages were answering.

    Its search is a six-page walk, and the real install's log shows the same
    shape on every attempt: pages 1-3 answered HTTP 200 and page 4 answered
    202. Not one refusal on record was on the first page. Raising there threw
    three pages of San Francisco homes away, recorded the source as failed, and
    two of those pushed it into a 24-hour backoff -- so the source that was
    working was reported as broken and delivered nothing at all.

    A refusal on the first page still raises: a source that really is blocked
    must still be reported as blocked rather than as a quiet zero.
    """
    preferences = profile("one_bedroom")
    whole_page = RedfinSource().search(FakeClient(FakeResponse(redfin_page())), preferences)
    assert whole_page, "the fixture should yield homes"

    client = FakeClient(FakeResponse(redfin_page()), FakeResponse("", 202))
    kept = RedfinSource().search(client, preferences)
    assert len(kept) == len(whole_page)

    with pytest.raises(SourceError):
        RedfinSource().search(FakeClient(FakeResponse("", 202)), preferences)


class FailingAfter(FakeClient):
    """Serves its pages once each, then fails the next request as ``failure`` says."""

    def __init__(self, failure: str, *pages: FakeResponse):
        super().__init__(*pages)
        self.failure = failure

    def get(self, url, **kwargs):
        if len(self.requested) < len(self.pages):
            return super().get(url, **kwargs)
        self.requested.append(url)
        request = httpx.Request("GET", url)
        if self.failure == "timeout":
            raise httpx.ReadTimeout("The read operation timed out", request=request)
        if self.failure == "dropped":
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.", request=request)
        return FakeResponse("", int(self.failure))


@pytest.mark.parametrize("failure", ["timeout", "dropped", "404", "400"])
def test_a_later_page_that_times_out_drops_or_is_missing_keeps_the_pages_already_read(failure: str) -> None:
    """Pre-launch audit: the guard above kept the pages read before a
    refusal, but a later page that timed out, dropped the connection or
    answered 400/404/410 still threw them all away and counted a failure
    towards pausing the source -- on all seven paginated sources."""
    preferences = profile("one_bedroom")
    whole_page = RedfinSource().search(FakeClient(FakeResponse(redfin_page())), preferences)

    kept = RedfinSource().search(FailingAfter(failure, FakeResponse(redfin_page())), preferences)

    assert len(kept) == len(whole_page)


def test_a_first_page_the_site_does_not_have_is_said_in_words() -> None:
    """A 404 on the first page reached the page as httpx's own text, MDN link
    and all, where every other refusal is said in words."""
    with pytest.raises(SourceError) as caught:
        RedfinSource().search(FakeClient(FakeResponse("", 404)), profile("one_bedroom"))

    assert "404" in str(caught.value)
    assert "developer.mozilla" not in str(caught.value)


def test_every_paginated_source_keeps_its_pages_the_same_way() -> None:
    """The seven paginated sources each carry a copy of one guard (the
    PARTIAL_WALK note in sf_housing/sources.py); Redfin's is proved above.
    This holds every copy to the same catch, so none falls back to keeping
    its pages after a refusal alone."""
    from sf_housing import sources

    text = Path(sources.__file__).read_text(encoding="utf-8")
    guard = r"\n\s+if (?:read_a_card|answered):"

    assert re.findall(r"except SourceError:" + guard, text) == []
    assert len(re.findall(r"except \(SourceError, httpx\.TransportError\):" + guard, text)) == 7


@pytest.mark.parametrize(
    ("value", "expected"),
    [("0", (0, 0)), ("3", (3, 3)), ("0-3", (0, 3)), ("1-2", (1, 2)), ("", None), (None, None), ("n/a", None)],
)
def test_a_bedroom_count_may_be_a_range(value, expected) -> None:
    """Redfin writes "0-3" for a building letting studios through
    three-bedrooms. Read as an integer it becomes 0 and hides every one."""
    assert _bedroom_span(value) == expected


@pytest.mark.parametrize(
    ("node", "expected"),
    [
        ({"price": 3395}, 3395),
        ({"price": "3395"}, 3395),
        ({"price": "3,395"}, 3395),
        ({"price": "$4,733"}, 4733),
        ({"price": "348-400"}, None),
        ({"price": "0"}, None),
        ({"price": True}, None),
        ({"lowPrice": 2795}, 2795),
    ],
)
def test_a_rent_quoted_as_text_is_still_a_rent(node, expected) -> None:
    """Redfin quotes rents as strings, which read as no rent at all until they
    are accepted; a looser match would turn a floor area into a price."""
    assert _offer_price(node) == expected


# --------------------------------------------------------------------------
# Redfin
# --------------------------------------------------------------------------


def test_redfin_reads_its_cards(preferences) -> None:
    listings = RedfinSource().search(FakeClient(FakeResponse(redfin_page())), preferences)

    assert listings, "the fixture holds real cards, so the parser must return some"
    for listing in listings:
        assert listing.platform == "Redfin"
        assert listing.original_url.startswith("https://www.redfin.com/")
        assert listing.housing_kind == WHOLE_UNIT
        assert listing.source_id


def test_redfin_pairs_each_rent_with_its_own_building(preferences) -> None:
    """The page publishes unpaired Accommodation blocks too, so pairing by
    position would print one building's rent against another."""
    page = redfin_page()
    priced = {
        block["url"]: int(block["offers"]["price"])
        for block in json_blocks(page)
        if block.get("@type") == "Product" and block.get("offers", {}).get("price")
    }
    assert priced, "fixture must contain priced Products to prove the pairing"

    listings = RedfinSource().search(FakeClient(FakeResponse(page)), preferences)

    for listing in listings:
        expected = priced.get(listing.original_url)
        if expected is not None and listing.metadata.get("price_from") is None:
            assert listing.price == expected


def test_redfin_never_borrows_a_neighbours_rent(preferences) -> None:
    """A card with no Product of its own must stay priceless rather than take
    the rent of whichever card happened to be parsed nearby."""
    page = redfin_page()
    unpaired = [
        block["url"]
        for block in json_blocks(page)
        if block.get("@type") == "Accommodation"
        and block["url"] not in {b.get("url") for b in json_blocks(page) if b.get("@type") == "Product"}
    ]
    assert unpaired, "fixture must contain an unpriced card to prove this"

    listings = RedfinSource().search(FakeClient(FakeResponse(page)), preferences)
    by_url = {listing.original_url: listing for listing in listings}
    for url in unpaired:
        if url in by_url:
            assert by_url[url].price is None
            assert "no rent for this home yet" in by_url[url].summary


def json_blocks(page: str) -> list[dict]:
    blocks: list[dict] = []
    for raw in re.findall(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', page, re.S):
        parsed = json.loads(raw)
        blocks.extend(parsed if isinstance(parsed, list) else [parsed])
    return blocks


def test_redfin_asks_only_for_a_bedroom_floor() -> None:
    """Asked for a bedroom count and a rent ceiling together, Redfin pads the
    page with whatever it holds and the bedroom filter stops meaning anything.
    Verified against the live site: min-beds=3 alone returned 28 three- and
    four-bedroom cards; adding max-price=3000 returned studios and a
    ten-bedroom."""
    client = FakeClient(FakeResponse(redfin_page()))
    RedfinSource().search(client, profile("three_bedroom", maximum=3000))

    assert client.requested[0].endswith("/filter/min-beds=3")
    for url in client.requested:
        assert "max-price" not in url
        assert "3000" not in url


def test_redfin_omits_the_filter_when_the_deal_wants_everything() -> None:
    client = FakeClient(FakeResponse(redfin_page()))
    RedfinSource().search(client, profile("studio", "three_bedroom"))
    assert client.requested[0] == RedfinSource.search_url


def test_redfin_keeps_a_building_whose_largest_home_fits(preferences) -> None:
    """A "0-3" building really does have a three-bedroom in it."""
    listings = RedfinSource().search(FakeClient(FakeResponse(redfin_page())), profile("three_bedroom"))

    assert listings, "the fixture contains buildings reaching three bedrooms"
    for listing in listings:
        assert listing.metadata["bedrooms_high"] >= 3


def test_redfin_drops_a_building_that_tops_out_too_small(preferences) -> None:
    everything = RedfinSource().search(FakeClient(FakeResponse(redfin_page())), profile("studio"))
    three = RedfinSource().search(FakeClient(FakeResponse(redfin_page())), profile("three_bedroom"))

    assert len(three) < len(everything), "the bedroom floor has to exclude something"
    small = [listing for listing in three if listing.metadata["bedrooms_high"] < 3]
    assert not small


def priced_range_page() -> str:
    """The fixture, with a rent attached to a building that lets 1 through 3
    bedrooms. Redfin's own filtered pages happen to carry only exact sizes, so
    the range-plus-rent case has to be built to be tested."""
    page = redfin_page()
    card = next(
        block
        for block in json_blocks(page)
        if block.get("@type") == "Accommodation" and str(block.get("numberOfRooms")) == "1-3"
    )
    product = json.dumps(
        {
            "@context": "http://schema.org",
            "@type": "Product",
            "name": card["name"],
            "offers": {"@type": "Offer", "price": "3200", "priceCurrency": "USD"},
            "url": card["url"],
        },
        separators=(",", ":"),
    )
    return page.replace("</head>", f'<script type="application/ld+json">{product}</script></head>', 1)


def test_redfin_does_not_price_a_large_home_at_a_small_ones_rent() -> None:
    """Redfin publishes one rent per building and it belongs to the smallest
    home. Printed against a three-bedroom it would read as a three-bedroom
    going for a studio's rent."""
    listings = RedfinSource().search(FakeClient(FakeResponse(priced_range_page())), profile("three_bedroom"))

    ranged = [
        listing
        for listing in listings
        if listing.metadata["bedrooms_low"] != listing.metadata["bedrooms_high"]
    ]
    assert ranged, "the page must contain a building letting a range of sizes"
    quoted = [listing for listing in ranged if listing.metadata.get("price_from")]
    assert quoted, "and one of them must carry a published rent, or this proves nothing"

    for listing in quoted:
        assert listing.price is None, "the building's starting rent is not this home's rent"
        assert listing.metadata["price_from"] == 3200
        assert listing.metadata["bedrooms"] == 3
        assert "is not published" in listing.summary


def test_redfin_keeps_the_rent_when_it_belongs_to_the_home_shown(preferences) -> None:
    listings = RedfinSource().search(FakeClient(FakeResponse(redfin_page())), preferences)
    exact = [
        listing
        for listing in listings
        if listing.metadata["bedrooms_low"] == listing.metadata["bedrooms_high"] and listing.price
    ]
    assert exact, "fixture must contain a single-size priced building"
    for listing in exact:
        assert listing.metadata.get("price_from") is None


def test_redfin_drops_a_home_outside_san_francisco(preferences) -> None:
    """Redfin widens a thin search without saying so, and its detail pages
    carry Mill Valley homes."""
    page = redfin_page().replace('"addressLocality":"San Francisco"', '"addressLocality":"Mill Valley"')
    assert "Mill Valley" in page, "the substitution must actually have applied"

    listings = RedfinSource().search(FakeClient(FakeResponse(page)), preferences)

    # A page that parsed but held nothing local is an empty result, not a
    # failure. Only a page that could not be read at all is an error, and
    # confusing the two would either hide a format change or cry wolf.
    assert listings == []


def test_redfin_strips_the_word_undefined_from_a_missing_name(preferences) -> None:
    """Redfin renders a building with no name as the literal word."""
    page = redfin_page().replace('"name":"952', '"name":"undefined - 952', 1)
    listings = RedfinSource().search(FakeClient(FakeResponse(page)), preferences)
    assert not any(listing.title.startswith("undefined") for listing in listings)


def test_redfin_stops_when_a_page_repeats_itself(preferences) -> None:
    """Past its last real page Redfin serves the first one again, so believing
    the numbering would re-read the same buildings until the budget ran out."""
    source = RedfinSource()
    # Read past the shipped two-page limit, because at two pages the loop ends
    # of its own accord and the guard cannot be observed at all. This is what
    # would happen the day somebody raises max_pages.
    source.max_pages = 4
    client = FakeClient(FakeResponse(redfin_page()), FakeResponse(redfin_page()))

    listings = source.search(client, preferences)

    urls = [listing.original_url for listing in listings]
    assert len(urls) == len(set(urls)), "a repeated page must not duplicate listings"
    # Deduplication alone would hide this: each wasted read costs three
    # megabytes and yields nothing.
    assert len(client.requested) == 2, f"it re-read {len(client.requested) - 1} pages for nothing"


def test_redfin_respects_the_result_cap() -> None:
    capped = parse_preferences(TEST_PREFERENCES + "\nsources:\n  max_results_per_source: 3\n")
    listings = RedfinSource().search(FakeClient(FakeResponse(redfin_page())), capped)
    assert len(listings) == 3


def test_redfin_places_a_building_in_a_neighbourhood(preferences) -> None:
    listings = RedfinSource().search(FakeClient(FakeResponse(redfin_page())), preferences)
    placed = [listing for listing in listings if listing.neighborhood]
    assert len(placed) >= len(listings) - 1, "the fixture carries a street address and a map pin for nearly all"


def test_redfin_reads_no_detail_pages(preferences) -> None:
    """A Redfin building page publishes priced Products for its neighbours and
    none for the home being viewed, so an enrichment step could only ever
    attach somebody else's rent."""
    assert RedfinSource.detail_budget == 0
    assert not hasattr(RedfinSource, "enrich")


def test_redfin_raises_when_the_page_holds_no_cards(preferences) -> None:
    with pytest.raises(SourceError, match="format may have changed"):
        RedfinSource().search(FakeClient(FakeResponse("<html><body>hello</body></html>")), preferences)


# --------------------------------------------------------------------------
# Rent.com
# --------------------------------------------------------------------------


def test_rent_com_keeps_only_the_san_francisco_card(preferences) -> None:
    """The captured page is a real San Francisco search: ten cards, one of
    which is in San Francisco."""
    page = rent_search_page()
    cards = [block for block in json_blocks(page) if block.get("@type") == "ApartmentComplex"]
    localities = {block["address"]["addressLocality"] for block in cards}
    assert len(cards) == 10 and len(localities) > 1, "fixture must be the padded page"

    listings = RentComSource().search(FakeClient(FakeResponse(page)), preferences)

    assert len(listings) == 1
    assert "san-francisco" in listings[0].original_url


def test_rent_com_drops_what_the_page_itself_calls_padding(preferences) -> None:
    """Rent.com names its own out-of-area results in expandedSearchIds, which
    catches a padded San Francisco address that the city check would not."""
    page = rent_search_page()
    padded = set(json.loads(re.search(r'__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.S).group(1))
                 ["props"]["pageProps"]["pageData"]["expandedSearchIds"])
    assert padded, "fixture must carry the padding list"

    listings = RentComSource().search(FakeClient(FakeResponse(page)), preferences)

    for listing in listings:
        assert RentComSource._listing_id(listing.original_url) not in padded


def test_rent_com_ignores_a_padded_card_even_in_san_francisco(preferences) -> None:
    page = rent_search_page()
    kept = RentComSource().search(FakeClient(FakeResponse(page)), preferences)[0]
    listing_id = RentComSource._listing_id(kept.original_url)
    padded = page.replace('"expandedSearchIds":[', f'"expandedSearchIds":["{listing_id}",', 1)
    assert listing_id in padded.split('"expandedSearchIds"')[1][:400]

    assert RentComSource().search(FakeClient(FakeResponse(padded)), preferences) == []


def test_rent_com_asks_only_for_a_bedroom_count() -> None:
    """Asked for bedrooms and a rent ceiling together, Rent.com padded nine of
    ten cards with other cities and stopped paginating."""
    client = FakeClient(FakeResponse(rent_search_page()))
    RentComSource().search(client, profile("three_bedroom", maximum=3000))

    assert client.requested[0].endswith("/3-bedrooms")
    for url in client.requested:
        assert "max-price" not in url


def test_rent_com_stops_when_the_page_number_disagrees(preferences) -> None:
    """Past the last real page Rent.com serves page one again while its own
    payload keeps saying "1"."""
    client = FakeClient(FakeResponse(rent_search_page()), FakeResponse(rent_search_page()))
    RentComSource().search(client, preferences)

    assert len(client.requested) == 2, "it must stop rather than walk to max_pages"


def test_rent_com_reads_a_page_two_that_really_is_page_two(preferences) -> None:
    second = rent_search_page().replace('"pageNumber":1', '"pageNumber":2')
    second = second.replace("6-doric-alley-san-francisco-ca-lv3253944554", "9-elm-alley-san-francisco-ca-lv9999999999")
    client = FakeClient(FakeResponse(rent_search_page()), FakeResponse(second), FakeResponse(rent_search_page()))

    listings = RentComSource().search(client, preferences)

    assert len(listings) == 2, "a genuine second page must be read"
    assert len(client.requested) >= 2


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.rent.com/r/6-doric-alley-san-francisco-ca-lv3253944554", "lv3253944554"),
        ("https://www.rent.com/apartment/garden-court-apartments-alameda-ca-lc5930580", "lc5930580"),
        ("https://www.rent.com/california/san-francisco-apartments", None),
    ],
)
def test_rent_com_reads_a_listing_id_from_its_link(url, expected) -> None:
    assert RentComSource._listing_id(url) == expected


def enriched(preferences: Preferences):
    source = RentComSource()
    listing = gateway_card(preferences, source)
    return source.enrich(FakeClient(FakeResponse(rent_building_page())), listing)


def test_rent_com_records_a_building_size_it_can_act_on(preferences) -> None:
    """The metadata reader stops at three digits, so a 1,254-home building put
    there would be recorded as one of 125 and read as within a 50-unit rule it
    plainly breaks."""
    listing = enriched(preferences)

    assert listing.building_units == 1254
    assert classify_listing(listing).building_units == 1254


def test_rent_com_prices_the_home_the_search_was_about() -> None:
    """Rent.com publishes a rent per bedroom count, so unlike every other
    building source the right home can be priced exactly."""
    source = RentComSource()
    listing = gateway_card(profile("one_bedroom"), source)
    completed = source.enrich(FakeClient(FakeResponse(rent_building_page())), listing)

    assert completed.metadata["bedrooms"] == 1
    assert completed.price == 5439


def test_rent_com_falls_back_when_the_building_lacks_that_size() -> None:
    """A building the search returned that turns out to hold nothing of the
    wanted size keeps its own cheapest home, so the mismatch is scored rather
    than hidden."""
    source = RentComSource()
    listing = gateway_card(profile("four_bedroom"), source)
    completed = source.enrich(FakeClient(FakeResponse(rent_building_page())), listing)

    assert completed.metadata["bedrooms"] == 0
    assert completed.price == 4733


def test_rent_com_marks_a_room_as_a_room(preferences) -> None:
    source = RentComSource()
    listing = gateway_card(preferences, source)
    page = rent_building_page().replace('"roomForRent":false', '"roomForRent":true')

    completed = source.enrich(FakeClient(FakeResponse(page)), listing)

    assert completed.housing_kind == ROOM


def test_rent_com_reports_a_detail_page_with_no_payload(preferences) -> None:
    source = RentComSource()
    listing = gateway_card(preferences, source)

    with pytest.raises(SourceError):
        source.enrich(FakeClient(FakeResponse("<html><head></head></html>")), listing)


def test_rent_com_states_an_earliest_move_in(preferences) -> None:
    listing = enriched(preferences)
    assert re.fullmatch(r"[A-Z][a-z]+ \d{1,2}, \d{4}", str(listing.metadata["available_on"]))
    assert "Available" in listing.summary


def test_rent_com_does_not_describe_free_homes_as_units(preferences) -> None:
    """"5 units available" would be read by the building-size parser as a
    five-unit building."""
    listing = enriched(preferences)
    assert "units" not in listing.summary.casefold()


def test_rent_com_raises_when_the_page_holds_no_cards(preferences) -> None:
    with pytest.raises(SourceError, match="format may have changed"):
        RentComSource().search(FakeClient(FakeResponse("<html><body>hi</body></html>")), preferences)


# --------------------------------------------------------------------------
# wiring
# --------------------------------------------------------------------------


def test_both_sources_run_without_setup_in_every_branch() -> None:
    class Mailbox:
        pass

    for sources in (default_sources(), default_sources(Mailbox())):
        by_platform = {source.platform: source for source in sources}
        for platform in ("Redfin", "Rent.com"):
            assert platform in by_platform, f"{platform} is missing from a default_sources branch"
            source = by_platform[platform]
            assert source.mode == "automatic"
            assert source.manual_reason is None
            assert getattr(source, "connector_key", None) is None


def test_neither_source_is_offered_as_an_email_connector() -> None:
    """Both read the site directly, so a Gmail setup card for either would ask
    for work that does nothing."""
    from sf_housing.connectors import GMAIL_PROVIDERS
    from sf_housing.coverage import ALERT_SETUP_SEARCHES

    names = {name for _, name in GMAIL_PROVIDERS}
    assert not names & {"Redfin", "Rent.com"}
    assert not set(ALERT_SETUP_SEARCHES) & {"Redfin", "Rent.com"}


def test_the_ready_check_still_probes_the_same_four_sources() -> None:
    """The live probe takes the first four automatic connector-free sources in
    default_sources order, so where a new source is placed is behaviour."""
    probed = [
        source.platform
        for source in default_sources()
        if getattr(source, "mode", "setup") == "automatic"
        and not getattr(source, "connector_key", None)
    ][:4]
    assert probed == ["Craigslist", "Listings Project", "Abacus (small buildings)", "SpareRoom"]


def test_the_scanner_runs_both_before_the_slow_helpers() -> None:
    import inspect

    from sf_housing.scanner import Scanner

    source = inspect.getsource(Scanner)
    order = {
        name: int(value)
        for name, value in re.findall(r'"([^"]+)": (\d+),', source.split("priority = {")[1].split("}")[0])
    }
    assert order["Redfin"] < order["Furnished Finder"]
    assert order["Rent.com"] < order["Facebook Marketplace"]


# --------------------------------------------------------------------------
# what a page that held nothing is allowed to claim
# --------------------------------------------------------------------------


def test_a_page_full_of_homes_is_not_a_wall(preferences) -> None:
    """The block signals are whole-body substring tests written for counting a
    search result. A three-megabyte listing page that mentions a captcha in its
    own JavaScript would fail two scans and put a working source into a day of
    backoff, so a page that parsed into real cards is never called a wall."""
    page = redfin_page().replace("</head>", "<script>var recaptchaSiteKey='x';</script></head>")

    listings = RedfinSource().search(FakeClient(FakeResponse(page)), preferences)

    assert listings, "a page with real cards must still be read"


@pytest.mark.parametrize("source", [RedfinSource(), RentComSource()])
def test_a_wall_with_no_cards_is_named_a_wall(source, preferences) -> None:
    wall = "<html><body>Please complete the CAPTCHA to continue.</body></html>"
    with pytest.raises(SourceError, match="bot check"):
        source.search(FakeClient(FakeResponse(wall)), preferences)


@pytest.mark.parametrize("source", [RedfinSource(), RentComSource()])
def test_an_unreadable_page_is_named_a_format_change(source, preferences) -> None:
    with pytest.raises(SourceError, match="format may have changed"):
        source.search(FakeClient(FakeResponse("<html><body>Welcome</body></html>")), preferences)


def test_rent_com_never_claims_a_home_was_posted_today(preferences) -> None:
    """updatedAt is when Rent.com last touched its own record. Stored as
    listing_timestamp it becomes published_at, is rewritten on every scan,
    renders as "Posted today" and pins every building to the top of the newest
    sort forever."""
    listing = enriched(preferences)

    assert "listing_timestamp" not in listing.metadata
    assert listing.metadata["record_updated"]


def test_rent_com_publishes_a_move_in_date_the_scoring_can_read(preferences) -> None:
    """An ISO date under a key of its own is a fact nothing consults."""
    from datetime import date

    from sf_housing.scoring import _available_on

    listing = enriched(preferences)

    assert _available_on(listing) == date(2026, 5, 19)


def test_rent_com_ignores_a_date_on_a_home_nobody_can_take(preferences) -> None:
    """A unit that is not available cannot be the earliest date somebody could
    move in, however early its own stamp reads."""
    from datetime import date

    from sf_housing.scoring import _available_on

    source = RentComSource()
    listing = gateway_card(preferences, source)
    page = rent_building_page().replace(
        '"dateAvailable":"2026-10-01T00:00:00.000Z","deposit":"600","isAvailable":false',
        '"dateAvailable":"2020-01-01T00:00:00.000Z","deposit":"600","isAvailable":false',
    )
    assert "2020-01-01" in page, "the fixture must carry an unavailable unit to prove this"

    completed = source.enrich(FakeClient(FakeResponse(page)), listing)

    assert _available_on(completed) == date(2026, 5, 19)


# --------------------------------------------------------------------------
# what happens when a detail page does not answer
# --------------------------------------------------------------------------


def test_rent_com_asks_again_after_a_detail_page_refuses(preferences) -> None:
    """Rent.com's card carries no price, no bedroom count and no building
    size: everything worth scoring is behind the detail fetch, and the site
    answers 202 when rate-limited. The scanner only re-reads a home whose
    summary is still its title, so a card that described itself would be
    stranded, priceless and sizeless, by one bad minute."""
    listing = RentComSource().search(FakeClient(FakeResponse(rent_search_page())), preferences)[0]

    assert listing.summary == listing.title
    assert listing.metadata["detail_pending"] is True


def test_the_scanner_re_reads_anything_still_marked_pending() -> None:
    """The general form of the Craigslist clause beside it."""
    import inspect

    from sf_housing.scanner import Scanner

    body = inspect.getsource(Scanner)
    assert 'stored_metadata.get("detail_pending") is True' in body


def test_rent_com_stops_asking_once_the_page_answers(preferences) -> None:
    listing = enriched(preferences)
    assert "detail_pending" not in listing.metadata
    assert listing.summary != listing.title


# --------------------------------------------------------------------------
# a building page that is not the building asked for
# --------------------------------------------------------------------------


def test_rent_com_refuses_a_building_in_another_city(preferences) -> None:
    """The scan client follows redirects, so a building that moved would put
    another city's rents, size and move-in date on this listing. And
    sf_area_from_address happily returns an SF neighbourhood for an East Bay
    street when the city sits in a separate field."""
    source = RentComSource()
    listing = gateway_card(preferences, source)
    page = rent_building_page().replace('"city":"San Francisco"', '"city":"Oakland"')
    assert "Oakland" in page

    with pytest.raises(SourceError, match="Oakland"):
        source.enrich(FakeClient(FakeResponse(page)), listing)


def test_rent_com_refuses_a_page_for_a_different_building(preferences) -> None:
    source = RentComSource()
    listing = gateway_card(preferences, source)
    page = rent_building_page().replace('"id":"lc6377711"', '"id":"lc9999999"')

    with pytest.raises(SourceError, match="lc9999999"):
        source.enrich(FakeClient(FakeResponse(page)), listing)


def test_rent_com_keeps_a_stable_identity_when_a_building_is_renamed(preferences) -> None:
    """The slug carries the building's name, so a rename would orphan the
    stored row and create a duplicate rather than update it."""
    page = rent_search_page()
    listing = RentComSource().search(FakeClient(FakeResponse(page)), preferences)[0]
    renamed = page.replace("6-doric-alley-san-francisco-ca", "six-doric-alley-apartments-san-francisco-ca")

    after = RentComSource().search(FakeClient(FakeResponse(renamed)), preferences)[0]

    assert listing.source_id == after.source_id == "lv3253944554"


def test_rent_com_says_when_a_building_has_left_the_market(preferences) -> None:
    """Left unsaid, a delisted building looks exactly like a dropped
    connection and keeps its place on the shortlist forever."""
    source = RentComSource()
    listing = gateway_card(preferences, source)
    page = rent_building_page().replace('"offMarket":false', '"offMarket":true')
    assert '"offMarket":true' in page

    completed = source.enrich(FakeClient(FakeResponse(page)), listing)

    assert completed.metadata["verified_inactive"] is True


# --------------------------------------------------------------------------
# scan order
# --------------------------------------------------------------------------


def test_the_heavy_new_sources_do_not_overtake_the_light_old_ones() -> None:
    """Redfin reads two three-megabyte pages and Rent.com reads a page per
    building. Placed ahead of a single public JSON call, they would push it
    towards a time budget that is already over-subscribed."""
    import inspect

    from sf_housing.scanner import Scanner

    body = inspect.getsource(Scanner)
    order = {
        name: int(value)
        for name, value in re.findall(r'"([^"]+)": (\d+),', body.split("priority = {")[1].split("}")[0])
    }
    assert order["SF Housing Portal"] < order["Redfin"]
    assert order["SF Housing Portal"] < order["Rent.com"]
    assert order["Redfin"] < order["Rent.com"], "the one that reads a page per building goes last"


def test_the_ready_check_names_the_sources_that_need_no_account() -> None:
    """Written by hand the sentence named four while seven ran."""
    from sf_housing.diagnostics import _no_account_sources
    from sf_housing.sources import default_sources

    sentence = _no_account_sources(default_sources())

    for platform in ("Craigslist", "Redfin", "Rent.com", "SF Housing Portal", "Apartment List"):
        assert platform in sentence
    assert sentence.endswith("do not require an account.")


@pytest.mark.parametrize("source", [RedfinSource(), RentComSource()])
def test_each_refusal_says_which_one_it_was(source, preferences) -> None:
    """All three raise, so a test that only checks the type cannot tell an
    empty body from a page whose shape changed -- and sending somebody to look
    for a parser bug when the site simply refused wastes the one clue there
    is."""
    with pytest.raises(SourceError, match="empty page"):
        source.search(FakeClient(FakeResponse("   ", 200)), preferences)
    with pytest.raises(SourceError, match="HTTP 202"):
        source.search(FakeClient(FakeResponse("", 202)), preferences)
    with pytest.raises(SourceError, match="bot check"):
        source.search(FakeClient(FakeResponse("<html>press & hold</html>")), preferences)
    with pytest.raises(SourceError, match="format may have changed"):
        source.search(FakeClient(FakeResponse("<html><body>ok</body></html>")), preferences)


def test_rent_com_treats_a_deleted_building_as_deleted(preferences) -> None:
    """A 404 is an answer. Raising instead would make a home that is gone look
    exactly like a dropped connection, and it would keep its place forever."""
    source = RentComSource()
    listing = gateway_card(preferences, source)

    completed = source.enrich(FakeClient(FakeResponse("", 404)), listing)

    assert completed.metadata["verified_inactive"] is True
    assert "detail_pending" not in completed.metadata
    assert "no longer listed" in completed.summary


def test_rent_com_reads_an_income_restriction_that_is_a_list(preferences) -> None:
    """The payload publishes a list, so `is True` could never fire."""
    source = RentComSource()
    listing = gateway_card(preferences, source)
    page = rent_building_page().replace('"incomeRestrictions":[]', '"incomeRestrictions":["LIHTC"]')
    assert "LIHTC" in page, "the fixture must carry the list shape to prove this"

    completed = source.enrich(FakeClient(FakeResponse(page)), listing)

    assert completed.metadata["below_market_rate"] is True


def unpadded_oakland_page() -> str:
    """The fixture with an East Bay card the page does NOT call padding.

    Every out-of-area card in the real capture is named in expandedSearchIds,
    so that list alone hides whether the city is checked at all. Rent.com is
    under no obligation to keep naming them."""
    page = rent_search_page()
    oakland = next(
        block
        for block in json_blocks(page)
        if block.get("@type") == "ApartmentComplex"
        and block["address"]["addressLocality"] == "Oakland"
    )
    return page.replace(f'"{RentComSource._listing_id(oakland["url"])}",', "", 1)


def test_rent_com_checks_the_city_even_when_the_page_admits_nothing(preferences) -> None:
    page = unpadded_oakland_page()
    freed = [
        block
        for block in json_blocks(page)
        if block.get("@type") == "ApartmentComplex"
        and block["address"]["addressLocality"] == "Oakland"
        and RentComSource._listing_id(block["url"])
        not in set(json.loads(
            re.search(r'__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.S).group(1)
        )["props"]["pageProps"]["pageData"]["expandedSearchIds"])
    ]
    assert freed, "the substitution must have freed an Oakland card from the padding list"

    listings = RentComSource().search(FakeClient(FakeResponse(page)), preferences)

    assert [listing.original_url for listing in listings] == [
        block["url"]
        for block in json_blocks(page)
        if block.get("@type") == "ApartmentComplex"
        and block["address"]["addressLocality"] == "San Francisco"
    ]


def test_rent_com_disbelieves_a_page_that_renumbers_itself(preferences) -> None:
    """Past its last page Rent.com serves page one again while its payload
    keeps saying "1". A page carrying different buildings under the wrong
    number is the case only the number can catch: deduplication sees new
    listings and reads on."""
    second = rent_search_page().replace(
        "6-doric-alley-san-francisco-ca-lv3253944554", "9-elm-alley-san-francisco-ca-lv9999999999"
    )
    client = FakeClient(FakeResponse(rent_search_page()), FakeResponse(second), FakeResponse(second))

    listings = RentComSource().search(client, preferences)

    assert len(client.requested) == 2, "a page numbered 1 is not page 2, whatever it contains"
    assert len(listings) == 1


def test_rent_com_enriches_against_the_deal_its_search_was_run_for() -> None:
    """search() is what tells enrich() which bedroom count was asked for. On a
    fresh instance it has nothing to go on and falls back to the cheapest home
    in the building, which for a three-bedroom search is a studio."""
    asked = RentComSource()
    listing = gateway_card(profile("one_bedroom"), asked)

    matched = asked.enrich(FakeClient(FakeResponse(rent_building_page())), listing)
    stranger = RentComSource().enrich(FakeClient(FakeResponse(rent_building_page())), listing)

    assert matched.metadata["bedrooms"] == 1 and matched.price == 5439
    assert stranger.metadata["bedrooms"] == 0, "a source that never searched knows of no deal"


def test_a_fresh_install_names_the_sources_that_will_run(tmp_path) -> None:
    """The sentence is only shown before anything has run, which is the one
    screen a new install reads first, and is exactly why nothing noticed it
    naming four sources while seven ran."""
    from sf_housing.database import Repository
    from sf_housing.diagnostics import _source_check

    repository = Repository(tmp_path / "housing.sqlite3")
    repository.initialize()

    check = _source_check(repository, default_sources())

    assert check.status == "not_applicable"
    for platform in ("Craigslist", "Redfin", "Rent.com", "Apartment List", "SF Housing Portal"):
        assert platform in check.explanation, f"{platform} runs without an account but is not named"


@pytest.mark.parametrize("source", [RedfinSource(), RentComSource()])
@pytest.mark.parametrize("status", [403, 429])
def test_a_refusal_reads_as_a_refusal(source, status, preferences) -> None:
    """Both of these turn away an unattended request often enough that the
    message is the usual one, not the rare one. Left to raise_for_status it
    reads "HTTPStatusError: Client error '429 Too Many Requests'", which tells
    a reader nothing they can act on."""
    with pytest.raises(SourceError, match=f"turned away an unattended request \\(HTTP {status}\\)"):
        source.search(FakeClient(FakeResponse("<html>nope</html>", status)), preferences)


def test_both_sources_ask_the_way_a_browser_asks() -> None:
    """Measured one request each: the monitor's own User-Agent gets 403 from
    Redfin and 429 from Rent.com; a browser string gets 200 from both. Without
    this every scan fails and both sources sit in backoff."""
    import inspect

    from sf_housing.sources import _BROWSER_HEADERS

    assert "Mozilla/5.0" in _BROWSER_HEADERS["User-Agent"]
    for source in (RedfinSource, RentComSource):
        body = inspect.getsource(source)
        requests = body.count("client.get(")
        carried = body.count("headers=_BROWSER_HEADERS")
        assert requests and carried == requests, (
            f"{source.__name__} makes {requests} request(s) but only {carried} would be answered"
        )


def test_the_starting_rate_note_names_the_size_without_doubling_its_article() -> None:
    """Three sources share this sentence. `_bedroom_phrase` carries its own
    article, so composing it after "the" shipped "the a 2-bedroom rent is not
    published" on Redfin and Uloop for as long as both have existed."""
    from sf_housing.sources import _starting_rate_note

    note = _starting_rate_note(4377, 0, 2)
    assert note == (
        "Rents here start at $4,377 a month for a studio; "
        "the 2-bedroom rent is not published."
    )
    assert "the a " not in note


def test_a_larger_home_is_never_quoted_the_smallest_homes_rent(preferences) -> None:
    """The building's published rent belongs to its smallest home. Read end to
    end so the note and the withheld price stay in step."""
    listings = RedfinSource().search(FakeClient(FakeResponse(redfin_page())), profile("two_bedroom"))
    spread = [item for item in listings if item.metadata.get("price_from")]
    assert spread, "fixture no longer contains a building letting several sizes"
    for item in spread:
        assert item.price is None
        assert "the a " not in (item.summary or "")
        assert "rent is not published" in (item.summary or "")
