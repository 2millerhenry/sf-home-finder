"""What counts as a near match.

A near match is a home the deal would have taken if it had scored a little
higher. It used to mean everything that was not on the shortlist, which on a
real board of 8,102 homes meant 7,785 "near matches": 7,625 of them homes the
deal had ruled out outright -- the wrong bedroom count, double the budget --
buried the 160 that actually came close.
"""

from pathlib import Path

import pytest

from sf_housing.database import NEAR_MATCH_MARGIN, Repository


CUT_OFF = 65


def board(tmp_path: Path) -> Repository:
    repository = Repository(tmp_path / "housing.sqlite3")
    repository.initialize()
    homes = [
        ("on the shortlist", CUT_OFF + 15, "eligible"),
        ("a point short", CUT_OFF - 1, "eligible"),
        ("still worth a look", CUT_OFF - NEAR_MATCH_MARGIN, "needs_verification"),
        ("well short", CUT_OFF - NEAR_MATCH_MARGIN - 1, "eligible"),
        ("ruled out by the deal", CUT_OFF + 20, "ineligible"),
    ]
    with repository.connection() as connection:
        for index, (title, score, eligibility) in enumerate(homes, start=1):
            connection.execute(
                "INSERT INTO listings (platform, source_id, title, original_url, canonical_url,"
                " price, housing_kind, concern, score, eligibility, status, first_found, last_seen)"
                " VALUES (?, ?, ?, ?, ?, ?, 'room', '', ?, ?, 'active', datetime('now'), datetime('now'))",
                (
                    "Craigslist",
                    f"n{index}",
                    title,
                    f"https://example.test/{index}",
                    f"https://example.test/{index}",
                    2000,
                    score,
                    eligibility,
                ),
            )
        connection.commit()
    return repository


def near_matches(repository: Repository) -> set[str]:
    return {
        row["title"]
        for row in repository.query_listings(minimum_score=CUT_OFF, view="near_matches")
    }


def test_a_home_the_deal_ruled_out_is_not_a_near_match(tmp_path: Path) -> None:
    """The whole complaint. A home that fails a hard constraint is not nearly
    right, however well it scores on everything else, and thousands of them
    make the list useless for the ones that are."""
    assert "ruled out by the deal" not in near_matches(board(tmp_path))


def test_a_home_that_just_missed_is_a_near_match(tmp_path: Path) -> None:
    found = near_matches(board(tmp_path))
    assert "a point short" in found
    # Still unverified is still worth a look: it is short of the cut-off, not
    # ruled out, which is exactly what this view is for.
    assert "still worth a look" in found


def test_a_home_nowhere_near_the_cut_off_is_not_a_near_match(tmp_path: Path) -> None:
    """Called near matches, so it has to mean near. The margin is the promise."""
    assert "well short" not in near_matches(board(tmp_path))


def test_the_shortlist_is_not_repeated_in_near_matches(tmp_path: Path) -> None:
    assert "on the shortlist" not in near_matches(board(tmp_path))
