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
from sf_housing.preferences import Preferences


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


def add(repository: Repository, title: str, *, score: int, eligibility: str,
        reasons: list[str], maximum: int | None = None, over_by: int | None = None) -> None:
    """One more home on the board, judged the way the scorer would have judged it."""
    import json

    details = {"price": {"maximum": maximum, "over_by": over_by}} if maximum else {}
    with repository.connection() as connection:
        connection.execute(
            "INSERT INTO listings (platform, source_id, title, original_url, canonical_url, price,"
            " housing_kind, concern, score, eligibility, eligibility_reasons_json, score_details_json,"
            " status, first_found, last_seen)"
            " VALUES ('Craigslist', ?, ?, ?, ?, 2000, 'room', '', ?, ?, ?, ?, 'active',"
            " datetime('now'), datetime('now'))",
            (title, title, f"https://example.test/{title}", f"https://example.test/{title}",
             score, eligibility, json.dumps(reasons), json.dumps(details)),
        )
        connection.commit()


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


# --- Missing by a little, where the score cannot say so ---------------------


def test_scoring_records_how_far_over_the_budget_a_home_is() -> None:
    """A home over the budget is ruled out, and ruled-out homes are capped at
    around 49 whatever else is right about them -- so on a real board a $5,430
    home and a $12,400 home against a $3,000 deal both scored 49. The score
    cannot tell "just over" from "four times over", and near matches is
    exactly the question of which one this is. So the gap itself is written
    down when the home is judged."""
    from sf_housing.preferences import parse_preferences
    from sf_housing.scoring import score_listing
    from sf_housing.classification import classify_listing
    from sf_housing.models import ListingCandidate

    preferences = parse_preferences(
        "profile_active: true\nminimum_score: 65\n"
        "housing_paths: [one_bedroom]\none_bedroom:\n  max_monthly: 3000\n"
    )
    home = classify_listing(
        ListingCandidate(
            platform="Craigslist",
            source_id="over",
            title="One bedroom in the Mission",
            original_url="https://example.test/over",
            price=3120,
            neighborhood="Mission District",
            listing_type="Apartment",
            summary="A one bedroom apartment available now.",
        )
    )

    result = score_listing(home, preferences)
    price = result.details.get("price") or {}

    assert price.get("maximum"), "the ceiling it was judged against is not written down"
    assert price["over_by"] == home.price - price["maximum"], (
        "how far over the line it fell is not written down"
    )


def test_a_home_a_little_over_the_budget_is_a_near_match(tmp_path: Path) -> None:
    """The case this exists for: everything about it fits except the rent, and
    the rent misses by a tenth. Its score says 49, the same as a home at four
    times the budget, so only the gap can tell them apart."""
    repository = board(tmp_path)
    add(repository, "a little over", score=49, eligibility="ineligible",
        reasons=["The monthly price exceeds this path's maximum."], maximum=3000, over_by=120)

    assert "a little over" in near_matches(repository)


def test_a_home_far_over_the_budget_is_not_a_near_match(tmp_path: Path) -> None:
    """A $12,400 home against a $3,000 deal missed by nothing that could be
    called nearly."""
    repository = board(tmp_path)
    add(repository, "far over", score=49, eligibility="ineligible",
        reasons=["The monthly price exceeds this path's maximum."], maximum=3000, over_by=9400)

    assert "far over" not in near_matches(repository)


def test_a_home_that_also_missed_something_else_is_not_a_near_match(tmp_path: Path) -> None:
    """Nearly means one thing away. Over the budget *and* in a neighbourhood
    the deal rules out is two."""
    repository = board(tmp_path)
    add(repository, "over and elsewhere", score=49, eligibility="ineligible",
        reasons=["The monthly price exceeds this path's maximum.", "Bayview is outside your target neighborhoods."],
        maximum=3000, over_by=120)

    assert "over and elsewhere" not in near_matches(repository)


def test_scoring_records_the_gap_for_a_room_too(preferences: Preferences) -> None:
    """Rooms are judged by a different path, where being over the rent line is
    not a rule that rules a home out but a cap that holds the score at 54. So
    a room fifty dollars over and one at twice the budget both score 54, and
    the cap sits below any band drawn around the cut-off -- the room case of
    exactly the problem the whole-home path had."""
    from sf_housing.classification import classify_listing
    from sf_housing.models import ListingCandidate
    from sf_housing.scoring import score_listing

    ceiling = int(preferences.section("budget").get("max_monthly"))
    room = classify_listing(
        ListingCandidate(
            platform="Craigslist",
            source_id="room-over",
            title="Private room in a shared flat near Golden Gate Park",
            original_url="https://example.test/room-over",
            price=ceiling + 100,
            neighborhood="NOPA",
            summary="A private room in a shared flat, available now. Six month lease.",
        )
    )

    price = (score_listing(room, preferences).details or {}).get("price") or {}

    assert price.get("maximum") == ceiling, "the room path never says what it judged the rent against"
    assert price.get("over_by") == 100, "the room path never says how far over the rent fell"


def test_a_room_a_little_over_the_budget_is_a_near_match(tmp_path: Path) -> None:
    """It is not ruled out -- it is capped at 54 -- so a band around the
    cut-off cannot reach it, and it used to disappear from every list."""
    repository = board(tmp_path)
    add(repository, "room a little over", score=54, eligibility="eligible",
        reasons=[], maximum=2000, over_by=100)

    assert "room a little over" in near_matches(repository)


def test_a_room_far_over_the_budget_is_not_a_near_match(tmp_path: Path) -> None:
    repository = board(tmp_path)
    add(repository, "room far over", score=54, eligibility="eligible",
        reasons=[], maximum=2000, over_by=1800)

    assert "room far over" not in near_matches(repository)
