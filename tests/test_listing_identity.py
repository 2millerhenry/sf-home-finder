"""Which cards are one real home: the rules, on cards shaped like the real ones.

Every card below is copied from a real board (ids, URLs and addresses as the
sites published them). The failure this guards against is asymmetric: merging
two different flats hides one and passes it on the user's behalf, so each
"these are two homes" case matters at least as much as each merge.
"""

from __future__ import annotations

import pytest

from sf_housing.listing_identity import (
    BUILDING_GRAIN,
    NO_GRAIN,
    UNIT_GRAIN,
    adoptable,
    choose_key,
    copy_rank,
    facts,
    grain,
    id_key,
    own_key,
    same_home,
    twin_ids,
)


def key(platform, source_id, url, address, unit_type="one_bedroom", price=3000, *, unit=None,
        title=None, housing_kind="whole_unit"):
    metadata = {"address": address} if address else {}
    if unit:
        metadata["unit"] = unit
    return own_key(platform, source_id, url, metadata, title or address, housing_kind, unit_type, price)


# --------------------------------------------------------------------------
# one flat, several sites
# --------------------------------------------------------------------------


def test_one_flat_on_five_sites_is_one_home_whatever_each_calls_its_unit() -> None:
    """295 Buchanan #105 was five rows on the shortlist, one per site."""
    keys = {
        key("Zumper", "29583305", "https://www.zumper.com/listings/29583305p/x", "295 Buchanan St #105"),
        key("Zillow", "2071571811", "https://www.zillow.com/homedetails/x/2071571811_zpid/", "295 Buchanan St APT 105"),
        key("Movoto", "a", "https://www.movoto.com/san-francisco-ca/295-buchanan/for-rent/", "295 Buchanan St", unit="APT 105"),
        key("ApartmentGuide", "LV1", "https://www.apartmentguide.com/rent/x-LV1/", "295 Buchanan St unit 105"),
        key("Rent.com", "lv2", "https://www.rent.com/r/x-lv2", "295 Buchanan Street, Unit 105"),
    }
    assert keys == {"unit:295|BUCHANAN ST|105"}


def test_a_flat_quoted_at_two_rents_is_still_one_flat() -> None:
    """Price is never part of a unit's identity: two rents for one door is
    the disagreement the page should show, not two homes."""
    a = key("Zillow", "1", "https://www.zillow.com/homedetails/x/1_zpid/", "858 Capp St #1794", "two_bedroom", 17790)
    b = key("Movoto", "b", "https://www.movoto.com/san-francisco-ca/858-capp/for-rent/", "858 Capp St #1794", "two_bedroom", 12820)
    assert a == b != ""


def test_leading_zeros_and_labels_do_not_split_a_unit() -> None:
    a = key("Zillow", "1", "https://www.zillow.com/homedetails/x/1_zpid/", "10 Main St #01")
    b = key("Zillow", "2", "https://www.zillow.com/homedetails/y/2_zpid/", "10 Main St Unit 001")
    assert a == b == "unit:10|MAIN ST|1"


# --------------------------------------------------------------------------
# two homes that look alike
# --------------------------------------------------------------------------


def test_two_flats_in_one_building_are_two_homes() -> None:
    """1405 Franklin #305 and #607: same street, same size, same rent."""
    a = key("Movoto", "a", "https://www.movoto.com/san-francisco-ca/1405-franklin-305/for-rent/", "1405 Franklin St #305", price=4495)
    b = key("Movoto", "b", "https://www.movoto.com/san-francisco-ca/1405-franklin-607/for-rent/", "1405 Franklin St #607", price=4495)
    assert a and b and a != b


def test_neighbouring_houses_are_two_homes() -> None:
    """606 and 608 Masonic: next door, both four bedrooms."""
    a = key("Zillow", "465332690", "https://www.zillow.com/homedetails/606-Masonic/465332690_zpid/", "606 Masonic Ave", "four_bedroom", 10500)
    b = key("Zillow", "241583743", "https://www.zillow.com/homedetails/608-Masonic/241583743_zpid/", "608 Masonic Ave", "four_bedroom", 11500)
    # Neither names a unit, so neither can claim to be one door: no key at all
    # rather than a key that could join a house to a flat above it.
    assert a == b == ""


def test_a_unit_followed_by_more_words_is_not_trusted() -> None:
    """1651 La Salle "unit 1649 Upstairs" (4 beds) and "unit 1649 New Model"
    (3 beds) are two homes that only share the first word of a unit."""
    upstairs = key("ApartmentGuide", "LV3262897237", "https://www.apartmentguide.com/rent/x-LV3262897237/",
                   "1651 La Salle Ave unit 1649 Upstairs", "four_bedroom", 7000)
    model = key("ApartmentGuide", "LV3262801689", "https://www.apartmentguide.com/rent/x-LV3262801689/",
                "1651 La Salle Ave unit 1649 New Model", "three_bedroom", 6000)
    assert upstairs == model == ""


@pytest.mark.parametrize(
    "address",
    [
        "1 Main St #1B-1Ba",       # a floorplan label, not a door
        "1390 Market St #STE 107",  # a leasing office's suite, shared by every home it lets
        "1 Main St #1567b8521",     # a site's hash
        "1 Main St #SMALL",         # a word
        "1 Main St #1234567",       # an id, not a door number
    ],
)
def test_a_unit_that_is_not_a_door_number_earns_no_key(address: str) -> None:
    assert key("Zillow", "9", "https://www.zillow.com/homedetails/x/9_zpid/", address) == ""


def test_zillow_s_invented_units_earn_no_key() -> None:
    assert key("Zillow", "9", "https://www.zillow.com/homedetails/x/9_zpid/", "1 Main St #737R") == ""
    assert key("Zillow", "9", "https://www.zillow.com/homedetails/x/9_zpid/", "1 Main St #3ID1621") == ""


def test_rooms_are_never_merged_by_address() -> None:
    """Three rooms in one flat share an address and are three homes."""
    assert key("Zillow", "1", "https://www.zillow.com/homedetails/x/1_zpid/", "10 Main St #2",
               housing_kind="room") == ""


@pytest.mark.parametrize(
    "lower, upper",
    [
        ("25-27 Dore St #25", "25-27 Dore St #27"),
        ("1417-1419 Masonic Ave #1417", "1417-1419 Masonic Ave #1419"),
        ("405-413 Laguna St Unit 407", "405-413 Laguna St Unit 411"),
    ],
)
def test_two_doors_of_a_building_numbered_as_a_range_are_two_homes(lower: str, upper: str) -> None:
    """A unit inside a ranged address is that door's own street number. Read as
    "the house repeating its number" it made every door of the building one
    home -- passing the lower flat passed the upper one."""
    a = key("Redfin", "1", "https://www.redfin.com/CA/x/unit-1/home/1", lower)
    b = key("Redfin", "2", "https://www.redfin.com/CA/x/unit-2/home/2", upper)
    assert a and b and a != b
    assert a.endswith("|=") and b.endswith("|=")


def test_a_ranged_door_is_keyed_by_its_own_number() -> None:
    assert key("Redfin", "1", "https://www.redfin.com/CA/x/unit-27/home/1", "25-27 Dore St #27") == "unit:27|DORE ST|="


def test_a_house_repeating_its_number_as_its_unit_matches_the_bare_house() -> None:
    """1128 Valencia St #1128 on Movoto and Zillow is the house itself."""
    a = key("Movoto", "2dax61hb6yab", "https://www.movoto.com/san-francisco-ca/1128-valencia/for-rent/",
            "1128 Valencia St #1128", "three_bedroom", 6095)
    b = key("Zillow", "2071571810", "https://www.zillow.com/apartments/x/?zid=2071571810",
            "1128 Valencia St #1128", "three_bedroom", 6095)
    assert a == b == "unit:1128|VALENCIA ST|="


# --------------------------------------------------------------------------
# building cards
# --------------------------------------------------------------------------


def test_one_building_offer_on_several_sites_is_one_home() -> None:
    """1114 Sutter "1 bed from $2,895" held 5 of 139 shortlist slots."""
    cards = [
        ("ApartmentGuide", "6589544", "https://www.apartmentguide.com/a/1114-Sutter-St-6589544/"),
        ("Rent.com", "lc6589544", "https://www.rent.com/apartment/1114-sutter-st-lc6589544"),
        ("Redfin", "570892", "https://www.redfin.com/CA/San-Francisco/1114-Sutter-St-94109/apartment/570892"),
        ("Movoto", "bqd3xnj87sab", "https://www.movoto.com/rental/1114-sutter-street/pid_bqd3xnj87sab/"),
    ]
    keys = {key(platform, sid, url, "1114 Sutter St", "one_bedroom", 2895) for platform, sid, url in cards}
    assert keys == {"offer:1114|SUTTER ST|one_bedroom|2895"}


def test_a_building_card_never_joins_a_unit_page() -> None:
    """Zillow's own page for 1114 Sutter APT 201 is one door; the building's
    "from $2,895" card says which offer, not which door."""
    unit = key("Zillow", "2099090461", "https://www.zillow.com/homedetails/x/2099090461_zpid/",
               "1114 Sutter St APT 201", "one_bedroom", 2895)
    offer = key("Redfin", "570892", "https://www.redfin.com/CA/x/apartment/570892", "1114 Sutter St", "one_bedroom", 2895)
    assert unit.startswith("unit:") and offer.startswith("offer:")


def test_a_building_s_offers_at_two_sizes_are_two_homes() -> None:
    """Vara: a studio from $3,852 and a one-bedroom from $5,160."""
    studio = key("Redfin", "184090318", "https://www.redfin.com/CA/x/home/184090318", "1600 15th St", "studio", 3852)
    one_bed = key("ApartmentGuide", "5891685", "https://www.apartmentguide.com/a/Vara-5891685/", "1600 15th St", "one_bedroom", 5160)
    assert studio != one_bed


def test_a_building_card_with_no_size_or_rent_earns_no_key() -> None:
    assert key("Rent.com", "lc6978389", "https://www.rent.com/apartment/x-lc6978389", "650 Alvarado St", None, None) == ""


# --------------------------------------------------------------------------
# one record syndicated by one backend
# --------------------------------------------------------------------------


def test_rentpath_twins_and_zillow_alerts_name_each_other() -> None:
    assert twin_ids("ApartmentGuide", "LV3299974248") == [("Rent.com", "lv3299974248")]
    assert twin_ids("Rent.com", "lv3299974248") == [("ApartmentGuide", "lv3299974248")]
    assert twin_ids("ApartmentGuide", "6586744") == [("Rent.com", "lc6586744")]
    assert twin_ids("Rent.com", "lc6586744") == [("ApartmentGuide", "6586744")]
    assert twin_ids("Zillow", "465332690_zpid") == [("Zillow", "465332690")]
    assert twin_ids("Zillow", "465332690") == [("Zillow", "465332690_zpid")]
    assert twin_ids("Zillow", "37.77--122.41") == []


def test_a_complex_twin_adopts_an_offer_only_at_its_own_size() -> None:
    """Mission Rock: ApartmentGuide 6586744 is a one-bedroom at $5,550; its
    Rent.com twin lc6586744 is a three-bedroom at $14,895. Adopting across
    sizes put a studio and a one-bedroom in one home at 855 Brannan."""
    offer = "offer:1023|3RD ST|one_bedroom|5550"
    assert adoptable(offer, "one_bedroom")
    assert not adoptable(offer, "three_bedroom")
    assert not adoptable(offer, None)
    assert adoptable("unit:108|LANGTON ST|B", None), "a unit's key names no size"
    assert choose_key("", [offer], "three_bedroom", "rentpath:lc6586744|three_bedroom") == (
        "rentpath:lc6586744|three_bedroom"
    )


def test_id_keys() -> None:
    assert id_key("Rent.com", "lv3299974248", None) == "rentpath:lv3299974248"
    assert id_key("ApartmentGuide", "LV3299974248", "one_bedroom") == "rentpath:lv3299974248"
    assert id_key("ApartmentGuide", "6586744", "one_bedroom") == "rentpath:lc6586744|one_bedroom"
    assert id_key("Rent.com", "lc6586744", None) == "", "a complex card of no size is no home"
    assert id_key("Zillow", "465332690_zpid", None) == id_key("Zillow", "465332690", None) == "zpid:465332690"
    assert id_key("Zillow", "465332690#12", None) == "", "a detached row's id belongs to another card"


# --------------------------------------------------------------------------
# the grain each site's pages have
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "platform, source_id, url, expected",
    [
        ("Movoto", "a", "https://www.movoto.com/san-francisco-ca/x/for-rent/", UNIT_GRAIN),
        ("Movoto", "a", "https://www.movoto.com/rental/x/pid_a/", BUILDING_GRAIN),
        ("Zillow", "123", "https://www.zillow.com/apartments/x/?zid=123", UNIT_GRAIN),
        ("Zillow", "37.7--122.4", "https://www.zillow.com/apartments/x/?zid=37.7--122.4", BUILDING_GRAIN),
        ("Redfin", "1", "https://www.redfin.com/CA/x/unit-1649/home/1", UNIT_GRAIN),
        ("Redfin", "1", "https://www.redfin.com/CA/x/apartment/1", BUILDING_GRAIN),
        ("Zumper", "1", "https://www.zumper.com/listings/1p/x", UNIT_GRAIN),
        ("Zumper", "1", "https://www.zumper.com/apartment-buildings/p1/x", BUILDING_GRAIN),
        ("Craigslist", "x", "https://www.craigslist.org/view/d/x", NO_GRAIN),
        ("SF Housing Portal", "a:studio", "https://housing.sfgov.org/listings/a?unit=studio", NO_GRAIN),
    ],
)
def test_grain(platform: str, source_id: str, url: str, expected: str) -> None:
    assert grain(platform, source_id, url) == expected


# --------------------------------------------------------------------------
# is a card still the home stored under its id
# --------------------------------------------------------------------------


def _facts(address: str | None, platform: str = "Zumper", url: str = "https://www.zumper.com/listings/1p/x"):
    return facts(platform, "1", url, {"address": address} if address else {}, address)


def test_for_a_row_nobody_owns_only_evidence_of_a_move_counts() -> None:
    stored = _facts("405 Laguna St #2")
    assert not same_home(stored, _facts("409 Laguna St #4C"), strict=False)
    assert not same_home(stored, _facts("405 Laguna St #3"), strict=False)
    assert same_home(stored, _facts(None), strict=False), "a thin card is no evidence"
    assert same_home(stored, _facts("405 Laguna Street, Unit 2"), strict=False)


def test_for_a_user_s_row_sameness_has_to_be_shown() -> None:
    stored = _facts("405 Laguna St #2")
    assert same_home(stored, _facts("405 Laguna Street Unit 2"), strict=True)
    # A card that drops the unit could be any flat in the building -- unless
    # it is the same page, when it is only a thinner card.
    assert not same_home(stored, _facts("405 Laguna St"), strict=True)
    assert same_home(stored, _facts("405 Laguna St"), strict=True, same_url=True)
    assert same_home(_facts("405 Laguna St"), _facts("405 Laguna St #2"), strict=True, same_url=True)
    # No address at all: only the same page counts.
    assert not same_home(stored, _facts(None), strict=True)
    assert same_home(stored, _facts(None), strict=True, same_url=True)
    assert not same_home(stored, _facts("409 Laguna St #2"), strict=True, same_url=True)


def test_copy_rank_prefers_what_tells_the_reader_more() -> None:
    bare = copy_rank("Zillow", "1", "https://www.zillow.com/homedetails/x/1_zpid/", {}, None, None, None)
    priced = copy_rank("Zillow", "1", "https://www.zillow.com/homedetails/x/1_zpid/", {}, 3000, None, None)
    dated = copy_rank("Zillow", "1", "https://www.zillow.com/homedetails/x/1_zpid/", {}, 3000, "2026-09-01", None)
    assert bare < priced < dated
    # With everything else equal, the fixed site order breaks the tie.
    assert copy_rank("Movoto", "a", "https://www.movoto.com/san-francisco-ca/x/", {}, 3000, None, None) > copy_rank(
        "Zillow", "1", "https://www.zillow.com/homedetails/x/1_zpid/", {}, 3000, None, None
    )


def _building(address: str, size: str | None):
    return facts("Rent.com", "lc6589544", "https://www.rent.com/apartment/1114-sutter-st-lc6589544",
                 {"address": address}, address, size)


def test_a_starred_building_card_is_the_same_offer_while_its_featured_unit_rotates() -> None:
    """Its featured unit changes every week; that is the card being current."""
    assert same_home(_building("405 Laguna St", "one_bedroom"), _building("405 Laguna St #5", "one_bedroom"),
                     strict=True)
    assert same_home(_building("405 Laguna St #5", "one_bedroom"), _building("405 Laguna St #7", "one_bedroom"),
                     strict=True)


def test_a_starred_complex_card_now_describing_another_floorplan_is_another_home() -> None:
    """A complex card shows whichever size suits the deal: once the starred
    one-bedroom is gone, the same id describes a two-bedroom."""
    assert not same_home(_building("1114 Sutter St", "one_bedroom"), _building("1114 Sutter St", "two_bedroom"),
                         strict=True, same_url=True)
    assert same_home(_building("1114 Sutter St", "one_bedroom"), _building("1114 Sutter St", None), strict=True)
