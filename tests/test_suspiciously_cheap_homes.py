"""A rent too good to be true must earn its place before it is recommended.

The plausibility check has been in the scorer from the beginning: a $1,125
three-bedroom is marked "unusually low for an entire SF 3 bedrooms" and then
left exactly where it was, which on the owner's real board meant second on the
shortlist at 86. A suspicion nobody acts on is no suspicion.

Acting on it can only ever mean demoting. Cheap is the thing this app is
looking for and some of these are real, so a rent below anything a home that
size lets for sinks below every home that raises no question, says why, and
stays on the shortlist where somebody can open it and decide in ten seconds.

What puts it back is a page: the scanner opens the listing's own page, and a
page that still shows the home returns it to the place its score gives it. A
fetch that fails, times out or is blocked leaves the home exactly as it was --
unknown is not gone -- and only the page saying the post is removed takes the
home off the shortlist, through the ``verified_inactive`` proof that has always
meant that and nothing else.
"""

from __future__ import annotations

import copy
import json
import pathlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
import yaml

from sf_housing.classification import classify_listing
from sf_housing.database import CHEAP_RENT_CONFIRMED_FOR, Repository
from sf_housing.models import ListingCandidate
from sf_housing.preferences import parse_preferences
from sf_housing.rent_estimate import FAR_BELOW_MARKET_SHARE, RentTable, ranking_order
from sf_housing.scanner import CHEAP_RENT_PAGES_PER_SCAN, Scanner
from sf_housing.scoring import IMPLAUSIBLE_RENT_FLOOR, score_listing


CUT_OFF = 60

# The owner's own deal, reduced to the one path the thirteen homes on their
# board are in: entire three-bedroom homes, whose plausibility floor is $1,700.
THREE_BEDROOM_DEAL = {
    "state": "active",
    "enabled_paths": ["three_bedroom"],
    "budgets": {"three_bedroom": {"maximum_monthly": 7500, "ideal_monthly": 5000}},
    "geography": {
        "anywhere_in_sf": False,
        "dream": ["Mission District"],
        "strong": [],
        "okay": [],
        "avoid": [],
    },
    "move_in": {"flexible": True},
    "lease": {"minimum_months": None, "maximum_months": None},
    "room_household": {"private_room_required": False, "maximum_people": None},
    "preferences": {},
}


def deal(**changes):
    profile = copy.deepcopy(THREE_BEDROOM_DEAL)
    profile.update(changes)
    return parse_preferences(yaml.safe_dump({"profile_version": 1, "profile": profile}))


def now_minus(hours: float) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours)).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# what the scorer writes down
# --------------------------------------------------------------------------


def whole_home(price: int, **changes) -> ListingCandidate:
    fields = {
        "platform": "Craigslist",
        "source_id": "cheap",
        "title": "Light and bright 3 bed, 2 bath flat",
        "original_url": "https://sfbay.craigslist.org/apa/d/x/cheap.html",
        "price": price,
        "neighborhood": "Mission District",
        "summary": "A three bedroom flat with a garden, laundry and parking.",
    }
    fields.update(changes)
    return classify_listing(ListingCandidate(**fields))


def test_the_scorer_writes_down_that_a_rent_is_below_the_floor_for_its_size() -> None:
    """The suspicion existed only as a sentence, so nothing could rank by it.

    Everything that acts on an implausible rent -- the order of the shortlist,
    the question on the card, the page the scanner goes and opens -- reads this
    one recorded fact, so the floor is applied in exactly one place.
    """
    result = score_listing(whole_home(1125), deal())

    assert result.details["price"]["implausibly_low"] is True


def test_a_rent_above_the_floor_is_not_written_down_as_suspicious() -> None:
    """Cheap is what this app is hunting for; only implausible is a question."""
    result = score_listing(whole_home(IMPLAUSIBLE_RENT_FLOOR[3]), deal())

    assert result.details["price"].get("implausibly_low") is not True


def test_a_home_let_below_market_on_purpose_is_not_suspicious() -> None:
    """The city's portal publishes rents a third of market. Those are the finds.

    The exemption already lived in the plausibility check; recording the check
    as a fact must not lose it, or every below-market-rate home would be
    demoted for being exactly what the owner is looking for.
    """
    result = score_listing(whole_home(1125, metadata={"below_market_rate": True}), deal())

    assert result.details["price"].get("implausibly_low") is not True


def test_a_suspiciously_cheap_home_is_still_scored_on_its_merits() -> None:
    """Capping the score is what this must not do: it would hide the home.

    Two earlier versions of the app capped a cheap home, at 49 and at 79, and
    against a cut-off of 80 that took a whole category off the shortlist by a
    single point. The demotion is an ordering, never a score.
    """
    cheap = score_listing(whole_home(1125), deal())
    plausible = score_listing(whole_home(5000), deal())

    assert cheap.score == plausible.score
    assert cheap.eligibility != "ineligible"


# --------------------------------------------------------------------------
# where a board puts one
# --------------------------------------------------------------------------


@pytest.fixture
def repository(tmp_path: pathlib.Path) -> Repository:
    instance = Repository(tmp_path / "housing.sqlite3")
    instance.initialize()
    return instance


def stored(
    repository: Repository,
    source_id: str,
    *,
    score: int = 90,
    price: int = 1400,
    implausibly_low: bool = True,
    page_verified_hours_ago: float | None = None,
    searched_hours_ago: float | None = None,
    home_key: str = "",
    platform: str = "Craigslist",
) -> int:
    """One stored home, as the scorer and the scanner would have left it."""
    metadata: dict[str, object] = {}
    if page_verified_hours_ago is not None:
        metadata["page_verified_at"] = now_minus(page_verified_hours_ago)
    if searched_hours_ago is not None:
        metadata["last_verified_at"] = now_minus(searched_hours_ago)
    details: dict[str, object] = {"price": {"value": 1.0, "weight": 25.0, "known": True}}
    if implausibly_low:
        details["price"]["implausibly_low"] = True
    with repository.connection() as connection:
        cursor = connection.execute(
            """INSERT INTO listings (platform, source_id, title, original_url, canonical_url,
                   price, housing_kind, unit_type, concern, score, eligibility, status,
                   metadata_json, score_details_json, home_key, first_found, last_seen)
               VALUES (?, ?, ?, ?, ?, ?, 'whole_unit', 'three_bedroom', '', ?, 'eligible',
                       'active', ?, ?, ?, ?, ?)""",
            (
                platform,
                source_id,
                f"3 bed flat {source_id}",
                f"https://example.test/{source_id}",
                f"https://example.test/{source_id}",
                price,
                score,
                json.dumps(metadata),
                json.dumps(details),
                home_key,
                now_minus(240),
                now_minus(1),
            ),
        )
        connection.commit()
        return int(cursor.lastrowid)


def shortlist(repository: Repository) -> list[str]:
    return [row["source_id"] for row in rows_of(repository, "active")]


def rows_of(repository: Repository, view: str) -> list[dict[str, object]]:
    return repository.query_listings(
        minimum_score=CUT_OFF,
        sort="score",
        housing_kind="whole_unit",
        unit_types=("three_bedroom",),
        view=view,
    )


def test_a_rent_nobody_has_confirmed_ranks_below_every_home_not_in_question(
    repository: Repository,
) -> None:
    """A $1,125 three-bedroom sat second on the owner's real board at 86.

    Nothing in the app had ever acted on the suspicion it had already written
    down, so the least believable rent on the board was also one of the first
    things the reader saw.
    """
    stored(repository, "too-good", score=92)
    stored(repository, "believable", score=70, price=5200, implausibly_low=False)

    assert shortlist(repository) == ["believable", "too-good"]


def test_a_suspiciously_cheap_home_is_demoted_and_never_hidden(
    repository: Repository,
) -> None:
    """Some of these are real, and cheap is the thing being searched for.

    Demote and label: the home stays on the shortlist, one click from its own
    listing, where somebody can open it and decide for themselves.
    """
    stored(repository, "too-good", score=92)

    assert shortlist(repository) == ["too-good"]
    assert [row["source_id"] for row in rows_of(repository, "near_matches")] == []


def test_a_cheap_home_whose_page_was_opened_keeps_the_place_its_score_earns(
    repository: Repository,
) -> None:
    """Confirmation is what lifts the demotion, and the only thing that does.

    Without this the rule would be a permanent penalty for being cheap rather
    than a question the app can answer by going and looking.
    """
    stored(repository, "too-good", score=92, page_verified_hours_ago=1)
    stored(repository, "believable", score=70, price=5200, implausibly_low=False)

    assert shortlist(repository) == ["too-good", "believable"]


def test_a_page_read_stops_vouching_for_a_cheap_home_once_it_is_old(
    repository: Repository,
) -> None:
    """A confirmation from last week says nothing about this morning.

    Scans run twice a day, so a page read that is older than three missed
    chances means the scanner has stopped answering for this home, and an
    unanswered question is not an answer.
    """
    hours = CHEAP_RENT_CONFIRMED_FOR.total_seconds() / 3600
    stored(repository, "too-good", score=92, page_verified_hours_ago=hours + 1)
    stored(repository, "believable", score=70, price=5200, implausibly_low=False)

    assert shortlist(repository) == ["believable", "too-good"]


def test_a_search_that_still_returns_the_home_does_not_answer_for_its_rent(
    repository: Repository,
) -> None:
    """Every search that returns a home stamps a confirmation on it.

    That one is about whether the source still lists the home, and the source
    listed a $1,125 three-bedroom yesterday too. Only a read of the listing's
    own page says anything about whether the rent belongs to something real,
    so only that lifts the demotion.
    """
    stored(repository, "too-good", score=92, searched_hours_ago=0.1)
    stored(repository, "believable", score=70, price=5200, implausibly_low=False)

    assert shortlist(repository) == ["believable", "too-good"]


def test_the_card_says_the_rent_is_below_anything_that_size_lets_for(
    repository: Repository,
) -> None:
    """Demote *and label*. Ten of the owner's thirteen said nothing at all.

    The single ``concern`` sentence is decided by a chain of elif, and an
    unstated neighbourhood wins it, so on ten of the thirteen the rent the app
    did not believe was the one thing the card never mentioned.
    """
    listing_id = stored(repository, "too-good", price=1125)

    row = repository.listing(listing_id)

    assert row is not None
    reasons = [entry["reason"] for entry in row["checks"]]
    assert any("$1,125" in reason for reason in reasons), reasons
    assert any("nobody has opened the listing" in reason for reason in reasons), reasons


def test_the_card_goes_on_asking_about_the_rent_after_the_page_is_read(
    repository: Repository,
) -> None:
    """Reading the page proves a listing is there, not that the rent is a month's.

    Only a person can settle whether $1,125 is the whole home or one room's
    share, so confirming the listing must not quietly retire the question --
    it retires exactly the half of it the app was able to answer.
    """
    listing_id = stored(repository, "too-good", price=1125, page_verified_hours_ago=1)

    row = repository.listing(listing_id)

    assert row is not None
    rent = [entry for entry in row["checks"] if entry["check"] == "rent"]
    assert rent, row["checks"]
    assert "nobody has opened the listing" not in rent[0]["reason"]
    assert "not a room price" in rent[0]["reason"]


def test_a_confirmation_date_nothing_can_read_is_no_confirmation(
    repository: Repository,
) -> None:
    """The card and the ranking must not disagree about a broken stamp.

    One is worked out in SQL over nine thousand rows and the other in Python
    on the way to the screen, and a date neither can parse -- a stamp written
    by an older build, a row restored from a backup -- has to read the same
    way to both, or a home sinks to the bottom of the shortlist with nothing
    on it saying why.
    """
    listing_id = stored(repository, "too-good", score=92)
    with repository.connection() as connection:
        connection.execute(
            "UPDATE listings SET metadata_json = json_set(metadata_json, "
            "'$.page_verified_at', 'whenever') WHERE id = ?",
            (listing_id,),
        )
        connection.commit()
    stored(repository, "believable", score=70, price=5200, implausibly_low=False)

    row = repository.listing(listing_id)

    assert shortlist(repository) == ["believable", "too-good"]
    assert row is not None
    rent = [entry for entry in row["checks"] if entry["check"] == "rent"]
    assert rent and "nobody has opened the listing" in rent[0]["reason"], row["checks"]


def test_a_home_another_site_prices_normally_is_not_held_down_by_the_cheap_copy(
    repository: Repository,
) -> None:
    """One flat, two listings: the dearer live quote already ranks the home.

    The shortlist ranks a home by its least favourable live copy, so a home
    another site prices at $5,200 is not being promoted on the strength of the
    $1,400 one, and demoting it would punish the home for the copy the ranking
    already ignores.
    """
    stored(repository, "cheap-copy", score=92, home_key="one-flat")
    stored(
        repository,
        "normal-copy",
        score=92,
        price=5200,
        implausibly_low=False,
        home_key="one-flat",
    )
    stored(repository, "believable", score=70, price=5200, implausibly_low=False)

    assert shortlist(repository)[0] != "believable"


def test_the_shortlist_keeps_its_order_when_an_unpriced_home_reranks_it(
    repository: Repository,
) -> None:
    """The one pass that reorders the shortlist after the query used to undo it.

    ``ranking_order`` gives an unpriced home a place by an estimated rent, and
    it did so by re-sorting every row on score alone -- which threw away the
    demotions the query had just applied. Seven homes on the owner's board
    state no rent, so on that board this pass ran on every page load.
    """
    listings = [
        {"id": 1, "score": 70, "home_score": 70, "price": 5200, "rank_group": (0, 0)},
        {"id": 2, "score": 92, "home_score": 92, "price": 1400, "rank_group": (0, 1)},
    ]

    ordered, _ = ranking_order(listings, {}, RentTable({}, {}), lambda listing: None)

    assert [item["id"] for item in ordered] == [1, 2]


# --------------------------------------------------------------------------
# going and looking
# --------------------------------------------------------------------------


def cheap_listing(source_id: str, price: int = 1400, neighborhood: str = "Mission District"):
    # Not Craigslist: a Craigslist whole unit is capped at 59 until its detail
    # page has been read, which would keep every home in these tests off the
    # shortlist for a reason that has nothing to do with its rent.
    return classify_listing(
        ListingCandidate(
            platform="Zillow",
            source_id=source_id,
            title=f"3 bed 2 bath flat {source_id}",
            original_url=f"https://example.test/homes/{source_id}",
            price=price,
            neighborhood=neighborhood,
            summary="A three bedroom flat with a garden, laundry and parking.",
        )
    )


class Source:
    """A source whose search returns cards and whose pages can be read."""

    platform = "Zillow"
    mode = "automatic"
    search_url = "https://example.test/search"
    manual_reason = None
    detail_budget = 0

    def __init__(self, returns, *, removed=(), unreachable=()):
        self._returns = list(returns)
        self.removed = set(removed)
        self.unreachable = set(unreachable)
        self.enriched: list[str] = []

    def search(self, client, preferences):
        return list(self._returns)

    def enrich(self, client, listing):
        self.enriched.append(listing.source_id)
        if listing.source_id in self.unreachable:
            raise RuntimeError("connection reset")
        if listing.source_id in self.removed:
            return replace(
                listing,
                metadata={
                    **listing.metadata,
                    "verified_inactive": True,
                    "verification_concern": "Verified inactive: the source has removed this post.",
                },
            )
        return replace(listing, summary="Still up, with a full description.")


class Blind:
    """A source with no way to read a listing's own page. Ten of the app's have none."""

    platform = "Zillow"
    mode = "automatic"
    search_url = "https://example.test/search"
    manual_reason = None
    detail_budget = 0

    def __init__(self, returns):
        self._returns = list(returns)

    def search(self, client, preferences):
        return list(self._returns)


def board(tmp_path: pathlib.Path):
    repository = Repository(tmp_path / "housing.sqlite3")
    repository.initialize()
    return repository, deal()


def scan(repository, preferences, source, trigger="scheduled"):
    Scanner(repository, lambda: preferences, [source]).run_scan(trigger)


def forget_the_page_reads(repository: Repository) -> None:
    """Drop every page confirmation, as the hours between scans effectively do."""
    with repository.connection() as connection:
        connection.execute(
            "UPDATE listings SET metadata_json = json_remove(metadata_json, '$.page_verified_at')"
        )
        connection.commit()


def test_a_shortlisted_home_far_below_what_its_area_costs_has_its_page_opened(
    tmp_path: pathlib.Path,
) -> None:
    """The half of the rule that lets a real bargain back to the top.

    Nothing in the app ever re-opened the page of a home its search was still
    returning, so a cheap home the app did not believe could never stop being
    disbelieved.
    """
    repository, preferences = board(tmp_path)
    scan(repository, preferences, Source([cheap_listing("too-good")]), "manual")
    forget_the_page_reads(repository)

    second = Source([cheap_listing("too-good")])
    scan(repository, preferences, second)

    assert second.enriched == ["too-good"]
    row = repository.listing(
        repository.query_listings(0, view="all", housing_kind="whole_unit")[0]["id"]
    )
    assert row is not None and row["metadata"].get("page_verified_at")


def test_a_rent_above_the_floor_for_its_size_never_costs_a_page_read(
    tmp_path: pathlib.Path,
) -> None:
    """Scope it tightly: thirteen homes on the owner's board, not a crawl."""
    repository, preferences = board(tmp_path)
    scan(repository, preferences, Source([cheap_listing("ordinary", price=5200)]), "manual")
    forget_the_page_reads(repository)

    second = Source([cheap_listing("ordinary", price=5200)])
    scan(repository, preferences, second)

    assert second.enriched == []


def test_a_home_in_an_area_that_is_simply_cheap_never_costs_a_page_read(
    tmp_path: pathlib.Path,
) -> None:
    """Below the floor is not the same as below what this neighbourhood asks.

    Both halves have to be true before a request is spent: below anything a
    home that size lets for, and far below what its own area and size go for.
    A $1,600 three-bedroom where the area's own three-bedrooms go for $2,600
    is cheap for the city and ordinary for the street.
    """
    repository, preferences = board(tmp_path)
    area = [cheap_listing(f"neighbour-{index}", price=2600) for index in range(8)]
    scan(repository, preferences, Source([*area, cheap_listing("cheap", price=1600)]), "manual")
    forget_the_page_reads(repository)

    second = Source([*area, cheap_listing("cheap", price=1600)])
    scan(repository, preferences, second)

    assert second.enriched == []


def test_an_area_whose_own_median_is_below_the_floor_cannot_vouch_for_it(
    tmp_path: pathlib.Path,
) -> None:
    """Otherwise a pile of identical fakes would vouch for one another.

    The medians are taken from the same board the suspicious homes are on, so
    a source flooding it with $1,400 three-bedrooms would make $1,400 the
    going rate for the area and none of them would ever be opened.
    """
    repository, preferences = board(tmp_path)
    flood = [cheap_listing(f"fake-{index}") for index in range(8)]
    scan(repository, preferences, Source(flood), "manual")
    forget_the_page_reads(repository)

    second = Source(flood)
    scan(repository, preferences, second)

    assert len(second.enriched) == len(flood)


def test_a_page_that_says_the_post_is_gone_takes_the_home_off_the_shortlist(
    tmp_path: pathlib.Path,
) -> None:
    """Proof removes, and proof is the only thing that does.

    The page read is not there to hide homes -- but when the page itself says
    the post is removed, that is the one kind of evidence this app acts on.
    """
    repository, preferences = board(tmp_path)
    scan(repository, preferences, Source([cheap_listing("scam")]), "manual")
    forget_the_page_reads(repository)

    scan(repository, preferences, Source([cheap_listing("scam")], removed={"scam"}))

    assert shortlist(repository) == []
    rows = repository.query_listings(0, view="all", housing_kind="whole_unit")
    assert rows, "still stored, and still searchable"
    assert rows[0]["metadata"].get("page_verified_at") is None, (
        "a page that says the post is gone has proved the opposite of confirming it"
    )


def test_a_page_read_that_fails_leaves_the_home_exactly_as_it_was(
    tmp_path: pathlib.Path,
) -> None:
    """Unreachable is not gone, and a wifi drop must not touch the board.

    The home keeps its score, keeps its place on the shortlist, and keeps
    saying that nobody has confirmed it -- so the next scan tries again rather
    than counting a failure as an answer.
    """
    repository, preferences = board(tmp_path)
    scan(repository, preferences, Source([cheap_listing("too-good")]), "manual")
    forget_the_page_reads(repository)
    before = repository.query_listings(0, view="all", housing_kind="whole_unit")[0]

    offline = Source([cheap_listing("too-good")], unreachable={"too-good"})
    outcome = Scanner(repository, lambda: preferences, [offline]).run_scan("scheduled")

    after = repository.query_listings(0, view="all", housing_kind="whole_unit")[0]
    assert outcome.status == "completed", "a failed page read is not a failed scan"
    assert offline.enriched == ["too-good"], "it was tried"
    assert after["score"] == before["score"]
    assert after["eligibility"] == before["eligibility"]
    assert after["metadata"].get("page_verified_at") is None, "nothing was confirmed"
    assert shortlist(repository) == ["too-good"]


def test_a_home_whose_page_was_read_this_morning_is_not_opened_again(
    tmp_path: pathlib.Path,
) -> None:
    """A page read costs a request, and the same answer twice costs two."""
    repository, preferences = board(tmp_path)
    scan(repository, preferences, Source([cheap_listing("too-good")]), "manual")

    second = Source([cheap_listing("too-good")])
    scan(repository, preferences, second)

    assert second.enriched == []


def test_no_more_pages_than_the_budget_are_opened_in_one_scan(
    tmp_path: pathlib.Path,
) -> None:
    """A source with a hundred cheap homes must not become a crawl of them."""
    repository, preferences = board(tmp_path)
    many = [cheap_listing(f"cheap-{index}") for index in range(CHEAP_RENT_PAGES_PER_SCAN + 5)]
    scan(repository, preferences, Source(many), "manual")
    forget_the_page_reads(repository)

    second = Source(many)
    scan(repository, preferences, second)

    assert len(second.enriched) == CHEAP_RENT_PAGES_PER_SCAN


def test_a_page_the_scan_has_just_read_is_not_read_a_second_time(
    tmp_path: pathlib.Path,
) -> None:
    """A newly collected cheap home is enriched on the way in already.

    The confirmation is the same fact whichever pass did the reading, so
    without recording it the scan that first collected a suspicious home
    would immediately open its page again for the answer it already had --
    two requests for one, on every new cheap listing.
    """
    repository, preferences = board(tmp_path)
    reader = Source([cheap_listing("too-good")])
    reader.detail_budget = 1
    scan(repository, preferences, reader, "manual")

    assert reader.enriched == ["too-good"], "read once, on the way in"
    row = repository.query_listings(0, view="all", housing_kind="whole_unit")[0]
    assert row["metadata"].get("page_verified_at"), "and the reading is written down"


def test_a_source_that_cannot_read_a_listing_page_is_never_asked_to(
    tmp_path: pathlib.Path,
) -> None:
    """Ten of the app's sources publish no page worth opening."""
    repository, preferences = board(tmp_path)
    blind = Blind([cheap_listing("too-good")])
    outcome = Scanner(repository, lambda: preferences, [blind]).run_scan("manual")

    assert outcome.status == "completed"


def test_the_numbers_this_rule_turns_on_are_the_ones_that_were_decided() -> None:
    """Every other test here is measured against these, so none of them pins one.

    Tuning them is a product decision meant to change this line with it;
    drifting into them by accident is not.
    """
    assert CHEAP_RENT_CONFIRMED_FOR == timedelta(hours=36)
    assert CHEAP_RENT_PAGES_PER_SCAN == 25
    assert FAR_BELOW_MARKET_SHARE == 0.6
