"""A confirmation date must be earned by actually reading the page.

Eight sources ship an ``enrich`` that is ``return listing``, and Zumper's
returns before any request whenever the rent is already known. The recheck pass
asked only whether a source had an ``enrich`` attribute, so those homes were
counted as rechecked and stamped ``last_verified_at`` without a single request.
On the author's own board a SpareRoom room last returned by a search on
5 September was printed as "Last confirmed 17 September"; a home taken down in
between would have read as current right up to the email.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from sf_housing.database import Repository
from sf_housing.models import ListingCandidate
from sf_housing.preferences import parse_preferences
from sf_housing.scanner import Scanner
from sf_housing.scoring import score_listing
from sf_housing.sources import SFHousingPortalSource, SpareRoomSource, ZumperSource
from tests.conftest import TEST_PREFERENCES


class ExplodingClient:
    """Any network use at all is a failure for this test."""

    def get(self, *args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError(f"a request was made: {args} {kwargs}")


def _stored(repository: Repository, preferences, platform: str, stamp: str) -> int:
    listing = ListingCandidate(
        platform=platform,
        source_id="audit-1",
        title="Large Noe Valley bedroom in a shared home",
        original_url=f"https://example.test/{platform.lower().replace(' ', '-')}/audit-1",
        price=1500,
        neighborhood="NOPA",
        summary="A private room in a shared home, laundry on site, flexible lease.",
        listing_type="Room/share",
        metadata={"last_verified_at": stamp},
    )
    result = score_listing(listing, preferences)
    assert result.score >= preferences.minimum_score, result.score
    listing_id, _ = repository.upsert_listing(listing, result)
    return listing_id


@pytest.mark.parametrize(
    "source",
    [SpareRoomSource(), SFHousingPortalSource(), ZumperSource()],
    ids=lambda source: source.platform,
)
def test_a_source_that_cannot_read_the_page_never_writes_a_confirmation(
    tmp_path, source
) -> None:
    repository = Repository(tmp_path / "db.sqlite3")
    repository.initialize()
    preferences = parse_preferences(TEST_PREFERENCES)
    stale = (datetime.now(UTC) - timedelta(hours=72)).isoformat(timespec="seconds")
    listing_id = _stored(repository, preferences, source.platform, stale)
    scanner = Scanner(repository, lambda: preferences, [source], detail_delay_seconds=0.0)

    checked = scanner._recheck_absent(
        ExplodingClient(),
        source,
        preferences,
        seen_source_ids=set(),  # the search has stopped returning it
        deadline=scanner._clock() + 600,
    )

    stored = repository.listing(listing_id)
    assert checked == 0, "nothing was confirmed"
    assert stored["last_verified_at"] == stale, "the confirmation date is untouched"
    assert any(
        check["check"] == "confirmation" for check in stored["checks"]
    ), "and the home still says nobody has confirmed it lately"


def test_a_source_that_does_read_the_page_still_confirms(tmp_path) -> None:
    """The fix must not quietly switch rechecking off altogether."""

    class Reader:
        """A source whose enrich reads the page and answers with what it found."""

        platform = "Craigslist"
        mode = "automatic"
        recheck_budget = 6

        def enrich(self, client, listing):
            return replace(listing, summary="Still up, with a full description.")

    repository = Repository(tmp_path / "db.sqlite3")
    repository.initialize()
    preferences = parse_preferences(TEST_PREFERENCES)
    stale = (datetime.now(UTC) - timedelta(hours=72)).isoformat(timespec="seconds")
    listing_id = _stored(repository, preferences, "Craigslist", stale)
    source = Reader()
    scanner = Scanner(repository, lambda: preferences, [source], detail_delay_seconds=0.0)

    checked = scanner._recheck_absent(
        object(),
        source,
        preferences,
        seen_source_ids=set(),
        deadline=scanner._clock() + 600,
    )

    stored = repository.listing(listing_id)
    assert checked == 1
    assert stored["last_verified_at"] != stale
    assert not any(check["check"] == "confirmation" for check in stored["checks"])
