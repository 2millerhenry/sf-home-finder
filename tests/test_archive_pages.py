"""WO-5 item 2: the Archive is read a page at a time.

The Archive tab had no LIMIT. On a year-scale board of 213,408 rows its
one-bedroom tab was one page of about 30,700 homes: 183 MB of HTML, 20.3
seconds and 1.24 GB of peak memory before anything could be clicked. On the
real board today it was 17.4 MB, 240,786 DOM nodes and 4.2 seconds to
interactive. A page is now 300 homes. The pages of a view neither repeat nor
skip a home whatever the order and however many homes tie on it, and the page,
the CSV of it and every way back to it agree on which 300 those are.
"""

from __future__ import annotations

import csv
import html
import io
import json
import random
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from itertools import product
from pathlib import Path
from urllib.parse import unquote

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sf_housing.app import create_app
from sf_housing.database import Repository
from sf_housing.models import ListingCandidate, ScoreResult
from sf_housing.settings import Settings
from tests.conftest import TEST_PREFERENCES

PAGE = 300
FOUND = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
ORDERS = ("score", "price", "newest", "available", "contact", "unopened")
# A home that states no rent but would be the best on the board at its area's
# rent: 85 against the test deal at $1,500, as scored by the real scorer.
GOOD_ROOM = (
    "Private room in a sunny Victorian house with a backyard near the park. "
    "Quiet, communal household of 4. 6-12 month lease."
)


def room(number: int, *, price: int | None = 1500, score: int = 80, platform: str = "Zillow",
         summary: str = "Private room in a shared flat.", details: dict | None = None,
         **metadata) -> tuple[ListingCandidate, ScoreResult]:
    """A room at an address of its own, so every one is its own home."""
    address = f"{number} Valencia St"
    url = {
        "Zillow": f"https://www.zillow.com/homedetails/x/{number}_zpid/",
        "Listings Project": f"https://www.listingsproject.com/real-estate/san-francisco/{number}",
    }[platform]
    listing = ListingCandidate(
        platform=platform, source_id=str(number), title=address, original_url=url, price=price,
        neighborhood="Mission", listing_type="Room", summary=summary,
        metadata={"address": address, **metadata}, housing_kind="room",
    )
    return listing, ScoreResult(score, ["Stored"], "", details or {}, eligibility="eligible")


def put(repository: Repository, pair) -> int:
    listing, result = pair
    listing_id, _ = repository.upsert_listing(listing, result)
    return listing_id


def app_over(folder: Path) -> tuple[FastAPI, Repository]:
    """The real app with the test deal (a rooms deal) over an empty board.

    Homes go in afterwards: the app scores the board when it is built, and
    these homes must keep the verdicts stored with them."""
    data_dir = folder / "data"
    preferences_path = folder / "preferences.yaml"
    preferences_path.write_text(TEST_PREFERENCES, encoding="utf-8")
    settings = Settings(data_dir=data_dir, preferences_path=preferences_path,
                        database_path=data_dir / "housing.sqlite3", log_path=data_dir / "test.log")
    repository = Repository(settings.database_path)
    repository.initialize()
    return create_app(settings=settings, sources=[], enable_scheduler=False), repository


@dataclass(frozen=True)
class Archive:
    application: FastAPI
    repository: Repository
    homes: list[int]
    # Alike in everything any order reads: only the row id tells them apart.
    alike: list[int]


@pytest.fixture(scope="module")
def archive(tmp_path_factory: pytest.TempPathFactory) -> Archive:
    """650 rooms: two full pages and fifty over.

    Every 13th home differs -- in score, rent, posting and move-in dates, a
    direct application, whether it was opened -- so each order has real work
    to do; the other 600 tie on all of it. Built once for the module because
    the upserts are the slow part, so the tests only read it; the one that
    stars a home takes the star off again.
    """
    application, repository = app_over(tmp_path_factory.mktemp("archive"))
    homes, alike, opened, found = [], [], [], {}
    for index in range(650):
        if index % 13:
            listing_id = put(repository, room(100 + index))
            alike.append(listing_id)
            found[listing_id] = FOUND
        else:
            step = index // 13
            extra = {}
            if step % 3 == 0:
                extra["listing_timestamp"] = (FOUND + timedelta(days=step % 9)).isoformat(timespec="seconds")
            if step % 5 == 0:
                extra["application_url"] = f"https://abacus.appfolio.com/listings/detail/{step}"
            listing_id = put(repository, room(
                100 + index, price=(1100, 1900, None, 1500)[step % 4], score=70 + (step * 7) % 21,
                details={"availability": {"available_on": f"2026-10-{1 + step % 28:02d}"}} if step % 2 == 0 else {},
                **extra,
            ))
            found[listing_id] = FOUND - timedelta(hours=step)
            if step % 6 == 0:
                opened.append(listing_id)
        homes.append(listing_id)
    with repository.connection() as connection:
        connection.executemany(
            "UPDATE listings SET first_found = ? WHERE id = ?",
            [(stamp.isoformat(timespec="seconds"), listing_id) for listing_id, stamp in found.items()],
        )
        connection.commit()
    for listing_id in opened:
        repository.mark_listing_opened(listing_id)
    return Archive(application, repository, homes, alike)


# --------------------------------------------------------------------------
# reading the page as a person would
# --------------------------------------------------------------------------

ROW = re.compile(r'<a class="listing-title-link" href="/listings/(\d+)\?')
COUNT = r'<span class="result-count">.*?</span>'
PAGER = r'<nav class="pager".*?</nav>'


def shown(page: str) -> list[int]:
    """The homes a page lists, top to bottom."""
    return [int(listing_id) for listing_id in ROW.findall(page)]


def said(page: str, element: str) -> str:
    """What an element says, as read on screen."""
    found = re.search(element, page, re.S)
    assert found, f"no {element} on the page"
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", found.group(0))).split())


def link(page: str, rel: str) -> str:
    found = re.search(rf'<a href="([^"]+)" rel="{rel}">', page)
    assert found, f"no {rel} link on the page"
    return html.unescape(found.group(1))


class FilterForm(HTMLParser):
    """The dashboard's filter form, as a browser submits it."""

    def __init__(self, page: str) -> None:
        super().__init__()
        self.fields: dict[str, str] = {}
        self.download: dict[str, str | None] = {}
        self._inside = False
        self._select: str | None = None
        self._options: list[tuple[str, bool]] = []
        self.feed(page)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "form":
            self._inside = "data-filter-form" in attributes
        elif not self._inside:
            return
        elif tag == "input" and attributes.get("type") == "hidden":
            self.fields[str(attributes["name"])] = attributes.get("value") or ""
        elif tag == "select":
            self._select, self._options = attributes["name"], []
        elif tag == "option" and self._select:
            self._options.append((attributes.get("value") or "", "selected" in attributes))
        elif tag == "button" and "data-download-csv" in attributes:
            self.download = attributes

    def handle_endtag(self, tag: str) -> None:
        if tag == "select" and self._select:
            chosen = [value for value, selected in self._options if selected] or [self._options[0][0]]
            self.fields[self._select] = chosen[0]
            self._select = None
        elif tag == "form":
            self._inside = False

    def csv_download(self) -> tuple[str, dict[str, str]]:
        """Where the CSV button sends the browser, and with what: the form's
        fields, and the button's own name and value."""
        button = self.download
        sent = {str(button["name"]): button.get("value") or ""} if button.get("name") else {}
        return str(button["formaction"]), {**self.fields, **sent}


# --------------------------------------------------------------------------
# the Archive, a page at a time
# --------------------------------------------------------------------------


def test_the_archive_shows_300_homes_a_page_and_says_how_many_there_are(archive: Archive) -> None:
    tab = "/?housing=room&view=all&sort=score"  # where the Archive tab links
    with TestClient(archive.application) as client:
        pages = [client.get(address) for address in (tab, f"{tab}&page=2", f"{tab}&page=3", f"{tab}&page=99")]
        assert {response.status_code for response in pages} == {200}
        first, second, third, beyond = (response.text for response in pages)

        assert [len(shown(page)) for page in (first, second, third, beyond)] == [300, 300, 50, 50]
        assert sorted(shown(first) + shown(second) + shown(third)) == sorted(archive.homes), "each home once"
        assert shown(beyond) == shown(third), "past the end is the last page, not an empty one"
        assert [said(page, COUNT) for page in (first, second, third)] == [
            "1–300 of 650 rooms", "301–600 of 650 rooms", "601–650 of 650 rooms",
        ]
        assert said(first, PAGER) == "Page 1 of 3 Next →"
        assert said(second, PAGER) == "← Previous Page 2 of 3 Next →"
        assert said(third, PAGER) == said(beyond, PAGER) == "← Previous Page 3 of 3"
        # And the links lead where they say.
        assert shown(client.get(link(first, "next")).text) == shown(second)
        assert shown(client.get(link(third, "prev")).text) == shown(second)


@pytest.mark.parametrize("sort", ORDERS)
def test_pages_neither_repeat_nor_skip_a_home_whatever_the_order(archive: Archive, sort: str) -> None:
    """600 of the homes tie on everything this order reads, so the row id every
    order ends in is all that decides where one page stops and the next begins."""
    repository = archive.repository
    whole = [item["id"] for item in repository.query_listings(view="all", housing_kind="", sort=sort)]

    pages = [
        repository.query_page(view="all", housing_kind="", sort=sort, limit=PAGE, offset=offset)
        for offset in (0, 300, 600)
    ]

    assert [len(page.rows) for page in pages] == [300, 300, 50]
    assert [page.total for page in pages] == [650, 650, 650]
    paged = [item["id"] for page in pages for item in page.rows]
    assert paged == whole
    assert sorted(paged) == sorted(archive.homes)
    # Newest row first among homes the order cannot tell apart. Asserted, not
    # left to the equal pages above: with the tiebreak taken out, SQLite still
    # sorted these ties the same way with and without a LIMIT, and only this
    # noticed.
    tied = set(archive.alike)
    assert [listing_id for listing_id in paged if listing_id in tied] == sorted(tied, reverse=True)


def test_past_the_last_page_there_are_no_homes_but_still_a_count(archive: Archive) -> None:
    """The count is what lets a link kept while the board shrank land on the
    last page rather than on an empty one."""
    beyond = archive.repository.query_page(view="all", housing_kind="", limit=PAGE, offset=900)
    assert (len(beyond.rows), beyond.total) == (0, 650)


# --------------------------------------------------------------------------
# the orders SQL now cuts pages by
# --------------------------------------------------------------------------

# The keys the page used to sort by in Python, once every row was in hand.
# SQL now puts the rows in this order so that LIMIT can cut a page; the
# Python that shows each row still reads the same JSON its own way.
def by_move_in(item: dict) -> tuple:
    return (
        not bool(item["available_on"]),
        str(item["available_on"] or "9999-12-31"),
        -int(item["score"]),
        str(item["first_found"]),
    )


def by_contact(item: dict) -> tuple:
    return (int(item["contact_rank"]), -int(item["score"]), -int(item["id"]))


MISSING = object()
APPLICATION_URLS = (
    MISSING, "https://abacus.appfolio.com/listings/detail/7", "https://abacus.appfolio.com/",
    # startswith is case-sensitive and takes no wildcards; a LIKE would do both.
    "HTTPS://ABACUS.APPFOLIO.COM/listings/detail/7", "http://abacus.appfolio.com/listings/detail/7",
    "https://www.example.com/apply", "", 1, ["https://abacus.appfolio.com/listings/detail/7"],
)
DIRECT_LISTERS = (MISSING, True, False, 0, 1, 2, 0.0, 0.5, "yes", "", [], [1], {}, {"a": 1}, None)
MOVE_INS = (
    {"availability": {"available_on": "2026-10-01"}},
    {"availability": {"available_on": "2026-09-15"}},
    {"availability": {"available_on": "soon"}},
    {"availability": {"available_on": ""}},
    {"availability": {"available_on": 0}},
    {"availability": {"available_on": None}},
    {"availability": {"available_on": ["2026-09-01"]}},
    {"availability": {"available_on": {"on": "2026-09-01"}}},
    {"availability": {}},
    {"availability": ["2026-09-01"]},
    {"availability": "2026-09-01"},
    {},
)


def a_board_of_odd_json(repository: Repository) -> None:
    """Every stored shape of a contact route, on Listings Project and on a site
    where a direct lister means nothing, each home with one of the odd shapes a
    move-in date has been stored in. Scores and first-found dates repeat, so
    most homes tie on something and the row id has to settle it."""
    chance = random.Random(2026)
    stamps = [(FOUND - timedelta(days=days)).isoformat(timespec="seconds") for days in (0, 3, 9)]
    stored = []
    combinations = product(APPLICATION_URLS, DIRECT_LISTERS, ("Listings Project", "Zillow"))
    for index, (application_url, direct_lister, platform) in enumerate(combinations):
        listing_id = put(repository, room(1000 + index, platform=platform))
        metadata = {"address": f"{1000 + index} Valencia St"}
        if application_url is not MISSING:
            metadata["application_url"] = application_url
        if direct_lister is not MISSING:
            metadata["direct_lister"] = direct_lister
        stored.append((
            json.dumps(metadata), json.dumps(MOVE_INS[index % len(MOVE_INS)]),
            chance.choice((60, 75, 90)), chance.choice(stamps), listing_id,
        ))
    # Written raw: no source stores these shapes on purpose, which is the point.
    with repository.connection() as connection:
        connection.executemany(
            "UPDATE listings SET metadata_json = ?, score_details_json = ?, score = ?, first_found = ? WHERE id = ?",
            stored,
        )
        connection.commit()


@pytest.mark.parametrize("sort, python_order", [("available", by_move_in), ("contact", by_contact)])
def test_the_orders_sql_cuts_pages_by_agree_with_the_python_that_shows_the_rows(
    repository: Repository, sort: str, python_order
) -> None:
    """Move-in and contact were sorted in Python after every row was read. SQL
    now orders by them so a page can be cut by LIMIT, which means SQL must read
    each odd JSON value exactly as ``_dashboard_row`` does -- a direct_lister of
    {} or 0.0 is no direct lister, an available_on of 0 or [] is no date -- or
    a home is on a page its own row says it should not be."""
    a_board_of_odd_json(repository)
    items = repository.query_listings(view="all", housing_kind="", sort=sort)
    assert {item["contact_rank"] for item in items} == {0, 1, 2}
    assert {bool(item["available_on"]) for item in items} == {True, False}

    first = repository.query_page(view="all", housing_kind="", sort=sort, limit=25, offset=0)
    paged = list(first.rows)
    for offset in range(25, first.total, 25):
        paged += repository.query_page(view="all", housing_kind="", sort=sort, limit=25, offset=offset).rows

    # Equals in the Python order keep the tiebreak every SQL order ends in.
    expected = sorted(items, key=lambda item: (python_order(item), -int(item["id"])))
    assert [item["id"] for item in paged] == [item["id"] for item in expected]


# --------------------------------------------------------------------------
# the CSV of a page, and the way back to it
# --------------------------------------------------------------------------


def test_the_csv_of_a_page_holds_that_page(archive: Archive) -> None:
    """The export's promise is the rows on screen. On page two the form's CSV
    button must send the page along, or the file is page one's homes."""
    with TestClient(archive.application) as client:
        first = client.get("/?view=all").text
        second = client.get("/?view=all&page=2").text
        action, sent = FilterForm(second).csv_download()
        download = client.get(action, params=sent)

    assert (action, sent.get("page")) == ("/listings.csv", "2")
    assert download.status_code == 200
    table = list(csv.DictReader(io.StringIO(download.text)))
    titles = re.findall(r'<h3 title="([^"]*)">', second)
    assert len(table) == len(titles) == 300
    assert [row["Address"] for row in table] == [html.unescape(title) for title in titles]
    assert not {row["Address"] for row in table} & set(re.findall(r'<h3 title="([^"]*)">', first))


def test_a_home_starred_on_page_three_returns_to_page_three(archive: Archive) -> None:
    here = "/?sort=score&view=all&page=3"
    with TestClient(archive.application) as client:
        page = client.get("/?view=all&page=3").text
        home = shown(page)[0]
        returns = {html.unescape(value) for value in re.findall(r'name="return_to" value="([^"]*)"', page)}
        opens = {unquote(value) for value in re.findall(r'\?from=([^"]+)"', page)}
        assert returns == opens == {here}

        try:
            starred = client.post(f"/listings/{home}/status", data={"status": "saved", "return_to": here},
                                  follow_redirects=False)
            assert (starred.status_code, starred.headers["location"]) == (303, here)
            back = client.get(starred.headers["location"]).text
        finally:
            client.post(f"/listings/{home}/status", data={"status": "active", "return_to": here})
        assert home in shown(back) and said(back, PAGER) == "← Previous Page 3 of 3"
        assert "★ Starred" in back, "back on page three, with the star showing"
        # The home's own page leads back to page three as well.
        opened = re.search(rf'href="(/listings/{home}\?from=[^"]+)"', page)
        details = client.get(html.unescape(opened.group(1))).text
        assert 'href="/?sort=score&amp;view=all&amp;page=3">← Back to the archive' in details

    # A new order, another tab or new filters start again at page one.
    sorting = re.findall(r'<th scope="col" class="cell-[a-z-]+"[^>]*><a href="([^"]+)"', page)
    tabs = re.findall(r'<nav class="(?:view|housing)-tabs".*?</nav>', page, re.S)
    assert len(sorting) == 4 and not any("page=" in href for href in sorting)
    assert tabs and not any("page=" in nav for nav in tabs)
    assert "page" not in FilterForm(page).fields
    assert FilterForm(page).csv_download()[1]["page"] == "3"


def test_the_first_page_takes_you_back_to_the_plain_archive(archive: Archive) -> None:
    """Page one is the Archive itself, so a star there returns to the address
    the Archive tab links to rather than to a page-numbered copy of it."""
    with TestClient(archive.application) as client:
        page = client.get("/?view=all").text
    returns = {html.unescape(value) for value in re.findall(r'name="return_to" value="([^"]*)"', page)}
    assert returns == {"/?sort=score&view=all"}


# --------------------------------------------------------------------------
# the shortlist is put in order whole, then cut
# --------------------------------------------------------------------------


def test_the_shortlist_is_ordered_whole_before_it_is_cut(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A home that states no rent is ranked at what homes like it rent for (D3),
    which can carry it across a page boundary. Its stored score of 60 puts it
    last, on page two; at the Mission's $1,500 it is the best room here. Cut
    first and ordered after, it could never reach page one."""
    application, repository = app_over(tmp_path)
    priced = [put(repository, room(100 + index, score=61 + index % 10)) for index in range(349)]
    lifted = put(repository, room(99, price=None, score=60, summary=GOOD_ROOM))
    by_stored_score = repository.query_listings(minimum_score=60, housing_kind="room", view="active")
    assert by_stored_score[-1]["id"] == lifted, "the premise: by its own score it is the last home"

    with TestClient(application) as client:
        first = client.get("/?housing=room&view=active&sort=score").text
        second = client.get("/?housing=room&view=active&sort=score&page=2").text
        beyond = client.get("/?housing=room&view=active&sort=score&page=99").text
        assert [len(shown(first)), len(shown(second))] == [300, 50]
        # The oracle: the same shortlist with a page big enough for all of it.
        monkeypatch.setattr("sf_housing.app.PAGE_SIZE", 10_000)
        whole = shown(client.get("/?housing=room&view=active&sort=score").text)

    assert "placed by what similar homes nearby rent for" in first
    assert lifted in shown(first) and lifted not in shown(second)
    assert shown(first) + shown(second) == whole
    assert sorted(whole) == sorted([*priced, lifted])
    assert shown(beyond) == shown(second), "past the end is the last page here too"


# --------------------------------------------------------------------------
# page numbers nobody should have typed
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "asked", ["abc", "-3", "1.5", "", "0"], ids=["letters", "negative", "fraction", "empty", "zero"]
)
def test_an_unreadable_page_number_is_the_first_page(archive: Archive, asked: str) -> None:
    with TestClient(archive.application) as client:
        response = client.get("/", params={"view": "all", "page": asked})
        first = client.get("/?view=all").text
    assert response.status_code == 200
    assert len(shown(first)) == 300
    assert shown(response.text) == shown(first)
    assert said(response.text, PAGER) == "Page 1 of 3 Next →"


def test_a_page_number_past_any_board_is_the_last_page_not_a_server_error(archive: Archive) -> None:
    """Uncapped, page 10**30 asks SQLite to skip more rows than an integer holds."""
    beyond = str(10**30)
    with TestClient(archive.application) as client:
        page = client.get("/", params={"view": "all", "page": beyond})
        download = client.get("/listings.csv", params={"view": "all", "page": beyond})
        last = client.get("/?view=all&page=3").text
    assert (page.status_code, download.status_code) == (200, 200)
    assert shown(page.text) == shown(last)
    assert len(shown(last)) == 50
    assert said(page.text, PAGER) == "← Previous Page 3 of 3"
    assert len(list(csv.DictReader(io.StringIO(download.text)))) == 50


def test_the_homes_you_passed_come_a_page_at_a_time_too(tmp_path: Path) -> None:
    """Passed is the user's own list, but a year of passes is still a year:
    it is read a page at a time in SQL like the Archive, and says how many."""
    application, repository = app_over(tmp_path)
    passed = [put(repository, room(1000 + number)) for number in range(PAGE + 1)]
    for listing_id in passed:
        repository.set_listing_status(listing_id, "dismissed")

    with TestClient(application) as client:
        first = client.get("/?view=dismissed&sort=newest").text
        second = client.get(link(first, "next")).text

    assert len(shown(first)) == PAGE and len(shown(second)) == 1
    assert set(shown(first)) | set(shown(second)) == set(passed)
    assert said(first, COUNT) == f"1–{PAGE} of {PAGE + 1} homes"
