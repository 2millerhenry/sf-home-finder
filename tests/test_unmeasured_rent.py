"""A home whose rent was never measured against the deal must not read as perfect.

A listing whose shape the classifier cannot call -- neither a room nor an
entire home -- is scored down the room path. That path reads the legacy
``budget`` section, and a deal that enables no private-room path has no room
budget, so the section is empty and the rent criterion is dropped as
unconfigured before it can judge anything.

On the live board that left exactly one criterion participating. A $7,500
two-bedroom condo was rendered "100 match", with "Mission District is one of
your two ideal areas" as the reason and the price printed beside it, for a deal
whose one-bedroom ceiling is $3,000. Seventy stored homes were in that state.

An unmeasured rent is an unknown, and this app caps homes it has not managed to
check rather than recommending them.
"""

from __future__ import annotations

import copy

import yaml

from sf_housing.classification import classify_listing
from sf_housing.models import ListingCandidate
from sf_housing.preferences import parse_preferences
from sf_housing.scoring import score_listing


WHOLE_HOME_ONLY = {
    "state": "active",
    "enabled_paths": ["one_bedroom"],
    "budgets": {"one_bedroom": {"maximum_monthly": 3000, "ideal_monthly": 1800}},
    "geography": {
        "anywhere_in_sf": False,
        "dream": ["Mission District"],
        "strong": [],
        "okay": [],
        "avoid": [],
    },
    "move_in": {"flexible": True},
    "lease": {"minimum_months": None, "maximum_months": None},
    "room_household": {"private_room_required": False, "maximum_people": None},
    "preferences": {},
}


def deal(**changes):
    profile = copy.deepcopy(WHOLE_HOME_ONLY)
    profile.update(changes)
    return parse_preferences(yaml.safe_dump({"profile_version": 1, "profile": profile}))


def unshaped_listing(price: int) -> ListingCandidate:
    """A real one, reduced: the classifier calls neither room nor whole home."""
    return ListingCandidate(
        platform="Craigslist",
        source_id="unshaped",
        title="Luxury Mission Condo - 2 bed + office w/ parking",
        original_url="https://example.test/unshaped",
        price=price,
        neighborhood="Mission District",
        listing_type="condo",
        summary="A luxury condo in the Mission with parking.",
    )


def test_an_unshaped_home_is_not_a_perfect_match_on_its_area_alone() -> None:
    """The regression: one criterion participated, so the home scored 100."""
    listing = classify_listing(unshaped_listing(7500))
    assert listing.housing_kind == "unknown", "the fixture must exercise the unshaped path"

    result = score_listing(listing, deal())

    assert result.score <= 59, (
        "a home more than twice over the deal's ceiling, whose rent was never "
        f"measured against it, scored {result.score}"
    )
    assert any(
        item.get("check") == "price" and item.get("status") == "unknown"
        for item in result.details["hard_constraints"]
    ), "the unmeasured rent has to be said out loud, not merely priced out of the top"


def test_the_cap_does_not_touch_a_home_whose_rent_was_measured() -> None:
    """A deal with a room budget does measure the rent, and nothing changes."""
    with_rooms = deal(
        enabled_paths=["one_bedroom", "private_room"],
        room_household={"private_room_required": True, "maximum_people": None},
        budgets={
            "one_bedroom": {"maximum_monthly": 3000, "ideal_monthly": 1800},
            "private_room": {"maximum_monthly": 2000, "ideal_monthly": 1500},
        },
    )
    listing = classify_listing(
        ListingCandidate(
            platform="Craigslist",
            source_id="room",
            title="Private room for rent in the Mission District",
            original_url="https://example.test/room",
            price=1500,
            neighborhood="Mission District",
            listing_type="apartment",
            summary="A private bedroom for rent with housemates, twelve month lease.",
        )
    )
    result = score_listing(listing, with_rooms)
    assert result.score > 59
    assert any("$1,500" in reason for reason in result.reasons), (
        "the rent has to have been measured for this to be the control case"
    )
    assert not any(
        item.get("check") == "price" and item.get("status") == "unknown"
        for item in result.details["hard_constraints"]
    )
