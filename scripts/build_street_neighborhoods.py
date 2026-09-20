#!/usr/bin/env python3
"""Build the shipped street-to-neighbourhood table from San Francisco open data.

The city housing portal publishes a street address but never a neighbourhood,
and most SF ZIP codes straddle two areas, so a ZIP cannot answer the question.
This turns DataSF's address dataset into a small table the app can consult
offline: no network call during a scan, nothing to rate-limit, and no third
party told which homes a user is looking at.

The unit is the hundred block, not the street. A street-wide address range is
destroyed by a single mis-geocoded address -- one stray record put Russian Hill
on 0-3040 Larkin St, which swallows the whole of Nob Hill and the Tenderloin.
Grouping by block instead makes 94% of blocks unanimous, and a block that is
genuinely split is recorded as contested so the resolver refuses it.

Two neighbourhood layers are read, finer first. DataSF's 41 "analysis
neighborhoods" are too coarse for this product's vocabulary: "Sunset/Parkside"
is three of the product's areas and "West of Twin Peaks" is four, so a block
inside one of them could only be guessed at, and abstaining instead left a
third of the city's blocks unanswered. The city's "SF Find Neighborhoods" layer
draws 117 areas and names Inner Sunset, Outer Sunset and Parkside separately,
which is the vocabulary the product already uses. A block is answered from the
fine layer when that layer is dominant and names an area the product knows, and
only otherwise from the coarse one, so nothing the coarse layer used to answer
is lost.

Both layers are San Francisco open data and free to redistribute: the address
dataset is released under the Open Data Commons PDDL, and SF Find Neighborhoods
is published as a public-domain work of government.

Run it to refresh the data; the output is committed so the app never needs it.

    python scripts/build_street_neighborhoods.py
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "sf_housing" / "data" / "sf_streets.json"
DATASET = "https://data.sfgov.org/resource/ramy-di5m.json"

# The address dataset carries a column holding, for each address, which SF Find
# neighbourhood its point falls in; this is the copy of that layer Socrata
# computes the column against, and it is what turns the column's feature ids
# back into names. The same boundaries are published for people to look at as
# gfpk-269f, "SF Find Neighborhoods".
REGION_DATASET = "https://data.sfgov.org/resource/6qbp-sg9q.json"
REGION_COLUMN = ":@computed_region_6qbp_sg9q"

# A block where the winning neighbourhood holds less than this share of the
# addresses is a real boundary rather than a stray record, and is recorded as
# contested. A wrong neighbourhood costs more than an unknown one, because area
# carries the most weight in the score. The same bar applies to both layers:
# lowering it for the finer one would buy coverage with guesses.
DOMINANCE = 0.8

# The city's 117 "SF Find" neighbourhoods against the product's own vocabulary.
# Only names with one honest counterpart are mapped. The rest -- "Lower Nob
# Hill", "Dolores Heights", "Mt. Davidson Manor" and sixty more -- are areas the
# product has no name for, and a block inside one falls through to the coarse
# layer below rather than being rounded to whichever product area looks nearest.
FIND_TO_CANONICAL = {
    "Alamo Square": "Alamo Square",
    "Anza Vista": "Anza Vista",
    "Bayview": "Bayview",
    "Bernal Heights": "Bernal Heights",
    "Castro": "Castro",
    "Chinatown": "Chinatown",
    "Civic Center": "Civic Center",
    "Cole Valley": "Cole Valley",
    "Cow Hollow": "Cow Hollow",
    "Crocker Amazon": "Crocker Amazon",
    "Diamond Heights": "Diamond Heights",
    "Dogpatch": "Dogpatch",
    "Duboce Triangle": "Duboce Triangle",
    "Eureka Valley": "Eureka Valley",
    "Excelsior": "Excelsior",
    "Financial District": "Financial District",
    "Forest Hill": "Forest Hill",
    "Glen Park": "Glen Park",
    "Haight Ashbury": "Haight-Ashbury",
    "Hayes Valley": "Hayes Valley",
    "Ingleside": "Ingleside",
    "Inner Richmond": "Inner Richmond",
    "Inner Sunset": "Inner Sunset",
    "Lower Haight": "Lower Haight",
    "Marina": "Marina",
    "Mission": "Mission District",
    "Mission Bay": "Mission Bay",
    "Mission Dolores": "Mission Dolores",
    "Nob Hill": "Nob Hill",
    "Noe Valley": "Noe Valley",
    "North Beach": "North Beach",
    "Oceanview": "Oceanview",
    "Outer Mission": "Outer Mission",
    "Outer Richmond": "Outer Richmond",
    "Outer Sunset": "Outer Sunset",
    "Pacific Heights": "Pacific Heights",
    "Parkmerced": "Park Merced",
    "Parkside": "Parkside",
    "Portola": "Portola",
    "Potrero Hill": "Potrero Hill",
    "Presidio Heights": "Presidio Heights",
    "Russian Hill": "Russian Hill",
    "Seacliff": "Sea Cliff",
    "South of Market": "SoMa",
    "St. Francis Wood": "St. Francis Wood",
    "Sunnyside": "Sunnyside",
    "Telegraph Hill": "Telegraph Hill",
    "Tenderloin": "Tenderloin",
    "Visitacion Valley": "Visitacion Valley",
    "West Portal": "West Portal",
    "Western Addition": "Western Addition",
}

# DataSF's 41 "analysis neighborhoods", the coarse layer, kept as the fallback:
# it still answers Twin Peaks, and the whole of Portola and Visitacion Valley,
# where the fine layer names areas the product does not know. Same rule -- an
# analysis neighbourhood covering several of the product's areas is left out,
# and a block that reaches here inside one is recorded as contested.
ANALYSIS_TO_CANONICAL = {
    "Bayview Hunters Point": "Bayview",
    "Bernal Heights": "Bernal Heights",
    "Castro/Upper Market": "Castro",
    "Chinatown": "Chinatown",
    "Excelsior": "Excelsior",
    "Financial District/South Beach": "Financial District",
    "Glen Park": "Glen Park",
    "Haight Ashbury": "Haight-Ashbury",
    "Hayes Valley": "Hayes Valley",
    "Inner Richmond": "Inner Richmond",
    "Inner Sunset": "Inner Sunset",
    "Marina": "Marina",
    "Mission": "Mission District",
    "Mission Bay": "Mission Bay",
    "Nob Hill": "Nob Hill",
    "Noe Valley": "Noe Valley",
    "North Beach": "North Beach",
    "Outer Mission": "Outer Mission",
    "Outer Richmond": "Outer Richmond",
    "Pacific Heights": "Pacific Heights",
    "Portola": "Portola",
    "Potrero Hill": "Potrero Hill",
    "Presidio Heights": "Presidio Heights",
    "Russian Hill": "Russian Hill",
    "Seacliff": "Sea Cliff",
    "South of Market": "SoMa",
    "Tenderloin": "Tenderloin",
    "Twin Peaks": "Twin Peaks",
    "Visitacion Valley": "Visitacion Valley",
    "Western Addition": "Western Addition",
}


def soql(dataset: str, query: str) -> list[dict]:
    """Run one SoQL statement.

    Sent as ``$query`` rather than as ``$select``/``$group`` parameters because
    a gateway in front of the portal now answers 403 to any request carrying a
    ``$select``, whatever it selects. The single-statement form asks the same
    question and is served.
    """
    url = f"{dataset}?{urlencode({'$query': query})}"
    with urlopen(url, timeout=300) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_region_names() -> dict[str, str]:
    """Feature id -> SF Find neighbourhood name, for the address column."""
    rows = soql(REGION_DATASET, "select _feature_id, name limit 1000")
    return {str(row["_feature_id"]): str(row["name"]).strip() for row in rows}


def fetch() -> list[dict]:
    """One grouped query; the whole city comes back in ~12,000 rows."""
    rows = soql(
        DATASET,
        "select street_name, street_type, nhood, "
        f"`{REGION_COLUMN}` as region, floor(address_number/100) as blk, "
        "count(*) as n "
        f"group by street_name, street_type, nhood, `{REGION_COLUMN}`, blk "
        "limit 100000",
    )
    if len(rows) >= 100000:  # pragma: no cover - a silent truncation would ship
        raise SystemExit("the dataset outgrew one page; add paging before trusting this")
    return rows


def _dominant(tally: dict[str, int], mapping: dict[str, str]) -> str | None:
    """The product's name for this block, or None if the layer cannot say."""
    if not tally:
        return None
    winner, votes = max(tally.items(), key=lambda item: (item[1], item[0]))
    if votes / sum(tally.values()) < DOMINANCE:
        return None
    return mapping.get(winner)


def build(rows: list[dict], region_names: dict[str, str]) -> dict:
    fine: dict[tuple[str, int], dict[str, int]] = defaultdict(lambda: defaultdict(int))
    coarse: dict[tuple[str, int], dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for row in rows:
        name = str(row.get("street_name") or "").strip().upper()
        suffix = str(row.get("street_type") or "").strip().upper()
        if not name:
            continue
        try:
            block, count = int(row["blk"]), int(row["n"])
        except (KeyError, TypeError, ValueError):
            continue
        if block < 0 or count <= 0:
            continue
        key = (f"{name} {suffix}".strip(), block)
        analysis = str(row.get("nhood") or "").strip()
        if analysis:
            coarse[key][analysis] += count
        found = region_names.get(str(row.get("region") or ""))
        if found:
            fine[key][found] += count

    names: list[str] = sorted(
        set(FIND_TO_CANONICAL.values()) | set(ANALYSIS_TO_CANONICAL.values())
    )
    index = {name: position for position, name in enumerate(names)}
    streets: dict[str, dict[str, int]] = defaultdict(dict)
    contested = 0
    from_fine = 0
    for key in sorted(set(fine) | set(coarse)):
        street, block = key
        canonical = _dominant(fine.get(key, {}), FIND_TO_CANONICAL)
        if canonical is not None:
            from_fine += 1
        else:
            canonical = _dominant(coarse.get(key, {}), ANALYSIS_TO_CANONICAL)
        if canonical is None:
            # -1 means "the city's own data cannot name this block in the
            # product's vocabulary". Recording it is the point: an omitted block
            # would fall through to the street-wide answer, which is exactly the
            # wrong answer on a boundary block.
            streets[street][str(block)] = -1
            contested += 1
            continue
        streets[street][str(block)] = index[canonical]

    return {
        "source": (
            "DataSF Addresses with Units (ramy-di5m), placed by SF Find "
            "Neighborhoods (6qbp-sg9q) and, where that layer cannot say, by the "
            "analysis neighborhoods the address dataset carries"
        ),
        "note": (
            "Hundred block -> neighbourhood, per street. -1 means the block is "
            "split between neighbourhoods, or lies in one the product has no "
            "name for, and must resolve to nothing."
        ),
        "dominance": DOMINANCE,
        "names": names,
        "streets": {street: dict(sorted(blocks.items(), key=lambda i: int(i[0])))
                    for street, blocks in sorted(streets.items())},
        "contested_blocks": contested,
        "blocks_from_fine_layer": from_fine,
    }


def main() -> None:
    table = build(fetch(), fetch_region_names())
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(table, separators=(",", ":")), encoding="utf-8")
    blocks = sum(len(b) for b in table["streets"].values())
    print(f"streets:  {len(table['streets'])}")
    print(f"blocks:   {blocks} ({blocks - table['contested_blocks']} resolve, "
          f"{table['contested_blocks']} contested)")
    print(f"          {table['blocks_from_fine_layer']} answered by SF Find, the rest "
          f"by the analysis neighbourhoods")
    print(f"written:  {OUTPUT} ({OUTPUT.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    sys.exit(main())
