"""Whether the stored scores are still the scores this code would produce.

Start-up used to re-rank every stored listing before the server bound its port:
on a real board of 8,680 homes that is twelve seconds on an idle machine and
approaching fifty on a busy one, and it ran on *every* service start -- a login,
an automatic upgrade, a restart after a crash -- not only after somebody changed
their deal. The dashboard answered nothing until it finished, and the Open
script used to give up and tell people the app had failed while it was working.

Almost all of that work was provably redundant. ``score_listing`` is a pure
function of a listing and a set of preferences: it reads no clock, and scoring
the same home twice returns the same answer. So if neither the answers nor the
code have changed since the last complete pass, neither can the scores have, and
the right pass takes no time at all because it does not happen.

This module works out what a pass would be computed from; the board itself
stores what the last one *was* computed from, in ``scoring_state``. Keeping the
two in one file is the point: a mark beside the database can be separated from
it -- by a restored backup, a data directory copied to another machine, a file
pulled out of Time Machine -- and a separated mark eventually vouches for rows
it never saw. A pass retracts the mark before it rewrites anything and records
it again only once every row is written, so a board being rewritten, or left
half rewritten by a crash, carries no claim at all.

Everything that cannot be established is answered "yes, a pass is owed": no
mark, a database too old to have the table, a source tree that cannot be
hashed. Being wrong in that direction costs a slow start-up, which is what the
app did unconditionally before this existed. Being wrong in the other direction
would leave somebody looking at a shortlist ranked against a deal they have
already changed, which is the failure this must never have.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import cycle at runtime, types only
    from .database import Repository
    from .preferences import Preferences

# Bumped by hand to invalidate every stored mark on purpose -- for a change
# that alters scores without editing any of the modules below, such as new
# reference data. Ordinary code changes need no bump: they are caught by the
# source fingerprint, which is the whole reason this is not a number somebody
# has to remember.
SCORING_VERSION = 1

# Everything that decides what a pass writes. Two different reasons put a module
# on this list, and `test_rescore_marker.py` checks both against the source, so
# neither can quietly stop being true.
#
# The first six are the import closure of `scoring`: it imports classification,
# location, models, deal_profile and preferences; classification imports models;
# location and preferences import deal_profile; models and deal_profile import
# nothing further.
#
# `sources.py` is here for a different reason, and it is the one that is easy to
# miss: a pass is not only `score_listing`. `Scanner._rescore` rewrites a
# Facebook Marketplace listing's neighbourhood through
# `facebook_coordinate_neighborhood`, and backfills a Furnished Finder one
# through `visible_sf_area_hint`, before scoring either -- so those two decide
# scores as surely as scoring does, and both live there. Both are self-contained
# within that file, so hashing its contents covers them; its own imports are not
# followed, which is why connectors, apify and gmail_alerts are absent.
#
# `database.py` is here for the same kind of reason as `sources.py`:
# `Repository._update_score` is not a passive writer. It classifies the listing
# again and merges the classified metadata back into the row, and a later pass
# reads that metadata -- `verified_inactive` caps a score at 49 -- so what it
# stores decides both what this pass writes and what the next one sees.
#
# The cost is honest and worth naming: a release that touches only a scraper or
# only the database layer still invalidates every mark and buys one slow start.
# That is one pass per upgrade against one per launch, which is the trade this
# whole module exists to make, and being wrong the other way would be silent.
SCORING_MODULES = (
    "scoring.py",
    "classification.py",
    "location.py",
    "models.py",
    "deal_profile.py",
    "preferences.py",
    "sources.py",
    "database.py",
)

_PACKAGE_ROOT = Path(__file__).resolve().parent


def scoring_fingerprint() -> str | None:
    """What this build scores with, or None if that cannot be established.

    Derived from the source rather than declared by hand. A constant somebody
    has to remember to bump is a constant somebody eventually forgets, and the
    cost of forgetting here is silent: every existing install keeps stale
    scores forever and nothing looks wrong. Reading the eight files costs under
    a millisecond against the twelve seconds it saves.

    None when any of them cannot be read -- a source-less build, an unreadable
    file -- because a fingerprint that cannot see the code cannot promise the
    code has not changed.
    """
    digest = hashlib.sha256()
    digest.update(f"v{SCORING_VERSION}\n".encode())
    for name in SCORING_MODULES:
        try:
            digest.update(name.encode())
            digest.update((_PACKAGE_ROOT / name).read_bytes())
        except OSError:
            return None
    return digest.hexdigest()


def deal_fingerprint(preferences: "Preferences") -> str | None:
    """What this deal scores as, or None if that cannot be established.

    Both halves of it, because scoring reads both. ``preferences.section(...)``
    and ``preferences.weights`` come from ``Preferences.data``, which is
    ``legacy_view(profile, technical_settings(document))`` -- so the technical
    block feeds scoring and has to be here. But scoring also reads the canonical
    profile directly: ``deal_profile.budgets`` for a canonical budget,
    ``anywhere_in_sf`` for an area, ``enabled_paths`` for a path. The view does
    not carry every field of the profile, and against the author's own deal
    seven of them survive the round trip unchanged -- ``ideal_monthly`` among
    them. Hashing the view alone, somebody changing the price they would ideally
    pay would have kept every stale score.

    None when the document cannot be serialised at all. ``sort_keys`` cannot
    order a mapping whose keys are of mixed types, and a preferences file can
    reach here having parsed, validated and scored perfectly well -- so this
    answers the way everything unknowable here answers, and a pass is owed.
    """
    try:
        blob = json.dumps(
            {"view": preferences.data, "canonical": preferences.canonical},
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def current_fingerprint(preferences: "Preferences") -> str | None:
    """The mark a complete pass over this board would earn, or None if unknowable."""
    code = scoring_fingerprint()
    deal = deal_fingerprint(preferences)
    if code is None or deal is None:
        return None
    return f"{code}:{deal}"


def rescore_is_needed(repository: "Repository", preferences: "Preferences") -> bool:
    """Whether a full pass is owed before these scores can be believed."""
    expected = current_fingerprint(preferences)
    if expected is None:
        return True
    return repository.scoring_mark() != expected
