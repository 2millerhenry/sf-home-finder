from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ListingCandidate:
    platform: str
    source_id: str
    title: str
    original_url: str
    price: int | None = None
    neighborhood: str | None = None
    listing_type: str | None = None
    summary: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    housing_kind: str = "unknown"
    unit_type: str | None = None
    building_units: int | None = None


@dataclass(slots=True)
class ScoreResult:
    score: int
    reasons: list[str]
    concern: str
    details: dict[str, Any]
    confidence: int = 0
    eligibility: str = "eligible"
    unknowns: list[str] = field(default_factory=list)
    eligibility_reasons: list[str] = field(default_factory=list)


# Every hard constraint carries the short name of what is unresolved as well as
# the sentence explaining it. "Needs verification" told a renter that something
# was unknown but never what, which is the one thing they could have acted on.
#
# Ordered by how much knowing the answer should change what they do next: a
# listing page that may be gone is worth a minute right now, a building size the
# source never publishes is something they can park. A name missing from this
# tuple keeps its place at the end rather than vanishing, so adding a constraint
# can never silently hide it.
#
# "rent" is back on this list, and it is worth saying what it now is and is
# not. It once meant "this rent is suspiciously low, confirm it before
# counting the home", and it held cheap homes out of the shortlist -- and a
# cheap home is the thing this app is looking for, so it was removed. It now
# means "this rent is below anything a home this size lets for and nobody has
# opened the listing to check", it is raised on the way to the screen rather
# than by the scorer, and it clears nothing: the home is on the shortlist
# whatever it says. High on the list because it is what would most change
# what the reader does in the next minute.
CHECK_ORDER = (
    "dealbreaker",
    "listing page",
    "rent",
    "confirmation",
    "stay length",
    "home type",
    "private room",
    "price",
    "area",
    "sublet term",
    "lease",
    "move-in",
    "household",
    "building size",
)
_CHECK_RANK = {name: index for index, name in enumerate(CHECK_ORDER)}

# The same fact reaches the reader twice: once as a criterion that could not be
# measured ("building size is not stated") and once as the constraint it fails
# ("confirm the building has 50 units or fewer"). Naming the criterion here lets
# the second one be recognised as the same question and asked only once. Kept
# beside CHECK_ORDER so the two vocabularies cannot drift apart unnoticed.
CRITERION_CHECKS = {
    "availability": "move-in",
    "building_size": "building size",
    "household": "household",
    "lease": "lease",
    "neighborhood": "area",
    "price": "price",
    "private_room": "private room",
    "unit_type": "home type",
}


def unmeasured_criteria(score_details: Any, answered: set[str]) -> list[str]:
    """Criteria that could not be measured and are not already being asked about.

    These belong with the coverage figure rather than with the questions: they
    lower confidence, but none of them is the reason a listing was held back.
    """
    if not isinstance(score_details, dict):
        return []
    unmeasured: list[str] = []
    for name, detail in score_details.items():
        if not isinstance(detail, dict) or detail.get("known") is not False:
            continue
        missing = str(detail.get("missing") or "").strip()
        if not missing or missing in unmeasured:
            continue
        if CRITERION_CHECKS.get(name, name) in answered or missing in answered:
            continue
        unmeasured.append(missing)
    return unmeasured


# The same names, said the way a person would say them, for the line that
# explains an empty shortlist. Kept beside CHECK_ORDER so one vocabulary covers
# both the badge on a row and the reason a home never reached one.
CHECK_EXCLUSION_PHRASES = {
    "area": "outside your areas",
    "price": "over your budget",
    "rent": "priced in a way that needs checking",
    "home type": "a home type you have not turned on",
    "private room": "not a private room",
    "building size": "in a building larger than your limit",
    "lease": "a lease length that does not fit",
    "move-in": "available outside your move-in dates",
    "household": "sharing with more people than you allow",
    "sublet term": "a sublet shorter than you accept",
    "stay length": "a stay shorter than a month",
    "listing page": "a listing page that could not be confirmed",
    "confirmation": "not confirmed as still listed recently",
    "dealbreaker": "carrying something you listed as a dealbreaker",
}


def ordered_checks(constraints: Any, status: str) -> list[dict[str, str]]:
    """The constraints of one status, most decision-relevant first.

    Ordering happens on the way to the screen rather than at scoring time, so a
    stored result is never rewritten and nothing that reads it changes meaning.
    A row scored before checks were named has no short name; it keeps its
    sentence, so an older listing degrades to what it showed before instead of
    disappearing from the page.
    """
    if not isinstance(constraints, list):
        return []
    named: list[dict[str, str]] = []
    seen: set[str] = set()
    for constraint in constraints:
        if not isinstance(constraint, dict) or constraint.get("status") != status:
            continue
        reason = str(constraint.get("reason") or "").strip()
        if not reason:
            continue
        check = str(constraint.get("check") or "").strip()
        # Two constraints can raise the same question -- an unstated building
        # size is both an unknown criterion and an unmet limit -- and a renter
        # should be asked once.
        key = check or reason
        if key in seen:
            continue
        seen.add(key)
        named.append({"check": check, "reason": reason})
    return sorted(named, key=lambda item: _CHECK_RANK.get(item["check"], len(CHECK_ORDER)))


@dataclass(slots=True)
class ScanOutcome:
    run_id: int
    status: str
    listings_seen: int = 0
    listings_added: int = 0
    listings_updated: int = 0
    sources_failed: int = 0
    already_running: bool = False
