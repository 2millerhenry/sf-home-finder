"""The city housing portal states an address but never a neighbourhood.

Area carries the most weight in the score, so a portal listing with no
neighbourhood could not be ranked against a renter's actual preferences: it
scored a neutral 0.5 and told them "neighbourhood is not stated". Nine out of
ten now resolve.

Two properties matter more than coverage, and most of these tests are about
them. The answer must never be a guess -- a block the city's own data splits
between two neighbourhoods resolves to nothing -- and it must be identical from
one scan to the next, so a home does not move between areas while someone is
deciding about it.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from sf_housing import location
from sf_housing.deal_profile import SF_NEIGHBORHOODS
from sf_housing.location import parse_street_address, sf_area_from_address


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
TABLE_PATH = REPO_ROOT / "sf_housing" / "data" / "sf_streets.json"


# --------------------------------------------------------------------------
# accuracy: addresses whose neighbourhood is independently known
# --------------------------------------------------------------------------

# Verified against San Francisco geography rather than against the table that
# produced them, so this fails if the build script ever mangles the data.
KNOWN = [
    ("3436 Pierce St", "Marina"),
    ("1303 Larkin St", "Nob Hill"),
    ("1400 Sacramento St", "Nob Hill"),
    ("1000 Chestnut St", "Russian Hill"),
    ("800 Grant Ave", "Chinatown"),
    ("1200 Grant Ave", "North Beach"),
    ("1 Market St", "Financial District"),
    ("600 Geary St", "Tenderloin"),
    ("500 Castro St", "Castro"),
    ("1500 Haight St", "Haight-Ashbury"),
    ("3200 22nd St", "Mission District"),
    ("3900 24th St", "Noe Valley"),
    ("300 Cortland Ave", "Bernal Heights"),
    ("1500 Palou Ave", "Bayview"),
    ("1000 Balboa St", "Inner Richmond"),
    ("3500 Balboa St", "Outer Richmond"),
    ("2800 Diamond St", "Glen Park"),
    ("1200 Fillmore St", "Western Addition"),
    ("400 Hayes St", "Hayes Valley"),
    ("1200 4th St", "Mission Bay"),
    ("100 9th St", "SoMa"),
    ("3200 Sacramento St", "Presidio Heights"),
    ("500 El Camino Del Mar", "Sea Cliff"),
    ("100 Persia Ave", "Excelsior"),
    ("1500 Silver Ave", "Portola"),
    ("100 Leland Ave", "Visitacion Valley"),
    ("2200 Pacific Ave", "Pacific Heights"),
    ("1500 Stockton St", "North Beach"),
    ("900 Folsom St", "SoMa"),
    ("1200 Valencia St", "Mission District"),
]


@pytest.mark.parametrize("address,expected", KNOWN)
def test_a_known_address_resolves_to_its_real_neighbourhood(address: str, expected: str) -> None:
    assert sf_area_from_address(address) == expected


def test_the_answer_is_the_city_s_own_boundary_not_local_usage() -> None:
    """A documented limit, pinned so a future change is a decision, not a slip.

    Both of these are unanimous in the city's own data but differ from what a
    renter would call the area. Assigning the city's answer is defensible and,
    more importantly, identical every time; guessing the vernacular one would be
    neither. A source that states its own neighbourhood still overrides this.
    """
    assert sf_area_from_address("100 Bosworth St") == "Outer Mission"  # locally Glen Park
    assert sf_area_from_address("500 Divisadero St") == "Hayes Valley"  # locally NoPa


# --------------------------------------------------------------------------
# the fine layer: the coarse one could not name a third of the city
# --------------------------------------------------------------------------

# The city's 41 "analysis neighborhoods" lump several of the product's areas
# under one name -- "Sunset/Parkside" is three of them, "West of Twin Peaks" is
# four -- so every block inside one of those had to be refused, and 525 streets
# refused at every block. Each of these is a real address whose area a renter
# would recognise, and every one of them used to answer "neighbourhood unknown".
FINER_THAN_THE_COARSE_LAYER = [
    ("3434 Santiago St", "Outer Sunset"),
    ("2167 40th Ave", "Outer Sunset"),
    ("2817 Pacheco St", "Outer Sunset"),
    ("1500 Noriega St", "Outer Sunset"),
    ("2691 37th Ave", "Parkside"),
    ("2727 34th Ave", "Parkside"),
    ("1200 Taraval St", "Parkside"),
    ("300 Foerster St", "Sunnyside"),
    ("100 Monterey Blvd", "Sunnyside"),
    ("100 Lakeview Ave", "Oceanview"),
    ("1500 Broad St", "Oceanview"),
    ("50 West Portal Ave", "West Portal"),
    ("300 Laguna Honda Blvd", "Forest Hill"),
    ("100 Santa Ana Ave", "St. Francis Wood"),
    ("1 Font Blvd", "Park Merced"),
    ("5000 Diamond Heights Blvd", "Diamond Heights"),
    ("1200 Vermont St", "Potrero Hill"),
    ("500 Church St", "Mission Dolores"),
    ("2235 Third St", "Dogpatch"),
]


@pytest.mark.parametrize("address,expected", FINER_THAN_THE_COARSE_LAYER)
def test_an_address_the_coarse_city_layer_could_not_place_now_names_its_area(
    address: str, expected: str
) -> None:
    """The owner's own report: homes with a full street address and no area.

    816 of his 9,615 active homes had no neighbourhood, and 563 of those carried
    a street number -- "3434 Santiago St", "2691 37th Ave" -- every one of them
    in the Sunset, which the coarse layer cannot tell apart from Parkside.
    """
    assert sf_area_from_address(address) == expected


def test_one_street_still_changes_area_as_it_crosses_the_sunset() -> None:
    """Splitting the Sunset is the point, and the block is still the unit.

    Filling the gap by calling the whole of "Sunset/Parkside" one area would
    satisfy most of the addresses above and be wrong about the rest. Taraval St
    runs from Parkside out to the ocean, so its blocks must not agree.
    """
    assert sf_area_from_address("1200 Taraval St") == "Parkside"
    assert sf_area_from_address("3544 Taraval St") == "Outer Sunset"
    assert sf_area_from_address("800 Irving St") == "Inner Sunset"


# --------------------------------------------------------------------------
# refusal: a wrong neighbourhood costs more than an unknown one
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "address",
    [
        "2000 Telegraph Ave, Oakland, CA",
        "1200 Shattuck Ave, Berkeley, CA 94709",
        "100 John Daly Blvd, Daly City, CA",
        "500 Grand Ave, South San Francisco, CA",
    ],
)
def test_an_address_outside_san_francisco_resolves_to_nothing(address: str) -> None:
    """Never the nearest SF area. "South San Francisco" contains the string a
    naive check would accept and is a different city."""
    assert sf_area_from_address(address) is None


def test_a_block_the_city_splits_between_two_areas_resolves_to_nothing() -> None:
    """Arguello is the Richmond/Presidio Heights line, and its 400 block is a
    genuine 41/55 split in the city's data. Powell's 400 block is 71% Nob Hill,
    which is a majority but not an answer."""
    assert sf_area_from_address("400 Arguello Blvd") is None
    assert sf_area_from_address("450 Powell St") is None
    assert sf_area_from_address("1699 Market St") is None


def test_an_area_the_product_has_no_name_for_resolves_to_nothing() -> None:
    """Coverage stops where the product's vocabulary stops, and abstains there.

    The finer city layer names 117 areas and the product knows 55 of them.
    Dorado Ter is in Mt. Davidson Manor, this block of Ulloa St is in Laguna
    Honda and this one of Ocean Ave is in Merced Manor -- three areas the
    product has no name for, inside "West of Twin Peaks", which covers four of
    the ones it does. Neither layer can answer in the product's words, so
    neither is allowed to pick a neighbour. A realtor would call Dorado Ter
    Ingleside; the city would not, and the guess is what this forbids.
    """
    assert sf_area_from_address("100 Dorado Ter") is None
    assert sf_area_from_address("700 Ulloa St") is None
    assert sf_area_from_address("2600 Ocean Ave") is None


@pytest.mark.parametrize(
    "address",
    ["", "   ", "PO Box 1234", "P.O. Box 99, San Francisco, CA", "San Francisco",
     "1600 Nonexistent Parkway", "Ask for details", "12345678901 Market St"],
)
def test_an_address_that_cannot_be_read_resolves_to_nothing(address: str) -> None:
    assert sf_area_from_address(address) is None


def test_a_missing_address_is_not_an_error() -> None:
    assert sf_area_from_address(None) is None


def test_a_street_number_the_city_has_no_record_of_answers_only_when_the_street_is_whole() -> None:
    """New construction, and addresses the city dataset has not caught up with,
    still resolve on a street that lies wholly in one neighbourhood. Southern
    Heights Ave is entirely in Potrero Hill; Market St crosses six areas, so an
    unrecorded number on it stays unknown rather than taking a neighbour."""
    assert sf_area_from_address("999 Southern Heights Ave") == "Potrero Hill"
    assert sf_area_from_address("9900 Market St") is None


# --------------------------------------------------------------------------
# parsing the shapes the portal really publishes
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "written,number,street",
    [
        ("1303 Larkin St", 1303, "LARKIN ST"),
        ("1303 Larkin Street", 1303, "LARKIN ST"),
        ("1201 Tennessee St.", 1201, "TENNESSEE ST"),  # trailing abbreviation point
        ("250-260 McAllister St", 250, "MCALLISTER ST"),  # a range
        ("3254-3264 23rd St", 3254, "23RD ST"),
        ("390 1st St", 390, "01ST ST"),  # the city zero-pads numbered streets
        ("1051 Third Street", 1051, "03RD ST"),  # and people spell them out
        ("3436 Pierce St #8", 3436, "PIERCE ST"),
        ("1275 Fell St Apt 4", 1275, "FELL ST"),
        ("1275 Fell St, Unit B", 1275, "FELL ST"),
        ("100 Church St Suite 300", 100, "CHURCH ST"),
        ("451 Kansas St at 17th St", 451, "KANSAS ST"),  # a cross street
        ("1 Polk St, San Francisco, CA 94102", 1, "POLK ST"),
        ("588 Mission Bay Blvd North", 588, "MISSION BAY BLVD"),  # a divided street
        ("1200 Market", 1200, "MARKET ST"),  # no street type given
        ("  1200   market   st  ", 1200, "MARKET ST"),
        ("1 Mrs. Jackson Way", 1, "MRS. JACKSON WAY"),  # a real point, kept
        ("700 Avenue E", 700, "AVENUE E"),  # really ends in a directional
    ],
)
def test_the_written_address_is_read_the_way_the_city_records_it(
    written: str, number: int, street: str
) -> None:
    assert parse_street_address(written) == (number, street)


def test_a_street_name_that_fits_two_streets_is_not_guessed() -> None:
    """Dropping the street type is only safe when one street can be meant.
    Geary Blvd is in the Richmond and Geary St is in the Tenderloin, four miles
    apart, so "1200 Geary" must not pick one."""
    table = json.loads(TABLE_PATH.read_text(encoding="utf-8"))["streets"]
    assert {"GEARY BLVD", "GEARY ST"} <= set(table)
    assert parse_street_address("1200 Geary") is None
    assert parse_street_address("1200 Scott") is None  # Scott Aly and Scott St
    # With the type, both answer.
    assert sf_area_from_address("1200 Geary Blvd") == "Western Addition"
    assert sf_area_from_address("400 Geary St") == "Tenderloin"


def test_a_street_named_after_an_area_is_not_read_as_that_area() -> None:
    """Mission St runs four miles from the Financial District to Bernal
    Heights, and most of it is not in the Mission. Reading the street name as a
    neighbourhood is the mistake the address number exists to prevent."""
    assert sf_area_from_address("1390 Mission St") == "SoMa"
    assert sf_area_from_address("3800 Mission St") == "Bernal Heights"
    assert sf_area_from_address("2200 Mission St") == "Mission District"
    assert sf_area_from_address("1500 Church St") == "Noe Valley"  # not Mission Dolores


# --------------------------------------------------------------------------
# determinism: the same home must not move between scans
# --------------------------------------------------------------------------


def test_the_same_address_resolves_identically_every_time() -> None:
    answers = {sf_area_from_address("1303 Larkin St") for _ in range(200)}
    assert answers == {"Nob Hill"}


def test_a_boundary_street_is_stably_refused_rather_than_alternating() -> None:
    assert {sf_area_from_address("400 Arguello Blvd") for _ in range(200)} == {None}


@pytest.mark.parametrize(
    "variant",
    ["1303 Larkin St", "1303 larkin st", "1303 LARKIN STREET", " 1303  Larkin  St ",
     "1303 Larkin St #12", "1303 Larkin St, San Francisco, CA 94109"],
)
def test_the_same_home_written_differently_resolves_the_same_way(variant: str) -> None:
    assert sf_area_from_address(variant) == "Nob Hill"


# --------------------------------------------------------------------------
# coverage against the addresses the portal actually publishes
# --------------------------------------------------------------------------

# Every distinct building address in one real fetch of the city portal, recorded
# so the bar is measured against real data without the suite needing a network.
REAL_PORTAL_ADDRESSES = [
    "1 Polk Street", "100 Van Ness Ave", "1010 16th St", "1023 3rd St", "1028 Market St",
    "1051 Third Street", "1101 Fairfax Ave", "1101 Howard St", "1201 Tennessee St.",
    "1222 Harrison St", "1303 Larkin St", "1390 Mission St", "1533 Sunnydale Ave",
    "1600 15th St", "1637 15th St", "1699 Market St", "1830 Alemany Blvd", "200 Buchanan St",
    "2051 3rd St", "2095 Bryant St", "2235 Third St", "230 Folsom St", "235 Valencia St",
    "239 Clayton St", "250-260 McAllister St", "255 Fremont St", "272 Folsom St",
    "280 Brighton Ave", "286 Valencia St", "29-35 Fair Ave", "3 Bayside Village Pl",
    "3254-3264 23rd St", "33 8th St", "333 Harrison St", "345 6th St", "3544 Taraval St",
    "360 Berry St", "361 Turk Street", "38 Harriet St", "3800 Mission St", "388 Beale St",
    "39 Bruton St", "390 1st St", "3945 Judah St", "40 Sycamore St", "555 Bryant St",
    "588 Mission Bay Blvd North", "660 King St", "680 Indiana St", "70 Ocean Ave",
    "750 Golden Gate Avenue", "750 Harrison St", "77 Bruton St", "785 Brannan St",
    "848 Fairfax Avenue", "855 Brannan St", "900 Folsom St", "949 Post St", "99 Ocean Ave",
]


def test_at_least_nine_in_ten_real_portal_addresses_resolve() -> None:
    """The bar was four in five while a third of the city's blocks had to be
    refused. The finer layer takes this fetch to 93%, and the bar moves with it
    so a table that quietly goes back to abstaining is caught here."""
    located = [a for a in REAL_PORTAL_ADDRESSES if sf_area_from_address(a)]
    ratio = len(located) / len(REAL_PORTAL_ADDRESSES)
    assert ratio >= 0.90, f"only {ratio:.0%} of real portal addresses resolved"


def test_every_real_portal_address_is_at_least_readable() -> None:
    """A refusal must come from an honest boundary, never from a parser that
    could not read the address."""
    unreadable = [a for a in REAL_PORTAL_ADDRESSES if parse_street_address(a) is None]
    assert unreadable == []


# --------------------------------------------------------------------------
# the table itself
# --------------------------------------------------------------------------


def test_every_neighbourhood_in_the_table_is_one_the_product_knows() -> None:
    table = json.loads(TABLE_PATH.read_text(encoding="utf-8"))
    unknown = [n for n in table["names"] if n not in SF_NEIGHBORHOODS]
    assert unknown == [], f"the table names areas the product cannot score: {unknown}"


def test_an_unknown_area_name_never_reaches_a_listing(monkeypatch, tmp_path) -> None:
    """If the product renames an area, the table must go quiet rather than
    assign a name nothing can score."""
    broken = tmp_path / "sf_streets.json"
    broken.write_text(
        json.dumps({"names": ["Atlantis"], "streets": {"LARKIN ST": {"13": 0}}}),
        encoding="utf-8",
    )
    _reload_table(monkeypatch, broken)
    assert sf_area_from_address("1303 Larkin St") is None


@pytest.mark.parametrize("content", ["", "not json", '{"names": []}', '{"streets": 4}'])
def test_a_missing_or_broken_table_degrades_to_unknown(monkeypatch, tmp_path, content) -> None:
    """The portal goes back to saying "neighbourhood unknown"; nothing raises."""
    path = tmp_path / "sf_streets.json"
    if content:
        path.write_text(content, encoding="utf-8")
    _reload_table(monkeypatch, path)
    assert sf_area_from_address("1303 Larkin St") is None


def test_the_table_ships_inside_the_wheel() -> None:
    """It is read at scan time, so a wheel without it silently loses every
    portal neighbourhood."""
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"sf_housing" = ["data/*.json"]' in pyproject


def test_the_table_stays_small_enough_to_ship() -> None:
    assert TABLE_PATH.stat().st_size < 512 * 1024


def _reload_table(monkeypatch, path: pathlib.Path) -> None:
    monkeypatch.setattr(location, "_STREET_TABLE_PATH", path)
    monkeypatch.setattr(location, "_STREET_TABLE", None)
    monkeypatch.setattr(location, "_STREET_NAMES", [])
    monkeypatch.setattr(location, "_BARE_STREETS", {})


# --------------------------------------------------------------------------
# the rule that builds the table: finer layer first, coarse one as a fallback
# --------------------------------------------------------------------------

# Feature ids as the address dataset's own column reports them, so the rows
# below have the shape the build script really receives.
REGION_NAMES = {
    "1": "Parkside",
    "2": "Lower Nob Hill",
    "3": "Nob Hill",
    "4": "Mt. Davidson Manor",
    "5": "Inner Sunset",
}


def _rows(street: str, block: int, tallies: list[tuple[str, str | None, int]]) -> list[dict]:
    """One grouped row per (analysis neighbourhood, fine region) pair."""
    name, _, suffix = street.rpartition(" ")
    return [
        {"street_name": name, "street_type": suffix, "nhood": coarse,
         "region": region, "blk": str(block), "n": str(count)}
        for coarse, region, count in tallies
    ]


def _resolve(rows: list[dict], street: str, block: int) -> str | None:
    from scripts.build_street_neighborhoods import build

    table = build(rows, REGION_NAMES)
    position = table["streets"][street][str(block)]
    return None if position < 0 else table["names"][position]


def test_the_finer_layer_answers_a_block_the_coarse_one_can_only_lump() -> None:
    """The whole fix. "Sunset/Parkside" is three of the product's areas, so the
    coarse layer had to refuse every block in it; the finer layer names
    Parkside outright and is asked first."""
    rows = _rows("TARAVAL ST", 12, [("Sunset/Parkside", "1", 40)])
    assert _resolve(rows, "TARAVAL ST", 12) == "Parkside"


def test_a_block_the_finer_layer_cannot_settle_still_gets_the_coarse_answer() -> None:
    """The fallback is what makes this safe to ship: the finer layer splits
    this block evenly between Nob Hill and Lower Nob Hill, and the coarse layer
    is unanimous, so the answer the table gave before the change survives."""
    rows = _rows("PINE ST", 9, [("Nob Hill", "2", 20), ("Nob Hill", "3", 20)])
    assert _resolve(rows, "PINE ST", 9) == "Nob Hill"


def test_a_finer_area_the_product_cannot_name_falls_back_instead_of_guessing() -> None:
    """Sixty-odd of the city's 117 areas have no counterpart in the product's
    vocabulary. A block inside one asks the coarse layer next, and if that
    cannot say either -- "West of Twin Peaks" is four of the product's areas --
    the block is contested, never rounded to the nearest known area."""
    named = _rows("POST ST", 7, [("Tenderloin", "2", 30)])
    assert _resolve(named, "POST ST", 7) == "Tenderloin"

    unnamed = _rows("DORADO TER", 1, [("West of Twin Peaks", "4", 38)])
    assert _resolve(unnamed, "DORADO TER", 1) is None


def test_the_dominance_bar_is_the_same_for_both_layers() -> None:
    """Coverage must not be bought by lowering the bar. A block the finer layer
    calls Parkside in four addresses out of five is answered; one that calls it
    Parkside in three out of four with nothing else to fall back on is not."""
    at_the_bar = _rows("ULLOA ST", 20, [("Sunset/Parkside", "1", 8), ("Sunset/Parkside", "5", 2)])
    assert _resolve(at_the_bar, "ULLOA ST", 20) == "Parkside"

    below_it = _rows("ULLOA ST", 21, [("Sunset/Parkside", "1", 6), ("Sunset/Parkside", "5", 2)])
    assert _resolve(below_it, "ULLOA ST", 21) is None


def test_the_table_only_ever_names_areas_the_product_knows() -> None:
    """Both mappings feed one name list, and a name nothing can score must
    never reach it -- the resolver's own guard is the second line, not the
    first."""
    from scripts.build_street_neighborhoods import (
        ANALYSIS_TO_CANONICAL,
        FIND_TO_CANONICAL,
    )

    produced = set(FIND_TO_CANONICAL.values()) | set(ANALYSIS_TO_CANONICAL.values())
    assert produced <= set(SF_NEIGHBORHOODS), sorted(produced - set(SF_NEIGHBORHOODS))


# --------------------------------------------------------------------------
# what the renter actually sees: the portal listing carries an area
# --------------------------------------------------------------------------


def _portal_listings(records: list[dict]):
    from sf_housing.preferences import parse_preferences
    from sf_housing.sources import SFHousingPortalSource
    from tests.conftest import TEST_PREFERENCES

    class Response:
        def json(self):
            return {"listings": records}

        def raise_for_status(self):
            return None

    class Client:
        def get(self, url, **kwargs):
            return Response()

    return SFHousingPortalSource().search(Client(), parse_preferences(TEST_PREFERENCES))


def _portal_fixture() -> list[dict]:
    path = pathlib.Path(__file__).parent / "fixtures" / "sf_portal_listings.json"
    return json.loads(path.read_text(encoding="utf-8"))["listings"]


@pytest.mark.usefixtures("portal_lotteries_open")
def test_a_portal_listing_now_carries_the_neighbourhood_its_address_implies() -> None:
    """The listing used to arrive with no area at all, which scored a neutral
    0.5 on the criterion that carries the most weight."""
    listings = _portal_listings(_portal_fixture())
    larkin = [l for l in listings if l.metadata.get("address") == "1303 Larkin St"]

    assert larkin, "the fixture contains the Larkin St building"
    assert all(l.neighborhood == "Nob Hill" for l in larkin)


@pytest.mark.usefixtures("portal_lotteries_open")
def test_most_of_a_real_portal_response_arrives_with_an_area() -> None:
    listings = _portal_listings(_portal_fixture())
    located = [l for l in listings if l.neighborhood]

    assert len(located) / len(listings) >= 0.80


def test_an_area_the_portal_states_itself_still_wins() -> None:
    """The city puts the 500 block of Church St in Mission Dolores; a renter
    standing there would say Duboce Triangle. A name the portal publishes is
    the landlord's own account of where the building is, and it is more local
    than any boundary the city draws, so the table must not overwrite it."""
    record = {
        "Id": "test-duboce",
        "Tenure": "Re-rental",
        "Name": "Duboce Triangle Apartments",
        "Building_Street_Address": "500 Church St",
        "Building_City": "San Francisco",
        "Building_Zip_Code": "94114",
        "unitSummaries": {"general": [{"unitType": "One Bedroom", "minMonthlyRent": 2000}]},
    }
    assert sf_area_from_address("500 Church St") == "Mission Dolores"
    listings = _portal_listings([record])
    assert listings and all(l.neighborhood == "Duboce Triangle" for l in listings)


def test_the_zip_still_answers_when_the_address_cannot() -> None:
    """The ZIP fallback is narrower, not gone; it covers streets the table
    refuses."""
    record = {
        "Id": "test-zip",
        "Tenure": "Re-rental",
        "Name": "A building",
        "Building_Street_Address": "Corner of nowhere",
        "Building_City": "San Francisco",
        "Building_Zip_Code": "94123",
        "unitSummaries": {"general": [{"unitType": "One Bedroom", "minMonthlyRent": 2000}]},
    }
    assert sf_area_from_address("Corner of nowhere") is None
    listings = _portal_listings([record])
    assert listings and all(l.neighborhood == "Marina" for l in listings)
