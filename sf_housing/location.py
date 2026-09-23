"""Small, explicit location checks shared by source parsing and scoring."""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import NamedTuple

from .deal_profile import SF_NEIGHBORHOODS


_OUTSIDE_SF_CITIES = (
    "alameda",
    "albany",
    "american canyon",
    "antioch",
    "belmont",
    "benicia",
    "berkeley",
    "brisbane",
    "burlingame",
    "calistoga",
    "campbell",
    "castro valley",
    "concord",
    "corte madera",
    "cupertino",
    "daly city",
    "danville",
    "dublin",
    "el cerrito",
    "emeryville",
    "fairfield",
    "foster city",
    "fremont",
    "half moon bay",
    "hayward",
    "healdsburg",
    "lafayette",
    "larkspur",
    "livermore",
    "los altos",
    "los gatos",
    "martinez",
    "menlo park",
    "mill valley",
    "millbrae",
    "milpitas",
    "moraga",
    "mountain view",
    "napa",
    "newark",
    "novato",
    "oakland",
    "orinda",
    "pacifica",
    "palo alto",
    "petaluma",
    "pleasanton",
    "redwood city",
    "richmond",
    "rohnert park",
    "san anselmo",
    "san bruno",
    "san carlos",
    "san diego",
    "san jose",
    "san leandro",
    "san lorenzo",
    "san mateo",
    "san pablo",
    "san rafael",
    "san ramon",
    "santa clara",
    "santa rosa",
    "sausalito",
    "sonoma",
    "south san francisco",
    "st helena",
    "suisun city",
    "sunnyvale",
    "tiburon",
    "union city",
    "vacaville",
    "vallejo",
    "walnut creek",
    "windsor",
    "yountville",
)

# Cities whose names are also San Francisco streets, parks or districts. A
# listing near Lafayette Park or on Vallejo Street is in San Francisco, so these
# need "<city>, CA" spelled out before they mean anywhere else.
_ALSO_SAN_FRANCISCO_PLACES = frozenset(
    {"richmond", "lafayette", "moraga", "vallejo", "napa", "santa rosa", "corte madera", "sonoma"}
)

_UNAMBIGUOUS_OUTSIDE_SF_CITIES = tuple(
    city for city in _OUTSIDE_SF_CITIES if city not in _ALSO_SAN_FRANCISCO_PLACES
)

# Shared with scoring: "south san francisco" contains the string a naive
# San Francisco check would accept, and is a different city.
OUTSIDE_SF_CITIES = _OUTSIDE_SF_CITIES


# These run for every listing on every rescore, once per city, and building the
# patterns inside the loop meant re.escape and a cache lookup on each one: 315
# regex searches per listing, and a rescore of 870 homes spending fifteen
# seconds of pure CPU while start-up blocked on it. Same patterns, same order,
# compiled once.
_LOCATION_PREFIX = (
    r"(?:in|at|near|around|location\s*:\s*|located\s+in|available\s+in|"
    r"for\s+rent\s+in|downtown|central|north|south|east|west)\s+"
)
_CITY_WITH_STATE = tuple(
    (re.compile(rf"(?<!\w){re.escape(city)}(?!\w)\s*,?\s+ca(?:\s+\d{{5}})?\b"), f"{city.title()} (outside SF)")
    for city in _OUTSIDE_SF_CITIES
)
# Group posts and short public cards often omit the state. Only accept strong
# location grammar for unambiguous city names; "Richmond" alone can mean San
# Francisco's Richmond District and therefore still requires CA. Both patterns
# for a city are tried before the next city, exactly as before.
_CITY_BY_GRAMMAR = tuple(
    (
        re.compile(rf"{_LOCATION_PREFIX}(?:the\s+)?{re.escape(city)}(?!\w)"),
        re.compile(rf"^[^a-z0-9]{{0,8}}{re.escape(city)}(?!\w)"),
        f"{city.title()} (outside SF)",
    )
    for city in _UNAMBIGUOUS_OUTSIDE_SF_CITIES
)
_WHITESPACE = re.compile(r"\s+")


# One question asked before the hundred below it: does this text name any of
# these cities at all? On a real board of 8,680 homes, 22 of them do -- three
# in a thousand -- and the other 8,658 were each run past 212 patterns to be
# told what a single search could have said. The cache underneath helps a
# second pass over the same board and nothing at all on the first, because the
# key is each listing's own text: measured over one pass it hit 3%.
#
# Built from the labels of both pattern sets rather than from a list of cities
# kept beside them. An earlier attempt at this drew on
# _UNAMBIGUOUS_OUTSIDE_SF_CITIES, which is the narrower set _CITY_BY_GRAMMAR
# needs, and so quietly stopped recognising every city only _CITY_WITH_STATE
# knows -- "richmond, ca" among them, which then read as San Francisco's own
# Richmond district. Derived from the patterns it is guarding, it cannot fall
# behind them: a city added to either set joins this in the same edit.
_GATE_TERMS = frozenset(
    label.removesuffix(" (outside SF)").casefold()
    for label in (
        [label for _, label in _CITY_WITH_STATE]
        + [label for _, _, label in _CITY_BY_GRAMMAR]
    )
)
_MENTIONS_ANY_CITY = re.compile(
    "|".join(re.escape(term) for term in sorted(_GATE_TERMS, key=len, reverse=True))
)


# Keyed on the listing text, which is what makes it worth keeping: scoring the
# same board against a second deal asks this the same questions about the same
# strings. It was 45% of a scoring pass -- 227 regex searches per listing, most
# of them here -- and the shortlist estimate re-scores on every keystroke.
# Bounded because the key is the whole text of a listing, not a word of it.
@lru_cache(maxsize=2048)
def declared_outside_sf_area_hint(text: str | None) -> str | None:
    """Return a clearly declared city outside San Francisco."""
    normalized = _WHITESPACE.sub(" ", (text or "").casefold()).strip()
    if not normalized:
        return None
    # Naming a city is necessary for either loop below to match, so text that
    # names none cannot match and is answered here.
    if not _MENTIONS_ANY_CITY.search(normalized):
        return None
    for pattern, label in _CITY_WITH_STATE:
        if pattern.search(normalized):
            return label
    for prefixed, at_start, label in _CITY_BY_GRAMMAR:
        if prefixed.search(normalized) or at_start.match(normalized):
            return label
    return None


def outside_sf_location_label(label: str | None) -> str | None:
    """Return the city when a search card's own area label is another one.

    Craigslist pads a thin San Francisco search with the rest of the Bay Area,
    and those cards label themselves plainly ("Napa"). Only an exact match
    counts, and only for a name that is not also a San Francisco place: the
    city's own area labels include "richmond / seacliff", which must never read
    as the city of Richmond.
    """
    normalized = re.sub(r"\s+", " ", (label or "").casefold()).strip(" ()")
    if not normalized:
        return None
    for city in _UNAMBIGUOUS_OUTSIDE_SF_CITIES:
        if normalized == city:
            return f"{city.title()} (outside SF)"
    return None


_CITY_IN_URL = tuple(
    (re.compile(rf"/view/d/{re.escape(city.replace(' ', '-'))}(?:-|/)"), f"{city.title()} (outside SF)")
    for city in _OUTSIDE_SF_CITIES
)


def declared_outside_sf_url_hint(url: str | None) -> str | None:
    """Return an outside city explicitly encoded in a Craigslist detail URL."""
    normalized = (url or "").casefold()
    if "/view/d/" not in normalized:
        return None
    for pattern, label in _CITY_IN_URL:
        if pattern.search(normalized):
            return label
    return None


# ---------------------------------------------------------------------------
# street address -> neighbourhood
# ---------------------------------------------------------------------------

# The city housing portal states a street address and a ZIP but never a
# neighbourhood, and most SF ZIPs straddle two areas, so the ZIP could only
# answer for a handful of listings. The table below is built from the city's own
# address dataset by scripts/build_street_neighborhoods.py and consulted
# offline: no network call during a scan, and no third party told which homes
# someone is looking at.

_STREET_TABLE_PATH = Path(__file__).resolve().parent / "data" / "sf_streets.json"

# Written form -> the abbreviation the city's dataset uses.
_STREET_TYPES = {
    "ALLEY": "ALY", "ALY": "ALY",
    "AV": "AVE", "AVE": "AVE", "AVENUE": "AVE",
    "BLVD": "BLVD", "BOULEVARD": "BLVD",
    "CIR": "CIR", "CIRCLE": "CIR",
    "COURT": "CT", "CT": "CT",
    "DR": "DR", "DRIVE": "DR",
    "HIGHWAY": "HWY", "HWY": "HWY",
    "LANE": "LN", "LN": "LN",
    "LOOP": "LOOP",
    "PARK": "PARK",
    "PARKWAY": "PKWY", "PKWY": "PKWY",
    "PL": "PL", "PLACE": "PL",
    "PLAZA": "PLZ", "PLZ": "PLZ",
    "RD": "RD", "ROAD": "RD",
    "ST": "ST", "STREET": "ST",
    "STAIRWAY": "STWY", "STWY": "STWY",
    "TER": "TER", "TERR": "TER", "TERRACE": "TER",
    "WALK": "WALK",
    "WAY": "WAY", "WY": "WAY",
}

# The dataset zero-pads numbered streets ("03RD ST", "15TH AVE"), and people
# write the small ones as words.
_SPELLED_ORDINALS = {
    "FIRST": "01ST", "SECOND": "02ND", "THIRD": "03RD", "FOURTH": "04TH",
    "FIFTH": "05TH", "SIXTH": "06TH", "SEVENTH": "07TH", "EIGHTH": "08TH",
    "NINTH": "09TH", "TENTH": "10TH", "ELEVENTH": "11TH", "TWELFTH": "12TH",
}

# A half of a divided street ("Mission Bay Blvd North"). Tried only as a
# fallback: "Avenue E" and "Avenue N" on Treasure Island are streets in
# their own right.
_BLOCK_QUALIFIERS = {"NORTH", "SOUTH", "EAST", "WEST", "N", "S", "E", "W"}

# "1200 Market St #501", "Apt 2", "Unit B", "Ste 300".
_UNIT_SUFFIX = re.compile(
    r"\s*(?:#|\b(?:APT|APARTMENT|UNIT|STE|SUITE|RM|ROOM|FL|FLOOR|NO)\b\.?\s*)\S*.*$"
)
# "451 Kansas St at 17th St", "Powell St & Market St", cross streets and cities.
_TAIL = re.compile(r"\s+(?:AT|AND|NEAR|BETWEEN|BTWN)\s+.*$|\s*[,&/(].*$")

_STREET_TABLE: dict[str, dict[str, int]] | None = None
_STREET_NAMES: list[str] = []
_BARE_STREETS: dict[str, list[str]] = {}


def _load_street_table() -> dict[str, dict[str, int]]:
    """Read the shipped table once. A missing or broken file is not fatal: the
    portal simply goes back to answering "neighbourhood unknown"."""
    global _STREET_TABLE, _STREET_NAMES, _BARE_STREETS
    if _STREET_TABLE is not None:
        return _STREET_TABLE
    try:
        raw = json.loads(_STREET_TABLE_PATH.read_text(encoding="utf-8"))
        names = [str(name) for name in raw["names"]]
        streets = {str(k): {str(b): int(i) for b, i in v.items()}
                   for k, v in raw["streets"].items()}
    except (OSError, ValueError, KeyError, TypeError):
        names, streets = [], {}
    # A name the product no longer knows must not reach a listing, so drop it
    # here rather than letting an unknown area through to the score.
    known = [name if name in SF_NEIGHBORHOODS else "" for name in names]
    bare: dict[str, list[str]] = {}
    for key in streets:
        head = key.rsplit(" ", 1)[0] if " " in key else key
        bare.setdefault(head, []).append(key)
    _STREET_NAMES, _STREET_TABLE, _BARE_STREETS = known, streets, bare
    return _STREET_TABLE


def _normalise_street(text: str) -> str:
    words = []
    for word in text.split():
        word = _SPELLED_ORDINALS.get(word, word)
        ordinal = re.fullmatch(r"(\d{1,2})(ST|ND|RD|TH)", word)
        if ordinal:
            word = f"{int(ordinal.group(1)):02d}{ordinal.group(2)}"
        words.append(word)
    if len(words) > 1 and words[-1] in _STREET_TYPES:
        words[-1] = _STREET_TYPES[words[-1]]
    return " ".join(words)


def _street_variants(street: str) -> list[str]:
    """The spellings worth trying, most literal first.

    Each fallback is only reached when the more literal spelling is not a real
    street, which is what keeps "Mrs. Jackson Way" and "Avenue E" intact.
    """
    variants = [street]
    words = street.split()
    # "1201 Tennessee St." -- an abbreviation point the city does not record.
    # Only the final word is trimmed, because "Mayor Edwin M. Lee Ave" keeps its.
    if words and words[-1].endswith(".") and words[-1].rstrip(".") in _STREET_TYPES:
        variants.append(" ".join([*words[:-1], _STREET_TYPES[words[-1].rstrip(".")]]))
    # "588 Mission Bay Blvd North" -- the city records the north and south halves
    # of a divided street under the one name.
    for variant in list(variants):
        parts = variant.split()
        if len(parts) > 2 and parts[-1] in _BLOCK_QUALIFIERS:
            variants.append(" ".join(parts[:-1]))
    return variants


def _street_key(street: str) -> str | None:
    """Match a written street against the dataset's own spelling."""
    table = _load_street_table()
    for variant in _street_variants(street):
        if variant in table:
            return variant
        # No street type given ("1200 Market"): answer only when one street fits.
        candidates = _BARE_STREETS.get(variant, [])
        if len(candidates) == 1:
            return candidates[0]
    return None


# The unit itself, where _UNIT_SUFFIX only needs to know where it starts: the
# label that introduced it and the first token after it ("APT 304", "# 9-842").
_UNIT_TOKEN = re.compile(
    r"(#|\b(?:APT|APARTMENT|UNIT|STE|SUITE|RM|ROOM|FL|FLOOR|NO)\b\.?)\s*([A-Z0-9][A-Z0-9-]*)"
)


class StreetAddress(NamedTuple):
    """A written address resolved against the city's street dataset."""

    number: int
    street: str
    # How the address introduced its unit ("APT", "#", "STE"...), or None.
    unit_label: str | None
    unit: str | None
    # The last number of a range ("405-413 Laguna St"), or ``number`` itself.
    number_to: int = 0
    # Words follow the unit before the next comma ("unit 1649 Upstairs"): the
    # token is only the start of a description, not a door number.
    unit_has_more: bool = False


def split_street_address(address: str | None) -> StreetAddress | None:
    """The number, the dataset's street key, and the unit the address names.

    The same reading as ``parse_street_address``, which is this without the
    unit: the unit is what tells two flats in one building apart, so identity
    needs it while the neighbourhood lookup deliberately does not.
    """
    text = re.sub(r"\s+", " ", str(address or "")).strip().upper()
    if not text:
        return None
    # Read before the tail goes: "1400 Mission St, Apt 5" names its unit after
    # the comma the tail pattern cuts at.
    unit_match = _UNIT_TOKEN.search(text)
    text = _TAIL.sub("", text)
    # "1451 Sacramento Street - #07": the dash was only there to introduce the
    # unit, and left behind it hides the street type.
    text = _UNIT_SUFFIX.sub("", text).strip().rstrip(" -")
    # A range ("1200-1250 Market St") is answered by its first number; a
    # trailing letter ("1200A") is a unit, not part of the number.
    leading = re.match(r"^(\d{1,5})(?:\s*-\s*(\d{1,5}))?([A-Z])?\s+(.+)$", text)
    if not leading:
        return None
    number, street = int(leading.group(1)), _normalise_street(leading.group(4))
    key = _street_key(street)
    if not key:
        return None
    number_to = int(leading.group(2)) if leading.group(2) and int(leading.group(2)) >= number else number
    if unit_match:
        rest = re.split(r"[,(]", unit_match.string[unit_match.end():], maxsplit=1)[0]
        return StreetAddress(
            number, key, unit_match.group(1).rstrip("."), unit_match.group(2),
            number_to, bool(re.search(r"[A-Z0-9]", rest)),
        )
    if leading.group(3):
        return StreetAddress(number, key, None, leading.group(3), number_to)
    return StreetAddress(number, key, None, None, number_to)


def read_unit(text: str | None) -> tuple[str, str] | None:
    """The label and token of a unit written on its own ("APT 323", "#4C")."""
    match = _UNIT_TOKEN.search(re.sub(r"\s+", " ", str(text or "")).strip().upper())
    return (match.group(1).rstrip("."), match.group(2)) if match else None


def parse_street_address(address: str | None) -> tuple[int, str] | None:
    """Split a written address into its number and the dataset's street key."""
    parsed = split_street_address(address)
    return (parsed.number, parsed.street) if parsed else None


# Labels that name the city rather than any part of it. Scoring treats them as
# no neighbourhood at all, and the filter and the page leave them out.
GENERIC_LOCATIONS = {"sf", "san francisco", "city of san francisco", "san francisco, ca"}


def _area_fold(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.casefold())


_CANONICAL_AREAS = {_area_fold(name): name for name in SF_NEIGHBORHOODS}
_GENERIC_AREAS = {_area_fold(label) for label in GENERIC_LOCATIONS}
_STATE_SUFFIX = re.compile(r",?\s*(?:ca|california)\.?$", re.IGNORECASE)


# One area has one spelling, so the same few hundred labels are folded over
# and over: the rent table alone asked 9,703 times about 537 distinct strings
# while ranking one board. The answer depends on nothing but the string, and
# the tables it reads are built once at import, so a cache of them is the same
# function with the repeated work removed. Sized well past the ~540 spellings
# a real board holds, and bounded so a source inventing labels cannot grow it
# without limit.
@lru_cache(maxsize=8192)
def canonical_neighborhood(value: str | None) -> str | None:
    """One spelling for one area, so one area is one group everywhere.

    Craigslist writes its area labels in lower case and the other sources use
    the product's own names, so the same neighbourhood was stored as
    "Tenderloin" 490 times and "tenderloin" 207: two groups in every median,
    every count and the filter list. A value that names one of the product's
    neighbourhoods -- ignoring case, punctuation and spacing, and a trailing
    ", CA" -- becomes that name exactly; a label for the whole city becomes
    "San Francisco"; anything else is only trimmed. Nothing is ever
    title-cased: "SoMa" and "NOPA" are already right, and a Craigslist
    compound like "SOMA / south beach" is not this function's to split.
    """
    if value is None:
        return None
    text = _WHITESPACE.sub(" ", str(value)).strip()
    if not text:
        return text
    for candidate in (text, _STATE_SUFFIX.sub("", text).strip()):
        folded = _area_fold(candidate)
        if folded in _CANONICAL_AREAS:
            return _CANONICAL_AREAS[folded]
        if folded in _GENERIC_AREAS:
            return "San Francisco"
    return text


def sf_area_from_address(address: str | None) -> str | None:
    """Return the San Francisco neighbourhood a street address sits in.

    Answers from the hundred block, because a street-wide range is destroyed by
    one mis-geocoded record: a single stray address put Russian Hill across all
    of Larkin St, swallowing Nob Hill and the Tenderloin. A block the city's own
    data splits between neighbourhoods returns nothing, so the answer is stable
    from one scan to the next and never guesses a boundary.
    """
    if declared_outside_sf_area_hint(address):
        return None
    parsed = parse_street_address(address)
    if parsed is None:
        return None
    number, key = parsed
    blocks = _load_street_table()[key]
    position = blocks.get(str(number // 100))
    if position is None:
        # An address number the city has no record of. A street that is wholly
        # inside one neighbourhood can still answer; a street that crosses one
        # cannot.
        distinct = set(blocks.values())
        if len(distinct) != 1 or -1 in distinct:
            return None
        position = distinct.pop()
    if position < 0 or position >= len(_STREET_NAMES):
        return None
    return _STREET_NAMES[position] or None


# Sources write whatever they have into the area field, and not all of them
# have an area. Two kinds of string arrive that no reader would ever pick out
# of a neighbourhood list: a place somewhere else entirely, and a mailing
# address. The patterns below are what tells those apart from a name.

# Every state's postal abbreviation but California's, and the District. A label
# that ends in one is an address from somewhere else -- "Miami, FL", "New York,
# NY", "Chicago, IL" -- and needs the comma: "Downtown LA" is how a Los Angeles
# card writes itself, but "LA" alone is also Louisiana, and a bare two letters
# at the end of a label is not evidence of anything.
_OTHER_STATES = (
    "AL AK AZ AR CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT "
    "NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC"
).split()
_ENDS_IN_ANOTHER_STATE = re.compile(
    rf",\s*(?:{'|'.join(_OTHER_STATES)})(?![A-Za-z])[\s,.]*$", re.IGNORECASE
)
# Stricter than _STATE_SUFFIX above, which exists to trim a tail before looking
# a name up and can afford to be loose about where "ca" starts. Here the match
# is the whole evidence that the label is an address, so "Manteca" and "Santa
# Monica" must not read as ending in California.
_ENDS_IN_CALIFORNIA = re.compile(
    r",?\s*(?<![A-Za-z])(?:CA|CALIF|CALIFORNIA)(?![A-Za-z])[\s,.]*$", re.IGNORECASE
)
_SAYS_SAN_FRANCISCO = re.compile(r"(?<![A-Za-z])(?:SAN\s+FRANCISCO|SF)(?![A-Za-z])", re.IGNORECASE)
_ENDS_IN_SAN_FRANCISCO = re.compile(
    rf",?\s*{_SAYS_SAN_FRANCISCO.pattern}[\s,.]*$", re.IGNORECASE
)
# The street types that only ever end a street. _STREET_TYPES knows three more
# -- PARK, TER and WALK -- and those are also how San Francisco names places
# people live: Holly Park, Glen Park, Midtown Terrace. A label ending in one of
# the three is left alone whatever the street table says about it.
_ENDS_IN_STREET_TYPE = re.compile(
    r"(?<![A-Za-z])(?:ST|STREET|AVE|AV|AVENUE|BLVD|BOULEVARD|DR|DRIVE|RD|ROAD|LN|LANE|"
    r"CT|COURT|PL|PLACE|WAY|WY|CIR|CIRCLE|HWY|HIGHWAY|PLZ|PLAZA|ALY|ALLEY)\.?$",
    re.IGNORECASE,
)


def misfiled_area_label(label: str | None) -> str | None:
    """Return why a stored area string names no San Francisco area, or None.

    The reason is for a test or a log to quote, never for a reader: the caller
    only needs to know there is one. Deliberately narrow, because most of the
    strings that are not one of the product's own area names are perfectly
    good Craigslist compounds -- "downtown / civic / van ness" carries 208 of
    the author's homes on its own, "SOMA / south beach" 91 -- and a rule that
    kept only canonical names would take those away to be rid of "Miami, FL".

    So it answers only where the string says plainly that it is something
    else. It cannot recognise a bare out-of-town neighbourhood ("Bushwick",
    "Harlem", "Stockton"): nothing here knows those names, and guessing at
    them would eventually guess at one of ours.
    """
    text = _WHITESPACE.sub(" ", str(label or "")).strip()
    if not text:
        return None
    elsewhere = outside_sf_location_label(text) or declared_outside_sf_area_hint(text)
    if elsewhere:
        return elsewhere
    if _ENDS_IN_ANOTHER_STATE.search(text):
        return "names a state other than California"
    # A label that spells out the state is giving a mailing address, and an
    # address that never says San Francisco is not one: "Santa Cruz, CA",
    # "Aromas, CA", "1371 Camilla St. Manteca, Ca.". Neither helper above
    # catches these -- outside_sf_location_label matches a bare city name
    # exactly, so the suffix defeats it, and the Bay Area city lists have no
    # reason to know Aromas or Manteca.
    if _ENDS_IN_CALIFORNIA.search(text):
        # Unless the state was glued to one of our own area names, which is
        # common enough that canonical_neighborhood strips it before looking a
        # name up. "Castro, CA" is the Castro.
        rest = _ENDS_IN_CALIFORNIA.sub("", text).strip(" ,")
        if (
            rest
            and not _SAYS_SAN_FRANCISCO.search(rest)
            and _area_fold(rest) not in _CANONICAL_AREAS
        ):
            return "a California address that never says San Francisco"
    if split_street_address(text):
        return "a street address"
    # The same address with the house number left off, which is most of what
    # these sources write: "Sweeny St, San Francisco, CA", "35th Ave, San
    # Francisco, CA". split_street_address needs the number, so the street is
    # read from its own two halves instead -- a written street type, and the
    # city written after it, which is what makes the label an address rather
    # than a name. Both halves are needed: Holly Park, Lake Merced, Van Ness
    # and West Portal are all in the city's street table, and all four are
    # places somebody lives.
    head = _ENDS_IN_CALIFORNIA.sub("", text).strip(" ,")
    head = _ENDS_IN_SAN_FRANCISCO.sub("", head).strip(" ,")
    if head != text and _ENDS_IN_STREET_TYPE.search(head):
        return "a street, not an area"
    return None
