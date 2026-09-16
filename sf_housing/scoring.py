from __future__ import annotations

import re
from functools import lru_cache
from dataclasses import dataclass
from datetime import date, datetime
from math import ceil
from typing import Callable

from .classification import (
    bathrooms_from_listing,
    FOUR_BEDROOM,
    ONE_BEDROOM,
    STUDIO,
    THREE_BEDROOM,
    TWO_BEDROOM,
    WHOLE_UNIT,
    classify_listing,
    unit_type_label,
)
from .location import (
    OUTSIDE_SF_CITIES,
    declared_outside_sf_area_hint,
    declared_outside_sf_url_hint,
)
from .models import ListingCandidate, ScoreResult
from .deal_profile import DEFAULT_OCCUPANTS, DEFAULT_PER_PERSON, SF_NEIGHBORHOODS, SPLIT_PATHS
from .preferences import Preferences


# Every path that describes an entire home, shared or not. Listed once so a
# home of a size nobody remembered to add here cannot quietly become "entire
# homes are not enabled in your deal".
WHOLE_HOME_PATHS = frozenset({STUDIO, ONE_BEDROOM, *SPLIT_PATHS})


# Below these, a monthly figure is more likely to be something other than a
# month's rent -- a weekly rate, a deposit, a per-person share posted as the
# whole, a typo -- than a cheap home. They are absolute, and deliberately not a
# fraction of what somebody said they could pay.
#
# A fraction is what this used to be: half the deal's ceiling for a whole home,
# 45% for a shared one. Raising a budget to $8,000 therefore made every San
# Francisco studio under $4,000 "unusually low", and a four-bedroom deal --
# whose ceiling is four people's shares added together -- called everything
# under $14,400 suspicious, which is every four-bedroom in the city. Each one
# was capped at 79 against a cut-off of 80, so the whole category missed the
# shortlist by a single point.
#
# Cheap is the thing being searched for. Only implausible is worth a question.
_WHOLE_UNIT_SIZES = {
    STUDIO: 0,
    ONE_BEDROOM: 1,
    TWO_BEDROOM: 2,
    THREE_BEDROOM: 3,
    FOUR_BEDROOM: 4,
}

IMPLAUSIBLE_RENT_FLOOR = {0: 900, 1: 1100, 2: 1400, 3: 1700, 4: 2000}
DEFAULT_RENT_FLOOR = 900


def _bedrooms_of(listing: ListingCandidate) -> int | None:
    """How many bedrooms this home has, as far as anything here knows."""
    stated = listing.metadata.get("bedrooms")
    if isinstance(stated, bool) or not isinstance(stated, (int, float)):
        return _WHOLE_UNIT_SIZES.get(str(listing.unit_type or ""))
    return int(stated)


def _implausible_rent(listing: ListingCandidate, bedrooms: int | None) -> bool:
    """Is this figure too small to be a month's rent for a home this size?

    A home let below market on purpose is exempt: the city's own portal and
    two of the building sources publish rents that are a third of market, and
    those are the finds, not the mistakes.
    """
    if listing.price is None:
        return False
    if listing.metadata.get("below_market_rate"):
        return False
    floor = IMPLAUSIBLE_RENT_FLOOR.get(bedrooms if bedrooms is not None else -1, DEFAULT_RENT_FLOOR)
    return int(listing.price) < floor


PRIVATE_POSITIVE = (
    "private room",
    "own room",
    "own bedroom",
    "private bedroom",
    "single room",
    "room for rent",
    "bedroom for rent",
    "room available",
    "available room",
)
PRIVATE_NEGATIVE = ("shared room", "shared bedroom", "per bed", "per-bed", "roommate in the room")
ROOM_EVIDENCE = (*PRIVATE_POSITIVE, "roommate", "roommates", "housemate", "housemates", "habitación", "cuarto")
FLEXIBLE_TERMS = (
    "month to month",
    "month-to-month",
    "flexible lease",
    "short term",
    "short-term",
    "sublease",
    "sublet",
)
SUBLET_TERMS = ("sublease", "sublet", "lease takeover", "lease transfer")
SUBLET_PLATFORMS = {"Craigslist", "Facebook Marketplace", "Facebook Groups"}
# The shortest sublet worth showing when the reader has not said otherwise.
# Sublet inventory churns, so a default floor is right; imposing it over an
# explicit "three months is fine" is the app deciding the deal for them.
SUBLET_MINIMUM_MONTHS = 6
_DURATION_WORDS = {
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
}
_DURATION_TOKEN = r"(?:\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
_SUBLET_RANGE_PATTERN = re.compile(
    rf"\b(?P<minimum>{_DURATION_TOKEN})\s*(?:-|to|–|—)\s*(?P<maximum>{_DURATION_TOKEN})\s*(?:-|\s)?months?\b",
    re.IGNORECASE,
)
_SUBLET_PLUS_PATTERN = re.compile(
    rf"\b(?P<minimum>{_DURATION_TOKEN})\s*\+\s*months?\b", re.IGNORECASE
)
_SUBLET_EXPLICIT_DURATION_PATTERN = re.compile(
    rf"\b(?:minimum|min\.?|at\s+least|for|of|lasting)\s+(?P<minimum>{_DURATION_TOKEN})\s*(?:-|\s)?months?\b",
    re.IGNORECASE,
)
_SUBLET_LABELLED_DURATION_PATTERN = re.compile(
    rf"\b(?P<minimum>{_DURATION_TOKEN})\s*(?:-|\s)?month\s+(?:sublet|sublease|lease\s+(?:takeover|transfer))\b",
    re.IGNORECASE,
)
_SUBLET_CONTEXT_DURATION_PATTERN = re.compile(
    rf"\b(?:sublet|sublease|lease\s+(?:takeover|transfer))\b[^.!?\n]{{0,80}}?\b(?P<minimum>{_DURATION_TOKEN})\s*(?:-|\s)?months?\b",
    re.IGNORECASE,
)
SUNLIGHT_POSITIVE = ("sunny", "sunlight", "natural light", "bright room", "bright bedroom", "sun-filled")
SUNLIGHT_NEGATIVE = ("no natural light", "windowless", "dark room")
PARK_POSITIVE = ("near park", "park access", "golden gate park", "dolores park", "presidio")
OUTDOOR_POSITIVE = ("yard", "garden", "patio", "deck", "balcony", "roof deck", "backyard")
OUTDOOR_NEGATIVE = ("no yard", "no outdoor space")
LAUNDRY_POSITIVE = ("in-unit laundry", "in unit laundry", "washer dryer", "washer/dryer", "laundry in building", "on-site laundry")
LAUNDRY_NEGATIVE = ("no laundry", "laundromat only")
FURNISHED_POSITIVE = ("furnished", "fully furnished")
FURNISHED_NEGATIVE = ("unfurnished", "not furnished")
PETS_POSITIVE = ("pets allowed", "pet friendly", "pet-friendly", "cats allowed", "dogs allowed")
PETS_NEGATIVE = ("no pets", "pets not allowed")
PARKING_POSITIVE = ("parking included", "garage parking", "off-street parking", "parking space")
PARKING_NEGATIVE = ("no parking", "street parking only")
PROPERTY_ALIASES = {
    "house": ("house",),
    "townhouse": ("townhouse", "town house"),
    "victorian": ("victorian",),
    "shared flat": ("shared flat", "flat", "shared apartment"),
    "apartment": ("apartment",),
}
GENERIC_LOCATIONS = {"sf", "san francisco", "city of san francisco", "san francisco, ca"}
LOCATION_FALSE_SUFFIXES = {
    "avenue", "ave", "boulevard", "blvd", "road", "rd", "street", "st", "way", "valley"
}
STREET_PART = r"(?:\d+(?:st|nd|rd|th)|[a-z][a-z.'-]*(?:\s+[a-z][a-z.'-]*){0,2})\s+(?:st(?:reet)?|ave(?:nue)?|blvd|boulevard|rd|road|dr(?:ive)?|way)"
CROSS_STREET_PATTERN = re.compile(
    rf"\b(?:corner\s+of\s+)?(?P<first>{STREET_PART})\.?\s*(?:&|and|/)\s*(?P<second>{STREET_PART})\.?(?!\w)",
    re.IGNORECASE,
)
WOMEN_ONLY_PATTERN = re.compile(
    r"\b(?:women|woman|female|females|girls?)\s+(?:only|preferred)\b|"
    r"\b(?:only|prefer(?:ably)?)\s+(?:women|woman|female|females|girls?)\b",
    re.IGNORECASE,
)
MEN_ONLY_PATTERN = re.compile(
    r"\b(?:men|male|males|guys?)\s+(?:only|preferred)\b|"
    r"\b(?:only|prefer(?:ably)?)\s+(?:men|male|males|guys?)\b",
    re.IGNORECASE,
)
AVAILABLE_DATE_PATTERN = re.compile(
    r"\bavailable(?:\s+(?:on|from|starting))?\s*:?\s*"
    r"(?P<date>[a-z]{3,9}\.?\s+\d{1,2},?\s+\d{4})\b",
    re.IGNORECASE,
)
SHORT_STAY_RANGE_PATTERN = re.compile(
    r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?\s+"
    r"(?P<start>\d{1,2})\s*(?:-|–|—|to|through)\s*(?P<end>\d{1,2})\b",
    re.IGNORECASE,
)
SHORT_STAY_NUMERIC_RANGE_PATTERN = re.compile(
    r"\b(?P<month>\d{1,2})/(?P<start>\d{1,2})\s*(?:-|–|—|to|through)\s*"
    r"(?:(?P<end_month>\d{1,2})/)?(?P<end>\d{1,2})\b",
    re.IGNORECASE,
)


@dataclass(slots=True)
class Criterion:
    name: str
    value: float
    configured: bool
    known: bool
    positive: str | None
    missing: str
    mismatch: str | None = None
    match_label: str | None = None


def _finalize_score(result: ScoreResult) -> ScoreResult:
    """Separate deal fit from evidence coverage and hard eligibility."""
    weighted_known = 0.0
    weighted_total = 0.0
    unknowns: list[str] = []
    for name, detail in result.details.items():
        if not isinstance(detail, dict) or not isinstance(detail.get("weight"), (int, float)):
            continue
        weight = float(detail["weight"])
        if weight <= 0:
            continue
        weighted_total += weight
        if detail.get("known") is True:
            weighted_known += weight
        elif isinstance(detail.get("missing"), str) and detail["missing"]:
            unknowns.append(str(detail["missing"]))

    hard_constraints = result.details.get("hard_constraints")
    failures: list[str] = []
    verification: list[str] = []
    if isinstance(hard_constraints, list):
        for constraint in hard_constraints:
            if not isinstance(constraint, dict):
                continue
            status = constraint.get("status")
            reason = str(constraint.get("reason") or "").strip()
            if status == "fail" and reason:
                failures.append(reason)
            elif status == "unknown" and reason:
                verification.append(reason)
    eligibility = "ineligible" if failures else "needs_verification" if verification else "eligible"
    return ScoreResult(
        score=result.score,
        reasons=result.reasons,
        concern=result.concern,
        details=result.details,
        confidence=max(0, min(100, round(weighted_known / weighted_total * 100))) if weighted_total else 0,
        eligibility=eligibility,
        unknowns=list(dict.fromkeys(unknowns + verification)),
        eligibility_reasons=failures or verification,
    )


def _normal(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").casefold()).strip()


@lru_cache(maxsize=4096)
def _word_pattern(phrase: str) -> re.Pattern[str] | None:
    """One compiled whole-word matcher per phrase.

    The phrase lists are fixed vocabularies -- amenities, sublet terms,
    neighborhood names -- so the same few hundred patterns were being rebuilt
    and re-escaped for every listing on every rescore.
    """
    normalized = _normal(phrase)
    if not normalized:
        return None
    return re.compile(rf"(?<!\w){re.escape(normalized)}(?!\w)")


def _contains(text: str, phrases: tuple[str, ...] | list[str]) -> bool:
    for phrase in phrases:
        pattern = _word_pattern(phrase)
        if pattern is not None and pattern.search(text):
            return True
    return False


def _duration_months(value: str) -> int | None:
    normalized = value.casefold().strip()
    if normalized.isdigit():
        return int(normalized)
    return _DURATION_WORDS.get(normalized)


def _sublet_minimum(preferences: Preferences) -> int:
    """The shortest sublet this reader would take.

    Their own stated minimum wins; the built-in floor only applies when they
    have not set one. Someone who said three months is fine was still having
    four-month sublets refused by a number they never chose.
    """
    try:
        stated = preferences.section("lease").get("min_months")
    except Exception:
        return SUBLET_MINIMUM_MONTHS
    if stated is None:
        return SUBLET_MINIMUM_MONTHS
    try:
        value = int(stated)
    except (TypeError, ValueError):
        return SUBLET_MINIMUM_MONTHS
    return value if value > 0 else SUBLET_MINIMUM_MONTHS


def _targeted_sublet_term(listing: ListingCandidate, text: str) -> tuple[bool, int | None]:
    """Return whether this source advertises a sublet and its stated minimum term.

    This is deliberately source-scoped. A normal six-month lease should keep
    following the user's existing lease preferences; only Facebook and
    Craigslist sublet inventory needs the extra six-month admission rule.
    """
    is_sublet = listing.platform in SUBLET_PLATFORMS and _contains(text, SUBLET_TERMS)
    if not is_sublet:
        return False, None
    for pattern in (
        _SUBLET_RANGE_PATTERN,
        _SUBLET_PLUS_PATTERN,
        _SUBLET_EXPLICIT_DURATION_PATTERN,
        _SUBLET_LABELLED_DURATION_PATTERN,
        _SUBLET_CONTEXT_DURATION_PATTERN,
    ):
        match = pattern.search(text)
        if match:
            months = _duration_months(str(match.group("minimum")))
            if months is not None:
                return True, months
    return True, None


@lru_cache(maxsize=2048)
def _location_pattern(phrase: str) -> re.Pattern[str] | None:
    """Match an area name however a source happens to punctuate it.

    Sources write "haight ashbury"; the product calls it "Haight-Ashbury". A
    literal match meant that name, and "St. Francis Wood", could never match a
    real listing, so anyone who chose either got nothing from Craigslist. The
    words have to match in order; whatever sits between them does not.
    """
    normalized = _normal(phrase)
    words = [word for word in re.split(r"[^a-z0-9]+", normalized) if word]
    if not words:
        return None
    return re.compile(rf"(?<!\w){r'[^a-z0-9]*'.join(map(re.escape, words))}(?!\w)")


def _contains_location(text: str, phrase: str) -> bool:
    """Match a neighborhood phrase without treating street/region names as it."""
    pattern = _location_pattern(phrase)
    if pattern is None:
        return False
    for match in pattern.finditer(text):
        remainder = text[match.end():].lstrip(" ,-/")
        next_word = re.match(r"([a-z]+)", remainder)
        if next_word and next_word.group(1) in LOCATION_FALSE_SUFFIXES:
            continue
        return True
    return False


def _text(listing: ListingCandidate) -> str:
    metadata_text = " ".join(str(value) for value in listing.metadata.values())
    return _normal(" ".join(filter(None, [listing.title, listing.summary, listing.listing_type, metadata_text])))


def _declared_cross_street_hint(listing: ListingCandidate) -> str | None:
    """Return only a cross street explicitly stated in a public listing's text."""
    raw_text = " ".join(filter(None, [listing.title, listing.summary, listing.listing_type]))
    match = CROSS_STREET_PATTERN.search(raw_text)
    if not match:
        return None

    def display(value: str) -> str:
        titled = re.sub(r"\s+", " ", value).strip().rstrip(".").title()
        return re.sub(r"(\d+)(St|Nd|Rd|Th)\b", lambda item: item.group(1) + item.group(2).lower(), titled)

    return f"{display(match.group('first'))} & {display(match.group('second'))}"


def _household_restriction(text: str) -> str | None:
    """Surface explicit household eligibility wording without guessing anyone's gender."""
    if WOMEN_ONLY_PATTERN.search(text):
        return "Women-only or women-preferred household; confirm eligibility."
    if MEN_ONLY_PATTERN.search(text):
        return "Men-only or men-preferred household; confirm eligibility."
    return None


def _monthly_price(listing: ListingCandidate, preferences: Preferences) -> Criterion:
    budget = preferences.section("budget")
    low, high = budget.get("min_monthly"), budget.get("max_monthly")
    ideal = budget.get("ideal_monthly")
    sweet_low, sweet_high = budget.get("sweet_spot_min"), budget.get("sweet_spot_max")
    flexible_margin = min(0.15, max(0.0, float(budget.get("flexible_margin_percent", 0.05))))
    configured = low is not None or high is not None
    if listing.price is None:
        return Criterion("price", 0.5, configured, False, None, "Unknown: monthly price is not stated.")
    if not configured:
        return Criterion("price", 0.5, False, True, None, "")
    in_range = (low is None or listing.price >= int(low)) and (high is None or listing.price <= int(high))
    in_flexible_range = (
        (low is None or listing.price >= round(int(low) * (1 - flexible_margin)))
        and (high is None or listing.price <= round(int(high) * (1 + flexible_margin)))
    )
    in_sweet_spot = (
        sweet_low is not None
        and sweet_high is not None
        and int(sweet_low) <= listing.price <= int(sweet_high)
    )
    bounds = f"${int(low):,}–${int(high):,}" if low is not None and high is not None else (
        f"at most ${int(high):,}" if high is not None else f"at least ${int(low):,}"
    )
    if in_sweet_spot:
        price_value = 1.0
        if ideal is None:
            # Nobody named an ideal, so there is no sweet spot to praise; the
            # home is simply inside the budget.
            positive = f"${listing.price:,}/month is within your {bounds} budget."
        elif listing.price == int(ideal):
            positive = f"${listing.price:,}/month is exactly your ideal price."
        elif low is None or int(sweet_low) <= 1:
            # No floor was stated, so the band has no meaningful bottom and
            # naming one reads as "$1-$2,500". What the reader cares about is
            # that it lands at or under the number they called ideal.
            positive = f"${listing.price:,}/month is at or below your ${int(sweet_high):,} ideal."
        else:
            positive = (
                f"${listing.price:,}/month is in your "
                f"${int(sweet_low):,}–${int(sweet_high):,} sweet spot."
            )
    elif in_range:
        price_value = 0.72
        positive = f"${listing.price:,}/month is within your wider {bounds} budget."
    elif in_flexible_range:
        price_value = 0.5
        positive = None
    else:
        price_value = 0.0
        positive = None
    return Criterion(
        "price",
        price_value,
        True,
        True,
        positive,
        "",
        (
            None
            if in_range
            else f"Price ${listing.price:,}/month is slightly outside your {bounds} budget."
            if in_flexible_range
            else f"Price ${listing.price:,}/month is outside your {bounds} budget."
        ),
    )


def _neighborhood(listing: ListingCandidate, preferences: Preferences) -> Criterion:
    ideal = preferences.list_value("ideal_neighborhoods")
    preferred = preferences.list_value("preferred_neighborhoods")
    acceptable = preferences.list_value("acceptable_neighborhoods")
    configured = bool(ideal or preferred or acceptable)
    outside_area = declared_outside_sf_url_hint(listing.original_url) or declared_outside_sf_area_hint(
        " ".join(filter(None, [listing.title, listing.summary]))
    )
    if outside_area:
        # Always configured, whatever the deal says about areas. This is a San
        # Francisco housing search, so a home the listing itself places in
        # another city is out of scope even for someone who listed no
        # neighbourhoods -- and "anywhere in San Francisco" is exactly that
        # someone. Marking it unconfigured dropped the criterion before it could
        # fail anything, which put Palo Alto, Union City and Daly City rooms on
        # the shortlist at 81.
        return Criterion(
            "neighborhood",
            0.0,
            True,
            True,
            None,
            "",
            f"{outside_area} is outside San Francisco.",
        )
    declared_detail_area = listing.metadata.get("detail_declared_neighborhood")
    location = _normal(
        str(declared_detail_area)
        if declared_detail_area not in (None, "")
        else listing.neighborhood
    )
    if preferences.deal_profile.anywhere_in_sf:
        if not location:
            return Criterion(
                "neighborhood", 0.5, True, False, None,
                "Unknown: confirm that the home is inside San Francisco.",
            )
        if not _recognisably_san_francisco(location):
            # Say what is actually known rather than asserting a city the
            # listing never claimed.
            return Criterion(
                "neighborhood", 0.5, True, False, None,
                f"Unknown: \u201c{str(declared_detail_area or listing.neighborhood)}\u201d "
                "is not a recognized San Francisco area; confirm the home is in the city.",
            )
        return Criterion(
            "neighborhood", 1.0, True, True,
            "The source places this home in San Francisco.", "",
            match_label=str(declared_detail_area or listing.neighborhood),
        )
    # Search-card locations are sometimes only "San Francisco", while a title or
    # summary contains the actual neighborhood. Use the fuller listing text for a
    # positive area match, but never treat a generic city label as an out-of-area
    # neighborhood.
    aliases = preferences.mapping_value("neighborhood_aliases")

    def matches(text: str, areas: list[str]) -> str | None:
        for area in areas:
            phrases = [area, *aliases.get(area, [])]
            if any(_contains_location(text, phrase) for phrase in phrases):
                return area
        return None

    # A source-provided location is stronger evidence than prose. In particular,
    # "15 minutes to Duboce" must not turn a SOMA home into a Duboce listing.
    # A source can honestly say "Near Golden Gate Park" without knowing its
    # actual neighborhood. Keep that useful display hint neutral for area score.
    generic_or_landmark_location = not location or location in GENERIC_LOCATIONS or location.startswith("near ")
    if not generic_or_landmark_location:
        detail = location
    else:
        detail = _normal(" ".join(filter(None, [listing.neighborhood, listing.title, listing.summary])))

    ideal_match = matches(detail, ideal)
    preferred_match = matches(detail, preferred)
    acceptable_match = matches(detail, acceptable)
    if ideal_match:
        return Criterion(
            "neighborhood", 1.0, True, True, f"{ideal_match} is one of your two ideal areas.", "",
            match_label=ideal_match,
        )
    if preferred_match:
        return Criterion(
            "neighborhood", 0.8, True, True, f"{preferred_match} is a strongly preferred neighborhood.", "",
            match_label=preferred_match,
        )
    if acceptable_match:
        return Criterion(
            "neighborhood", 0.55, True, True, f"{acceptable_match} is a secondary neighborhood fit.", "",
            match_label=acceptable_match,
        )
    if generic_or_landmark_location:
        return Criterion("neighborhood", 0.5, configured, False, None, "Unknown: neighborhood is not stated.")
    if not configured:
        return Criterion("neighborhood", 0.5, False, True, None, "")
    display_location = str(declared_detail_area or listing.neighborhood)
    return Criterion(
        "neighborhood", 0.0, True, True, None, "", f"{display_location} is outside your target neighborhoods."
    )


def _private_room(text: str, preferences: Preferences) -> Criterion:
    wanted = preferences.data.get("private_room")
    configured = isinstance(wanted, bool)
    positive = _contains(text, PRIVATE_POSITIVE)
    negative = _contains(text, PRIVATE_NEGATIVE)
    if not positive and not negative:
        return Criterion("private_room", 0.5, configured, False, None, "Unknown: private-room status is not clear.")
    matches = positive and not negative if wanted is not False else negative
    return Criterion(
        "private_room",
        1.0 if matches else 0.0,
        configured,
        True,
        "The listing explicitly offers a private room." if matches and wanted is not False else None,
        "",
        None if matches else "The listing appears to offer a shared room rather than a private room.",
    )


def _has_room_evidence(text: str) -> bool:
    """Separate an unknown room detail from a card that looks like a whole home."""
    return _contains(text, ROOM_EVIDENCE)


def _property_type(text: str, listing: ListingCandidate, preferences: Preferences) -> Criterion:
    wanted = [_normal(item) for item in preferences.list_value("property_types")]
    configured = bool(wanted)
    found: str | None = None
    raw_property = _normal(str(listing.metadata.get("property_type", "")))
    # Structured source metadata wins. In prose, an explicit apartment is more
    # reliable than nearby/style words such as "Victorian lobby" or
    # "townhouse-style apartment".
    search_order = list(PROPERTY_ALIASES)
    property_text = raw_property or text
    if not raw_property and _contains(text, PROPERTY_ALIASES["apartment"]) and not _contains(
        text, PROPERTY_ALIASES["shared flat"]
    ):
        search_order = ["apartment", *[item for item in search_order if item != "apartment"]]
    for property_type in search_order:
        if _contains(property_text, PROPERTY_ALIASES[property_type]):
            found = property_type
            break
    # A private bedroom advertised inside an ordinary shared apartment is the
    # same real-world housing shape users often call a "nice flat." Only an explicit
    # apartment complex remains a dealbreaker. This avoids penalizing sources
    # that label every flat as an apartment in their structured metadata.
    if (
        found == "apartment"
        and "shared flat" in wanted
        and _contains(text, [*PRIVATE_POSITIVE, "roommate", "roommates", "housemate", "housemates"])
        and not _contains(text, ["apartment complex"])
    ):
        found = "shared flat"
    if found is None:
        return Criterion("property_type", 0.5, configured, False, None, "Unknown: property type is not stated.")
    if not configured:
        return Criterion("property_type", 0.5, False, True, None, "")
    matches = found in wanted
    return Criterion(
        "property_type",
        1.0 if matches else 0.2,
        True,
        True,
        f"The {found} format matches your preferred home types." if matches else None,
        "",
        None if matches else f"The listing appears to be a {found}, outside your preferred home types.",
        match_label=found,
    )


def _lease(listing: ListingCandidate, text: str, preferences: Preferences) -> Criterion:
    lease = preferences.section("lease")
    low, high, wants_flexible = lease.get("min_months"), lease.get("max_months"), lease.get("flexible")
    ideal_low, ideal_high = lease.get("ideal_min_months"), lease.get("ideal_max_months")
    configured = low is not None or high is not None or isinstance(wants_flexible, bool)
    is_sublet, sublet_months = _targeted_sublet_term(listing, text)
    if is_sublet:
        if sublet_months is None:
            # A sublet that never states its length is an unknown, not a
            # refusal. Failing it outright broke the one rule the rest of this
            # file keeps -- a missing fact lowers confidence and gets named for
            # checking, it does not decide the answer -- and it threw away homes
            # whose term nobody had asked about yet.
            return Criterion(
                "lease",
                0.5,
                configured,
                False,
                None,
                f"Unknown: this sublet does not state its length; confirm it runs at least "
                f"{_sublet_minimum(preferences)} months.",
            )
        if sublet_months < _sublet_minimum(preferences):
            return Criterion(
                "lease",
                0.0,
                configured,
                True,
                None,
                "",
                (
                    f"This sublet offers {sublet_months} months, below the "
                    f"{_sublet_minimum(preferences)}-month minimum."
                ),
            )
        in_ideal = (
            ideal_low is not None
            and ideal_high is not None
            and int(ideal_low) <= sublet_months <= int(ideal_high)
        )
        in_maximum = (low is None or sublet_months >= int(low)) and (
            high is None or sublet_months <= int(high)
        )
        if not in_maximum:
            return Criterion(
                "lease",
                0.0,
                configured,
                True,
                None,
                "",
                "The stated sublet term does not match your lease window.",
            )
        return Criterion(
            "lease",
            1.0 if in_ideal else 0.7,
            configured,
            True,
            f"Verified {sublet_months}-month sublet meets your six-month minimum.",
            "",
        )
    is_flexible = _contains(text, FLEXIBLE_TERMS)
    range_match = re.search(r"\b(\d{1,2})\s*(?:-|–|to)\s*(\d{1,2})\s*months?\b", text)
    month_match = re.search(r"\b(\d{1,2})[ -]months?(?: lease)?\b", text)
    months = 12 if _contains(text, ["one-year lease", "one year lease"]) else int(month_match.group(1)) if month_match else None
    if not is_flexible and months is None:
        return Criterion("lease", 0.5, configured, False, None, "Unknown: lease length and flexibility are not stated.")
    matches = False
    description = ""
    if is_flexible and wants_flexible is True:
        matches, description = True, "The listing mentions a sublease, short-term, or month-to-month arrangement."
    elif range_match:
        stated_low, stated_high = int(range_match.group(1)), int(range_match.group(2))
        ideal_overlap = (
            ideal_low is not None
            and ideal_high is not None
            and stated_high >= int(ideal_low)
            and stated_low <= int(ideal_high)
        )
        within_maximum = (low is None or stated_low >= int(low)) and (high is None or stated_high <= int(high))
        matches = ideal_overlap or within_maximum
        description = f"The stated {stated_low}–{stated_high} month range fits your lease window."
    elif months is not None:
        in_ideal = (
            ideal_low is not None
            and ideal_high is not None
            and int(ideal_low) <= months <= int(ideal_high)
        )
        in_maximum = (low is None or months >= int(low)) and (high is None or months <= int(high))
        matches = in_ideal or in_maximum
        description = f"The stated {months}-month term fits your lease range."
    value = 1.0 if is_flexible and matches else 1.0 if matches and (
        range_match is not None or (months is not None and ideal_low is not None and ideal_high is not None and int(ideal_low) <= months <= int(ideal_high))
    ) else 0.7 if matches else 0.0
    return Criterion(
        "lease", value, configured, True, description if matches else None, "",
        None if matches else "The stated lease terms do not match your preferred flexibility or length.",
    )


def _profile_date(value: object) -> date | None:
    """Read a simple ISO date from the editable deal profile."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def _available_on(listing: ListingCandidate) -> date | None:
    """Read only an explicitly published move-in date; never infer one."""
    raw_values: list[str] = []
    structured = listing.metadata.get("available_on")
    if structured is not None:
        raw_values.append(str(structured))
    raw_values.extend(filter(None, [listing.summary, listing.title]))
    for value in raw_values:
        match = AVAILABLE_DATE_PATTERN.search(value)
        candidate = match.group("date") if match else value.strip()
        candidate = re.sub(r"(?<=[A-Za-z])\.", "", candidate)
        candidate = re.sub(r"\s+", " ", candidate)
        for pattern in ("%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y"):
            try:
                return datetime.strptime(candidate, pattern).date()
            except ValueError:
                continue
    return None


def _availability(listing: ListingCandidate, preferences: Preferences) -> Criterion:
    """Favor listings that explicitly begin before the user's near-term cutoff.

    An absent move-in date is genuinely unknown, not a late opening.  It keeps
    neutral credit and remains eligible for the shortlist, as requested.
    """
    timing = preferences.section("availability")
    preferred_by = _profile_date(timing.get("preferred_by"))
    latest_by = _profile_date(timing.get("latest_by"))
    configured = preferred_by is not None or latest_by is not None
    available_on = _available_on(listing)
    if available_on is None:
        return Criterion(
            "availability",
            0.5,
            configured,
            False,
            None,
            "Unknown: the available-from date is not stated.",
        )
    if not configured:
        return Criterion("availability", 0.5, False, True, None, "")
    display = available_on.strftime("%b %-d, %Y")
    if preferred_by is None or available_on <= preferred_by:
        return Criterion(
            "availability",
            1.0,
            True,
            True,
            f"Available {display}, inside your August move-in window.",
            "",
        )
    if latest_by is None or available_on <= latest_by:
        return Criterion(
            "availability",
            0.55,
            True,
            True,
            f"Available {display}, still within your near-term window.",
            "",
        )
    cutoff = latest_by or preferred_by
    cutoff_display = cutoff.strftime("%b %-d, %Y") if cutoff else "your near-term cutoff"
    return Criterion(
        "availability",
        0.0,
        True,
        True,
        None,
        "",
        f"Available {display}, after your {cutoff_display} near-term cutoff.",
    )


def _recognisably_san_francisco(location: str) -> bool:
    """Is this location string actually evidence of San Francisco?

    "Anywhere in San Francisco" still means in San Francisco. Treating any
    non-empty location as proof let Oakland, Berkeley, San Jose and Discovery
    Bay score as full matches, because a bare city name carries none of the
    grammar the outside-SF check needs.
    """
    text = _normal(location)
    if not text:
        return False
    # South San Francisco and Daly City are their own cities; the first even
    # contains the string this check would otherwise accept.
    if any(re.search(rf"(?<!\w){re.escape(city)}(?!\w)", text) for city in OUTSIDE_SF_CITIES):
        return False
    if "san francisco" in text or re.search(r"(?<!\w)s\.?f\.?(?!\w)", text):
        return True
    return any(_contains_location(text, area) for area in SF_NEIGHBORHOODS)


def _home_facts(text: str, criteria: list[Criterion]) -> dict[str, object]:
    """Present concrete room facts without turning missing facts into guesses."""
    by_name = {item.name: item for item in criteria}
    private_room = by_name.get("private_room")
    property_type = by_name.get("property_type")
    room_label = (
        "Private room"
        if private_room and private_room.known and private_room.value == 1
        else "Shared room"
        if private_room and private_room.known and private_room.value == 0
        else "Room status not stated"
    )
    property_label = property_type.match_label.title() if property_type and property_type.match_label else ""
    secondary: list[str] = []
    bathroom = re.search(r"\b\d+\s+(private|shared)\s+bathrooms?\b", text)
    if bathroom:
        secondary.append(f"{bathroom.group(1).title()} bath")
    beds = re.search(r"\b([2-9]|1\d)\s*(?:bed|bedroom)s?\b", text)
    if beds:
        secondary.append(f"{beds.group(1)}-bed home")
    return {
        "primary": f"{room_label} · {property_label}" if property_label else room_label,
        "secondary": secondary,
    }


def _household(text: str, listing: ListingCandidate, preferences: Preferences) -> Criterion:
    household = preferences.section("household")
    low, high = household.get("min_people"), household.get("max_people")
    ideal = household.get("ideal_people")
    configured = low is not None or high is not None
    people: int | None = None
    number_words = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
                    "seven": 7, "eight": 8, "nine": 9, "ten": 10}
    number_pattern = r"(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten)"

    def count(match: re.Match[str]) -> int:
        value = match.group(1)
        return int(value) if value.isdigit() else number_words[value]

    total_match = re.search(rf"\b{number_pattern}\s+(?:people|person household)\b", text)
    household_of_match = re.search(rf"\bhousehold of\s+{number_pattern}\b", text)
    roommate_match = re.search(
        rf"\b{number_pattern}\s+(?:(?:other|current)\s+)?(?:roommates?|housemates?)\b", text
    )
    if total_match:
        people = count(total_match)
    elif household_of_match:
        people = count(household_of_match)
    elif roommate_match:
        # Listings normally describe the people already living there; include
        # the searcher to compare against the configured total household size.
        people = count(roommate_match) + 1
    else:
        # SpareRoom exposes room count rather than resident count. It is only a
        # fallback estimate when the listing text gives no household evidence.
        raw_rooms = listing.metadata.get("rooms_in_property")
        if raw_rooms is not None and str(raw_rooms).isdigit():
            people = int(raw_rooms)
    if people is None:
        return Criterion("household", 0.5, configured, False, None, "Unknown: household size is not stated.")
    if not configured:
        return Criterion("household", 0.5, False, True, None, "")
    matches = (low is None or people >= int(low)) and (high is None or people <= int(high))
    value = 1.0 if matches and ideal is not None and people == int(ideal) else 0.8 if matches else 0.0
    description = (
        f"The reported household size ({people}) matches your ideal."
        if matches and ideal is not None and people == int(ideal)
        else f"The reported household/property size ({people}) is within your five-person maximum."
    )
    return Criterion(
        "household", value, True, True,
        description if matches else None, "",
        None if matches else f"The reported household/property size ({people}) is outside your range.",
    )


def _feature(
    name: str,
    text: str,
    preferences: Preferences,
    positives: tuple[str, ...],
    negatives: tuple[str, ...],
    label: str,
) -> Criterion:
    wanted = preferences.section("features").get(name)
    configured = wanted is True
    positive, negative = _contains(text, positives), _contains(text, negatives)
    if not positive and not negative:
        return Criterion(name, 0.5, configured, False, None, f"Unknown: {label} is not described.")
    matches = positive and not negative
    return Criterion(
        name, 1.0 if matches else 0.0, configured, True,
        f"The listing mentions {label}." if matches else None, "",
        None if matches else f"The listing explicitly says it lacks {label}.",
    )


def _lifestyle(text: str, preferences: Preferences) -> Criterion:
    keywords = preferences.list_value("lifestyle_keywords")
    configured = bool(keywords)
    hits = [keyword for keyword in keywords if _contains(text, [keyword])]
    if not configured:
        return Criterion("lifestyle", 0.5, False, False, None, "")
    if not hits:
        return Criterion("lifestyle", 0.5, True, False, None, "Unknown: household lifestyle fit is not described.")
    shown = ", ".join(hits[:3])
    value = min(1.0, 0.5 + 0.25 * len(hits))
    return Criterion("lifestyle", value, True, True, f"Lifestyle signals match: {shown}.", "")


def _score_whole_unit(listing: ListingCandidate, preferences: Preferences) -> ScoreResult:
    """Score only the constraints that define the separate whole-unit search."""
    # Every shared path reads its own section, rather than a branch per path.
    # Written that way, four-bedroom was left out of the branch and out of the
    # set above it, so it fell through to the whole-unit settings -- whose
    # allowed types are studio and one-bedroom -- and every four-bedroom home
    # was refused as "outside this deal path" no matter what the deal enabled.
    # A fifth path added to SPLIT_PATHS now works without touching this.
    is_split_unit = listing.unit_type in SPLIT_PATHS
    if is_split_unit:
        path = str(listing.unit_type)
        settings = preferences.section(path)
        occupants = int(settings.get("occupants", DEFAULT_OCCUPANTS[path]))
        max_per_person = int(settings.get("max_per_person", DEFAULT_PER_PERSON[path]))
    else:
        settings = preferences.section("whole_unit")
        occupants = 1
        max_per_person = None
    max_monthly = occupants * max_per_person if max_per_person is not None else int(settings.get("max_monthly", 3000))
    canonical_budget = preferences.deal_profile.budgets.get(str(listing.unit_type or ""))
    if canonical_budget is not None:
        max_monthly = canonical_budget.total_maximum
    # None is a real answer: "any size". Kept apart from a number rather than
    # standing in for one, so nothing prints a ceiling the reader never chose
    # and nothing is refused for a size they said they did not mind.
    raw_building_limit = settings.get("max_building_units", 50)
    max_building_units = None if raw_building_limit in (None, "") else int(raw_building_limit)
    building_size_matters = max_building_units is not None
    allowed_types = {listing.unit_type} if is_split_unit else {
        str(value).strip().casefold() for value in settings.get("unit_types", [STUDIO, ONE_BEDROOM]) if str(value).strip()
    }
    neighborhood = _neighborhood(listing, preferences)
    type_label = unit_type_label(listing.unit_type)
    type_matches = listing.unit_type in allowed_types
    price_known = listing.price is not None
    price_matches = price_known and int(listing.price) <= max_monthly
    building_known = listing.building_units is not None
    building_matches = not building_size_matters or (
        building_known and int(listing.building_units) <= max_building_units
    )
    verified_inactive = listing.metadata.get("verified_inactive") is True
    is_sublet, sublet_months = _targeted_sublet_term(listing, _text(listing))
    sublet_term_eligible = (
        not is_sublet
        or (sublet_months is not None and sublet_months >= _sublet_minimum(preferences))
    )
    craigslist_detail_checked = listing.metadata.get("craigslist_detail_checked") is True
    craigslist_content_rejected = listing.metadata.get("craigslist_content_rejected") is True
    craigslist_public_detail = "craigslist.org/" in listing.original_url.casefold()
    unverified_craigslist_unit = (
        listing.platform == "Craigslist" and craigslist_public_detail and not craigslist_detail_checked
    )
    implausibly_low_craigslist_unit = (
        listing.platform == "Craigslist"
        and craigslist_public_detail
        and listing.price is not None
        and int(listing.price) < 1000
    )
    per_person_monthly = ceil(int(listing.price) / occupants) if is_split_unit and listing.price is not None else None
    unusually_low = _implausible_rent(listing, _bedrooms_of(listing))
    stay_text = " ".join(filter(None, [listing.title, listing.summary]))
    stay_match = SHORT_STAY_RANGE_PATTERN.search(stay_text)
    numeric_stay_match = SHORT_STAY_NUMERIC_RANGE_PATTERN.search(stay_text)
    short_stay_days = None
    if stay_match:
        start_day = int(stay_match.group("start"))
        end_day = int(stay_match.group("end"))
        if end_day >= start_day:
            short_stay_days = end_day - start_day + 1
    elif numeric_stay_match:
        start_month = int(numeric_stay_match.group("month"))
        end_month = int(numeric_stay_match.group("end_month") or start_month)
        start_day = int(numeric_stay_match.group("start"))
        end_day = int(numeric_stay_match.group("end"))
        if end_month == start_month and end_day >= start_day:
            short_stay_days = end_day - start_day + 1

    text = _text(listing)
    amenity_criteria = [
        _feature("sunlight", text, preferences, SUNLIGHT_POSITIVE, SUNLIGHT_NEGATIVE, "good natural light"),
        _feature("outdoor_space", text, preferences, OUTDOOR_POSITIVE, OUTDOOR_NEGATIVE, "outdoor space"),
        _feature("laundry", text, preferences, LAUNDRY_POSITIVE, LAUNDRY_NEGATIVE, "laundry"),
        _feature("furnished", text, preferences, FURNISHED_POSITIVE, FURNISHED_NEGATIVE, "furnished space"),
        _feature("pets", text, preferences, PETS_POSITIVE, PETS_NEGATIVE, "pet-friendly terms"),
        _feature("parking", text, preferences, PARKING_POSITIVE, PARKING_NEGATIVE, "parking"),
    ]
    preference_weights = preferences.weights
    active_amenities = [
        item for item in amenity_criteria
        if item.configured and preference_weights.get(item.name, 0) > 0
    ]
    values = {
        "neighborhood": neighborhood.value,
        "unit_type": 1.0 if type_matches else 0.5 if listing.unit_type is None else 0.0,
        "price": 1.0 if price_matches else 0.5 if not price_known else 0.0,
        # An unknown size is half a fact when a limit was set. Where none
        # was, building_matches is already true and this is simply full marks:
        # a question the reader declined is not a gap in the listing.
        "building_size": 1.0 if building_matches else 0.5 if not building_known else 0.0,
    }
    weights = {"neighborhood": 35.0, "unit_type": 25.0, "price": 25.0, "building_size": 15.0}
    for item in active_amenities:
        values[item.name] = item.value
        weights[item.name] = preference_weights[item.name]
    raw_score = sum(values[name] * weight for name, weight in weights.items()) / sum(weights.values()) * 100
    score = max(0, min(100, round(raw_score)))
    neighborhood_priority = (
        "dream"
        if neighborhood.value >= 1.0
        else "strong"
        if neighborhood.value >= 0.8
        else "secondary"
        if neighborhood.value > 0
        else None
    )

    # These four facts define admission to this view. Unknown building size is
    # reviewable, but unknown area, rent, or unit type cannot be recommended.
    if neighborhood.known and neighborhood.value == 0:
        score = min(score, 45)
    if not type_matches:
        if listing.unit_type is not None:
            score = min(score, 39)
    if price_known and not price_matches:
        score = min(score, 49)
    if building_known and not building_matches:
        score = min(score, 49)
    # A secondary area remains available as a fallback, but it must not look
    # equally strong as a dream or strong neighborhood simply because the card
    # happens to disclose its building size.
    if neighborhood_priority == "secondary":
        score = min(score, 74)
    if verified_inactive:
        score = min(score, 49)
    if is_sublet and not sublet_term_eligible:
        score = min(score, 49 if sublet_months is not None else 59)
    if craigslist_content_rejected:
        score = min(score, 49)
    if unverified_craigslist_unit:
        score = min(score, 59)
    # Nothing caps a home for being cheap. Both of these used to: an
    # implausibly cheap Craigslist unit was held at 49 and any low rent at 79,
    # which against a cut-off of 80 meant a whole category missed the
    # shortlist by one point. A rent that looks too good is still said out
    # loud in the concern below and still shows on the card -- it is a note to
    # read, not a reason to bury the home. Whoever is searching can open the
    # listing and decide in ten seconds; this cannot.
    if short_stay_days is not None and short_stay_days < 28:
        score = min(score, 49)

    reasons: list[tuple[float, str]] = []
    if neighborhood.positive:
        reasons.append((weights["neighborhood"] * neighborhood.value, neighborhood.positive))
    if type_matches:
        type_reason = (
            f"The listing is explicitly identified as a {type_label.casefold()} apartment."
            if is_split_unit
            else f"The listing is explicitly identified as a {type_label.casefold()}."
        )
        reasons.append((weights["unit_type"], type_reason))
    if price_matches and listing.price is not None:
        price_reason = (
            f"${listing.price:,} total is ${per_person_monthly:,}/person for {occupants} people."
            if is_split_unit and per_person_monthly is not None
            else f"${listing.price:,}/month is within your ${max_monthly:,} cap."
        )
        reasons.append((weights["price"], price_reason))
    if building_size_matters and building_matches and listing.building_units is not None:
        reasons.append(
            (
                weights["building_size"],
                f"The listing states {listing.building_units} units, within your {max_building_units}-unit maximum.",
            )
        )
    shown_reasons = [reason for _, reason in sorted(reasons, reverse=True)[:3]]

    if verified_inactive:
        concern = str(
            listing.metadata.get("verification_concern")
            or "Verified inactive: the source is not currently accepting applications."
        ).strip()[:500]
    elif craigslist_content_rejected:
        concern = str(
            listing.metadata.get("verification_concern")
            or "Rejected: the Craigslist detail page contains conflicting source evidence."
        ).strip()[:500]
    elif not type_matches:
        concern = (
            "The listing is not explicitly identified as an entire 2- or 3-bedroom apartment."
            if is_split_unit
            else "The listing is not explicitly identified as a studio or one-bedroom."
        )
    elif not price_known:
        concern = "Unknown: monthly price is not stated."
    elif not price_matches:
        concern = (
            f"Price ${listing.price:,}/month is above your ${max_monthly:,} total cap (${max_per_person:,} per person)."
            if is_split_unit and max_per_person is not None
            else f"Price ${listing.price:,}/month is above your ${max_monthly:,} cap."
        )
    elif neighborhood.known and neighborhood.value == 0:
        concern = neighborhood.mismatch or "The listing is outside your target neighborhoods."
    elif not neighborhood.known:
        concern = neighborhood.missing or "Unknown: neighborhood is not stated."
    elif short_stay_days is not None and short_stay_days < 28:
        concern = (
            f"The listing is only available for {short_stay_days} days, so its price is not a full monthly rent."
        )
    elif is_sublet and sublet_months is None:
        concern = (
            f"This Facebook or Craigslist sublet needs an explicit {_sublet_minimum(preferences)}-month "
            "term before it can be recommended."
        )
    elif is_sublet and not sublet_term_eligible:
        concern = (
            f"This sublet offers {sublet_months} months, below the "
            f"{_sublet_minimum(preferences)}-month minimum."
        )
    elif implausibly_low_craigslist_unit:
        concern = (
            f"Not recommended: ${listing.price:,}/month is below the $1,000 Craigslist whole-unit "
            "safety floor; verify it manually before considering it."
        )
    elif unverified_craigslist_unit:
        concern = "Not recommended yet: this Craigslist whole unit still needs a clean detail-page check."
    elif unusually_low:
        concern = (
            f"Verify: ${listing.price:,}/month is unusually low for an entire SF "
            f"{type_label.casefold()}; "
            "confirm it is the total rent, not a room price or deposit."
        )
    elif building_size_matters and building_known and not building_matches:
        concern = (
            f"The listing states {listing.building_units} units, above your "
            f"{max_building_units}-unit maximum."
        )
    elif building_size_matters and not building_known:
        concern = (
            f"Unknown: building size is not stated; verify it has {max_building_units} units or fewer."
        )
    elif str(listing.metadata.get("verification_concern") or "").strip():
        # A browser or human verification pass can surface a concrete property
        # concern that the listing copy itself does not contain. Keep it visible
        # without letting it override harder admission failures above.
        concern = str(listing.metadata["verification_concern"]).strip()[:500]
    else:
        concern = "No major concern found in the available details."

    building_label = (
        f"{listing.building_units}-unit building"
        if listing.building_units is not None
        else "Building size not stated"
    )
    # Most sources never state a bathroom count. Saying so is more useful than
    # leaving a gap the reader has to interpret.
    bathrooms = bathrooms_from_listing(listing)
    if bathrooms is None:
        bath_label = "Baths n/a"
    elif float(bathrooms).is_integer():
        bath_label = f"{int(bathrooms)} bath" if bathrooms == 1 else f"{int(bathrooms)} baths"
    else:
        bath_label = f"{bathrooms:g} baths"
    secondary_facts = [bath_label, building_label]
    if is_sublet and sublet_months is not None and sublet_term_eligible:
        secondary_facts.insert(0, f"{sublet_months}-month sublet")
    if per_person_monthly is not None:
        secondary_facts.insert(0, f"${per_person_monthly:,}/person for {occupants}")
    details = {
        "neighborhood": {
            "value": neighborhood.value,
            "weight": weights["neighborhood"],
            "known": neighborhood.known,
            "missing": neighborhood.missing,
            "main_results_eligible": neighborhood.known and neighborhood.value > 0,
            "priority": neighborhood_priority,
        },
        "unit_type": {
            "value": values["unit_type"],
            "weight": weights["unit_type"],
            "known": listing.unit_type is not None,
            "missing": "Unknown: the unit type is not stated.",
            "match_label": type_label if type_matches else None,
            "main_results_eligible": type_matches,
        },
        "price": {
            "value": values["price"],
            "weight": weights["price"],
            "known": price_known,
            "missing": "Unknown: monthly price is not stated.",
            # The line it was judged against, and how far past it the rent
            # fell. A home over the line is ruled out, and ruled-out homes are
            # scored much the same whatever else is right about them: on a real
            # board a $5,430 home and a $12,400 one against a $3,000 deal both
            # scored 49. So the score cannot say whether this missed by a tenth
            # or by four times, and that is the entire question when deciding
            # whether it nearly matched.
            "maximum": max_monthly,
            "over_by": max(0, int(listing.price) - max_monthly) if price_known else None,
            "main_results_eligible": price_matches,
        },
        "building_size": {
            "value": values["building_size"],
            "weight": weights["building_size"],
            "known": building_known,
            "missing": "Unknown: building size is not stated.",
            "max_units": max_building_units,
            "main_results_eligible": not building_known or building_matches,
        },
        "housing_kind": WHOLE_UNIT,
        "search_mode": "split_unit" if is_split_unit else "whole_unit",
        "per_person_monthly": per_person_monthly,
        "occupants": occupants if is_split_unit else None,
        "home_facts": {"primary": type_label, "secondary": secondary_facts},
        "sublease": {
            "is_sublease": is_sublet,
            "minimum_months": sublet_months,
            "main_results_eligible": sublet_term_eligible,
        },
        "hard_constraints": [],
    }
    for item in active_amenities:
        details[item.name] = {
            "value": item.value,
            "weight": weights[item.name],
            "known": item.known,
            "missing": item.missing,
        }
    constraints = details["hard_constraints"]
    if neighborhood.known and neighborhood.value == 0:
        constraints.append({"status": "fail", "check": "area", "reason": neighborhood.mismatch or "Outside your selected areas."})
    elif not neighborhood.known:
        constraints.append({"status": "unknown", "check": "area", "reason": neighborhood.missing})
    if listing.unit_type is None:
        constraints.append({"status": "unknown", "check": "home type", "reason": "Confirm the unit type."})
    elif not type_matches:
        constraints.append({"status": "fail", "check": "home type", "reason": "The home type is outside this deal path."})
    if not price_known:
        constraints.append({"status": "unknown", "check": "price", "reason": "Confirm the monthly price."})
    elif not price_matches:
        constraints.append({"status": "fail", "check": "price", "reason": "The monthly price exceeds this path's maximum."})
    if not building_size_matters:
        pass
    elif not building_known:
        constraints.append({"status": "unknown", "check": "building size", "reason": f"Confirm the building has {max_building_units} units or fewer."})
    elif not building_matches:
        constraints.append({"status": "fail", "check": "building size", "reason": "The stated building size exceeds your maximum."})
    if verified_inactive:
        constraints.append({"status": "fail", "check": "listing page", "reason": "The source verified this listing is inactive."})
    if craigslist_content_rejected:
        constraints.append({"status": "fail", "check": "listing page", "reason": "The Craigslist detail page conflicts with the result card."})
    elif unverified_craigslist_unit:
        constraints.append({"status": "unknown", "check": "listing page", "reason": "Confirm the Craigslist detail page before relying on this home."})
    if short_stay_days is not None and short_stay_days < 28:
        constraints.append({"status": "fail", "check": "stay length", "reason": "The stated stay is shorter than one month."})
    if is_sublet and sublet_months is None:
        constraints.append({"status": "unknown", "check": "sublet term", "reason": f"Confirm a sublet term of at least {_sublet_minimum(preferences)} months."})
    elif is_sublet and not sublet_term_eligible:
        constraints.append({"status": "fail", "check": "sublet term", "reason": f"The sublet is shorter than {_sublet_minimum(preferences)} months."})
    if neighborhood.match_label:
        details["neighborhood"]["match_label"] = neighborhood.match_label
    return _finalize_score(ScoreResult(score, shown_reasons, concern, details))


def _enforce_enabled_path(
    listing: ListingCandidate, preferences: Preferences, result: ScoreResult
) -> ScoreResult:
    enabled = set(preferences.deal_profile.enabled_paths)
    failure: str | None = None
    verification: str | None = None
    if listing.housing_kind == WHOLE_UNIT:
        if listing.unit_type is None:
            if not enabled.intersection(WHOLE_HOME_PATHS):
                failure = "Entire homes are not enabled in your deal."
            else:
                verification = "Confirm which enabled whole-home path this listing belongs to."
        elif listing.unit_type not in enabled:
            failure = f"{unit_type_label(listing.unit_type)} is not enabled in your deal."
    elif listing.housing_kind != "unknown" and "private_room" not in enabled:
        failure = "Private rooms are not enabled in your deal."
    elif listing.housing_kind == "unknown":
        verification = "Confirm whether this is one of your enabled home types."

    constraints = result.details.setdefault("hard_constraints", [])
    if failure:
        constraints.append({"status": "fail", "check": "home type", "reason": failure})
        result = ScoreResult(
            min(result.score, 20), result.reasons, failure, result.details
        )
    elif verification:
        constraints.append({"status": "unknown", "check": "home type", "reason": verification})
    result.details["deal_path"] = {
        "unit_type": listing.unit_type,
        "housing_kind": listing.housing_kind,
        "enabled": sorted(enabled),
    }
    return _finalize_score(result)


def score_listing(listing: ListingCandidate, preferences: Preferences) -> ScoreResult:
    listing = classify_listing(listing)
    if listing.housing_kind == WHOLE_UNIT:
        return _enforce_enabled_path(listing, preferences, _score_whole_unit(listing, preferences))
    text = _text(listing)
    criteria = [
        _monthly_price(listing, preferences),
        _neighborhood(listing, preferences),
        _private_room(text, preferences),
        _property_type(text, listing, preferences),
        _lease(listing, text, preferences),
        _availability(listing, preferences),
        _household(text, listing, preferences),
        _feature("sunlight", text, preferences, SUNLIGHT_POSITIVE, SUNLIGHT_NEGATIVE, "good natural light"),
        _feature("park_access", text, preferences, PARK_POSITIVE, (), "nearby park access"),
        _feature("outdoor_space", text, preferences, OUTDOOR_POSITIVE, OUTDOOR_NEGATIVE, "outdoor space"),
        _feature("laundry", text, preferences, LAUNDRY_POSITIVE, LAUNDRY_NEGATIVE, "laundry"),
        _feature("furnished", text, preferences, FURNISHED_POSITIVE, FURNISHED_NEGATIVE, "furnished space"),
        _feature("pets", text, preferences, PETS_POSITIVE, PETS_NEGATIVE, "pet-friendly terms"),
        _feature("parking", text, preferences, PARKING_POSITIVE, PARKING_NEGATIVE, "parking"),
        _lifestyle(text, preferences),
    ]
    weights = preferences.weights
    active = [item for item in criteria if item.configured and weights.get(item.name, 0) > 0]
    denominator = sum(weights[item.name] for item in active)
    if denominator <= 0:
        return ScoreResult(0, [], "Preferences are not configured yet.", {})

    raw_score = sum(item.value * weights[item.name] for item in active) / denominator * 100
    dealbreaker_hits = [item for item in preferences.list_value("dealbreakers") if _contains(text, [item])]
    household_restriction = _household_restriction(text)
    location_hint = _declared_cross_street_hint(listing)
    is_sublet, sublet_months = _targeted_sublet_term(listing, text)
    score = max(0, min(100, round(raw_score - (20 if dealbreaker_hits else 0))))
    by_name = {item.name: item for item in active}
    neighborhood = by_name.get("neighborhood")

    # These are explicit boundaries in the housing deal, not soft preferences.
    # Keep violations for history, but do not let unrelated amenities lift them
    # into the recommended view.
    if neighborhood and neighborhood.known and neighborhood.value == 0:
        score = min(score, 45)
    private_room = by_name.get("private_room")
    if private_room and private_room.known and private_room.value == 0:
        score = min(score, 39)
    # Unknown information is never treated as a negative. But an entire-unit
    # card with no mention of a room, bedroom, or roommates is not yet enough
    # evidence to recommend as a private-room match. Keep it searchable in the
    # archive with its unknown concern instead of promoting it on location alone.
    room_evidence = _has_room_evidence(text)
    price = by_name.get("price")
    # The configured price range is a shortlist boundary. A small allowance
    # is still useful for surfacing near misses in history, but must not let a
    # listing above the chosen cap enter the recommended results.
    if price and price.known and price.value < 0.72:
        score = min(score, 54)
    lease = by_name.get("lease")
    if lease and lease.known and lease.value == 0:
        score = min(score, 54)
    # A Facebook or Craigslist sublet belongs in the normal room results only
    # when the card explicitly commits to at least six months.  The ordinary
    # lease preference is a soft score; this is the requested admission rule.
    if is_sublet and (sublet_months is None or sublet_months < _sublet_minimum(preferences)):
        score = min(score, 49 if sublet_months is not None else 59)
    availability = by_name.get("availability")
    if availability and availability.known and availability.value == 0:
        score = min(score, 59)
    household = by_name.get("household")
    if household and household.known and household.value == 0:
        score = min(score, 54)
    if dealbreaker_hits:
        score = min(score, 49)
    # A room the source has taken down is not a room you can rent. Whole units
    # have refused this since the beginning; rooms are the larger half of what
    # Craigslist publishes and were never asked, so a deleted post kept its full
    # score and stayed on the shortlist indefinitely.
    room_verified_inactive = listing.metadata.get("verified_inactive") is True
    if room_verified_inactive:
        score = min(score, 49)

    ranked_reasons = sorted(
        (item for item in active if item.positive),
        key=lambda item: (item.value * weights[item.name], weights[item.name]),
        reverse=True,
    )
    reasons = [item.positive for item in ranked_reasons[:3] if item.positive]

    if room_verified_inactive:
        concern = str(
            listing.metadata.get("verification_concern")
            or "Verified inactive: the source has removed this post."
        ).strip()[:500]
    elif dealbreaker_hits:
        concern = f"Possible dealbreaker mentioned: {', '.join(dealbreaker_hits[:2])}."
    elif household_restriction:
        concern = household_restriction
    else:
        mismatches = sorted(
            (item for item in active if item.mismatch), key=lambda item: weights[item.name], reverse=True
        )
        unknowns = sorted(
            (item for item in active if not item.known and item.missing),
            key=lambda item: weights[item.name],
            reverse=True,
        )
        concern = (
            mismatches[0].mismatch
            if mismatches
            else unknowns[0].missing
            if unknowns
            else "No major concern found in the available details."
        )

    details = {
        item.name: {
            "value": item.value,
            "weight": weights[item.name],
            "known": item.known,
            "missing": item.missing,
        }
        for item in active
    }
    for item in active:
        if item.match_label:
            details[item.name]["match_label"] = item.match_label
    available_on = _available_on(listing)
    if available_on and "availability" in details:
        details["availability"]["available_on"] = available_on.isoformat()
    details["home_facts"] = _home_facts(text, criteria)
    details["sublease"] = {
        "is_sublease": is_sublet,
        "minimum_months": sublet_months,
        "main_results_eligible": (
            not is_sublet
            or (sublet_months is not None and sublet_months >= _sublet_minimum(preferences))
        ),
    }
    if household_restriction:
        details["household_restriction"] = household_restriction
    if location_hint:
        details["location_hint"] = location_hint
    if neighborhood:
        details["neighborhood"]["main_results_eligible"] = not (
            neighborhood.known and neighborhood.value == 0
        )
    if private_room:
        details["private_room"]["main_results_eligible"] = not (
            not private_room.known and not room_evidence
        )
    hard_constraints: list[dict[str, str]] = []
    if neighborhood:
        if neighborhood.known and neighborhood.value == 0:
            hard_constraints.append({"status": "fail", "check": "area", "reason": neighborhood.mismatch or "Outside your selected areas."})
        elif not neighborhood.known:
            hard_constraints.append({"status": "unknown", "check": "area", "reason": neighborhood.missing})
    if private_room:
        if private_room.known and private_room.value == 0:
            hard_constraints.append({"status": "fail", "check": "private room", "reason": private_room.mismatch or "This is not a private room."})
        elif not private_room.known:
            hard_constraints.append({"status": "unknown", "check": "private room", "reason": private_room.missing})
    for criterion, label, check in (
        (price, "price", "price"),
        (lease, "lease", "lease"),
        (availability, "move-in timing", "move-in"),
        (household, "household size", "household"),
    ):
        if criterion and criterion.known and criterion.value == 0:
            hard_constraints.append({"status": "fail", "check": check, "reason": criterion.mismatch or f"The {label} conflicts with your deal."})
        elif criterion and not criterion.known:
            hard_constraints.append({"status": "unknown", "check": check, "reason": criterion.missing})
    if room_verified_inactive:
        hard_constraints.append({
            "status": "fail",
            "check": "listing page",
            "reason": "The source verified this listing is inactive.",
        })
    elif (
        listing.platform == "Craigslist"
        and "craigslist.org/" in listing.original_url.casefold()
        and listing.metadata.get("craigslist_detail_checked") is not True
    ):
        # Only the search card was read. Whole units have said so since the
        # beginning; rooms never did, so a card the app had only skimmed could
        # sit on the shortlist at 90 with "100% evidence" beside it, having
        # never been opened and with no posting date known. The budget for
        # detail pages is finite by design, so this is a normal state -- it just
        # has to be stated rather than counted as certainty.
        hard_constraints.append({
            "status": "unknown",
            "check": "listing page",
            "reason": "Only the search card was read; open the listing to confirm the details.",
        })
    if dealbreaker_hits:
        hard_constraints.append({"status": "fail", "check": "dealbreaker", "reason": f"Possible dealbreaker: {', '.join(dealbreaker_hits[:2])}."})
    if is_sublet and sublet_months is None:
        hard_constraints.append({"status": "unknown", "check": "sublet term", "reason": f"Confirm a sublet term of at least {_sublet_minimum(preferences)} months."})
    elif is_sublet and sublet_months < _sublet_minimum(preferences):
        hard_constraints.append({"status": "fail", "check": "sublet term", "reason": f"The sublet is shorter than {_sublet_minimum(preferences)} months."})
    details["hard_constraints"] = hard_constraints
    return _enforce_enabled_path(
        listing, preferences, _finalize_score(ScoreResult(score, reasons, concern, details))
    )
