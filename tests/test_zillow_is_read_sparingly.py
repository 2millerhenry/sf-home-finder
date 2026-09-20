"""Zillow blocks by address, and the block covers its search.

Reading a home's own page is the only way to learn Zillow has taken it down
(see tests/test_zillow.py). But Zillow answers about thirty detail pages from
one address before its bot protection starts returning a captcha to
everything -- measured while building this: after thirty-five reads in a few
minutes the search page itself began answering 403. The search finds every
home this source contributes, so spending it to check a handful is a bad
trade however stale those few are.
"""

from __future__ import annotations

import inspect

import pytest

from sf_housing.database import Repository
from sf_housing.models import ListingCandidate, ScoreResult
from sf_housing.preferences import Preferences, parse_preferences
from sf_housing.scanner import RECHECK_HARD_CEILING, Scanner
from sf_housing.sources import CraigslistSource, SourceError, ZillowSource
from tests.conftest import TEST_PREFERENCES


def test_zillow_reads_far_fewer_pages_a_scan_than_any_other_source() -> None:
    """The number is the whole safeguard, so it is written down here too."""
    assert ZillowSource.recheck_budget == 5
    assert ZillowSource.recheck_budget < RECHECK_HARD_CEILING / 10
    assert getattr(CraigslistSource, "recheck_budget", RECHECK_HARD_CEILING) == RECHECK_HARD_CEILING


class Refusing:
    """A source that turns away every page it is asked for."""

    platform = "Refusing"
    mode = "automatic"
    manual_reason = None
    detail_budget = 0
    search_url = "https://example.test/refusing"
    recheck_budget = 5
    recheck_floor = 5

    def __init__(self) -> None:
        self.asked: list[str] = []

    def search(self, client, preferences):
        return []

    def enrich(self, client, listing):
        self.asked.append(listing.original_url)
        raise SourceError("Refusing turned away an unattended request (HTTP 403).")


def shortlisted(repository: Repository, count: int) -> None:
    for index in range(count):
        repository.upsert_listing(
            ListingCandidate(
                platform="Refusing",
                source_id=f"home-{index}",
                title="A room in NOPA",
                original_url=f"https://example.test/{index}",
                price=1500,
                neighborhood="NOPA",
                listing_type="Room/share",
                summary="Private room in a shared home, flexible lease.",
                metadata={"property_type": "house", "rooms_in_property": "4"},
            ),
            ScoreResult(92, ["Stored"], "", {}, eligibility="eligible"),
        )


def test_a_source_that_refuses_one_page_is_not_asked_for_the_next(
    repository: Repository,
) -> None:
    """The failure this exists to stop: reading on into a block.

    Once a site has refused, the page after it is refused too -- and on
    Zillow each one teaches the same bot protection that then turns the
    search away, which costs every home the source finds. Five homes are
    waiting and the budget allows five; exactly one page is asked for.
    """
    preferences: Preferences = parse_preferences(TEST_PREFERENCES)
    shortlisted(repository, 5)
    source = Refusing()
    scanner = Scanner(repository, lambda: preferences, [source], detail_delay_seconds=0)

    scanner._recheck_absent(
        object(),
        source,
        preferences,
        seen_source_ids=set(),
        deadline=scanner._clock() + 600,
    )

    assert source.asked == ["https://example.test/4"], "it read on past a refusal"


def test_a_refusal_reaches_the_caller_rather_than_being_swallowed() -> None:
    """``_confirm_from_page`` absorbs every other failure, because an
    unreachable page proves nothing and the home is simply tried again. A
    refusal is different in kind -- the site asking us to stop -- and the
    caller is the only place that can."""
    body = inspect.getsource(Scanner._confirm_from_page)
    assert body.index("except SourceError:") < body.index("except Exception as exc:")

    for pass_name in ("_recheck_absent", "_confirm_cheap_homes"):
        source = inspect.getsource(getattr(Scanner, pass_name))
        assert "except SourceError as refusal:" in source, f"{pass_name} reads on through a refusal"
