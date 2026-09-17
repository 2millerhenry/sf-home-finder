from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from bisect import bisect_left, bisect_right
from typing import Any, Iterator, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .classification import classify_listing
from .connectors import CONNECTOR_STATES, ConnectorStatus
from .location import parse_street_address
from .models import (
    CHECK_EXCLUSION_PHRASES,
    ListingCandidate,
    ScoreResult,
    ordered_checks,
    unmeasured_criteria,
)


SCHEMA_VERSION = 3


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
"""


# A day without confirmation is the point at which the app stops being able
# to vouch for a home. It matches the scanner's own window with room for a
# missed scan, so a single skipped run does not flag the whole board.
CONFIRMATION_STALE_AFTER = timedelta(hours=24)


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


class Repository:
    def __init__(self, path: Path):
        self.path = Path(path)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        try:
            self._initialize()
        except sqlite3.DatabaseError as exc:
            raise DatabaseUnreadableError(
                f"The housing database at {self.path} could not be opened ({exc}). "
                "Your saved homes are not lost: double-click Repair SF Home Finder, "
                "which restores the most recent backup from the app's backups folder. "
                "Do not delete the file."
            ) from exc

    def _initialize(self) -> None:
        with self.connection() as connection:
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
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            connection.commit()

    def integrity_check(self) -> tuple[bool, str]:
        """Return SQLite's bounded integrity verdict for support diagnostics."""
        try:
            with self.connection() as connection:
                row = connection.execute("PRAGMA integrity_check(1)").fetchone()
        except sqlite3.Error as exc:
            return False, f"{type(exc).__name__}: {exc}"
        verdict = str(row[0] if row is not None else "No result")
        return verdict.casefold() == "ok", verdict

    def schema_version(self) -> int:
        try:
            with self.connection() as connection:
                row = connection.execute("PRAGMA user_version").fetchone()
        except sqlite3.Error:
            return -1
        return int(row[0]) if row is not None else 0

    def find_listing(self, platform: str, source_id: str, url: str) -> sqlite3.Row | None:
        canonical = canonicalize_url(url)
        with self.connection() as connection:
            return connection.execute(
                """SELECT * FROM listings
                   WHERE (platform = ? AND source_id = ?) OR canonical_url = ?
                   LIMIT 1""",
                (platform, source_id, canonical),
            ).fetchone()

    def upsert_listing(
        self,
        listing: ListingCandidate,
        result: ScoreResult,
        seen_at: str | None = None,
    ) -> tuple[int, bool]:
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
        with self.connection() as connection:
            existing = connection.execute(
                """SELECT id FROM listings
                   WHERE (platform = ? AND source_id = ?) OR canonical_url = ?
                   LIMIT 1""",
                (listing.platform, listing.source_id, canonical),
            ).fetchone()
            if existing:
                listing_id = int(existing["id"])
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
                        listing.platform,
                        listing.source_id,
                        canonical,
                        listing.title,
                        listing.price,
                        listing.neighborhood,
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
                        listing.platform,
                        listing.source_id,
                        canonical,
                        listing.title,
                        listing.price,
                        listing.neighborhood,
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
                        now,
                        now,
                        published_at,
                        listing.original_url,
                    ),
                )
                listing_id = int(cursor.lastrowid)
                created = True
            connection.commit()
        return listing_id, created

    def update_score(
        self,
        listing_id: int,
        result: ScoreResult,
        neighborhood: str | None = None,
        listing: ListingCandidate | None = None,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        """Write one listing's verdict.

        ``connection`` lets a caller rescoring the whole board reuse a single
        connection. Opening and closing one per listing costs an fsync each
        time; on a board of 870 homes that turned a rescore into half a minute
        of disk sync, and start-up blocks on it.
        """
        if connection is not None:
            self._update_score(connection, listing_id, result, neighborhood, listing)
            return
        with self.connection() as connection:
            self._update_score(connection, listing_id, result, neighborhood, listing)

    def _update_score(
        self,
        connection: sqlite3.Connection,
        listing_id: int,
        result: ScoreResult,
        neighborhood: str | None = None,
        listing: ListingCandidate | None = None,
    ) -> None:
        classified = classify_listing(listing) if listing is not None else None
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
        connection.commit()

    def all_candidates(self) -> list[tuple[int, ListingCandidate]]:
        with self.connection() as connection:
            rows = connection.execute("SELECT * FROM listings").fetchall()
        return [(int(row["id"]), self._row_to_candidate(row)) for row in rows]

    def shortlist_pool(
        self,
        kinds: Sequence[str] = (),
        ceiling: int = 900,
        strata: int = 4,
        unit_types: Sequence[str] = (),
    ) -> tuple[list[tuple[int, ListingCandidate]], dict[int, int], bool]:
        """The homes a cut-off would be measured against, whole or sampled.

        The pool is every home still standing, not every home that suits the
        saved deal. Stored ``eligibility`` is a verdict on the deal as saved --
        a home over that budget, or outside those areas, is written down
        ineligible -- so filtering on it here would answer for the old deal
        while somebody edits a new one. On a real board it threw away 6,442 of
        6,935 homes and pinned the count at 423 however high the draft budget
        went. Whether a home suits the deal in hand is for the scoring of the
        deal in hand to say, which is why the caller rescores what it gets.

        ``housing_kind`` is different and does belong here: it says what a home
        *is*, not what anybody wants, so it narrows the pool without dating it.
        ``unit_types`` is the same kind of fact -- how many bedrooms the source
        stated -- and belongs here for the same reason, because the whole-home
        tabs are one per size and a home of another size, or of none stated,
        has no page to appear on. Leaving it out counted 133 such homes into a
        draft's answer at every cut-off below 55 on a real board.

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
            clauses.append(f"unit_type IN ({','.join('?' for _ in sizes)})")
            parameters.extend(sizes)
        where = " AND ".join(clauses)
        with self.connection() as connection:
            index = connection.execute(
                f"SELECT id, score, price FROM listings WHERE {where}", parameters
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
                f"SELECT * FROM listings WHERE id IN ({','.join('?' for _ in picked)})",
                picked,
            ).fetchall()
        return (
            [(band_of[int(row["id"])], self._row_to_candidate(row)) for row in rows],
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
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if view not in {"saved", "dismissed", "all", "near_matches"}:
            clauses.append("score >= ?")
            parameters.append(minimum_score)
        if view == "saved":
            clauses.append("status = 'saved'")
        elif view == "dismissed":
            clauses.append("status = 'dismissed'")
        elif view == "all":
            pass
        elif view == "near_matches":
            clauses.append("status IN ('active', 'saved')")
            # Two ways to nearly match, and no others. A home the deal accepts
            # that fell a few points short of the cut-off; or a home ruled out
            # by one thing only, the rent, and only just -- which is the case
            # the score is blind to.
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
                ")"
            )
            parameters.extend(
                [minimum_score, max(0, minimum_score - NEAR_MATCH_MARGIN), NEAR_MATCH_OVER_BUDGET]
            )
        else:
            clauses.append("status IN ('active', 'saved')")
            clauses.append("eligibility IN ('eligible', 'needs_verification')")
        if neighborhood:
            clauses.append("neighborhood = ? COLLATE NOCASE")
            parameters.append(neighborhood)
        if platform:
            clauses.append("platform = ? COLLATE NOCASE")
            parameters.append(platform)
        if housing_kind in {"room", "whole_unit"}:
            clauses.append("housing_kind = ?")
            parameters.append(housing_kind)
        valid_unit_types = {"studio", "one_bedroom", "two_bedroom", "three_bedroom", "four_bedroom"}
        if unit_type in valid_unit_types:
            clauses.append("unit_type = ?")
            parameters.append(unit_type)
        elif unit_types:
            selected_types = tuple(value for value in unit_types if value in valid_unit_types)
            if selected_types:
                clauses.append("unit_type IN (" + ", ".join("?" for _ in selected_types) + ")")
                parameters.extend(selected_types)
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
        order_by = {
            "score": "score DESC, confidence DESC, COALESCE(published_at, first_found) DESC",
            "price": "price IS NULL, price ASC, score DESC",
            # The column reads "Posted", so newest must mean newest posted.
            # Sources that publish no date fall back to when we found it.
            "newest": "COALESCE(published_at, first_found) DESC, score DESC",
            "available": "score DESC, first_found DESC",
            # A click is recorded on every outbound listing link. Keep untouched
            # homes at the top, then preserve the normal recommendation order
            # within each group so the sort stays useful rather than arbitrary.
            "unopened": "opened_at IS NOT NULL ASC, score DESC, first_found DESC",
            # Ordered by score here and re-ordered by how near each home came
            # once the rows are in hand: the gap lives in JSON no ORDER BY can
            # reach cheaply, and sorting in Python afterwards is stable, so
            # homes that missed by the same amount keep the order they had
            # rather than shuffling between one refresh and the next.
            "closeness": "score DESC, confidence DESC, COALESCE(published_at, first_found) DESC",
        }.get(sort, "first_found DESC, score DESC")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM listings{where} ORDER BY {order_by}"
        with self.connection() as connection:
            rows = connection.execute(sql, parameters).fetchall()
        items = [self._dashboard_row(row) for row in rows]
        if sort == "closeness" and view == "near_matches":
            # Only where it means something. Asked for anywhere else -- a stale
            # link, or the control left on it while switching tabs -- the rows
            # keep the score order the query already gave them.
            items.sort(key=lambda item: near_miss_distance(item, minimum_score))
        elif sort == "available":
            items.sort(
                key=lambda item: (
                    not bool(item.get("available_on")),
                    str(item.get("available_on") or "9999-12-31"),
                    -int(item["score"]),
                    str(item["first_found"]),
                )
            )
        elif sort == "contact":
            # A direct application is the shortest route to a real response;
            # a direct lister page is next best.  This only changes display
            # order, never the matching score or the user's safeguards.
            items.sort(
                key=lambda item: (
                    int(item.get("contact_rank", 2)),
                    -int(item["score"]),
                    -int(item["id"]),
                )
            )
        return items

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
        """
        clauses = ["status IN ('active', 'saved')", "housing_kind = ?"]
        parameters: list[Any] = [housing_kind]
        if unit_types:
            placeholders = ",".join("?" for _ in unit_types)
            clauses.append(f"(unit_type IN ({placeholders}) OR unit_type IS NULL)")
            parameters.extend(unit_types)
        clauses.append("(eligibility = 'ineligible' OR score < ?)")
        parameters.append(int(minimum_score))
        sql = f"SELECT score, eligibility, score_details_json FROM listings WHERE {' AND '.join(clauses)}"
        with self.connection() as connection:
            rows = connection.execute(sql, parameters).fetchall()

        counts: dict[str, int] = {}
        for row in rows:
            if row["eligibility"] == "ineligible":
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

        ``unit_types`` -- the tabs are per size, so a whole home whose size the
        source never stated, or whose size this deal did not enable, has no page
        to appear on. Thirty-two of those sat inside the 316 the slider claimed
        at the saved cut-off while the two tabs held 284 between them. Rooms
        have no per-size tabs, so a room deal passes none and nothing narrows.
        """
        clauses = [
            "status IN ('active', 'saved')",
            "eligibility IN ('eligible', 'needs_verification')",
        ]
        parameters: list[Any] = []
        wanted = [str(kind) for kind in kinds if str(kind)]
        if wanted:
            clauses.append(f"housing_kind IN ({','.join('?' for _ in wanted)})")
            parameters.extend(wanted)
        sizes = [str(value) for value in unit_types if str(value)]
        if sizes:
            clauses.append(f"unit_type IN ({','.join('?' for _ in sizes)})")
            parameters.extend(sizes)
        with self.connection() as connection:
            scores = [
                int(row[0])
                for row in connection.execute(
                    f"SELECT score FROM listings WHERE {' AND '.join(clauses)}", parameters
                )
            ]
        scores.sort()
        counts: dict[int, int] = {}
        for threshold in thresholds:
            counts[int(threshold)] = len(scores) - bisect_left(scores, int(threshold))
        return counts

    def corroborations(self, listing_id: int) -> list[dict[str, Any]]:
        """The same building, as other sources describe it.

        One source saying a home exists is a lead; two saying it is a fact, and
        where one publishes no rent the other often does. That is the whole
        question a reader has on this page -- is this worth going to look at --
        and no single source can answer it.

        Matched on the street address normalised by ``parse_street_address``,
        because no two of these sites agree on a URL and half of them do not
        publish a name anyone would recognise. A listing never corroborates
        itself, and neither does another row from its own platform: two cards
        from one site are that site repeating itself, not a second opinion.
        """
        with self.connection() as connection:
            rows = connection.execute(
                """SELECT id, platform, title, price, original_url, neighborhood,
                          metadata_json, status
                     FROM listings
                    WHERE status <> 'dismissed' AND metadata_json LIKE '%"address"%'"""
            ).fetchall()

        def key(raw: str | None) -> tuple[int, str] | None:
            return parse_street_address(raw) if raw else None

        by_id = {row["id"]: row for row in rows}
        subject = by_id.get(listing_id)
        if subject is None:
            return []
        wanted = key(json.loads(subject["metadata_json"] or "{}").get("address"))
        if wanted is None:
            return []

        found: list[dict[str, Any]] = []
        for row in rows:
            if row["id"] == listing_id or row["platform"] == subject["platform"]:
                continue
            metadata = json.loads(row["metadata_json"] or "{}")
            if key(metadata.get("address")) != wanted:
                continue
            found.append(
                {
                    "id": row["id"],
                    "platform": row["platform"],
                    "title": row["title"],
                    "price": row["price"],
                    "neighborhood": row["neighborhood"],
                    "address": metadata.get("address"),
                    "original_url": row["original_url"],
                    "saved": row["status"] == "saved",
                }
            )
        # A published rent first, because that is what the reader came for, and
        # a source that has one answers the question the others left open.
        found.sort(key=lambda item: (item["price"] is None, item["price"] or 0, item["platform"]))
        return found

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
            and application_url.startswith("https://abacus.appfolio.com/")
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
    ) -> tuple[list[str], list[str]]:
        type_clause = ""
        type_parameters: list[Any] = []
        selected_types = tuple(
            value
            for value in unit_types
            if value in {"studio", "one_bedroom", "two_bedroom", "three_bedroom", "four_bedroom"}
        )
        if selected_types:
            type_clause = " AND unit_type IN (" + ", ".join("?" for _ in selected_types) + ")"
            type_parameters.extend(selected_types)
        # Sources write whatever they have in the location field, and for some
        # of them that is the city. Scoring has always treated those as an
        # absent neighborhood; the filter did not, so "San Francisco" and
        # "city of san francisco" sat in the list beside Castro and Nob Hill
        # as though a reader could choose between them. Filtered here rather
        # than in SQL so the one definition in scoring stays the only one.
        from .scoring import GENERIC_LOCATIONS

        with self.connection() as connection:
            neighborhoods = [
                row[0]
                for row in connection.execute(
                    f"""SELECT DISTINCT neighborhood FROM listings
                       WHERE score >= ? AND housing_kind = ?
                         {type_clause}
                         AND neighborhood IS NOT NULL AND neighborhood != ''
                       ORDER BY neighborhood COLLATE NOCASE""",
                    (minimum_score, housing_kind, *type_parameters),
                )
                if str(row[0]).strip().casefold() not in GENERIC_LOCATIONS
            ]
            platforms = [
                row[0]
                for row in connection.execute(
                    f"""SELECT DISTINCT platform FROM listings
                       WHERE score >= ? AND housing_kind = ? {type_clause}
                       ORDER BY platform COLLATE NOCASE""",
                    (minimum_score, housing_kind, *type_parameters),
                )
            ]
        return neighborhoods, platforms

    def set_listing_status(self, listing_id: int, status: str) -> bool:
        if status not in {"active", "saved", "dismissed"}:
            raise ValueError("Invalid listing status")
        with self.connection() as connection:
            cursor = connection.execute("UPDATE listings SET status = ? WHERE id = ?", (status, listing_id))
            connection.commit()
        return cursor.rowcount == 1

    def set_listing_note(self, listing_id: int, note: str) -> bool:
        with self.connection() as connection:
            cursor = connection.execute("UPDATE listings SET note = ? WHERE id = ?", (note, listing_id))
            connection.commit()
        return cursor.rowcount == 1

    def mark_listing_opened(self, listing_id: int) -> bool:
        """Keep the first time a user opened a listing without changing review status."""
        with self.connection() as connection:
            cursor = connection.execute(
                "UPDATE listings SET opened_at = COALESCE(opened_at, ?) WHERE id = ?",
                (utc_now(), listing_id),
            )
            connection.commit()
        return cursor.rowcount == 1

    def open_listing_url(self, listing_id: int) -> str | None:
        """Record an open and return the stored destination as one local operation."""
        with self.connection() as connection:
            row = connection.execute(
                "SELECT original_url FROM listings WHERE id = ?", (listing_id,)
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE listings SET opened_at = COALESCE(opened_at, ?) WHERE id = ?",
                (utc_now(), listing_id),
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
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM scan_runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

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
