"""WO-5 item 5: a page asks the board everything on one connection.

A dashboard request opened 29 SQLite connections -- one for every question
the page asked, one or more per source on the status panel -- and each paid
the open, the pragmas and a cold page cache: ~23 ms a page in churn alone,
before a row was read. A request's questions now share one connection, lent
to one block at a time and closed when the request ends.

Sharing is only safe while every block still behaves as though the
connection were its own, the way closing it used to make it. A write a block
left uncommitted, or that failed half way, must never be committed by the
next block's commit or hold the write lock for the rest of the page; a block
asked inside another must neither see nor commit the outer one's work; a
connection a block failed on is never lent again; nothing is held open
between requests; and no other thread -- the scan, its lane, the scheduler,
another request -- is ever handed a request's connection. Each test here
forces one of those on real connections, and checks the connection really
was shared, since a test of sharing passes vacuously when nothing is.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import closing, contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sf_housing.app import create_app
from sf_housing.database import SCHEMA_VERSION, Repository
from sf_housing.models import ListingCandidate, ScoreResult
from sf_housing.settings import Settings
from tests.conftest import TEST_PREFERENCES

# Taken before any test replaces it, for the reads and writes the tests make
# on connections of their own -- another program's, never the app's.
REAL_CONNECT = sqlite3.connect


class Spied(sqlite3.Connection):
    """A real connection that remembers which thread opened it, what it was
    asked, and whether anybody closed it."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.opened_on = threading.get_ident()
        self.asked: list[str] = []
        self.was_closed = False

    def execute(self, sql, *args, **kwargs):
        self.asked.append(" ".join(str(sql).split()))
        return super().execute(sql, *args, **kwargs)

    def close(self) -> None:
        self.was_closed = True
        super().close()

    @property
    def questions(self) -> list[str]:
        """What it was asked, less the two pragmas every connection opens with."""
        return [sql for sql in self.asked if not sql.startswith(("PRAGMA foreign_keys", "PRAGMA busy_timeout"))]


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> list[Spied]:
    """Every connection opened from here on, in order: real ones, spied on."""
    connections: list[Spied] = []

    def connect(*args, **kwargs):
        kwargs.setdefault("factory", Spied)
        connection = REAL_CONNECT(*args, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", connect)
    return connections


def room(slug: str, address: str, *, platform: str = "Zillow", price: int | None = 1500, score: int = 80,
         neighborhood: str = "Mission",
         summary: str = "Private room in a sunny shared Victorian flat, near the park.") -> tuple:
    url = {
        "Zillow": f"https://www.zillow.com/homedetails/x/{slug}_zpid/",
        "Movoto": f"https://www.movoto.com/san-francisco-ca/{slug}/for-rent/",
    }[platform]
    listing = ListingCandidate(
        platform=platform, source_id=slug, title=address, original_url=url, price=price,
        neighborhood=neighborhood, listing_type="Room", summary=summary,
        metadata={"address": address}, housing_kind="room",
    )
    return listing, ScoreResult(score, ["Stored"], "", {}, eligibility="eligible")


def put(repository: Repository, pair) -> int:
    listing, result = pair
    listing_id, _ = repository.upsert_listing(listing, result)
    return listing_id


def notes(repository: Repository) -> dict[int, str]:
    """What is stored, read on a connection of the test's own."""
    with closing(REAL_CONNECT(repository.path)) as connection:
        return dict(connection.execute("SELECT id, note FROM listings"))


def write_elsewhere(repository: Repository, sql: str, *parameters, wait: float = 0.5) -> None:
    """A write by another program -- a scan run from the command line, a
    second copy of the app -- on a connection that gives up after ``wait``."""
    with closing(REAL_CONNECT(repository.path, timeout=wait)) as connection:
        connection.execute(sql, parameters)
        connection.commit()


def run_on_a_thread(name: str, work) -> None:
    """``work`` on a thread of its own, the way the scan runs; re-raises what it raised."""
    raised: list[BaseException] = []

    def target() -> None:
        try:
            work()
        except BaseException as exc:  # handed back to the test below
            raised.append(exc)

    thread = threading.Thread(target=target, name=name)
    thread.start()
    thread.join(timeout=20)
    assert not thread.is_alive(), f"{name} never finished"
    if raised:
        raise raised[0]


# --------------------------------------------------------------------------
# the pages
# --------------------------------------------------------------------------


class Source:
    """A row on the status panel: each one used to cost the page three
    connections of its own."""

    mode = "automatic"
    manual_reason = None

    def __init__(self, platform: str):
        self.platform = platform
        self.source_key = platform
        self.search_url = f"https://example.test/{platform.lower()}"


@pytest.fixture
def app_and_repository(tmp_path: Path):
    data_dir = tmp_path / "data"
    preferences_path = tmp_path / "preferences.yaml"
    preferences_path.write_text(TEST_PREFERENCES, encoding="utf-8")
    settings = Settings(data_dir=data_dir, preferences_path=preferences_path,
                        database_path=data_dir / "housing.sqlite3", log_path=data_dir / "test.log")
    repository = Repository(settings.database_path)
    repository.initialize()
    sources = [Source(name) for name in ("Craigslist", "Zillow", "Movoto", "SpareRoom", "Redfin")]
    return create_app(settings=settings, sources=sources, enable_scheduler=False), repository


def stock(repository: Repository) -> list[int]:
    """A board with something on every page: a shortlist, a home no site
    prices (which the shortlist ranks by estimate), a starred, a passed and a
    nearly matching home, and one home on a second site."""
    homes = [
        put(repository, room("1", "1 Oak St #1", price=1400)),
        put(repository, room("2", "2 Oak St #1", price=1650, neighborhood="NOPA")),
        put(repository, room("3", "3 Oak St #1", price=1900, neighborhood="Inner Richmond")),
        put(repository, room("4", "4 Oak St #1", price=None)),
        put(repository, room("5", "5 Oak St #1", price=1200)),
        put(repository, room("6", "6 Oak St #1", price=1300)),
        put(repository, room("7", "7 Oak St #1", price=1500, score=55)),
        put(repository, room("m1", "1 Oak St #1", platform="Movoto", price=1450)),
    ]
    repository.set_listing_status(homes[4], "saved")
    repository.set_listing_status(homes[5], "dismissed")
    return homes


# Each page, and a home it shows -- so a page that quietly read nothing
# cannot pass for one that read the board on a single connection.
PAGES = {
    "/": "2 Oak St #1",  # the shortlist, and its status panel with a row per source
    "/?view=all": "6 Oak St #1",  # the Archive
    "/?view=saved": "5 Oak St #1",
    "/?view=dismissed": "6 Oak St #1",
    "/?view=near_matches": "7 Oak St #1",
    "/listings.csv": "4 Oak St #1",
    "/listings.csv?view=all": "6 Oak St #1",
    "/listings/{home}": "1 Oak St #1",
    "/preferences": None,
    "/alerts": None,
}


@pytest.mark.parametrize("page", PAGES)
def test_a_page_opens_one_connection_however_many_questions_it_asks(app_and_repository, opened, page: str) -> None:
    application, repository = app_and_repository
    homes = stock(repository)

    per_request = []
    with TestClient(application) as client:
        for _ in range(2):  # cold, then with every answer the board remembers
            before = len(opened)
            response = client.get(page.format(home=homes[0]))
            assert response.status_code == 200, response.text[:500]
            per_request.append(len(opened) - before)
    if PAGES[page]:
        assert PAGES[page] in response.text

    assert per_request == [1, 1]


@pytest.mark.parametrize("page", ["/support", "/support/report.json"])
def test_the_support_page_reads_the_file_afresh_for_its_two_checks_and_shares_one_connection_for_the_rest(
    app_and_repository, opened, page: str
) -> None:
    """The integrity verdict and the schema version are about the file as it
    is now, so each opens its own; everything else the page asks shares one."""
    application, repository = app_and_repository
    stock(repository)

    with TestClient(application) as client:
        before = len(opened)
        assert client.get(page).status_code == 200
    connections = opened[before:]

    checks = [c for c in connections if {"PRAGMA integrity_check(1)", "PRAGMA user_version"} & set(c.asked)]
    assert sorted(c.questions for c in checks) == [["PRAGMA integrity_check(1)"], ["PRAGMA user_version"]]
    assert len(connections) == 3
    assert all(c.was_closed for c in connections)


def test_nothing_is_held_open_between_requests(app_and_repository, opened) -> None:
    """Closed when the request ends, so the write-ahead log is folded back
    into the file and Repair, an install or Windows can replace it."""
    application, repository = app_and_repository
    homes = stock(repository)
    wal = repository.path.with_name(repository.path.name + "-wal")
    with repository.connection() as connection:
        connection.execute("SELECT COUNT(*) FROM listings").fetchone()
        assert wal.exists(), "an open connection keeps a write-ahead log beside the file"
    assert not wal.exists(), "and it goes when the last connection closes"

    with TestClient(application) as client:
        for page, status in [*((page, 200) for page in PAGES), ("/listings/{gone}", 404)]:
            before = len(opened)
            assert client.get(page.format(home=homes[0], gone=max(homes) + 1)).status_code == status
            left_open = [c.questions[:2] for c in opened[before:] if not c.was_closed]
            assert opened[before:] and left_open == [], page
            assert not wal.exists(), f"{page} left a connection open"


def test_a_page_that_fails_closes_its_connection_and_the_next_page_opens_one(
    app_and_repository, opened, monkeypatch: pytest.MonkeyPatch
) -> None:
    application, repository = app_and_repository
    stock(repository)
    served = application.state.repository
    summarise = served.exclusion_summary
    failures = [True]

    def fails_once(*args, **kwargs):
        # Fails on the request's own connection, after the page has already
        # asked it something, the way a busy board or a full disk would.
        if failures:
            failures.pop()
            with served.connection() as connection:
                connection.execute("SELECT reason FROM a_table_the_disk_lost")
        return summarise(*args, **kwargs)

    monkeypatch.setattr(served, "exclusion_summary", fails_once)
    with TestClient(application, raise_server_exceptions=False) as client:
        before = len(opened)
        failed = client.get("/")
        during_failure = opened[before:]
        before = len(opened)
        recovered = client.get("/")
        after_failure = opened[before:]

    assert failed.status_code == 500 and not failures, "the page really failed"
    assert during_failure and all(c.was_closed for c in during_failure)
    assert recovered.status_code == 200 and "2 Oak St #1" in recovered.text
    assert all(c.was_closed for c in after_failure)
    assert len(after_failure) == 1


# --------------------------------------------------------------------------
# one request's questions
# --------------------------------------------------------------------------


def test_a_request_s_questions_share_one_connection_that_is_closed_when_the_request_ends(
    repository: Repository, opened
) -> None:
    home = put(repository, room("1", "1 Oak St #1"))
    opened.clear()  # only what the request opens

    with repository.reusing_one_connection():
        assert repository.count_listings() == 1
        repository.query_listings(minimum_score=0, housing_kind="room")
        repository.exclusion_summary(60, "room", ())
        assert repository.set_listing_note(home, "Asked about the deposit")
        held = [c for c in opened if not c.was_closed]

    assert notes(repository)[home] == "Asked about the deposit"
    assert held == opened, "held for the whole request"
    assert all(c.was_closed for c in opened), "and closed when it ended"
    assert len(opened) == 1


def test_a_write_a_question_left_uncommitted_is_undone_not_committed_by_the_next_question(
    repository: Repository, opened
) -> None:
    """Closing used to throw such a write away. Kept for the next block, it
    would be committed by that block's commit, and until then hold the write
    lock against the scan."""
    forgotten = put(repository, room("1", "1 Oak St #1"))
    meant = put(repository, room("2", "2 Oak St #1"))

    with repository.reusing_one_connection():
        with repository.connection() as first:
            first.execute("UPDATE listings SET note = 'never committed' WHERE id = ?", (forgotten,))
        # Another writer is not kept waiting on it (it would give up at once).
        write_elsewhere(repository, "UPDATE listings SET title = title WHERE id = ?", meant)
        with repository.connection() as second:
            second.execute("UPDATE listings SET note = 'saved on purpose' WHERE id = ?", (meant,))
            second.commit()

    assert notes(repository) == {forgotten: "", meant: "saved on purpose"}
    assert second is first, "both were the request's one connection"


def test_a_write_that_failed_half_way_is_not_committed_by_the_next_question(
    repository: Repository, opened
) -> None:
    half = put(repository, room("1", "1 Oak St #1"))
    whole = put(repository, room("2", "2 Oak St #1"))

    with repository.reusing_one_connection():
        with repository.connection() as asked:
            asked.execute("SELECT COUNT(*) FROM listings").fetchone()
        with pytest.raises(RuntimeError):
            with repository.connection() as failing:
                failing.execute("UPDATE listings SET note = 'half written' WHERE id = ?", (half,))
                raise RuntimeError("the disk filled up half way through")
        with repository.connection() as next_question:
            next_question.execute("UPDATE listings SET note = 'written whole' WHERE id = ?", (whole,))
            next_question.commit()

    assert notes(repository) == {half: "", whole: "written whole"}
    assert failing is asked, "the failed write was on the request's one connection"


def test_a_connection_a_question_failed_on_is_never_lent_again(repository: Repository, opened) -> None:
    put(repository, room("1", "1 Oak St #1"))

    with repository.reusing_one_connection():
        with repository.connection() as first:
            first.execute("SELECT 1").fetchone()
        with pytest.raises(sqlite3.OperationalError):
            with repository.connection() as failing:
                failing.execute("SELECT reason FROM a_table_the_disk_lost")
        closed_at_once = failing.was_closed
        with repository.connection() as after:
            stored = after.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
        with repository.connection() as later:
            later.execute("SELECT 1").fetchone()

    assert after is not failing, "whatever went wrong, the next question starts from a new connection"
    assert closed_at_once, "closed when the question failed, not left for the end of the request"
    assert stored == 1
    assert later is after, "and the new one is the request's from then on"
    assert failing is first, "the question failed on the request's one connection"


def test_a_connection_that_could_not_be_handed_back_is_never_lent_again(repository: Repository, opened) -> None:
    """Handing a connection back rolls back what its block left open; when
    even that fails -- a disk gone away, or as here a connection closed under
    the block -- the next question starts from a new one."""
    with repository.reusing_one_connection():
        with repository.connection() as first:
            first.execute("SELECT 1").fetchone()
        with repository.connection() as unusable:
            unusable.close()
        with repository.connection() as after:
            stored = after.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
        with repository.connection() as later:
            later.execute("SELECT 1").fetchone()

    assert after is not unusable and stored == 0
    assert later is after
    assert unusable is first, "it was the request's one connection"


def test_a_question_asked_inside_another_gets_a_connection_of_its_own(repository: Repository, opened) -> None:
    """Lent to one block at a time: sharing a connection with the block it
    was asked inside, a question would see that block's uncommitted write,
    and its commit would commit it."""
    home = put(repository, room("1", "1 Oak St #1"))

    with repository.reusing_one_connection():
        with repository.connection() as outer:
            outer.execute("UPDATE listings SET note = 'the outer write' WHERE id = ?", (home,))
            with repository.connection() as inner:
                seen_inside = inner.execute("SELECT note FROM listings WHERE id = ?", (home,)).fetchone()[0]
                inner.commit()
            inner_closed = inner.was_closed
            stored_meanwhile = notes(repository)[home]
            outer.commit()
        with repository.connection() as next_question:
            next_question.execute("SELECT 1").fetchone()

    assert inner is not outer
    assert seen_inside == "", "the inner question did not see the outer one's uncommitted write"
    assert stored_meanwhile == "", "and its commit did not commit it"
    assert notes(repository)[home] == "the outer write", "the outer block still committed its own"
    assert inner_closed, "the inner question's connection was its own to close"
    assert next_question is outer, "the request's connection was lent again afterwards"


def test_a_scope_entered_inside_another_is_part_of_it(repository: Repository, opened) -> None:
    opened.clear()
    with repository.reusing_one_connection():
        repository.count_listings()
        with repository.reusing_one_connection():
            repository.count_listings()
        still_open = not opened[0].was_closed
        repository.count_listings()

    assert still_open, "the inner scope ending did not close the request's connection"
    assert opened[0].was_closed
    assert len(opened) == 1


def test_a_request_sees_what_another_writer_committed_between_its_questions(repository: Repository, opened) -> None:
    home = put(repository, room("1", "1 Oak St #1"))
    passed_elsewhere = put(repository, room("2", "2 Oak St #1"))

    def shown() -> list[int]:
        return [row["id"] for row in repository.query_listings(minimum_score=0, housing_kind="room")]

    opened.clear()
    with repository.reusing_one_connection():
        shown_before, note_before = shown(), repository.listing(home)["note"]
        write_elsewhere(repository, "UPDATE listings SET note = 'Tour on Saturday' WHERE id = ?", home)
        write_elsewhere(
            repository, "UPDATE listings SET status = 'dismissed', status_reason = 'user' WHERE id = ?",
            passed_elsewhere,
        )
        shown_after, note_after = shown(), repository.listing(home)["note"]

    assert (note_before, note_after) == ("", "Tour on Saturday")
    assert passed_elsewhere in shown_before and passed_elsewhere not in shown_after
    assert len(opened) == 1, "every question was asked on the request's one connection"


def test_start_up_the_integrity_check_and_the_schema_version_read_the_file_afresh(
    repository: Repository, opened
) -> None:
    """Asked inside a request, each still opens a connection of its own:
    start-up's migration takes its own transaction, and the two checks are a
    verdict on the file as it is now."""
    opened.clear()
    with repository.reusing_one_connection():
        repository.count_listings()
        request = opened[0]
        assert repository.integrity_check() == (True, "ok")
        assert repository.schema_version() == SCHEMA_VERSION
        repository.initialize()
        fresh_closed = all(c.was_closed for c in opened[1:])
        repository.count_listings()

    for check in ("PRAGMA integrity_check(1)", "PRAGMA user_version", "PRAGMA quick_check(1)"):
        asked_on = [c for c in opened if check in c.asked]
        assert asked_on and request not in asked_on, check
    assert fresh_closed, "each let the file go as soon as it had its answer"
    assert [c for c in opened if "SELECT COUNT(*) FROM listings" in c.asked] == [request], (
        "the request's own questions all went to its one connection"
    )


# --------------------------------------------------------------------------
# other threads, other boards
# --------------------------------------------------------------------------


def test_two_requests_on_two_threads_never_share_a_connection(
    repository: Repository, opened, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A connection belongs to the thread that opened it: shared, SQLite
    refuses it outright, or two requests commit and roll back each other's
    work."""
    steps = 100
    homes = {
        name: [put(repository, room(f"{name}{n}", f"{n} {name.title()} St #1")) for n in range(4)]
        for name in ("left", "right")
    }
    handed: dict[str, list[sqlite3.Connection]] = {"left": [], "right": []}
    lend = repository.connection

    @contextmanager
    def recorded(*args, **kwargs):
        with lend(*args, **kwargs) as connection:
            handed[threading.current_thread().name].append(connection)
            yield connection

    monkeypatch.setattr(repository, "connection", recorded)
    both_inside = threading.Barrier(2, timeout=10)
    raised: list[BaseException] = []

    def request(name: str) -> None:
        try:
            with repository.reusing_one_connection():
                both_inside.wait()
                for step in range(steps):
                    assert repository.set_listing_note(homes[name][step % 4], f"{name} {step}")
                    repository.exclusion_summary(60, "room", ())
                    repository.query_listings(minimum_score=0, housing_kind="room")
        except BaseException as exc:
            raised.append(exc)

    threads = [threading.Thread(target=request, args=(name,), name=name) for name in handed]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not any(thread.is_alive() for thread in threads)
    assert raised == []
    stored = notes(repository)
    for name in handed:
        assert [stored[home] for home in homes[name]] == [f"{name} {steps - 4 + n}" for n in range(4)]
    left, right = ({id(c) for c in handed[name]} for name in ("left", "right"))
    assert left.isdisjoint(right)
    for thread in threads:
        assert {c.opened_on for c in handed[thread.name]} == {thread.ident}
    assert len(left) == len(right) == 1, "each request was answered from its one connection throughout"


def test_a_scan_asking_while_a_request_holds_its_connection_is_given_its_own(
    repository: Repository, opened
) -> None:
    """The scan, its lane and the scheduler keep a connection per question,
    whatever a request on another thread is holding."""
    home = put(repository, room("1", "1 Oak St #1"))
    scan: list[Spied] = []

    def scan_writes_a_note() -> None:
        with repository.connection() as connection:
            connection.execute("UPDATE listings SET note = ? WHERE id = ?", (f"scan {len(scan)}", home))
            connection.commit()
            scan.append(connection)

    with repository.reusing_one_connection():
        with repository.connection() as request:
            request.execute("SELECT 1").fetchone()
            run_on_a_thread("scan", scan_writes_a_note)  # while the request's connection is lent
        run_on_a_thread("scan", scan_writes_a_note)  # while it is held between questions
        with repository.connection() as again:
            seen = again.execute("SELECT note FROM listings WHERE id = ?", (home,)).fetchone()[0]

    assert len(scan) == 2 and all(c is not request for c in scan)
    assert all(c.was_closed for c in scan)
    assert seen == "scan 1", "the request saw what the scan committed"
    assert again is request


def test_a_request_on_one_board_never_answers_from_another_board_s_connection(
    tmp_path: Path, repository: Repository, opened
) -> None:
    """A connection belongs to one file as well as one thread."""
    another_board = Repository(tmp_path / "another" / "housing.sqlite3")
    another_board.initialize()
    put(repository, room("1", "1 Oak St #1"))
    put(repository, room("2", "2 Oak St #1"))
    put(another_board, room("9", "9 Pine St #1"))
    opened.clear()

    with repository.reusing_one_connection():
        ours = repository.count_listings()
        theirs = another_board.count_listings()
        ours_again = repository.count_listings()

    assert (ours, theirs, ours_again) == (2, 1, 2)
    assert len(opened) == 2, "one for the request's board, and the other board's own"


def test_outside_a_request_every_question_has_a_connection_of_its_own(repository: Repository, opened) -> None:
    """How the scan and the scheduler ask, as before: nothing lent, nothing kept."""
    home = put(repository, room("1", "1 Oak St #1"))
    opened.clear()

    repository.count_listings()
    repository.set_listing_note(home, "Asked about parking")
    with repository.connection() as first:
        first.execute("SELECT 1").fetchone()
    with repository.connection() as second:
        second.execute("SELECT 1").fetchone()

    assert first is not second
    assert len(opened) == 4 and all(c.was_closed for c in opened)
