"""Zillow, read directly rather than waited on.

Zillow was a setup source here for as long as this app has existed: connect an
inbox, save a search on Zillow, wait for it to email you. It had delivered
nothing. Its own search page answers an ordinary request -- including one that
identifies itself honestly, which is rarer here than the browser string most of
these need -- and carries 41 rentals of a stated 2,568 in the page itself.

The fixture is real captured records: a building letting three sizes, a building
letting one, a single home whose numbers are numbers, a real Oakland record, and
one bent to FOR_SALE at $1,495,000. That last one is the single check standing
between this source and a million-dollar "rent", because a sale and a let are
the same record with a different status.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import httpx
import pytest
import yaml

from sf_housing.classification import ROOM, WHOLE_UNIT
from sf_housing.preferences import Preferences, parse_preferences
from sf_housing.models import ListingCandidate
from sf_housing.sources import (
    DEEP_SWEEP_TRIGGER,
    CraigslistSource,
    PartialReadError,
    SourceError,
    ZillowSource,
    card_proves_listed,
    default_sources,
)
from tests.conftest import TEST_PREFERENCES


FIXTURES = Path(__file__).parent / "fixtures"

PRISM = "37.781826--122.41123"        # building, three sizes
TRINITY = "37.777885--122.41319"      # building, one size
MISSION = "459079645"                 # a single home, and the 32767 placeholder
OAKLAND_CITY = "Oakland"
SALE = "sale-1"


def search_page() -> str:
    return (FIXTURES / "zillow_search.html").read_text(encoding="utf-8")


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
    return parse_preferences(
        yaml.safe_dump(
            {
                "profile_version": 1,
                "profile": {
                    "state": "active",
                    "enabled_paths": list(paths),
                    "budgets": {
                        path: {"maximum_monthly": maximum, "minimum_monthly": 100, "occupants": 3}
                        for path in paths
                    },
                    "geography": {"anywhere_in_sf": True},
                },
            }
        )
    )


@pytest.fixture
def preferences() -> Preferences:
    return parse_preferences(TEST_PREFERENCES)


def found(preferences: Preferences, page: str | None = None):
    return ZillowSource().search(FakeClient(FakeResponse(page or search_page())), preferences)


def by_id(listings):
    return {item.source_id: item for item in listings}


def doctored(identifier: str, **changes) -> str:
    """The captured page with one record's fields changed."""
    page = search_page()
    payload = json.loads(
        re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.S).group(1)
    )
    results = payload["props"]["pageProps"]["searchPageState"]["cat1"]["searchResults"]["listResults"]
    target = next(x for x in results if str(x.get("id")) == identifier)
    target.update(changes)
    return (
        '<html><body><script id="__NEXT_DATA__">' + json.dumps(payload) + "</script></body></html>"
    )


# --------------------------------------------------------------------------
# the check that matters most
# --------------------------------------------------------------------------


def test_a_home_for_sale_is_never_read_as_a_home_to_let(preferences) -> None:
    """A sale and a let are the same record with a different statusType, and
    the price field means a sale price on one and a monthly rent on the other.
    The fixture carries one bent to FOR_SALE at $1,495,000."""
    stored = by_id(found(preferences))

    assert SALE not in stored
    assert all(item.price is None or item.price < 100_000 for item in stored.values())


def test_the_status_is_what_is_checked_not_the_url(preferences) -> None:
    page = doctored(MISSION, statusType="FOR_SALE", unformattedPrice=1_650_000)
    assert MISSION not in by_id(found(preferences, page))


def test_only_a_card_zillow_marks_for_rent_is_read_as_a_home_to_let(preferences) -> None:
    """A home Zillow no longer lets -- off the market, sold, or shown as the
    home-value page that offers to let its owner claim it -- arrives in the
    same ``listResults`` as a rental, differing only in its status. The check
    is an allow-list for that reason, and this says so: one written as a
    refusal of FOR_SALE alone would pass every other test in this file and
    let every off-market card onto the board.

    A card with no status at all is refused for the same reason. Zillow saying
    nothing about what a record is, is not Zillow saying it is a rental."""
    for status in ("OTHER", "SOLD", "RECENTLY_SOLD", "PRE_FORECLOSURE", "", None):
        assert MISSION not in by_id(found(preferences, doctored(MISSION, statusType=status)))
    # The gate lets a real rental through, so none of the above is passing by
    # the reader having simply stopped reading.
    assert MISSION in by_id(found(preferences))


def test_a_home_whose_card_states_no_rent_is_still_read(preferences) -> None:
    """A card without a rent is a card without a rent, never proof that the
    home is gone. Zillow's rentals search does publish them: on the owner's
    board 38 of 2,061 Zillow homes are stored unpriced, every one of them an
    apartment building or a unit inside one, and Zillow's own search was still
    returning most of them the day this was written. Refusing a card for
    saying no rent would have hidden those 38 real homes and removed no ghost
    at all, so both shapes the source reads are kept and simply say so."""
    single = by_id(found(preferences, doctored(MISSION, unformattedPrice=None)))
    assert MISSION in single
    assert single[MISSION].price is None

    quiet = [{"price": "Price Unknown", "beds": "1", "roomForRent": False}]
    building = by_id(found(preferences, doctored(PRISM, units=quiet)))
    assert PRISM in building
    assert building[PRISM].price is None
    assert "at a rent it does not publish" in (building[PRISM].summary or "")


def test_a_home_in_another_city_is_left_on_the_page(preferences) -> None:
    """The fixture carries a real Oakland record from Zillow's Oakland page."""
    for item in found(preferences):
        assert OAKLAND_CITY.lower() not in (item.metadata.get("address") or "").lower()
    assert len(found(preferences)) == 3


# --------------------------------------------------------------------------
# two shapes on one page
# --------------------------------------------------------------------------


def test_both_shapes_on_the_page_are_read(preferences) -> None:
    """Buildings carry `units` with rents as text; single homes carry numbers.
    Read as one shape, four homes in every page of 41 lose their bedroom count
    and 37 lose their rent."""
    assert set(by_id(found(preferences))) == {PRISM, TRINITY, MISSION}


def test_a_building_is_priced_at_the_size_the_deal_asked_for() -> None:
    """Prism lets a studio at $3,675, a one-bedroom at $4,517 and a
    two-bedroom at $4,973."""
    assert by_id(ZillowSource().search(FakeClient(FakeResponse(search_page())), profile("studio")))[PRISM].price == 3675
    assert by_id(ZillowSource().search(FakeClient(FakeResponse(search_page())), profile("one_bedroom")))[PRISM].price == 4517
    assert by_id(ZillowSource().search(FakeClient(FakeResponse(search_page())), profile("two_bedroom")))[PRISM].price == 4973


def test_a_buildings_other_sizes_are_reported_without_repeating_the_one_quoted() -> None:
    summary = by_id(ZillowSource().search(
        FakeClient(FakeResponse(search_page())), profile("studio")
    ))[PRISM].summary or ""

    assert "Its studio homes start at $3,675 a month." in summary
    assert "also lets a 1-bedroom from $4,517, a 2-bedroom from $4,973" in summary
    assert summary.count("$3,675") == 1


def test_a_single_home_reads_its_own_numbers(preferences) -> None:
    listing = by_id(found(preferences))[MISSION]

    assert listing.price == 2300
    assert listing.metadata["bedrooms"] == 0
    assert listing.metadata["floor_area"] == "265 sq ft"
    assert "1 bathroom." in (listing.summary or "")
    assert listing.listing_type == "Home"


def test_a_bedroom_count_written_as_the_word_studio_is_still_a_studio(preferences) -> None:
    """Zillow writes these as strings, and a studio arrives as "0" on some
    buildings and "Studio" on others. Read only as digits, the second becomes
    no bedroom count at all."""
    page = doctored(TRINITY, units=[{"price": "$2,900+", "beds": "Studio", "roomForRent": False}])
    listing = by_id(found(preferences, page))[TRINITY]

    assert listing.metadata["bedrooms"] == 0
    assert listing.price == 2900


def test_a_room_let_inside_a_home_is_not_scored_as_a_whole_home(preferences) -> None:
    """Zillow marks this on the unit. None of the captured pages carried one,
    so it is induced here -- a lodger's room reaching a whole-home deal is the
    failure, and it must not wait for the first one to appear in the wild."""
    page = doctored(TRINITY, units=[{"price": "$1,400+", "beds": "1", "roomForRent": True}])

    assert by_id(found(preferences, page))[TRINITY].housing_kind == ROOM
    assert by_id(found(preferences))[TRINITY].housing_kind == WHOLE_UNIT


def test_what_is_free_is_never_recorded_as_the_size_of_the_building(preferences) -> None:
    listing = by_id(found(preferences))[PRISM]

    assert listing.metadata["homes_available"] == 6
    assert listing.building_units is None
    assert "6 units" not in (listing.summary or "")


# --------------------------------------------------------------------------
# identity and links
# --------------------------------------------------------------------------


def test_one_building_listed_twice_does_not_overwrite_itself(preferences) -> None:
    """Zillow lists a building as itself and again as a unit inside it, both
    pointing at one detail page: Vara arrived as a studio building at $3,852
    and as 1863 Mission St #306 at $3,300. `canonical_url` is unique, so
    stored as they come the second silently replaces the first."""
    from sf_housing.database import canonicalize_url

    same_page = doctored(TRINITY, detailUrl=json.loads(
        re.search(r'"detailUrl":\s*("[^"]+")', search_page()).group(1)
    ))
    links = {canonicalize_url(item.original_url) for item in found(preferences, same_page)}

    assert len(links) == len(found(preferences, same_page))


def test_the_link_is_absolute_and_on_zillow(preferences) -> None:
    """Some detailUrls arrive as a path and some as a full address."""
    for listing in found(preferences):
        assert listing.original_url.startswith("https://www.zillow.com/")


def test_a_link_pointing_at_another_host_is_refused(preferences) -> None:
    assert MISSION not in by_id(found(preferences, doctored(MISSION, detailUrl="https://evil.example/x")))


def test_the_placeholder_unit_number_is_not_shown_to_anybody(preferences) -> None:
    """32767 is the largest signed 16-bit integer, and Zillow publishes it
    where a home has no unit -- on Trulia too, which it owns."""
    listing = by_id(found(preferences))[MISSION]

    assert listing.title == "1825 Mission St"
    assert "32767" not in (listing.summary or "")
    assert listing.metadata["address"] == "1825 Mission St"


def test_every_home_publishes_the_address_corroboration_matches_on(preferences) -> None:
    from sf_housing.location import parse_street_address

    for listing in found(preferences):
        assert parse_street_address(listing.metadata["address"]) is not None


# --------------------------------------------------------------------------
# paging, walls, and cost
# --------------------------------------------------------------------------


def test_paging_stops_when_the_page_repeats_itself(preferences) -> None:
    client = FakeClient(FakeResponse(search_page()))
    ZillowSource().search(client, preferences)

    assert len(client.requested) == 2
    assert client.requested[0] == "https://www.zillow.com/san-francisco-ca/rentals/"
    assert client.requested[1] == "https://www.zillow.com/san-francisco-ca/rentals/2_p/"


def test_the_nightly_sweep_reads_at_least_as_deep_as_a_waiting_scan() -> None:
    """The page claims 2,568 rentals and hands over about a thousand: page 25
    is refused however patiently it is asked. So the sweep's job here is to
    probe a little past today's wall, not to chase a number paging cannot
    reach."""
    from sf_housing.sources import DEEP_SWEEP_TRIGGER, _pages_for_trigger

    source = ZillowSource()
    assert _pages_for_trigger(source, "scheduled") == source.max_pages
    assert _pages_for_trigger(source, DEEP_SWEEP_TRIGGER) >= source.max_pages


def test_the_per_source_cap_is_honoured() -> None:
    preferences = parse_preferences(
        yaml.safe_dump({**yaml.safe_load(TEST_PREFERENCES), "sources": {"max_results_per_source": 2}})
    )
    assert len(ZillowSource().search(FakeClient(FakeResponse(search_page())), preferences)) == 2


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
        ZillowSource().search(FakeClient(response), preferences)


@pytest.mark.parametrize(
    "payload_text",
    [
        "{not json",
        "[1,2,3]",
        '{"props": {}}',
        '{"props": []}',
        '{"props": {"pageProps": {"searchPageState": {"cat1": {"searchResults": {"listResults": "no"}}}}}}',
    ],
    ids=["malformed", "not-an-object", "no-searchPageState", "props-is-a-list", "results-wrong-type"],
)
def test_a_payload_this_no_longer_understands_is_reported_not_crashed(
    payload_text, preferences
) -> None:
    page = f'<html><body><script id="__NEXT_DATA__">{payload_text}</script></body></html>'
    with pytest.raises(SourceError) as error:
        ZillowSource().search(FakeClient(FakeResponse(page)), preferences)
    assert "format may have changed" in str(error.value)


def test_junk_records_inside_a_good_payload_are_skipped_not_fatal(preferences) -> None:
    page = search_page()
    payload = json.loads(
        re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.S).group(1)
    )
    results = payload["props"]["pageProps"]["searchPageState"]["cat1"]["searchResults"]
    results["listResults"] = [None, "nonsense", {}, *results["listResults"]]
    doctored_page = (
        '<html><body><script id="__NEXT_DATA__">' + json.dumps(payload) + "</script></body></html>"
    )

    assert len(found(preferences, doctored_page)) == 3


def test_a_page_of_only_out_of_town_homes_is_read_not_raised(preferences) -> None:
    page = search_page().replace('"San Francisco"', '"Oakland"')
    assert ZillowSource().search(FakeClient(FakeResponse(page)), preferences) == []


# --------------------------------------------------------------------------
# it stopped being a setup source
# --------------------------------------------------------------------------


def test_zillow_runs_without_anybody_connecting_anything() -> None:
    """It was a setup source for as long as this app has existed -- connect an
    inbox, save a search on Zillow, wait -- and had delivered nothing."""
    class FakeMailbox:
        credential = None

        def configured(self):
            return False

    for label, sources in (
        ("no inbox", default_sources()),
        ("inbox connected", default_sources(FakeMailbox())),
    ):
        zillow = [item for item in sources if item.platform == "Zillow"]
        assert len(zillow) == 1, f"{label}: {len(zillow)} sources called Zillow"
        assert zillow[0].mode == "automatic", label
        assert not getattr(zillow[0], "connector_key", None), label


def test_the_page_no_longer_offers_zillow_as_something_to_set_up(tmp_path) -> None:
    """The email list is derived from what is not checked directly, so this
    follows on its own -- which is how Zumper left it. Asserted on the rendered
    page because the day it stops following, somebody is asked to set up a
    source that already runs, to add nothing."""
    import re as _re

    from fastapi.testclient import TestClient

    from sf_housing.app import create_app
    from sf_housing.preferences import ensure_preferences
    from sf_housing.settings import Settings

    data = tmp_path / "data"
    settings = Settings(
        data_dir=data,
        preferences_path=data / "config" / "preferences.yaml",
        database_path=data / "housing.sqlite3",
        log_path=data / "test.log",
    )
    data.mkdir(parents=True, exist_ok=True)
    (data / "config").mkdir(parents=True, exist_ok=True)
    # A finished deal, or the page redirects to onboarding instead.
    from tests.test_named_checks import PREFERENCES

    settings.preferences_path.write_text(PREFERENCES, encoding="utf-8")
    ensure_preferences(settings.preferences_path)
    with TestClient(create_app(settings=settings, sources=None, enable_scheduler=False)) as client:
        page = client.get("/alerts").text

    already = page[page.index('class="already-list"') : page.index("</p>", page.index('class="already-list"'))]
    assert ">Zillow<" in already, "Zillow is not shown among the sources that already work"

    # And it is gone from the four the page still asks somebody to connect.
    steps = page[page.index("already-list") :]
    offered = _re.findall(r'<summary>.*?>([A-Za-z. ]+)</span>\s*<span>about', steps, _re.S)
    assert "Zillow" not in offered, offered


def test_a_newly_collected_zillow_home_needs_no_page_fetched_for_it() -> None:
    """The card carries everything a new home needs, so nothing is spent
    filling one in. That is what a detail budget of zero buys, and it is
    unchanged -- ``enrich`` exists for the other question, asked later: is
    this home still let? The card cannot answer that one (see
    ``card_proves_listed``), so the page is read instead.
    """
    assert ZillowSource.detail_budget == 0
    assert callable(ZillowSource.enrich)


def test_the_source_keeps_no_per_deal_state() -> None:
    assert vars(ZillowSource()) == {}


def test_being_turned_away_is_reported_as_rate_limiting_not_breakage(preferences) -> None:
    """A 403 and a page whose shape changed are different problems with
    different answers -- wait, or fix the parser. Read as a page of zero
    results both come out as "the format may have changed", which sends
    somebody to look at code that is fine."""
    with pytest.raises(SourceError) as refused:
        ZillowSource().search(FakeClient(FakeResponse("", 429)), preferences)
    assert "rate-limiting rather than broken" in str(refused.value)

    with pytest.raises(SourceError) as changed:
        ZillowSource().search(
            FakeClient(FakeResponse("<html><body>hello</body></html>")), preferences
        )
    assert "format may have changed" in str(changed.value)


def test_zillow_runs_before_the_sources_that_fetch_a_page_per_building(
    repository, preferences
) -> None:
    """Six search pages and no detail reads, and the most reliable of the
    large portals. It must not queue behind the ones that fetch a page per
    building."""
    from sf_housing.scanner import Scanner
    from sf_housing.sources import ApartmentListSource, RentComSource

    scanner = Scanner(
        repository,
        lambda: preferences,
        [RentComSource(), ApartmentListSource(), ZillowSource()],
        detail_delay_seconds=0,
    )
    order = [item.platform for item in scanner._eligible_sources("scheduled")]

    assert order.index("Zillow") < order.index("Apartment List")
    assert order.index("Zillow") < order.index("Rent.com")


# --------------------------------------------------------------------------
# how deep the search actually goes
# --------------------------------------------------------------------------


class Refused:
    """A refusal that raises the way httpx does, rather than asserting."""

    status_code = 400
    text = ""

    def raise_for_status(self):
        raise httpx.HTTPStatusError("400", request=None, response=None)


def renamed(page: str, suffix: str) -> str:
    """The same page with every id changed, standing in for a later page.

    Both keys, because the reader takes whichever it finds first and a page
    whose homes are all already seen ends the search on its own.
    """
    for key in ("id", "zpid"):
        page = re.sub(
            rf'"{key}":"([^"]+)"', lambda m: f'"{key}":"{m.group(1)}{suffix}"', page
        )
    return page


def test_the_wall_at_the_end_of_the_results_is_not_an_error(preferences) -> None:
    """Zillow serves about a thousand homes and then refuses the next page
    outright rather than answering with an empty one. Raising there would
    throw away every home already read in order to report the page after the
    last one."""
    page_one = FakeResponse(search_page())
    page_two = FakeResponse(renamed(search_page(), "b"))
    wall = Refused()
    client = FakeClient(page_one, page_two, wall)

    listings = ZillowSource().search(client, preferences)

    assert listings, "the homes read before the wall have to survive it"
    assert len(client.requested) == 3, "it stops asking once it is refused"


def test_a_refusal_on_the_very_first_page_is_still_a_failure(preferences) -> None:
    """Nothing has been read yet, so this is Zillow turning the app away
    rather than the end of the results, and it has to be reported."""
    client = FakeClient(Refused())

    with pytest.raises((SourceError, httpx.HTTPStatusError)):
        ZillowSource().search(client, preferences)


def test_the_search_reads_far_enough_to_reach_what_zillow_serves() -> None:
    """Six pages was 246 homes out of the roughly one thousand Zillow will
    actually hand over, so three quarters of the reachable inventory was
    never asked for."""
    source = ZillowSource()

    assert source.max_pages >= 24, "the reachable pages are not being read"
    # Page 25 is refused, so aiming far past it only buys refused requests.
    assert source.deep_max_pages <= 40
    assert source.deep_max_pages >= source.max_pages


# --------------------------------------------------------------------------
# past the cap: several narrower searches instead of one wide one
# --------------------------------------------------------------------------


class BandClient:
    """Answers per URL, so a test can give each band its own results."""

    def __init__(self, pages: dict[str, FakeResponse] | None = None, default: FakeResponse | None = None):
        self.pages = pages or {}
        self.default = default or FakeResponse(search_page())
        self.requested: list[str] = []
        self.options: list[dict] = []

    def get(self, url, **kwargs):
        self.requested.append(url)
        self.options.append(kwargs)
        for fragment, response in self.pages.items():
            if fragment in url:
                return response
        return self.default


def unpaced() -> ZillowSource:
    """The reader without its second between pages, for tests about what it asks.

    Pacing has tests of its own below; everywhere else the pause only makes a
    sweep of fakes take fifteen seconds.
    """
    source = ZillowSource()
    source.PAGE_PAUSE_SECONDS = 0
    return source


BAND_PATH = re.compile(r"/homes/for_rent/San-Francisco,-CA_rb/(\d+)-(\d*)_mp/(\d+)_p/$")


def band_asked(url: str) -> tuple[tuple[int | None, int | None], int] | None:
    """The rent band and page a URL asks Zillow for, or None for any other URL.

    Read off the path the way Zillow reads it: "0-2000" is up to $2,000, and
    "9000-" has no ceiling.
    """
    from urllib.parse import urlsplit

    found = BAND_PATH.search(urlsplit(url).path)
    if not found:
        return None
    low, high, page = found.groups()
    return (int(low) or None, int(high) if high else None), int(page)


def test_a_check_somebody_pressed_reads_one_plain_search(preferences) -> None:
    """Slicing costs several times the requests. A person waiting on a spinner
    is not who should pay for the long tail."""
    client = BandClient()
    unpaced().search_for_trigger(client, preferences, "scheduled")

    assert all(band_asked(url) is None for url in client.requested), client.requested[:3]
    assert all(url.startswith(ZillowSource.search_url) for url in client.requested)


def test_the_nightly_sweep_asks_every_band(preferences) -> None:
    """One search reaches 984 homes of the 2,565 Zillow states it holds. The
    rest are only reachable by asking narrower questions."""
    source = unpaced()
    client = BandClient()
    source.search_for_trigger(client, preferences, DEEP_SWEEP_TRIGGER)

    asked = {band_asked(url)[0] for url in client.requested if band_asked(url)}
    for band in source.PRICE_BANDS:
        assert band in asked, f"band {band} was never asked for"


def test_every_band_lands_under_the_cap_zillow_enforces() -> None:
    """The whole point is that each question is narrow enough to be answered
    in full. Measured against live Zillow, the largest band holds 671 homes
    against a cap of about 984; a band over it loses its overflow in exactly
    the silence this exists to end."""
    source = ZillowSource()
    reachable = source.max_pages * 41

    assert source.PRICE_BANDS[0][0] is None, "the cheapest homes need no floor"
    assert source.PRICE_BANDS[-1][1] is None, "the dearest need no ceiling"
    edges = [high for _, high in source.PRICE_BANDS[:-1]]
    assert edges == sorted(edges), "the bands have to climb"
    for index, (low, high) in enumerate(source.PRICE_BANDS[1:], start=1):
        assert low == source.PRICE_BANDS[index - 1][1], "a gap between bands is homes nobody asks for"
    assert reachable >= 900, "the cap this is measured against moved"


def test_a_band_asks_zillow_for_rentals_only() -> None:
    """Without it the bands fill with homes for sale, whose prices mean
    something else entirely and would land in the pool as rents. The path says
    it: Zillow echoed ``isForRent`` true and every for-sale kind false for a
    band asked for under /homes/for_rent/."""
    for band in ZillowSource.PRICE_BANDS:
        assert ZillowSource()._band_url(band, 1).startswith(
            "https://www.zillow.com/homes/for_rent/"
        ), band


def test_a_band_is_asked_for_in_the_one_form_that_both_filters_and_is_allowed() -> None:
    """Two forms fail, in opposite ways. On the plain search's address,
    "2500-3500_price/" and "2500-3500_mp/" are accepted and ignored -- 200 and
    the unfiltered page, 12 of 94 prices inside the band -- which is eight
    copies of one search that all look right. Zillow's own query state
    filters, and is ``Disallow: /*?searchQueryState=*`` in its robots.txt: the
    shape every band of every nightly sweep was sent in. The rent path under
    /homes/for_rent/ does both jobs, and is spelled the way Zillow read it back
    on 18 September."""
    source = ZillowSource()

    for band in source.PRICE_BANDS:
        url = source._band_url(band, 1)
        assert "searchQueryState" not in url and "?" not in url, url
        assert not url.startswith(source.search_url), "path filters are ignored there"
        assert band_asked(url) == (band, 1), url

    assert source._band_url((None, 2000), 1).endswith("/0-2000_mp/1_p/")
    assert source._band_url((2000, 2600), 1).endswith("/2000-2600_mp/1_p/")
    assert source._band_url((9000, None), 1).endswith("/9000-_mp/1_p/")


def test_page_two_of_a_band_asks_for_page_two_of_that_band() -> None:
    """The page lives in the path, and so does page one: the bare band path is
    not among the ones robots.txt allows, and "1_p/" is, so it is asked for as
    that rather than left off."""
    source = ZillowSource()

    assert source._band_url((2000, 3000), 1).endswith("/2000-3000_mp/1_p/")
    assert source._band_url((2000, 3000), 2).endswith("/2000-3000_mp/2_p/")


def robots_rules(text: str) -> list[tuple[bool, str]]:
    """The rules Zillow gives every crawler: the ``User-agent: *`` group.

    As RFC 9309 groups them -- a run of user-agent lines, then its rules.
    Returned as (is an allow, path pattern).
    """
    rules: list[tuple[bool, str]] = []
    agents: list[str] = []
    in_rules = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        field, value = (part.strip() for part in line.split(":", 1))
        field = field.casefold()
        if field == "user-agent":
            if in_rules:
                agents, in_rules = [], False
            agents.append(value)
        elif field in {"allow", "disallow"}:
            in_rules = True
            if "*" in agents and value:
                rules.append((field == "allow", value))
    return rules


def robots_allow(rules: list[tuple[bool, str]], url: str) -> bool:
    """RFC 9309: the longest matching rule decides, and an allow wins a tie."""
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    target = parts.path + (f"?{parts.query}" if parts.query else "")
    best: tuple[int, bool] | None = None
    for allow, pattern in rules:
        anchored = pattern.endswith("$")
        body = re.escape(pattern[:-1] if anchored else pattern).replace(r"\*", ".*")
        if re.match(body + (r"\Z" if anchored else ""), target):
            if best is None or (len(pattern), allow) > best:
                best = (len(pattern), allow)
    return best is None or best[1]


def test_every_page_zillow_is_asked_for_is_one_its_robots_txt_allows() -> None:
    """The nightly sweep sent every one of its bands, every night, in a shape
    Zillow's robots.txt disallows for all crawlers. Checked here against the
    file as Zillow published it on 18 September, for every page either read can
    ask for -- and the old band URL is checked too, so a matcher that allows
    everything cannot pass this."""
    rules = robots_rules((FIXTURES / "zillow_robots.txt").read_text(encoding="utf-8"))
    source = ZillowSource()
    deepest = max(source.max_pages, source.deep_max_pages)

    asked = [source._page_url(page) for page in range(1, deepest + 1)]
    asked += [
        source._band_url(band, page)
        for band in source.PRICE_BANDS
        for page in range(1, deepest + 1)
    ]
    refused = [url for url in asked if not robots_allow(rules, url)]
    assert not refused, refused[:3]

    old_band = (
        "https://www.zillow.com/san-francisco-ca/rentals/?searchQueryState="
        "%7B%22filterState%22%3A%7B%22mp%22%3A%7B%22max%22%3A2000%7D%7D%7D"
    )
    assert not robots_allow(rules, old_band), "the matcher never says no"
    assert not robots_allow(rules, source.BAND_SEARCH_URL + "2000-2600_mp/"), (
        "a band path without a page is disallowed, which is why page one is 1_p/"
    )


def test_a_home_in_two_bands_reaches_the_pool_once(preferences) -> None:
    """A building whose rents straddle a boundary is returned on both sides of
    it, and canonical_url is UNIQUE."""
    client = BandClient()
    listings = ZillowSource().search_for_trigger(client, preferences, DEEP_SWEEP_TRIGGER)

    identifiers = [item.source_id for item in listings]
    assert len(identifiers) == len(set(identifiers)), "the same home came back twice"


def test_a_band_reads_its_own_pages_even_where_an_earlier_band_overlapped(
    preferences,
) -> None:
    """Whether a page is a band's last is a question about that band's own
    pages. Asked of every home found so far, a band stopped at its first page
    the moment that page held anything an earlier band already had -- so the
    homes deeper in it were never reached. That cost about a thousand homes
    against live Zillow before it was measured.

    Here every band's first page is identical, which is the worst case: with
    the question asked globally, only the first band ever gets past page one.
    """
    source = unpaced()
    # The same first page for every band, and a second page of new homes.
    client = BandClient(pages={"_mp/2_p/": FakeResponse(renamed(search_page(), "p2"))})

    source.search_for_trigger(client, preferences, DEEP_SWEEP_TRIGGER)

    reached_page_two = [url for url in client.requested if "_mp/2_p/" in url]
    assert len(reached_page_two) == len(source.PRICE_BANDS), (
        "a band stopped at page one because an earlier band had already seen its homes"
    )


def test_a_refused_band_keeps_the_homes_the_others_found(preferences) -> None:
    """Zillow refuses outright past a search's last page. With bands, raising
    there would throw away the bands still to come as well."""
    source = unpaced()
    client = BandClient(pages={"/4000-5500_mp/": Refused()})

    listings = source.search_for_trigger(client, preferences, DEEP_SWEEP_TRIGGER)

    assert listings, "one refused band lost every other band's homes"


class RateLimited:
    """Zillow turning an unattended request away, as httpx reports it."""

    status_code = 403
    text = ""

    def raise_for_status(self):  # pragma: no cover - _require_page acts first
        raise httpx.HTTPStatusError("403", request=None, response=None)


def test_being_turned_away_is_not_read_as_the_end_of_the_results(preferences) -> None:
    """The wall past a search's last page is a 400. A 403 is Zillow declining
    to serve us at all, and reading it as an ending would collect a little,
    report success, and come back tomorrow to be turned away again -- the
    backoff would never hear about it."""
    source = unpaced()
    client = BandClient(pages={"/4000-5500_mp/": RateLimited()})

    with pytest.raises(PartialReadError) as refused:
        source.search_for_trigger(client, preferences, DEEP_SWEEP_TRIGGER)

    assert isinstance(refused.value, SourceError), "the refusal still reads as a failure"
    assert "403" in str(refused.value)
    # And it no longer costs what was already read: the bands before it travel
    # with the failure, and a site that has just said no is not asked again.
    assert refused.value.listings, "the bands read before the refusal were thrown away"
    assert "/4000-5500_mp/" in client.requested[-1], "it went on asking after being refused"


class Moved:
    """Zillow sending a band's page somewhere else, as httpx reports it unfollowed."""

    status_code = 301
    text = ""

    def raise_for_status(self):
        raise httpx.HTTPStatusError("301", request=None, response=None)


def test_a_band_past_its_last_page_is_not_followed_into_another_search(preferences) -> None:
    """Past a band's last page Zillow redirected to the start of the unfiltered
    search. Followed, that page was read as the band's next one and one more was
    asked for: four requests to end each band where one will do, thirty-two of
    the sixty-nine the real sweep sent on 18 September. A redirect is the end of
    the band, and it is never followed -- the plain search is read on its own
    schedule, and a redirect target is not a page anybody checked was
    allowed."""
    source = unpaced()
    client = BandClient(pages={"_mp/2_p/": Moved()})

    listings = source.search_for_trigger(client, preferences, DEEP_SWEEP_TRIGGER)

    assert listings
    assert len(client.requested) == 2 * len(source.PRICE_BANDS), client.requested
    assert all(band_asked(url) for url in client.requested)
    assert all(options.get("follow_redirects") is False for options in client.options)


def test_the_plain_search_still_follows_where_zillow_sends_it(preferences) -> None:
    """Only a band's pages stay put; the plain search is Zillow's own address
    for San Francisco rentals, and wherever it answers from is where it is."""
    client = BandClient()
    unpaced().search_for_trigger(client, preferences, "scheduled")

    assert client.options and all(options.get("follow_redirects") is True for options in client.options)


def band_page() -> str:
    """A real band page, trimmed: Zillow's answer to 2000-2600 on 18 September."""
    return (FIXTURES / "zillow_band_search.html").read_text(encoding="utf-8")


def test_zillow_says_which_band_it_answered_and_that_is_what_is_believed() -> None:
    """Every page echoes the search it ran, and the echo is the only thing
    that tells a band from the plain search -- both are a page of San Francisco
    rentals. Read from the real page, 2000-2600 is the band it answered and no
    other; a page that echoes nothing is given the benefit of the doubt."""
    state = ZillowSource._search_state(band_page())

    assert not ZillowSource._band_ignored(state, (2000, 2600))
    assert ZillowSource._band_ignored(state, (2600, 3200))
    assert ZillowSource._band_ignored(state, (None, 2000))
    assert not ZillowSource._band_ignored(ZillowSource._search_state(search_page()), (2600, 3200))
    assert ZillowSource._rows(state), "the real page's homes are read from the same state"

    # The two open ends, as Zillow echoed them on 18 September for "0-2000_mp"
    # and "9000-_mp": a floor of 0 is no floor, and a missing ceiling is none.
    def echoed(low, high):
        return {"queryState": {"filterState": {"monthlyPayment": {"min": low, "max": high}}}}

    assert not ZillowSource._band_ignored(echoed(0, 2000), (None, 2000))
    assert not ZillowSource._band_ignored(echoed(9000, None), (9000, None))
    assert ZillowSource._band_ignored(echoed(9000, 12000), (9000, None))


def test_a_band_zillow_did_not_apply_is_not_walked_as_the_plain_search(preferences) -> None:
    """Path filters on the plain search's address were accepted and ignored,
    and nothing noticed until the prices were counted. Should the band path go
    the same way, every band would walk the unfiltered search to the bottom:
    eight times thirty pages a night, from every install, for one search's
    homes. The echo says so on the first page, and the band stops there."""
    unfiltered = band_page().replace(',"monthlyPayment":{"min":2000,"max":2600}', "")
    assert "monthlyPayment" not in unfiltered

    class EveryPageNew(BandClient):
        """Zillow ignoring the band: the plain search, a fresh page every time."""

        def get(self, url, **kwargs):
            super().get(url, **kwargs)
            page = band_asked(url)[1]
            return FakeResponse(renamed(unfiltered, f"-{page}"))

    source = unpaced()
    client = EveryPageNew()

    listings = source.search_for_trigger(client, preferences, DEEP_SWEEP_TRIGGER)

    assert len(client.requested) == len(source.PRICE_BANDS), len(client.requested)
    assert listings, "what the first pages held is still kept"


def test_pages_are_spaced_so_a_sweep_is_not_a_burst(preferences, monkeypatch) -> None:
    """Zillow blocks by address and it lasts: about three hundred requests over
    half an hour drew a 403 that outlived six hours, on a browser string and an
    honest one alike. Unpaced, eight bands is fifty-two requests inside a
    minute -- a burst rate several times the average that drew it."""
    import sf_housing.sources as sources

    waited: list[float] = []
    monkeypatch.setattr(sources.time, "sleep", lambda seconds: waited.append(seconds))
    client = BandClient()

    ZillowSource().search_for_trigger(client, preferences, DEEP_SWEEP_TRIGGER)

    assert waited, "the pages go out as fast as the network allows"
    assert len(waited) == len(client.requested) - 1, "every page after the first waits"
    assert all(pause == ZillowSource.PAGE_PAUSE_SECONDS for pause in waited)


def test_the_pause_carries_across_bands(preferences, monkeypatch) -> None:
    """The rate Zillow sees is from this address. It does not reset because a
    new band started, so neither does the count."""
    import sf_housing.sources as sources

    waited: list[float] = []
    monkeypatch.setattr(sources.time, "sleep", lambda seconds: waited.append(seconds))
    client = BandClient()

    ZillowSource().search_for_trigger(client, preferences, DEEP_SWEEP_TRIGGER)

    # One un-paused request in total, not one per band.
    assert len(client.requested) - len(waited) == 1


def test_the_pacing_still_fits_inside_both_ceilings() -> None:
    """A pause that outlasts the ceiling abandons the source, which loses more
    than a block would."""
    from sf_housing.scanner import (
        DEEP_SOURCE_CEILING_SECONDS,
        SOURCE_HARD_CEILING_SECONDS,
    )

    source = ZillowSource()
    interactive = source.max_pages * source.PAGE_PAUSE_SECONDS
    sweep = 52 * source.PAGE_PAUSE_SECONDS

    assert source.PAGE_PAUSE_SECONDS > 0, "the requests go out as one burst"
    assert interactive < SOURCE_HARD_CEILING_SECONDS / 2, "no room left to actually read"
    assert sweep < DEEP_SOURCE_CEILING_SECONDS / 2


def test_zillow_asks_not_to_be_read_again_within_the_quarter_hour() -> None:
    """Zillow blocks by address for hours, on rate. Twenty-four pages a press,
    pressed while waiting, is how a person earns that. A floor caps it at four
    reads an hour however often the button is pressed."""
    source = ZillowSource()
    floor_minutes = source.min_seconds_between_reads / 60
    reads_per_hour = 60 / floor_minutes
    worst_case_per_minute = reads_per_hour * source.max_pages / 60

    assert floor_minutes >= 10, "a press still costs a full read"
    # Roughly ten a minute sustained is what drew the block being defended against.
    assert worst_case_per_minute < 3, worst_case_per_minute


def test_the_page_leads_with_the_names_somebody_recognises(tmp_path) -> None:
    """The order on the page is not the order the sources are read in. Scan
    order opened the pitch with Craigslist, Listings Project and a small
    landlord nobody has heard of, and put Zillow fourteenth."""
    import re as _re

    from fastapi.testclient import TestClient

    from sf_housing.app import create_app
    from sf_housing.preferences import ensure_preferences
    from sf_housing.settings import Settings
    from tests.test_named_checks import PREFERENCES

    data = tmp_path / "data"
    settings = Settings(
        data_dir=data,
        preferences_path=data / "config" / "preferences.yaml",
        database_path=data / "housing.sqlite3",
        log_path=data / "housing.log",
    )
    data.mkdir(parents=True, exist_ok=True)
    (data / "config").mkdir(parents=True, exist_ok=True)
    settings.preferences_path.write_text(PREFERENCES, encoding="utf-8")
    ensure_preferences(settings.preferences_path)
    with TestClient(create_app(settings=settings, sources=None, enable_scheduler=False)) as client:
        page = client.get("/alerts").text

    start = page.index('class="already-list"')
    already = page[start : page.index("</p>", start)]
    # The name is the chip's own trailing text, after its logo or letter mark.
    shown = [n.strip() for n in _re.findall(r">\s*([A-Za-z][^<>]*?)\s*</span>", already) if n.strip()]

    assert shown[:3] == ["Zillow", "Trulia", "Redfin"], shown[:6]
    assert shown.index("Zillow") < shown.index("AvalonBay"), shown


# ---------------------------------------------------------------------------
# reading a home's own page, because the card cannot be taken at its word
# ---------------------------------------------------------------------------

# The two shapes, cut down from real pages. A live one carries the status
# inside a JSON string, so it arrives escaped; an off-market one carries only
# "OTHER", for this home and the ones Zillow suggests instead of it.
LIVE_PAGE = (
    '<html><body><script>{"props":"{\\"homeStatus\\":\\"FOR_RENT\\",'
    '\\"price\\":3200}"}</script><h1>$3,200/mo</h1>Request a tour</body></html>'
)
OFF_MARKET_PAGE = (
    '<html><body><span>Off market</span><h1>Price Unknown</h1>'
    '<script>{"homeStatus":"OTHER"}</script><button>Claim home</button></body></html>'
)


def a_home() -> ListingCandidate:
    return ListingCandidate(
        platform="Zillow",
        source_id="465182922",
        title="502 Precita Ave",
        original_url="https://www.zillow.com/homedetails/502-Precita-Ave/465182922_zpid/",
        price=7395,
        neighborhood="Bernal Heights",
    )


def read(page: FakeResponse) -> ListingCandidate:
    return ZillowSource().enrich(FakeClient(page), a_home())


def test_a_home_whose_own_page_is_off_the_market_is_proved_gone() -> None:
    """Zillow's search goes on returning homes its pages have taken down.

    502 Precita Ave and 161 Albion St were both arriving as FOR_RENT cards on
    the days their own pages read "Off market" -- and three of eight active
    Zillow homes sampled from the owner's board were off the market, about 790
    of its 2,103. Nothing could catch them: the search never stops returning
    them, so absence says nothing, and this source had no ``enrich`` at all,
    so the page was never read. They left only by ageing out after 21 days.
    """
    home = read(FakeResponse(OFF_MARKET_PAGE))

    assert home.metadata["verified_inactive"] is True
    assert "off the market" in home.metadata["verification_concern"]


def test_a_home_its_page_still_lets_keeps_its_place_and_counts_as_read() -> None:
    """The other half, and the one that must never be got wrong.

    A new copy rather than the one passed in, because the scanner reads an
    unchanged object as "this source went nowhere" and would not record that
    the page had been read at all.
    """
    home = read(FakeResponse(LIVE_PAGE))

    assert "verified_inactive" not in home.metadata
    assert home.metadata["zillow_page_checked"] is True
    assert home.price == 7395


def test_a_page_that_says_neither_thing_leaves_the_home_exactly_as_it_was() -> None:
    """An unread page must never cost a home its place.

    Zillow renders its status into page data; a page that arrives without it
    and without the words a reader would see has answered while telling us
    nothing. The home is handed back untouched, which the scanner reads as
    nothing learned, and it is tried again.
    """
    home = a_home()
    assert ZillowSource().enrich(FakeClient(FakeResponse("<html>a wall</html>")), home) is home


@pytest.mark.parametrize("status", [404, 410])
def test_a_home_zillow_has_removed_outright_is_proved_gone(status: int) -> None:
    """Said as plainly as Craigslist's 410, and read the same way."""
    home = read(FakeResponse("", status))

    assert home.metadata["verified_inactive"] is True
    assert str(status) in home.metadata["verification_concern"]


@pytest.mark.parametrize("status", [403, 429, 500])
def test_a_refusal_is_never_read_as_a_home_being_gone(status: int) -> None:
    """The failure that would matter: a site turning us away, read as every
    home on it having been taken down."""
    with pytest.raises(SourceError):
        read(FakeResponse("", status))


def test_a_zillow_card_is_not_taken_as_proof_the_home_is_still_listed() -> None:
    """Which is what sends its homes to be re-read in the first place.

    Every other source's card is its own current answer, so re-reading the
    page it just handed over would spend the recheck allowance learning
    nothing. Zillow's card is the one that lies.
    """
    assert card_proves_listed(ZillowSource) is False
    assert card_proves_listed(CraigslistSource) is True
