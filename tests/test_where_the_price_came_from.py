"""The number in the price column says which of three things it is.

The owner's complaint: "some listings didn't show price and you had a price
there, if its estimated say that, if its previous say that, just needs to be
flagged." A figure with no provenance is worse than no figure, because the
reader plans around it -- and the one the app worked out itself is the figure
they would most want to know the origin of.

Three states and no fourth that puts a number on the page: a rent the source
is still publishing, printed plain; the last rent it published before it
stopped listing the home (``UNSEEN_DEMOTE_AFTER``, the same absence the
ranking already acts on); and the neighbourhood median, where nobody ever
published a rent at all. A home no site prices and no median covers still
reads "Unknown", as it always has.

The line these tests guard hardest: an estimate may never be mistaken for the
asking rent. It carries the word wherever it is printed, and it never reaches
the export's Price column, which a spreadsheet sorts, filters and totals.
"""

from __future__ import annotations

import csv
import io
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sf_housing.app import create_app
from sf_housing.database import UNSEEN_DEMOTE_AFTER, Repository
from sf_housing.models import ListingCandidate, ScoreResult
from sf_housing.rent_estimate import (
    MINIMUM_SAMPLE,
    PRICE_ESTIMATED,
    PRICE_LAST_SEEN,
    PRICE_STATED,
    PRICE_UNKNOWN,
    RentTable,
    mark_price_basis,
)
from sf_housing.settings import Settings
from tests.conftest import TEST_PREFERENCES


# What the board's Bernal Heights rooms let for, and what an unpriced Bernal
# room is therefore estimated at. Distinctive digits, so a test can tell the
# estimate from every other number on the page.
BERNAL_RENT = 1234
# Long enough that the source has stopped returning the home, short enough
# that it is still on the shortlist rather than in Near matches.
QUIET_HOURS = UNSEEN_DEMOTE_AFTER.total_seconds() / 3600 + 12


def room(
    slug: str, neighborhood: str, price: int | None, *, score: int = 70
) -> tuple[ListingCandidate, ScoreResult]:
    listing = ListingCandidate(
        platform="Craigslist",
        source_id=slug,
        title=f"Private room {slug}",
        original_url=f"https://sfbay.craigslist.org/roo/d/{slug}.html",
        price=price,
        neighborhood=neighborhood,
        listing_type="Room/share",
        summary="A private room in a shared house, flexible lease, 4 roommates, communal garden.",
        metadata={"property_type": "house", "rooms_in_property": "4"},
    )
    return listing, ScoreResult(score, ["Stored"], "", {}, eligibility="eligible")


def _hours_ago(hours: float) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours)).isoformat(timespec="seconds")


def stopped_appearing(repository: Repository, listing_id: int, hours: float) -> None:
    """Put a home's last sighting ``hours`` back, with its source still searching.

    Both halves matter: the rule counts a home's absence against its own
    source's last successful search, so without the search on record the home
    is not absent at all, it is merely stored on a board nobody has scanned.
    """
    with repository.connection() as connection:
        connection.execute("UPDATE listings SET last_seen = ? WHERE id = ?", (_hours_ago(hours), listing_id))
        scan = connection.execute(
            "INSERT INTO scan_runs (trigger, status, started_at) VALUES ('test', 'completed', ?)",
            (_hours_ago(0),),
        ).lastrowid
        connection.execute(
            """INSERT INTO source_runs (scan_run_id, platform, status, started_at, listings_seen, covered)
               VALUES (?, 'Craigslist', 'success', ?, 40, 1)""",
            (scan, _hours_ago(0)),
        )
        connection.commit()


@pytest.fixture
def board(tmp_path: Path):
    """A shortlist with one home of each kind on it.

    ``stated`` is a rent Craigslist is still publishing, ``quiet`` one it
    published before it stopped returning the home, and ``unpriced`` a home
    nobody has ever put a rent on, in an area the board knows the rents of.
    """
    data_dir = tmp_path / "data"
    preferences_path = tmp_path / "preferences.yaml"
    preferences_path.write_text(TEST_PREFERENCES, encoding="utf-8")
    settings = Settings(
        data_dir=data_dir,
        preferences_path=preferences_path,
        database_path=data_dir / "housing.sqlite3",
        log_path=data_dir / "test.log",
    )
    repository = Repository(settings.database_path)
    repository.initialize()
    # The sample the median is taken from. Passed, so none of them is on the
    # page to be mistaken for the homes the assertions are about.
    for index in range(MINIMUM_SAMPLE):
        sample, result = room(f"sample{index}", "Bernal Heights", BERNAL_RENT)
        sample_id, _ = repository.upsert_listing(sample, result)
        repository.set_listing_status(sample_id, "dismissed")
    homes = {
        name: repository.upsert_listing(*room(name, area, price))[0]
        for name, area, price in (
            ("stated", "Mission", 1800),
            ("quiet", "Mission", 1750),
            ("unpriced", "Bernal Heights", None),
        )
    }
    stopped_appearing(repository, homes["quiet"], QUIET_HOURS)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    return application, repository, homes


def price_cells(page: str) -> dict[str, str]:
    """Each home's price column, by the title of the row it belongs to."""
    cells = {}
    for row in page.split('<tr class="listing-row')[1:]:
        cell = re.search(r'<td class="cell-price">(.*?)</td>', row, re.S)
        title = re.search(r"Private room ([a-z0-9]+)", row)
        if cell and title:
            cells[title.group(1)] = cell.group(1)
    return cells


def sheet_rows(text: str) -> dict[str, dict[str, str]]:
    return {row["Address"]: row for row in csv.DictReader(io.StringIO(text))}


# --------------------------------------------------------------------------
# which of the three it is
# --------------------------------------------------------------------------


def test_a_rent_its_source_still_publishes_is_printed_with_no_marker() -> None:
    """A marker on every row is a marker nobody reads.

    The ordinary case is a rent the site is showing today, and it has to stay
    ordinary: only the two figures a reader could be misled by are annotated.
    """
    item = {"price": 1800, "home_price": 1800, "still_listed_by_source": True}

    mark_price_basis([item], RentTable({}, {}))

    assert item["price_basis"] == PRICE_STATED
    assert item["estimated_price"] is None


def test_a_rent_from_a_source_that_has_stopped_listing_the_home_is_a_last_seen_price() -> None:
    """The board went on printing a bare rent for homes nobody was advertising.

    The same absence the ranking already demotes for, said in the price
    column: a Zillow flat at $2,775 that went off-market kept printing $2,775
    as though somebody would still take it.
    """
    item = {"price": 2775, "home_price": 2775, "still_listed_by_source": False}

    mark_price_basis([item], RentTable({}, {}))

    assert item["price_basis"] == PRICE_LAST_SEEN
    assert item["estimated_price"] is None


def test_a_home_nobody_priced_shows_the_median_and_calls_it_an_estimate() -> None:
    """The column read "Unknown" while the row's place had been decided by a
    figure the reader was never shown."""
    item = {
        "price": None,
        "home_price": None,
        "neighborhood": "Mission",
        "housing_kind": "whole_unit",
        "unit_type": "studio",
    }

    mark_price_basis([item], RentTable.from_observations([("Mission", "studio", 2095)] * MINIMUM_SAMPLE))

    assert item["price_basis"] == PRICE_ESTIMATED
    assert item["estimated_price"] == 2095


def test_a_home_the_board_knows_too_few_of_its_kind_to_price_still_reads_unknown() -> None:
    """Inventing a figure from two homes would be worse than the blank it
    replaced; too small a sample has always meant no estimate at all."""
    item = {
        "price": None,
        "home_price": None,
        "neighborhood": "Mission",
        "housing_kind": "whole_unit",
        "unit_type": "four_bedroom",
    }

    mark_price_basis([item], RentTable.from_observations([("Mission", "studio", 2095)] * MINIMUM_SAMPLE))

    assert item["price_basis"] == PRICE_UNKNOWN
    assert item["estimated_price"] is None


def test_a_home_another_site_has_priced_is_never_given_an_estimate() -> None:
    """One site stating no rent is not a home nobody has priced.

    The shortlist already ranks the home at the rent the other site quotes and
    the row names both, so an estimate here would be the app talking over a
    figure somebody actually published.
    """
    item = {
        "price": None,
        "home_price": 3400,
        "neighborhood": "Mission",
        "housing_kind": "whole_unit",
        "unit_type": "studio",
    }

    mark_price_basis([item], RentTable.from_observations([("Mission", "studio", 2095)] * MINIMUM_SAMPLE))

    assert item["price_basis"] == PRICE_UNKNOWN
    assert item["estimated_price"] is None


def test_a_stored_home_with_no_housing_kind_is_priced_as_the_room_it_is_read_as() -> None:
    """The column and the ranking must not disagree about which homes they
    can price at all.

    A row whose housing kind was never stored is read as a room everywhere
    else (``Repository._row_to_candidate``), so the ranking estimates it; a
    price column that skipped it would place the home by a figure and then
    print "Unknown" in the cell that figure decided.
    """
    item = {"price": None, "home_price": None, "neighborhood": "Mission", "housing_kind": None}

    mark_price_basis([item], RentTable.from_observations([("Mission", "room", 1495)] * MINIMUM_SAMPLE))

    assert item["estimated_price"] == 1495


def test_a_row_that_cannot_say_whether_its_source_still_lists_it_is_not_flagged() -> None:
    """Absence of evidence never costs a home anything, here as everywhere.

    A row shaped outside the shortlist query -- a single listing's own page --
    carries no answer about its source's searches, and reading that silence as
    "no longer listed" would put an amber marker on a live listing.
    """
    item = {"price": 1800, "home_price": 1800}

    mark_price_basis([item], RentTable({}, {}))

    assert item["price_basis"] == PRICE_STATED


# --------------------------------------------------------------------------
# on the page
# --------------------------------------------------------------------------


def test_the_price_column_tells_the_three_apart(board) -> None:
    """All three printed the same bare number, or nothing at all.

    A reader comparing rows could not see that one rent was a fortnight old
    and another was the app's own arithmetic.
    """
    application, _, _ = board
    with TestClient(application) as client:
        cells = price_cells(client.get("/?housing=room&view=active&sort=score").text)

    assert "$1,800" in cells["stated"] and "price-basis" not in cells["stated"]
    assert "$1,750" in cells["quiet"] and "Last seen" in cells["quiet"]
    assert f"${BERNAL_RENT:,}" in cells["unpriced"] and "Estimated" in cells["unpriced"]


def test_each_marker_opens_one_sentence_on_a_click(board) -> None:
    """A marker that only says "Estimated" leaves the reader to guess at
    estimated from what; a hover tooltip says it to a mouse alone."""
    application, _, _ = board
    with TestClient(application) as client:
        cells = price_cells(client.get("/?housing=room&view=active&sort=score").text)

    for name, sentence in (
        ("quiet", "The rent Craigslist showed when it last listed this home"),
        ("unpriced", "No site published a rent, so this is the median for homes its size in Bernal Heights."),
    ):
        opens = re.search(r"<details[^>]*>(.*?)</details>", cells[name], re.S)
        assert opens, f"{name} has no marker to open"
        assert "<summary>" in opens.group(1) and sentence in opens.group(1)
        assert sentence not in re.sub(r"<details.*?</details>", "", cells[name], flags=re.S), (
            "the sentence is a tooltip as well as a disclosure"
        )


def test_the_estimate_is_marked_on_a_narrow_screen_too(board) -> None:
    """The price column is hidden below 1280px and this line replaces it.

    Left alone it would have printed "Price unknown" for a home whose rent the
    wide table now estimates -- or, worse for a later reader, the bare figure.
    """
    application, _, _ = board
    with TestClient(application) as client:
        page = client.get("/?housing=room&view=active&sort=score").text

    compact = re.findall(r'<span class="meta-price">(.*?)</span>', page)
    assert f"Estimated &#8776;${BERNAL_RENT:,}/mo" in compact
    assert any(line.startswith("$1,750/mo, last seen ") for line in compact)
    assert "$1,800/mo" in compact


def test_the_price_says_where_it_came_from_whatever_the_page_is_sorted_by(board) -> None:
    """Only the recommended order runs the pass that ranks by an estimate, and
    the marking must not be tied to it: the same home under "Newest" would
    have printed "Unknown" for the figure the other sort showed.
    """
    application, _, _ = board
    with TestClient(application) as client:
        cells = price_cells(client.get("/?housing=room&view=active&sort=newest").text)

    assert f"${BERNAL_RENT:,}" in cells["unpriced"] and "Estimated" in cells["unpriced"]
    assert "Last seen" in cells["quiet"]


def test_an_unpriced_home_reads_unknown_when_the_board_cannot_estimate_it(tmp_path: Path) -> None:
    """The blank is still the answer where there is no evidence for any other.

    A board this small has no median for a room, and the column must say so
    rather than fall back on some figure it can reach.
    """
    data_dir = tmp_path / "data"
    preferences_path = tmp_path / "preferences.yaml"
    preferences_path.write_text(TEST_PREFERENCES, encoding="utf-8")
    settings = Settings(
        data_dir=data_dir,
        preferences_path=preferences_path,
        database_path=data_dir / "housing.sqlite3",
        log_path=data_dir / "test.log",
    )
    repository = Repository(settings.database_path)
    repository.initialize()
    repository.upsert_listing(*room("priced", "Mission", 1800))
    repository.upsert_listing(*room("unpriced", "Mission", None))
    application = create_app(settings=settings, sources=[], enable_scheduler=False)

    with TestClient(application) as client:
        cells = price_cells(client.get("/?housing=room&view=active&sort=score").text)

    assert cells["unpriced"].strip() == "<span>Unknown</span>"


# --------------------------------------------------------------------------
# in the export
# --------------------------------------------------------------------------


def test_the_export_never_puts_an_estimate_in_the_price_column(board) -> None:
    """A sheet is sorted, filtered and totalled, and every one of those would
    treat a guess in the Price column as a rent somebody is asking."""
    application, _, _ = board
    with TestClient(application) as client:
        rows = sheet_rows(client.get("/listings.csv?housing=room&view=active&sort=score").text)

    unpriced = rows["Private room unpriced"]
    assert unpriced["Price"] == ""
    assert unpriced["Price basis"] == (
        f"Estimated ${BERNAL_RENT:,} -- no rent published, this is the local median"
    )


def test_every_figure_in_the_export_that_nobody_published_carries_the_word(board) -> None:
    """The guard rail in one assertion: an estimate is marked wherever it
    appears, or it does not appear."""
    application, _, _ = board
    with TestClient(application) as client:
        text = client.get("/listings.csv?housing=room&view=active&sort=score").text

    for row in csv.reader(io.StringIO(text)):
        for cell in row:
            if str(BERNAL_RENT) in cell.replace(",", ""):
                assert "Estimated" in cell, f"an estimate stands unmarked in {cell!r}"


def test_the_export_says_which_rents_their_sources_have_stopped_publishing(board) -> None:
    """The sheet carried the same bare number for both, and the reader took it
    away from the page that had explained the difference."""
    application, _, _ = board
    with TestClient(application) as client:
        rows = sheet_rows(client.get("/listings.csv?housing=room&view=active&sort=score").text)

    assert rows["Private room quiet"]["Price"] == "1750"
    assert rows["Private room quiet"]["Price basis"] == (
        "Last seen price -- the source has stopped listing this home"
    )
    assert rows["Private room stated"]["Price basis"] == ""


# --------------------------------------------------------------------------
# what it must not disturb
# --------------------------------------------------------------------------


def test_the_figure_shown_is_the_figure_the_ranking_placed_the_home_by(board) -> None:
    """Two ways of reaching the same median is two medians eventually.

    The column would drift from the order the rows are in -- a home shown at
    one figure and ranked at another -- and nothing on the page would say so.
    """
    application, repository, homes = board
    with TestClient(application) as client:
        page = client.get("/?housing=room&view=active&sort=score").text

    from sf_housing.app import _rent_table
    from sf_housing.rent_estimate import ranking_order

    rows = repository.query_listings(minimum_score=60, housing_kind="room")
    used: list[int] = []
    ranking_order(
        rows,
        repository.home_candidates([int(row["id"]) for row in rows if row["price"] is None]),
        _rent_table(repository),
        lambda listing: used.append(listing.price) or ScoreResult(70, [], "", {}),
    )

    assert used == [BERNAL_RENT]
    assert f"${BERNAL_RENT:,}" in price_cells(page)["unpriced"]


def test_showing_the_estimate_writes_nothing_to_the_board(board) -> None:
    """A figure the app worked out becoming a home's stored price would make
    it indistinguishable from a rent a site published, for good."""
    application, repository, _ = board

    def stored() -> list[tuple]:
        with sqlite3.connect(repository.path) as connection:
            connection.row_factory = sqlite3.Row
            return [
                (row["id"], row["price"], row["score"], row["eligibility"], row["availability_state"])
                for row in connection.execute("SELECT * FROM listings ORDER BY id")
            ]

    before = stored()
    with TestClient(application) as client:
        client.get("/?housing=room&view=active&sort=score")
        client.get("/listings.csv?housing=room&view=active&sort=score")

    assert stored() == before


def test_a_last_seen_price_is_flagged_and_not_hidden(board) -> None:
    """Saying a rent is old is the whole of the remedy.

    Absence only ever demotes: a home whose source has gone quiet stays on the
    shortlist, one click from its listing, with the flag to explain it.
    """
    application, _, _ = board
    with TestClient(application) as client:
        page = client.get("/?housing=room&view=active&sort=score").text

    assert "Private room quiet" in page
