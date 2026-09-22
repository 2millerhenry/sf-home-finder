from __future__ import annotations

from sf_housing.apify import ApifyTokenStore
from sf_housing.preferences import Preferences
from sf_housing.sources import FacebookGroupsSource


VALID_TOKEN = "apify_api_abcdefghijklmnopqrstuvwxyz123456"
GROUP_URL = "https://www.facebook.com/groups/1105487206638421/"


class GroupResponse:
    status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return [
            {
                "url": "https://www.facebook.com/groups/1105487206638421/permalink/123456789/",
                "time": "2026-07-20T10:00:00.000Z",
                "text": (
                    "Sunny private room available in Mission for $1,600. "
                    "Month-to-month in a clean shared Victorian."
                ),
                "groupTitle": "SF/Bay Area Housing",
                "inputUrl": GROUP_URL,
            },
            {
                "url": "https://www.facebook.com/groups/1105487206638421/permalink/987654321/",
                "time": "2026-07-20T10:02:00.000Z",
                "text": "Looking for a room in San Francisco to move in next month, budget $1,500.",
                "groupTitle": "SF/Bay Area Housing",
                "inputUrl": GROUP_URL,
            },
        ]


class GroupClient:
    def __init__(self):
        self.call = None

    def post(self, url, **kwargs):
        self.call = (url, kwargs)
        return GroupResponse()


def group_preferences(preferences: Preferences) -> Preferences:
    return Preferences(
        {
            **preferences.data,
            "sources": {
                **preferences.section("sources"),
                "facebook_group_urls": [GROUP_URL],
            },
        }
    )


def test_public_facebook_group_posts_become_deduplicable_housing_candidates(tmp_path, preferences) -> None:
    tokens = ApifyTokenStore(tmp_path / "apify-token.txt")
    tokens.save(VALID_TOKEN)
    source = FacebookGroupsSource(tokens)
    client = GroupClient()

    listings = source.search(client, group_preferences(preferences))

    assert len(listings) == 1
    listing = listings[0]
    assert listing.platform == "Facebook Groups"
    assert listing.original_url.endswith("/permalink/123456789/")
    assert listing.price == 1600
    assert listing.neighborhood == "Mission"
    assert listing.metadata["listing_timestamp"] == "2026-07-20T10:00:00.000Z"
    assert listing.metadata["facebook_group_title"] == "SF/Bay Area Housing"
    url, request = client.call
    assert VALID_TOKEN not in url
    assert request["headers"] == {"Authorization": f"Bearer {VALID_TOKEN}"}
    assert request["json"] == {
        "startUrls": [{"url": GROUP_URL}],
        "resultsLimit": 10,
        "viewOption": "CHRONOLOGICAL",
        # A month: a room posted three weeks ago is still worth seeing, and a
        # fortnight was discarding half the board.
        "onlyPostsNewerThan": "30 days",
    }


def test_manual_group_scan_reads_deeper_than_scheduled_scan(tmp_path, preferences) -> None:
    tokens = ApifyTokenStore(tmp_path / "apify-token.txt")
    tokens.save(VALID_TOKEN)
    source = FacebookGroupsSource(tokens)
    client = GroupClient()

    source.search_for_trigger(client, group_preferences(preferences), "manual")

    assert client.call[1]["json"]["resultsLimit"] == 30


def test_group_parser_keeps_sf_offers_and_rejects_seekers_and_outside_cities(
    tmp_path, preferences
) -> None:
    tokens = ApifyTokenStore(tmp_path / "apify-token.txt")
    rows = [
        {
            "url": f"{GROUP_URL}permalink/pacific-heights/",
            "text": "Private room available in NOPA for $2,000 per month.",
            "inputUrl": GROUP_URL,
        },
        {
            "url": f"{GROUP_URL}permalink/takeover/",
            "text": "Looking for someone to take over my room in Mission Dolores for $1,800.",
            "inputUrl": GROUP_URL,
        },
        {
            "url": f"{GROUP_URL}permalink/seeker/",
            "text": (
                "I'm looking for a private room in Nob Hill. If anyone knows of a place "
                "available or coming available, please message me."
            ),
            "inputUrl": GROUP_URL,
        },
        {
            "url": f"{GROUP_URL}permalink/seeker-with-room/",
            "text": (
                "I'm looking to move back to SF and primarily looking for a private room. "
                "If you have a room available, please send me a message."
            ),
            "inputUrl": GROUP_URL,
        },
        {
            "url": f"{GROUP_URL}permalink/sunnyvale/",
            "text": "Two rooms available in Central Sunnyvale for $1,900 each.",
            "inputUrl": GROUP_URL,
        },
        {
            "url": f"{GROUP_URL}permalink/san-jose/",
            "text": "Room for rent in North San Jose for $1,500.",
            "inputUrl": GROUP_URL,
        },
    ]

    listings = FacebookGroupsSource(tokens).parse_rows(rows, preferences)

    assert [listing.original_url for listing in listings] == [
        f"{GROUP_URL}permalink/pacific-heights/",
        f"{GROUP_URL}permalink/takeover/",
    ]
    assert [listing.neighborhood for listing in listings] == ["NOPA", "Mission"]


def test_facebook_group_post_budget_stops_before_the_free_tier_limit(tmp_path) -> None:
    tokens = ApifyTokenStore(tmp_path / "apify-token.txt")

    assert tokens.reserve_monthly_group_posts(5, limit=10) is True
    assert tokens.reserve_monthly_group_posts(5, limit=10) is True
    assert tokens.reserve_monthly_group_posts(5, limit=10) is False
