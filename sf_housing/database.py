from __future__ import annotations

import copy
import json
import re
import sqlite3
import threading
import time
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from bisect import bisect_left, bisect_right
from typing import Any, Callable, Iterator, Mapping, NoReturn, Sequence, TypeVar
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .classification import classify_listing
from .connectors import CONNECTOR_STATES, ConnectorStatus
from .freshness import NO_NETWORK_PHRASES
from .listing_identity import (
    BUILDING_GRAIN,
    GROUP_SQL,
    MOVED_PREFIX,
    building_of,
    IDENTITY_VERSION,
    choose_key,
    copy_rank,
    facts,
    grain,
    group_sql,
    id_key,
    is_id_key,
    own_key,
    same_home,
    twin_ids,
    UNIT_GRAIN,
)
from .location import canonical_neighborhood
from .models import (
    CHECK_EXCLUSION_PHRASES,
    ListingCandidate,
    ScoreResult,
    ordered_checks,
    unmeasured_criteria,
)


SCHEMA_VERSION = 5

_Answer = TypeVar("_Answer")


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class DatabaseUnreadableError(RuntimeError):
    """The database file exists but SQLite cannot read it.

    Raised instead of a bare sqlite3 error so the failure names the file and the
    recovery. The file is never moved or replaced automatically: it holds every
    star, note and first-found date the user has built up, and a corrupt file can
    often still be recovered, so destroying it to get the app running would be
    the worst possible trade.
    """


class DatabaseBusyError(RuntimeError):
    """Another program held the database the whole time start-up waited.

    Kept apart from ``DatabaseUnreadableError`` on purpose: that one sends the
    reader to Repair, which restores a backup, and a board that is merely in
    use by a scan or a second copy of the app is not damaged at all.
    """


@dataclass(frozen=True)
class ListingPage:
    """One page of a view: its homes in order, and how many the view holds."""

    rows: list[dict[str, Any]]
    total: int


def _is_busy(exc: sqlite3.Error) -> bool:
    message = str(exc).casefold()
    return "locked" in message or "busy" in message


def canonicalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    ignored = {
        "fbclid",
        "gclid",
        "listing_click",
        "search_id",
        "search_results",
        "utm_campaign",
        "utm_content",
        "utm_medium",
        "utm_source",
        "utm_term",
    }
    query = urlencode(sorted((key, value) for key, value in parse_qsl(parts.query) if key not in ignored))
    return urlunsplit((parts.scheme.casefold(), parts.netloc.casefold(), parts.path.rstrip("/"), query, ""))


SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    source_id TEXT NOT NULL,
    canonical_url TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    price INTEGER,
    neighborhood TEXT,
    listing_type TEXT,
    summary TEXT,
    housing_kind TEXT NOT NULL DEFAULT 'room',
    unit_type TEXT,
    building_units INTEGER,
    match_reasons_json TEXT NOT NULL DEFAULT '[]',
    concern TEXT NOT NULL,
    score INTEGER NOT NULL CHECK(score BETWEEN 0 AND 100),
    confidence INTEGER NOT NULL DEFAULT 0 CHECK(confidence BETWEEN 0 AND 100),
    eligibility TEXT NOT NULL DEFAULT 'eligible' CHECK(eligibility IN ('eligible', 'needs_verification', 'ineligible')),
    eligibility_reasons_json TEXT NOT NULL DEFAULT '[]',
    unknowns_json TEXT NOT NULL DEFAULT '[]',
    score_details_json TEXT NOT NULL DEFAULT '{}',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    first_found TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    published_at TEXT,
    availability_state TEXT NOT NULL DEFAULT 'unknown',
    original_url TEXT NOT NULL,
    opened_at TEXT,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'saved', 'dismissed')),
    note TEXT NOT NULL DEFAULT '',
    -- Which real home this row is a copy of (see listing_identity); empty
    -- when nothing but the row itself is known to be that home.
    home_key TEXT,
    -- Who put the row where it is: 'user' for a star, a pass or a restore,
    -- 'aged' for the 21-day archive, NULL for nobody.
    status_reason TEXT,
    -- When the archive last aged the row out (see ``_prune``).
    aged_at TEXT,
    -- How much this copy tells the reader, for choosing which copy to show.
    copy_rank INTEGER NOT NULL DEFAULT 0,
    UNIQUE(platform, source_id)
);

CREATE INDEX IF NOT EXISTS idx_listings_results
ON listings(status, score DESC, first_found DESC);

CREATE TABLE IF NOT EXISTS scan_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    listings_seen INTEGER NOT NULL DEFAULT 0,
    listings_added INTEGER NOT NULL DEFAULT 0,
    listings_updated INTEGER NOT NULL DEFAULT 0,
    sources_failed INTEGER NOT NULL DEFAULT 0,
    message TEXT
);

CREATE TABLE IF NOT EXISTS source_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_run_id INTEGER NOT NULL REFERENCES scan_runs(id),
    platform TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT '',
    source_key TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    listings_seen INTEGER NOT NULL DEFAULT 0,
    listings_added INTEGER NOT NULL DEFAULT 0,
    listings_updated INTEGER NOT NULL DEFAULT 0,
    fetched INTEGER NOT NULL DEFAULT 0,
    parsed INTEGER NOT NULL DEFAULT 0,
    classified INTEGER NOT NULL DEFAULT 0,
    deduplicated INTEGER NOT NULL DEFAULT 0,
    hard_filtered INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0,
    message TEXT,
    search_url TEXT
);

CREATE INDEX IF NOT EXISTS idx_source_runs_latest
ON source_runs(platform, id DESC);

CREATE TABLE IF NOT EXISTS source_initializations (
    source_key TEXT PRIMARY KEY,
    initialized_at TEXT NOT NULL,
    status TEXT NOT NULL,
    message TEXT
);

CREATE TABLE IF NOT EXISTS source_coverage (
    platform TEXT PRIMARY KEY,
    listing_count INTEGER NOT NULL,
    taken_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS connector_states (
    connector_key TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    configured_at TEXT,
    last_attempt_at TEXT,
    last_success_at TEXT,
    observed_items INTEGER NOT NULL DEFAULT 0,
    message TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
-- What the stored scores were computed from, so start-up can tell whether they
-- are still the scores this code and this deal would produce. Kept inside the
-- board rather than in a file beside it: a mark that can be separated from the rows it vouches
-- for is a mark that eventually vouches for the wrong ones -- a restored
-- backup, a data directory copied to another machine, a file recovered from
-- Time Machine. Travelling with the board, it cannot say "already scored" about
-- rows it never saw.
CREATE TABLE IF NOT EXISTS scoring_state (
    name TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


# A day without confirmation is the point at which the app stops being able
# to vouch for a home. It matches the scanner's own window with room for a
# missed scan, so a single skipped run does not flag the whole board.
CONFIRMATION_STALE_AFTER = timedelta(hours=24)


# What a home's own source going quiet about it costs, in two steps. A source
# dropping a home is absence, never proof: it can mean the flat is let, and it
# can equally mean the home slipped past a result cap or a search that ranked
# it differently today. So absence only ever demotes -- nothing here writes
# ``verified_inactive``, which is reserved for a page we fetched that said the
# home is gone, and nothing here deletes a row.
#
# Both numbers are named because they are the two the owner will want to tune,
# and both are counted against the source's own clock rather than the wall
# clock (see ``_UNSEEN_DAYS``).
#
# Below every home that is still showing up. The scanner runs twice a day, so
# thirty-six hours is three chances missed: enough that a home has stopped
# appearing rather than been unlucky once, and short enough that the top of
# the shortlist is homes somebody can still go and see today.
UNSEEN_DEMOTE_AFTER = timedelta(hours=36)
# Off the shortlist altogether, into Near matches, where it is still ranked,
# still filtered, still searchable and still one click from its listing. Six
# missed chances rather than three, because this is the step a reader does not
# see happen: a home that has been absent this long is far more likely let
# than unlucky, but it is never so likely that the row should disappear.
UNSEEN_SHORTLIST_AFTER = timedelta(days=3)


# How long a read of a home's own page goes on vouching for a rent the app
# does not believe.
#
# A rent below the floor for a home that size (scoring's
# ``IMPLAUSIBLE_RENT_FLOOR``, recorded on the score as ``price.implausibly_low``)
# is a question, not a verdict: cheap is the thing being searched for and some
# of these homes are real. So it costs the home its place at the top and
# nothing else -- it stays on the shortlist, one click from its own listing --
# until the scanner opens that listing and finds the home still there, which
# puts it straight back where its score says it belongs.
#
# Thirty-six hours for the same reason ``UNSEEN_DEMOTE_AFTER`` is: scans run
# twice a day, so it is three missed chances. Long enough that a confirmation
# never lapses while the scanner is running -- the scanner re-reads these
# pages every scan, well inside this -- and short enough that a scanner which
# has stopped running stops vouching, rather than a page read last week going
# on recommending a rent nobody has checked since.
CHEAP_RENT_CONFIRMED_FOR = timedelta(hours=36)


def _confirmation_check(stamp: object) -> dict[str, str] | None:
    """The open question a home raises simply by not having been seen lately.

    Returns nothing while the home is inside the window, so a freshly confirmed
    listing carries no extra noise.
    """
    if not isinstance(stamp, str) or not stamp:
        return {
            "check": "confirmation",
            "reason": "Nobody has confirmed this home is still listed; open it before relying on it.",
        }
    try:
        moment = datetime.fromisoformat(stamp).astimezone(UTC)
    except ValueError:
        return None
    age = datetime.now(UTC) - moment
    if age < CONFIRMATION_STALE_AFTER:
        return None
    days = max(1, int(age.total_seconds() // 86400))
    when = "a day" if days == 1 else f"{days} days"
    return {
        "check": "confirmation",
        "reason": f"Not confirmed as still listed for {when}; open it before relying on it.",
    }


def _cheap_rent_check(price: object, score_details: dict, metadata: dict) -> dict[str, str] | None:
    """The open question a rent below the floor for its size raises.

    Nothing unless the scorer has said the rent is below the floor for a home
    that size. When it has, the question is always worth printing: only a
    person can settle whether a figure is a month's rent for the whole home
    or a room's share, a weekly rate or a deposit, so it is not a question
    reading the page answers. On the author's board ten of the thirteen homes
    below their floor said nothing about the rent at all, because the single
    ``concern`` sentence is chosen by a chain of elif and an unstated
    neighbourhood wins it.

    What a page read does answer is whether there is still a listing there,
    and the sentence says which of the two is outstanding.

    Worked out here rather than in the scorer for the reason the confirmation
    check is: half of it depends on the clock, and a stored score must never
    change meaning because time passed.
    """
    detail = score_details.get("price")
    if not isinstance(detail, dict) or detail.get("implausibly_low") is not True:
        return None
    stamp = metadata.get("page_verified_at")
    confirmed = False
    if isinstance(stamp, str) and stamp:
        try:
            moment = datetime.fromisoformat(stamp).astimezone(UTC)
        except ValueError:
            moment = None
        confirmed = moment is not None and datetime.now(UTC) - moment < CHEAP_RENT_CONFIRMED_FOR
    amount = f"${int(price):,}/month is" if isinstance(price, (int, float)) else "This rent is"
    unread = "" if confirmed else ", and nobody has opened the listing to confirm it is still up"
    return {
        "check": "rent",
        "reason": (
            f"{amount} below anything a home this size lets for{unread}; "
            "confirm it is the total rent, not a room price or a deposit."
        ),
    }


# Ten rent bands crossed with four score bands is forty strata for 900 samples,
# so even the crowded ones keep enough homes to speak for themselves.
RENT_BANDS = 10


# How far below the cut-off a home can be and still be "nearly" a match. A near
# match is one the deal would have taken if it had scored a little higher, not
# every home the deal turned down: on a real board of 8,102 homes, "everything
# not shortlisted" meant 7,785 near matches, of which 7,625 had failed a hard
# constraint outright -- the wrong number of bedrooms, twice the budget -- and
# buried the 160 that had actually come close.
NEAR_MATCH_MARGIN = 10
# And how far over the rent line still counts as nearly. The score cannot say:
# a home over the line is ruled out, and ruled-out homes all score about the
# same -- so the gap the scorer wrote down is what separates a home over by a
# hundred a month from one at four times the budget.
NEAR_MATCH_OVER_BUDGET = 0.10


def near_miss_distance(item: dict[str, Any], minimum_score: int) -> float:
    """How near this home came, as a fraction of the way to being excluded.

    Near matches holds two kinds of miss that cannot be compared as they
    stand: points short of the cut-off, and money over the rent line. Scoring
    flattens both -- a whole home ruled out for its rent lands near 49 whatever
    it costs, and a room over the line is capped at 54 -- so on a real board
    the best of them all scored 49 while costing $5,430 to $12,400 against a
    $3,000 deal. Sorted by score, a home $40 over sat at random among homes at
    four times the budget.

    So each kind is measured against its own allowance: points against the ten
    below the cut-off this view holds, money against the tenth over the line.
    0.0 is on the line and 1.0 is the edge of what the view holds, which makes
    the two comparable. A home that is both -- a room is capped for its rent,
    which leaves it short on points as well -- is judged by whichever miss is
    the nearer, because that is the one somebody would forgive.

    Only the two misses the deal itself measured. A home nobody is listing any
    more has missed nothing, so it has no distance here at all; where that home
    goes is ``near_miss_order``'s question, not this one's.

    Never raises and never divides by a missing ceiling: a row stored before
    any of this shipped is put last rather than taking the page down with it.
    """
    distances: list[float] = []
    score = int(item.get("score") or 0)
    if minimum_score > 0 and NEAR_MATCH_MARGIN > 0 and score < minimum_score:
        distances.append((minimum_score - score) / NEAR_MATCH_MARGIN)
    over_by = int(item.get("over_budget_by") or 0)
    maximum = int(item.get("budget_maximum") or 0)
    if over_by > 0 and maximum > 0 and NEAR_MATCH_OVER_BUDGET > 0:
        distances.append(over_by / (maximum * NEAR_MATCH_OVER_BUDGET))
    return min(distances) if distances else float("inf")


def near_miss_order(item: dict[str, Any], minimum_score: int) -> tuple[int, float, float]:
    """Where a home sits in the closeness order: measured misses first.

    A third kind of home reaches this view: one short of nothing the deal
    asked for, whose own source has simply stopped returning it
    (``UNSEEN_SHORTLIST_AFTER``). Absence was measured on the same scale as a
    real miss, and that let it promote -- a home nobody has listed for a day
    longer than we allow outranked a home still on its site that missed the
    budget by $50. In a view that promises to say how close each home came,
    the home somebody can still ring about has to come first: absence is not
    an answer to that question, and it may only ever demote.

    So a home with a measured miss is ranked ahead of every home that has only
    gone quiet, and the quiet ones are ordered among themselves by how long
    ago anyone last saw them -- 0.0 the day it left the shortlist, 1.0 twice
    as long gone -- because a home that scored 92 yesterday is the likeliest
    of them to still be worth a call. Absence breaks ties within a measured
    miss too, which is the same rule once more: of two homes that missed by
    exactly as much, the one still being listed goes first.
    """
    unseen = float(item.get("unseen_days") or 0.0)
    measured = near_miss_distance(item, minimum_score)
    if measured != float("inf"):
        return (0, measured, unseen)
    allowance = _as_days(UNSEEN_SHORTLIST_AFTER)
    if allowance > 0 and unseen > allowance:
        return (1, (unseen - allowance) / allowance, 0.0)
    # Nothing measurable and nothing missing: a row stored before any of this
    # shipped. Last, rather than in front of homes that did come close.
    return (1, float("inf"), 0.0)


def _as_days(after: timedelta) -> float:
    """``after`` as the number of days the SQL clauses below compare against."""
    return after.total_seconds() / 86400.0


# A copy whose site says the home is no longer listed. Never the copy a home is
# shown by while another copy says it is still there.
_GONE = "COALESCE(json_extract(metadata_json, '$.verified_inactive'), 0) = 1"
# A copy no search or page has confirmed within the day the app can vouch for
# (``CONFIRMATION_STALE_AFTER``): shown only when no fresher copy is, and never
# the copy whose rent holds the home down.
_STALE = (
    "COALESCE(julianday('now') - julianday(COALESCE("
    "json_extract(metadata_json, '$.last_verified_at'), last_seen)), 99) > 1.0"
)
# A copy priced below anything a home its size lets for, whose own page nobody
# has opened within ``CHEAP_RENT_CONFIRMED_FOR``. The rent is the scorer's
# judgement, read back rather than worked out again here, so there is one
# floor and not a second one written in SQL.
#
# Deliberately not ``last_verified_at``: every search that returns a home
# stamps that, and a search returning a $1,125 three-bedroom is the same
# search that returned it yesterday -- it is no answer to whether the rent is
# real. Only ``page_verified_at``, which the scanner writes when it has
# actually read the listing's own page and the page still showed the home,
# counts here.
_CHEAP_RENT_UNCONFIRMED = (
    "COALESCE(json_extract(score_details_json, '$.price.implausibly_low'), 0) = 1"
    " AND COALESCE(julianday('now') - julianday("
    "json_extract(metadata_json, '$.page_verified_at')), 99) > "
    f"{_as_days(CHEAP_RENT_CONFIRMED_FOR)}"
)

# When a copy was last actually seen: a search that returned it, or a fetch of
# its own page that confirmed it, whichever happened later. Later rather than
# ``COALESCE``, which prefers the page stamp and would age a home the searches
# have gone on returning ever since it was last opened.
_LAST_SEEN = (
    "MAX(listings.last_seen, COALESCE("
    "json_extract(listings.metadata_json, '$.last_verified_at'), listings.last_seen))"
)

# The clock absence is measured against: not now, but the last time this
# home's own source finished a search and came back with homes in it.
#
# This is what keeps one outage from emptying the shortlist, which is the
# worst false positive available here. A site that is blocking us, timing out
# or simply unreachable records no successful run, so its homes' clocks stop
# where they were and every one of them keeps its place until the site answers
# again. The same evidence ``freshness.py`` derives a source's health and its
# backoff from -- the ``source_runs`` the scanner writes -- read here in SQL
# because this is a question asked of nine thousand rows at a time and keyed
# by the platform a listing stores, not by the source object the scan held.
#
# ``listings_seen > 0`` because a run that read a page and parsed nothing out
# of it is no evidence about any particular home. That is how a site quietly
# changing its markup looks: success, no error, and zero results. Without this
# the next such change would drain the shortlist in three days and blame the
# homes. It costs the opposite mistake -- a source with genuinely nothing to
# show holds its homes' clocks still -- which is the mistake this app prefers.
_SOURCE_LAST_SEARCHED = (
    "(SELECT MAX(run.started_at) FROM source_runs AS run"
    "  WHERE run.platform = listings.platform"
    "    AND run.status = 'success' AND run.listings_seen > 0)"
)

# How long a copy has gone unseen, in days, against that clock. Nothing is
# known of a home whose source has never completed a search, or whose dates
# are not dates, so it counts as seen: absence has to be evidenced before it
# can cost anything.
_UNSEEN_DAYS = (
    f"MAX(0.0, COALESCE(julianday({_SOURCE_LAST_SEARCHED}) - julianday({_LAST_SEEN}), 0.0))"
)

# The user's own work, which no rule about absence may hide. A star or a note
# is the one thing on this board that cannot be fetched again, and somebody
# who starred a home wants to decide for themselves when to give up on it.
_KEPT_BY_USER = "(status = 'saved' OR COALESCE(note, '') <> '')"


# Where an application can be made directly (see ``Repository._dashboard_row``).
_APPLICATION_PREFIX = "https://abacus.appfolio.com/"

# Two things ``_dashboard_row`` works out in Python, worked out in SQL as well,
# so a page ordered by them can be cut by LIMIT rather than by reading every
# home in the Archive first. Each must say exactly what the Python says of any
# stored value; tests/test_archive_pages.py holds them to it over odd ones. A
# column that is not JSON at all is nothing, as it is to the Python -- never
# an error that would take the whole page down.
#
# The move-in date a listing states: text, or nothing.
_AVAILABLE_ON = (
    "CASE WHEN json_valid(listings.score_details_json) THEN "
    "CASE WHEN json_type(listings.score_details_json, '$.availability.available_on') = 'text' "
    "THEN json_extract(listings.score_details_json, '$.availability.available_on') END END"
)
# 0 for a direct application, 1 for a direct lister, else 2. Python's truth of
# ``direct_lister`` whatever JSON it holds: an empty string, list or object,
# zero, false and null are all false.
_CONTACT_RANK = (
    "CASE WHEN NOT json_valid(listings.metadata_json) THEN 2 "
    "WHEN json_type(listings.metadata_json, '$.application_url') = 'text' "
    f"AND substr(json_extract(listings.metadata_json, '$.application_url'), 1, {len(_APPLICATION_PREFIX)}) "
    f"= '{_APPLICATION_PREFIX}' THEN 0 "
    "WHEN listings.platform = 'Listings Project' AND (CASE json_type(listings.metadata_json, '$.direct_lister') "
    "WHEN 'true' THEN 1 "
    "WHEN 'integer' THEN json_extract(listings.metadata_json, '$.direct_lister') <> 0 "
    "WHEN 'real' THEN json_extract(listings.metadata_json, '$.direct_lister') <> 0 "
    "WHEN 'text' THEN json_extract(listings.metadata_json, '$.direct_lister') <> '' "
    "WHEN 'array' THEN json_array_length(listings.metadata_json, '$.direct_lister') > 0 "
    "WHEN 'object' THEN json_extract(listings.metadata_json, '$.direct_lister') <> '{}' "
    "ELSE 0 END) THEN 1 ELSE 2 END"
)


# A rent a listing's own text states as monthly -- "$7,495 a month", "$3,200/mo"
# -- or a range of them, "$4,640 - $5,895/mo".
_MONTHLY = r"\s*(?:/\s*mo(?:nth)?\b|per\s+month|a\s+month|monthly)"
_AMOUNT = r"\$\s?(\d{1,2},\d{3}|\d{3,5})(?:\.\d{2})?"
_QUOTED_RENT = re.compile(
    rf"{_AMOUNT}(?:\s*(?:-|\u2013|to)\s*{_AMOUNT})?{_MONTHLY}", re.IGNORECASE
)


# Money in a description that is not the listing's rent: parking, a deposit,
# one person's share of it.
_NOT_RENT_BEFORE = re.compile(
    r"parking|garage|storage|utilit|deposit|\bfees?\b|\bpets?\b|laundry|furnish|roommate|tenant|\beach\b|\bshare"
    r"|effective|concession|special|promo|\bfree\b|offer"
)
_NOT_RENT_AFTER = re.compile(
    r"^\s*(?:per|a|/)\s*(?:person|head|room|bed|tenant|roommate)|^\s*each|^\s*(?:for\s+)?(?:\w+\s+)?(?:parking|garage|storage)"
)


def quoted_rent_mismatch(summary: str | None, price: int | None, grain: str | None = None) -> str | None:
    """A monthly rent the description states that is not the listing's rent.

    WO-3 todo 5: 1,414 of 8,254 priced listings quoted a rent in their text
    that differed from the price beside it -- "$7,195/mo" over "$7,495 a
    month". A description kept from a detail page can be older than the
    card's price, so rather than let the two sit side by side as if they
    agreed, the page names the difference: the quote as written ("$7,495",
    or "$4,640-$5,895" for a range). None when the text states no monthly
    rent, states this one, or states a range this one falls in -- and for a
    building's card, whose text lists what several floorplans cost and is
    not about the one rent the card shows.
    """
    if price is None or not summary or grain == BUILDING_GRAIN:
        return None
    quotes: list[tuple[int, int, bool]] = []
    for match in _QUOTED_RENT.finditer(summary):
        before = summary[max(0, match.start() - 40) : match.start()].casefold()
        after = summary[match.end() : match.end() + 25].casefold()
        # Parking, a deposit, one person's share: money that may be named as
        # the rent only if nothing says it is something else.
        nameable = not (_NOT_RENT_BEFORE.search(before) or _NOT_RENT_AFTER.search(after))
        low = int(match.group(1).replace(",", ""))
        high = int(match.group(2).replace(",", "")) if match.group(2) else low
        if low >= 500:
            quotes.append((min(low, high), max(low, high), nameable))
    # Checked against every amount the text states, so "$1,075 per month
    # each" still agrees with a room let at $1,075.
    if any(low <= int(price) <= high for low, high, _ in quotes):
        return None
    named = [(low, high) for low, high, nameable in quotes if nameable]
    if not named:
        return None
    low, high = named[0]
    return f"${low:,}" if low == high else f"${low:,}\u2013${high:,}"


def _confirmed_long_ago(row: sqlite3.Row) -> bool:
    """``_STALE`` for a row in hand."""
    stamp = _json_object(row["metadata_json"]).get("last_verified_at") or row["last_seen"]
    try:
        moment = datetime.fromisoformat(str(stamp)).astimezone(UTC)
    except (TypeError, ValueError):
        return True
    return datetime.now(UTC) - moment > CONFIRMATION_STALE_AFTER


def _json_object(text: str | None) -> dict[str, Any]:
    try:
        value = json.loads(text or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _available_on(score_details: str | dict[str, Any] | None) -> str | None:
    details = score_details if isinstance(score_details, dict) else _json_object(score_details)
    availability = details.get("availability")
    value = availability.get("available_on") if isinstance(availability, dict) else None
    return value if isinstance(value, str) else None


def _owned(row: sqlite3.Row) -> bool:
    """Whether the user has put anything of theirs on this row.

    A home the archive aged out is not the user's: it may be matched and
    updated as freely as one nobody touched.
    """
    reason = row["status_reason"]
    return (
        reason == "user"
        or bool(str(row["note"] or "").strip())
        or (str(row["status"]) != "active" and reason != "aged")
    )


def _may_follow(
    owned: bool, own: str, current: str, by_id: str, partners_before: Sequence[str], displaced: bool
) -> bool:
    """Whether a row may take the new home key its syndicated twin gives it.

    A row nobody has decided about, or one whose own address names its home,
    always may. A row the user starred, noted or passed that has no address
    of its own follows its twin only as the same record: when the two were
    one home already (a rent change moves both) or the row has so far been
    nothing but its site's id (the twin is the first to say where it is) --
    and never when the write that moved the twin had just pushed another flat
    out of the id, which is the site re-letting it.
    """
    if not owned or own:
        return True
    if displaced:
        return False
    return current in {"", by_id} or current in partners_before


def _spellings(source_id: str) -> tuple[str, str, str]:
    """The ways one syndicated id is cased: ApartmentGuide's "LV32..." is
    Rent.com's "lv32...". Three exact values, so the unique index answers."""
    return source_id, source_id.upper(), source_id.lower()


def _attached_url(row: sqlite3.Row) -> str:
    """The canonical URL a row answers to, without a detached row's suffix."""
    url = str(row["canonical_url"])
    suffix = f"#{int(row['id'])}"
    return url[: -len(suffix)] if url.endswith(suffix) else url


def _is_detached(row: sqlite3.Row) -> bool:
    return str(row["source_id"]).endswith(f"#{int(row['id'])}")


def _tabs_key(outside_tabs: tuple[bool, Sequence[str]] | None) -> tuple[bool, tuple[str, ...]] | None:
    """``outside_tabs`` as part of a key: the same tabs however they were spelled."""
    if outside_tabs is None:
        return None
    rooms, sizes = outside_tabs
    return bool(rooms), tuple(str(size) for size in sizes)


class Repository:
    # How long a connection waits for another writer before giving up.
    BUSY_SECONDS = 10.0
    # How many times start-up waits that long before saying the board is busy.
    START_ATTEMPTS = 3
    # How many remembered answers one board keeps (see ``_remembered``).
    REMEMBERED_ANSWERS = 64

    def __init__(self, path: Path):
        self.path = Path(path)
        # The connection this thread reuses inside ``reusing_one_connection``.
        # Per instance and per thread, never shared: a connection belongs to
        # the thread that opened it, and to one file.
        self._reuse = threading.local()
        # Answers that depend on nothing but the stored listings, filed by the
        # board version they were read at (see ``_remembered``).
        self._answers: dict[tuple[int, tuple[Any, ...]], Any] = {}
        self._answers_lock = threading.Lock()

    def _open(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=self.BUSY_SECONDS)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {int(self.BUSY_SECONDS * 1000)}")
        return connection

    @contextmanager
    def connection(self, *, reuse: bool = True) -> Iterator[sqlite3.Connection]:
        """A connection to the board for one block, gone when the block ends.

        Inside ``reusing_one_connection`` it is the scope's one connection,
        lent to one block at a time. A block that asked while another still
        held it -- nothing does today -- would get a connection of its own,
        exactly as outside a scope, so no block can ever commit or roll back
        work that is not its own. ``reuse=False`` always opens a new one: for
        start-up, and for the checks that must read the file as it is now.

        Read every row a query returns, or drop its cursor, before the block
        ends. A cursor kept past its block holds its read snapshot open on a
        connection the next block reuses: that block would not see another
        writer's commits, and its own write would fail at once as locked.
        Closing each block's connection used to hide this; nothing here does it.
        """
        if not reuse or not getattr(self._reuse, "depth", 0) or getattr(self._reuse, "lent", False):
            connection = self._open()
            try:
                yield connection
            finally:
                connection.close()
            return
        held = getattr(self._reuse, "connection", None)
        if held is None:
            held = self._reuse.connection = self._open()
        self._reuse.lent = True
        failed = False
        try:
            yield held
        except BaseException:
            failed = True
            raise
        finally:
            self._reuse.lent = False
            self._hand_back(held, discard=failed)

    def _hand_back(self, connection: sqlite3.Connection, *, discard: bool) -> None:
        """Leave the board as closing the connection would have left it.

        Closing threw away whatever a block had not committed. A connection
        kept for the next block has to do that by hand, or a write that failed
        half way -- a busy board, a full disk -- would be committed by the next
        block's commit, or hold the write lock for the rest of the request.
        After a failure the connection is not lent again at all: whatever went
        wrong, the next block starts from a new one.
        """
        try:
            if connection.in_transaction:
                connection.rollback()
        except sqlite3.Error:
            discard = True
        if discard:
            self._reuse.connection = None
            with suppress(sqlite3.Error):
                connection.close()

    @contextmanager
    def reusing_one_connection(self) -> Iterator[None]:
        """Answer everything this thread asks of the board inside from one connection.

        A dashboard request opened 29 connections -- one per question, most of
        them one per source on the status panel -- each paying the open, the
        pragmas and a cold page cache. Inside this scope the first question
        opens one and the rest reuse it, and it is closed when the scope ends:
        between requests nothing is held open, so the write-ahead log is still
        folded back into the file, and Repair, an install or Windows can still
        replace or delete it.

        For a request's own thread. The scan, its lane and the scheduler keep
        a connection per question, and a scope entered inside another one is
        part of it.
        """
        depth = getattr(self._reuse, "depth", 0)
        self._reuse.depth = depth + 1
        try:
            yield
        finally:
            self._reuse.depth = depth
            if not depth:
                held = getattr(self._reuse, "connection", None)
                self._reuse.connection = None
                self._reuse.lent = False
                if held is not None:
                    with suppress(sqlite3.Error):
                        if held.in_transaction:
                            held.rollback()
                    with suppress(sqlite3.Error):
                        held.close()

    def board_version(self) -> int | None:
        """A number that moves whenever a listing is added, changed or removed.

        Kept by triggers inside the file (see ``_count_changes``), so it moves
        for every writer -- this process, a scan run from the command line, a
        test's raw SQL -- and a write that is rolled back leaves it where it
        was. Opening a listing is the one write it leaves out: nothing keyed on
        it reads when a home was opened. ``None`` for a board without the
        counter, which then remembers nothing.
        """
        with self.connection() as connection:
            return self._board_version(connection)

    @staticmethod
    def _board_version(connection: sqlite3.Connection) -> int | None:
        try:
            row = connection.execute("SELECT value FROM listings_version WHERE id = 1").fetchone()
        except sqlite3.OperationalError:
            return None
        return int(row[0]) if row is not None else None

    def _remembered(
        self, connection: sqlite3.Connection, key: tuple[Any, ...], compute: Callable[[], _Answer]
    ) -> _Answer:
        """``compute()``, or what it answered the last time the board was exactly this.

        Only for answers that depend on the stored listings and ``key`` alone:
        no clock, no other table, nothing about when a home was opened. The
        version is read first, on the connection that then computes, so an
        answer is never older than the version it is filed under -- a write
        landing in between files a newer answer under the older version, which
        only requests that began before that write can still ask for. Copies
        go in and come out, so no caller can change what the next one is told.
        """
        version = self._board_version(connection)
        if version is None:
            # Nothing can be vouched for without the counter, including what
            # was filed before it went: a counter made again from nothing could
            # climb back to a number already filed.
            with self._answers_lock:
                self._answers.clear()
            return compute()
        entry = (version, key)
        with self._answers_lock:
            if entry in self._answers:
                return copy.deepcopy(self._answers[entry])
        answer = compute()
        with self._answers_lock:
            # Filed under any other version, an answer is one nobody should be
            # given again; and a board is only ever asked a few questions.
            if any(held != version for held, _ in self._answers) or (
                len(self._answers) >= self.REMEMBERED_ANSWERS
            ):
                self._answers.clear()
            self._answers[entry] = copy.deepcopy(answer)
        return answer

    def initialize(self) -> None:
        """Open, check and migrate the board.

        The one-time identity migration takes the write lock, so a scan or a
        second copy of the app writing at that moment makes start-up wait.
        That is waited out a few times and then reported as busy -- never as
        a damaged file, whose remedy is restoring a backup.
        """
        for attempt in range(self.START_ATTEMPTS):
            try:
                self._initialize()
                return
            except sqlite3.OperationalError as exc:
                if not _is_busy(exc):
                    self._unreadable(exc)
                if attempt + 1 == self.START_ATTEMPTS:
                    raise DatabaseBusyError(
                        f"The housing database at {self.path} stayed in use by another program "
                        "for the whole time start-up waited. Nothing is wrong with it: quit any "
                        "other copy of SF Home Finder and start it again."
                    ) from exc
            except sqlite3.DatabaseError as exc:
                self._unreadable(exc)

    def _unreadable(self, exc: sqlite3.Error) -> NoReturn:
        raise DatabaseUnreadableError(
            f"The housing database at {self.path} could not be opened ({exc}). "
            "Your saved homes are not lost: double-click Repair SF Home Finder, "
            "which restores the most recent backup from the app's backups folder. "
            "Do not delete the file."
        ) from exc

    def _initialize(self) -> None:
        # Never a reused connection: the check below has to read the file as
        # it is now, and the migration opens its own transaction.
        with self.connection(reuse=False) as connection:
            self._refuse_a_damaged_file(connection)
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(SCHEMA)
            columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(listings)")}
            if "opened_at" not in columns:
                connection.execute("ALTER TABLE listings ADD COLUMN opened_at TEXT")
            if "housing_kind" not in columns:
                connection.execute("ALTER TABLE listings ADD COLUMN housing_kind TEXT NOT NULL DEFAULT 'room'")
            if "unit_type" not in columns:
                connection.execute("ALTER TABLE listings ADD COLUMN unit_type TEXT")
            if "building_units" not in columns:
                connection.execute("ALTER TABLE listings ADD COLUMN building_units INTEGER")
            if "confidence" not in columns:
                connection.execute("ALTER TABLE listings ADD COLUMN confidence INTEGER NOT NULL DEFAULT 0")
            if "eligibility" not in columns:
                connection.execute("ALTER TABLE listings ADD COLUMN eligibility TEXT NOT NULL DEFAULT 'eligible'")
            if "eligibility_reasons_json" not in columns:
                connection.execute("ALTER TABLE listings ADD COLUMN eligibility_reasons_json TEXT NOT NULL DEFAULT '[]'")
            if "unknowns_json" not in columns:
                connection.execute("ALTER TABLE listings ADD COLUMN unknowns_json TEXT NOT NULL DEFAULT '[]'")
            if "published_at" not in columns:
                connection.execute("ALTER TABLE listings ADD COLUMN published_at TEXT")
            if "availability_state" not in columns:
                connection.execute("ALTER TABLE listings ADD COLUMN availability_state TEXT NOT NULL DEFAULT 'unknown'")
            connection.execute(
                """CREATE INDEX IF NOT EXISTS idx_listings_housing_results
                   ON listings(housing_kind, status, score DESC, first_found DESC)"""
            )
            source_columns = {
                str(row["name"]) for row in connection.execute("PRAGMA table_info(source_runs)")
            }
            source_column_migrations = {
                "provider": "TEXT NOT NULL DEFAULT ''",
                "source_key": "TEXT NOT NULL DEFAULT ''",
                "listings_updated": "INTEGER NOT NULL DEFAULT 0",
                "fetched": "INTEGER NOT NULL DEFAULT 0",
                "parsed": "INTEGER NOT NULL DEFAULT 0",
                "classified": "INTEGER NOT NULL DEFAULT 0",
                "deduplicated": "INTEGER NOT NULL DEFAULT 0",
                "hard_filtered": "INTEGER NOT NULL DEFAULT 0",
                "active": "INTEGER NOT NULL DEFAULT 0",
                "archived": "INTEGER NOT NULL DEFAULT 0",
            }
            for name, declaration in source_column_migrations.items():
                if name not in source_columns:
                    connection.execute(f"ALTER TABLE source_runs ADD COLUMN {name} {declaration}")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_source_runs_identity ON source_runs(source_key, id DESC)"
            )
            # Until the scanner could skip a source the deal has no use for,
            # SpareRoom and Abacus skipped themselves from inside their search
            # and were filed as successful checks that found nothing -- no
            # request behind any of them, and some also "rechecked" homes they
            # never read. Left as they were, the newest of them would later be
            # quoted as SpareRoom's last good result. Refiled as what they
            # were, by the exact words only those two ever wrote; running this
            # again finds nothing to do.
            connection.execute(
                """UPDATE source_runs
                      SET status = 'not_needed',
                          message = substr(message, 1, instr(message, 'in Your deal.') + 12)
                    WHERE status = 'success'
                      AND message LIKE 'Skipped because % are not enabled in Your deal.%'
                      AND instr(message, 'in Your deal.') > 0"""
            )
            connection.commit()
            self._migrate_identity(connection)

    def _refuse_a_damaged_file(self, connection: sqlite3.Connection) -> None:
        """Fail here, where the message is, rather than at the first query.

        Opening a database only reads page one. A file damaged below that --
        the bad sector, the full disk, the write cut off by a power loss --
        opens clean, answers PRAGMAs, and then raises ``database disk image is
        malformed`` out of whichever query ran first. On a 38MB board that was
        the home page: a bare 500 with nothing named and no way back, while
        /health still said ok and Open still said the app was ready.

        ``quick_check`` reads the pages. It costs 0.2s on that same board,
        paid once per start, and it turns the silent 500 into the message that
        already names the file and the recovery.
        """
        if not self.path.exists() or self.path.stat().st_size == 0:
            return
        row = connection.execute("PRAGMA quick_check(1)").fetchone()
        verdict = str(row[0]) if row is not None else "no result"
        if verdict.casefold() != "ok":
            raise sqlite3.DatabaseError(f"database disk image is malformed: {verdict}")

    # The row of ``scoring_state`` saying which identity rules the stored home
    # keys were computed by.
    IDENTITY_MARK = "identity_version"

    def _migrate_identity(self, connection: sqlite3.Connection) -> None:
        """Give every row its home, once, inside one transaction.

        ``BEGIN IMMEDIATE`` so that two starts racing on one board (the
        launcher and a manual start) cannot both add the columns or both
        rebuild: the second waits, then finds the work done. Nothing here
        touches a status the user set or any note; it only records who set
        them and which rows are one home.
        """
        connection.execute("BEGIN IMMEDIATE")
        try:
            # Which version last opened this board: every version stamps its
            # own on every start, so anything else means an older (or newer)
            # one has been here since this one last was.
            stamped = int(connection.execute("PRAGMA user_version").fetchone()[0])
            # Added here rather than with the older columns above because two
            # starts racing would both add them; here the second finds them.
            columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(listings)")}
            for name, declaration in (
                ("home_key", "TEXT"),
                ("status_reason", "TEXT"),
                ("copy_rank", "INTEGER NOT NULL DEFAULT 0"),
                ("aged_at", "TEXT"),
            ):
                if name not in columns:
                    connection.execute(f"ALTER TABLE listings ADD COLUMN {name} {declaration}")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_listings_home ON listings(home_key)")
            # Before this version only the user ever moved a home off the
            # active list, and a version before this one run on this board
            # since (a downgrade) writes statuses without a reason: whatever it
            # moved, the user moved -- including a home this version had aged
            # out and the older one then starred or restored. Only then: it
            # reads every row, which on a year of listings was 0.6s of every
            # start spent finding nothing.
            if stamped != SCHEMA_VERSION:
                connection.execute(
                    "UPDATE listings SET status_reason = 'user' "
                    "WHERE (status <> 'active' AND status_reason IS NULL) "
                    "OR (status_reason = 'aged' AND status <> 'dismissed')"
                )
            mark = connection.execute(
                "SELECT value FROM scoring_state WHERE name = ?", (self.IDENTITY_MARK,)
            ).fetchone()
            current = mark is not None and str(mark["value"]) == str(IDENTITY_VERSION)
            # A NULL key is a row no version of these rules has seen: written
            # by an older version after a downgrade.
            if not current or connection.execute(
                "SELECT 1 FROM listings WHERE home_key IS NULL LIMIT 1"
            ).fetchone():
                self._rebuild_identity(connection, only_unkeyed=current)
                connection.execute(
                    "INSERT INTO scoring_state (name, value) VALUES (?, ?) "
                    "ON CONFLICT(name) DO UPDATE SET value = excluded.value",
                    (self.IDENTITY_MARK, str(IDENTITY_VERSION)),
                )
            self._prepare_for_a_year(connection)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    # The order a home's copies are read in to decide what the home is (see
    # ``_HOME_SHAPE``). One text for the query and for the index that serves
    # it, so the index can never stop matching.
    _SHAPE_ORDER = "unit_type IS NOT NULL DESC, housing_kind = 'whole_unit' DESC, copy_rank DESC, id DESC"

    # Columns whose writes change nothing a remembered answer says: when a
    # home was opened is read by nothing that ``_remembered`` keeps.
    _UNCOUNTED_COLUMNS = frozenset({"id", "opened_at"})

    def _prepare_for_a_year(self, connection: sqlite3.Connection) -> None:
        """The indexes and the change counter that keep a year of listings fast.

        Idempotent, and built inside the migration's transaction, once: about
        two seconds per index on a year of listings (213,000 rows), then free.
        """
        # Every copy of a home, in the order that decides the home's shape,
        # carrying each column that decides it: finding a home's rows, and a
        # home's shape, never reads the rows themselves -- whose last columns
        # sit behind several kilobytes of JSON. Without it every page read a
        # year of listings to shape the few hundred homes it showed.
        connection.execute(
            f"CREATE INDEX IF NOT EXISTS idx_listings_home_shape ON listings("
            f"{GROUP_SQL}, {self._SHAPE_ORDER}, housing_kind, unit_type, home_key)"
        )
        # Each scan's own source runs. ``recent_scans`` counted them by reading
        # every source run ever recorded, twice per scan, on every page.
        connection.execute("CREATE INDEX IF NOT EXISTS idx_source_runs_scan ON source_runs(scan_run_id)")
        # When each site was last searched successfully, which retention asks
        # of every row it would delete (see ``_prune``).
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_source_runs_searched ON source_runs(platform, status, started_at)"
        )
        # Who put a row where it is, and when a search last returned it, so
        # retention reaches only the aged-out rows old enough to go, oldest
        # first, rather than reading a year of rows to find them. (The planner
        # reads Passed through ``idx_listings_results`` instead, every dismissed
        # row; retention is what keeps that short.)
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_listings_status_seen ON listings(status, status_reason, last_seen)"
        )
        # Its first shape, which only a development build ever made.
        connection.execute("DROP INDEX IF EXISTS idx_listings_status_reason")
        # When each row was last seen, with what the rent medians read from it,
        # so the last sixty days are found without reading the other ten
        # months (see ``rent_observations``).
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_listings_seen "
            "ON listings(last_seen, price, neighborhood, housing_kind, unit_type, home_key)"
        )
        self._count_changes(connection)

    def _count_changes(self, connection: sqlite3.Connection) -> None:
        """Keep ``board_version`` moving with every write to the listings.

        Triggers rather than bookkeeping in Python, so no writer can miss one:
        a scan in another process, a version of the app that knows nothing of
        this, a test's raw SQL. The update trigger names every column but those
        in ``_UNCOUNTED_COLUMNS``, and is rewritten whenever the columns change,
        so a column added later is counted without anybody remembering to.
        """
        connection.execute(
            "CREATE TABLE IF NOT EXISTS listings_version "
            "(id INTEGER PRIMARY KEY CHECK (id = 1), value INTEGER NOT NULL)"
        )
        connection.execute("INSERT OR IGNORE INTO listings_version (id, value) VALUES (1, 0)")
        bump = "UPDATE listings_version SET value = value + 1 WHERE id = 1;"
        counted = [
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(listings)")
            if str(row["name"]) not in self._UNCOUNTED_COLUMNS
        ]
        wanted = {
            "listings_version_insert": f"CREATE TRIGGER listings_version_insert AFTER INSERT ON listings BEGIN {bump} END",
            "listings_version_delete": f"CREATE TRIGGER listings_version_delete AFTER DELETE ON listings BEGIN {bump} END",
            "listings_version_update": (
                f"CREATE TRIGGER listings_version_update AFTER UPDATE OF {', '.join(counted)} "
                f"ON listings BEGIN {bump} END"
            ),
        }
        existing = {
            str(row["name"]): str(row["sql"])
            for row in connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'listings'"
            )
        }
        for name, sql in wanted.items():
            if existing.get(name) != sql:
                connection.execute(f"DROP TRIGGER IF EXISTS {name}")
                connection.execute(sql)

    def _rebuild_identity(
        self, connection: sqlite3.Connection, *, only_unkeyed: bool, among: Sequence[int] | None = None
    ) -> set[int]:
        """Recompute home keys, copy ranks and neighbourhood spellings.

        Stored neighbourhoods are respelled here too (Craigslist's
        "tenderloin" becomes "Tenderloin") because the same area under two
        spellings was two groups in every count and median.

        ``among`` limits it to those rows and their syndicated twins: a row's
        key follows from its own facts and its twin's, so rows whose facts
        did not change keep their keys unless a twin's moved. What a rescore
        of the live homes passes -- keying the whole board after it read every
        row of a year of listings, and rewrote each one. Returns the rows it
        keyed.
        """
        columns = """id, platform, source_id, original_url, title, housing_kind, unit_type,
                     price, metadata_json, score_details_json, published_at, neighborhood,
                     home_key, status, note, status_reason"""
        by_record: dict[tuple[str, str], int] = {}
        if among is None:
            rows = connection.execute(f"SELECT {columns} FROM listings").fetchall()
            for row in rows:
                by_record[(str(row["platform"]), str(row["source_id"]).casefold())] = int(row["id"])
        else:
            # Every record on the board, from the (platform, source_id) index
            # alone, to find the twins; then only the rows asked about and
            # those twins, whole.
            record_of: dict[int, tuple[str, str]] = {}
            for listing_id, platform, source_id in connection.execute(
                "SELECT id, platform, source_id FROM listings"
            ):
                by_record[(str(platform), str(source_id).casefold())] = int(listing_id)
                record_of[int(listing_id)] = (str(platform), str(source_id))
            wanted: set[int] = set()
            frontier = {int(value) for value in among if int(value) in record_of}
            while frontier:
                wanted |= frontier
                frontier = {
                    by_record[(platform, sid.casefold())]
                    for listing_id in frontier
                    for platform, sid in twin_ids(*record_of[listing_id])
                    if (platform, sid.casefold()) in by_record
                } - wanted
            ordered = sorted(wanted)
            rows = []
            for offset in range(0, len(ordered), 500):
                chunk = ordered[offset : offset + 500]
                rows.extend(connection.execute(
                    f"SELECT {columns} FROM listings WHERE id IN ({','.join('?' for _ in chunk)})", chunk
                ).fetchall())
        own: dict[int, str] = {}
        stored_key: dict[int, str] = {int(row["id"]): str(row["home_key"] or "") for row in rows}
        metadata_of: dict[int, dict[str, Any]] = {}
        for row in rows:
            metadata = _json_object(row["metadata_json"])
            metadata_of[int(row["id"])] = metadata
            own[int(row["id"])] = own_key(
                row["platform"], row["source_id"], row["original_url"], metadata,
                row["title"], row["housing_kind"], row["unit_type"], row["price"],
            )
        touched: set[str] = set()
        for row in rows:
            listing_id = int(row["id"])
            if only_unkeyed and row["home_key"] is not None:
                continue
            partners = [
                (platform, sid.casefold())
                for platform, sid in twin_ids(row["platform"], row["source_id"])
                if (platform, sid.casefold()) in by_record
            ]
            twins = [own[by_record[pair]] for pair in partners]
            key = choose_key(
                own[listing_id], twins, row["unit_type"],
                id_key(row["platform"], row["source_id"], row["unit_type"]),
            )
            current = str(row["home_key"] or "")
            if not own[listing_id] and current and (
                _is_detached(row) or current.startswith(MOVED_PREFIX)
            ):
                key = current
            elif row["home_key"] is not None and key != current and not _may_follow(
                _owned(row), own[listing_id], current,
                id_key(row["platform"], row["source_id"], row["unit_type"]),
                [stored_key[by_record[pair]] for pair in partners], False,
            ):
                # A user's row that earns no key of its own keeps the home it
                # was decided about, unless its twin is the same record moving.
                key = current
            rank = copy_rank(
                row["platform"], row["source_id"], row["original_url"], metadata_of[listing_id],
                row["price"], row["published_at"], _available_on(row["score_details_json"]),
            )
            area = canonical_neighborhood(row["neighborhood"])
            connection.execute(
                "UPDATE listings SET home_key = ?, copy_rank = ?, neighborhood = ? WHERE id = ?",
                (key, rank, area, listing_id),
            )
            if key:
                touched.add(key)
        for key in touched:
            self._settle_home(connection, key)
        # Every row of every home these rows are now in, not only the rows
        # read: another site's copy already filed under the key a re-keyed row
        # joined is in that home too, and a rescore that scores the home has
        # to score it.
        keyed = {int(row["id"]) for row in rows}
        keys = sorted(touched)
        for start in range(0, len(keys), 500):
            chunk = keys[start : start + 500]
            keyed.update(
                int(found[0])
                for found in connection.execute(
                    f"SELECT id FROM listings WHERE home_key IN ({', '.join('?' for _ in chunk)})", chunk
                )
            )
        return keyed

    def refresh_identities(self, connection: sqlite3.Connection, among: Sequence[int] | None = None) -> set[int]:
        """Recompute rows' home keys in one pass, on the caller's connection.

        What a rescore calls once at the end, rather than re-keying row by row:
        for every row, or only ``among`` and their twins (see
        ``_rebuild_identity``). Returns the rows it keyed.
        """
        connection.execute("BEGIN IMMEDIATE")
        try:
            keyed = self._rebuild_identity(connection, only_unkeyed=False, among=among)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        return keyed

    @staticmethod
    def _settle_home(connection: sqlite3.Connection, key: str) -> list[int]:
        """Make every copy of one home say what the user last said about it.

        Needed only where copies that were separate homes become one -- a
        rule change, or a twin arriving that joins two groups -- because every
        write the user makes already lands on all copies at once. When the
        copies disagree there is no telling which was said later, so the
        choice is the one that hides nothing: a star over a restore over a
        pass (a note on a copy still listed counts as keeping it). A home
        nobody decided about is left for the archive to judge. Returns the
        rows it moved.
        """
        rows = connection.execute(
            "SELECT id, status, status_reason, note FROM listings WHERE home_key = ?", (key,)
        ).fetchall()
        decided = {str(row["status"]) for row in rows if row["status_reason"] == "user"}
        # A note on a copy still on the list is the user still considering
        # it -- the archive treats it so too -- and outranks a pass.
        noted = any(str(row["note"] or "").strip() and row["status"] == "active" for row in rows)
        if not (decided or noted) or len(rows) < 2:
            return []
        status = next(
            value for value in ("saved", "active", "dismissed")
            if value in decided or (value == "active" and noted)
        )
        # Kept on the list by a note alone, the home is not thereby a decision:
        # clear the note and it is an ordinary home again, one the archive
        # can age out.
        reason = None if status == "active" and "active" not in decided else "user"
        connection.execute(
            "UPDATE listings SET status = ?, status_reason = ? "
            "WHERE home_key = ? AND (status <> ? OR COALESCE(status_reason, '') <> COALESCE(?, ''))",
            (status, reason, key, status, reason),
        )
        return [
            int(row["id"]) for row in rows
            if row["status"] != status or (row["status_reason"] or "") != (reason or "")
        ]

    # The one row of ``scoring_state`` that says what produced the stored scores.
    SCORING_MARK = "rescore_fingerprint"

    def scoring_mark(self) -> str | None:
        """What the stored scores were computed from, or None if nothing says.

        Never raises: a database from before this table existed, an unreadable
        one, anything at all -- the answer is "nothing is known", and the caller
        scores the board again rather than trusting it.
        """
        try:
            with self.connection() as connection:
                row = connection.execute(
                    "SELECT value FROM scoring_state WHERE name = ?", (self.SCORING_MARK,)
                ).fetchone()
        except sqlite3.Error:
            return None
        return str(row["value"]) if row is not None else None

    def set_scoring_mark(self, connection: sqlite3.Connection, value: str | None) -> None:
        """Record what produced the scores being written, on the caller's transaction.

        Takes the connection rather than opening one so the caller decides when
        it lands. A pass is not a single transaction -- ``_update_score``
        commits each listing as it goes -- so the caller retracts the mark
        before rewriting anything and records it again only once every row is
        written. Between those two points the board carries no claim, which is
        what a board being rewritten deserves.

        ``None`` clears it, which is what a build that cannot fingerprint itself
        should leave behind: no claim rather than a stale one.
        """
        if value is None:
            connection.execute("DELETE FROM scoring_state WHERE name = ?", (self.SCORING_MARK,))
            return
        connection.execute(
            "INSERT INTO scoring_state (name, value) VALUES (?, ?) "
            "ON CONFLICT(name) DO UPDATE SET value = excluded.value",
            (self.SCORING_MARK, value),
        )

    def integrity_check(self) -> tuple[bool, str]:
        """Return SQLite's bounded integrity verdict for support diagnostics.

        On a connection of its own: a verdict on the file as it is now.
        """
        try:
            with self.connection(reuse=False) as connection:
                row = connection.execute("PRAGMA integrity_check(1)").fetchone()
        except sqlite3.Error as exc:
            return False, f"{type(exc).__name__}: {exc}"
        verdict = str(row[0] if row is not None else "No result")
        return verdict.casefold() == "ok", verdict

    def schema_version(self) -> int:
        try:
            with self.connection(reuse=False) as connection:
                row = connection.execute("PRAGMA user_version").fetchone()
        except sqlite3.Error:
            return -1
        return int(row[0]) if row is not None else 0

    def find_listing(
        self,
        platform: str,
        source_id: str,
        url: str,
        listing: ListingCandidate | None = None,
    ) -> sqlite3.Row | None:
        """The stored row a card is a copy of, decided as ``upsert_listing`` decides.

        The scan reads this to carry what is stored onto the card before
        storing it, so it must answer exactly as the write will: a stored row
        the card only shares an id with, and is not, is not returned, or its
        address and summary would be carried onto a different home.
        """
        if listing is not None:
            # Classified as the write will classify it, so the size compared
            # here is the size the write will compare.
            classified = classify_listing(listing)
            incoming = facts(
                classified.platform, classified.source_id, classified.original_url,
                classified.metadata, classified.title, classified.unit_type,
            )
        else:
            incoming = facts(platform, source_id, url, None, None)
        with self.connection() as connection:
            target, _ = self._resolve(connection, platform, source_id, canonicalize_url(url), incoming)
            return target

    def _resolve(
        self,
        connection: sqlite3.Connection,
        platform: str,
        source_id: str,
        canonical: str,
        incoming: Any,
    ) -> tuple[sqlite3.Row | None, list[sqlite3.Row]]:
        """The row a card updates, if any, and every row holding its id or page.

        A site's id and URL name a slot, not a home: Zumper re-lets one URL to
        each flat in turn, a building card features a new unit every week.
        So a row found by id or URL is the card's only if it is still the same
        home (``listing_identity.same_home``) -- judged strictly for a row the
        user has starred, noted or passed, whose star must never move to a
        different flat. A row set aside earlier because its id was reused
        (detached) is a candidate too, so a slot that alternates between two
        flats keeps two rows instead of minting a new one each time.
        """
        candidates: list[sqlite3.Row] = []
        seen: set[int] = set()

        def consider(row: sqlite3.Row | None) -> None:
            if row is not None and int(row["id"]) not in seen:
                seen.add(int(row["id"]))
                candidates.append(row)

        consider(connection.execute(
            "SELECT * FROM listings WHERE platform = ? AND source_id = ?", (platform, source_id)
        ).fetchone())
        consider(connection.execute(
            "SELECT * FROM listings WHERE canonical_url = ?", (canonical,)
        ).fetchone())
        # A detached row is "<id>#<row id>": the ranges below are every string
        # that starts "<id>#", served from the unique indexes.
        for row in connection.execute(
            """SELECT * FROM listings
               WHERE (platform = ? AND source_id > ? AND source_id < ?)
                  OR (canonical_url > ? AND canonical_url < ?)
               ORDER BY id DESC LIMIT 20""",
            (platform, f"{source_id}#", f"{source_id}$", f"{canonical}#", f"{canonical}$"),
        ):
            if _is_detached(row) and (
                str(row["source_id"]) == f"{source_id}#{int(row['id'])}" or _attached_url(row) == canonical
            ):
                consider(row)
        same = [
            row
            for row in candidates
            if same_home(
                facts(
                    row["platform"], row["source_id"], row["original_url"],
                    _json_object(row["metadata_json"]), row["title"], row["unit_type"],
                ),
                incoming,
                strict=_owned(row),
                same_url=_attached_url(row) == canonical,
            )
        ]
        target = next((row for row in same if _owned(row)), same[0] if same else None)
        return target, candidates

    def _refresh_identity(
        self,
        connection: sqlite3.Connection,
        listing_id: int,
        *,
        partners_before: Sequence[str] | None = None,
        displaced: bool = False,
    ) -> tuple[str, str]:
        """Recompute one row's home key and copy rank from what it stores.

        ``partners_before`` are the keys its twins had before the write that
        prompted this (by default, the keys they have now); see
        ``_may_follow``. Returns the key it had and the key it has now.
        """
        row = connection.execute(
            """SELECT id, platform, source_id, original_url, title, housing_kind, unit_type, price,
                      metadata_json, score_details_json, published_at, home_key, status, note,
                      status_reason
               FROM listings WHERE id = ?""",
            (listing_id,),
        ).fetchone()
        if row is None:
            return "", ""
        metadata = _json_object(row["metadata_json"])
        twins: list[str] = []
        stored: list[str] = []
        for platform, sid in twin_ids(row["platform"], row["source_id"]):
            twin = connection.execute(
                """SELECT platform, source_id, original_url, title, housing_kind, unit_type, price,
                          metadata_json, home_key
                   FROM listings WHERE platform = ? AND source_id IN (?, ?, ?) LIMIT 1""",
                (platform, *_spellings(sid)),
            ).fetchone()
            if twin is not None:
                twins.append(own_key(
                    twin["platform"], twin["source_id"], twin["original_url"],
                    _json_object(twin["metadata_json"]), twin["title"], twin["housing_kind"],
                    twin["unit_type"], twin["price"],
                ))
                stored.append(str(twin["home_key"] or ""))
        own = own_key(
            row["platform"], row["source_id"], row["original_url"], metadata, row["title"],
            row["housing_kind"], row["unit_type"], row["price"],
        )
        by_id = id_key(row["platform"], row["source_id"], row["unit_type"])
        key = choose_key(own, twins, row["unit_type"], by_id)
        current = str(row["home_key"] or "")
        if not own and current and (_is_detached(row) or current.startswith(MOVED_PREFIX)):
            # Set aside, or moved off a re-let id: frozen with the home it was.
            key = current
        elif key != current and not _may_follow(
            _owned(row), own, current, by_id, stored if partners_before is None else partners_before, displaced
        ):
            key = current
        rank = copy_rank(
            row["platform"], row["source_id"], row["original_url"], metadata, row["price"],
            row["published_at"], _available_on(row["score_details_json"]),
        )
        connection.execute(
            "UPDATE listings SET home_key = ?, copy_rank = ? WHERE id = ?", (key, rank, listing_id)
        )
        return str(row["home_key"] or ""), key

    def _rejoin(self, connection: sqlite3.Connection, listing_id: int, *, displaced: bool = False) -> list[int]:
        """Re-key a written row and its syndicated twins, and settle the homes they join.

        ``displaced`` says the write set aside another home that held this
        id: the site re-let it to a different flat (see ``_may_follow``).
        Returns the rows whose status settling moved (see ``_settle_home``).
        """
        row = connection.execute(
            "SELECT platform, source_id FROM listings WHERE id = ?", (listing_id,)
        ).fetchone()
        if row is None:
            return []
        twins: list[int] = []
        for platform, sid in twin_ids(row["platform"], row["source_id"]):
            twin = connection.execute(
                "SELECT id FROM listings WHERE platform = ? AND source_id IN (?, ?, ?) LIMIT 1",
                (platform, *_spellings(sid)),
            ).fetchone()
            if twin is not None:
                twins.append(int(twin["id"]))
        before, after = self._refresh_identity(connection, listing_id, displaced=displaced)
        changed = [after] if after else []
        for twin_id in twins:
            twin_before, twin_after = self._refresh_identity(
                connection, twin_id, partners_before=[before], displaced=displaced
            )
            if twin_after and twin_after != twin_before:
                changed.append(twin_after)
        settled: list[int] = []
        for key in dict.fromkeys(changed):
            settled.extend(self._settle_home(connection, key))
        return settled

    @classmethod
    def _mark_gone_from_page(cls, connection: sqlite3.Connection, listing_id: int, platform: str) -> None:
        """Record that a flat's own page now shows a different flat.

        The same verdict the recheck reaches when a page names another unit,
        and the one scoring reads as the source saying the listing is
        inactive.
        """
        cls._mark_verified_inactive(
            connection, listing_id, f"{platform} now shows a different home on this listing's page."
        )

    @staticmethod
    def _mark_verified_inactive(connection: sqlite3.Connection, listing_id: int, reason: str) -> bool:
        """Write a source's own proof that a listing is inactive, with its consequences.

        Scoring caps a verified-inactive home at 49 and rules it out, but a
        stored row keeps whatever score it was last given until something
        scores it again. Writing the verdict and its consequences together is
        what makes the home leave the shortlist now rather than at the next
        rescore, and a later pass reaches the same answer over again from the
        metadata this leaves behind. A starred or noted row keeps its star and
        note, and says why it is gone.

        Answers False for a row that is no longer there, so a caller working
        through a list it read a moment ago cannot be taken down by a home
        deleted underneath it.
        """
        row = connection.execute(
            "SELECT metadata_json, score_details_json, eligibility_reasons_json, score FROM listings WHERE id = ?",
            (listing_id,),
        ).fetchone()
        if row is None:
            return False
        metadata = {**_json_object(row["metadata_json"]), "verified_inactive": True, "verification_concern": reason}
        details = _json_object(row["score_details_json"])
        details["hard_constraints"] = [
            *(details.get("hard_constraints") or []),
            {"status": "fail", "check": "listing page", "reason": "The source verified this listing is inactive."},
        ]
        try:
            reasons = json.loads(row["eligibility_reasons_json"] or "[]")
        except ValueError:
            reasons = []
        connection.execute(
            """UPDATE listings
               SET metadata_json = ?, score_details_json = ?, eligibility_reasons_json = ?,
                   eligibility = 'ineligible', score = MIN(score, 49), concern = ?
               WHERE id = ?""",
            (
                json.dumps(metadata, ensure_ascii=False),
                json.dumps(details, ensure_ascii=False),
                json.dumps([*reasons, "The source verified this listing is inactive."], ensure_ascii=False),
                reason,
                listing_id,
            ),
        )
        return True

    @staticmethod
    def _pin_moved_home(connection: sqlite3.Connection, listing_id: int, old_key: str) -> None:
        """Keep a re-let id's old home together under a key the id can never produce.

        A home held together only by a record id (``rentpath:lv...``,
        ``zpid:...``) would otherwise be joined by whatever flat the site
        re-lets the id to, the moment that flat's card is keyed -- and the
        user's star or pass would be carried onto it. The row set aside and
        every copy the user has decided about go to ``moved:<row id>``;
        copies nobody touched follow the record, as it now describes.
        """
        pinned = f"{MOVED_PREFIX}{listing_id}"
        connection.execute("UPDATE listings SET home_key = ? WHERE id = ?", (pinned, listing_id))
        for copy in connection.execute(
            "SELECT id, status, note, status_reason FROM listings WHERE home_key = ? AND id <> ?",
            (old_key, listing_id),
        ).fetchall():
            if _owned(copy):
                connection.execute("UPDATE listings SET home_key = ? WHERE id = ?", (pinned, int(copy["id"])))

    @staticmethod
    def _detach(connection: sqlite3.Connection, listing_id: int) -> None:
        """Set a row aside: its id and page now belong to a different home.

        The row is kept whole -- star, note, rent, everything -- and only stops
        answering to the id, so nothing the site sends later can rewrite it.
        """
        connection.execute(
            """UPDATE listings
               SET source_id = source_id || '#' || id, canonical_url = canonical_url || '#' || id
               WHERE id = ? AND source_id NOT LIKE '%#' || id""",
            (listing_id,),
        )

    def upsert_listing(
        self,
        listing: ListingCandidate,
        result: ScoreResult,
        seen_at: str | None = None,
        *,
        settled: list[int] | None = None,
    ) -> tuple[int, bool]:
        """Store one listing as a search returned it. Returns (id, created).

        ``settled``, when given, receives the other copies whose status the
        write moved: a star or pass the home already carried, now reaching a
        copy this write joined to it (see ``_settle_home``). Their scores are
        the caller's to redo -- one the archive aged out still holds the
        verdict of the deal it left with.
        """
        listing = classify_listing(listing)
        now = seen_at or utc_now()
        canonical = canonicalize_url(listing.original_url)
        reasons = json.dumps(result.reasons, ensure_ascii=False)
        score_details = json.dumps(result.details, ensure_ascii=False)
        eligibility_reasons = json.dumps(result.eligibility_reasons, ensure_ascii=False)
        unknowns = json.dumps(result.unknowns, ensure_ascii=False)
        metadata = json.dumps(listing.metadata, ensure_ascii=False)
        published_at = listing.metadata.get("listing_timestamp")
        if not isinstance(published_at, str) or not published_at.strip():
            published_at = None
        neighborhood = canonical_neighborhood(listing.neighborhood)
        incoming = facts(
            listing.platform, listing.source_id, listing.original_url, listing.metadata, listing.title,
            listing.unit_type,
        )
        with self.connection() as connection:
            # Looked up, decided and written in one transaction taken for
            # writing, so a star landing between the lookup and the write
            # cannot be matched as a row nobody owns.
            connection.execute("BEGIN IMMEDIATE")
            try:
                target, candidates = self._resolve(
                    connection, listing.platform, listing.source_id, canonical, incoming
                )
                platform, source_id = listing.platform, listing.source_id
                if (
                    target is not None
                    and not _is_detached(target)
                    and target["platform"] != listing.platform
                ):
                    # Found by its page alone, on another site's row: that row
                    # stays that site's, or the next card from it would find
                    # nothing under its own id and make the home twice.
                    platform, source_id = str(target["platform"]), str(target["source_id"])
                displaced = False
                for row in candidates:
                    if target is not None and int(row["id"]) == int(target["id"]):
                        continue
                    if not _is_detached(row) and (
                        (row["platform"], row["source_id"]) == (platform, source_id)
                        or row["canonical_url"] == canonical
                    ):
                        # In the way of the card: set aside rather than
                        # overwritten, and rather than failing the write on the
                        # unique id or page.
                        self._detach(connection, int(row["id"]))
                        holder = (row["platform"], row["source_id"]) == (platform, source_id)
                        before = facts(
                            row["platform"], row["source_id"], row["original_url"],
                            _json_object(row["metadata_json"]), row["title"], row["unit_type"],
                        )
                        if (
                            before.grain == UNIT_GRAIN
                            and incoming.grain == UNIT_GRAIN
                            and not same_home(before, incoming, strict=False)
                        ):
                            self._mark_gone_from_page(connection, int(row["id"]), listing.platform)
                        if holder and is_id_key(str(row["home_key"] or "")):
                            self._pin_moved_home(connection, int(row["id"]), str(row["home_key"]))
                        self._refresh_identity(connection, int(row["id"]))
                        displaced = displaced or holder
                values = (
                    listing.title,
                    listing.price,
                    neighborhood,
                    listing.listing_type,
                    listing.summary,
                    listing.housing_kind,
                    listing.unit_type,
                    listing.building_units,
                    reasons,
                    result.concern,
                    result.score,
                    result.confidence,
                    result.eligibility,
                    eligibility_reasons,
                    unknowns,
                    score_details,
                    metadata,
                )
                if target is not None:
                    listing_id = int(target["id"])
                    connection.execute(
                        """UPDATE listings SET
                            platform = ?, source_id = ?, canonical_url = ?, title = ?, price = ?,
                            neighborhood = ?, listing_type = ?, summary = ?, housing_kind = ?,
                            unit_type = ?, building_units = ?, match_reasons_json = ?,
                            concern = ?, score = ?, confidence = ?, eligibility = ?,
                            eligibility_reasons_json = ?, unknowns_json = ?, score_details_json = ?,
                            metadata_json = ?, last_seen = ?, published_at = COALESCE(?, published_at),
                            original_url = ?
                           WHERE id = ?""",
                        (
                            platform,
                            source_id,
                            canonical,
                            *values,
                            now,
                            published_at,
                            listing.original_url,
                            listing_id,
                        ),
                    )
                    created = False
                else:
                    cursor = connection.execute(
                        """INSERT INTO listings (
                            platform, source_id, canonical_url, title, price, neighborhood,
                            listing_type, summary, housing_kind, unit_type, building_units,
                            match_reasons_json, concern, score, confidence, eligibility,
                            eligibility_reasons_json, unknowns_json, score_details_json, metadata_json,
                            first_found, last_seen, published_at, original_url
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            platform,
                            source_id,
                            canonical,
                            *values,
                            now,
                            now,
                            published_at,
                            listing.original_url,
                        ),
                    )
                    listing_id = int(cursor.lastrowid)
                    created = True
                # A new copy of a home the user has starred or passed arrives
                # starred or passed: the decision was about the home.
                moved = self._rejoin(connection, listing_id, displaced=displaced)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        if settled is not None:
            settled.extend(moved_id for moved_id in moved if moved_id != listing_id)
        return listing_id, created

    def update_score(
        self,
        listing_id: int,
        result: ScoreResult,
        neighborhood: str | None = None,
        listing: ListingCandidate | None = None,
        *,
        connection: sqlite3.Connection | None = None,
        rekey: bool = True,
    ) -> None:
        """Write one listing's verdict.

        ``connection`` lets a caller rescoring the whole board reuse a single
        connection. Opening and closing one per listing costs an fsync each
        time; on a board of 870 homes that turned a rescore into half a minute
        of disk sync, and start-up blocks on it.

        ``rekey`` False leaves the row's home key for the caller to recompute
        once for the whole board (``refresh_identities``): re-keying one row at
        a time doubled the time a rescore of 8,680 rows took.
        """
        if connection is not None:
            self._update_score(connection, listing_id, result, neighborhood, listing, rekey=rekey)
            return
        with self.connection() as connection:
            self._update_score(connection, listing_id, result, neighborhood, listing, rekey=rekey)

    def _update_score(
        self,
        connection: sqlite3.Connection,
        listing_id: int,
        result: ScoreResult,
        neighborhood: str | None = None,
        listing: ListingCandidate | None = None,
        *,
        rekey: bool = True,
    ) -> None:
        classified = classify_listing(listing) if listing is not None else None
        neighborhood = canonical_neighborhood(neighborhood)
        if classified is not None:
            # The evidence has to be stored with the verdict. Without this a
            # recheck wrote "inactive" into the score and threw away the
            # metadata that said why, so the next rescore read the old
            # metadata and put the home straight back on the shortlist.
            stored = connection.execute(
                "SELECT metadata_json FROM listings WHERE id = ?", (listing_id,)
            ).fetchone()
            merged = json.loads(stored["metadata_json"] or "{}") if stored else {}
            merged.update(classified.metadata)
            connection.execute(
                "UPDATE listings SET metadata_json = ? WHERE id = ?",
                (json.dumps(merged, ensure_ascii=False), listing_id),
            )
        values = (
            result.score,
            json.dumps(result.reasons, ensure_ascii=False),
            result.concern,
            result.confidence,
            result.eligibility,
            json.dumps(result.eligibility_reasons, ensure_ascii=False),
            json.dumps(result.unknowns, ensure_ascii=False),
            json.dumps(result.details, ensure_ascii=False),
        )
        if classified is not None:
            connection.execute(
                """UPDATE listings
                   SET score = ?, match_reasons_json = ?, concern = ?, confidence = ?,
                       eligibility = ?, eligibility_reasons_json = ?, unknowns_json = ?,
                       score_details_json = ?,
                       neighborhood = COALESCE(?, neighborhood), housing_kind = ?, unit_type = ?,
                       building_units = ?
                   WHERE id = ?""",
                (
                    *values,
                    neighborhood,
                    classified.housing_kind,
                    classified.unit_type,
                    classified.building_units,
                    listing_id,
                ),
            )
        elif neighborhood:
            connection.execute(
                """UPDATE listings
                   SET score = ?, match_reasons_json = ?, concern = ?, confidence = ?,
                       eligibility = ?, eligibility_reasons_json = ?, unknowns_json = ?,
                       score_details_json = ?,
                       neighborhood = ?
                   WHERE id = ?""",
                (*values, neighborhood, listing_id),
            )
        else:
            connection.execute(
                """UPDATE listings
                   SET score = ?, match_reasons_json = ?, concern = ?, confidence = ?,
                       eligibility = ?, eligibility_reasons_json = ?, unknowns_json = ?,
                       score_details_json = ?
                   WHERE id = ?""",
                (*values, listing_id),
            )
        # A recheck can change what the row says about itself -- its size, its
        # move-in date -- and with it which home it is and how much it tells.
        if rekey:
            self._rejoin(connection, listing_id)
        connection.commit()

    def all_candidates(self) -> list[tuple[int, ListingCandidate]]:
        with self.connection() as connection:
            rows = connection.execute("SELECT * FROM listings").fetchall()
        return [(int(row["id"]), self._row_to_candidate(row)) for row in rows]

    # A row the 21-day archive aged out and nobody has touched since.
    _AGED_OUT = "(status = 'dismissed' AND COALESCE(status_reason, '') = 'aged')"
    # Every copy of a home any copy of which is not aged out: on the shortlist
    # side, starred or passed. What a rescore scores (see ``live_candidates``).
    _IN_PLAY = (
        f"{GROUP_SQL} IN (SELECT {GROUP_SQL} FROM listings "
        "WHERE status IN ('active', 'saved') "
        "OR (status = 'dismissed' AND COALESCE(status_reason, '') <> 'aged'))"
    )

    def live_candidates(self) -> list[tuple[int, ListingCandidate]]:
        """Every copy of every home still in play, for scoring them again.

        In play: any copy on the shortlist side, starred or passed -- anything
        but aged out. A home every copy of which the archive has aged out is
        left with the scores it had when it left the shortlist. Rescoring it
        with the rest was a year of listings scored on the first start after
        every update and on every saved deal: eleven minutes with the
        dashboard unreachable, for homes nobody is shown until they are
        restored -- which scores them again first (``Scanner.rescore_home``).
        """
        with self.connection() as connection:
            rows = connection.execute(f"SELECT * FROM listings WHERE {self._IN_PLAY} ORDER BY id").fetchall()
        return [(int(row["id"]), self._row_to_candidate(row)) for row in rows]

    def count_live_listings(self) -> int:
        """How many rows a rescore scores (``live_candidates``), for the page
        that has to explain the wait."""
        with self.connection() as connection:
            return int(connection.execute(f"SELECT COUNT(*) FROM listings WHERE {self._IN_PLAY}").fetchone()[0])

    def home_rows(self, listing_id: int) -> list[tuple[int, ListingCandidate]]:
        """Every copy of the home a row belongs to, the row itself included."""
        with self.connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM listings WHERE {self._SAME_HOME} ORDER BY id", (listing_id, listing_id)
            ).fetchall()
        return [(int(row["id"]), self._row_to_candidate(row)) for row in rows]

    def home_is_aged_out(self, listing_id: int) -> bool:
        """Whether every copy of this row's home is one the archive aged out --
        so its scores may be from an older deal than the one in hand."""
        with self.connection() as connection:
            row = connection.execute(
                f"SELECT COUNT(*), SUM({self._AGED_OUT}) FROM listings WHERE {self._SAME_HOME}",
                (listing_id, listing_id),
            ).fetchone()
        return bool(row[0]) and int(row[0]) == int(row[1] or 0)

    def shortlist_pool(
        self,
        kinds: Sequence[str] = (),
        ceiling: int = 900,
        strata: int = 4,
        unit_types: Sequence[str] = (),
        *,
        with_copies: bool = False,
    ) -> tuple[list[Any], dict[int, int], bool]:
        """The homes a cut-off would be measured against, whole or sampled.

        One entry per home, as ``(band, listing)``. With ``with_copies`` it is
        every copy of each home taken, as ``(band, home, listing)``: the page
        shows a home when any of its copies suits the deal, so a count made
        from one copy of each could only come in under what the page will show.

        The pool is every home still standing, not every home that suits the
        saved deal. Stored ``eligibility`` is a verdict on the deal as saved --
        a home over that budget, or outside those areas, is written down
        ineligible -- so filtering on it here would answer for the old deal
        while somebody edits a new one. On a real board it threw away 6,442 of
        6,935 homes and pinned the count at 423 however high the draft budget
        went. Whether a home suits the deal in hand is for the scoring of the
        deal in hand to say, which is why the caller rescores what it gets.

        ``housing_kind`` is different and may narrow it: it says what a home
        *is*, not what anybody wants, so it narrows the pool without dating it.
        ``unit_types`` is the same kind of fact -- how many bedrooms the source
        stated. The deal page passes neither any more: a home of no stated
        size, which once had no page and inflated a draft's answer by 133
        homes, is on the Other homes tab now, and the draft's own scoring
        rules out what it does not want.

        Above ``ceiling`` it is sampled instead, in bands of rent crossed with
        bands of the score each home already has.

        Rent does most of the work. Stored score alone was the first attempt,
        on the grounds that the old score and the new one are not independent,
        and it held up everywhere except where it mattered most: on a tight
        budget, where only a small corner of the board clears at all, it read
        230 homes against a true 172. Homes within a narrow rent band nearly
        all clear a given budget or nearly all fail it, so banding on rent
        collapses the guesswork on exactly the question a budget asks. The old
        score is kept as the second axis because it still carries what rent
        cannot: how well a home reads against everything else in a deal.

        Evenly spaced rather than randomly drawn so that a deal asked twice
        gives the same number, instead of flickering between keystrokes that
        changed nothing.

        Returns the listings paired with the band they came from, the true size
        of every band, and whether this is the whole pool or a sample of it.
        """
        clauses = ["status IN ('active', 'saved')"]
        parameters: list[Any] = []
        wanted = [str(kind) for kind in kinds if str(kind)]
        if wanted:
            clauses.append(f"housing_kind IN ({','.join('?' for _ in wanted)})")
            parameters.extend(wanted)
        sizes = [str(value) for value in unit_types if str(value)]
        if sizes:
            # Sizes narrow whole homes only: a room states none, and a deal
            # for rooms and one-bedrooms must not lose its rooms.
            clauses.append(f"(housing_kind <> 'whole_unit' OR unit_type IN ({','.join('?' for _ in sizes)}))")
            parameters.extend(sizes)
        where = " AND ".join(clauses)
        # One copy per home -- the one the page would show it by -- so a flat
        # five sites list is one home in the answer, as it is on the page.
        with self.connection() as connection:
            index = connection.execute(
                f"""SELECT id, score, price FROM (
                        SELECT id, score, price, ROW_NUMBER() OVER (
                            PARTITION BY {GROUP_SQL}
                            ORDER BY status = 'saved' DESC, COALESCE(note, '') <> '' DESC,
                                     {_GONE} ASC, copy_rank DESC, id DESC
                        ) AS copy_number
                        FROM listings WHERE {where}
                    ) WHERE copy_number = 1""",
                parameters,
            ).fetchall()
            # Rent bands by quantile, not by round dollar amounts: San Francisco
            # rents bunch up, and fixed bands would leave one band holding half
            # the board and several holding nothing.
            rents = sorted(int(row["price"]) for row in index if row["price"] is not None)
            cuts = (
                [rents[len(rents) * step // RENT_BANDS] for step in range(1, RENT_BANDS)]
                if len(rents) >= RENT_BANDS
                else []
            )
            width = 100 / max(1, strata)
            bands: dict[int, list[int]] = {}
            for row in index:
                if row["price"] is None:
                    # A home with no rent stated answers a budget differently
                    # from any home that states one, so it gets its own band
                    # rather than being filed with the cheapest.
                    rent_band = RENT_BANDS
                else:
                    rent_band = bisect_right(cuts, int(row["price"]))
                score = max(0, int(row["score"] or 0))
                score_band = min(strata - 1, int(score / width))
                # Carried with the id so the sample can be spread along it.
                bands.setdefault(rent_band * strata + score_band, []).append(
                    (score, int(row["id"]))
                )
            sizes = {band: len(members) for band, members in bands.items()}
            total = len(index)
            exact = total <= ceiling
            if exact:
                picked = [int(row["id"]) for row in index]
            else:
                # Shared out by how big each band is, not equally. Equal shares
                # gave a band of 4,172 homes the same 45 samples as a band of
                # 30, and the big band is most of the answer -- 45 homes
                # standing in for 4,172 left the count wrong by a tenth or more.
                picked = []
                for members in bands.values():
                    # Along the stored score, not along the id. Walking the ids
                    # meant walking the order homes were found in, which says
                    # nothing about how well they suit anybody: a band's sample
                    # could then be drawn from one end of its scores and read
                    # 284 homes at a cut-off of 90 against a true 238. Walking
                    # the score instead spreads every sample evenly across the
                    # band's range, which is what the high cut-offs ask about.
                    ordered = sorted(members)
                    take = min(
                        len(ordered), max(1, round(ceiling * len(ordered) / total))
                    )
                    # Taken from the middle of each step rather than its start.
                    # Starting at the step meant every band's sample began at
                    # its lowest score and never reached its highest, which bent
                    # every high cut-off downwards -- 195 homes at 90 against a
                    # true 238, wrong in the same direction on every deal tried.
                    picked.extend(
                        ordered[(2 * index + 1) * len(ordered) // (2 * take)][1]
                        for index in range(take)
                    )
            if not picked:
                return [], {}, True
            band_of = {
                listing_id: band
                for band, members in bands.items()
                for _, listing_id in members
            }
            rows = connection.execute(
                f"SELECT *, {GROUP_SQL} AS home FROM listings WHERE id IN ({','.join('?' for _ in picked)})",
                picked,
            ).fetchall()
            if not with_copies:
                return (
                    [(band_of[int(row["id"])], self._row_to_candidate(row)) for row in rows],
                    sizes,
                    exact,
                )
            band_of_home = {str(row["home"]): band_of[int(row["id"])] for row in rows}
            keys = [str(row["home_key"]) for row in rows if row["home_key"]]
            copies = list(rows)
            for offset in range(0, len(keys), 500):
                chunk = keys[offset : offset + 500]
                copies.extend(connection.execute(
                    f"SELECT *, {GROUP_SQL} AS home FROM listings WHERE {where} "
                    f"AND home_key IN ({','.join('?' for _ in chunk)}) "
                    f"AND id NOT IN ({','.join('?' for _ in picked)})",
                    [*parameters, *chunk, *picked],
                ).fetchall())
        return (
            [(band_of_home[str(row["home"])], str(row["home"]), self._row_to_candidate(row)) for row in copies],
            sizes,
            exact,
        )

    @staticmethod
    def _row_to_candidate(row: sqlite3.Row) -> ListingCandidate:
        return ListingCandidate(
            platform=row["platform"],
            source_id=row["source_id"],
            title=row["title"],
            original_url=row["original_url"],
            price=row["price"],
            neighborhood=row["neighborhood"],
            listing_type=row["listing_type"],
            summary=row["summary"],
            metadata=json.loads(row["metadata_json"] or "{}"),
            housing_kind=row["housing_kind"] or "room",
            unit_type=row["unit_type"],
            building_units=row["building_units"],
        )

    VALID_UNIT_TYPES = ("studio", "one_bedroom", "two_bedroom", "three_bedroom", "four_bedroom")

    @classmethod
    def _home_shape(cls, within: Sequence[str] = ()) -> str:
        """Every home's shape -- a room, or a whole home of a size -- read from
        the copy that says most about it: one that states a size, if any does.
        Tabs hold homes, not rows, so a flat Zillow calls a one-bedroom and
        Movoto gives no size is on the one-bedroom tab only, and not also on
        the tab for homes of no stated size.

        ``within`` narrows it to the homes some row matching those clauses
        belongs to -- every copy of each, whatever the copy's own status, so a
        home's shape is the same whichever view asks. Shaping every home ever
        stored was a year of listings read on every page, for a shortlist of a
        few hundred.
        """
        narrowed = (
            f" WHERE {GROUP_SQL} IN (SELECT {GROUP_SQL} FROM listings WHERE {' AND '.join(within)})"
            if within
            else ""
        )
        return f"""
        SELECT home, housing_kind AS home_kind, unit_type AS home_size FROM (
            SELECT {GROUP_SQL} AS home, housing_kind, unit_type,
                   ROW_NUMBER() OVER (PARTITION BY {GROUP_SQL} ORDER BY {cls._SHAPE_ORDER}) AS shape_rank
            FROM listings{narrowed}
        ) WHERE shape_rank = 1"""

    @classmethod
    def _tab_clauses(
        cls,
        housing_kind: str,
        unit_type: str = "",
        unit_types: Sequence[str] = (),
        outside_tabs: tuple[bool, Sequence[str]] | None = None,
        within: tuple[Sequence[str], Sequence[Any]] = ((), ()),
    ) -> tuple[list[str], list[Any]]:
        """The homes one tab holds, by what each home is.

        ``housing_kind`` "other" is the tab for homes none of the deal's own
        tabs can hold -- a whole home whose size no source stated, a card that
        could not be told apart from an office -- given as ``outside_tabs``:
        whether rooms have a tab, and which sizes do. Without it 941 scored
        homes on a real board were on no page at all. An empty
        ``housing_kind`` is every home.

        ``within`` is the rest of the caller's WHERE -- its clauses and their
        parameters -- so only the homes it can reach are shaped (see
        ``_home_shape``). Leaving it out shapes every home, which is always
        right and, on a year of listings, slow.
        """
        predicate: list[str] = []
        parameters: list[Any] = []
        if housing_kind == "other":
            rooms, sizes = outside_tabs or (False, ())
            covered = ["(COALESCE(home_kind, '') = 'room' AND ?)"]
            parameters.append(1 if rooms else 0)
            chosen = [value for value in sizes if value in cls.VALID_UNIT_TYPES]
            if chosen:
                # COALESCE, or a home of no stated size makes the whole test
                # NULL and NOT NULL keeps nothing -- exactly the homes this
                # tab exists for.
                covered.append(
                    f"(home_kind = 'whole_unit' AND COALESCE(home_size, '') IN ({','.join('?' for _ in chosen)}))"
                )
                parameters.extend(chosen)
            predicate.append(f"NOT ({' OR '.join(covered)})")
        else:
            if housing_kind:
                predicate.append("home_kind = ?")
                parameters.append(housing_kind)
            selected = (
                (unit_type,)
                if unit_type in cls.VALID_UNIT_TYPES
                else tuple(value for value in unit_types if value in cls.VALID_UNIT_TYPES)
            )
            if selected:
                predicate.append(f"home_size IN ({', '.join('?' for _ in selected)})")
                parameters.extend(selected)
        if not predicate:
            return [], []
        narrowing, narrowing_parameters = within
        return (
            [f"{GROUP_SQL} IN (SELECT home FROM ({cls._home_shape(narrowing)}) WHERE {' AND '.join(predicate)})"],
            [*narrowing_parameters, *parameters],
        )

    @staticmethod
    def _shortlist_clauses(minimum_score: int) -> tuple[list[str], list[Any]]:
        """The active view's own predicate: the shortlist, whatever the tab.

        Two things beyond the score and the deal, and the difference between
        them is the whole of this app's rule about what may hide a home.

        A copy whose own page said it is gone leaves at once, whatever the
        clock says. Scoring caps such a copy at 49 and that alone kept it off
        the shortlist, but only from the next time something scored it; said
        here, the proof takes effect the moment it is written and cannot be
        undone by a cut-off the reader drags below 49.

        A copy its source has merely stopped returning leaves after
        ``UNSEEN_SHORTLIST_AFTER``, and only for Near matches -- unless the
        user starred or noted it, which no rule about absence may override.
        This is per copy, so a home two sites list is held by whichever of
        them still shows it: one site going quiet about a home the other is
        still advertising says nothing about the home.
        """
        return (
            [
                "score >= ?",
                "status IN ('active', 'saved')",
                "eligibility IN ('eligible', 'needs_verification')",
                f"NOT ({_GONE})",
                f"({_KEPT_BY_USER} OR {_UNSEEN_DAYS} <= ?)",
            ],
            [minimum_score, _as_days(UNSEEN_SHORTLIST_AFTER)],
        )

    def _view_clauses(self, view: str, minimum_score: int) -> tuple[list[str], list[Any]]:
        """The rows one view holds, before the tab and the filters narrow them."""
        clauses: list[str] = []
        parameters: list[Any] = []
        if view == "saved":
            clauses.append("status = 'saved'")
        elif view == "dismissed":
            # What the user passed. A home the archive aged out is not one
            # they passed, and the Archive tab is where it is found.
            clauses.append("status = 'dismissed'")
            clauses.append("COALESCE(status_reason, '') <> 'aged'")
        elif view == "all":
            pass
        elif view == "near_matches":
            clauses.append("status IN ('active', 'saved')")
            # Three ways to nearly match, and no others. A home the deal
            # accepts that fell a few points short of the cut-off; a home
            # ruled out by one thing only, the rent, and only just -- which is
            # the case the score is blind to; or a home short of nothing at
            # all, which its own source has stopped returning.
            #
            # That third one is why this view is where the unseen rule sends
            # homes rather than hiding them. The home is still stored, still
            # ranked among the others by how nearly it matches, still one
            # click from the listing the reader can check for themselves --
            # which is what lets the shortlist be strict about absence without
            # the app ever having to be sure.
            clauses.append(
                "("
                "(eligibility IN ('eligible', 'needs_verification') AND score < ? AND score >= ?)"
                " OR ("
                # Over the rent line by a little, however the deal says so. For
                # a whole home that is a hard rule and the home is ruled out --
                # so it must have missed nothing else. For a room it is a cap
                # on the score instead, which leaves it eligible but sitting
                # below any band drawn around the cut-off.
                "COALESCE(json_extract(score_details_json, '$.price.over_by'), 0) > 0"
                " AND json_extract(score_details_json, '$.price.over_by')"
                " <= json_extract(score_details_json, '$.price.maximum') * ?"
                " AND (eligibility != 'ineligible'"
                "      OR json_array_length(COALESCE(eligibility_reasons_json, '[]')) = 1)"
                ")"
                " OR ("
                # Good enough for the shortlist, and gone quiet. Deliberately
                # not a copy proved gone: that one has left for good and has
                # its own place in the Archive, and putting it here would bury
                # the homes that might still be real under the ones that are
                # not -- which is the mistake Near matches was made to undo.
                "eligibility IN ('eligible', 'needs_verification') AND score >= ?"
                f" AND NOT ({_GONE}) AND {_UNSEEN_DAYS} > ?"
                ")"
                ")"
            )
            parameters.extend(
                [
                    minimum_score,
                    max(0, minimum_score - NEAR_MATCH_MARGIN),
                    NEAR_MATCH_OVER_BUDGET,
                    minimum_score,
                    _as_days(UNSEEN_SHORTLIST_AFTER),
                ]
            )
            # Nor is a home near a match when another copy of it is on the
            # shortlist already: it is on the shortlist. (A home is on one
            # tab only, so its own tab's shortlist is the only one it can be
            # on.)
            shortlist, shortlist_parameters = self._shortlist_clauses(minimum_score)
            clauses.append(
                f"{GROUP_SQL} NOT IN (SELECT {GROUP_SQL} FROM listings WHERE {' AND '.join(shortlist)})"
            )
            parameters.extend(shortlist_parameters)
        else:
            shortlist, shortlist_parameters = self._shortlist_clauses(minimum_score)
            clauses.extend(shortlist)
            parameters.extend(shortlist_parameters)
        return clauses, parameters

    def has_homes(
        self,
        minimum_score: int,
        view: str,
        housing_kind: str,
        outside_tabs: tuple[bool, Sequence[str]] | None = None,
    ) -> bool:
        """Whether a tab holds anything in a view, without reading its rows."""
        clauses, parameters = self._view_clauses(view, int(minimum_score))
        tab_clauses, tab_parameters = self._tab_clauses(
            housing_kind, "", (), outside_tabs, within=(clauses, parameters)
        )
        where = " AND ".join([*clauses, *tab_clauses]) or "1"
        key = ("has_homes", int(minimum_score), view, housing_kind, _tabs_key(outside_tabs))
        with self.connection() as connection:
            return self._remembered(
                connection,
                key,
                lambda: connection.execute(
                    f"SELECT 1 FROM listings WHERE {where} LIMIT 1", [*parameters, *tab_parameters]
                ).fetchone() is not None,
            )

    def query_listings(
        self,
        minimum_score: int = 60,
        sort: str = "score",
        neighborhood: str = "",
        platform: str = "",
        home_style: str = "",
        housing_kind: str = "room",
        unit_type: str = "",
        unit_types: tuple[str, ...] = (),
        view: str = "active",
        area_priority: str = "",
        outside_tabs: tuple[bool, Sequence[str]] | None = None,
    ) -> list[dict[str, Any]]:
        """One row per home: the copy that says most, and a second where it disagrees.

        Rows are filtered one copy at a time -- a home appears when any of its
        copies belongs here -- and then each home is shown once, by the copy
        the user has starred or noted if any, else by the copy that tells the
        reader most (``copy_rank``). The home sorts by the least favourable
        score among its copies here, so a site quoting a teaser rent cannot
        lift a flat above where its own other listing puts it. A second copy
        is attached (``other_copy``) only when it disagrees about something a
        reader would act on; three identical listings of one flat are one row.
        """
        return self.query_page(
            minimum_score=minimum_score,
            sort=sort,
            neighborhood=neighborhood,
            platform=platform,
            home_style=home_style,
            housing_kind=housing_kind,
            unit_type=unit_type,
            unit_types=unit_types,
            view=view,
            area_priority=area_priority,
            outside_tabs=outside_tabs,
        ).rows

    def query_page(
        self,
        minimum_score: int = 60,
        sort: str = "score",
        neighborhood: str = "",
        platform: str = "",
        home_style: str = "",
        housing_kind: str = "room",
        unit_type: str = "",
        unit_types: tuple[str, ...] = (),
        view: str = "active",
        area_priority: str = "",
        outside_tabs: tuple[bool, Sequence[str]] | None = None,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> ListingPage:
        """``query_listings``, a page at a time: ``limit`` homes from ``offset``.

        Every order ends in the row id, so the pages of a view neither repeat
        nor skip a home however many share a score or a second. Only the
        page's homes are turned into rows and given their copies: the Archive
        of a year was 183MB of HTML, 20 seconds and 1.2GB of memory in one
        page. ``total`` is how many homes the whole view holds.
        """
        clauses, parameters = self._view_clauses(view, minimum_score)
        if neighborhood:
            clauses.append("neighborhood = ? COLLATE NOCASE")
            parameters.append(canonical_neighborhood(neighborhood))
        if platform:
            clauses.append("platform = ? COLLATE NOCASE")
            parameters.append(platform)
        home_style_terms = {
            "house": ("house",),
            "shared_flat": ("apartment", "flat"),
            "townhouse": ("townhouse", "town house"),
            "victorian": ("victorian",),
            "condo": ("condo",),
        }
        if home_style in home_style_terms:
            visible_text = "LOWER(COALESCE(listing_type, '') || ' ' || COALESCE(title, '') || ' ' || COALESCE(summary, ''))"
            style_clauses = [f"{visible_text} LIKE ?" for _ in home_style_terms[home_style]]
            clauses.append("(" + " OR ".join(style_clauses) + ")")
            parameters.extend(f"%{term}%" for term in home_style_terms[home_style])
        if area_priority == "dream_strong":
            clauses.append(
                "json_extract(score_details_json, '$.neighborhood.priority') IN ('dream', 'strong')"
            )
        elif area_priority == "secondary":
            clauses.append("json_extract(score_details_json, '$.neighborhood.priority') = 'secondary'")
        # Last, so only homes the rest of the WHERE can reach are shaped.
        tab_clauses, tab_parameters = self._tab_clauses(
            housing_kind, unit_type, unit_types, outside_tabs, within=(list(clauses), list(parameters))
        )
        clauses.extend(tab_clauses)
        parameters.extend(tab_parameters)
        # Homes still showing up, then homes that have gone quiet
        # (``UNSEEN_DEMOTE_AFTER``), and the recommendation order unchanged
        # inside each group -- so a home that stopped appearing yesterday
        # sinks below every home somebody can still go and see, without the
        # ranking the rest of the app spends its time on being thrown away.
        # Only on the recommended order: the other sorts each answer a
        # question the reader asked in so many words, and "cheapest first"
        # that puts a dearer home first is a broken sort, not a careful one.
        still_showing_first = f"(COALESCE(home_unseen_days, 0) > {_as_days(UNSEEN_DEMOTE_AFTER)}) ASC"
        # Then homes whose rent the app believes, ahead of homes priced below
        # anything their size lets for that nobody has opened the page of
        # (``_CHEAP_RENT_UNCONFIRMED``). Second, because "can I still go and
        # see it" comes before "is that rent real" -- and a demotion either
        # way, never a removal: a $1,125 three-bedroom is exactly what the
        # reader is hunting for and some of them are real, so it keeps its
        # score, keeps its place on the shortlist and says on the card what
        # has not been checked.
        believable_rents_first = "(COALESCE(home_cheap_rent_unconfirmed, 0) = 1) ASC"
        order_by = {
            "score": f"{still_showing_first}, {believable_rents_first}, home_score DESC, score DESC, confidence DESC, COALESCE(published_at, first_found) DESC",
            # By the dearest live quote: a cheaper copy is not a cheaper home.
            "price": "rank_price IS NULL, rank_price ASC, score DESC",
            # The column reads "Posted", so newest must mean newest posted.
            # Sources that publish no date fall back to when we found it.
            "newest": "COALESCE(published_at, first_found) DESC, score DESC",
            # Soonest stated move-in first, homes stating none last: "0" and
            # the date, or "1" -- one reading of the JSON per row.
            "available": f"COALESCE('0' || NULLIF({_AVAILABLE_ON}, ''), '1'), score DESC, first_found ASC",
            # A click is recorded on every outbound listing link. Keep untouched
            # homes at the top, then preserve the normal recommendation order
            # within each group so the sort stays useful rather than arbitrary.
            "unopened": "home_opened_at IS NOT NULL ASC, score DESC, first_found DESC",
            # Ordered by score here and re-ordered by how near each home came
            # once the rows are in hand: the gap lives in JSON no ORDER BY can
            # reach cheaply, and sorting in Python afterwards is stable, so
            # homes that missed by the same amount keep the order they had
            # rather than shuffling between one refresh and the next.
            "closeness": "score DESC, confidence DESC, COALESCE(published_at, first_found) DESC",
            # A direct application is the shortest route to a real response;
            # a direct lister page is next best. This only changes display
            # order, never the matching score or the user's safeguards.
            "contact": f"{_CONTACT_RANK}, score DESC",
        }.get(sort, "first_found DESC, score DESC")
        # Only where it means something, and only on a view the deal bounds:
        # the near misses are a few of the shortlist's own homes.
        by_closeness = sort == "closeness" and view == "near_matches"
        paged = limit is not None and not by_closeness
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        # The window runs over narrow rows and joins the chosen copy back:
        # carrying every JSON column through it cost a second and a half on
        # the Archive tab of a real board.
        homes = f"""
            WITH picked AS (
                SELECT id AS picked_id, {GROUP_SQL} AS home,
                       status = 'saved' AS starred, COALESCE(note, '') <> '' AS noted,
                       {_GONE} AS gone, eligibility = 'ineligible' AS outside, {_STALE} AS stale,
                       copy_rank AS picked_rank,
                       score AS picked_score, opened_at AS picked_opened_at,
                       {_UNSEEN_DAYS} AS picked_unseen_days,
                       ({_CHEAP_RENT_UNCONFIRMED}) AS picked_cheap_rent
                FROM listings{where}
            ),
            ranked AS (
                -- The copy a home is shown by: the user's own first, then one
                -- still listed, inside the deal, and confirmed lately -- so a
                -- starred home reads the same in Saved as on the shortlist --
                -- and only then the one that says most.
                SELECT picked_id, home,
                       ROW_NUMBER() OVER (
                           PARTITION BY home
                           ORDER BY starred DESC, noted DESC, gone ASC, outside ASC, stale ASC,
                                    picked_rank DESC, picked_id DESC
                       ) AS copy_number,
                       MIN(picked_score) OVER (PARTITION BY home) AS picked_home_score,
                       MAX(picked_opened_at) OVER (PARTITION BY home) AS home_opened_at,
                       -- A home is still showing up while any of its copies
                       -- is, so the freshest copy speaks for the home.
                       MIN(picked_unseen_days) OVER (PARTITION BY home) AS home_unseen_days,
                       -- And one copy priced like the rest of the market
                       -- answers for the home: the shortlist already ranks a
                       -- home by its dearest live quote, so it is not being
                       -- recommended on the strength of the cheap copy and
                       -- must not be demoted for it either.
                       MIN(picked_cheap_rent) OVER (PARTITION BY home) AS home_cheap_rent_unconfirmed
                FROM picked
            ),
            -- Every live copy's verdict, not only the copies this view kept:
            -- a dearer quote that fails the deal is exactly the one that
            -- must hold the home down.
            -- A copy that only leaves the rent or the size unstated scores
            -- low for saying less, which is no claim against the home: the
            -- copies that state them are the ones that may hold it down.
            live AS (
                SELECT {GROUP_SQL} AS live_home,
                       COALESCE(
                           MIN(CASE WHEN NOT {_STALE} AND price IS NOT NULL
                                     AND (unit_type IS NOT NULL OR housing_kind <> 'whole_unit')
                                    THEN score END),
                           MIN(CASE WHEN NOT {_STALE} THEN score END)
                       ) AS live_score,
                       MAX(CASE WHEN NOT {_STALE} THEN price END) AS fresh_price,
                       MAX(price) AS live_price
                FROM listings
                -- The homes this view holds, not every home ever stored: a
                -- JSON read per row of a year of listings, for a page of them.
                WHERE NOT {_GONE} AND {GROUP_SQL} IN (SELECT home FROM ranked WHERE copy_number = 1)
                GROUP BY live_home
            )"""
        select = f"""
            SELECT listings.*, ranked.home, ranked.copy_number, ranked.home_opened_at,
                   ranked.home_unseen_days, ranked.home_cheap_rent_unconfirmed,
                   live.live_price AS home_price,
                   COALESCE(live.fresh_price, live.live_price, listings.price) AS rank_price,
                   COALESCE(live.live_score, ranked.picked_home_score) AS home_score,
                   COUNT(*) OVER () AS homes_in_view
            FROM ranked
            JOIN listings ON listings.id = ranked.picked_id
            LEFT JOIN live ON live.live_home = ranked.home
            WHERE ranked.copy_number = 1 ORDER BY {order_by}, listings.id DESC"""
        window = [int(limit), max(0, int(offset))] if paged else []
        with self.connection() as connection:
            rows = connection.execute(
                homes + select + (" LIMIT ? OFFSET ?" if paged else ""), [*parameters, *window]
            ).fetchall()
            if rows:
                total = int(rows[0]["homes_in_view"])
            elif paged and offset > 0:
                # Past the last page: still say how many there are.
                total = int(connection.execute(
                    homes + " SELECT COUNT(*) FROM ranked WHERE copy_number = 1", parameters
                ).fetchone()[0])
            else:
                total = 0
            copies = self._other_copies(connection, rows)
        items = []
        for row in rows:
            item = self._dashboard_row(row)
            for key in ("home", "copy_number", "homes_in_view"):
                item.pop(key, None)
            # Having read the flat on one site is having read it.
            item["opened_at"] = item.pop("home_opened_at", None) or item.get("opened_at")
            # How long the home has gone unseen, so the page can rank it, sort
            # it by how nearly it still matches, and say why it moved. Worked
            # out per home by the query above rather than per row here,
            # because it is a question about every copy of the home and about
            # its source's history, neither of which a single row holds.
            item["unseen_days"] = float(item.pop("home_unseen_days", 0.0) or 0.0)
            item["unseen_too_long"] = item["unseen_days"] > _as_days(UNSEEN_SHORTLIST_AFTER)
            item["cheap_rent_unconfirmed"] = bool(item.pop("home_cheap_rent_unconfirmed", 0))
            # Whether the home's own source is still returning it
            # (``UNSEEN_DEMOTE_AFTER``). The ranking below already acts on
            # this; the price column says it in words, because a rent from a
            # listing that has stopped appearing is the last rent anybody
            # published rather than one the reader can turn up and pay. One
            # line of truth for both, so the marker can never contradict the
            # order the rows are in.
            item["still_listed_by_source"] = item["unseen_days"] <= _as_days(UNSEEN_DEMOTE_AFTER)
            # The groups the ORDER BY above put this home in, carried on the
            # row so that whatever re-ranks these afterwards can keep them.
            # One pass does: ``rent_estimate.ranking_order`` gives an unpriced
            # home a place by an estimated rent, and re-sorting on score alone
            # threw both demotions away -- seven homes on the author's board
            # state no rent, so that pass runs on every page load of it.
            item["rank_group"] = (
                0 if item["still_listed_by_source"] else 1,
                1 if item["cheap_rent_unconfirmed"] else 0,
            )
            self._attach_copies(item, row, copies.get(str(row["home"]), []))
            items.append(item)
        if by_closeness:
            # Only where it means something. Asked for anywhere else -- a stale
            # link, or the control left on it while switching tabs -- the rows
            # keep the score order the query already gave them.
            items.sort(key=lambda item: near_miss_order(item, minimum_score))
            if limit is not None:
                items = items[max(0, int(offset)) : max(0, int(offset)) + int(limit)]
        return ListingPage(rows=items, total=total)

    def _other_copies(
        self, connection: sqlite3.Connection, rows: Sequence[sqlite3.Row]
    ) -> dict[str, list[sqlite3.Row]]:
        """Every copy of the homes on a page, other than the copy each is shown by."""
        keys = sorted({str(row["home_key"]) for row in rows if row["home_key"]})
        shown = {int(row["id"]) for row in rows}
        found: dict[str, list[sqlite3.Row]] = {}
        for offset in range(0, len(keys), 500):
            chunk = keys[offset : offset + 500]
            for copy in connection.execute(
                f"SELECT * FROM listings WHERE home_key IN ({','.join('?' for _ in chunk)})", chunk
            ):
                if int(copy["id"]) not in shown:
                    found.setdefault(str(copy["home_key"]), []).append(copy)
        return found

    def _attach_copies(self, item: dict[str, Any], row: sqlite3.Row, others: list[sqlite3.Row]) -> None:
        """Say what the home's other copies add: a disagreement, and any note.

        The second copy is chosen by what it would change for the reader, in
        this order: one copy says the home is gone or outside the deal and the
        other does not; the rents differ (or only the other states one); the
        other states a posting or move-in date this one lacks. A copy that
        adds none of those is not shown -- though its site is still named, and
        its note always is, because a note is the user's own writing.
        """
        item["copy_count"] = 1 + len(others)
        item["other_platforms"] = sorted({str(other["platform"]) for other in others} - {str(row["platform"])})
        item["other_notes"] = [
            {"platform": str(other["platform"]), "note": str(other["note"])}
            for other in others
            if str(other["note"] or "").strip()
        ]
        item["other_copy"] = None
        if not others:
            return
        mine = item["metadata"]
        available = item.get("available_on")

        def tier(other: sqlite3.Row) -> int | None:
            metadata = _json_object(other["metadata_json"])
            gone = metadata.get("verified_inactive") is True
            if gone != (mine.get("verified_inactive") is True):
                return 0
            if _confirmed_long_ago(other):
                # A copy no search has returned lately is no evidence about
                # the rent, the dates or the deal now; only a site saying the
                # home is gone is worth showing from it.
                return None
            if (other["eligibility"] == "ineligible") != (item["eligibility"] == "ineligible"):
                return 0
            if other["price"] is not None and other["price"] != item["price"]:
                return 1
            if (other["published_at"] and not item.get("published_at")) or (
                _available_on(other["score_details_json"]) and not available
            ):
                return 2
            return None

        ranked = sorted(
            ((tier(other), -int(other["copy_rank"] or 0), int(other["id"]), other) for other in others),
            key=lambda entry: (entry[0] is None, entry[0] or 0, entry[1], entry[2]),
        )
        best = ranked[0]
        if best[0] is None:
            return
        copy = self._dashboard_row(best[3])
        copy["disagreement"] = ("status", "price", "dates")[best[0]]
        copy["verified_inactive"] = copy["metadata"].get("verified_inactive") is True
        # Which side the disagreement is on: the copy shown may be the one a
        # site has taken down (a noted copy is shown first even then).
        copy["shown_inactive"] = mine.get("verified_inactive") is True
        item["other_copy"] = copy

    def rent_observations(self, window_days: int, *, now: str | None = None) -> list[tuple[str | None, str | None, int]]:
        """(neighbourhood, size, rent) for every priced home seen in the window.

        One entry per home, at the highest rent any copy quotes. Size is
        "room" for a room and the stated size for a whole home.

        The window's rows are gathered first, through the index on when a
        row was last seen (a day's margin for stamps written with an offset;
        ``julianday`` still decides), and only then grouped into homes:
        grouped straight from the table it read a year of listings to find
        sixty days of them, half a second on every page after a change.
        """
        moment = now or utc_now()
        try:
            earliest = (
                datetime.fromisoformat(moment) - timedelta(days=int(window_days) + 1)
            ).isoformat(timespec="seconds")
        except ValueError:
            earliest = ""
        with self.connection() as connection:
            rows = connection.execute(
                f"""WITH seen AS MATERIALIZED (
                        SELECT neighborhood, housing_kind, unit_type, price, {GROUP_SQL} AS home
                        FROM listings
                        WHERE last_seen >= ?
                          AND price IS NOT NULL AND price > 0
                          AND julianday(last_seen) >= julianday(?) - ?
                    )
                    SELECT MAX(neighborhood) AS neighborhood,
                           MAX(CASE WHEN housing_kind = 'room' THEN 'room'
                                    WHEN housing_kind = 'whole_unit' THEN unit_type END) AS size,
                           MAX(price) AS price
                    FROM seen GROUP BY home""",
                (earliest, moment, int(window_days)),
            ).fetchall()
        return [(row["neighborhood"], row["size"], int(row["price"])) for row in rows]

    def is_owned(self, listing_id: int) -> bool:
        """Whether the user has starred, noted, passed or restored this row."""
        with self.connection() as connection:
            row = connection.execute(
                "SELECT status, note, status_reason FROM listings WHERE id = ?", (listing_id,)
            ).fetchone()
        return row is not None and _owned(row)

    def home_candidates(self, listing_ids: Sequence[int]) -> dict[int, list[ListingCandidate]]:
        """Each listing's home as stored listings: the listing first, then every
        other copy still listed (none a site says is gone)."""
        wanted = [int(value) for value in listing_ids]
        found: dict[int, list[ListingCandidate]] = {}
        with self.connection() as connection:
            for offset in range(0, len(wanted), 500):
                chunk = wanted[offset : offset + 500]
                subjects = connection.execute(
                    f"SELECT * FROM listings WHERE id IN ({','.join('?' for _ in chunk)})", chunk
                ).fetchall()
                for subject in subjects:
                    home = [self._row_to_candidate(subject)]
                    if subject["home_key"]:
                        home.extend(
                            self._row_to_candidate(copy)
                            for copy in connection.execute(
                                f"SELECT * FROM listings WHERE home_key = ? AND id <> ? AND NOT {_GONE}",
                                (subject["home_key"], int(subject["id"])),
                            )
                        )
                    found[int(subject["id"])] = home
        return found

    def candidates(self, listing_ids: Sequence[int]) -> dict[int, ListingCandidate]:
        """The stored listings behind some rows, for scoring them again."""
        wanted = [int(value) for value in listing_ids]
        found: dict[int, ListingCandidate] = {}
        with self.connection() as connection:
            for offset in range(0, len(wanted), 500):
                chunk = wanted[offset : offset + 500]
                for row in connection.execute(
                    f"SELECT * FROM listings WHERE id IN ({','.join('?' for _ in chunk)})", chunk
                ):
                    found[int(row["id"])] = self._row_to_candidate(row)
        return found

    def shortlisted_absent_from_search(
        self,
        platform: str,
        seen_source_ids: set[str],
        minimum_score: int,
        *,
        limit: int = 8,
        recheck_after: timedelta = timedelta(hours=20),
        now: datetime | None = None,
    ) -> list[tuple[int, ListingCandidate]]:
        """Homes still on the shortlist that this source has stopped listing.

        A listing is enriched once, when it is first collected, and never looked
        at again, so a room that was verified live on Monday and taken down on
        Wednesday stays on the shortlist looking exactly as current as one posted
        this morning. Absence from one search is not proof -- a source returns
        one page and an older post falls off it -- so these are candidates to go
        and check, not homes to mark gone.

        Oldest-checked first, so attention rotates rather than landing on the
        same few every scan.
        """
        moment = (now or datetime.now(UTC)).astimezone(UTC)
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM listings WHERE platform = ? AND status IN ('active', 'saved') "
                "AND eligibility != 'ineligible' AND score >= ? ORDER BY score DESC",
                (platform, int(minimum_score)),
            ).fetchall()

        candidates: list[tuple[float, int, ListingCandidate]] = []
        for row in rows:
            if str(row["source_id"]) in seen_source_ids:
                continue
            metadata = json.loads(row["metadata_json"] or "{}")
            checked = metadata.get("last_verified_at")
            age = None
            if isinstance(checked, str):
                try:
                    age = moment - datetime.fromisoformat(checked).astimezone(UTC)
                except ValueError:
                    age = None
                if age is not None and age < recheck_after:
                    continue
            candidates.append((
                age.total_seconds() if age is not None else float("inf"),
                int(row["id"]),
                self._row_to_candidate(row),
            ))
        candidates.sort(key=lambda item: (-item[0], -item[1]))
        return [(listing_id, candidate) for _, listing_id, candidate in candidates[:limit]]

    def cheap_homes_to_confirm(
        self,
        platform: str,
        minimum_score: int,
        *,
        limit: int,
        confirm_after: timedelta,
        now: datetime | None = None,
    ) -> list[tuple[int, ListingCandidate]]:
        """Shortlisted homes of one source priced below the floor for their size.

        The homes the scanner should go and open before recommending them: on
        the shortlist, so about to appear near the top, and priced below
        anything a home that size lets for. Candidates to confirm, never homes
        to mark gone -- absence proves nothing here and this pass never writes
        anything by itself.

        A home whose page was read within ``confirm_after`` is left out, and
        the rest come oldest-read first, so attention rotates instead of
        landing on the same few every scan and a page nobody has ever opened
        goes first.

        Only what SQL can decide cheaply. Whether the rent is also far below
        what the neighbourhood asks is the caller's question, because the
        answer lives in a table of medians rather than in the row.
        """
        moment = (now or datetime.now(UTC)).astimezone(UTC)
        with self.connection() as connection:
            rows = connection.execute(
                f"""SELECT * FROM listings
                    WHERE platform = ?
                      AND status IN ('active', 'saved')
                      AND eligibility != 'ineligible'
                      AND score >= ?
                      AND NOT ({_GONE})
                      AND COALESCE(
                            json_extract(score_details_json, '$.price.implausibly_low'), 0) = 1
                    ORDER BY score DESC""",
                (platform, int(minimum_score)),
            ).fetchall()

        candidates: list[tuple[float, int, ListingCandidate]] = []
        for row in rows:
            read = _json_object(row["metadata_json"]).get("page_verified_at")
            age = None
            if isinstance(read, str):
                try:
                    age = moment - datetime.fromisoformat(read).astimezone(UTC)
                except ValueError:
                    age = None
                if age is not None and age < confirm_after:
                    continue
            candidates.append((
                age.total_seconds() if age is not None else float("inf"),
                int(row["id"]),
                self._row_to_candidate(row),
            ))
        candidates.sort(key=lambda item: (-item[0], -item[1]))
        return [(listing_id, candidate) for _, listing_id, candidate in candidates[: max(0, int(limit))]]

    def exclusion_summary(
        self,
        minimum_score: int,
        housing_kind: str = "room",
        unit_types: tuple[str, ...] = (),
        *,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Why the homes that were collected did not reach the shortlist.

        An empty shortlist with nothing else on the page reads as a broken
        search. Almost always the search worked and the deal is narrow, so this
        counts the leading reason each collected home was held back and lets the
        page say so.

        Read on every shortlist page, and it reads every home on the board's
        active side -- 54% of a page's time on a real board -- so it is
        remembered until the listings next change (see ``_remembered``).
        """
        key = ("exclusion_summary", int(minimum_score), housing_kind, tuple(unit_types), int(limit))
        with self.connection() as connection:
            return self._remembered(
                connection,
                key,
                lambda: self._exclusion_summary(connection, int(minimum_score), housing_kind, unit_types, limit),
            )

    def _exclusion_summary(
        self,
        connection: sqlite3.Connection,
        minimum_score: int,
        housing_kind: str,
        unit_types: Sequence[str],
        limit: int,
    ) -> list[dict[str, Any]]:
        # The homes of this tab, as the tab itself reads them.
        tab, tab_parameters = self._tab_clauses(
            housing_kind, "", unit_types, within=(["status IN ('active', 'saved')"], [])
        )
        # Counted by home, and a home one of whose copies made the shortlist
        # was not held back at all.
        shortlist, shortlist_parameters = self._shortlist_clauses(int(minimum_score))
        # The third way to be held back, beside the deal's rules and its
        # cut-off: nothing about the home at all, only that its source has
        # stopped returning it. Left out, the one explanation of a thin
        # shortlist the reader gets could name every cause but the one that
        # had just moved seventeen homes off a shortlist of a hundred and
        # thirty-three.
        unseen = f"(eligibility IN ('eligible', 'needs_verification') AND score >= ? AND {_UNSEEN_DAYS} > ?)"
        clauses = [
            "status IN ('active', 'saved')",
            *tab,
            f"(eligibility = 'ineligible' OR score < ? OR {unseen})",
            f"{GROUP_SQL} NOT IN (SELECT {GROUP_SQL} FROM listings WHERE {' AND '.join(shortlist)})",
        ]
        parameters: list[Any] = [
            *tab_parameters,
            int(minimum_score),
            int(minimum_score),
            _as_days(UNSEEN_SHORTLIST_AFTER),
            *shortlist_parameters,
        ]
        # A home's copies tied on verdict and score are read oldest row first:
        # the order they always came in (the table's own, which the sort kept),
        # written down now that an index decides how the rows are found.
        sql = (
            f"SELECT {GROUP_SQL} AS home, score, eligibility, score_details_json, "
            f"{_UNSEEN_DAYS} AS unseen_days FROM listings "
            f"WHERE {' AND '.join(clauses)} ORDER BY home, eligibility = 'ineligible', score DESC, id"
        )
        rows = connection.execute(sql, parameters).fetchall()

        counts: dict[str, int] = {}
        counted: set[str] = set()
        days = _as_days(UNSEEN_SHORTLIST_AFTER)
        for row in rows:
            if row["home"] in counted:
                continue
            counted.add(row["home"])
            if (
                row["eligibility"] != "ineligible"
                and int(row["score"] or 0) >= int(minimum_score)
                and float(row["unseen_days"] or 0.0) > days
            ):
                # Named as the absence it is. "Below your cut-off" would be
                # false of a home that cleared it, and "outside your deal"
                # falser still: the deal never turned this one down.
                label = f"not listed by their own source for {days:g} days or more"
            elif row["eligibility"] == "ineligible":
                try:
                    details = json.loads(row["score_details_json"] or "{}")
                except ValueError:
                    details = {}
                blockers = ordered_checks(details.get("hard_constraints"), "fail")
                name = blockers[0]["check"] if blockers and blockers[0]["check"] else ""
                label = CHECK_EXCLUSION_PHRASES.get(name, "outside your deal's limits")
            else:
                label = f"below your {int(minimum_score)} match cut-off"
            counts[label] = counts.get(label, 0) + 1
        ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        return [{"reason": reason, "count": count} for reason, count in ordered[:limit]]

    def shortlist_counts(
        self,
        thresholds: Sequence[int],
        kinds: Sequence[str] = (),
        unit_types: Sequence[str] = (),
    ) -> dict[int, int]:
        """How many homes each cut-off would put on the shortlist.

        The same predicate the active view uses, so the number under the slider
        is the number the reader will actually get. ``kinds`` narrows it to the
        home shapes the deal enables, because the tabs only ever show those: a
        deal for rooms alone counted whole units it would never display, and the
        slider read six higher than the page. One pass over the scores rather
        than one query per stop.

        Two narrowings the active view does and this did not, which is the
        whole of the number disagreeing with the page:

        ``eligibility`` -- a home the deal rules out keeps its stored score and
        is never shown. Counting it put 3,905 ruled-out homes under a cut-off of
        30 on a real board, which read 4,357 against a page holding 319.

        ``unit_types`` -- narrows whole homes to those sizes, for a caller
        counting some tabs only. The deal page passes neither narrowing: since
        the Other homes tab holds every home no size tab does, each shortlisted
        home is on exactly one tab, and the slider counts them all. (Before
        that tab existed, 32 homes of no stated size sat inside the 316 the
        slider claimed while the tabs held 284.) Counted as homes, not rows.

        And the two the shortlist itself applies beyond the score: a copy
        proved gone, and one its source has stopped returning
        (``UNSEEN_SHORTLIST_AFTER``), neither of which the page shows at any
        cut-off. Written out here rather than borrowed from
        ``_shortlist_clauses`` because this counts every cut-off at once and
        so cannot take that predicate's ``score >= ?``; ``tests/`` holds the
        two to agreement.
        """
        clauses = [
            "status IN ('active', 'saved')",
            "eligibility IN ('eligible', 'needs_verification')",
            f"NOT ({_GONE})",
            f"({_KEPT_BY_USER} OR {_UNSEEN_DAYS} <= ?)",
        ]
        parameters: list[Any] = [_as_days(UNSEEN_SHORTLIST_AFTER)]
        wanted = [str(kind) for kind in kinds if str(kind)]
        if wanted:
            clauses.append(f"housing_kind IN ({','.join('?' for _ in wanted)})")
            parameters.extend(wanted)
        sizes = [str(value) for value in unit_types if str(value)]
        if sizes:
            # Sizes narrow whole homes only: a room states none, and a deal
            # for rooms and one-bedrooms must not lose its rooms.
            clauses.append(f"(housing_kind <> 'whole_unit' OR unit_type IN ({','.join('?' for _ in sizes)}))")
            parameters.extend(sizes)
        # Homes, not rows: the page shows one flat once however many sites
        # list it, at the best score any of its copies earns here.
        with self.connection() as connection:
            scores = [
                int(row[0])
                for row in connection.execute(
                    f"SELECT MAX(score) FROM listings WHERE {' AND '.join(clauses)} GROUP BY {GROUP_SQL}",
                    parameters,
                )
            ]
        scores.sort()
        counts: dict[int, int] = {}
        for threshold in thresholds:
            counts[int(threshold)] = len(scores) - bisect_left(scores, int(threshold))
        return counts

    def elsewhere(self, listing_id: int) -> dict[str, Any]:
        """What the other sources say: about this home, and about its building.

        Two different questions, kept apart. ``copies`` are this same home on
        other sites (the rows ``listing_identity`` joined to it), each with its
        own rent -- when they disagree the reader should see both figures.
        ``building_sources`` only names the other sites listing *some* home at
        this street address. That says the building is real; it says nothing
        about this flat's rent, so no rent is offered from it. Offering one
        was the old "Also listed elsewhere", which matched every flat in a
        building by street alone and put a different flat's rent forward as
        this one's figure to check.
        """
        with self.connection() as connection:
            subject = connection.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()
            if subject is None:
                return {"copies": [], "building_sources": []}
            copies = (
                connection.execute(
                    "SELECT * FROM listings WHERE home_key = ? AND id <> ? ORDER BY copy_rank DESC, id",
                    (subject["home_key"], listing_id),
                ).fetchall()
                if subject["home_key"]
                else []
            )
            here = building_of(_json_object(subject["metadata_json"]))
            neighbours: list[sqlite3.Row] = []
            if here is not None:
                neighbours = connection.execute(
                    """SELECT id, platform, source_id, original_url, title, metadata_json, home_key
                       FROM listings
                       WHERE status <> 'dismissed' AND platform <> ? AND id <> ?
                         AND metadata_json LIKE '%"address"%'""",
                    (subject["platform"], listing_id),
                ).fetchall()
        home = {int(row["id"]) for row in copies}
        building: set[str] = set()
        for row in neighbours:
            if int(row["id"]) in home:
                continue
            there = building_of(_json_object(row["metadata_json"]))
            if there is not None and there[0] == here[0] and max(here[1], there[1]) <= min(here[2], there[2]):
                building.add(str(row["platform"]))
        return {
            "copies": [
                {
                    "id": int(row["id"]),
                    "platform": row["platform"],
                    "title": row["title"],
                    "price": row["price"],
                    "original_url": row["original_url"],
                    "status": row["status"],
                    "status_reason": row["status_reason"],
                    "available_on": _available_on(row["score_details_json"]),
                    "published_at": row["published_at"],
                    "verified_inactive": _json_object(row["metadata_json"]).get("verified_inactive") is True,
                    "note": str(row["note"] or ""),
                }
                for row in copies
            ],
            "building_sources": sorted(building, key=str.casefold),
        }

    def listing(self, listing_id: int) -> dict[str, Any] | None:
        """One listing, shaped exactly as a dashboard row.

        The detail page has to agree with the row the reader clicked from, so it
        goes through the same shaping rather than reading the columns again.
        """
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM listings WHERE id = ?", (listing_id,)
            ).fetchone()
        return self._dashboard_row(row) if row is not None else None

    @staticmethod
    def _dashboard_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["match_reasons"] = json.loads(item.pop("match_reasons_json") or "[]")
        item["eligibility_reasons"] = json.loads(item.pop("eligibility_reasons_json") or "[]")
        item["unknowns"] = json.loads(item.pop("unknowns_json") or "[]")
        item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
        application_url = item["metadata"].get("application_url")
        item["application_url"] = (
            application_url
            if isinstance(application_url, str)
            and application_url.startswith(_APPLICATION_PREFIX)
            else None
        )
        item["direct_lister"] = bool(item["metadata"].get("direct_lister")) and item["platform"] == "Listings Project"
        item["contact_rank"] = 0 if item["application_url"] else 1 if item["direct_lister"] else 2
        item["contact_ready"] = item["contact_rank"] < 2
        score_details = json.loads(item.pop("score_details_json") or "{}")
        # What is still unresolved, named. "Needs verification" told the reader
        # that something was unknown but never what, and the one sentence the
        # row did show came from a different selection than the constraints
        # that actually held the listing back.
        constraints = score_details.get("hard_constraints")
        item["checks"] = ordered_checks(constraints, "unknown")
        item["blockers"] = ordered_checks(constraints, "fail")
        # How far past the rent line this one fell, in money. "The monthly
        # price exceeds this path's maximum" is a rule being quoted at
        # somebody; "$120/mo over your budget" is a number they can decide
        # about, and it is the only thing separating a home worth a look from
        # one at four times the budget, since both score the same.
        price_detail = score_details.get("price") or {}
        over_by = price_detail.get("over_by") or 0
        item["over_budget_by"] = int(over_by)
        item["budget_maximum"] = int(price_detail.get("maximum") or 0)
        # How long since anyone confirmed the source still lists this home. A
        # score says how well it fits; it says nothing about whether the home is
        # still there, and a home nobody has confirmed for a day is not one the
        # app can vouch for. Computed here rather than in scoring, so a stored
        # score never changes meaning just because time passed.
        # last_seen is when a search last returned this home, which is the same
        # confirmation by another name. Using it as the fallback means an install
        # that predates the explicit stamp is judged on what it actually knows,
        # rather than every stored home flagging itself the moment of upgrade.
        item["last_verified_at"] = item["metadata"].get("last_verified_at") or item.get("last_seen")
        confirmation = _confirmation_check(item["last_verified_at"])
        if confirmation is not None and item["eligibility"] != "ineligible":
            item["checks"] = ordered_checks(
                [{"status": "unknown", **confirmation}]
                + [{"status": "unknown", **entry} for entry in item["checks"]],
                "unknown",
            )
        # A rent the app does not believe, that nobody has gone and looked at.
        # Ahead of the other questions for the same reason the ordering puts
        # the home below the ones that raise none: it is the thing most likely
        # to make the reader stop and check before they spend an afternoon on
        # it. On the author's board ten of the thirteen homes priced below
        # their own floor said nothing about the rent at all, because the one
        # ``concern`` sentence is chosen by a chain of elif and an unstated
        # neighbourhood wins it.
        cheap_rent = _cheap_rent_check(item.get("price"), score_details, item["metadata"])
        if cheap_rent is not None and item["eligibility"] != "ineligible":
            item["checks"] = ordered_checks(
                [{"status": "unknown", **cheap_rent}]
                + [{"status": "unknown", **entry} for entry in item["checks"]],
                "unknown",
            )
        item["lead_check"] = item["checks"][0]["check"] if item["checks"] else ""
        # Criteria that are merely unmeasured rather than blocking. They belong
        # with the coverage figure, not with the questions, and a fact already
        # named as a check must not be asked about twice in two different voices.
        answered = set()
        for entry in item["checks"] + item["blockers"]:
            answered.add(entry["reason"])
            if entry["check"]:
                answered.add(entry["check"])
        item["other_unknowns"] = unmeasured_criteria(score_details, answered)
        neighborhood_details = score_details.get("neighborhood")
        item["neighborhood_priority"] = (
            neighborhood_details.get("priority")
            if isinstance(neighborhood_details, dict)
            and neighborhood_details.get("priority") in {"dream", "strong", "secondary"}
            else None
        )
        item["location_hint"] = score_details.get("location_hint")
        item["household_restriction"] = score_details.get("household_restriction")
        availability = score_details.get("availability")
        item["available_on"] = (
            availability.get("available_on")
            if isinstance(availability, dict) and isinstance(availability.get("available_on"), str)
            else None
        )
        # Scoring already treats a bare city label as no neighbourhood at all.
        # Printing it in the area column implied a precision that was never
        # there, so the display agrees with the score.
        area = str(item.get("neighborhood") or "").strip()
        if re.fullmatch(r"(?:city\s+(?:and\s+county\s+)?of\s+)?san\s+francisco(?:,?\s*ca)?", area, re.IGNORECASE):
            item["neighborhood"] = ""
        home_facts = score_details.get("home_facts")
        item["home_facts"] = home_facts if isinstance(home_facts, dict) else {}
        item["summary_quoted_rent"] = quoted_rent_mismatch(
            item.get("summary"),
            item.get("price"),
            grain(str(item.get("platform") or ""), str(item.get("source_id") or ""), str(item.get("original_url") or "")),
        )
        item["per_person_monthly"] = score_details.get("per_person_monthly")
        item["occupants"] = score_details.get("occupants")
        sublease = score_details.get("sublease")
        item["sublease_months"] = (
            sublease.get("minimum_months")
            if isinstance(sublease, dict)
            and sublease.get("is_sublease") is True
            and sublease.get("main_results_eligible") is True
            and isinstance(sublease.get("minimum_months"), int)
            else None
        )
        return item

    def filter_options(
        self,
        minimum_score: int,
        housing_kind: str = "room",
        unit_types: tuple[str, ...] = (),
        outside_tabs: tuple[bool, Sequence[str]] | None = None,
    ) -> tuple[list[str], list[str]]:
        """The neighbourhoods and sites a view's filters offer, remembered until
        the listings next change (see ``_remembered``)."""
        key = ("filter_options", int(minimum_score), housing_kind, tuple(unit_types), _tabs_key(outside_tabs))
        with self.connection() as connection:
            return self._remembered(
                connection,
                key,
                lambda: self._filter_options(connection, int(minimum_score), housing_kind, unit_types, outside_tabs),
            )

    def _filter_options(
        self,
        connection: sqlite3.Connection,
        minimum_score: int,
        housing_kind: str,
        unit_types: Sequence[str],
        outside_tabs: tuple[bool, Sequence[str]] | None,
    ) -> tuple[list[str], list[str]]:
        base = ["score >= ?"]
        base_parameters: list[Any] = [minimum_score]
        if minimum_score > 0:
            # The shortlist's choices come from homes the shortlist can show.
            base.append("status IN ('active', 'saved')")
        # Shaped only where the score and status narrow anything: every row
        # scores at least 0, and shaping every home is cheaper whole.
        tab, tab_parameters = self._tab_clauses(
            housing_kind,
            "",
            unit_types,
            outside_tabs,
            within=(base, base_parameters) if minimum_score > 0 else ((), ()),
        )
        if housing_kind and housing_kind not in {"room", "whole_unit", "other"}:
            tab, tab_parameters = ["housing_kind = ?"], [housing_kind]
        clauses = [*base, *tab]
        parameters: list[Any] = [*base_parameters, *tab_parameters]
        where = " AND ".join(clauses)
        # Sources write whatever they have in the location field, and for some
        # of them that is the city. Scoring has always treated those as an
        # absent neighborhood; the filter did not, so "San Francisco" and
        # "city of san francisco" sat in the list beside Castro and Nob Hill
        # as though a reader could choose between them. Filtered here rather
        # than in SQL so the one definition in scoring stays the only one.
        from .scoring import GENERIC_LOCATIONS

        neighborhoods: list[str] = []
        seen: set[str] = set()
        for row in connection.execute(
            f"""SELECT DISTINCT neighborhood FROM listings
               WHERE {where} AND neighborhood IS NOT NULL AND neighborhood != ''
               ORDER BY neighborhood COLLATE NOCASE, neighborhood""",
            parameters,
        ):
            folded = str(row[0]).strip().casefold()
            # One entry per area however a source cased it: the filter
            # matches without regard to case, so two entries were one choice.
            if folded in GENERIC_LOCATIONS or folded in seen:
                continue
            seen.add(folded)
            neighborhoods.append(str(row[0]))
        platforms = [
            row[0]
            for row in connection.execute(
                f"""SELECT DISTINCT platform FROM listings
                   WHERE {where}
                   ORDER BY platform COLLATE NOCASE""",
                parameters,
            )
        ]
        return neighborhoods, platforms

    # Every copy of the home a row belongs to, the row itself included; bound
    # twice to the row's id. Written against the columns rather than
    # ``GROUP_SQL`` so the home_key index serves it.
    _SAME_HOME = (
        "(id = ? OR (COALESCE(home_key, '') <> '' AND home_key = "
        "(SELECT home_key FROM listings WHERE id = ?)))"
    )

    def set_listing_status(self, listing_id: int, status: str) -> bool:
        """Star, pass or restore a home: every copy of it, in one statement.

        Passing the Zillow copy of a flat has to pass the Movoto and Redfin
        copies too, or the flat is back on the shortlist from the next site.
        The reason is recorded as the user's, so the archive never overrides
        it -- including a restore from Passed or the Archive, which is the user
        saying "keep this". Taking a star off is not that: the home is simply
        ordinary again, and ages out like any other. Returns whether the
        listing exists.
        """
        if status not in {"active", "saved", "dismissed"}:
            raise ValueError("Invalid listing status")
        with self.connection() as connection:
            cursor = connection.execute(
                f"""UPDATE listings
                    SET status_reason = CASE
                            WHEN :status = 'active' AND status = 'saved' THEN NULL
                            WHEN :status = 'active' AND status = 'active' THEN status_reason
                            ELSE 'user'
                        END,
                        status = :status
                    WHERE {self._SAME_HOME.replace("?", ":listing_id")}""",
                {"status": status, "listing_id": listing_id},
            )
            connection.commit()
        return cursor.rowcount >= 1

    def set_listing_note(self, listing_id: int, note: str) -> bool:
        """Write the note on this copy; a home with a note is never aged out.

        A note on a home the archive had already aged out brings the home
        back, since a noted home is one the user is still thinking about.
        """
        with self.connection() as connection:
            cursor = connection.execute("UPDATE listings SET note = ? WHERE id = ?", (note, listing_id))
            if cursor.rowcount == 1 and note.strip():
                connection.execute(
                    f"UPDATE listings SET status = 'active', status_reason = NULL "
                    f"WHERE status_reason = 'aged' AND {self._SAME_HOME}",
                    (listing_id, listing_id),
                )
            connection.commit()
        return cursor.rowcount == 1

    # How long an ordinary home stays on the board after it was first found.
    ARCHIVE_AFTER_DAYS = 21
    # How long a home the archive has aged out is kept once no site lists it.
    RETAIN_UNSEEN_DAYS = 120
    # Rows the retention sweep deletes per transaction, and how long one sweep
    # spends on a backlog before leaving the rest to the next scan. The first
    # sweep of a year of listings found 119,000 rows to delete; in one
    # transaction that is the write lock held for longer than a star waits
    # (``BUSY_SECONDS``). A thousand took about half a second each, and a
    # sweep cleared some nine to fourteen thousand.
    PRUNE_BATCH = 1000
    PRUNE_SECONDS = 5.0

    @staticmethod
    def _dated(value: str) -> str:
        """A stamp that is a date: SQLite reads a bare "17" as a Julian day,
        4,700 BC, which would make any row look ancient."""
        return f"{value} GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'"

    def _age_out(self, connection: sqlite3.Connection, keep_days: int, moment: str) -> int:
        """The 21-day archive (see ``archive_stale_listings``), on ``connection``.

        Asked per candidate rather than of every home: only active rows nobody
        decided about can age, so those are read, and each one's copies are
        found through the home index. Grouping the whole board to find them
        read a year of listings on every scan.
        """
        cursor = connection.execute(
            f"""UPDATE listings SET status = 'dismissed', status_reason = 'aged', aged_at = :now
                WHERE id IN (
                    SELECT candidate.id FROM listings AS candidate
                    WHERE candidate.status = 'active'
                      AND candidate.status_reason IS NULL
                      AND COALESCE(candidate.note, '') = ''
                      AND {self._dated('candidate.first_found')}
                      AND julianday(candidate.first_found) <= julianday(:now) - :days
                      AND NOT EXISTS (
                          SELECT 1 FROM listings AS copy
                          WHERE {group_sql('copy')} = {group_sql('candidate')}
                            AND (copy.status = 'saved'
                                 OR COALESCE(copy.note, '') <> ''
                                 OR COALESCE(copy.status_reason, '') = 'user')
                      )
                )""",
            {"now": moment, "days": int(keep_days)},
        )
        return max(0, cursor.rowcount)

    def _prune(self, connection: sqlite3.Connection, keep_days: int, moment: str, batch: int) -> int:
        """One batch of ``prune_old_listings``, on ``connection``. Returns rows deleted.

        Two conditions beyond the dates. The row has sat in the archive for
        ``keep_days`` since it was last aged out: a home whose star or note
        comes off after months unlisted ages out at the next scan, and must
        wait in the Archive, where the user can see and restore it, rather
        than be deleted by the same sweep. (A row aged before ``aged_at``
        existed has no stamp, and is judged by its other dates.) And its own
        site has been searched successfully since it was last seen: "no site
        lists it" is only known of a site that was asked. A scan that reached
        nothing -- offline after a month away, or a site blocking the app for
        a season -- deletes nothing that site might still list, rather than
        emptying the board and bringing every home back as new.
        """
        verified = "json_extract(candidate.metadata_json, '$.last_verified_at')"
        # For the index only: a day's slack on the cut-off, so a stamp written
        # with an offset is never passed over; ``julianday`` still decides.
        try:
            latest = (datetime.fromisoformat(moment) - timedelta(days=int(keep_days) - 1)).isoformat(
                timespec="seconds"
            )
        except ValueError:
            return 0
        cursor = connection.execute(
            f"""DELETE FROM listings WHERE id IN (
                    SELECT candidate.id FROM listings AS candidate
                    WHERE candidate.status = 'dismissed'
                      AND candidate.status_reason = 'aged'
                      AND candidate.last_seen <= :latest
                      AND COALESCE(candidate.note, '') = ''
                      AND {self._dated('candidate.first_found')}
                      AND {self._dated('candidate.last_seen')}
                      AND julianday(candidate.first_found) <= julianday(:now) - :days
                      AND julianday(candidate.last_seen) <= julianday(:now) - :days
                      AND json_valid(candidate.metadata_json)
                      AND ({verified} IS NULL
                           OR ({self._dated(verified)} AND julianday({verified}) <= julianday(:now) - :days))
                      AND (candidate.aged_at IS NULL OR julianday(candidate.aged_at) <= julianday(:now) - :days)
                      AND EXISTS (
                          SELECT 1 FROM source_runs AS run
                          WHERE run.platform = candidate.platform
                            AND run.status = 'success'
                            AND run.started_at > candidate.last_seen
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM listings AS copy
                          WHERE {group_sql('copy')} = {group_sql('candidate')}
                            AND (copy.status = 'saved'
                                 OR COALESCE(copy.note, '') <> ''
                                 OR COALESCE(copy.status_reason, '') = 'user'
                                 OR (copy.status <> 'active' AND COALESCE(copy.status_reason, '') <> 'aged'))
                      )
                    ORDER BY candidate.last_seen, candidate.id LIMIT :batch
                )""",
            {"now": moment, "days": int(keep_days), "batch": int(batch), "latest": latest},
        )
        return max(0, cursor.rowcount)

    def _prune_backlog(self, connection: sqlite3.Connection, keep_days: int, moment: str, deleted: int) -> int:
        """Keep deleting in batches, each its own transaction, until nothing is
        left or ``PRUNE_SECONDS`` have gone. ``deleted`` is what the first batch
        took; a short batch means there is nothing more."""
        total = batch = deleted
        until = time.monotonic() + self.PRUNE_SECONDS
        while batch >= self.PRUNE_BATCH and time.monotonic() < until:
            connection.execute("BEGIN IMMEDIATE")
            try:
                batch = self._prune(connection, keep_days, moment, self.PRUNE_BATCH)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            total += batch
        return total

    def prune_old_listings(self, keep_days: int = RETAIN_UNSEEN_DAYS, *, now: str | None = None) -> int:
        """Delete aged-out homes no site has listed, and the archive has held, for ``keep_days``.

        Returns rows deleted.

        Retention, and the only thing in the app that deletes a listing. Only
        a row the 21-day archive has already aged out (``status_reason`` is
        ``aged``) can go, so nothing the archive would keep is ever deleted;
        and only when no search has returned it and nothing has confirmed it
        for ``keep_days``, by ``last_seen`` and any ``last_verified_at`` as
        well as by when it was first found. Measured from first-found alone it
        would delete a big building's card while every scan still returned
        it, and the next scan would bring it back as a new home on the
        shortlist -- every 120 days, for as long as the site listed it. Also
        only once the archive has held it that long, and its own site has been
        searched successfully since it was last seen (see ``_prune``).

        Never deleted: any copy of a home one of whose copies is starred,
        noted, passed or restored -- the user's own work, and the only thing
        on the board that cannot be fetched again -- nor a row whose dates are
        not dates, or lie in the future. A home deleted here that a site lists
        again comes back as a new one, as any newly listed home does.
        """
        if int(keep_days) < self.ARCHIVE_AFTER_DAYS:
            raise ValueError("keep_days must be at least as long as the archive keeps homes")
        moment = now or utc_now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                deleted = self._prune(connection, int(keep_days), moment, self.PRUNE_BATCH)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            return self._prune_backlog(connection, int(keep_days), moment, deleted)

    def retire_old_listings(
        self,
        *,
        archive_days: int = ARCHIVE_AFTER_DAYS,
        keep_days: int = RETAIN_UNSEEN_DAYS,
        now: str | None = None,
    ) -> tuple[int, int]:
        """The end-of-scan sweep: age out, then delete. Returns (archived, deleted).

        One pass, one transaction and one moment for both, as the work orders
        asked: a star landing while it runs lands wholly before it (and the
        home is kept) or wholly after it (and the star wins); the two can
        never disagree about what "now" is. Only a backlog -- the first sweep
        on a board older than retention -- carries on past that transaction,
        a batch at a time.
        """
        if int(archive_days) < 1:
            raise ValueError("archive_days must be at least 1")
        if int(keep_days) < int(archive_days):
            raise ValueError("keep_days must be at least as long as the archive keeps homes")
        moment = now or utc_now()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                archived = self._age_out(connection, int(archive_days), moment)
                deleted = self._prune(connection, int(keep_days), moment, self.PRUNE_BATCH)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            return archived, self._prune_backlog(connection, int(keep_days), moment, deleted)

    def archive_stale_listings(self, keep_days: int = ARCHIVE_AFTER_DAYS, *, now: str | None = None) -> int:
        """Age out listings first found more than ``keep_days`` ago. Returns rows moved.

        Each listing by its own first-found date, as D1 decided: a copy a site
        started listing today has not sat on anybody's shortlist, and it is
        fresh evidence the home is still to let -- measured by the home's
        oldest copy it was archived in the very scan that found it, even when
        it was the first copy that fitted the deal. Never touched: every copy
        of a home any copy of which is starred, noted, or was passed or
        restored by the user -- the archive only tidies what nobody has
        decided about -- and a row whose first-found date is not a date, which
        cannot be aged honestly.
        The rows go to Passed with the reason ``aged``, so the page can say
        "aged out" rather than claim the user passed them, and one click
        (Restore) brings a home back for good.
        """
        if int(keep_days) < 1:
            raise ValueError("keep_days must be at least 1")
        moment = now or utc_now()
        with self.connection() as connection:
            moved = self._age_out(connection, int(keep_days), moment)
            connection.commit()
        return moved

    def stated_application_deadlines(self) -> dict[int, str]:
        """Every live copy that states a deadline to apply by, as it states it.

        Only the rows a sweep could still act on. One already known gone needs
        no second verdict and would have the same one written over it, and one
        the archive has put away has left the shortlist already. A row whose
        metadata is not JSON at all, or whose deadline is not text, states
        nothing -- never an error that would cost the scan its sweep.
        """
        with self.connection() as connection:
            rows = connection.execute(
                """SELECT id, json_extract(metadata_json, '$.application_due_date') AS due
                     FROM listings
                    WHERE status IN ('active', 'saved')
                      AND json_valid(metadata_json)
                      AND json_type(metadata_json, '$.application_due_date') = 'text'
                      AND COALESCE(json_extract(metadata_json, '$.verified_inactive'), 0) <> 1"""
            ).fetchall()
        return {int(row["id"]): str(row["due"]) for row in rows if row["due"]}

    def close_expired_applications(self, deadlines: Mapping[int, str]) -> int:
        """Take homes nobody can apply for any more off the shortlist. Returns rows moved.

        Teaching a source to skip a closed lottery only stops it being added
        again; the ones already stored would sit where they are for ever,
        because a source dropping a home is absence and absence only ever
        demotes it. A deadline the source stated itself, which has now passed,
        is not absence: it is the source saying nobody can apply. So it is
        written the way every other proof of a gone home is written, and the
        home is found where every other gone home is found rather than
        vanishing.

        The caller decides which deadlines have passed, because that is a
        question about the day it is in San Francisco rather than a question
        about the board.
        """
        if not deadlines:
            return 0
        moved = 0
        with self.connection() as connection:
            # One transaction for the whole sweep, as the end-of-scan
            # retirement takes: a star landing while it runs lands wholly
            # before it or wholly after it, and a sweep killed part way
            # through leaves the board as it found it rather than half judged.
            connection.execute("BEGIN IMMEDIATE")
            try:
                for listing_id, due in deadlines.items():
                    if self._mark_verified_inactive(
                        connection,
                        int(listing_id),
                        f"Applications closed: the source stated a deadline of {str(due)[:10]}.",
                    ):
                        moved += 1
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return moved

    def mark_listing_opened(self, listing_id: int) -> bool:
        """Keep the first time a user opened a listing without changing review status.

        Recorded on every copy: having read the flat on one site is having
        read it.
        """
        with self.connection() as connection:
            cursor = connection.execute(
                f"UPDATE listings SET opened_at = COALESCE(opened_at, ?) WHERE {self._SAME_HOME}",
                (utc_now(), listing_id, listing_id),
            )
            connection.commit()
        return cursor.rowcount >= 1

    def open_listing_url(self, listing_id: int) -> str | None:
        """Record an open and return the stored destination as one local operation."""
        with self.connection() as connection:
            row = connection.execute(
                "SELECT original_url FROM listings WHERE id = ?", (listing_id,)
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                f"UPDATE listings SET opened_at = COALESCE(opened_at, ?) WHERE {self._SAME_HOME}",
                (utc_now(), listing_id, listing_id),
            )
            connection.commit()
        return str(row["original_url"])

    def abandon_interrupted_scans(self) -> int:
        """Close out checks a stopped process left recorded as running.

        Nothing else ever clears these rows, so the record kept insisting a
        check was in flight long after the process running it was gone -- and
        the Ready Check told people that reopening the app would settle it,
        which was advice the app did not honour. Callers are responsible for
        proving no check is actually running before calling this.
        """
        stopped = utc_now()
        with self.connection() as connection:
            scans = connection.execute(
                "UPDATE scan_runs SET status = 'interrupted', finished_at = ?, "
                "message = COALESCE(message, ?) WHERE status = 'running'",
                (stopped, "The app stopped before this check finished."),
            )
            abandoned = int(scans.rowcount or 0)
            connection.execute(
                "UPDATE source_runs SET status = 'interrupted', finished_at = ?, "
                "message = COALESCE(message, ?) WHERE status = 'running'",
                (stopped, "The app stopped before this source finished."),
            )
            connection.commit()
        return abandoned

    def begin_scan(self, trigger: str) -> int:
        with self.connection() as connection:
            cursor = connection.execute(
                "INSERT INTO scan_runs(trigger, status, started_at) VALUES (?, 'running', ?)",
                (trigger, utc_now()),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def finish_scan(
        self,
        run_id: int,
        status: str,
        seen: int = 0,
        added: int = 0,
        updated: int = 0,
        failed: int = 0,
        message: str | None = None,
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                """UPDATE scan_runs SET status = ?, finished_at = ?, listings_seen = ?,
                   listings_added = ?, listings_updated = ?, sources_failed = ?, message = ?
                   WHERE id = ?""",
                (status, utc_now(), seen, added, updated, failed, message, run_id),
            )
            connection.commit()

    def source_runs_say(self, scan_run_id: int, words: str) -> bool:
        """Whether any source run of scan ``scan_run_id`` recorded ``words`` in its message."""
        with self.connection() as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM source_runs WHERE scan_run_id = ? AND instr(COALESCE(message, ''), ?) > 0 LIMIT 1",
                    (scan_run_id, words),
                ).fetchone()
                is not None
            )

    def begin_source_run(
        self,
        scan_run_id: int,
        platform: str,
        search_url: str,
        provider: str = "",
        source_key: str = "",
    ) -> int:
        with self.connection() as connection:
            cursor = connection.execute(
                """INSERT INTO source_runs(
                       scan_run_id, platform, provider, source_key, status, started_at, search_url
                   ) VALUES (?, ?, ?, ?, 'running', ?, ?)""",
                (scan_run_id, platform, provider, source_key, utc_now(), search_url),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def finish_source_run(
        self,
        source_run_id: int,
        status: str,
        seen: int = 0,
        added: int = 0,
        message: str | None = None,
        *,
        provider: str | None = None,
        source_key: str | None = None,
        updated: int = 0,
        fetched: int = 0,
        parsed: int = 0,
        classified: int = 0,
        deduplicated: int = 0,
        hard_filtered: int = 0,
        active: int = 0,
        archived: int = 0,
    ) -> None:
        with self.connection() as connection:
            connection.execute(
                """UPDATE source_runs SET status = ?, finished_at = ?, listings_seen = ?,
                   listings_added = ?, listings_updated = ?, fetched = ?, parsed = ?,
                   classified = ?, deduplicated = ?, hard_filtered = ?, active = ?,
                   archived = ?, message = ?, provider = COALESCE(?, provider),
                   source_key = COALESCE(?, source_key) WHERE id = ?""",
                (
                    status,
                    utc_now(),
                    seen,
                    added,
                    updated,
                    fetched,
                    parsed,
                    classified,
                    deduplicated,
                    hard_filtered,
                    active,
                    archived,
                    message,
                    provider,
                    source_key,
                    source_run_id,
                ),
            )
            connection.commit()

    def source_initialized(self, source_key: str) -> bool:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT status FROM source_initializations WHERE source_key = ?", (source_key,)
            ).fetchone()
        return row is not None and row["status"] == "success"

    def mark_source_initialized(self, source_key: str, status: str, message: str | None = None) -> None:
        with self.connection() as connection:
            connection.execute(
                """INSERT INTO source_initializations(source_key, initialized_at, status, message)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(source_key) DO UPDATE SET
                     initialized_at = excluded.initialized_at,
                     status = excluded.status,
                     message = excluded.message""",
                (source_key, utc_now(), status, message),
            )
            connection.commit()

    def delivery_since(self, moment: datetime) -> dict[str, int]:
        """How many homes each source has actually brought in since ``moment``.

        The honest, unblockable version of "what is this source worth". A count
        scraped from a third party can be refused, rate-limited or faked; this
        is the app's own record of what arrived, and no company can take it
        away or lie about it.
        """
        with self.connection() as connection:
            rows = connection.execute(
                """SELECT platform, COUNT(*) AS delivered
                     FROM listings
                    WHERE first_found >= ?
                 GROUP BY platform""",
                (moment.astimezone(UTC).isoformat(),),
            ).fetchall()
        return {str(row["platform"]): int(row["delivered"]) for row in rows}

    def record_source_coverage(self, platform: str, count: int) -> None:
        """Store how many homes a disconnected source is holding.

        Its own table rather than connector metadata, because this is derived,
        disposable and refreshed on a different rhythm to a connector's state.
        Keeping them apart means a count can never overwrite the answer to "is
        this connector working", and a state write can never silently drop a
        count.
        """
        if count <= 0:
            return
        with self.connection() as connection:
            connection.execute(
                """INSERT INTO source_coverage(platform, listing_count, taken_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(platform) DO UPDATE SET
                     listing_count = excluded.listing_count,
                     taken_at = excluded.taken_at""",
                (str(platform), int(count), utc_now()),
            )
            connection.commit()

    def source_coverage(self) -> dict[str, dict[str, Any]]:
        """Every stored count, by platform, with the moment it was taken."""
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT platform, listing_count, taken_at FROM source_coverage"
            ).fetchall()
        return {
            str(row["platform"]): {
                "count": int(row["listing_count"]),
                "taken_at": str(row["taken_at"]),
            }
            for row in rows
        }

    def set_connector_state(
        self,
        connector_key: str,
        state: str,
        *,
        message: str | None = None,
        observed_items: int | None = None,
        metadata: dict[str, Any] | None = None,
        configured: bool = False,
        attempted: bool = False,
        succeeded: bool = False,
    ) -> None:
        if state not in CONNECTOR_STATES:
            raise ValueError(f"Invalid connector state: {state}")
        now = utc_now()
        with self.connection() as connection:
            current = connection.execute(
                "SELECT * FROM connector_states WHERE connector_key = ?", (connector_key,)
            ).fetchone()
            previous_items = int(current["observed_items"]) if current else 0
            previous_metadata = json.loads(current["metadata_json"] or "{}") if current else {}
            connection.execute(
                """INSERT INTO connector_states(
                       connector_key, state, configured_at, last_attempt_at, last_success_at,
                       observed_items, message, metadata_json
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(connector_key) DO UPDATE SET
                     state = excluded.state,
                     configured_at = COALESCE(connector_states.configured_at, excluded.configured_at),
                     last_attempt_at = COALESCE(excluded.last_attempt_at, connector_states.last_attempt_at),
                     last_success_at = COALESCE(excluded.last_success_at, connector_states.last_success_at),
                     observed_items = MAX(connector_states.observed_items, excluded.observed_items),
                     message = excluded.message,
                     metadata_json = excluded.metadata_json""",
                (
                    connector_key,
                    state,
                    now if configured else None,
                    now if attempted else None,
                    now if succeeded else None,
                    max(previous_items, observed_items or 0),
                    (message or "")[:1000],
                    json.dumps({**previous_metadata, **(metadata or {})}, ensure_ascii=False),
                ),
            )
            connection.commit()

    def connector_state(self, connector_key: str) -> ConnectorStatus | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM connector_states WHERE connector_key = ?", (connector_key,)
            ).fetchone()
        if row is None:
            return None
        return ConnectorStatus(
            key=str(row["connector_key"]),
            state=str(row["state"]),
            configured_at=row["configured_at"],
            last_attempt_at=row["last_attempt_at"],
            last_success_at=row["last_success_at"],
            observed_items=int(row["observed_items"]),
            message=str(row["message"] or ""),
            metadata=json.loads(row["metadata_json"] or "{}"),
        )

    def connector_states(self) -> dict[str, ConnectorStatus]:
        with self.connection() as connection:
            rows = connection.execute("SELECT connector_key FROM connector_states").fetchall()
        return {
            str(row["connector_key"]): status
            for row in rows
            if (status := self.connector_state(str(row["connector_key"]))) is not None
        }

    def count_listings(self) -> int:
        """How many homes are stored, for pages that have to explain a wait."""
        with self.connection() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM listings").fetchone()[0])

    def recent_scans(self, limit: int = 8) -> list[dict[str, Any]]:
        """Recent checks, each carrying how many sources it never reached.

        A scan that runs out of its time budget marks the rest "skipped" and
        still finishes as "completed" with zero errors. One real check on Sep 8
        reached one source of twenty-two and was displayed as a clean green row,
        so ``sources_skipped`` travels with the summary rather than living only
        in ``source_runs`` where nothing reads it.

        ``sources_reached`` is how many sources this scan actually got a word
        out of: one that came back with something, or one that failed for a
        reason a site gave it. A scan run with the wifi off reaches nothing --
        every source dies in the resolver before a socket opens -- and the
        by-hand budget, which exists to keep those sites from being hammered,
        has nothing to charge for.

        A success with nothing behind it does not count. SpareRoom records one
        every scan without opening a socket, because private rooms are not in
        this deal, and that single phantom row was enough to charge somebody
        for a check that asked nobody anything. A source that answers an alert
        mailbox rather than a web page is the same story from the budget's side:
        no rental site was read. Counted here because only SQL sees these rows.
        """
        offline = " OR ".join(
            "LOWER(COALESCE(source_runs.message, '')) LIKE ?" for _ in NO_NETWORK_PHRASES
        )
        patterns = [f"%{phrase}%" for phrase in NO_NETWORK_PHRASES]
        with self.connection() as connection:
            rows = connection.execute(
                f"""SELECT scan_runs.*,
                          (SELECT COUNT(*) FROM source_runs
                            WHERE source_runs.scan_run_id = scan_runs.id
                              AND source_runs.status = 'skipped') AS sources_skipped,
                          (SELECT COUNT(*) FROM source_runs
                            WHERE source_runs.scan_run_id = scan_runs.id
                              AND ((source_runs.status = 'success'
                                    AND (source_runs.fetched > 0
                                         OR source_runs.parsed > 0
                                         OR source_runs.listings_seen > 0))
                                   OR (source_runs.status = 'error'
                                       AND NOT ({offline})))) AS sources_reached
                     FROM scan_runs ORDER BY id DESC LIMIT ?""",
                (*patterns, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def last_successful_source_run(
        self,
        *,
        source_key: str,
        platform: str,
    ) -> dict[str, Any] | None:
        """The most recent run this source actually completed, however long ago.

        Separate from ``source_run_history`` because that window is bounded and
        a failing source fills it fastest: each deferred automatic attempt
        writes a "backoff" row, so a source down for a few days pushes its own
        last good run out of sight and the panel then reports it as a source
        that has never worked at all.
        """
        legacy_key = source_key.rsplit("::", 1)[0]
        with self.connection() as connection:
            row = connection.execute(
                """SELECT * FROM source_runs
                   WHERE status = 'success'
                     AND (source_key IN (?, ?) OR (source_key = '' AND platform = ?))
                   ORDER BY id DESC LIMIT 1""",
                (source_key, legacy_key, platform),
            ).fetchone()
        return dict(row) if row else None

    def source_failures_since(
        self,
        *,
        source_key: str,
        platform: str,
        after_id: int = 0,
        reached_only: bool = False,
    ) -> tuple[int, dict[str, Any] | None]:
        """How many runs of this source failed after run ``after_id``, and the newest.

        For the same reason ``last_successful_source_run`` exists: counted
        inside the bounded history, the failures of a source that has been
        failing longest are exactly the ones pushed out of sight.
        ``reached_only`` leaves out the failures in which nothing reached the
        site -- no network (``NO_NETWORK_PHRASES``) -- as the backoff does.
        """
        legacy_key = source_key.rsplit("::", 1)[0]
        match = "status = 'error' AND id > ? AND (source_key IN (?, ?) OR (source_key = '' AND platform = ?))"
        values: tuple[Any, ...] = (int(after_id), source_key, legacy_key, platform)
        if reached_only:
            match += " AND NOT (" + " OR ".join(
                "LOWER(COALESCE(message, '')) LIKE ?" for _ in NO_NETWORK_PHRASES
            ) + ")"
            values = (*values, *(f"%{phrase}%" for phrase in NO_NETWORK_PHRASES))
        with self.connection() as connection:
            count = connection.execute(
                f"SELECT COUNT(*) FROM source_runs WHERE {match}", values
            ).fetchone()[0]
            newest = connection.execute(
                f"SELECT * FROM source_runs WHERE {match} ORDER BY id DESC LIMIT 1", values
            ).fetchone()
        return int(count), (dict(newest) if newest else None)

    def typical_source_seconds(self, limit: int = 6) -> dict[str, float]:
        """How long each source usually takes, from its own recent runs.

        A progress bar counting sources treats Craigslist and Listings Project
        as equal thirds of a percent apiece, so it sits at nothing for the 75
        seconds the first one takes and then jumps. Weighted by these instead,
        it moves at the rate the scan is actually progressing.

        Only completed runs count: a skipped source finishes instantly and a
        stalled one is abandoned, and neither is how long the work takes.
        """
        with self.connection() as connection:
            rows = connection.execute(
                """SELECT platform, started_at, finished_at
                     FROM source_runs
                    WHERE status IN ('success', 'error')
                      AND finished_at IS NOT NULL
                    ORDER BY id DESC
                    LIMIT ?""",
                (limit * 64,),
            ).fetchall()
        seen: dict[str, list[float]] = {}
        for row in rows:
            platform = str(row["platform"])
            samples = seen.setdefault(platform, [])
            if len(samples) >= limit:
                continue
            try:
                started = datetime.fromisoformat(str(row["started_at"]))
                finished = datetime.fromisoformat(str(row["finished_at"]))
            except (TypeError, ValueError):
                continue
            seconds = (finished - started).total_seconds()
            # A negative clock change is not a duration, and no single source
            # legitimately runs for an hour.
            if 0 <= seconds <= 3600:
                samples.append(seconds)
        return {
            platform: sorted(samples)[len(samples) // 2]
            for platform, samples in seen.items()
            if samples
        }

    def latest_source_runs(self) -> list[dict[str, Any]]:
        """Return one latest run per durable source, not merely per display platform.

        Older databases predate ``source_key``.  Their empty key deliberately
        falls back to platform so a repair/install migration preserves useful
        history instead of making it disappear from the dashboard.
        """
        with self.connection() as connection:
            rows = connection.execute(
                """SELECT source_runs.* FROM source_runs
                   JOIN (
                     SELECT CASE WHEN source_key <> '' THEN source_key ELSE platform END AS identity,
                            MAX(id) AS latest_id
                     FROM source_runs
                     GROUP BY CASE WHEN source_key <> '' THEN source_key ELSE platform END
                   ) latest ON latest.latest_id = source_runs.id
                   ORDER BY CASE source_runs.platform
                     WHEN 'Facebook Marketplace' THEN 1 WHEN 'Craigslist' THEN 2
                     WHEN 'Listings Project' THEN 3 WHEN 'Abacus (small buildings)' THEN 4
                     WHEN 'Zillow' THEN 5 WHEN 'HotPads' THEN 6
                     WHEN 'SpareRoom' THEN 7 WHEN 'Roomies' THEN 8 ELSE 99 END"""
            ).fetchall()
        return [dict(row) for row in rows]

    def last_source_attempt(self, *, source_key: str, platform: str) -> str | None:
        """When this source was last actually asked, skips excluded.

        Deliberately its own query rather than a slice of the history: a source
        with a floor records a skipped run every time a check declines to read
        it, and those pile up fast when somebody keeps pressing. Reading a
        fixed window of recent runs let them push the last real attempt out of
        sight, and the floor lapsed exactly when it was working hardest.
        """
        with self.connection() as connection:
            row = connection.execute(
                "SELECT COALESCE(finished_at, started_at) AS asked FROM source_runs "
                "WHERE (source_key = ? OR platform = ?) AND status IN ('success', 'error') "
                "ORDER BY id DESC LIMIT 1",
                (source_key, platform),
            ).fetchone()
        return str(row["asked"]) if row and row["asked"] else None

    def source_run_history(
        self,
        *,
        source_key: str,
        platform: str,
        limit: int = 12,
    ) -> list[dict[str, Any]]:
        """Read the bounded durable history for one source identity.

        Source keys make same-named providers distinguishable going forward.
        The platform fallback keeps pre-v3 records visible after migration.
        This is intentionally read-only: the watchdog derives state from scan
        history rather than persisting a competing health record.
        """
        safe_limit = max(1, min(int(limit), 50))
        # Version 3 used the class/source name without a provider suffix.
        # Preserve that history after the provider-qualified key migration.
        legacy_key = source_key.rsplit("::", 1)[0]
        with self.connection() as connection:
            rows = connection.execute(
                """SELECT * FROM source_runs
                   WHERE source_key IN (?, ?) OR (source_key = '' AND platform = ?)
                   ORDER BY id DESC LIMIT ?""",
                (source_key, legacy_key, platform, safe_limit),
            ).fetchall()
        return [dict(row) for row in rows]
