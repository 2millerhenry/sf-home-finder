"""WO-5's definition of done: a year of listings starts in under 5 s and draws
its dashboard in under 1 s.

Measured on a year-scale board -- 213,408 rows, 952 MB -- before this work
order, the app took 4.6 s to start and 5.7 s to draw the dashboard. 0.6 s of
every start went on a repair that read every row to find nothing, and every
question the dashboard asked read the whole table: each tab's shape was
windowed over every row ever stored, the live rents parsed the JSON of every
row, the rent table's cache key counted every row, and the list of recent
checks counted source runs that had no index, twice for each check shown.
After it the same dashboard took 0.16 s.

Nothing here times anything: a laptop under load would make a timing flaky,
and a year of rows would make the suite slow. These pin what made the
difference instead. A start on a board this version opened last repairs
nothing and rebuilds nothing, and each question below -- what the shortlist,
Saved and Passed ask of the listings and the scan history, and the sweep at
the end of every scan -- reaches those two tables through an index, never by
reading either whole. Nothing runs ANALYZE, so SQLite plans by the shape of a
query and the indexes there are, not by how many rows there are: the plan
read off a board of two hundred rows is the plan a year of them gets. (When
this was written the two were compared question by question, on the board of
213,408 rows and again after retention had brought it down to 70,622.)
"""

from __future__ import annotations

import re
import sqlite3
import threading
from collections.abc import Callable, Iterable
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from sf_housing import rescore_marker
from sf_housing.app import create_app
from sf_housing.database import SCHEMA_VERSION, Repository
from sf_housing.models import ListingCandidate, ScoreResult
from sf_housing.settings import Settings
from tests.conftest import TEST_PREFERENCES

# How every plan below is read: on a connection of its own, never on one the
# app opened, which are the connections being recorded.
PLAIN_CONNECT = sqlite3.connect

# The tables a year makes big. Every other table grows by a row a check, or
# not at all.
YEAR_SCALE_TABLES = ("listings", "source_runs")

# The deal's own tabs, as the Other homes tab is told them.
TABS = (False, ("one_bedroom", "three_bedroom"))

# What a start adds for a year of listings: the indexes the questions below
# are answered through, and the counter the remembered answers are filed by.
MADE_FOR_A_YEAR = {
    ("index", "idx_listings_home_shape"),
    ("index", "idx_source_runs_scan"),
    ("index", "idx_source_runs_searched"),
    ("index", "idx_listings_status_seen"),
    ("index", "idx_listings_seen"),
    ("table", "listings_version"),
    ("trigger", "listings_version_insert"),
    ("trigger", "listings_version_update"),
    ("trigger", "listings_version_delete"),
}

# The repair of statuses an older version wrote without saying who set them.
REPAIR = re.compile(r"\bUPDATE\s+listings\s+SET\s+status_reason\b", re.IGNORECASE)
# A statement that changes a listing, as opposed to reading one.
WRITES_A_LISTING = re.compile(
    r"^\s*(?:UPDATE(?:\s+OR\s+\w+)?|DELETE\s+FROM|(?:INSERT|REPLACE)(?:\s+OR\s+\w+)?\s+INTO)\s+listings\b",
    re.IGNORECASE,
)
# A statement that has a plan: a question or a write, not a PRAGMA or DDL.
ASKS = re.compile(r"^\s*(?:--[^\n]*\n\s*)*(?:SELECT|WITH|UPDATE|DELETE|INSERT|REPLACE)\b", re.IGNORECASE)
# Words that can follow a table's name without being an alias for it.
NOT_AN_ALIAS = {
    "WHERE", "SET", "ON", "JOIN", "LEFT", "RIGHT", "FULL", "INNER", "OUTER", "CROSS", "NATURAL", "GROUP",
    "ORDER", "LIMIT", "USING", "INDEXED", "NOT", "WINDOW", "UNION", "EXCEPT", "INTERSECT", "HAVING",
    "VALUES", "RETURNING", "DEFAULT", "AND", "OR",
}


def ago(days: float) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")


def flat(platform: str, slug: str, address: str, *, unit_type: str | None = "one_bedroom",
         price: int | None = 1900) -> ListingCandidate:
    url = {
        "Zillow": f"https://www.zillow.com/homedetails/x/{slug}_zpid/",
        "Movoto": f"https://www.movoto.com/san-francisco-ca/{slug}/for-rent/",
    }[platform]
    return ListingCandidate(
        platform=platform, source_id=slug, title=address, original_url=url, price=price,
        neighborhood="Mission", listing_type="Apartment",
        summary="Entire apartment in a Victorian, whole place to yourself, sunny, near the park.",
        metadata={"address": address}, housing_kind="whole_unit", unit_type=unit_type,
    )


def room(slug: str, *, price: int = 1400) -> ListingCandidate:
    return ListingCandidate(
        platform="Craigslist", source_id=slug, title=f"Sunny room in a shared flat ({slug})",
        original_url=f"https://sfbay.craigslist.org/sfc/roo/d/sunny-room/{slug}.html", price=price,
        neighborhood="Inner Richmond", listing_type="Room",
        summary="Private room in a shared Victorian flat, quiet household.",
        metadata={}, housing_kind="room", unit_type=None,
    )


def put(repository: Repository, listing: ListingCandidate, score: int = 80, eligibility: str = "eligible") -> int:
    # A stored verdict rather than a scored one: what these tests read is how
    # the board is asked, not what the test deal makes of each home.
    listing_id, _ = repository.upsert_listing(listing, ScoreResult(score, ["Stored"], "", {}, eligibility=eligibility))
    return listing_id


def state(repository: Repository, listing_id: int) -> tuple[str, str | None]:
    with closing(PLAIN_CONNECT(repository.path)) as connection:
        return tuple(connection.execute(
            "SELECT status, status_reason FROM listings WHERE id = ?", (listing_id,)
        ).fetchone())


def stamp_of(path: Path) -> int:
    with closing(PLAIN_CONNECT(path)) as connection:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])


def schema(path: Path) -> list[tuple[Any, ...]]:
    with closing(PLAIN_CONNECT(path)) as connection:
        return connection.execute(
            "SELECT type, name, tbl_name, rootpage, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()


def schema_changes(path: Path) -> int:
    """SQLite's count of changes to the schema. An index dropped and made
    again can land on the page it had, and a trigger made again reads the
    same, but either moves this."""
    with closing(PLAIN_CONNECT(path)) as connection:
        return int(connection.execute("PRAGMA schema_version").fetchone()[0])


def made(path: Path) -> set[tuple[str, str]]:
    return {(kind, name) for kind, name, *_ in schema(path)}


def changes_counted(path: Path) -> int:
    with closing(PLAIN_CONNECT(path)) as connection:
        return int(connection.execute("SELECT value FROM listings_version WHERE id = 1").fetchone()[0])


def reads_of(
    path: Path, statements: Iterable[str], tables: tuple[str, ...] = YEAR_SCALE_TABLES
) -> list[tuple[str, str]]:
    """Every step of every plan that reads one of ``tables``, with its statement.

    A table is read under its own name or under an alias the statement gives
    it (the sweep asks of ``listings AS candidate`` and ``listings AS copy``).
    Each statement once: SQLite hands a trace the statement again for every
    row a trigger fires on.
    """
    wanted = "|".join(tables)
    found: list[tuple[str, str]] = []
    with closing(PLAIN_CONNECT(path)) as connection:
        for statement in dict.fromkeys(statements):
            if not ASKS.match(statement):
                continue
            names = set(tables) | {
                alias
                for alias in re.findall(rf"\b(?:{wanted})\s+(?:AS\s+)?([A-Za-z_]\w*)", statement, re.IGNORECASE)
                if alias.upper() not in NOT_AN_ALIAS
            }
            for row in connection.execute(f"EXPLAIN QUERY PLAN {statement}"):
                step = str(row[3])
                named = re.match(r"(?:SCAN|SEARCH) (?:TABLE )?(\w+)", step)
                if named and named.group(1) in names:
                    found.append((step, statement))
    return found


def whole(reads: Iterable[tuple[str, str]]) -> str:
    """The steps that read their table whole -- a scan of it, or of an index
    on it, or an index SQLite would build for itself by scanning it first --
    one to a line with the statement each is from. Empty when there are none."""
    return "\n".join(
        f"{step}  <-  {' '.join(statement.split())[:200]}"
        for step, statement in reads
        if step.startswith("SCAN") or "USING AUTOMATIC" in step
    )


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every statement run on a connection this test's thread opens from here
    on, as SQLite ran it -- values bound in, which is how it hands them to a
    trace. Only this thread's: a thread an earlier test left running would
    otherwise have what it asked charged to this one."""
    statements: list[str] = []
    here = threading.get_ident()

    def connect(*args, **kwargs):
        connection = PLAIN_CONNECT(*args, **kwargs)
        if threading.get_ident() == here:
            connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(sqlite3, "connect", connect)
    return statements


@pytest.fixture(scope="module")
def miniature(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A year of listings in miniature: a few of every kind of row a year holds.

    Flats listed on two sites, one copy leaving the size out; homes of every
    size and of none; rooms; homes ruled out and homes under the cut-off;
    starred, passed and noted homes; homes aged out a month ago, and long
    enough ago for retention to take; a copy a site has taken down and one
    nobody has confirmed for weeks; and a history of checks. Built once, and
    copied for each test, because the sweep changes it.
    """
    path = tmp_path_factory.mktemp("year-in-miniature") / "housing.sqlite3"
    board = Repository(path)
    board.initialize()
    sizes = ("one_bedroom", "three_bedroom", "two_bedroom", None, "studio")
    homes: list[list[int]] = []
    for index in range(100):
        address = f"{100 + index} Oak St #{index % 7 + 1}"
        size = sizes[index % len(sizes)]
        score = 30 + (index * 13) % 66
        eligibility = "ineligible" if index % 7 == 0 else "eligible"
        price = None if index % 6 == 0 else 1500 + 10 * index
        copies = [put(board, flat("Zillow", f"z{index}", address, unit_type=size, price=price), score, eligibility)]
        if index % 2 == 0:
            copies.append(put(
                board,
                flat("Movoto", f"m{index}", address, unit_type=None if index % 4 == 0 else size,
                     price=price + 75 if price and index % 3 == 0 else price),
                max(0, score - 4), eligibility,
            ))
        homes.append(copies)
    for index in range(50):
        homes.append([put(board, room(f"c{index}", price=1100 + 10 * index), 35 + index)])
    for starred in (homes[1], homes[8], homes[120]):
        board.set_listing_status(starred[0], "saved")
    for passed in (homes[3], homes[12], homes[125]):
        board.set_listing_status(passed[0], "dismissed")
    for noted in (homes[6], homes[130]):
        board.set_listing_note(noted[-1], "Asked about parking, waiting to hear")
    with closing(PLAIN_CONNECT(path)) as connection:
        connection.execute("UPDATE listings SET first_found = ?, last_seen = ? WHERE id % 3 = 1", (ago(40), ago(30)))
        connection.execute("UPDATE listings SET first_found = ?, last_seen = ? WHERE id % 9 = 4", (ago(300), ago(200)))
        connection.execute(
            "UPDATE listings SET metadata_json = json_set(metadata_json, '$.verified_inactive', json('true')) "
            "WHERE id = ?", (homes[10][-1],),
        )
        connection.execute(
            "UPDATE listings SET metadata_json = json_set(metadata_json, '$.last_verified_at', ?) WHERE id = ?",
            (ago(20), homes[14][-1]),
        )
        connection.commit()
    board.archive_stale_listings()
    for _ in range(6):
        run = board.begin_scan("scheduled")
        for platform, status in (("Zillow", "success"), ("Movoto", "success"), ("Craigslist", "error"),
                                 ("Redfin", "skipped")):
            source_run = board.begin_source_run(run, platform, search_url=f"https://example.test/{platform}")
            board.finish_source_run(
                source_run, status, seen=12 if status == "success" else 0,
                fetched=12 if status == "success" else 0,
                message="Connection reset by peer" if status == "error" else None,
            )
        board.finish_scan(run, "completed", seen=24, added=3, updated=21, failed=1)
    with closing(PLAIN_CONNECT(path)) as connection:
        kinds = {tuple(row) for row in connection.execute(
            "SELECT DISTINCT status, COALESCE(status_reason, '') FROM listings"
        )}
        shared = connection.execute(
            "SELECT COUNT(*) FROM (SELECT home_key FROM listings WHERE home_key <> '' "
            "GROUP BY home_key HAVING COUNT(*) > 1)"
        ).fetchone()[0]
        rows = connection.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
    # For the questions below to be asked of anything, all of it has to be there.
    assert {("active", ""), ("saved", "user"), ("dismissed", "user"), ("dismissed", "aged")} <= kinds, kinds
    assert shared >= 40, "flats listed on two sites are one home each"
    assert rows == 200
    return path


@pytest.fixture
def board(miniature: Path, tmp_path: Path) -> Repository:
    """A copy of the miniature of this test's own."""
    path = tmp_path / "housing.sqlite3"
    with closing(PLAIN_CONNECT(miniature)) as source, closing(PLAIN_CONNECT(path)) as copy:
        source.backup(copy)
    return Repository(path)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    data_dir = tmp_path / "data"
    preferences_path = tmp_path / "preferences.yaml"
    preferences_path.write_text(TEST_PREFERENCES, encoding="utf-8")
    return Settings(data_dir=data_dir, preferences_path=preferences_path,
                    database_path=data_dir / "housing.sqlite3", log_path=data_dir / "test.log")


# --------------------------------------------------------------------------
# starting
# --------------------------------------------------------------------------


def test_a_start_on_a_board_this_version_opened_last_repairs_nothing(
    repository: Repository, recorded: list[str]
) -> None:
    """The repair read every row, on every start: 0.6 s of each start on a
    year of listings, finding nothing. Only another version leaves anything
    to repair, and every version stamps its own number on the board as it
    starts, so a board carrying this version's number has nothing to find."""
    passed = put(repository, flat("Zillow", "1", "295 Buchanan St #105"))
    repository.set_listing_status(passed, "dismissed")
    put(repository, flat("Zillow", "2", "10 Oak St #3"))
    recorded.clear()

    Repository(repository.path).initialize()

    assert any("BEGIN IMMEDIATE" in statement for statement in recorded), "the start was not recorded"
    assert [statement for statement in recorded if REPAIR.search(statement)] == []
    scans = whole(reads_of(repository.path, recorded, ("listings",)))
    assert not scans, f"the start read the listings whole:\n{scans}"


@pytest.mark.parametrize(
    "stamp", [3, 4, SCHEMA_VERSION + 1], ids=["an older version", "the released version", "a newer version"]
)
def test_a_start_after_another_version_opened_the_board_repairs_what_it_left(
    repository: Repository, stamp: int
) -> None:
    """Any other number on the board means another version has been here since
    this one was, writing statuses its own way: a pass that says nobody's, a
    star or a restore over a home this version had aged out."""
    passed, starred, restored, aged, untouched = [
        put(repository, flat("Zillow", str(index), f"{index} Oak St #1")) for index in range(1, 6)
    ]
    with closing(PLAIN_CONNECT(repository.path)) as connection:
        connection.execute("UPDATE listings SET status = 'dismissed', status_reason = NULL WHERE id = ?", (passed,))
        connection.execute("UPDATE listings SET status = 'saved', status_reason = 'aged' WHERE id = ?", (starred,))
        connection.execute("UPDATE listings SET status = 'active', status_reason = 'aged' WHERE id = ?", (restored,))
        connection.execute("UPDATE listings SET status = 'dismissed', status_reason = 'aged' WHERE id = ?", (aged,))
        connection.execute(f"PRAGMA user_version = {stamp}")
        connection.commit()

    Repository(repository.path).initialize()

    assert state(repository, passed) == ("dismissed", "user")
    assert state(repository, starred) == ("saved", "user")
    assert state(repository, restored) == ("active", "user")
    assert state(repository, aged) == ("dismissed", "aged"), "a home the other version left aged out still is"
    assert state(repository, untouched) == ("active", None)
    assert stamp_of(repository.path) == SCHEMA_VERSION, "so the next start knows this version was here last"


def test_the_app_started_again_on_its_own_board_rewrites_no_home_and_reads_none_row_by_row(
    settings: Settings, recorded: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The start the definition of done times, through create_app: its
    migration, its connectors, its interrupted checks and its question of
    whether the board is scored by this code and this deal. The whole-file
    integrity check is the one full read a start keeps on purpose -- it turns
    a damaged file into a message naming it -- and it is a PRAGMA, not a
    question with a plan."""
    # The source as it was when the test began. The fingerprint is read off
    # the files, and a save to one of them between the two starts -- other
    # work in this tree -- would have the second rescore, rightly, for a
    # reason this test is not about.
    fingerprint = rescore_marker.scoring_fingerprint()
    monkeypatch.setattr(rescore_marker, "scoring_fingerprint", lambda: fingerprint)
    board = Repository(settings.database_path)
    board.initialize()
    for index in range(1, 6):
        put(board, flat("Zillow", str(index), f"{index} Oak St #2"))
    put(board, room("c1"))
    create_app(settings=settings, sources=[], enable_scheduler=False)
    recorded.clear()

    create_app(settings=settings, sources=[], enable_scheduler=False)

    assert any("quick_check" in statement for statement in recorded), "the second start was not recorded"
    assert [statement for statement in recorded if WRITES_A_LISTING.match(statement)] == []
    scans = whole(reads_of(settings.database_path, recorded, ("listings",)))
    assert not scans, f"the start read the listings whole:\n{scans}"


def test_a_start_makes_what_a_year_of_listings_needs_and_the_next_start_rebuilds_none_of_it(
    repository: Repository,
) -> None:
    """Built once, about two seconds an index on a year of listings, and free
    from then on: a start that rebuilt one would pay that on every start."""
    assert MADE_FOR_A_YEAR <= made(repository.path)
    listing_id = put(repository, flat("Zillow", "1", "295 Buchanan St #105"))
    repository.set_listing_status(listing_id, "saved")
    before, changed = schema(repository.path), schema_changes(repository.path)
    counted = changes_counted(repository.path)

    Repository(repository.path).initialize()

    assert schema(repository.path) == before, "a start that finds them made changes nothing"
    assert schema_changes(repository.path) == changed, "not even by making one again as it was"
    assert changes_counted(repository.path) == counted, (
        "and never winds the change counter back, or an answer remembered at a number "
        "could be handed out again for a different board"
    )


@pytest.mark.parametrize(
    "stamp", [4, SCHEMA_VERSION], ids=["the released version", "an earlier build of this version"]
)
def test_a_board_left_without_them_gains_them_on_its_next_start(repository: Repository, stamp: int) -> None:
    """The released version stamps 4 and makes none of it, so the first start
    after an update is the one that builds them. Nor may this version's own
    number stand in for having them: a build of it made before one of them
    existed stamped the same number."""
    listing_id = put(repository, flat("Zillow", "1", "295 Buchanan St #105"))
    with closing(PLAIN_CONNECT(repository.path)) as connection:
        for kind, name in sorted(MADE_FOR_A_YEAR, reverse=True):
            connection.execute(f"DROP {kind.upper()} IF EXISTS {name}")
        connection.execute(f"PRAGMA user_version = {stamp}")
        connection.commit()
    assert not MADE_FOR_A_YEAR & made(repository.path)

    Repository(repository.path).initialize()

    assert MADE_FOR_A_YEAR <= made(repository.path)
    counted = changes_counted(repository.path)
    repository.set_listing_status(listing_id, "saved")
    assert changes_counted(repository.path) > counted, "the counter counts from the first write after it"


# --------------------------------------------------------------------------
# asking
# --------------------------------------------------------------------------


# What the shortlist, Saved and Passed ask of the listings and the scan
# history, as the page asks it, and the sweep that ends every scan -- each with
# whether it reads either table at all. The rent table's key is a number the
# board keeps, not a count of the board.
QUESTIONS: dict[str, tuple[Callable[[Repository], Any], bool]] = {
    "the rooms tab": (
        lambda board: board.query_listings(60, housing_kind="room", view="active", outside_tabs=TABS), True),
    "the one-bedroom tab": (
        lambda board: board.query_listings(
            60, housing_kind="whole_unit", unit_types=("one_bedroom",), view="active", outside_tabs=TABS
        ), True),
    "the other homes tab": (
        lambda board: board.query_listings(60, housing_kind="other", view="active", outside_tabs=TABS), True),
    "the near matches": (
        lambda board: board.query_listings(
            60, housing_kind="whole_unit", unit_types=("one_bedroom",), view="near_matches", outside_tabs=TABS
        ), True),
    "what held homes back": (lambda board: board.exclusion_summary(60, "whole_unit", ("one_bedroom",)), True),
    "the filters' choices": (lambda board: board.filter_options(60, "whole_unit", ("one_bedroom",), TABS), True),
    "whether the other homes tab is offered": (lambda board: board.has_homes(60, "active", "other", TABS), True),
    "a page of saved homes": (
        lambda board: board.query_page(60, housing_kind="", view="saved", outside_tabs=TABS, limit=300), True),
    "a page of passed homes": (
        lambda board: board.query_page(60, housing_kind="", view="dismissed", outside_tabs=TABS, limit=300), True),
    "the rent medians": (lambda board: board.rent_observations(60), True),
    "the rent table's key": (lambda board: board.board_version(), False),
    "the recent checks": (lambda board: board.recent_scans(20), True),
    "the end-of-scan sweep": (lambda board: board.retire_old_listings(), True),
}


@pytest.mark.parametrize("question", list(QUESTIONS))
def test_the_question_reads_the_rows_it_is_about_not_a_year_of_them(
    question: str, board: Repository, recorded: list[str]
) -> None:
    """A search reads the rows a question is about; a scan reads every row a
    year has left in the table, which is what took the page to 5.7 s."""
    ask, reads_the_board = QUESTIONS[question]

    ask(board)

    assert [statement for statement in recorded if ASKS.match(statement)], "nothing was recorded, so nothing was read"
    reads = reads_of(board.path, recorded)
    scans = whole(reads)
    assert not scans, f"{question} read a table whole:\n{scans}"
    assert bool(reads) is reads_the_board, reads


def test_the_tab_question_finds_each_home_s_copies_through_the_shape_index(
    board: Repository, recorded: list[str]
) -> None:
    """By the home, in the order that decides its shape, from an index carrying
    every column the shape is read from -- so shaping a home never reads a row
    itself, whose last columns sit behind kilobytes of JSON."""
    board.query_listings(60, housing_kind="whole_unit", unit_types=("one_bedroom",), view="active",
                         outside_tabs=TABS)

    steps = [step for step, _ in reads_of(board.path, recorded, ("listings",))]
    assert any(
        re.match(r"SEARCH (?:TABLE )?listings USING COVERING INDEX idx_listings_home_shape\b", step) for step in steps
    ), steps
