"""Absence only counts against a home when something actually looked for it.

The rule about homes a source has stopped returning read every finished
search as a search of that source's inventory. Most of them are nothing of
the kind. Craigslist is six price-and-bedroom-filtered searches of one
server-capped page each; Zillow serves about a thousand of the 2,568 homes it
says it has and refuses page 25; every paginated source stops at a ceiling;
and a read the check's own time cut short was recorded as a completed one.

A home none of those searches could ever have reached is absent from their
results for a reason that has nothing to do with the home. Measured on a real
board of 9,615 homes, 3,232 were past the three-day line; with the gate in
place 528 are, and the 2,704 it spares are homes no search had ever reached.

So a run now records whether it covered its source, and only a run that did
is allowed to start the clock. Where coverage cannot be established the home
keeps its place: absence has to be evidenced before it can cost anything.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import httpx
import pytest

from sf_housing import sources as sources_module
from sf_housing.database import Repository
from sf_housing.models import ListingCandidate
from sf_housing.preferences import Preferences, parse_preferences
from sf_housing.scanner import Scanner
from sf_housing.sources import (
    CraigslistSource,
    MovotoSource,
    UloopSource,
    ZillowSource,
    covers_inventory,
)
from tests.conftest import TEST_PREFERENCES
from tests.test_unseen_homes import (
    DEMOTE_HOURS,
    SHORTLIST_HOURS,
    home,
    near_matches,
    searched,
    shortlist,
)


def deal() -> Preferences:
    """The deal the sources here are read against."""
    return parse_preferences(TEST_PREFERENCES)


def room(platform: str, source_id: str) -> ListingCandidate:
    return ListingCandidate(
        platform=platform,
        source_id=source_id,
        title="Sunny private room in a house near Golden Gate Park",
        original_url=f"https://example.test/{platform}/{source_id}",
        price=1500,
        neighborhood="NOPA",
        summary="Flexible lease, communal garden, 4 roommates.",
        metadata={"property_type": "house", "rooms_in_property": "4"},
    )


# --------------------------------------------------------------------------
# what the board does with a search that covered nothing
# --------------------------------------------------------------------------


def test_a_home_keeps_its_place_when_no_search_that_covered_it_has_run(
    repository: Repository,
) -> None:
    """A capped search returning without a home is not evidence the home is gone.

    Every Craigslist and Zillow search on the author's board finished, said
    success, and read a fraction of what the board holds for those sites.
    Read as complete coverage, they put 2,506 homes past the three-day line
    between them -- homes nobody had looked for.
    """
    home(repository, "never_looked_for", seen=SHORTLIST_HOURS + 48, score=92)
    searched(repository, covered=False)

    assert shortlist(repository) == ["never_looked_for"]
    assert near_matches(repository) == []


def test_a_home_a_covering_search_stopped_returning_leaves_the_shortlist(
    repository: Repository,
) -> None:
    """The rule still works where the evidence is real.

    The gate is about which searches may be quoted, not about switching the
    rule off: a source that reads its whole inventory and comes back without
    a home has said something, and the home moves to Near matches as before.
    """
    home(repository, "really_gone_quiet", seen=SHORTLIST_HOURS + 48, score=92)
    searched(repository, covered=True)

    assert shortlist(repository) == []
    assert near_matches(repository) == ["really_gone_quiet"]


def test_a_search_that_covered_nothing_does_not_even_push_a_home_down_the_page(
    repository: Repository,
) -> None:
    """The thirty-six hour demotion rests on the same evidence as the removal.

    Sinking a home below every home still showing up is a smaller cost than
    moving it to Near matches, but it is the same claim -- that this source
    has stopped returning it -- and an uncovered search cannot make it.
    """
    home(repository, "never_looked_for", seen=DEMOTE_HOURS + 12, score=95)
    home(repository, "showing", seen=1, score=70)
    searched(repository, covered=False)

    assert shortlist(repository) == ["never_looked_for", "showing"]


def test_the_clock_is_the_last_search_that_covered_the_source_not_the_last_one(
    repository: Repository,
) -> None:
    """A later capped search must not age homes the covering search returned.

    Sources are read many times a day and only some of those reads cover
    anything -- Redfin and Trulia are read shallowly whenever somebody is
    waiting and to the bottom once a night. Taking the newest finished run as
    the clock would let this morning's shallow read age every home the
    overnight sweep had just confirmed.
    """
    home(repository, "seen_by_the_deep_read", seen=SHORTLIST_HOURS - 1, score=92)
    searched(repository, hours_ago=SHORTLIST_HOURS - 1, covered=True)
    searched(repository, hours_ago=0, covered=False)

    assert shortlist(repository) == ["seen_by_the_deep_read"]


def test_a_board_upgraded_today_treats_no_home_as_unseen(repository: Repository) -> None:
    """The migration's default has to be the answer that costs nothing.

    Every run already on an installed board was recorded before anything
    asked about coverage. Defaulting those to "covered" would mean an upgrade
    silently re-deciding thousands of homes on evidence nobody ever gathered;
    defaulting them to "not covered" costs one scan's delay and nothing else.
    """
    home(repository, "on_the_board_already", seen=SHORTLIST_HOURS + 48, score=92)
    moment = "2026-09-19T12:00:00+00:00"
    with repository.connection() as connection:
        scan = connection.execute(
            "INSERT INTO scan_runs (trigger, status, started_at) VALUES ('test', 'completed', ?)",
            (moment,),
        ).lastrowid
        # Exactly the columns a board written before this shipped has.
        connection.execute(
            """INSERT INTO source_runs (scan_run_id, platform, status, started_at, listings_seen)
               VALUES (?, 'Zillow', 'success', ?, 40)""",
            (scan, moment),
        )
        connection.commit()

    assert shortlist(repository) == ["on_the_board_already"]


# --------------------------------------------------------------------------
# which sources are allowed to claim they covered anything
# --------------------------------------------------------------------------


def test_only_the_sources_that_read_a_whole_inventory_claim_to_cover_one() -> None:
    """Claiming coverage is a decision, and it has to be visible in the diff.

    The default is no claim, so a source added tomorrow says nothing and is
    believed about nothing. This is the list of sources that have said
    otherwise, written down so that adding one is a change somebody reviews
    rather than an attribute that slipped in beside a parser fix.
    """
    claimed = {
        cls.platform
        for cls in vars(sources_module).values()
        if inspect.isclass(cls) and getattr(cls, "search_covers_inventory", False)
    }

    assert claimed == {
        "Abacus (small buildings)",
        "ApartmentGuide",
        "AppFolio",
        "AvalonBay",
        "Listings Project",
        "Movoto",
        "SF Housing Portal",
        "UDR",
        "Uloop",
    }


# A source whose search reads one page returns the same number of homes every
# run, because that number is the page. These three did, on the owner's own
# board, while every source that really walks its inventory moved with the
# city: Craigslist between 143 and 822, Movoto between 250 and 1,971.
READS_ONE_PAGE = [
    ("Zumper", "ZumperSource", "49 or 50 homes on each of 71 runs"),
    ("Apartment List", "ApartmentListSource", "19 or 20 buildings on each of 66 runs"),
    ("SpareRoom", "SpareRoomSource", "11 rooms on each of 12 runs"),
]


@pytest.mark.parametrize("platform,class_name,measured", READS_ONE_PAGE)
def test_a_source_that_reads_one_page_does_not_claim_the_inventory(
    platform: str, class_name: str, measured: str
) -> None:
    """A constant count is a page size, and absence behind it means nothing.

    Each of these claimed to read its whole San Francisco inventory, so a home
    it stopped returning was treated as a home taken down: ranked below after
    36 hours, moved to near matches after three days. It was really a home that
    had been pushed onto page two.
    """
    source = getattr(sources_module, class_name)
    assert source.platform == platform
    assert covers_inventory(source) is False, measured


@pytest.mark.parametrize(
    "source", [ZillowSource(), CraigslistSource()], ids=["zillow", "craigslist"]
)
def test_the_sources_that_cannot_reach_their_own_inventory_never_claim_to(
    source: object,
) -> None:
    """Zillow and Craigslist are the two that matter, and neither can cover.

    Zillow refuses page 25 however patiently it is asked -- about a thousand
    homes of the 2,568 its own page claims -- so most of its inventory is not
    reachable by paging at all. Craigslist is six searches, each of a single
    static page the site caps for itself, and each narrowed by the deal's
    rents and bedroom counts before it is sent. Absence from either is the
    only evidence the app will ever have about those homes, and it is not
    enough, so their homes are left alone.
    """
    assert covers_inventory(source) is False


def uloop_board() -> str:
    """The real board capture the Uloop tests are built on."""
    return (Path(__file__).parent / "fixtures" / "uloop_board.html").read_text(encoding="utf-8")


class Pages:
    """Answers each request with the next page in turn, repeating the last."""

    def __init__(self, *pages: str) -> None:
        self.pages = list(pages)
        self.requested: list[str] = []

    def get(self, url, **kwargs):
        self.requested.append(url)
        return httpx.Response(
            200,
            text=self.pages[min(len(self.requested) - 1, len(self.pages) - 1)],
            request=httpx.Request("GET", url),
        )


def test_a_walk_that_ends_at_its_page_ceiling_does_not_claim_to_have_covered_the_board() -> None:
    """Twelve pages that were all still new is a truncated read, not a board.

    Uloop's walk already logged this case -- "a truncated result that says
    nothing reads exactly like complete coverage" -- and then handed back a
    list that looked exactly like a complete one. Distinct slugs per page,
    because the slug is the identity and a renumbered id would make every
    page the same page.
    """
    source = UloopSource()
    board = Pages(
        *(
            re.sub(r"(/housing/view\.php/\d+/)", rf"\g<1>p{page}-", uloop_board())
            for page in range(source.max_pages + 3)
        )
    )

    source.search(board, deal())

    assert len(board.requested) == source.max_pages
    assert covers_inventory(source) is False


def test_a_walk_that_reaches_the_end_of_the_board_does_claim_to_have_covered_it() -> None:
    """The same walk, stopped by the board running out, is the one that counts.

    Without this the test above would pass just as well on a source that
    never claims coverage at all, which would make the gate a way of
    switching the rule off rather than a way of grounding it.
    """
    source = UloopSource()
    board = Pages(uloop_board(), "<html></html>")

    source.search(board, deal())

    assert len(board.requested) < source.max_pages
    assert covers_inventory(source) is True


def test_a_walk_a_refusal_stopped_part_way_does_not_claim_to_have_covered_anything() -> None:
    """The pages after a 403 hold homes nobody looked at.

    A paginated source keeps what it read when a page is refused, which is
    right, and that partial list then read as the whole of Movoto.
    """
    source = MovotoSource()
    page = (
        '<script id="__INITIAL_STATE__">'
        '{"pageData": {"listings": [{"mlsNumber": "a1", "houseRealStatus": "FOR_RENT",'
        ' "geo": {"city": "San Francisco"}, "path": "/rentals/a1/", "price": 2400}]}}'
        "</script>"
    )

    class RefusesThePageAfterTheFirst:
        def __init__(self) -> None:
            self.pages = 0

        def get(self, url, **kwargs):
            self.pages += 1
            return httpx.Response(
                200 if self.pages == 1 else 403,
                text=page if self.pages == 1 else "",
                request=httpx.Request("GET", url),
            )

    source.search(RefusesThePageAfterTheFirst(), deal())

    assert covers_inventory(source) is False


# --------------------------------------------------------------------------
# what the scanner writes down
# --------------------------------------------------------------------------


class Stub:
    """A source that hands back the homes it was given, and says whether it covered."""

    mode = "automatic"
    manual_reason = None
    detail_budget = 0

    def __init__(self, platform: str, *, covers: bool, listings=(), search=None) -> None:
        self.platform = platform
        self.search_url = f"https://example.test/{platform}"
        self.search_covers_inventory = covers
        self._listings = list(listings)
        self._search = search

    def search(self, client, preferences):
        if self._search is not None:
            self._search()
        return list(self._listings)


def covered_runs(repository: Repository) -> dict[str, int]:
    with repository.connection() as connection:
        return {
            str(row["platform"]): int(row["covered"])
            for row in connection.execute(
                "SELECT platform, covered FROM source_runs WHERE status = 'success'"
            )
        }


@pytest.fixture
def board(tmp_path: Path) -> Repository:
    """A board of its own, because a scan writes to more of one than a query reads."""
    instance = Repository(tmp_path / "scanned.sqlite3")
    instance.initialize()
    return instance


def scan(board: Repository, sources, preferences: Preferences) -> None:
    Scanner(board, lambda: preferences, list(sources), detail_delay_seconds=0).run_scan("scheduled")


def test_the_scanner_records_whether_each_search_covered_its_source(
    board: Repository, preferences: Preferences
) -> None:
    """Coverage has to reach the board, or the rule has nothing to read.

    Both halves in one scan, because recording it for nobody and recording it
    for everybody are the two ways this fails and they look identical from a
    single source.
    """
    scan(
        board,
        [
            Stub("Covers", covers=True, listings=[room("Covers", "1")]),
            Stub("Slice", covers=False, listings=[room("Slice", "1")]),
        ],
        preferences,
    )

    assert covered_runs(board) == {"Covers": 1, "Slice": 0}


def test_a_search_that_filled_its_result_ceiling_did_not_cover_its_source(
    board: Repository, preferences: Preferences
) -> None:
    """At the ceiling the search stopped counting rather than ran out.

    Every source stops at ``max_results_per_source``. A source that hands
    back exactly as many homes as it is allowed to has more behind it, and
    the homes behind it were never looked at.
    """
    ceiling = int(preferences.section("sources").get("max_results_per_source") or 250)

    scan(
        board,
        [Stub("Covers", covers=True, listings=[room("Covers", str(n)) for n in range(ceiling)])],
        preferences,
    )

    assert covered_runs(board) == {"Covers": 0}
