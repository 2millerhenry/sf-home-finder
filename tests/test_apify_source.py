from __future__ import annotations

import os

import pytest

from sf_housing.apify import ApifyTokenStore
from sf_housing.sources import (
    ApifyFacebookMarketplaceSource,
    SourceError,
    facebook_coordinate_neighborhood,
)


VALID_TOKEN = "apify_api_abcdefghijklmnopqrstuvwxyz123456"


class FakeResponse:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return [
            {
                "listingTitle": "Sunny private room in a Victorian house",
                "itemUrl": "https://www.facebook.com/marketplace/item/123456789/?ref=search",
                "listingPrice": {"formatted_amount": "$1,500"},
                "locationText": {"text": "Bernal Heights, San Francisco"},
                "description": {
                    "text": "Month-to-month with two chill roommates, a garden, and natural light."
                },
                "isLive": True,
                "timestamp": "2026-07-20T10:00:00Z",
            }
        ]


class FakeClient:
    def __init__(self):
        self.call = None

    def post(self, url, **kwargs):
        self.call = (url, kwargs)
        return FakeResponse()


def test_apify_facebook_result_is_normalized_without_exposing_token(tmp_path, preferences) -> None:
    tokens = ApifyTokenStore(tmp_path / "apify-token.txt")
    tokens.save(VALID_TOKEN)
    source = ApifyFacebookMarketplaceSource(tokens)
    client = FakeClient()

    listings = source.search(client, preferences)

    assert len(listings) == 1
    listing = listings[0]
    assert listing.platform == "Facebook Marketplace"
    assert listing.source_id == "123456789"
    assert listing.original_url == "https://www.facebook.com/marketplace/item/123456789/"
    assert listing.price == 1500
    assert listing.neighborhood == "Bernal Heights"
    assert "Month-to-month" in listing.summary
    url, request = client.call
    assert VALID_TOKEN not in url
    assert request["headers"] == {"Authorization": f"Bearer {VALID_TOKEN}"}
    # An empty board gets one deep read; see the steady-state test below.
    assert request["json"]["resultsLimit"] == 100
    starts = [entry["url"] for entry in request["json"]["startUrls"]]
    assert any("propertyrentals" in url for url in starts), "whole units"
    assert any("sublet" in url for url in starts), "and sublets, which never reach that feed"
    assert request["json"]["includeListingDetails"] is True
    assert request["timeout"] == 75.0
    # The rentals feed still leads, and the band is a quarter either side of
    # the deal rather than a five-percent collar.
    assert starts[0] == (
        "https://www.facebook.com/marketplace/114952118516947/propertyrentals/"
        "?sortBy=creation_time_descend&radius=10&exact=false&minPrice=750&maxPrice=9375"
    )


def test_apify_token_is_saved_private(tmp_path) -> None:
    path = tmp_path / "apify-token.txt"
    tokens = ApifyTokenStore(path)

    tokens.save(VALID_TOKEN)

    assert tokens.token == VALID_TOKEN
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_facebook_coordinate_only_pin_maps_to_supplied_target_area() -> None:
    # A live Facebook Property Rentals card in the supplied Mission Dolores
    # search box. Facebook did not provide a human neighborhood label.
    assert facebook_coordinate_neighborhood("37.77032 -122.41304 San Francisco CA") == "Mission Dolores"
    assert facebook_coordinate_neighborhood("37.75780 -122.40093 San Francisco CA") == "Potrero Hill"
    assert facebook_coordinate_neighborhood("37.80346 -122.44019 San Francisco CA") == "Marina"
    assert facebook_coordinate_neighborhood("37.70364 -122.46227 Daly City CA") is None


def test_apify_facebook_filters_non_sf_cards_and_keeps_structured_rental_facts(tmp_path, preferences) -> None:
    class MixedResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return [
                {
                    "listingTitle": "Sunny private room",
                    "itemUrl": "https://www.facebook.com/marketplace/item/111/",
                    "listingPrice": {"amount": "1500.00", "currency": "USD"},
                    "location": {
                        "latitude": 37.77032,
                        "longitude": -122.41304,
                        "reverse_geocode_detailed": {"city": "San Francisco", "state": "CA"},
                    },
                    "details": [{"pdp_fields": [{"display_label": "3 Month Lease"}, {"display_label": "2 persons live here"}]}],
                    "description": {"text": "Clean, social home near Dolores Park."},
                    "isLive": True,
                },
                {
                    "listingTitle": "Daly City room",
                    "itemUrl": "https://www.facebook.com/marketplace/item/222/",
                    "listingPrice": {"amount": "1300.00", "currency": "USD"},
                    "location": {"latitude": 37.70364, "longitude": -122.46227, "reverse_geocode_detailed": {"city": "Daly City", "state": "CA"}},
                    "isLive": True,
                },
            ]

    class MixedClient(FakeClient):
        def post(self, url, **kwargs):
            self.call = (url, kwargs)
            return MixedResponse()

    tokens = ApifyTokenStore(tmp_path / "apify-token.txt")
    tokens.save(VALID_TOKEN)
    listings = ApifyFacebookMarketplaceSource(tokens).search(MixedClient(), preferences)

    assert len(listings) == 1
    assert listings[0].neighborhood == "Mission Dolores"
    assert "3 Month Lease" in listings[0].summary
    assert "2 persons live here" in listings[0].summary


def test_apify_facebook_rejects_a_daly_city_pin_on_the_sf_boundary() -> None:
    assert ApifyFacebookMarketplaceSource._is_san_francisco(
        "Daly City CA 94014 37.706222645442 -122.46835597785"
    ) is False


def test_apify_monthly_run_cap_is_persistent(tmp_path) -> None:
    tokens = ApifyTokenStore(tmp_path / "apify-token.txt")

    assert tokens.reserve_monthly_run(limit=2) is True
    assert tokens.reserve_monthly_run(limit=2) is True
    assert tokens.reserve_monthly_run(limit=2) is False
    assert ApifyTokenStore(tmp_path / "apify-token.txt").reserve_monthly_run(limit=2) is False
    assert os.stat(tokens.usage_path).st_mode & 0o777 == 0o600


def test_the_first_scan_fills_the_board_and_the_rest_stay_cheap(tmp_path, preferences) -> None:
    """An empty board is the worst moment to be frugal -- the first read is what
    makes the source look like it works at all. Every read after it only has to
    catch what is new, and at roughly $0.013 of Apify credit a listing that
    difference is the whole month's budget."""
    tokens = ApifyTokenStore(tmp_path / "apify-token.txt")
    tokens.save(VALID_TOKEN)
    source = ApifyFacebookMarketplaceSource(tokens)

    assert tokens.first_scan_pending("marketplace") is True

    client = FakeClient()
    source.search(client, preferences)
    assert client.call[1]["json"]["resultsLimit"] == 100, "fill the board once"
    assert tokens.first_scan_pending("marketplace") is False, "and only once"

    again = FakeClient()
    source.search(again, preferences)
    assert again.call[1]["json"]["resultsLimit"] == 10, "then only catch what is new"


def test_a_failed_first_scan_keeps_its_deep_read(tmp_path, preferences) -> None:
    """Spending the one deep read on a run that returned nothing would leave a
    permanently empty board trickling ten a scan."""
    tokens = ApifyTokenStore(tmp_path / "apify-token.txt")
    tokens.save(VALID_TOKEN)
    source = ApifyFacebookMarketplaceSource(tokens)

    class EmptyResponse(FakeResponse):
        def json(self):
            return []

    class EmptyClient(FakeClient):
        def post(self, url, **kwargs):
            self.call = (url, kwargs)
            return EmptyResponse()

    with pytest.raises(SourceError):
        source.search(EmptyClient(), preferences)

    assert tokens.first_scan_pending("marketplace") is True, "the deep read is still owed"


def test_every_feed_carries_the_price_band_and_asks_for_the_newest(tmp_path, preferences) -> None:
    """The keyword feeds were added without a price filter, so a room at any
    price could spend one of the hundred slots the first scan is given. Each
    slot is about $0.013 of Apify credit; none of them should go to a listing
    nobody could take."""
    tokens = ApifyTokenStore(tmp_path / "apify-token.txt")
    tokens.save(VALID_TOKEN)
    source = ApifyFacebookMarketplaceSource(tokens)

    client = FakeClient()
    source.search(client, preferences)
    starts = [entry["url"] for entry in client.call[1]["json"]["startUrls"]]

    assert len(starts) > 1, "the keyword feeds are the ones that had no filter"
    for url in starts:
        assert "minPrice=750" in url and "maxPrice=9375" in url, url
        assert "sortBy=creation_time_descend" in url, "newest first, on every feed"
