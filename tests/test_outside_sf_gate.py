"""The shortcut in front of the outside-SF check must answer exactly as the check does.

``declared_outside_sf_area_hint`` ran 212 patterns over every listing to find
that three in a thousand name a city outside San Francisco -- 22 of 8,680 on the
author's own board -- and it was 45% of a scoring pass. A single alternation now
answers the common case first.

That shortcut is the dangerous kind of optimisation: when it is wrong it returns
a plausible answer rather than raising, and the answer decides whether somebody
is shown a home in Oakland. A first attempt at it drew its terms from
``_UNAMBIGUOUS_OUTSIDE_SF_CITIES``, which is the narrower set the grammar
patterns need, and so stopped recognising every city only ``_CITY_WITH_STATE``
knows -- "richmond, ca" among them, which then read as San Francisco's own
Richmond district.

So it is not tested by example. It is tested by equivalence: the gated function
and the ungated one must agree on everything, including the strings that made
the first attempt wrong.
"""

from __future__ import annotations

import itertools

import pytest

from sf_housing.location import (
    _CITY_BY_GRAMMAR,
    _CITY_WITH_STATE,
    _MENTIONS_ANY_CITY,
    _WHITESPACE,
    declared_outside_sf_area_hint,
)


def _ungated(text: str | None) -> str | None:
    """What the function did before the shortcut was put in front of it."""
    normalized = _WHITESPACE.sub(" ", (text or "").casefold()).strip()
    if not normalized:
        return None
    for pattern, label in _CITY_WITH_STATE:
        if pattern.search(normalized):
            return label
    for prefixed, at_start, label in _CITY_BY_GRAMMAR:
        if prefixed.search(normalized) or at_start.match(normalized):
            return label
    return None


def _every_city() -> list[str]:
    labels = [label for _, label in _CITY_WITH_STATE]
    labels += [label for _, _, label in _CITY_BY_GRAMMAR]
    return sorted({label.removesuffix(" (outside SF)") for label in labels})


def test_the_gate_knows_every_city_either_loop_can_name() -> None:
    """The gate is a superset of both pattern sets or it is a bug.

    Derived from the same labels it guards, so a city added to either set joins
    the gate in the same edit. This asserts that relationship directly rather
    than trusting it.
    """
    for city in _every_city():
        assert _MENTIONS_ANY_CITY.search(city.casefold()), (
            f"{city!r} can be returned by the full check but the gate does not know it"
        )


@pytest.mark.parametrize("city", _every_city())
def test_every_city_survives_the_shortcut_in_the_shapes_that_name_it(city: str) -> None:
    """Each city, in the sentence shapes a real listing writes it in."""
    lower = city.casefold()
    for text in (
        city,
        f"{city}, CA",
        f"{city} CA 94501",
        f"Lovely 2BR in {city}, CA with parking",
        f"in {lower}, ca",
        f"{lower}",
        f"  {city.upper()} , CA  ",
    ):
        assert declared_outside_sf_area_hint(text) == _ungated(text), text


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "   ",
        "\n\t ",
        "Sunny studio in NOPA",
        "Inner Richmond 1BR with parking",
        "Outer Richmond studio",
        "Richmond District, San Francisco",
        "Richmond, CA 2BR",
        "apartment in richmond ca",
        "richmond/seacliff",
        "South San Francisco, CA",
        "san francisco, ca",
        "Daly City, CA",
        "Mission District",
        "Potrero Hill, San Francisco",
        "Studio near Berkeley BART but in SF",
        "$2,800 — 1BR — Hayes Valley",
        "OAKLAND, CA",
        "a" * 5000,
        "é ñ ü — unicode and punctuation, CA",
    ],
)
def test_the_shortcut_agrees_on_the_strings_that_catch_shortcuts(text: str | None) -> None:
    """SF's own Richmond and South San Francisco are where a careless gate goes wrong."""
    assert declared_outside_sf_area_hint(text) == _ungated(text)


def test_inner_richmond_is_not_the_city_of_richmond() -> None:
    """The specific misclassification the first attempt at this shortcut caused."""
    assert declared_outside_sf_area_hint("Spacious Inner Richmond 1BR w/ Parking") is None
    assert declared_outside_sf_area_hint("Richmond, CA 2BR") == "Richmond (outside SF)"


def test_the_shortcut_agrees_across_a_generated_corpus() -> None:
    """Every city crossed with the fragments that surround one in a real title."""
    prefixes = ["", "Bright 2BR in ", "$3,100 — ", "Lovely home, ", "near ", "NOT in "]
    suffixes = ["", ", CA", " CA", ", ca 94501", " — available now", " district", "/seacliff"]
    disagreements = []
    for city, prefix, suffix in itertools.product(_every_city(), prefixes, suffixes):
        text = f"{prefix}{city}{suffix}"
        if declared_outside_sf_area_hint(text) != _ungated(text):
            disagreements.append(text)
    assert not disagreements, f"{len(disagreements)} disagreements, first: {disagreements[:5]}"


def test_the_shortcut_actually_short_circuits() -> None:
    """If it never skipped the loops it would be pure overhead, correct but pointless."""
    assert _MENTIONS_ANY_CITY.search("sunny studio in nopa with laundry") is None
    assert _MENTIONS_ANY_CITY.search("two bedroom, potrero hill, parking") is None
    assert _MENTIONS_ANY_CITY.search("oakland, ca") is not None


def test_the_gate_is_not_vacuous() -> None:
    """An empty alternation matches everything and would silently disable the guard."""
    assert _MENTIONS_ANY_CITY.pattern, "the gate pattern is empty"
    assert len(_every_city()) > 50, "the city list collapsed; the gate would be meaningless"
