"""The Neighborhood dropdown offered places that are not in San Francisco.

Its choices are the distinct strings sources wrote into the area field, minus
the handful that name the whole city. Everything else became a choice, so a
reader picking a neighbourhood was offered "Miami, FL", "New York, NY",
"Santa Cruz, CA" and "1371 Camilla St. Manteca, Ca." beside Castro and Nob
Hill -- and, from the sources that write a mailing address where an area
belongs, "Sweeny St, San Francisco, CA" and "161 Albion St, # A, San
Francisco, CA".

On the author's board that was 169 of the 395 stored strings. The homes behind
them were never reachable anyway: scoring's outside-SF gate keeps them off the
shortlist and out of near matches. Only the dropdown was wrong.

The fix must stay narrow. Most strings that are not one of the product's own
area names are legitimate Craigslist compounds -- "downtown / civic / van
ness" alone carries 208 homes, "SOMA / south beach" 91 -- so a rule that kept
only canonical names would cost more than it fixed. These tests hold both
edges: the misfiled labels go, and the compounds stay.
"""

from __future__ import annotations

import pytest

from sf_housing.database import Repository
from sf_housing.location import misfiled_area_label
from sf_housing.models import ListingCandidate, ScoreResult


def result(score: int = 75) -> ScoreResult:
    return ScoreResult(score, ["Good price"], "Unknown: lease terms.", {"price": {"known": True}})


def stored(repository: Repository, areas: tuple[str, ...]) -> list[str]:
    """The dropdown's choices after a source wrote each of ``areas``."""
    for index, area in enumerate(areas):
        repository.upsert_listing(
            ListingCandidate(
                platform="Craigslist",
                source_id=str(index),
                title=f"Room in {area}",
                original_url=f"https://example.test/{index}",
                price=1500,
                neighborhood=area,
            ),
            result(),
            "2026-09-20T12:00:00+00:00",
        )
    neighborhoods, _ = repository.filter_options(minimum_score=0)
    return neighborhoods


def test_the_dropdown_leaves_out_places_that_are_not_in_san_francisco(
    repository: Repository,
) -> None:
    choices = stored(
        repository,
        ("Castro", "Miami, FL", "New York, NY", "Santa Cruz, CA", "Daly City, CA", "Aromas, CA"),
    )

    assert "Castro" in choices
    for elsewhere in ("Miami, FL", "New York, NY", "Santa Cruz, CA", "Daly City, CA", "Aromas, CA"):
        assert elsewhere not in choices, f"{elsewhere!r} is not a San Francisco neighborhood"


def test_the_dropdown_leaves_out_an_address_a_source_filed_as_an_area(
    repository: Repository,
) -> None:
    choices = stored(
        repository,
        (
            "Nob Hill",
            "2050 Divisadero St",
            "161 Albion St, # A, San Francisco, CA",
            "Sweeny St, San Francisco, CA",
            "14th Ave, San Francisco, CA",
        ),
    )

    assert "Nob Hill" in choices
    for address in (
        "2050 Divisadero St",
        "161 Albion St, # A, San Francisco, CA",
        "Sweeny St, San Francisco, CA",
        "14th Ave, San Francisco, CA",
    ):
        assert address not in choices, f"{address!r} is an address, not an area"


def test_the_compound_labels_craigslist_writes_stay_choosable(repository: Repository) -> None:
    """The 1,700 homes that sit behind a label the product has no name for."""
    compounds = (
        "downtown / civic / van ness",
        "SOMA / south beach",
        "ingleside / SFSU / CCSF",
        "sunset / parkside",
        "richmond / seacliff",
        "USF / panhandle",
        "north beach / telegraph hill",
        "inner sunset / UCSF",
        "laurel hts / presidio",
        "twin peaks / diamond hts",
        "excelsior / outer mission",
        "west portal / forest hill",
        "cole valley / ashbury hts",
        "alamo square / nopa",
    )

    choices = stored(repository, compounds)

    assert sorted(choices, key=str.casefold) == sorted(compounds, key=str.casefold)


@pytest.mark.parametrize(
    "label",
    [
        "Miami, FL",
        "New York, NY",
        "Howard Beach, NY",
        "Chicago, IL",
        "Boston, MA",
        "White Plains, NY ,",
        "Spokane, WA",
    ],
)
def test_a_label_that_spells_out_another_state_is_somewhere_else(label: str) -> None:
    assert misfiled_area_label(label)


@pytest.mark.parametrize(
    "label",
    ["Santa Cruz, CA", "Aromas, CA", "Watsonville, CA", "1371 Camilla St. Manteca, Ca."],
)
def test_a_california_label_that_never_says_san_francisco_is_somewhere_else(label: str) -> None:
    """"Santa Cruz, CA" and "Aromas, CA" name no city the Bay Area lists know.

    The two helpers this could have leant on both answer None for them:
    ``outside_sf_location_label`` matches a bare city name exactly, so the
    state suffix defeats it, and ``declared_outside_sf_area_hint`` only knows
    cities near enough to pad a San Francisco search.
    """
    assert misfiled_area_label(label)


@pytest.mark.parametrize(
    "label", ["Berkeley", "daly city", "Rohnert Park", "San Rafael CA", "Palo Alto, CA"]
)
def test_a_bay_area_city_is_somewhere_else_however_the_source_wrote_it(label: str) -> None:
    assert misfiled_area_label(label)


@pytest.mark.parametrize(
    "label",
    [
        "2050 Divisadero St",
        "161 Albion St, # A, San Francisco, CA",
        "990 Fulton St, Apt 205, SF, California",
        "876-78 Guerrero St, # 876-1, San Francisco",
        "Sweeny St, San Francisco, CA",
        "35th Ave, San Francisco, CA",
        "1755 Franklin St San Francisco, CA",
        "California St, San Francisco, CA",
        "Mission St, San Francisco, CA",
    ],
)
def test_an_address_is_an_address_with_or_without_its_house_number(label: str) -> None:
    """A street is not an area even when the number that proves it is missing.

    ``split_street_address`` needs a leading number, so it answers None for
    "Sweeny St, San Francisco, CA" and "35th Ave, San Francisco, CA" -- which
    are exactly the rows these sources write most. It also answers None for
    "1755 Franklin St San Francisco, CA", which has its number but no comma
    before the city.
    """
    assert misfiled_area_label(label)


@pytest.mark.parametrize(
    "label",
    [
        "Holly Park",
        "Holly Park, San Francisco, CA",
        # St Francis Wood opens with a street type. Only a type that ends the
        # label is one, or this neighbourhood reads as an address.
        "St. Francis Wood, San Francisco, CA",
        "Lake Merced",
        "West Portal",
        "Van Ness",
        "Midtown Terrace",
        "Clinton Park",
        "Mission",
        "Presidio",
        "Buena Vista",
    ],
)
def test_an_area_named_after_a_street_is_still_an_area(label: str) -> None:
    """Holly Park, Lake Merced, Van Ness and West Portal are all SF streets.

    The city's own street table answers for all four, so a check that asked it
    whether a label was a street would have taken four real areas out of the
    dropdown. What makes a label an address is written on it instead: a street
    type ending it, and the city written after that.
    """
    assert misfiled_area_label(label) is None


@pytest.mark.parametrize(
    "label",
    [
        "downtown / civic / van ness",
        "SOMA / south beach",
        "Castro",
        "Nob Hill",
        "Mid-Market",
        "treasure island",
        "Dolores Heights",
        "Golden Gate Heights",
        "Lone Mountain",
        "Mission Bay, San Francisco",
        # Ends in MA: the state check wants the comma an address puts before
        # its state, not any two letters that finish a name.
        "SoMa",
    ],
)
def test_a_san_francisco_area_stays_a_choice(label: str) -> None:
    assert misfiled_area_label(label) is None


@pytest.mark.parametrize(
    "label", ["Castro, CA", "Noe Valley, CA", "Mission District, San Francisco, CA", "SoMa, CA"]
)
def test_one_of_our_own_areas_survives_the_state_a_source_glued_to_it(label: str) -> None:
    """"Castro, CA" is the Castro, and the rule for "Santa Cruz, CA" ate it.

    Sources do write the state after an area name -- ``canonical_neighborhood``
    exists to strip it before looking the name up -- so reading every
    "<place>, CA" as a city outside San Francisco takes real areas with it.
    """
    assert misfiled_area_label(label) is None


@pytest.mark.parametrize("label", ["Van Ness Ave", "West Portal Ave", "Mission St"])
def test_a_street_name_on_its_own_is_left_alone(label: str) -> None:
    """A label is read as an address only when it was written as one.

    Van Ness and West Portal name areas as readily as they name the streets
    they run on, so a bare street name is not enough. What makes the rows
    these sources write an address is the city after the street: "35th Ave,
    San Francisco, CA".
    """
    assert misfiled_area_label(label) is None


@pytest.mark.parametrize("label", ["Manteca", "Santa Monica", "Bushwick", "Stockton"])
def test_an_out_of_town_name_with_nothing_to_give_it_away_stays(label: str) -> None:
    """The limit of the rule, written down so it is a decision and not a bug.

    Nothing in the product knows these are elsewhere, and the two that end in
    "ca" are the reason the California suffix has to be anchored: matched
    loosely it reads "Manteca" as Mante, California.
    """
    assert misfiled_area_label(label) is None
