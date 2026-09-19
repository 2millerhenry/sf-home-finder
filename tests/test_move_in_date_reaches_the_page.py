"""A published move-in date must reach the page even with no move-in preference.

Four sources publish an explicit "available from" date -- AvalonBay, Rent.com,
ApartmentGuide and AppFolio write it to ``metadata["available_on"]``, and 251
homes in the author's own database carry one. None of them ever reached the
dashboard's Move-in column, the "Soonest move-in" sort, or the CSV export,
which all read ``score_details["availability"]["available_on"]``.

The date was only copied there when ``"availability"`` was already a key in the
score details, and that key exists only when the availability *criterion* took
part in scoring -- which needs a move-in window in the deal. A reader who left
move-in flexible, which is the default the onboarding writes, disabled the
criterion and with it the only path the date had to the page. So the column
read "Not stated" on every row of a database that held 251 real dates.
"""

from __future__ import annotations

from sf_housing.models import ListingCandidate
from sf_housing.preferences import parse_preferences
from sf_housing.scoring import score_listing
from tests.conftest import TEST_PREFERENCES


def test_a_published_move_in_date_survives_a_flexible_deal() -> None:
    listing = ListingCandidate(
        platform="AvalonBay",
        source_id="avalon-1",
        title="Avalon Dogpatch - 800 Indiana St",
        original_url="https://www.avaloncommunities.com/example",
        price=2900,
        neighborhood="Dogpatch",
        summary="An entire one-bedroom with laundry in the building.",
        listing_type="Apartment",
        metadata={"available_on": "December 23, 2026"},
    )

    details = score_listing(listing, parse_preferences(TEST_PREFERENCES)).details

    assert "availability" in details, "the page has nowhere to read the date from"
    assert details["availability"].get("available_on") == "2026-12-23", details.get(
        "availability"
    )
