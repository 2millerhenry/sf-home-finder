"""The number under the cut-off slider against the rows the tabs actually hold.

``shortlist_counts`` says its docstring is "the same predicate the active view
uses". It was not. It counted every stored home whose score cleared the stop,
while the shortlist shows only homes that are still eligible and whose unit
type is one of the tabs the deal put on screen.

On the real install that is not a rounding difference. At the saved cut-off of
65 the slider read 316 while the two tabs held 284 between them, and at the
bottom of the slider it read 4,357 against 319 -- fourteen times the page.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sf_housing.database import Repository
from sf_housing.models import ListingCandidate
from sf_housing.scoring import ScoreResult


def store(
    repository: Repository,
    index: int,
    *,
    score: int,
    eligibility: str = "eligible",
    unit_type: str | None = "one_bedroom",
) -> None:
    listing = ListingCandidate(
        platform="Test",
        source_id=f"home-{index}",
        title=f"An entire one bedroom apartment, home {index}",
        original_url=f"https://example.test/home-{index}",
        price=2400,
        neighborhood="Mission District",
        listing_type="apartment",
        summary="A whole 1 bed apartment, entire place to yourself, nine month lease.",
        housing_kind="whole_unit",
        unit_type=unit_type,
    )
    repository.upsert_listing(
        listing,
        ScoreResult(score, ["Stored"], "", {}, eligibility=eligibility),
    )
    # upsert_listing re-classifies from the text, which is how a real home gets
    # its unit type. A home whose source never said its size is a real row --
    # thirty-two of them sat in the count on the live board -- so write that
    # state back rather than pretending every home is classifiable.
    with repository.connection() as connection:
        connection.execute(
            "UPDATE listings SET unit_type = ?, eligibility = ? WHERE source_id = ?",
            (unit_type, eligibility, f"home-{index}"),
        )
        connection.commit()


@pytest.fixture
def board(tmp_path: Path) -> Repository:
    repository = Repository(tmp_path / "housing.sqlite3")
    repository.initialize()
    # Three homes the tabs will show.
    for index in range(3):
        store(repository, index, score=80)
    # One ruled out by the deal. It keeps its stored score, as every ineligible
    # home does, and the active view never shows it.
    store(repository, 10, score=80, eligibility="ineligible")
    # One whose size the source never stated. There is no tab for "unknown
    # size", so no page can ever show it.
    store(repository, 11, score=80, unit_type=None)
    # One of a size this deal did not enable.
    store(repository, 12, score=80, unit_type="two_bedroom")
    return repository


def test_the_slider_counts_only_homes_a_tab_will_show(board: Repository) -> None:
    """The regression: ineligible homes, homes of no stated size, and homes of a
    size the deal never enabled were all counted under the slider and shown on
    no page."""
    counted = board.shortlist_counts(
        [60], kinds=["whole_unit"], unit_types=("one_bedroom", "three_bedroom")
    )[60]
    shown = len(
        board.query_listings(
            minimum_score=60,
            housing_kind="whole_unit",
            unit_types=("one_bedroom",),
            view="active",
        )
    ) + len(
        board.query_listings(
            minimum_score=60,
            housing_kind="whole_unit",
            unit_types=("three_bedroom",),
            view="active",
        )
    )
    assert shown == 3
    assert counted == shown


def test_a_deal_that_names_no_sizes_still_counts_by_kind(board: Repository) -> None:
    """Rooms have no per-size tabs, so a room deal passes no unit types and the
    count must not collapse to nothing."""
    counted = board.shortlist_counts([60], kinds=["whole_unit"])[60]
    # Still narrowed by eligibility, still not narrowed by size.
    assert counted == 5
