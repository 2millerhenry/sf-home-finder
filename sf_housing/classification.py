"""Classify a listing's real housing shape from explicit source evidence.

The monitor keeps private rooms, small whole units, and two- or three-bedroom apartments
in separate workflows. Classification is intentionally conservative: an
attractive title is not enough to call a listing a whole unit unless the source
states the bedroom count and does not describe a room inside someone else's home.
"""

from __future__ import annotations

from dataclasses import replace
import re

from .models import ListingCandidate


ROOM = "room"
WHOLE_UNIT = "whole_unit"
UNKNOWN = "unknown"
STUDIO = "studio"
ONE_BEDROOM = "one_bedroom"
TWO_BEDROOM = "two_bedroom"
THREE_BEDROOM = "three_bedroom"
FOUR_BEDROOM = "four_bedroom"

_ROOM_PATTERN = re.compile(
    r"\b(?:private|shared|single|own)\s+(?:(?:primary|master|large|bright|sunny|spacious|furnished)\s+)?(?:room|bedroom)\b|"
    r"\broom\s+only\b|\bavailable\s+(?:private\s+)?room\b|"
    r"\bshared\s+studio\b|\broom\s+not\s+private\b|"
    r"\brooms?\s+(?:for\s+(?:rent|sublet)|available)\b|"
    r"\b(?:looking\s+for|seeking|need(?:ed)?|want(?:ed)?)\s+(?:an?\s+)?(?:[a-z][a-z'-]*\s+){0,3}(?:roommate|housemate)s?\b|"
    r"\b(?:roommate|housemate)\s+(?:wanted|needed|search|opening)\b|\bper[- ]bed\b|"
    r"\bone\s+bedroom\s+(?:for\s+rent|available)\b|"
    r"\bfor\s+(?:an?\s+)?(?:(?:large|bright|sunny|spacious|furnished|private)\s+)?(?:room|bedroom)\b|"
    r"\b(?:2|3|two|three)\s+(?:[a-z][a-z'-]*\s+){0,3}bedrooms?\s+(?:for\s+rent|available)\b|"
    r"\bshared\s*(?:bath(?:room)?|ba)\b|"
    r"\b(?:bathroom|bath|kitchen)\s+(?:(?:will\s+be|is|are)\s+)?shared\b|"
    r"\bshared\s+with\s+(?:\w+\s+){0,3}roommates?\b|"
    r"\b(?:habitaci[oó]n|cuarto)\s+privad[ao]\b|"
    r"\b(?:habitaci[oó]n|cuarto)\s+(?:en\s+alquiler|disponible)\b|"
    r"\bse\s+(?:renta|alquila)\s+(?:un[ao]?\s+)?(?:habitaci[oó]n|cuarto)\b|"
    r"\bse\s+(?:rentan|alquilan)\s+2\s+habitaci(?:o|ó)nes\b|"
    r"\b(?:ba[nñ]o|cocina)\s+compartid[ao]\b",
    re.IGNORECASE,
)
_NON_RESIDENTIAL_PATTERN = re.compile(
    r"\bcommercial\s+space\b|\boffice\b|\bretail\s+space\b|\bworkspace\b|"
    r"\b(?:administrative|business)\s+use\b",
    re.IGNORECASE,
)
_NON_OFFER_PATTERN = re.compile(
    r"\bscam(?:mer)?\s+listings?\b|\bfake\s+(?:ads?|listings?)\b",
    re.IGNORECASE,
)
_STUDIO_PATTERN = re.compile(
    r"\bstudio(?:\s+(?:apartment|unit|flat|condo|rental))?\b|"
    r"\b0\s*(?:bd|br|bed(?:room)?s?)\b|\befficiency\s+(?:apartment|unit)\b",
    re.IGNORECASE,
)
_ONE_BEDROOM_PATTERN = re.compile(
    r"\b1\s*(?:bd|br|bed(?:room)?s?)\b|\bone[- ]bed(?:room)?\b|\b1b[dr]\b",
    re.IGNORECASE,
)
_TWO_BEDROOM_PATTERN = re.compile(
    r"\b2\s*(?:bd|br|bed(?:room)?s?)\b|\btwo[- ]bed(?:room)?s?\b|\b2b[dr]\b|"
    r"\b2\s+habitaci(?:o|ó)nes\b",
    re.IGNORECASE,
)
_THREE_BEDROOM_PATTERN = re.compile(
    r"\b3\s*(?:bd|br|bed(?:room)?s?)\b|\bthree[- ]bed(?:room)?s?\b|\b3b[dr]\b|"
    r"\b3\s+habitaci(?:o|ó)nes\b",
    re.IGNORECASE,
)
# Deliberately stricter than the smaller sizes: listing cards state a whole
# property's bed count ("4 Beds 1.5 Baths") even when a single room in it is
# what is for rent, so a bare "4 beds" is not evidence of renting the whole
# home. A structured numberOfBedrooms of 4, which is what the portal, Zumper and
# Apartment List publish, still classifies normally.
_FOUR_BEDROOM_PATTERN = re.compile(
    r"\b4\s*(?:bd|br|bedrooms?)\b|\bfour[- ]bedrooms?\b|\b4b[dr]\b|"
    r"\b4\s+habitaci(?:o|ó)nes\b",
    re.IGNORECASE,
)
_NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
}
_BUILDING_COUNT = r"(?:\d{1,3}|(?:twenty|thirty|forty)(?:[- ](?:one|two|three|four|five|six|seven|eight|nine))?|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|fifty)"
_BUILDING_UNIT_PATTERNS = (
    re.compile(rf"\b({_BUILDING_COUNT})[- ]unit\s+(?:building|property|complex|walk-up)\b", re.IGNORECASE),
    re.compile(rf"\b(?:building|property|complex)\s+(?:with|of|has)\s+({_BUILDING_COUNT})\s+units?\b", re.IGNORECASE),
    re.compile(rf"\b({_BUILDING_COUNT})\s+units?\s+(?:in|at)\s+(?:the\s+)?(?:building|property|complex)\b", re.IGNORECASE),
)
_STRUCTURED_BUILDING_KEYS = (
    "building_units",
    "number_of_units",
    "total_units",
    "units_in_building",
)
_BUILDING_FORM_UNITS = {"duplex": 2, "triplex": 3, "fourplex": 4}


def _text(listing: ListingCandidate) -> str:
    metadata_text = " ".join(
        str(value)
        for key, value in listing.metadata.items()
        if key not in {"listing_timestamp", "alert_message_id"}
    )
    return " ".join(
        str(value)
        for value in (listing.title, listing.listing_type, listing.summary, metadata_text)
        if value
    )


def _structured_unit_type(listing: ListingCandidate) -> str | None:
    raw_type = str(listing.metadata.get("unit_type") or "").casefold().strip()
    if raw_type in {"studio", "efficiency", "0", "0 bedroom", "0 bedrooms"}:
        return STUDIO
    if raw_type in {"one_bedroom", "one bedroom", "1", "1 bedroom", "1 bedrooms"}:
        return ONE_BEDROOM
    if raw_type in {"two_bedroom", "two bedroom", "2", "2 bedroom", "2 bedrooms"}:
        return TWO_BEDROOM
    if raw_type in {"three_bedroom", "three bedroom", "3", "3 bedroom", "3 bedrooms"}:
        return THREE_BEDROOM
    if raw_type in {"four_bedroom", "four bedroom", "4", "4 bedroom", "4 bedrooms", "4 br"}:
        return FOUR_BEDROOM
    raw_bedrooms = listing.metadata.get("bedrooms")
    try:
        bedrooms = float(raw_bedrooms) if raw_bedrooms not in (None, "") else None
    except (TypeError, ValueError):
        bedrooms = None
    if bedrooms == 0:
        return STUDIO
    if bedrooms == 1:
        return ONE_BEDROOM
    if bedrooms == 2:
        return TWO_BEDROOM
    if bedrooms == 3:
        return THREE_BEDROOM
    if bedrooms == 4:
        return FOUR_BEDROOM
    return None


def unit_type_from_listing(listing: ListingCandidate) -> str | None:
    structured = _structured_unit_type(listing)
    if structured:
        return structured
    text = _text(listing)
    if _STUDIO_PATTERN.search(text):
        return STUDIO
    if _ONE_BEDROOM_PATTERN.search(text):
        return ONE_BEDROOM
    if _TWO_BEDROOM_PATTERN.search(text):
        return TWO_BEDROOM
    if _THREE_BEDROOM_PATTERN.search(text):
        return THREE_BEDROOM
    if _FOUR_BEDROOM_PATTERN.search(text):
        return FOUR_BEDROOM
    return None


def building_units_from_listing(listing: ListingCandidate) -> int | None:
    for key in _STRUCTURED_BUILDING_KEYS:
        value = listing.metadata.get(key)
        if value in (None, ""):
            continue
        match = re.search(r"\d{1,3}", str(value))
        if match:
            units = int(match.group())
            if units > 0:
                return units
    text = _text(listing)
    building_form = re.search(r"\b(duplex|triplex|fourplex)\b", text, re.IGNORECASE)
    if building_form:
        return _BUILDING_FORM_UNITS[building_form.group(1).casefold()]
    for pattern in _BUILDING_UNIT_PATTERNS:
        match = pattern.search(text)
        if match:
            raw_count = match.group(1).casefold().replace("-", " ")
            if raw_count.isdigit():
                units = int(raw_count)
            else:
                parts = raw_count.split()
                units = sum(_NUMBER_WORDS.get(part, 0) for part in parts)
            if units > 0:
                return units
    return None


_BATHROOM_PATTERN = re.compile(
    r"\b(\d+(?:\.\d)?)\s*(?:full\s+)?(?:ba|bath(?:room)?s?)\b",
    re.IGNORECASE,
)


def bathrooms_from_listing(listing: ListingCandidate) -> float | None:
    """Return a stated bathroom count, or None when nothing states one.

    Most sources never publish this. A guess would be worse than a blank, so
    anything unparseable, zero, or implausibly large stays unknown and the
    dashboard shows it as not available.
    """
    raw = listing.metadata.get("bathrooms")
    if isinstance(raw, bool):
        raw = None
    if isinstance(raw, (int, float)):
        return float(raw) if 0 < float(raw) <= 12 else None
    for candidate in (raw, listing.title, listing.listing_type, listing.summary):
        match = _BATHROOM_PATTERN.search(str(candidate or ""))
        if not match:
            continue
        try:
            value = float(match.group(1))
        except ValueError:
            continue
        if 0 < value <= 12:
            return value
    return None


def classify_listing(listing: ListingCandidate) -> ListingCandidate:
    """Return a copy with conservative housing, unit-type, and size facts."""
    text = _text(listing)
    room_evidence_text = re.sub(
        r"\b(?:no|without)\s+(?:roommates?|housemates?)\b",
        "",
        text,
        flags=re.IGNORECASE,
    )
    explicitly_non_residential = bool(_NON_RESIDENTIAL_PATTERN.search(text))
    explicitly_not_an_offer = bool(_NON_OFFER_PATTERN.search(listing.title))
    explicit_room = bool(_ROOM_PATTERN.search(room_evidence_text)) or str(listing.listing_type or "").casefold().startswith(
        ("room/share", "private room", "shared room", "room -")
    )
    unit_type = listing.unit_type or unit_type_from_listing(listing)
    if explicitly_non_residential or explicitly_not_an_offer:
        # "Studio" also describes offices and creative workspaces. Keep those
        # cards out of every housing result mode instead of guessing.
        housing_kind = UNKNOWN
        unit_type = None
    elif explicit_room:
        housing_kind = ROOM
        # A room can be inside a one-bedroom home; do not expose that as a
        # whole-unit match.
        unit_type = None
    elif unit_type in {STUDIO, ONE_BEDROOM, TWO_BEDROOM, THREE_BEDROOM, FOUR_BEDROOM}:
        housing_kind = WHOLE_UNIT
    else:
        # This monitor predates the second search mode, so ambiguous legacy
        # cards remain in the room archive. Whole-unit admission still requires
        # an explicit supported bedroom count.
        housing_kind = listing.housing_kind if listing.housing_kind in {ROOM, WHOLE_UNIT} else ROOM
    building_units = listing.building_units or building_units_from_listing(listing)
    metadata = listing.metadata
    bathrooms = bathrooms_from_listing(listing)
    if bathrooms is not None and metadata.get("bathrooms") != bathrooms:
        metadata = {**metadata, "bathrooms": bathrooms}
    return replace(
        listing,
        metadata=metadata,
        housing_kind=housing_kind,
        unit_type=unit_type,
        building_units=building_units,
    )


def short_unit_type_label(value: str | None) -> str:
    """The same type, short enough to sit beside the bathroom count.

    "1 bedroom - 1 bath" read as though the bedroom were the fact and the
    bath an afterthought, and on a tab that already says which size it is
    showing, the long form is the least useful word in the row. "1 br /
    1 bath" gives the two equal weight, which is how somebody reads them.
    """
    return {STUDIO: "Studio", ONE_BEDROOM: "1 br", TWO_BEDROOM: "2 br", THREE_BEDROOM: "3 br"}.get(
        value or "", unit_type_label(value)
    )


def unit_type_label(value: str | None) -> str:
    if value == STUDIO:
        return "Studio"
    if value == ONE_BEDROOM:
        return "1 bedroom"
    if value == TWO_BEDROOM:
        return "2 bedrooms"
    if value == THREE_BEDROOM:
        return "3 bedrooms"
    return "Unit type not confirmed"
