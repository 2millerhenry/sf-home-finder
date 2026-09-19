"""Which stored listings are one real home, and which are only one site's slot.

A row in ``listings`` is one site's card: ``(platform, source_id)`` and the URL
say where it came from, not what it is. The same flat on Zillow, Movoto and
Redfin was three rows that nothing connected -- so passing it on one site left
it on the shortlist from the other two, "also listed elsewhere" matched every
flat in a building because it keyed on the street alone, and a site that reused
an id quietly rewrote a starred home into a different apartment.

This module is the one place that decides. Everything that needs to know --
matching an incoming card to a stored row, carrying a star or a pass to every
copy, the 21-day age-out, choosing which copies to show, "this home elsewhere",
the rent estimate's sample -- reads what it says and never works it out again.

The rules are conservative on purpose. Merging two different flats hides one
and passes it on the user's behalf; missing a merge only leaves a duplicate on
the page. A key is issued only on positive evidence, and measured on a real
board of 8,680 rows the rules below join 1,525 rows into 625 homes (900 copies
fewer on the page), and no home mixes two stated sizes or holds two of one
site's unit pages:

* A unit page (one flat, from a site whose unit number is the flat on offer):
  the same street number, street and unit, whatever size each site states --
  sites that leave the bedrooms out would otherwise split one flat in two.
  Price is never part of it: one flat quoted at two rents is exactly what the
  page should show, not two homes. A unit that is itself a street number --
  a house repeating its own ("1128 Valencia St #1128"), or one door of a
  building numbered as a range ("25-27 Dore St #27") -- is keyed as that
  number's house, never the range's, so two doors of a duplex stay two homes.
* A building or floorplan card ("1 bed from $2,895" at an address): the same
  building, the same size and the same rent. It says which offer, not which
  door, so it never joins a unit page -- two free flats of one size behind two
  cards at one price are the same offer to anybody reading them.
* The same record syndicated by one backend: ApartmentGuide's ``LVx`` is
  Rent.com's ``lvx``, its ``6589544`` is Rent.com's ``lc6589544`` (a complex
  card, so the size has to agree too), and a Zillow alert email's
  ``<zpid>_zpid`` is the scraped Zillow ``<zpid>``. When one side of such a
  pair has a key of the kinds above, the other adopts it (see ``twin_ids`` and
  ``adoptable``).

Everything else is its own home: rows with no street address (Craigslist,
SpareRoom, the alert emails), rooms, and unit pages whose unit cannot be read.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlsplit

from .location import StreetAddress, read_unit, split_street_address

# Bumped whenever the rules below change, so stored keys are recomputed once on
# the next start rather than left describing the old rules.
IDENTITY_VERSION = 1

# The grouping value SQL uses: the shared key, or the row on its own.
def group_sql(table: str = "") -> str:
    prefix = f"{table}." if table else ""
    return f"COALESCE(NULLIF({prefix}home_key, ''), 'row:' || {prefix}id)"


GROUP_SQL = group_sql()

UNIT_GRAIN = "unit"
BUILDING_GRAIN = "building"
NO_GRAIN = "none"

# What D2 falls back to when two copies say equally much: the sites that are
# the landlord or the MLS first, then the aggregators that copy them.
PLATFORM_ORDER = (
    "AvalonBay",
    "AppFolio",
    "Abacus (small buildings)",
    "RentSFNow",
    "Movoto",
    "Zillow",
    "Redfin",
    "Trulia",
    "ApartmentGuide",
    "Rent.com",
    "Zumper",
    "Apartment List",
    "UDR",
    "SF Housing Portal",
    "Listings Project",
    "Craigslist",
    "SpareRoom",
    "Uloop",
)

# The labels an address uses to introduce a unit. As a unit *token* they mean
# the address was garbled ("#STE 107"): nothing to trust.
_LABEL_WORDS = frozenset({"STE", "SUITE", "RM", "ROOM", "FL", "FLOOR", "NO", "APT", "APARTMENT", "UNIT"})
# Labels whose number is not a home: a leasing office's suite is shared by every
# home the office lets (Fox Plaza's "STE 107" sits on three), a room is part of
# a home, and a floor can hold several.
_UNTRUSTED_LABELS = frozenset({"STE", "SUITE", "RM", "ROOM", "FL", "FLOOR"})
# Zillow invents unit ids that no other site prints ("#737R", "#1149RC",
# "#3ID1621").
_ZILLOW_SYNTHETIC_UNIT = re.compile(r"\d+R|\d+RC|\d*ID\d+")
# A detached row (see Repository.upsert_listing) keeps its old id with its own
# row id appended, so nothing can match it by id again.
_DETACHED = re.compile(r"^(.*)#\d+$")
# The key a home held together only by a site's record id moves to when the
# site re-lets that id to another flat: "moved:<row id>". No card can earn it,
# so the flat the id now names can never join the home the user decided about.
MOVED_PREFIX = "moved:"


def is_id_key(key: str) -> bool:
    """Whether a key says only which record a card is, not where the home is."""
    return key.startswith(("rentpath:", "zpid:"))


@dataclass(frozen=True)
class Facts:
    """What a card says about which home it is."""

    number: int | None
    number_to: int | None
    street: str | None
    # The unit when it can be trusted as a door number; "=" for a house that
    # repeats its own street number as its unit.
    unit: str | None
    # Whatever unit the address names, trusted or not, for the strict
    # comparison a user's own row gets.
    unit_raw: str | None
    grain: str
    address_raw: str | None
    # The size the card states, when it states one.
    size: str | None = None


def _text(value: Any) -> str:
    return str(value or "").strip()


def base_source_id(source_id: str) -> tuple[str, bool]:
    """The id a site gave the card, and whether the row has been detached."""
    sid = _text(source_id)
    detached = _DETACHED.match(sid)
    return (detached.group(1), True) if detached else (sid, False)


def grain(platform: str, source_id: str, original_url: str) -> str:
    """Whether a card is one home, a building (or floorplan), or cannot say.

    Decided per site from the shape of its id and URL, which is what each site
    actually tells apart; measured against every stored row of each platform.
    """
    path = urlsplit(_text(original_url)).path.casefold()
    sid, _ = base_source_id(source_id)
    if platform == "Movoto":
        # MLS listings are units; /rental/ pages are a community's floorplans.
        return UNIT_GRAIN if path.startswith("/san-francisco-ca/") else BUILDING_GRAIN
    if platform == "Zillow":
        # A home's own page, or a unit's own id on a building page. The
        # building cards are keyed by coordinates ("37.77--122.41"); an
        # alert email's "<zpid>_zpid" publishes no address to be a unit of.
        if "/homedetails/" in path or re.fullmatch(r"\d+", sid):
            return UNIT_GRAIN
        return BUILDING_GRAIN
    if platform == "Trulia":
        return BUILDING_GRAIN if path.startswith("/building/") else UNIT_GRAIN
    if platform in {"Rent.com", "ApartmentGuide"}:
        return UNIT_GRAIN if sid.casefold().startswith("lv") else BUILDING_GRAIN
    if platform == "Redfin":
        return UNIT_GRAIN if "/unit-" in path else BUILDING_GRAIN
    if platform == "Zumper":
        return UNIT_GRAIN if path.startswith("/listings/") else BUILDING_GRAIN
    if platform in {"AvalonBay", "AppFolio", "Abacus (small buildings)", "RentSFNow"}:
        return UNIT_GRAIN
    if platform in {"Apartment List", "UDR"}:
        return BUILDING_GRAIN
    # The city portal's lottery floorplans, and the sites that publish no
    # address a unit could be read from (Craigslist, SpareRoom, Listings
    # Project, Uloop, the alert emails).
    return NO_GRAIN


def _unit_value(token: str | None) -> str | None:
    value = re.sub(r"[^A-Z0-9]", "", _text(token).upper())
    if value.isdigit():
        value = value.lstrip("0") or "0"
    return value or None


def normalise_unit(
    platform: str,
    address: StreetAddress,
    label: str | None,
    token: str | None,
    has_more: bool = False,
) -> str | None:
    """The unit as a comparable door number, or None when it cannot be trusted.

    "#01", "APT 1" and "Unit 001" are one unit; "N-306" is not "306". Trusted
    by shape rather than by a list of exceptions: up to five digits, a
    hyphenated composite ("1-218"), or at most two letters with up to five
    digits ("4C", "161A", "PH"). Floorplan labels ("1B-1Ba"), site ids of six
    digits or more, hashes and words ("SAN", "SMALL") are not door numbers.
    """
    if not token or has_more:
        return None
    if label and label.upper() in _UNTRUSTED_LABELS:
        return None
    upper = token.upper()
    if upper in _LABEL_WORDS:
        return None
    value = _unit_value(upper)
    if value is None:
        return None
    parts = [part for part in upper.split("-") if part]
    digits = sum(character.isdigit() for character in value)
    letters = len(value) - digits
    if len(parts) > 1:
        trusted = all(re.fullmatch(r"[A-Z]{0,2}\d{1,5}[A-Z]?", part) for part in parts)
    else:
        trusted = letters <= 2 and digits <= 5 and (letters or digits)
    if not trusted:
        return None
    if platform == "Zillow" and _ZILLOW_SYNTHETIC_UNIT.fullmatch(value):
        return None
    return value


def facts(
    platform: str,
    source_id: str,
    original_url: str,
    metadata: Mapping[str, Any] | None,
    title: str | None,
    unit_type: str | None = None,
) -> Facts:
    """The street, number, unit, grain and size a card states."""
    kind = grain(platform, source_id, original_url)
    metadata = metadata or {}
    raw_address = _text(metadata.get("address"))
    address_raw = re.sub(r"[^a-z0-9]", "", raw_address.casefold()) or None
    size = _text(unit_type) or None
    parsed = split_street_address(raw_address)
    if parsed is None:
        return Facts(None, None, None, None, None, kind, address_raw, size)
    label, token, has_more = parsed.unit_label, parsed.unit, parsed.unit_has_more
    if token is None:
        # Movoto keeps it in a field of its own.
        stated = read_unit(_text(metadata.get("unit")))
        if stated is not None:
            label, token = stated
    if token is None and title:
        # RentSFNow keeps it only in the title ("655 Powell #503"), which
        # counts only when the title is this same address.
        titled = split_street_address(_text(title))
        if titled is not None and (titled.number, titled.street) == (parsed.number, parsed.street):
            label, token, has_more = titled.unit_label, titled.unit, titled.unit_has_more
    unit = normalise_unit(platform, parsed, label, token, has_more) if kind == UNIT_GRAIN else None
    number, number_to = parsed.number, max(parsed.number, parsed.number_to or parsed.number)
    if unit is not None and unit.isdigit() and number <= int(unit) <= number_to:
        # The unit is a street number: "462 21st Ave Unit 462" is the house
        # repeating its own number, and "25-27 Dore St #27" is the door at 27
        # of a building numbered 25 to 27. The home is that number's, marked
        # rather than dropped so the same house on two sites is one -- and
        # never the whole range's, or every door in it would be one home.
        number = number_to = int(unit)
        unit = "="
    return Facts(number, number_to, parsed.street, unit, _unit_value(token), kind, address_raw, size)


def building_of(metadata: Mapping[str, Any] | None) -> tuple[str, int, int] | None:
    """The building a card's address names: street and the range of numbers.

    Not the home: "25-27 Dore St #27" is the door at 27 (see ``facts``) in
    the building numbered 25 to 27, and its neighbour at #25 is in the same
    building.
    """
    parsed = split_street_address(_text((metadata or {}).get("address")))
    if parsed is None:
        return None
    return parsed.street, parsed.number, max(parsed.number, parsed.number_to or parsed.number)


def own_key(
    platform: str,
    source_id: str,
    original_url: str,
    metadata: Mapping[str, Any] | None,
    title: str | None,
    housing_kind: str | None,
    unit_type: str | None,
    price: int | None,
) -> str:
    """The key a card earns from what it states about itself, or ""."""
    if housing_kind != "whole_unit":
        # Rooms in one flat are different homes, whatever the address says.
        return ""
    stated = facts(platform, source_id, original_url, metadata, title, unit_type)
    if stated.street is None:
        return ""
    if stated.grain == UNIT_GRAIN and stated.unit is not None:
        return f"unit:{stated.number}|{stated.street}|{stated.unit}"
    if stated.grain == BUILDING_GRAIN and unit_type and price is not None:
        return f"offer:{stated.number}|{stated.street}|{unit_type}|{int(price)}"
    return ""


def id_key(platform: str, source_id: str, unit_type: str | None) -> str:
    """The key a syndicated record carries by its id alone, or ""."""
    sid, detached = base_source_id(source_id)
    if detached:
        # The id now belongs to whatever card reused it.
        return ""
    if platform in {"ApartmentGuide", "Rent.com"}:
        if re.fullmatch(r"lv\w+", sid, re.IGNORECASE):
            return f"rentpath:{sid.casefold()}"
        complex_id = (
            sid if platform == "ApartmentGuide" and re.fullmatch(r"\d+", sid)
            else sid[2:] if platform == "Rent.com" and re.fullmatch(r"lc\d+", sid, re.IGNORECASE)
            else ""
        )
        # A complex card describes whichever floorplan suits the deal, so the
        # same complex is one home only at one size.
        if complex_id and unit_type:
            return f"rentpath:lc{complex_id}|{unit_type}"
        return ""
    if platform == "Zillow":
        match = re.fullmatch(r"(\d+)(?:_zpid)?", sid)
        if match:
            return f"zpid:{match.group(1)}"
    return ""


def twin_ids(platform: str, source_id: str) -> list[tuple[str, str]]:
    """The (platform, source_id) pairs that are this same record elsewhere."""
    sid, detached = base_source_id(source_id)
    if detached:
        return []
    if platform == "ApartmentGuide":
        if re.fullmatch(r"lv\w+", sid, re.IGNORECASE):
            return [("Rent.com", sid.casefold())]
        if re.fullmatch(r"\d+", sid):
            return [("Rent.com", f"lc{sid}")]
    if platform == "Rent.com":
        if re.fullmatch(r"lv\w+", sid, re.IGNORECASE):
            return [("ApartmentGuide", sid)]
        if re.fullmatch(r"lc\d+", sid, re.IGNORECASE):
            return [("ApartmentGuide", sid[2:])]
    if platform == "Zillow":
        match = re.fullmatch(r"(\d+)(?:_zpid)?", sid)
        if match:
            other = match.group(1) if sid.endswith("_zpid") else f"{sid}_zpid"
            return [("Zillow", other)]
    return []


def adoptable(twin_key: str, unit_type: str | None) -> bool:
    """Whether a row may take the key its syndicated twin earned.

    A unit's key names no size, so it passes to the twin as it is. A building
    card's offer key names one size, and the twin of a complex card may be
    describing another of its floorplans: measured on a real board, adopting
    regardless joined a one-bedroom and a studio at 855 Brannan.
    """
    if twin_key.startswith("offer:"):
        return bool(unit_type) and twin_key.split("|")[2] == unit_type
    return bool(twin_key)


def choose_key(own: str, twin_own_keys: list[str], unit_type: str | None, by_id: str) -> str:
    """A row's home key: its own, else an adoptable twin's, else its id's."""
    if own:
        return own
    for twin_key in twin_own_keys:
        if twin_key and adoptable(twin_key, unit_type):
            return twin_key
    return by_id


def facts_for_row(row: Mapping[str, Any], metadata: Mapping[str, Any] | None = None) -> Facts:
    return facts(row["platform"], row["source_id"], row["original_url"], metadata, row["title"], row["unit_type"])


def facts_for_listing(listing: Any) -> Facts:
    return facts(
        listing.platform, listing.source_id, listing.original_url, listing.metadata, listing.title,
        listing.unit_type,
    )


def _overlap(a: Facts, b: Facts) -> bool:
    return max(a.number or 0, b.number or 0) <= min(a.number_to or 0, b.number_to or 0)


def same_home(stored: Facts, incoming: Facts, *, strict: bool, same_url: bool = False) -> bool:
    """Whether a card arriving under a key a stored row holds is still that home.

    For a row nobody owns the question is only "is there evidence it moved":
    two different streets, two numbers that cannot be one building, or two
    trusted unit numbers. A building card's featured unit rotates between
    scans, so for those only the street counts. No address on either side is
    no evidence, and the card is taken to be the one stored.

    For a row the user starred, noted or passed, sameness has to be shown, not
    assumed. Both addresses read and agreeing, including the unit -- trusted
    or not -- when both name one. A card naming a unit where the other names
    none is only a thinner or fuller card if it is the same page. A building
    card's featured unit rotates, so for those the same street and the same
    stated size are what show it is the same offer: a complex card now
    describing another floorplan is another home. With no address on either
    side, only the same page counts; a card naming an address the user's row
    never had is not shown to be it. Anything less and the user's row is left
    exactly as it is and the card becomes a row of its own.
    """
    if not strict:
        if stored.street and incoming.street:
            if stored.street != incoming.street:
                return False
            if BUILDING_GRAIN in {stored.grain, incoming.grain}:
                return True
            if not _overlap(stored, incoming):
                return False
        if stored.unit and incoming.unit and stored.unit != incoming.unit:
            return False
        return True
    if stored.size and incoming.size and stored.size != incoming.size and BUILDING_GRAIN in {
        stored.grain, incoming.grain
    }:
        return False
    if stored.street and incoming.street:
        if stored.street != incoming.street or not _overlap(stored, incoming):
            return False
        if BUILDING_GRAIN in {stored.grain, incoming.grain}:
            return True
        if stored.unit_raw and incoming.unit_raw:
            return stored.unit_raw == incoming.unit_raw
        if stored.unit_raw or incoming.unit_raw:
            return same_url
        return True
    if stored.street and incoming.address_raw and not incoming.street:
        # "Address not disclosed" on the page the star was put on: a
        # thinner card, as when it drops the unit.
        return same_url
    if stored.address_raw and incoming.address_raw:
        return stored.address_raw == incoming.address_raw
    if incoming.address_raw and not stored.address_raw:
        # The user's row never said where it was, and this card does: the
        # page may have been re-let since, so nothing shows it is the same.
        return False
    return same_url


def copy_rank(
    platform: str,
    source_id: str,
    original_url: str,
    metadata: Mapping[str, Any] | None,
    price: int | None,
    published_at: str | None,
    available_on: str | None,
) -> int:
    """How much a copy tells the reader, for choosing which copy to show.

    In order: a rent, a posting date, a move-in date, a direct way to contact
    (an application link or the lister themselves), a page for the flat rather
    than its building, then a fixed order of sites. The row's status and
    whether it is still live are read beside this at query time, because they
    change without the row being rewritten.
    """
    metadata = metadata or {}
    contact = bool(
        str(metadata.get("application_url") or "").startswith("https://abacus.appfolio.com/")
        or (metadata.get("direct_lister") and platform == "Listings Project")
    )
    flags = (
        price is not None,
        bool(_text(published_at)),
        bool(_text(available_on)),
        contact,
        grain(platform, source_id, original_url) == UNIT_GRAIN,
    )
    rank = 0
    for flag in flags:
        rank = rank * 2 + int(flag)
    order = PLATFORM_ORDER.index(platform) if platform in PLATFORM_ORDER else len(PLATFORM_ORDER)
    return rank * 100 + (99 - order)


def group_of(home_key_value: str | None, listing_id: int) -> str:
    """The grouping value in Python, matching ``GROUP_SQL``."""
    return home_key_value if home_key_value else f"row:{int(listing_id)}"
