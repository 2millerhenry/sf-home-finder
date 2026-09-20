"""A filter offers what the list in front of the reader holds.

Every tab shared one set of choices, built from the whole board at once, so
Near matches offered the shortlist's neighbourhoods and the shortlist offered
the archive's. Picking one that belonged to another tab returned an empty
page, and on the owner's board the Neighborhood list ran to hundreds of
entries on a tab showing nine areas.
"""

from __future__ import annotations

from sf_housing.database import Repository
from sf_housing.models import ListingCandidate, ScoreResult

CUT_OFF = 60


def home(repository: Repository, source_id: str, area: str, score: int) -> None:
    repository.upsert_listing(
        ListingCandidate(
            platform="Craigslist",
            source_id=source_id,
            title=f"A room in {area}",
            original_url=f"https://example.test/{source_id}",
            price=1500,
            neighborhood=area,
        ),
        ScoreResult(score, ["Stored"], "", {}, eligibility="eligible"),
        "2026-09-20T12:00:00+00:00",
    )


def areas(repository: Repository, view: str) -> list[str]:
    neighborhoods, _ = repository.filter_options(CUT_OFF, view=view)
    return neighborhoods


def test_the_shortlist_offers_its_own_areas_and_not_the_near_matches(
    repository: Repository,
) -> None:
    """Castro is on the shortlist and Bernal is not, so Castro is the choice."""
    home(repository, "on-the-shortlist", "Castro", CUT_OFF + 32)
    home(repository, "a-near-match", "Bernal Heights", CUT_OFF - 5)

    assert areas(repository, "active") == ["Castro"]


def test_near_matches_offers_its_own_areas_and_not_the_shortlist_s(
    repository: Repository,
) -> None:
    """And the other way round, which is the half the owner was looking at."""
    home(repository, "on-the-shortlist", "Castro", CUT_OFF + 32)
    home(repository, "a-near-match", "Bernal Heights", CUT_OFF - 5)

    assert areas(repository, "near_matches") == ["Bernal Heights"]


def test_a_tab_holding_nothing_offers_nothing_to_filter_by(
    repository: Repository,
) -> None:
    """An empty tab used to offer the whole board's areas, every one of which
    returned an empty page from it."""
    home(repository, "on-the-shortlist", "Castro", CUT_OFF + 32)

    assert areas(repository, "saved") == []


def test_every_area_a_tab_offers_returns_at_least_one_home_on_it(
    repository: Repository,
) -> None:
    """The guard behind the two cases above, and the promise the list makes:
    no choice on it is a dead end."""
    for index, (area, score) in enumerate(
        (("Castro", 92), ("Mission District", 88), ("Bernal Heights", 55), ("Marina", 52))
    ):
        home(repository, str(index), area, score)

    for view in ("active", "near_matches"):
        for area in areas(repository, view):
            rows = repository.query_listings(
                minimum_score=CUT_OFF, view=view, housing_kind="room", neighborhood=area
            )
            assert rows, f"{view} offers {area!r}, which returns nothing"
