"""Clicking a column heading sorts by it; clicking it again turns it over.

The headings were already links, but each asked for one fixed order however
often it was clicked, so there was no way to see the cheapest homes last or
the oldest posting first without going to the Order menu. Two states and no
third, because a reader should never have to remember where they are in a
cycle.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sf_housing.app import SORT_COLUMNS, create_app
from sf_housing.models import ListingCandidate, ScoreResult
from tests.test_dashboard import app_settings

# Heading, the sort a first click asks for, and the sort a second click does.
TOGGLES = [(column, plain, reverse) for column, (plain, _, reverse) in SORT_COLUMNS.items()]


def page(client: TestClient, sort: str) -> str:
    response = client.get(f"/?sort={sort}")
    assert response.status_code == 200, sort
    return response.text


def heading(page_text: str, column: str) -> tuple[str, str]:
    """The href a heading points at, and the aria-sort it declares."""
    cell = {"score": "cell-score", "price": "cell-price", "newest": "cell-found"}[column]
    match = re.search(rf'<th scope="col" class="{cell}"([^>]*)>(.*?)</th>', page_text, re.S)
    assert match, f"no {cell} heading on the page"
    aria = re.search(r'aria-sort="([a-z]+)"', match.group(1))
    href = re.search(r'href="([^"]+)"', match.group(2))
    assert href, f"the {cell} heading is not a link"
    return href.group(1), (aria.group(1) if aria else "")


@pytest.fixture
def client(tmp_path: Path):
    """A board with homes on it: an empty one draws no table to sort."""
    settings = app_settings(tmp_path)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    repository = application.state.repository
    for index, price in enumerate((1500, 1700, 1900)):
        repository.upsert_listing(
            ListingCandidate(
                platform="Craigslist",
                source_id=f"room-{index}",
                title=f"Sunny room {index} in NOPA",
                original_url=f"https://sfbay.craigslist.org/roo/d/x/{index}.html",
                price=price,
                neighborhood="NOPA",
                listing_type="Room/share",
                summary="A private room in a shared home, flexible lease.",
                metadata={"property_type": "house", "rooms_in_property": "4"},
            ),
            ScoreResult(90 - index, ["fits"], "", {}, eligibility="eligible"),
        )
    with TestClient(application) as opened:
        yield opened


@pytest.mark.parametrize("column,plain,reverse", TOGGLES, ids=[t[0] for t in TOGGLES])
def test_a_heading_already_sorting_offers_the_other_direction(
    client: TestClient, column: str, plain: str, reverse: str
) -> None:
    """The second click. Sorting by a column and clicking it again must turn
    it over rather than asking for the order it is already in."""
    href, _ = heading(page(client, plain), column)

    assert f"sort={reverse}" in href, f"{column} does not offer its reversal"


@pytest.mark.parametrize("column,plain,reverse", TOGGLES, ids=[t[0] for t in TOGGLES])
def test_a_reversed_heading_offers_its_way_back(
    client: TestClient, column: str, plain: str, reverse: str
) -> None:
    """And the third click returns, so the two states cycle between
    themselves and a reader can always get back."""
    href, _ = heading(page(client, reverse), column)

    assert f"sort={plain}" in href


@pytest.mark.parametrize("column,plain,reverse", TOGGLES, ids=[t[0] for t in TOGGLES])
def test_a_heading_says_which_way_its_column_is_sorted(
    client: TestClient, column: str, plain: str, reverse: str
) -> None:
    """Both directions are announced, and they are opposites.

    ``aria-sort`` is what a screen reader reads out and what the arrow is
    drawn from, so a column claiming the same direction in both states would
    be telling every reader the same wrong thing twice.
    """
    _, forward = heading(page(client, plain), column)
    _, back = heading(page(client, reverse), column)

    assert {forward, back} == {"ascending", "descending"}


@pytest.mark.parametrize("column,plain,reverse", TOGGLES, ids=[t[0] for t in TOGGLES])
def test_a_heading_that_is_not_sorting_claims_no_direction(
    client: TestClient, column: str, plain: str, reverse: str
) -> None:
    """Only the column in use is marked, or the table would claim to be
    sorted three ways at once."""
    others = [other for other, _, _ in TOGGLES if other != column]
    text = page(client, plain)

    for other in others:
        _, direction = heading(text, other)
        assert direction == "", f"{other} claims a direction while {column} is the sort"


def test_every_reversal_is_an_order_the_board_will_accept(client: TestClient) -> None:
    """A heading that offered a sort the board rejects would silently fall
    back to the recommended order, and the click would look like it did
    nothing."""
    from sf_housing.app import VALID_SORTS

    for column, plain, reverse in TOGGLES:
        assert plain in VALID_SORTS and reverse in VALID_SORTS, column
        assert page(client, reverse)
