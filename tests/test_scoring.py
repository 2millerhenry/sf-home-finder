from __future__ import annotations

from dataclasses import replace

from sf_housing.classification import ROOM, UNKNOWN, classify_listing
from sf_housing.models import ListingCandidate
import pytest

from sf_housing.preferences import Preferences, load_preferences, parse_preferences
from sf_housing.scoring import score_listing
from tests.paths import BENCHMARK_PROFILE


def test_strong_known_match_scores_high_and_returns_three_reasons(preferences: Preferences) -> None:
    listing = ListingCandidate(
        platform="Test",
        source_id="perfect",
        title="Sunny private room in quiet Victorian house near Golden Gate Park",
        original_url="https://example.test/perfect",
        price=1550,
        neighborhood="NOPA",
        listing_type="Private room",
        summary="Flexible lease in a communal home with a garden and bright natural light.",
        metadata={"property_type": "house", "rooms_in_property": "4"},
    )

    result = score_listing(listing, preferences)

    assert result.score >= 85
    assert len(result.reasons) == 3
    assert result.concern == "No major concern found in the available details."
    assert result.details["home_facts"]["primary"] == "Private room · House"


def test_whole_unit_studio_is_scored_in_a_separate_search_and_labeled_clearly() -> None:
    profile = load_preferences(BENCHMARK_PROFILE)
    listing = ListingCandidate(
        platform="Craigslist",
        source_id="studio",
        title="Sunny studio apartment in Potrero Hill",
        original_url="https://example.test/studio",
        price=2450,
        neighborhood="Potrero Hill",
        summary="Quiet studio apartment with laundry and a patio.",
    )

    result = score_listing(listing, profile)

    assert result.score >= profile.minimum_score
    assert result.details["housing_kind"] == "whole_unit"
    assert result.details["home_facts"]["primary"] == "Studio"
    # A bathroom count joins these facts, and says n/a when nothing states one.
    assert "Building size not stated" in result.details["home_facts"]["secondary"]
    assert "Baths n/a" in result.details["home_facts"]["secondary"]
    assert "verify it has 50 units or fewer" in result.concern


def test_whole_unit_one_bedroom_accepts_small_known_building_and_rejects_large_one() -> None:
    profile = load_preferences(BENCHMARK_PROFILE)
    small = ListingCandidate(
        platform="Zillow",
        source_id="small-one-bed",
        title="1 bed 1 bath apartment in Noe Valley",
        original_url="https://example.test/small-one-bed",
        price=2595,
        neighborhood="Noe Valley",
        summary="One-bedroom apartment in a 24-unit building.",
    )
    large = ListingCandidate(
        platform="Zillow",
        source_id="large-one-bed",
        title="1 bedroom apartment in Noe Valley",
        original_url="https://example.test/large-one-bed",
        price=2500,
        neighborhood="Noe Valley",
        summary="One-bedroom apartment in a 120-unit building.",
    )

    small_result = score_listing(small, profile)
    large_result = score_listing(large, profile)

    assert small_result.score >= profile.minimum_score
    # Size and bathrooms are one line now, given equal weight, with the
    # building underneath: "1 br / 1 bath" over "24-unit building".
    assert small_result.details["home_facts"] == {
        "primary": "1 br / 1 bath",
        "secondary": ["24-unit building"],
    }
    assert large_result.score < profile.minimum_score
    assert "above your 50-unit maximum" in large_result.concern


def test_one_bedroom_for_rent_is_not_mistaken_for_a_roommate_listing() -> None:
    profile = load_preferences(BENCHMARK_PROFILE)
    entire = ListingCandidate(
        platform="HotPads",
        source_id="entire-one-bed",
        title="1 bedroom for rent in Bernal Heights",
        original_url="https://example.test/entire-one-bed",
        price=2400,
        neighborhood="Bernal Heights",
        listing_type="Apartment",
    )
    roommate = ListingCandidate(
        platform="HotPads",
        source_id="room-in-one-bed",
        title="Private room in a 1 bedroom apartment",
        original_url="https://example.test/room-in-one-bed",
        price=1400,
        neighborhood="Bernal Heights",
    )

    entire_result = score_listing(entire, profile)
    roommate_result = score_listing(roommate, profile)

    assert entire_result.details["housing_kind"] == "whole_unit"
    assert "housing_kind" not in roommate_result.details
    assert roommate_result.details["home_facts"]["primary"].startswith("Private room")


def test_shared_and_spanish_room_cards_never_enter_the_whole_unit_search() -> None:
    cases = (
        ListingCandidate(
            platform="Craigslist",
            source_id="shared-studio",
            title="Temporary shared Studio",
            original_url="https://example.test/shared-studio",
            price=1400,
            neighborhood="Inner Richmond",
            summary="Another bed can go in the room behind a curtain.",
            metadata={"attributes": "1BR / 1Ba | room not private | apartment"},
        ),
        ListingCandidate(
            platform="Facebook Marketplace",
            source_id="spanish-private-room",
            title="Habitación privada en alquiler",
            original_url="https://example.test/spanish-private-room",
            price=1700,
            neighborhood="Bernal Heights",
            summary="Baño compartido y cocina compartida con una familia.",
        ),
        ListingCandidate(
            platform="Facebook Marketplace",
            source_id="spanish-room-in-apartment",
            title="1 habitación 1 baño - Departamento",
            original_url="https://example.test/spanish-room-in-apartment",
            price=1500,
            neighborhood="Mission Dolores",
            summary="Se renta cuarto con baño y se comparte con el propietario.",
        ),
        ListingCandidate(
            platform="Listings Project",
            source_id="plural-rooms-category",
            title="Sunny apartment in Potrero Hill",
            original_url="https://example.test/plural-rooms-category",
            price=2200,
            neighborhood="Potrero Hill",
            listing_type="Rooms for Rent",
            summary="This is a two-bedroom apartment looking for one person to occupy the second bedroom.",
        ),
    )

    for listing in cases:
        classified = classify_listing(listing)
        assert classified.housing_kind == ROOM
        assert classified.unit_type is None


def test_office_using_the_word_studio_is_not_treated_as_a_home() -> None:
    listing = ListingCandidate(
        platform="Facebook Marketplace",
        source_id="office-studio",
        title="Affordable Office in Market Downtown",
        original_url="https://example.test/office-studio",
        price=1999,
        neighborhood="Castro",
        summary="Commercial space ideal for office, studio, or administrative use.",
    )

    classified = classify_listing(listing)

    assert classified.housing_kind == UNKNOWN
    assert classified.unit_type is None


def test_no_roommates_is_positive_whole_unit_evidence_not_a_room_signal() -> None:
    listing = ListingCandidate(
        platform="Craigslist",
        source_id="private-studio",
        title="Private studio apartment",
        original_url="https://example.test/private-studio",
        price=2200,
        neighborhood="Potrero Hill",
        summary="Private entrance and private bathroom. No roommates.",
    )

    classified = classify_listing(listing)

    assert classified.housing_kind == "whole_unit"
    assert classified.unit_type == "studio"


def test_two_bedroom_uses_a_two_person_split_and_hard_total_cap() -> None:
    profile = load_preferences(BENCHMARK_PROFILE)
    match = ListingCandidate(
        platform="Craigslist",
        source_id="two-bed-match",
        title="2 bedroom apartment near Alamo Square",
        original_url="https://example.test/two-bed-match",
        price=5200,
        neighborhood="NOPA",
        summary="Entire two-bedroom apartment in a 12-unit building. Ideal for roommates.",
    )
    over_budget = ListingCandidate(
        platform="Craigslist",
        source_id="two-bed-over",
        title="2BR apartment near Alamo Square",
        original_url="https://example.test/two-bed-over",
        price=5500,
        neighborhood="NOPA",
        summary="Entire 2BR apartment in a 12-unit building.",
    )

    match_result = score_listing(match, profile)
    over_result = score_listing(over_budget, profile)

    assert match_result.score >= profile.minimum_score
    assert match_result.details["search_mode"] == "split_unit"
    assert match_result.details["per_person_monthly"] == 2600
    # No bathroom count here, so there is nothing to weigh the size against
    # and the gap goes quietly underneath rather than into the top line.
    assert match_result.details["home_facts"] == {
        "primary": "2 br",
        "secondary": ["$2,600/person for 2", "Baths n/a", "12-unit building"],
    }
    assert over_result.score < profile.minimum_score
    assert "$2,700 per person" in over_result.concern


def test_three_bedroom_uses_three_people_and_its_own_exact_cap() -> None:
    profile = load_preferences(BENCHMARK_PROFILE)
    at_cap = ListingCandidate(
        platform="Craigslist",
        source_id="three-bed-at-cap",
        title="3 bedroom apartment in Potrero Hill",
        original_url="https://example.test/three-bed-at-cap",
        price=7500,
        neighborhood="Potrero Hill",
        summary="Entire three-bedroom apartment in a 10-unit building.",
    )
    over_cap = ListingCandidate(
        platform="Craigslist",
        source_id="three-bed-over-cap",
        title="3BR apartment in Potrero Hill",
        original_url="https://example.test/three-bed-over-cap",
        price=7501,
        neighborhood="Potrero Hill",
        summary="Entire 3BR apartment in a 10-unit building.",
    )

    match_result = score_listing(at_cap, profile)
    over_result = score_listing(over_cap, profile)

    assert match_result.score >= profile.minimum_score
    assert match_result.details["search_mode"] == "split_unit"
    assert match_result.details["per_person_monthly"] == 2500
    assert match_result.details["occupants"] == 3
    assert match_result.details["home_facts"] == {
        "primary": "3 br",
        "secondary": ["$2,500/person for 3", "Baths n/a", "10-unit building"],
    }
    assert over_result.score < profile.minimum_score
    assert "$2,500 per person" in over_result.concern


def test_three_bedroom_still_enforces_target_area_and_small_building_limit() -> None:
    profile = load_preferences(BENCHMARK_PROFILE)
    large_building = ListingCandidate(
        platform="Zillow",
        source_id="three-bed-large-building",
        title="3 bedroom apartment in Noe Valley",
        original_url="https://example.test/three-bed-large-building",
        price=7200,
        neighborhood="Noe Valley",
        summary="Entire 3-bedroom apartment in a 51-unit building.",
    )
    outside_area = ListingCandidate(
        platform="Zillow",
        source_id="three-bed-outside-area",
        title="3 bedroom apartment in SoMa",
        original_url="https://example.test/three-bed-outside-area",
        price=6900,
        neighborhood="SoMa",
        summary="Entire 3-bedroom apartment in a 6-unit building.",
    )

    large_result = score_listing(large_building, profile)
    outside_result = score_listing(outside_area, profile)

    assert large_result.score < profile.minimum_score
    assert "above your 50-unit maximum" in large_result.concern
    assert outside_result.score < profile.minimum_score
    assert "outside your target neighborhoods" in outside_result.concern


def test_room_ads_inside_three_bedroom_homes_never_enter_the_split_search() -> None:
    cases = (
        ListingCandidate(
            platform="Facebook Marketplace",
            source_id="room-in-three-bed",
            title="Private room in a 3 bedroom apartment",
            original_url="https://example.test/room-in-three-bed",
            price=1750,
            neighborhood="Mission Dolores",
            summary="Looking for one roommate to share our home.",
        ),
        ListingCandidate(
            platform="Facebook Groups",
            source_id="three-rooms-available",
            title="Three private bedrooms available in a shared house",
            original_url="https://example.test/three-rooms-available",
            price=1600,
            neighborhood="Potrero Hill",
        ),
        ListingCandidate(
            platform="Facebook Marketplace",
            source_id="three-bed-marketplace-room-only",
            title="3 Beds 1 Bath Room only",
            original_url="https://example.test/three-bed-marketplace-room-only",
            price=1300,
            neighborhood="Mission Dolores",
            summary="One available room in a 3 bed apartment. Unit includes Room Only.",
        ),
        ListingCandidate(
            platform="Facebook Marketplace",
            source_id="three-bed-marketplace-master-room",
            title="3 Beds 2.5 Baths Apartment",
            original_url="https://example.test/three-bed-marketplace-master-room",
            price=2200,
            neighborhood="Mission Dolores",
            summary="Private master bedroom available. I would be one of the roommates in this 3 bed apartment.",
        ),
        ListingCandidate(
            platform="Facebook Marketplace",
            source_id="three-bed-marketplace-two-rooms",
            title="3 Beds 2 Baths House",
            original_url="https://example.test/three-bed-marketplace-two-rooms",
            price=1700,
            neighborhood="Mission District",
            summary="Looking for two awesome roommates. Two large bright bedrooms available in our Victorian.",
        ),
    )

    for listing in cases:
        classified = classify_listing(listing)
        assert classified.housing_kind == ROOM
        assert classified.unit_type is None


def test_secondary_whole_home_area_stays_visible_but_never_outranks_priority_areas() -> None:
    profile = load_preferences(BENCHMARK_PROFILE)
    listing = ListingCandidate(
        platform="Craigslist",
        source_id="secondary-two-bed",
        title="2 bedroom apartment in Inner Sunset",
        original_url="https://example.test/secondary-two-bed",
        price=4000,
        neighborhood="Inner Sunset",
        summary="Entire 2-bedroom apartment in a 4-unit building.",
    )

    result = score_listing(listing, profile)

    assert result.score == 74
    assert result.details["neighborhood"]["priority"] == "secondary"


def test_private_room_inside_a_two_bedroom_never_enters_the_apartment_split() -> None:
    listing = ListingCandidate(
        platform="Facebook Marketplace",
        source_id="room-in-two-bed",
        title="Private room in a 2 bedroom apartment",
        original_url="https://example.test/room-in-two-bed",
        price=1800,
        neighborhood="Mission Dolores",
        summary="Looking for a roommate to share the apartment.",
    )

    classified = classify_listing(listing)

    assert classified.housing_kind == ROOM
    assert classified.unit_type is None


def test_two_separate_bedrooms_for_rent_are_not_an_entire_two_bedroom() -> None:
    listing = ListingCandidate(
        platform="Facebook Groups",
        source_id="two-rooms",
        title="Two private bedrooms available in a shared house",
        original_url="https://example.test/two-rooms",
        price=1600,
        neighborhood="Potrero Hill",
    )

    classified = classify_listing(listing)

    assert classified.housing_kind == ROOM
    assert classified.unit_type is None


def test_marketplace_two_bedroom_shell_does_not_hide_roommate_or_room_copy() -> None:
    roommate = ListingCandidate(
        platform="Facebook Marketplace",
        source_id="marketplace-roommate",
        title="2 Beds 1 Bath Apartment",
        original_url="https://example.test/marketplace-roommate",
        price=1400,
        neighborhood="Bernal Heights",
        summary="I have lived here for years and am looking for a chill roommate.",
    )
    advertised_room = ListingCandidate(
        platform="Facebook Marketplace",
        source_id="marketplace-large-room",
        title="2 Beds 1 Bath Apartment",
        original_url="https://example.test/marketplace-large-room",
        price=1800,
        neighborhood="NOPA",
        summary="$1800 for large room with bay windows in a 2 bedroom Victorian apartment.",
    )

    for listing in (roommate, advertised_room):
        classified = classify_listing(listing)
        assert classified.housing_kind == ROOM
        assert classified.unit_type is None


def test_warning_post_is_not_classified_as_a_two_bedroom_offer() -> None:
    listing = ListingCandidate(
        platform="Craigslist",
        source_id="scam-warning",
        title="Scammer listings in SF",
        original_url="https://example.test/scam-warning",
        price=4500,
        neighborhood="Cole Valley",
        listing_type="2BR / 1Ba condo",
        summary="Be careful: there are many fake ads for apartments.",
    )

    classified = classify_listing(listing)

    assert classified.housing_kind == UNKNOWN
    assert classified.unit_type is None


def test_written_building_count_is_extracted() -> None:
    listing = ListingCandidate(
        platform="Craigslist",
        source_id="five-unit",
        title="2BR apartment",
        original_url="https://example.test/five-unit",
        summary="A spacious apartment in a classic five-unit walk-up.",
    )

    classified = classify_listing(listing)

    assert classified.building_units == 5


def test_explicit_outside_sf_address_overrides_a_bad_target_area_label() -> None:
    profile = load_preferences(BENCHMARK_PROFILE)
    listing = ListingCandidate(
        platform="Craigslist",
        source_id="mill-valley-labelled-nopa",
        title="2 bedroom apartment",
        original_url="https://example.test/mill-valley",
        price=3645,
        neighborhood="NOPA",
        summary="396 Pine Hill Rd, Mill Valley, CA 94941.",
    )

    result = score_listing(listing, profile)

    assert result.score < profile.minimum_score
    assert "Mill Valley (outside SF)" in result.concern


def test_a_cheap_two_bedroom_is_a_find_rather_than_a_warning() -> None:
    """This used to assert the opposite: that $1,780 for a two-bedroom was
    "unusually low" and had to be held back for verification.

    It was, against half the deal's ceiling -- which is what the rule measured
    and what made it wrong. A rent-controlled two-bedroom at $1,780 is the best
    thing this app could find in San Francisco, and capping it at 79 against a
    cut-off of 80 pushed it out of the shortlist and into near matches. Only a
    figure too small to be a month's rent at all is worth a question now."""
    profile = load_preferences(BENCHMARK_PROFILE)
    listing = ListingCandidate(
        platform="Facebook Marketplace",
        source_id="low-two-bedroom",
        title="2 Beds 1 Bath Apartment",
        original_url="https://example.test/low-two-bedroom",
        price=1780,
        neighborhood="Potrero Hill",
        summary="Entire apartment with two bedrooms and one bathroom.",
    )

    result = score_listing(listing, profile)

    assert result.score > 79
    assert "unusually low" not in (result.concern or "")


def test_a_two_bedroom_priced_like_a_single_room_is_still_flagged() -> None:
    """The case the rule above exists for, kept."""
    profile = load_preferences(BENCHMARK_PROFILE)
    listing = ListingCandidate(
        platform="Facebook Marketplace",
        source_id="room-priced-two-bedroom",
        title="2 Beds 1 Bath Apartment",
        original_url="https://example.test/room-priced-two-bedroom",
        price=800,
        neighborhood="Potrero Hill",
        summary="Entire apartment with two bedrooms and one bathroom.",
    )

    result = score_listing(listing, profile)

    assert "unusually low" in (result.concern or "")
    assert "room price or deposit" in result.concern


def test_craigslist_whole_unit_requires_a_clean_detail_check_and_price_floor() -> None:
    profile = load_preferences(BENCHMARK_PROFILE)
    unverified = ListingCandidate(
        platform="Craigslist",
        source_id="unchecked",
        title="Studio apartment in Potrero Hill",
        original_url="https://www.craigslist.org/view/d/san-francisco-unchecked/unchecked",
        price=2200,
        neighborhood="Potrero Hill",
        summary="Studio apartment with a private entrance.",
    )
    under_floor = ListingCandidate(
        platform="Craigslist",
        source_id="under-floor",
        title="Studio apartment in Potrero Hill",
        original_url="https://www.craigslist.org/view/d/san-francisco-under-floor/under-floor",
        price=950,
        neighborhood="Potrero Hill",
        summary="Studio apartment with a private entrance.",
        metadata={"craigslist_detail_checked": True},
    )
    rejected = ListingCandidate(
        platform="Craigslist",
        source_id="rejected",
        title="Studio apartment in Potrero Hill",
        original_url="https://www.craigslist.org/view/d/san-francisco-rejected/rejected",
        price=2200,
        neighborhood="Potrero Hill",
        summary="Studio apartment with a private entrance.",
        metadata={
            "craigslist_detail_checked": True,
            "craigslist_content_rejected": True,
            "verification_concern": "Rejected: detail text conflicts with the advertised home.",
        },
    )

    unchecked_result = score_listing(unverified, profile)
    under_floor_result = score_listing(under_floor, profile)
    rejected_result = score_listing(rejected, profile)

    # An unchecked detail page and rejected content are still held back: those
    # say the listing itself may not be what it claims.
    assert unchecked_result.score < profile.minimum_score
    assert "detail-page check" in unchecked_result.concern
    assert rejected_result.score < profile.minimum_score
    assert rejected_result.concern.startswith("Rejected:")

    # A low rent is not. It is said out loud and left in the results, because
    # a cheap home is the thing being searched for and holding one back for
    # being cheap is the app deciding something the reader is better placed to
    # decide by opening the listing.
    assert "safety floor" in under_floor_result.concern
    assert under_floor_result.score >= profile.minimum_score


def test_a_sublet_is_judged_against_the_reader_s_own_lease_minimum() -> None:
    """Sublet inventory churns, so a default floor is right when nobody has said
    otherwise. Applying it over an explicit answer is not: this profile accepts
    three months, and three-month sublets were being refused by a six-month
    number the reader never chose."""
    import yaml

    document = yaml.safe_load(BENCHMARK_PROFILE.read_text(encoding="utf-8"))
    document.setdefault("lease", {})["min_months"] = 3
    profile = parse_preferences(yaml.safe_dump(document))
    assert profile.section("lease")["min_months"] == 3

    def sublet(source_id: str, summary: str, platform: str = "Facebook Marketplace") -> ListingCandidate:
        return ListingCandidate(
            platform=platform,
            source_id=source_id,
            title="Noe Valley 1BR sublet",
            original_url=f"https://www.facebook.com/marketplace/item/{source_id}/",
            price=2300,
            neighborhood="Noe Valley",
            summary=summary,
        )

    at_the_line = score_listing(
        sublet("three", "Entire one-bedroom apartment. Sublet available for 3 months."), profile
    )
    below_the_line = score_listing(
        sublet("two", "Entire one-bedroom apartment. Sublet available for 2 months."), profile
    )
    unclear = score_listing(
        sublet("unclear", "Entire one-bedroom apartment. Flexible sublet, dates to discuss."), profile
    )

    assert at_the_line.score >= profile.minimum_score, "three months is what this reader asked for"
    assert below_the_line.score < profile.minimum_score, "two months is not"
    assert "below the 3-month minimum" in below_the_line.concern
    # An unstated term is an unknown, named for checking, not a refusal.
    assert unclear.eligibility != "ineligible"
    assert any(
        entry.get("check") in {"lease", "sublet term"} and entry.get("status") == "unknown"
        for entry in unclear.details.get("hard_constraints", [])
    ), unclear.details.get("hard_constraints")


def test_the_six_month_floor_still_applies_when_no_lease_minimum_is_set() -> None:
    """The default has to survive: someone who never answered still should not
    be shown a two-week sublet as a match."""
    import yaml

    document = yaml.safe_load(BENCHMARK_PROFILE.read_text(encoding="utf-8"))
    document.get("lease", {}).pop("min_months", None)
    profile = parse_preferences(yaml.safe_dump(document))
    assert profile.section("lease").get("min_months") is None

    listing = ListingCandidate(
        platform="Facebook Marketplace",
        source_id="four-month",
        title="Noe Valley 1BR sublet",
        original_url="https://www.facebook.com/marketplace/item/four/",
        price=2300,
        neighborhood="Noe Valley",
        summary="Entire one-bedroom apartment. Sublet available for 4 months.",
    )

    result = score_listing(listing, profile)

    assert result.score < profile.minimum_score
    assert "below the 6-month minimum" in result.concern


def test_shared_bathroom_and_one_bedroom_available_copy_are_room_signals() -> None:
    cases = (
        ListingCandidate(
            platform="Facebook Marketplace",
            source_id="shared-bathroom",
            title="1 Bed 1 Bath Apartment",
            original_url="https://example.test/shared-bathroom",
            price=1400,
            neighborhood="Bernal Heights",
            summary="This bedroom is furnished. The bathroom will be shared with three roommates.",
        ),
        ListingCandidate(
            platform="Craigslist",
            source_id="one-bedroom-available",
            title="Beautiful 1 bedroom 1 bath",
            original_url="https://example.test/one-bedroom-available",
            price=1950,
            neighborhood="Marina",
            summary="One bedroom available in a historic four bedroom home.",
        ),
    )

    for listing in cases:
        classified = classify_listing(listing)
        assert classified.housing_kind == ROOM
        assert classified.unit_type is None


def test_fourplex_is_a_known_small_building() -> None:
    listing = ListingCandidate(
        platform="Facebook Marketplace",
        source_id="fourplex",
        title="1 Bed 1 Bath House",
        original_url="https://example.test/fourplex",
        summary="Junior one-bedroom garden unit in a fourplex.",
    )

    assert classify_listing(listing).building_units == 4


def test_detail_area_and_outside_city_url_override_bad_card_locations() -> None:
    profile = load_preferences(BENCHMARK_PROFILE)
    detail_area = ListingCandidate(
        platform="Craigslist",
        source_id="detail-area",
        title="Studio apartment",
        original_url="https://www.craigslist.org/view/d/san-francisco-studio/detail-area",
        price=2200,
        neighborhood="Potrero Hill",
        summary="Private studio apartment.",
        metadata={"detail_declared_neighborhood": "Excelsior"},
    )
    outside_url = ListingCandidate(
        platform="Craigslist",
        source_id="outside-url",
        title="1BR apartment",
        original_url="https://www.craigslist.org/view/d/oakland-open-house/outside-url",
        price=2195,
        neighborhood="Marina",
        summary="One-bedroom apartment with monthly rent.",
    )

    detail_result = score_listing(detail_area, profile)
    outside_result = score_listing(outside_url, profile)

    assert detail_result.score < profile.minimum_score
    assert "Excelsior" in detail_result.concern
    assert outside_result.score < profile.minimum_score
    assert "Oakland (outside SF)" in outside_result.concern


def test_sub_month_date_range_does_not_count_as_monthly_rent() -> None:
    profile = load_preferences(BENCHMARK_PROFILE)
    cases = (
        ListingCandidate(
            platform="Facebook Marketplace",
            source_id="short-stay-words",
            title="One bedroom sublet - Aug 6-17",
            original_url="https://example.test/short-stay-words",
            price=2000,
            neighborhood="Hayes Valley",
            summary="Only available from Aug 6 to Aug 17.",
        ),
        ListingCandidate(
            platform="Facebook Marketplace",
            source_id="short-stay-numeric",
            title="1 Bed 1 Bath Apartment",
            original_url="https://example.test/short-stay-numeric",
            price=2400,
            neighborhood="Lower Haight",
            summary="Available 8/7-30 for the listed dates.",
        ),
    )

    results = [score_listing(listing, profile) for listing in cases]

    assert all(result.score < profile.minimum_score for result in results)
    assert "12 days" in results[0].concern
    assert "24 days" in results[1].concern
    assert all("not a full monthly rent" in result.concern for result in results)


def test_whole_unit_preserves_an_external_verification_concern() -> None:
    profile = load_preferences(BENCHMARK_PROFILE)
    listing = ListingCandidate(
        platform="HotPads",
        source_id="verified-concern",
        title="281 14th St studio",
        original_url="https://example.test/verified-concern",
        price=2000,
        neighborhood="Mission District",
        summary="Studio apartment available now in a 22-unit building.",
        metadata={
            "bedrooms": 0,
            "building_units": 22,
            "verification_concern": "Verify an open fire-safety item before applying.",
        },
    )

    result = score_listing(listing, profile)

    assert result.score >= profile.minimum_score
    assert result.concern == "Verify an open fire-safety item before applying."


def test_verified_inactive_whole_unit_stays_out_of_the_shortlist() -> None:
    profile = load_preferences(BENCHMARK_PROFILE)
    listing = ListingCandidate(
        platform="SpareRoom",
        source_id="lease-pending",
        title="One bedroom condo",
        original_url="https://example.test/lease-pending",
        price=2400,
        neighborhood="Inner Parkside",
        summary="One bedroom condo in a four-unit building.",
        metadata={
            "bedrooms": 1,
            "building_units": 4,
            "verified_inactive": True,
            "verification_concern": "Verified inactive: lease pending and applications are closed.",
        },
    )

    result = score_listing(listing, profile)

    assert result.score < profile.minimum_score
    assert result.concern == "Verified inactive: lease pending and applications are closed."


def test_missing_information_is_neutral_not_a_definite_negative(preferences: Preferences) -> None:
    unknown = ListingCandidate(
        platform="Test",
        source_id="unknown",
        title="Room available",
        original_url="https://example.test/unknown",
        price=1500,
        neighborhood="NOPA",
    )
    mismatch = ListingCandidate(
        platform="Test",
        source_id="mismatch",
        title="Shared bedroom in apartment",
        original_url="https://example.test/mismatch",
        price=3000,
        neighborhood="SOMA",
        listing_type="shared bedroom",
        summary="A 24-month lease with 12 roommates. No yard and no natural light.",
        metadata={"property_type": "apartment", "rooms_in_property": "12"},
    )

    unknown_result = score_listing(unknown, preferences)
    mismatch_result = score_listing(mismatch, preferences)

    assert unknown_result.score > mismatch_result.score
    assert unknown_result.score >= 60
    assert unknown_result.concern.startswith("Unknown:")


def test_real_profile_prioritizes_ideal_price_area_and_flexible_lease() -> None:
    profile = load_preferences(BENCHMARK_PROFILE)
    ideal = ListingCandidate(
        platform="Test",
        source_id="ideal",
        title="Sunny private room in a Victorian house near Dolores Park",
        original_url="https://example.test/ideal",
        price=1500,
        neighborhood="Potrero Hill",
        listing_type="Private room",
        summary="Month-to-month sublease with two clean, chill, outdoorsy roommates, a garden and laundry.",
        metadata={"property_type": "house", "rooms_in_property": "3"},
    )
    acceptable = ListingCandidate(
        platform="Test",
        source_id="acceptable",
        title="Private room in a nice shared flat",
        original_url="https://example.test/acceptable",
        price=2200,
        neighborhood="NOPA",
        listing_type="Private room",
        summary="12 month lease with four quiet roommates.",
        metadata={"property_type": "apartment", "rooms_in_property": "5"},
    )

    ideal_result = score_listing(ideal, profile)
    acceptable_result = score_listing(acceptable, profile)

    assert ideal_result.score >= 90
    assert acceptable_result.score >= profile.minimum_score
    assert ideal_result.score > acceptable_result.score
    assert any("ideal price" in reason for reason in ideal_result.reasons)


def test_real_profile_accepts_the_current_private_room_budget() -> None:
    profile = load_preferences(BENCHMARK_PROFILE)
    listing = ListingCandidate(
        platform="Test",
        source_id="room-budget-cap",
        title="Sunny private room near Dolores Park",
        original_url="https://example.test/near-miss",
        price=2700,
        neighborhood="Mission Dolores",
        summary="Flexible sublease with two clean roommates and a garden.",
        metadata={"property_type": "house", "rooms_in_property": "3"},
    )

    result = score_listing(listing, profile)

    assert result.score >= profile.minimum_score
    assert result.details["price"]["value"] == 0.72
    assert "outside your" not in result.concern


def test_real_profile_keeps_private_rooms_above_the_cap_out_of_the_shortlist() -> None:
    profile = load_preferences(BENCHMARK_PROFILE)
    listing = ListingCandidate(
        platform="Test",
        source_id="room-over-budget",
        title="Sunny private room near Dolores Park",
        original_url="https://example.test/over-budget",
        price=2710,
        neighborhood="Mission Dolores",
        summary="Flexible sublease with two clean roommates and a garden.",
        metadata={"property_type": "house", "rooms_in_property": "3"},
    )

    result = score_listing(listing, profile)

    assert result.score < profile.minimum_score
    assert "slightly outside your $800–$2,700 budget" in result.concern


def test_real_profile_prioritizes_august_availability_and_keeps_late_openings_out_of_shortlist() -> None:
    profile = load_preferences(BENCHMARK_PROFILE)
    shared_fields = {
        "platform": "Furnished Finder",
        "title": "Sunny private room in a Victorian house near Dolores Park",
        "price": 1500,
        "neighborhood": "Mission Dolores",
        "listing_type": "Private room · House",
        "metadata": {"property_type": "house", "rooms_in_property": "3"},
    }
    august = ListingCandidate(
        source_id="august-ready",
        original_url="https://example.test/august-ready",
        summary="Available: Aug. 15, 2026. Flexible month-to-month sublease with garden and laundry.",
        **shared_fields,
    )
    next_year = ListingCandidate(
        source_id="next-year",
        original_url="https://example.test/next-year",
        summary="Available: Jan. 19, 2027. Flexible month-to-month sublease with garden and laundry.",
        **shared_fields,
    )

    august_result = score_listing(august, profile)
    next_year_result = score_listing(next_year, profile)

    assert august_result.score > next_year_result.score
    assert august_result.details["availability"]["value"] == 1.0
    assert next_year_result.score < profile.minimum_score
    assert next_year_result.details["availability"]["value"] == 0.0
    assert "near-term cutoff" in next_year_result.concern


def test_missing_availability_stays_neutral() -> None:
    profile = load_preferences(BENCHMARK_PROFILE)
    listing = ListingCandidate(
        platform="Furnished Finder",
        source_id="date-unknown",
        title="Private room in a shared house",
        original_url="https://example.test/date-unknown",
        price=1500,
        neighborhood="Potrero Hill",
        summary="Flexible sublease with two clean roommates and a garden.",
        metadata={"property_type": "house", "rooms_in_property": "3"},
    )

    result = score_listing(listing, profile)

    assert result.details["availability"]["known"] is False
    assert result.details["availability"]["value"] == 0.5


def test_landmark_location_is_useful_display_context_but_not_an_out_of_area_penalty() -> None:
    profile = load_preferences(BENCHMARK_PROFILE)
    listing = ListingCandidate(
        platform="Furnished Finder",
        source_id="near-golden-gate-park",
        title="Sunny private room",
        original_url="https://example.test/near-golden-gate-park",
        price=1500,
        neighborhood="Near Golden Gate Park",
        summary="Available: Aug. 1, 2026. Flexible sublease with a garden.",
        metadata={"property_type": "house", "rooms_in_property": "3"},
    )

    result = score_listing(listing, profile)

    assert result.details["neighborhood"]["known"] is False
    assert result.details["neighborhood"]["value"] == 0.5


def test_facebook_like_mission_listing_scores_as_a_real_match() -> None:
    profile = load_preferences(BENCHMARK_PROFILE)
    listing = ListingCandidate(
        platform="Facebook Marketplace",
        source_id="mission-example",
        title="4 Beds 1.5 Baths - Apartment",
        original_url="https://www.facebook.com/marketplace/item/1412015050215985/",
        price=1360,
        neighborhood="San Francisco, CA",
        listing_type="Apartment",
        summary=(
            "Large sunny bay window room with rear garden view on a quiet street in the heart "
            "of the Mission District, six minutes from Dolores Park. Four roommates, sunny back "
            "porch, roof access, 1930s Edwardian, clean developer, hiking, skateboarding and cooking."
        ),
    )

    result = score_listing(listing, profile)

    assert result.score >= 70
    assert result.details["neighborhood"]["match_label"] == "Mission Dolores"


def test_real_profile_neighborhood_priority_beats_a_better_price_outside_target_areas() -> None:
    profile = load_preferences(BENCHMARK_PROFILE)
    ideal_area = ListingCandidate(
        platform="Test",
        source_id="potrero",
        title="Private room in a shared flat",
        original_url="https://example.test/potrero",
        price=1975,
        neighborhood="Potrero Hill",
        summary="Clean home with a flexible sublease.",
    )
    wrong_area = ListingCandidate(
        platform="Test",
        source_id="park-merced",
        title="Sunny private room in a house",
        original_url="https://example.test/park-merced",
        price=1500,
        neighborhood="Park Merced",
        summary="Flexible lease with garden and laundry.",
        metadata={"property_type": "house"},
    )

    ideal_result = score_listing(ideal_area, profile)
    wrong_area_result = score_listing(wrong_area, profile)

    assert ideal_result.score > wrong_area_result.score
    assert wrong_area_result.score < profile.minimum_score
    assert wrong_area_result.concern == "Park Merced is outside your target neighborhoods."


def test_real_profile_uses_neighborhood_aliases_without_penalizing_a_generic_city_label() -> None:
    profile = load_preferences(BENCHMARK_PROFILE)
    alias = ListingCandidate(
        platform="Test",
        source_id="nopa",
        title="Room in an Alamo Square house",
        original_url="https://example.test/nopa",
        price=1500,
        neighborhood="San Francisco",
    )
    generic = ListingCandidate(
        platform="Test",
        source_id="generic",
        title="Room in a house",
        original_url="https://example.test/generic",
        price=1500,
        neighborhood="San Francisco",
    )

    alias_result = score_listing(alias, profile)
    generic_result = score_listing(generic, profile)

    assert alias_result.details["neighborhood"]["value"] == 0.55
    assert generic_result.details["neighborhood"]["value"] == 0.5


def test_room_for_rent_is_a_private_room_signal_unless_explicitly_shared(preferences: Preferences) -> None:
    private = ListingCandidate(
        platform="Test",
        source_id="private",
        title="Room for rent in a shared house",
        original_url="https://example.test/private",
        price=1500,
        neighborhood="NOPA",
    )
    shared = ListingCandidate(
        platform="Test",
        source_id="shared",
        title="Shared room for rent in a house",
        original_url="https://example.test/shared",
        price=1500,
        neighborhood="NOPA",
    )

    private_result = score_listing(private, preferences)
    shared_result = score_listing(shared, preferences)

    assert private_result.details["private_room"]["value"] == 1.0
    assert shared_result.details["private_room"]["value"] == 0.0


def test_real_profile_rejects_explicit_shared_room_complex() -> None:
    profile = load_preferences(BENCHMARK_PROFILE)
    listing = ListingCandidate(
        platform="Test",
        source_id="bad",
        title="Shared room per bed in apartment complex",
        original_url="https://example.test/bad",
        price=2500,
        neighborhood="SOMA",
        listing_type="Shared room",
        summary="24 month lease, ten roommates, no yard and no natural light.",
        metadata={"property_type": "apartment", "rooms_in_property": "10"},
    )

    result = score_listing(listing, profile)

    assert result.score < profile.minimum_score
    assert result.concern.startswith("Possible dealbreaker")


def test_private_room_in_shared_apartment_counts_as_shared_flat() -> None:
    profile = load_preferences(BENCHMARK_PROFILE)
    listing = ListingCandidate(
        platform="Test",
        source_id="shared-flat-label",
        title="Sunny private room with two roommates",
        original_url="https://example.test/shared-flat-label",
        price=1500,
        neighborhood="Potrero Hill",
        listing_type="Private room",
        summary="A clean shared apartment with a garden and laundry.",
        metadata={"property_type": "apartment", "rooms_in_property": "3"},
    )

    result = score_listing(listing, profile)

    assert result.details["property_type"]["value"] == 1.0
    assert "outside your preferred home types" not in result.concern


def test_score_records_normalized_target_neighborhood_for_display() -> None:
    profile = load_preferences(BENCHMARK_PROFILE)
    listing = ListingCandidate(
        platform="Test",
        source_id="dolores-normalized",
        title="Quiet private room beside Dolores Park",
        original_url="https://example.test/dolores-normalized",
        price=1500,
        neighborhood="city of san francisco",
    )

    result = score_listing(listing, profile)

    assert result.details["neighborhood"]["match_label"] == "Mission Dolores"


def test_whole_unit_card_in_an_ideal_area_needs_room_evidence_for_shortlist(preferences) -> None:
    listing = ListingCandidate(
        platform="Facebook Marketplace",
        source_id="whole-unit-in-potrero",
        original_url="https://example.test/whole-unit-in-potrero",
        title="Entire apartment for rent",
        price=1700,
        neighborhood="Potrero Hill",
        summary="A bright apartment near the waterfront.",
    )

    result = score_listing(listing, preferences)

    assert result.score <= 59
    assert result.details["private_room"]["main_results_eligible"] is False


def test_scoring_flags_explicit_women_only_household_and_declared_cross_street(preferences) -> None:
    listing = ListingCandidate(
        platform="Facebook Marketplace",
        source_id="women-only-cross-street",
        original_url="https://example.test/women-only-cross-street",
        title="Sunny private room",
        price=1500,
        neighborhood="Potrero Hill",
        summary="Women only. Private room on the corner of 23rd St and Bryant St.",
    )

    result = score_listing(listing, preferences)

    assert result.concern == "Women-only or women-preferred household; confirm eligibility."
    assert result.details["household_restriction"] == result.concern
    assert result.details["location_hint"] == "23rd St & Bryant St"


def test_anywhere_in_sf_still_means_inside_san_francisco() -> None:
    """A bare out-of-city name carries none of the grammar the outside-SF check needs.

    Treating any non-empty location as proof of San Francisco let a Discovery Bay
    room rank above real Mission listings.
    """
    from sf_housing.deal_profile import deal_profile_from_form, legacy_view
    from sf_housing.preferences import Preferences

    class Form(dict):
        def getlist(self, key):
            value = self.get(key, [])
            return value if isinstance(value, list) else [value]

    profile = deal_profile_from_form(
        Form({
            "housing_paths": ["private_room"],
            "private_room_maximum": "2500",
            "anywhere_in_sf": "on",
            "move_in_flexible": "on",
        })
    )
    preferences = Preferences(data=legacy_view(profile), deal=profile)

    def room(area: str) -> ListingCandidate:
        return ListingCandidate(
            platform="Craigslist",
            source_id="1",
            title="Sunny private room",
            original_url="https://example.test/1",
            price=1500,
            neighborhood=area,
            listing_type="Room/share",
            summary="Private room in a shared home, flexible lease.",
        )

    for inside in ("Mission District", "potrero hill / dogpatch", "San Francisco"):
        result = score_listing(room(inside), preferences)
        assert result.eligibility == "eligible", inside
        assert result.details["neighborhood"]["known"] is True

    # South San Francisco is a different city and contains the very string a
    # naive check would accept.
    for outside in ("Discovery Bay", "oakland", "san jose", "berkeley", "south san francisco"):
        result = score_listing(room(outside), preferences)
        assert result.details["neighborhood"]["known"] is False, outside
        assert "not a recognized San Francisco area" in result.details["neighborhood"]["missing"]
        # It must not be able to outrank a confirmed San Francisco home.
        assert result.score < score_listing(room("Mission District"), preferences).score


def test_a_sublet_that_never_states_its_length_is_unknown_not_refused() -> None:
    """The rule the rest of the scorer keeps: a missing fact lowers confidence
    and gets named for checking. This one was refusing homes outright for a
    length nobody had stated either way."""
    from sf_housing.preferences import parse_preferences
    from tests.conftest import TEST_PREFERENCES

    preferences = parse_preferences(TEST_PREFERENCES)
    listing = ListingCandidate(
        platform="Craigslist",
        source_id="sub",
        title="Sublet in a sunny NOPA flat",
        original_url="https://sfbay.craigslist.org/roo/d/x/sub.html",
        price=1500,
        neighborhood="NOPA",
        listing_type="Room/share",
        summary="Sublease available in a shared home. Message for details.",
    )

    result = score_listing(classify_listing(listing), preferences)

    assert result.eligibility != "ineligible", "an unstated term is not a refusal"
    reasons = [
        entry["reason"]
        for entry in result.details.get("hard_constraints", [])
        if entry.get("check") == "lease"
    ]
    assert any(reason.startswith("Unknown:") for reason in reasons), reasons


def test_a_sublet_that_states_a_short_term_is_still_refused() -> None:
    """The guarantee that had to survive."""
    from sf_housing.preferences import parse_preferences
    from tests.conftest import TEST_PREFERENCES

    preferences = parse_preferences(TEST_PREFERENCES)
    listing = ListingCandidate(
        platform="Craigslist",
        source_id="short",
        title="2 month sublet in NOPA",
        original_url="https://sfbay.craigslist.org/roo/d/x/short.html",
        price=1500,
        neighborhood="NOPA",
        listing_type="Room/share",
        summary="Sublease for 2 months only, October through November.",
    )

    result = score_listing(classify_listing(listing), preferences)

    assert result.eligibility == "ineligible"


def test_an_area_matches_however_a_source_punctuates_it() -> None:
    """Craigslist writes "haight ashbury"; the product calls it
    "Haight-Ashbury". A literal match meant two of the app's own area names
    could never match a real listing, so choosing either returned nothing."""
    from sf_housing.scoring import _contains_location, _normal

    for written, canonical in [
        ("haight ashbury", "Haight-Ashbury"),
        ("haight-ashbury", "Haight-Ashbury"),
        ("st francis wood", "St. Francis Wood"),
        ("st. francis wood", "St. Francis Wood"),
        ("north beach / telegraph hill", "North Beach"),
    ]:
        assert _contains_location(_normal(written), canonical), f"{written} -> {canonical}"

    # The guard that had to survive: a longer area is not its shorter neighbour.
    assert not _contains_location(_normal("mission bay"), "Mission District")
    assert not _contains_location(_normal("outer mission"), "Mission District")


def test_every_area_the_form_offers_can_match_a_plainly_written_listing() -> None:
    """A name nobody can match is a name that quietly returns nothing."""
    import re

    from sf_housing.deal_profile import SF_NEIGHBORHOODS
    from sf_housing.scoring import _contains_location, _normal

    unmatchable = [
        area
        for area in SF_NEIGHBORHOODS
        if not _contains_location(_normal(re.sub(r"[^A-Za-z0-9 ]", " ", area)), area)
    ]

    assert unmatchable == [], f"these areas cannot match a plainly written listing: {unmatchable}"


# --------------------------------------------------------------------------
# "anywhere in San Francisco" means in San Francisco
# --------------------------------------------------------------------------


def anywhere_in_sf_profile() -> Preferences:
    """A real shape: anywhere in the city, no neighbourhoods listed."""
    import yaml

    return parse_preferences(
        yaml.safe_dump(
            {
                "profile_version": 1,
                "profile": {
                    "state": "active",
                    "enabled_paths": ["private_room"],
                    "budgets": {"private_room": {"maximum_monthly": 3000}},
                    "geography": {"anywhere_in_sf": True},
                    "room_household": {"private_room_required": True},
                },
                "technical": {"minimum_score": 60},
            }
        )
    )


@pytest.mark.parametrize(
    "url,city",
    [
        ("https://www.craigslist.org/view/d/palo-alto-seeking-a-roommate/a1", "Palo Alto"),
        ("https://www.craigslist.org/view/d/union-city-room-for-rent/b2", "Union City"),
        ("https://www.craigslist.org/view/d/daly-city-sunny-rooms/c3", "Daly City"),
        ("https://www.craigslist.org/view/d/south-san-francisco-private-bedroom/d4", "South San Francisco"),
    ],
)
def test_anywhere_in_sf_still_refuses_another_city(url: str, city: str) -> None:
    """Listing no neighbourhoods made the area criterion unconfigured, so it was
    dropped before it could fail anything and these sat on the shortlist at 81.
    Someone who said "anywhere in San Francisco" has stated a requirement, not
    waived one."""
    listing = ListingCandidate(
        platform="Craigslist",
        source_id=city.lower().replace(" ", "-"),
        title="Private room available",
        original_url=url,
        price=1500,
        listing_type="Room/share",
        summary="A private room in a shared home, available now.",
    )

    result = score_listing(classify_listing(listing), anywhere_in_sf_profile())

    assert result.eligibility == "ineligible", result.details.get("hard_constraints")
    failures = [
        entry["reason"]
        for entry in result.details.get("hard_constraints", [])
        if entry.get("status") == "fail"
    ]
    assert any(city in reason for reason in failures), failures
    assert any("outside San Francisco" in reason for reason in failures), failures


def test_anywhere_in_sf_still_accepts_a_san_francisco_home() -> None:
    """The guard must not refuse the homes it exists to find."""
    listing = ListingCandidate(
        platform="Craigslist",
        source_id="sf-room",
        title="Sunny private room in the Mission",
        original_url="https://sfbay.craigslist.org/sfc/roo/d/san-francisco-sunny-room/e5",
        price=1500,
        neighborhood="Mission District",
        listing_type="Room/share",
        summary="A private room in a shared flat in San Francisco, available now.",
        metadata={"craigslist_detail_checked": True},
    )

    result = score_listing(classify_listing(listing), anywhere_in_sf_profile())

    assert result.eligibility != "ineligible"
    assert result.score >= 60


# --------------------------------------------------------------------------
# every whole-home size a deal can enable
# --------------------------------------------------------------------------


def _deal(*paths: str, maximum: int = 8000) -> Preferences:
    """A deal enabling exactly these whole-home paths, anywhere in the city."""
    import yaml

    from sf_housing.deal_profile import DEFAULT_OCCUPANTS

    return parse_preferences(
        yaml.safe_dump(
            {
                "profile_version": 1,
                "profile": {
                    "state": "active",
                    "enabled_paths": list(paths),
                    "budgets": {
                        path: {
                            "maximum_monthly": maximum,
                            **({"occupants": DEFAULT_OCCUPANTS[path]} if path in DEFAULT_OCCUPANTS else {}),
                        }
                        for path in paths
                    },
                    "geography": {"anywhere_in_sf": True},
                },
            }
        )
    )


def _home(bedrooms: int, price: int = 6000) -> ListingCandidate:
    from sf_housing.classification import WHOLE_UNIT

    return ListingCandidate(
        platform="Test",
        source_id=f"home-{bedrooms}-{price}",
        title=f"A {bedrooms}-bedroom home",
        original_url=f"https://example.test/{bedrooms}/{price}",
        price=price,
        neighborhood="Mission District",
        listing_type="Condo",
        summary=f"Listed as a {bedrooms}-bedroom. Asking ${price:,} a month.",
        metadata={"address": "1 Valencia St", "bedrooms": bedrooms},
        housing_kind=WHOLE_UNIT,
    )


@pytest.mark.parametrize("bedrooms,path", [(2, "two_bedroom"), (3, "three_bedroom"), (4, "four_bedroom")])
def test_a_shared_home_of_any_enabled_size_is_not_refused_as_the_wrong_type(bedrooms, path) -> None:
    """Four-bedroom was missing from the set of shared paths and from the
    branch below it, so it fell through to the whole-unit settings -- whose
    allowed types are studio and one-bedroom -- and every four-bedroom home
    came back "The home type is outside this deal path", ineligible, however
    plainly the deal enabled it. Two- and three-bedroom worked; four did not,
    and enabling it did nothing at all."""
    result = score_listing(_home(bedrooms), _deal(path))

    assert result.eligibility != "ineligible", result.eligibility_reasons
    assert "outside this deal path" not in " ".join(result.eligibility_reasons)


@pytest.mark.parametrize("bedrooms,path", [(2, "two_bedroom"), (3, "three_bedroom"), (4, "four_bedroom")])
def test_a_shared_home_reads_its_own_paths_budget(bedrooms, path) -> None:
    """Its own section, not the whole-unit one. Read from the wrong section a
    four-bedroom was measured against a ceiling nobody set for it."""
    assert score_listing(_home(bedrooms, price=7000), _deal(path, maximum=8000)).eligibility != "ineligible"

    over = score_listing(_home(bedrooms, price=40000), _deal(path, maximum=8000))
    assert over.eligibility == "ineligible"
    assert any("maximum" in reason for reason in over.eligibility_reasons)


def test_a_size_the_deal_does_not_enable_is_still_refused() -> None:
    """The fix must not turn the check off: a three-bedroom is refused by a
    deal that only enabled four."""
    result = score_listing(_home(3), _deal("four_bedroom"))
    assert result.eligibility == "ineligible"
    assert any("not enabled" in reason for reason in result.eligibility_reasons)


def test_a_deal_of_only_large_homes_still_counts_as_wanting_entire_homes() -> None:
    """A whole home whose size could not be read is asked about, not refused.
    The set naming the whole-home paths omitted four-bedroom too, so a deal
    enabling only that was told entire homes were not part of it."""
    from sf_housing.classification import WHOLE_UNIT

    unsized = ListingCandidate(
        platform="Test",
        source_id="unsized",
        title="An apartment",
        original_url="https://example.test/unsized",
        price=6000,
        neighborhood="Mission District",
        summary="An apartment in the Mission.",
        metadata={"address": "1 Valencia St"},
        housing_kind=WHOLE_UNIT,
    )
    result = score_listing(unsized, _deal("four_bedroom"))
    assert "Entire homes are not enabled" not in " ".join(result.eligibility_reasons)


def test_every_shared_path_has_the_defaults_the_scorer_reads() -> None:
    """The branch is gone; the scorer looks these up by path name. A path in
    SPLIT_PATHS without an entry here would raise KeyError mid-scan."""
    from sf_housing.deal_profile import DEFAULT_OCCUPANTS, DEFAULT_PER_PERSON, SPLIT_PATHS
    from sf_housing.scoring import WHOLE_HOME_PATHS

    for path in SPLIT_PATHS:
        assert path in DEFAULT_OCCUPANTS, path
        assert path in DEFAULT_PER_PERSON, path
        assert path in WHOLE_HOME_PATHS, path


# --------------------------------------------------------------------------
# cheap is what is being searched for; only implausible is worth a question
# --------------------------------------------------------------------------


def _priced(bedrooms: int, price: int, **metadata) -> ListingCandidate:
    from sf_housing.classification import WHOLE_UNIT

    return ListingCandidate(
        platform="Rent.com",
        source_id=f"{bedrooms}-{price}",
        title=f"A {bedrooms}-bedroom",
        original_url=f"https://example.test/{bedrooms}/{price}",
        price=price,
        neighborhood="Mission District",
        listing_type="Condo",
        summary=f"Listed as a {bedrooms}-bedroom. Asking ${price:,} a month.",
        metadata={"address": "1 Valencia St", "bedrooms": bedrooms, **metadata},
        housing_kind=WHOLE_UNIT,
    )


def _flagged_low(result) -> bool:
    return any(
        "unusually low" in text
        for text in [*result.eligibility_reasons, result.concern or ""]
    )


@pytest.mark.parametrize(
    "bedrooms,price",
    [(0, 2300), (0, 3852), (0, 3702), (1, 4000), (4, 7000), (4, 9500)],
)
def test_an_ordinary_san_francisco_rent_is_not_treated_as_suspicious(bedrooms, price) -> None:
    """This was a fraction of the deal's own ceiling: half of it for a whole
    home, 45% for a shared one. Raising a budget to $8,000 therefore made
    every studio under $4,000 "unusually low", and a four-bedroom deal --
    whose ceiling is four shares added together -- called everything under
    $14,400 suspicious, which is every four-bedroom in the city.

    Each one was capped at 79 against a cut-off of 80, so an entire category
    of home missed the shortlist by a single point and landed in near matches
    with "confirm that this unusually low amount is the full monthly rent"."""
    result = score_listing(_priced(bedrooms, price), _deal("studio", "one_bedroom", "four_bedroom"))

    assert not _flagged_low(result), result.eligibility_reasons
    assert result.eligibility == "eligible"
    assert result.score > 79


@pytest.mark.parametrize("bedrooms,price", [(0, 600), (1, 700), (4, 1200)])
def test_a_figure_too_small_to_be_a_months_rent_is_still_questioned(bedrooms, price) -> None:
    """A weekly rate, a deposit, one person's share posted as the whole, a
    typo. The check is worth keeping; it was only measuring the wrong thing."""
    result = score_listing(_priced(bedrooms, price), _deal("studio", "one_bedroom", "four_bedroom"))

    assert _flagged_low(result)


def test_raising_a_budget_never_makes_a_listing_look_worse() -> None:
    """The property the old rule broke. A ceiling is what somebody is willing
    to pay; it says nothing about which rents are real."""
    listing = _priced(0, 2300)
    modest = score_listing(listing, _deal("studio", maximum=3000))
    generous = score_listing(listing, _deal("studio", maximum=20000))

    assert generous.score >= modest.score
    assert not _flagged_low(generous)


def test_a_home_let_below_market_on_purpose_is_never_called_implausible() -> None:
    """The city's own portal and two building sources publish rents a third of
    market. Those are the finds, not the mistakes."""
    result = score_listing(
        _priced(0, 700, below_market_rate=True), _deal("studio")
    )

    assert not _flagged_low(result)


def test_the_floor_rises_with_the_size_of_the_home() -> None:
    """$1,500 is a plausible studio and an implausible four-bedroom."""
    deal = _deal("studio", "four_bedroom")

    assert not _flagged_low(score_listing(_priced(0, 1500), deal))
    assert _flagged_low(score_listing(_priced(4, 1500), deal))


def test_a_home_with_no_stated_size_still_gets_a_floor() -> None:
    """An unstated bedroom count must not switch the plausibility floor off.
    Checked on the floor itself: a listing whose size is unknown has a more
    pressing thing to say in its one-line concern than its rent."""
    from sf_housing.scoring import _implausible_rent

    unsized = replace(_priced(0, 300), metadata={"address": "1 Valencia St"})

    assert _implausible_rent(unsized, None) is True
    assert _implausible_rent(replace(unsized, price=3000), None) is False


def test_a_flag_in_the_bedroom_field_is_not_a_bedroom_count() -> None:
    """`True` is an int in Python and `int(True)` is 1, so a boolean here
    would quietly pick the one-bedroom floor for a home of unknown size."""
    from sf_housing.scoring import _bedrooms_of

    listing = _priced(0, 3000)
    assert _bedrooms_of(replace(listing, metadata={"bedrooms": True})) is None
    assert _bedrooms_of(replace(listing, metadata={"bedrooms": 2})) == 2
    assert _bedrooms_of(replace(listing, metadata={})) is None


def test_nothing_holds_a_home_back_for_being_cheap() -> None:
    """The rule, stated once. A low rent may earn a note on the card; it may
    never cost a home its score or its place in the shortlist. Two separate
    caps used to do exactly that -- 79 for any low rent, 49 for a cheap
    Craigslist unit -- and against a cut-off of 80 both meant "not shown"."""
    deal = _deal("studio", "one_bedroom", "four_bedroom")
    full_price = score_listing(_priced(0, 3000), deal)

    for price in (2000, 1200, 900, 600, 200, 1):
        cheap = score_listing(_priced(0, price), deal)
        assert cheap.score >= full_price.score, f"${price} scored below ${3000}"
        assert cheap.eligibility == "eligible", f"${price} was held back: {cheap.eligibility_reasons}"


def test_a_rent_that_looks_too_good_still_says_so_on_the_card() -> None:
    """Removed from the score, kept as information. The reader opens the
    listing and decides in ten seconds; this cannot."""
    result = score_listing(_priced(0, 400), _deal("studio"))

    assert "unusually low" in (result.concern or "")
    assert result.eligibility == "eligible"


def test_a_cheap_home_is_never_asked_to_confirm_its_rent_before_counting() -> None:
    """The specific line from the dashboard: "confirm that this unusually low
    amount is the full monthly rent", which made the home a near match rather
    than a result."""
    for price in (400, 900, 1626, 2300, 3852):
        result = score_listing(_priced(0, price), _deal("studio"))
        assert not any(
            "full monthly rent" in reason for reason in result.eligibility_reasons
        ), f"${price} still has to confirm its rent"
