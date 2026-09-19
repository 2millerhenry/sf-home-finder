"""D2 and the views: each home once, a second copy only when it disagrees, and
every scored home on some page.

On the live shortlist 1114 Sutter St held 5 of 139 slots (WO-3 todo 1); 70
scored, active listings were reachable from no view at all (todo 7); and the
same flat was carried at two rents with the cheaper copy ranking higher, so
the sort favoured whichever site quoted the teaser (todo 4).
"""

from __future__ import annotations

import csv
import io
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sf_housing.app import create_app
from sf_housing.database import Repository
from sf_housing.models import ListingCandidate, ScoreResult
from sf_housing.settings import Settings
from tests.conftest import TEST_PREFERENCES


def unit(platform: str, slug: str, address: str, *, price: int | None = 3200, score: int = 80,
         eligibility: str = "eligible", published: str | None = None, move_in: str | None = None,
         unit_type: str | None = "one_bedroom", neighborhood: str = "Mission",
         summary: str = "Entire one bedroom apartment.", **metadata) -> ListingCandidate:
    url = {
        "Zillow": f"https://www.zillow.com/homedetails/x/{slug}_zpid/",
        "Movoto": f"https://www.movoto.com/san-francisco-ca/{slug}/for-rent/",
        "Redfin": f"https://www.redfin.com/CA/San-Francisco/x/unit-1/home/{slug}",
        "AvalonBay": f"https://www.avaloncommunities.com/unit/{slug}",
    }[platform]
    extra = {"address": address, **metadata}
    if published:
        extra["listing_timestamp"] = published
    listing = ListingCandidate(
        platform=platform, source_id=slug, title=address, original_url=url, price=price,
        neighborhood=neighborhood, listing_type="Apartment", summary=summary,
        metadata=extra, housing_kind="whole_unit", unit_type=unit_type,
    )
    details = {"availability": {"available_on": move_in}} if move_in else {}
    return listing, ScoreResult(score, ["Stored"], "", details, eligibility=eligibility)


def put(repository: Repository, pair) -> int:
    listing, result = pair
    listing_id, _ = repository.upsert_listing(listing, result)
    return listing_id


@pytest.fixture
def repository(tmp_path: Path) -> Repository:
    instance = Repository(tmp_path / "housing.sqlite3")
    instance.initialize()
    return instance


def shortlist(repository: Repository, **kwargs) -> list[dict]:
    return repository.query_listings(minimum_score=60, housing_kind="whole_unit", view="active", **kwargs)


# --------------------------------------------------------------------------
# at most two copies, and the second must earn its place
# --------------------------------------------------------------------------


def test_three_identical_copies_are_one_row(repository: Repository) -> None:
    for platform, slug in (("Zillow", "1"), ("Movoto", "m1"), ("AvalonBay", "a1")):
        put(repository, unit(platform, slug, "295 Buchanan St #105"))

    rows = shortlist(repository)

    assert len(rows) == 1
    assert rows[0]["other_copy"] is None, "two identical rows are noise"
    assert rows[0]["copy_count"] == 3
    assert len(rows[0]["other_platforms"]) == 2


def test_a_copy_quoting_another_rent_is_shown_under_it_and_a_third_is_not(repository: Repository) -> None:
    put(repository, unit("Zillow", "1", "295 Buchanan St #105", price=7195))
    put(repository, unit("Movoto", "m1", "295 Buchanan St #105", price=7495))
    put(repository, unit("AvalonBay", "a1", "295 Buchanan St #105", price=7195))

    rows = shortlist(repository)

    assert len(rows) == 1
    shown, other = rows[0], rows[0]["other_copy"]
    assert {shown["price"], other["price"]} == {7195, 7495}
    assert other["disagreement"] == "price"


def test_a_copy_with_a_date_the_first_lacks_is_shown(repository: Repository) -> None:
    posted = put(repository, unit("Zillow", "1", "295 Buchanan St #105", published="2026-09-10T00:00:00+00:00"))
    moving = put(repository, unit("Movoto", "m1", "295 Buchanan St #105", move_in="2026-10-01"))

    row = shortlist(repository)[0]

    # A posting date outranks a move-in date, so the posted copy leads and the
    # other is shown for the move-in date it alone states.
    assert row["id"] == posted and row["available_on"] is None
    assert row["other_copy"]["id"] == moving
    assert (row["other_copy"]["disagreement"], row["other_copy"]["available_on"]) == ("dates", "2026-10-01")


def test_the_copy_that_says_more_leads(repository: Repository) -> None:
    """Ranked by what each tells the reader, not by which site it is."""
    put(repository, unit("Movoto", "m1", "295 Buchanan St #105", price=None))
    dated = put(repository, unit("Zillow", "1", "295 Buchanan St #105", published="2026-09-10T00:00:00+00:00"))

    rows = shortlist(repository)

    assert rows[0]["id"] == dated
    assert rows[0]["other_copy"] is None, "the other adds no rent and no date"


def test_a_copy_that_says_the_home_is_gone_is_shown_first(repository: Repository) -> None:
    put(repository, unit("Zillow", "1", "295 Buchanan St #105", price=3200))
    put(repository, unit("Movoto", "m1", "295 Buchanan St #105", price=3400))
    gone = put(repository, unit("AvalonBay", "a1", "295 Buchanan St #105", price=3200, verified_inactive=True))

    rows = shortlist(repository)

    assert rows[0]["other_copy"]["id"] == gone
    assert rows[0]["other_copy"]["disagreement"] == "status"


def test_a_home_ranks_on_its_least_favourable_copy(repository: Repository) -> None:
    """WO-3 todo 4: the cheaper quote must not carry the flat up the list."""
    put(repository, unit("Zillow", "1", "295 Buchanan St #105", price=2900, score=92))
    put(repository, unit("Movoto", "m1", "295 Buchanan St #105", price=3500, score=70))
    single = put(repository, unit("Zillow", "2", "10 Oak St #3", score=80))

    order = [row["id"] for row in shortlist(repository)]

    assert order[0] == single


def test_a_note_on_any_copy_is_never_hidden(repository: Repository) -> None:
    a = put(repository, unit("Zillow", "1", "295 Buchanan St #105", published="2026-09-10T00:00:00+00:00"))
    b = put(repository, unit("Movoto", "m1", "295 Buchanan St #105"))
    c = put(repository, unit("AvalonBay", "a1", "295 Buchanan St #105"))
    repository.set_listing_note(b, "Spoke to Dana, tour Saturday")
    repository.set_listing_note(c, "Parking is extra")

    row = shortlist(repository)[0]

    shown = [row["note"]] + [entry["note"] for entry in row["other_notes"]]
    assert sorted(shown) == ["Parking is extra", "Spoke to Dana, tour Saturday"]
    assert row["id"] in {b, c}, "a noted copy leads, so its note is the one edited in place"


def test_the_slider_counts_homes_not_rows(repository: Repository) -> None:
    for platform, slug in (("Zillow", "1"), ("Movoto", "m1"), ("AvalonBay", "a1")):
        put(repository, unit(platform, slug, "295 Buchanan St #105"))
    put(repository, unit("Zillow", "2", "10 Oak St #3"))

    counted = repository.shortlist_counts([60], kinds=["whole_unit"], unit_types=("one_bedroom",))[60]

    assert counted == len(shortlist(repository, unit_types=("one_bedroom",))) == 2


def test_the_cut_off_estimate_counts_a_home_once(repository: Repository) -> None:
    for platform, slug in (("Zillow", "1"), ("Movoto", "m1"), ("AvalonBay", "a1")):
        put(repository, unit(platform, slug, "295 Buchanan St #105"))
    pool, _, exact = repository.shortlist_pool(kinds=["whole_unit"])
    assert exact and len(pool) == 1


def test_a_home_on_the_shortlist_is_not_also_a_near_match(repository: Repository) -> None:
    put(repository, unit("Zillow", "1", "295 Buchanan St #105", score=80))
    put(repository, unit("Movoto", "m1", "295 Buchanan St #105", score=57))

    near = repository.query_listings(minimum_score=60, housing_kind="whole_unit", view="near_matches")

    assert near == []


def test_what_held_homes_back_does_not_count_a_shortlisted_home(repository: Repository) -> None:
    put(repository, unit("Zillow", "1", "295 Buchanan St #105", score=80))
    put(repository, unit("Movoto", "m1", "295 Buchanan St #105", score=40))
    put(repository, unit("Zillow", "2", "10 Oak St #3", score=40))

    summary = repository.exclusion_summary(60, "whole_unit", ("one_bedroom",))

    assert sum(item["count"] for item in summary) == 1


def test_a_neighbourhood_is_offered_once_however_it_was_cased(repository: Repository) -> None:
    put(repository, unit("Zillow", "1", "1 A St #1", neighborhood="tenderloin"))
    put(repository, unit("Zillow", "2", "2 B St #1", neighborhood="Tenderloin"))
    put(repository, unit("Zillow", "3", "3 C St #1", neighborhood="SOMA / south beach"))
    put(repository, unit("Zillow", "4", "4 D St #1", neighborhood="SoMa / South Beach"))

    neighborhoods, _ = repository.filter_options(60, "whole_unit")

    assert "Tenderloin" in neighborhoods and "tenderloin" not in neighborhoods
    assert len([value for value in neighborhoods if value.casefold() == "soma / south beach"]) == 1
    assert len(shortlist(repository, neighborhood="TENDERLOIN")) == 2


# --------------------------------------------------------------------------
# every scored home is on some page
# --------------------------------------------------------------------------


def test_a_whole_home_of_no_stated_size_is_on_the_other_tab(repository: Repository) -> None:
    """871 of the 941 unreachable homes on a real board were this."""
    sizeless = put(repository, unit("Zillow", "1", "1 A St #1", unit_type=None))
    with repository.connection() as connection:
        connection.execute("UPDATE listings SET unit_type = NULL")
        connection.commit()

    tabs = (False, ("studio", "one_bedroom"))
    other = repository.query_listings(minimum_score=60, housing_kind="other", view="active", outside_tabs=tabs)
    one_bed = repository.query_listings(minimum_score=60, housing_kind="whole_unit", unit_types=("one_bedroom",))

    assert [row["id"] for row in other] == [sizeless]
    assert one_bed == []
    assert repository.has_homes(60, "active", "other", tabs)


def test_every_scored_active_home_lands_in_some_tab(repository: Repository) -> None:
    put(repository, unit("Zillow", "1", "1 A St #1"))
    put(repository, unit("Zillow", "2", "2 B St #1", unit_type="two_bedroom"))
    put(repository, unit("Zillow", "3", "3 C St #1", unit_type=None))
    with repository.connection() as connection:
        connection.execute("UPDATE listings SET unit_type = NULL WHERE source_id = '3'")
        connection.execute("UPDATE listings SET housing_kind = 'unknown' WHERE source_id = '2'")
        connection.commit()
        eligible = connection.execute(
            "SELECT COUNT(*) FROM listings WHERE score >= 60 AND eligibility <> 'ineligible'"
        ).fetchone()[0]
    tabs = (True, ("one_bedroom",))
    seen = len(repository.query_listings(minimum_score=60, housing_kind="room"))
    seen += len(repository.query_listings(minimum_score=60, housing_kind="whole_unit", unit_types=("one_bedroom",)))
    seen += len(repository.query_listings(minimum_score=60, housing_kind="other", outside_tabs=tabs))
    assert seen == eligible == 3


# --------------------------------------------------------------------------
# the page and its spreadsheet
# --------------------------------------------------------------------------


# What the test deal (rooms, and whole homes up to its budget in its areas)
# still shortlists after the app rescores the board on start.
SIZELESS = "Entire apartment in a Victorian, whole place to yourself, sunny, near the park."
ONE_BED = "Entire one bedroom apartment in a Victorian, sunny."


@pytest.fixture
def app_and_repository(tmp_path: Path):
    data_dir = tmp_path / "data"
    preferences_path = tmp_path / "preferences.yaml"
    preferences_path.write_text(TEST_PREFERENCES, encoding="utf-8")
    settings = Settings(data_dir=data_dir, preferences_path=preferences_path,
                        database_path=data_dir / "housing.sqlite3", log_path=data_dir / "test.log")
    repository = Repository(settings.database_path)
    repository.initialize()
    return create_app(settings=settings, sources=[], enable_scheduler=False), repository


def test_the_other_tab_is_a_page_and_is_offered_when_it_holds_something(app_and_repository) -> None:
    """No tab is for a whole home whose size no source stated."""
    application, repository = app_and_repository
    put(repository, unit("Zillow", "1", "295 Buchanan St #105", price=1900, unit_type=None, summary=SIZELESS))
    with TestClient(application) as client:
        shortlist_page = client.get("/?view=active")
        other_page = client.get("/?housing=other&view=active")
    assert shortlist_page.status_code == other_page.status_code == 200
    assert 'href="/?housing=other&amp;view=active&amp;sort=score"' in shortlist_page.text
    assert "295 Buchanan St #105" in other_page.text
    assert "Other homes worth a look" in other_page.text


def test_the_other_tab_is_not_offered_when_it_is_empty(app_and_repository) -> None:
    application, _ = app_and_repository
    with TestClient(application) as client:
        page = client.get("/?view=active")
    assert "housing=other" not in page.text


def test_saved_holds_every_kind_of_home_whatever_tab_was_open(app_and_repository) -> None:
    application, repository = app_and_repository
    starred = put(repository, unit("Zillow", "1", "295 Buchanan St #105"))
    repository.set_listing_status(starred, "saved")
    with TestClient(application) as client:
        page = client.get("/?view=saved&housing=room")
    assert "295 Buchanan St #105" in page.text
    assert 'aria-label="Home size"' not in page.text, "no size tabs on the user's own lists"


def test_the_page_shows_the_disagreeing_copy_under_the_home(app_and_repository) -> None:
    application, repository = app_and_repository
    put(repository, unit("Zillow", "1", "295 Buchanan St #105", price=1900, summary=ONE_BED))
    put(repository, unit("Movoto", "m1", "295 Buchanan St #105", price=1950, summary=ONE_BED))
    with TestClient(application) as client:
        page = client.get("/?housing=one_bedroom&view=active").text
    assert page.count('class="listing-copy"') == 1
    assert "the two sites quote different rents ($1,950 and $1,900)" in page or (
        "the two sites quote different rents ($1,900 and $1,950)" in page
    )


def test_the_spreadsheet_holds_the_page_s_rows_and_the_disagreement(app_and_repository) -> None:
    application, repository = app_and_repository
    put(repository, unit("Zillow", "1", "295 Buchanan St #105", price=1900, summary=ONE_BED))
    b = put(repository, unit("Movoto", "m1", "295 Buchanan St #105", price=1950, summary=ONE_BED))
    put(repository, unit("AvalonBay", "a1", "295 Buchanan St #105", price=1900, summary=ONE_BED))
    repository.set_listing_note(b, "Ask about the $50 gap")
    with TestClient(application) as client:
        response = client.get("/listings.csv?housing=one_bedroom&view=active")
    table = list(csv.DictReader(io.StringIO(response.text)))
    assert len(table) == 1
    assert table[0]["Other source"] in {"Zillow", "AvalonBay", "Movoto"}
    assert {table[0]["Price"], table[0]["Other price"]} == {"1900", "1950"}
    assert "Ask about the $50 gap" in table[0]["Note"]


def test_the_spreadsheet_says_aged_out_rather_than_passed(app_and_repository) -> None:
    application, repository = app_and_repository
    aged = put(repository, unit("Zillow", "1", "295 Buchanan St #105", price=1900, summary=ONE_BED))
    with sqlite3.connect(repository.path) as connection:
        connection.execute("UPDATE listings SET first_found = '2026-01-01T00:00:00+00:00'")
    repository.archive_stale_listings()
    with TestClient(application) as client:
        response = client.get("/listings.csv?housing=one_bedroom&view=all")
        page = client.get("/?housing=one_bedroom&view=all").text
    table = list(csv.DictReader(io.StringIO(response.text)))
    assert [row["Status"] for row in table] == ["aged out"]
    assert "Aged out" in page
    assert aged


def test_a_copy_that_puts_the_home_outside_the_deal_says_why(app_and_repository) -> None:
    """On the real board four of five copies shown were this: ApartmentGuide
    naming no area, Rent.com naming the Tenderloin, which the deal excludes."""
    from sf_housing.preferences import parse_preferences
    from sf_housing.scoring import score_listing

    application, repository = app_and_repository
    deal = parse_preferences(TEST_PREFERENCES)
    for listing, _ in (
        unit("Zillow", "1", "295 Buchanan St #105", price=1900, summary=ONE_BED),
        unit("Movoto", "m1", "295 Buchanan St #105", price=1900, summary=ONE_BED, neighborhood="Tenderloin"),
    ):
        repository.upsert_listing(listing, score_listing(listing, deal))
    with TestClient(application) as client:
        page = client.get("/?housing=one_bedroom&view=active").text
    import re
    cells = re.findall(r'<td class="cell-copy-note".*?</td>', page, re.S)
    assert any("listing puts it outside your deal: Tenderloin is outside your target neighborhoods." in cell
               for cell in cells), cells


def test_when_the_copy_shown_is_the_one_taken_down_the_page_says_so(app_and_repository) -> None:
    """A noted copy is shown first even after its site takes it down; the
    second row must then say which site still lists it, not blame that one."""
    application, repository = app_and_repository
    put(repository, unit("Zillow", "1", "295 Buchanan St #105", price=1900, summary=ONE_BED))
    gone = put(repository, unit("Movoto", "m1", "295 Buchanan St #105", price=1900, summary=ONE_BED))
    repository.set_listing_note(gone, "Called the agent on Monday")
    with sqlite3.connect(repository.path) as connection:
        connection.execute(
            "UPDATE listings SET metadata_json = json_set(metadata_json, '$.verified_inactive', json('true')) "
            "WHERE id = ?", (gone,),
        )
    with TestClient(application) as client:
        page = client.get("/?housing=one_bedroom&view=all").text
    assert "Movoto says it is no longer listed; Zillow still lists it" in page


def test_the_cut_off_slider_counts_the_other_homes_tab_too(app_and_repository) -> None:
    """Every shortlisted home is on exactly one tab, the Other homes tab
    included, so the number under the slider is the sum of the tabs."""
    import json
    import re

    from sf_housing.preferences import parse_preferences
    from sf_housing.scoring import score_listing

    application, repository = app_and_repository
    deal = parse_preferences(TEST_PREFERENCES)
    for listing, _ in (
        unit("Zillow", "1", "295 Buchanan St #105", price=1900, unit_type=None, summary=SIZELESS),
        unit("Zillow", "2", "10 Oak St #3", price=1900, summary=ONE_BED),
    ):
        repository.upsert_listing(listing, score_listing(listing, deal))
    with TestClient(application) as client:
        tabs = sum(
            client.get(f"/?housing={tab}&view=active").text.count('<tr class="listing-row')
            for tab in ("room", "studio", "one_bedroom", "two_bedroom", "three_bedroom", "other")
        )
        page = client.get("/preferences").text
    counts = json.loads(re.search(r"data-cutoff-counts='([^']+)'", page).group(1))
    assert tabs == 2
    assert counts[str(deal.minimum_score)] == tabs


def test_a_draft_deal_counts_a_home_when_any_copy_suits_it(repository: Repository) -> None:
    """The draft's estimate scored one copy of each home. When that copy
    failed the draft and another passed, the page after saving showed a home
    the estimate had not counted."""
    from sf_housing.preferences import parse_preferences
    from sf_housing.shortlist_estimate import estimate_shortlist_counts

    deal = parse_preferences(TEST_PREFERENCES)
    # Zillow outranks Redfin as the copy the home is shown by; only Redfin's
    # rent is inside the deal.
    put(repository, unit("Zillow", "1", "295 Buchanan St #105", price=3200, summary=ONE_BED))
    put(repository, unit("Redfin", "r1", "295 Buchanan St #105", price=1900, summary=ONE_BED))

    estimate = estimate_shortlist_counts(repository, deal, [60])

    assert estimate.exact and estimate.counts[60] == 1


def test_a_home_is_counted_at_its_best_copy(repository: Repository) -> None:
    from sf_housing.preferences import parse_preferences
    from sf_housing.scoring import score_listing
    from sf_housing.shortlist_estimate import estimate_shortlist_counts

    deal = parse_preferences(TEST_PREFERENCES)
    # One site places it in a preferred area, the other in one the deal only
    # accepts: both suit the deal, one better than the other.
    cheap, _ = unit("Zillow", "1", "295 Buchanan St #105", price=1900, summary=ONE_BED)
    dear, _ = unit("Redfin", "r1", "295 Buchanan St #105", price=1900, summary=ONE_BED,
                   neighborhood="Bernal Heights")
    scores = [score_listing(listing, deal) for listing in (cheap, dear)]
    assert all(result.eligibility != "ineligible" for result in scores)
    best, worst = max(result.score for result in scores), min(result.score for result in scores)
    assert best > worst, "the two rents must score differently for this to test anything"
    for listing, result in zip((cheap, dear), scores):
        repository.upsert_listing(listing, result)

    estimate = estimate_shortlist_counts(repository, deal, [best])

    assert estimate.counts[best] == 1


def _age(repository: Repository, listing_id: int, days: int) -> None:
    from datetime import UTC, datetime, timedelta

    stamp = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
    with sqlite3.connect(repository.path) as connection:
        connection.execute(
            "UPDATE listings SET last_seen = ?, metadata_json = json_set(metadata_json, '$.last_verified_at', ?) "
            "WHERE id = ?", (stamp, stamp, listing_id),
        )


def test_a_home_is_shown_by_a_copy_a_site_still_lists(repository: Repository) -> None:
    """Movoto stopped returning the flat three days ago; Zillow returned it
    today. The home is shown by Zillow's copy, and Movoto's old quote neither
    claims a disagreement nor holds the home down."""
    dropped = put(repository, unit("Movoto", "m5", "2400 Mission St #5", price=3150, score=40, eligibility="ineligible"))
    current = put(repository, unit("Zillow", "111", "2400 Mission St #5", price=2600, score=85))
    _age(repository, dropped, 3)
    rival = put(repository, unit("Zillow", "222", "10 Oak St #3", score=82))

    rows = repository.query_listings(minimum_score=60, housing_kind="whole_unit")

    assert [row["id"] for row in rows] == [current, rival]
    assert rows[0]["other_copy"] is None


def test_saved_shows_a_starred_home_by_the_copy_inside_the_deal(repository: Repository) -> None:
    put(repository, unit("Movoto", "m5", "2400 Mission St #5", price=3150, score=49, eligibility="ineligible"))
    fits = put(repository, unit("Zillow", "111", "2400 Mission St #5", price=2600, score=85))
    repository.set_listing_status(fits, "saved")

    saved = repository.query_listings(view="saved", housing_kind="")
    shortlist = repository.query_listings(minimum_score=60, housing_kind="whole_unit")

    assert [row["id"] for row in saved] == [row["id"] for row in shortlist] == [fits]


def test_of_two_copies_inside_the_deal_the_one_confirmed_lately_is_shown(repository: Repository) -> None:
    """Movoto outranks Zillow on site order, but it has not returned the flat
    for three days; Zillow returned it today."""
    dropped = put(repository, unit("Movoto", "m5", "2400 Mission St #5", price=2600, score=85))
    current = put(repository, unit("Zillow", "111", "2400 Mission St #5", price=2600, score=85))
    _age(repository, dropped, 3)

    rows = repository.query_listings(minimum_score=60, housing_kind="whole_unit")

    assert [row["id"] for row in rows] == [current]


def test_a_copy_that_only_says_less_does_not_hold_a_home_down(repository: Repository) -> None:
    """Movoto's copy states no rent and scores low for it; that is missing
    information, not a site saying the home is worse."""
    stated = put(repository, unit("Zillow", "111", "2400 Mission St #5", price=2600, score=85))
    put(repository, unit("Movoto", "m5", "2400 Mission St #5", price=None, score=62))
    rival = put(repository, unit("Zillow", "222", "10 Oak St #3", score=80))

    order = [row["id"] for row in repository.query_listings(minimum_score=60, housing_kind="whole_unit")]

    assert order == [stated, rival]


def test_a_home_no_copy_prices_ranks_on_its_lowest_copy(repository: Repository) -> None:
    """With no copy stating a rent, every copy says as little, and the home is
    ranked on the least favourable of them."""
    put(repository, unit("Zillow", "111", "2400 Mission St #5", price=None, score=85))
    put(repository, unit("Movoto", "m5", "2400 Mission St #5", price=None, score=70))
    rival = put(repository, unit("Zillow", "222", "10 Oak St #3", price=None, score=80))

    order = [row["id"] for row in repository.query_listings(minimum_score=60, housing_kind="whole_unit")]

    assert order[0] == rival
