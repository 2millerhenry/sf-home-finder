"""A rent to rank by when the listing states none -- and to do nothing else with.

40 of the homes on a real board's shortlist published no rent. Scoring
reads an unstated rent as neither in nor over budget, so those homes sat among
the rest on everything else they said, whatever their area and size usually
cost: a Tenderloin studio and a Pacific Heights three-bedroom were ranked as if
rent were no object for either.

The board already knows what homes like them cost. This takes the median rent
of the homes it holds of the same size in the same neighbourhood, and the
dashboard ranks an unpriced home as if that were its rent. Three rules, and the
tests hold each one:

* It moves the order of the shortlist and nothing else. Whether a home is
  eligible, whether it is on the shortlist at all, every count, the rent the
  page prints ("Unknown") and the CSV are all computed from what the listing
  said, never from this. An estimate that let a home past a budget would be the
  app recommending a flat because it guessed the flat was affordable.
* A median, not a mean: Financial District one-bedrooms include a $349 row
  that drags any average; the median does not notice it. Of an even sample the
  higher of the two middle values, so a tie is broken towards the dearer rent.
* Only from enough evidence. A neighbourhood and size with fewer than
  ``MINIMUM_SAMPLE`` homes falls back to the size's median across the city,
  and a size with fewer than that has no estimate at all -- the home keeps the
  place its own score gives it.

Each home is counted once however many sites list it, at the highest rent any
of them quotes, and only homes seen in the last ``WINDOW_DAYS`` count: rents
move, and a board kept for a year should not price this month's flats at last
year's rents. Neighbourhoods are compared by their canonical spelling, since
the same area stored as "Tenderloin" and "tenderloin" split one sample in two.

Measured on a real board of 8,680 rows (7,524 priced homes, each home
counted once): 147 neighbourhood and size groups have eight or more priced
homes, covering 5,560 of them -- Nob Hill one-bedroom $3,895, Tenderloin studio
$2,095 -- and the size medians across the city run from $1,500 for a room to
$9,795 for four bedrooms.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from typing import Iterable

from .location import canonical_neighborhood

# Fewer homes than this and a median says more about the few than about the area.
MINIMUM_SAMPLE = 8
# How far back a seen home still counts towards what rents are now.
WINDOW_DAYS = 60


def _median(values: list[int]) -> int:
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


def _area(value: str | None) -> str:
    return (canonical_neighborhood(value) or "").casefold()


@dataclass(frozen=True)
class RentTable:
    """Median rents by (neighbourhood, size) and by size alone."""

    by_area: dict[tuple[str, str], int]
    by_size: dict[str, int]

    @classmethod
    def from_observations(cls, observations: Iterable[tuple[str | None, str | None, int]]) -> "RentTable":
        area_rents: dict[tuple[str, str], list[int]] = defaultdict(list)
        size_rents: dict[str, list[int]] = defaultdict(list)
        for neighborhood, size, price in observations:
            if not size or price is None or int(price) <= 0:
                continue
            size_rents[size].append(int(price))
            area = _area(neighborhood)
            if area:
                area_rents[(area, size)].append(int(price))
        return cls(
            {key: _median(rents) for key, rents in area_rents.items() if len(rents) >= MINIMUM_SAMPLE},
            {key: _median(rents) for key, rents in size_rents.items() if len(rents) >= MINIMUM_SAMPLE},
        )

    def estimate(self, neighborhood: str | None, size: str | None) -> int | None:
        if not size:
            return None
        return self.by_area.get((_area(neighborhood), size), self.by_size.get(size))


def size_of(housing_kind: str | None, unit_type: str | None) -> str | None:
    """What a home is, for comparing rents: a room, or a whole home of a size."""
    if housing_kind == "room":
        return "room"
    if housing_kind == "whole_unit" and unit_type:
        return str(unit_type)
    return None


def ranking_order(listings: list[dict], copies: dict, table: RentTable, score) -> tuple[list[dict], bool]:
    """The listings in shortlist order, unpriced homes ranked by an estimated rent.

    ``copies`` maps a listing id to the stored listings of every live copy of
    its home (itself included), and ``score`` scores a listing against the
    deal in hand. An unpriced home is placed where its least favourable copy
    would be at the estimated rent -- the same rule a stated rent gets, so a
    site putting the home outside the deal still holds it down. Returns the
    reordered listings and whether any estimate was used. The listings
    themselves are never changed.
    """
    ranks: list[tuple[int, int]] = []
    estimated = False
    for position, item in enumerate(listings):
        rank = int(item.get("home_score", item["score"]))
        home = copies.get(int(item["id"])) or []
        if item.get("price") is None and item.get("home_price") is None and home:
            shown = home[0]
            guess = table.estimate(shown.neighborhood, size_of(shown.housing_kind, shown.unit_type))
            if guess is not None:
                rank = min(int(score(replace(copy, price=guess)).score) for copy in home)
                estimated = True
        ranks.append((-rank, position))
    order = sorted(range(len(listings)), key=lambda index: ranks[index])
    return [listings[index] for index in order], estimated
