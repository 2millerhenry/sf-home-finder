from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from pathlib import Path

from fastapi.testclient import TestClient

from datetime import UTC, datetime, timedelta

from sf_housing.app import (
    _compact_move_in_date,
    _compact_pacific_date,
    _is_recently_posted,
    _was_checked_today,
    _typical_scan_seconds,
    create_app,
)
from sf_housing.scheduling import PACIFIC
from sf_housing.database import Repository
from sf_housing.freshness import FRESHNESS_WINDOW
from sf_housing.models import ListingCandidate, ScoreResult
from sf_housing.preferences import load_preferences
from sf_housing.scoring import score_listing
from sf_housing.settings import Settings
from tests.test_deal_profile import block_body, stylesheet
from tests.conftest import TEST_PREFERENCES

def already_works_headline(html: str) -> str:
    """The page's own claim about how many sources need no setup, checked
    against the chips it actually renders beside it.

    The sentence used to be a literal that said "six" while seven sources ran.
    Counting the chips is what makes the claim true for whatever list the app
    was built with, rather than true only for the default one."""
    import re as _re

    from sf_housing.app import spelled_count

    headline = _re.search(r"<h2 id=\"already-title\">([^<]+)</h2>", html)
    assert headline, "the page no longer states what already works"
    section = html[html.index('id="already-title"') : html.index("</section>", html.index('id="already-title"'))]
    chips = _re.findall(r'class="source-chip"', section)
    assert headline.group(1) == f"{spelled_count(len(chips))} sources already work", (
        f"headline {headline.group(1)!r} disagrees with the {len(chips)} sources listed beside it"
    )
    return headline.group(1)




@contextmanager
def scan_requests(application, *, starts: bool = True):
    """Record what a route asks to scan, without running it.

    The Facebook check now really does reach the Facebook source, so a test
    that lets it run would call Apify for real.
    """
    scanner = application.state.scanner
    original = scanner.start_scan
    asked: list[tuple[str, list[str]]] = []

    def stub(trigger: str = "manual", sources=None):
        state = application.state.repository.connector_state("apify")
        asked.append((state.state if state else None, [s.platform for s in (sources or [])]))
        return starts

    scanner.start_scan = stub
    try:
        yield asked
    finally:
        scanner.start_scan = original


class WaitingDashboardSource:
    platform = "Waiting source"
    mode = "automatic"
    search_url = "https://example.test/waiting"
    manual_reason = None
    detail_budget = 0

    def __init__(self, started: threading.Event, release: threading.Event):
        self.started = started
        self.release = release

    def search(self, client, preferences):
        self.started.set()
        assert self.release.wait(timeout=3)
        return []

    def enrich(self, client, listing):
        return listing


class EmptyFacebookSource:
    platform = "Facebook Marketplace"
    mode = "automatic"
    search_url = "https://example.test/facebook"
    manual_reason = None


class StaleDashboardSource:
    platform = "Craigslist"
    mode = "automatic"
    search_url = "https://example.test/craigslist"
    manual_reason = None


def app_settings(tmp_path: Path) -> Settings:
    data_dir = tmp_path / "data"
    preferences_path = tmp_path / "preferences.yaml"
    preferences_path.write_text(TEST_PREFERENCES, encoding="utf-8")
    return Settings(
        data_dir=data_dir,
        preferences_path=preferences_path,
        database_path=data_dir / "housing.sqlite3",
        log_path=data_dir / "test.log",
    )


def add_listing(
    repository: Repository,
    platform: str,
    source_id: str,
    title: str,
    price: int,
    neighborhood: str,
    metadata: dict | None = None,
):
    repository.upsert_listing(
        ListingCandidate(
            platform=platform,
            source_id=source_id,
            title=title,
            original_url=f"https://example.test/{source_id}",
            price=price,
            neighborhood=neighborhood,
            metadata=metadata or {},
        ),
        ScoreResult(80, ["Good fit"], "Unknown: lease.", {}),
    )


def test_dashboard_filters_by_platform_and_neighborhood(tmp_path: Path) -> None:
    settings = app_settings(tmp_path)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    repository = application.state.repository
    add_listing(repository, "Craigslist", "wanted", "Wanted Mission room", 1500, "Mission")
    add_listing(repository, "SpareRoom", "wrong-platform", "Other Mission room", 1400, "Mission")
    add_listing(repository, "Craigslist", "wrong-area", "Richmond room", 1300, "Inner Richmond")

    with TestClient(application) as client:
        response = client.get("/?platform=Craigslist&neighborhood=Mission&sort=price")

    assert response.status_code == 200
    assert "Wanted Mission room" in response.text
    assert "Other Mission room" not in response.text
    assert "Richmond room" not in response.text
    assert 'href="/?sort=score&amp;view=active&amp;neighborhood=Mission&amp;platform=Craigslist"' in response.text
    assert 'href="/?sort=newest&amp;view=active&amp;neighborhood=Mission&amp;platform=Craigslist"' in response.text


def test_dashboard_calls_out_a_stale_source_without_hiding_other_results(tmp_path: Path) -> None:
    settings = app_settings(tmp_path)
    source = StaleDashboardSource()
    application = create_app(settings=settings, sources=[source], enable_scheduler=False)
    repository = application.state.repository
    run_id = repository.begin_scan("manual")
    source_run = repository.begin_source_run(
        run_id, source.platform, source.search_url, source_key="StaleDashboardSource"
    )
    repository.finish_source_run(source_run, "success", seen=1)
    repository.finish_scan(run_id, "completed")
    old = (datetime.now(UTC) - FRESHNESS_WINDOW - timedelta(minutes=1)).isoformat()
    with repository.connection() as connection:
        connection.execute("UPDATE source_runs SET finished_at = ? WHERE id = ?", (old, source_run))
        connection.commit()
    add_listing(repository, "Craigslist", "still-visible", "Still visible room", 1500, "Mission")

    with TestClient(application) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "Still visible room" in response.text
    assert "1 source needs attention" in response.text
    # A broken source is called out where the eye goes first, with the sentence
    # saying what to do about it -- not filed among the ones that are fine.
    alerts = response.text[response.text.index('class="source-alerts"') :]
    alerts = alerts[: alerts.index("</ul>")]
    assert "Craigslist" in alerts
    assert "Stale" in alerts
    assert "Use Check for new homes now" in alerts


def test_dashboard_keeps_each_listing_link_in_the_left_side_of_the_table(tmp_path: Path) -> None:
    settings = app_settings(tmp_path)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    repository = application.state.repository
    add_listing(repository, "Craigslist", "direct-link", "Direct link room", 1500, "Mission")

    with TestClient(application) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert 'class="cell-quick-open"' in response.text
    assert 'aria-label="Open Direct link room"' in response.text
    assert 'data-mark-opened="/listings/' in response.text
    listing_id = repository.query_listings(0, view="all")[0]["id"]
    # Two different destinations, not the same one twice: the button leaves for
    # the source, the title opens the home's own page.
    assert response.text.count(f'href="/listings/{listing_id}/open"') == 1
    assert f'href="/listings/{listing_id}?from=' in response.text


def test_dashboard_prioritizes_direct_application_and_direct_lister_routes(tmp_path: Path) -> None:
    settings = app_settings(tmp_path)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    repository = application.state.repository
    add_listing(
        repository,
        "Listings Project",
        "direct-lister",
        "Direct lister home",
        2200,
        "Mission",
        metadata={"direct_lister": True},
    )
    add_listing(
        repository,
        "Abacus (small buildings)",
        "direct-apply",
        "Direct application home",
        2100,
        "Mission",
        metadata={"application_url": "https://abacus.appfolio.com/listings/rental_applications/new?one"},
    )

    with TestClient(application) as client:
        response = client.get("/?sort=contact")

    assert response.status_code == 200
    assert 'value="contact" selected' in response.text
    assert "Apply now for Direct application home" in response.text
    assert "Contact the lister for Direct lister home" in response.text
    assert response.text.index("Direct application home") < response.text.index("Direct lister home")
    assert "Copy contact note" in response.text


def test_dashboard_marks_a_listing_viewed_after_opening_it(tmp_path: Path) -> None:
    settings = app_settings(tmp_path)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    repository = application.state.repository
    add_listing(repository, "Craigslist", "opened", "Opened room", 1500, "Mission")
    listing_id = repository.query_listings(0, view="all")[0]["id"]

    with TestClient(application) as client:
        marked = client.post(f"/listings/{listing_id}/opened")
        response = client.get("/")

    assert marked.status_code == 204
    assert 'class="listing-row opened-listing"' in response.text


def test_dashboard_can_sort_unopened_listings_first(tmp_path: Path) -> None:
    settings = app_settings(tmp_path)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    repository = application.state.repository
    add_listing(repository, "Craigslist", "unopened", "Untouched room", 1500, "Mission")
    add_listing(repository, "Craigslist", "opened-first", "Opened room", 1600, "Mission")
    opened_id = next(
        listing["id"]
        for listing in repository.query_listings(0, view="all")
        if listing["title"] == "Opened room"
    )
    assert repository.mark_listing_opened(opened_id)

    with TestClient(application) as client:
        response = client.get("/?sort=unopened")

    assert response.status_code == 200
    assert 'value="unopened" selected' in response.text
    assert "Not opened yet first" in response.text
    assert response.text.index("Untouched room") < response.text.index("Opened room")


def test_dashboard_open_route_marks_listing_then_redirects_to_original_url(tmp_path: Path) -> None:
    settings = app_settings(tmp_path)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    repository = application.state.repository
    add_listing(repository, "Craigslist", "open-route", "Open route room", 1500, "Mission")
    listing_id = repository.query_listings(0, view="all")[0]["id"]

    with TestClient(application) as client:
        response = client.get(f"/listings/{listing_id}/open", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "https://example.test/open-route"
    assert repository.query_listings(0, view="all")[0]["opened_at"] is not None


def test_dashboard_source_filter_keeps_connected_sources_visible_without_matches(tmp_path: Path) -> None:
    settings = app_settings(tmp_path)
    application = create_app(
        settings=settings,
        sources=[EmptyFacebookSource()],
        enable_scheduler=False,
    )

    with TestClient(application) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert 'value="Facebook Marketplace"' in response.text


def test_dashboard_exposes_home_filter_and_move_in_sort(tmp_path: Path) -> None:
    settings = app_settings(tmp_path)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    application.state.repository.upsert_listing(
        ListingCandidate(
            platform="Furnished Finder",
            source_id="house",
            title="Private room in a house",
            original_url="https://example.test/house",
            listing_type="Private room · House",
        ),
        ScoreResult(
            80,
            ["Good fit"],
            "Unknown: lease.",
            {
                "availability": {"available_on": "2026-08-01"},
                "home_facts": {"primary": "Private room · House", "secondary": ["Private bath"]},
            },
        ),
    )

    with TestClient(application) as client:
        response = client.get("/?platform=Furnished+Finder&home_style=house&sort=available")

    assert response.status_code == 200
    assert "Soonest move-in" in response.text
    assert 'name="home_style"' in response.text
    assert 'value="house" selected' in response.text
    assert "Home &amp; room" in response.text
    assert "Move-in" in response.text


def test_dashboard_uses_the_whole_unit_budget_in_its_labels(tmp_path: Path) -> None:
    settings = app_settings(tmp_path)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)

    with TestClient(application) as client:
        response = client.get("/?housing=whole_unit")

    assert response.status_code == 200
    assert "up to $3,000" in response.text
    assert "Entire places up to $3,000" in response.text


def test_dashboard_highlights_applied_filters_and_normalizes_an_invalid_order(tmp_path: Path) -> None:
    settings = app_settings(tmp_path)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    repository = application.state.repository
    add_listing(repository, "Craigslist", "filtered", "Filtered room", 1500, "Mission")

    with TestClient(application) as client:
        filtered = client.get("/?platform=Craigslist&sort=newest")
        invalid = client.get("/?sort=not-a-real-order")

    assert filtered.status_code == 200
    assert 'name="platform" class="is-selected"' in filtered.text
    assert 'name="sort" data-auto-submit-order class="is-selected"' in filtered.text
    assert 'th scope="col" class="cell-found" aria-sort="descending"' in filtered.text
    assert "Update filters" in filtered.text
    assert invalid.status_code == 200
    assert 'value="score" selected' in invalid.text


def test_dashboard_explains_empty_connected_source_filter(tmp_path: Path) -> None:
    settings = app_settings(tmp_path)
    application = create_app(
        settings=settings,
        sources=[EmptyFacebookSource()],
        enable_scheduler=False,
    )

    with TestClient(application) as client:
        response = client.get("/?platform=Facebook+Marketplace")

    assert "No Facebook Marketplace matches yet" in response.text
    assert "View every imported Facebook Marketplace listing" in response.text


def test_compact_listing_dates_are_easy_to_scan() -> None:
    now = datetime(2026, 7, 20, 12, 0, tzinfo=PACIFIC)

    assert _compact_pacific_date("2026-07-20T08:00:00-07:00", now=now) == "Today"
    assert _compact_pacific_date("2026-07-19T08:00:00-07:00", now=now) == "Yesterday"
    assert _compact_pacific_date("2026-06-03T08:00:00-07:00", now=now) == "Jun 3"
    assert _compact_move_in_date("2026-08-01") == "Aug 1, 2026"
    assert _was_checked_today("2026-07-20T08:00:00-07:00", now=now)
    assert not _was_checked_today("2026-07-19T23:59:00-07:00", now=now)


def test_recent_posting_indicator_uses_source_posted_time_not_first_discovery() -> None:
    now = datetime(2026, 7, 20, 19, 0, tzinfo=UTC)

    assert _is_recently_posted(
        {"metadata": {"listing_timestamp": (now - timedelta(hours=47, minutes=59)).isoformat()}}, now=now
    )
    assert _is_recently_posted({"metadata": {"listing_timestamp": (now - timedelta(days=2)).isoformat()}}, now=now)
    assert not _is_recently_posted({"metadata": {"listing_timestamp": (now - timedelta(days=2, seconds=1)).isoformat()}}, now=now)
    assert _is_recently_posted({"metadata": {}, "summary": "Listed yesterday"}, now=now)
    assert not _is_recently_posted({"metadata": {}, "summary": "Listed 3 days ago"}, now=now)
    assert not _is_recently_posted({"metadata": {}, "summary": "No date given"}, now=now)


def test_dashboard_marks_recent_listings_as_new(tmp_path: Path) -> None:
    settings = app_settings(tmp_path)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    add_listing(
        application.state.repository,
        "Craigslist",
        "fresh",
        "Fresh Mission room",
        1500,
        "Mission",
        metadata={"listing_timestamp": datetime.now(UTC).isoformat()},
    )

    with TestClient(application) as client:
        response = client.get("/")

    assert 'class="listing-row new-listing"' in response.text
    assert 'class="new-badge" title="Posted in the last two days">New</b>' in response.text


def test_typical_scan_time_uses_completed_runs_of_same_kind() -> None:
    scans = [
        {"trigger": "manual", "status": "completed", "started_at": "2026-07-20T12:00:00+00:00", "finished_at": "2026-07-20T12:00:03+00:00"},
        {"trigger": "manual", "status": "completed_with_errors", "started_at": "2026-07-20T12:01:00+00:00", "finished_at": "2026-07-20T12:01:05+00:00"},
        {"trigger": "scheduled", "status": "completed", "started_at": "2026-07-20T12:02:00+00:00", "finished_at": "2026-07-20T12:03:40+00:00"},
    ]

    assert _typical_scan_seconds(scans, "manual") == 4


def test_dashboard_shows_live_scan_progress_and_status_endpoint(tmp_path: Path) -> None:
    settings = app_settings(tmp_path)
    started, release = threading.Event(), threading.Event()
    source = WaitingDashboardSource(started, release)
    application = create_app(settings=settings, sources=[source], enable_scheduler=False)

    with TestClient(application) as client:
        response = client.post("/scan", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/?scan=starting"
        assert started.wait(timeout=2)

        page = client.get("/")
        active = client.get("/scan/status").json()
        assert "data-scan-progress" in page.text
        assert "scan-progress.js" in page.text
        assert active["running"] is True
        assert active["current_source"] == "Waiting source"
        assert active["sources_total"] == 1

        release.set()
        # Waited on through the scanner rather than by asking the endpoint over
        # and over until a deadline passed: what the endpoint reports is what
        # the scanner has finished writing, so that is the thing to block on.
        assert application.state.scanner.wait_until_idle(), "the scan never finished"
        finished = client.get("/scan/status").json()

    assert finished["running"] is False
    assert finished["status"] == "completed"
    assert finished["percent"] == 100


def test_dashboard_keeps_short_completion_feedback_after_an_instant_scan(tmp_path: Path) -> None:
    settings = app_settings(tmp_path)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)

    with TestClient(application) as client:
        response = client.post("/scan", follow_redirects=False)
        page = client.get(response.headers["location"])

    assert response.status_code == 303
    assert response.headers["location"] == "/?scan=starting"
    assert "data-scan-progress" in page.text
    assert "scan-progress.js" in page.text


def test_dashboard_defaults_to_best_match_order(tmp_path: Path) -> None:
    settings = app_settings(tmp_path)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    repository = application.state.repository
    repository.upsert_listing(
        ListingCandidate("Test", "possible", "Possible home", "https://example.test/possible", 1500, "Mission"),
        ScoreResult(62, ["Possible"], "Unknown: lease.", {}),
    )
    repository.upsert_listing(
        ListingCandidate("Test", "strong", "Strong home", "https://example.test/strong", 1500, "Mission"),
        ScoreResult(88, ["Strong"], "Unknown: lease.", {}),
    )

    with TestClient(application) as client:
        response = client.get("/")

    assert response.text.index("Strong home") < response.text.index("Possible home")
    assert 'option value="score" selected' in response.text


def test_dashboard_review_actions_persist(tmp_path: Path) -> None:
    settings = app_settings(tmp_path)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    repository = application.state.repository
    add_listing(repository, "Craigslist", "one", "Room to save", 1500, "Mission")
    listing_id = repository.query_listings(0, view="all")[0]["id"]

    with TestClient(application) as client:
        initial = client.get("/")
        saved = client.post(
            f"/listings/{listing_id}/status",
            data={"status": "saved", "return_to": "/"},
            follow_redirects=False,
        )
        noted = client.post(
            f"/listings/{listing_id}/note",
            data={"note": "Message tonight", "return_to": "/"},
            follow_redirects=False,
        )

    assert saved.status_code == 303
    assert noted.status_code == 303
    assert "☆ Star" in initial.text
    stored = repository.query_listings(0, view="saved")[0]
    assert stored["note"] == "Message tonight"

    with TestClient(application) as client:
        starred = client.get("/?view=saved")

    assert starred.status_code == 200
    assert "Starred listings" in starred.text
    # Saved is a comparison view, not the scanning table: the note you wrote is
    # visible without opening anything, and the star can be undone from here.
    assert "Message tonight" in starred.text
    # The same control the table uses, so the star reads the same either place.
    assert 'aria-label="Remove from starred listings"' in starred.text
    assert "★ Starred" in starred.text
    assert "Why it fits" in starred.text
    assert "Room to save" in starred.text


def test_dashboard_keeps_whole_units_separate_and_labels_the_unit_type(tmp_path: Path) -> None:
    settings = app_settings(tmp_path)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    repository = application.state.repository
    preferences = load_preferences(settings.preferences_path)
    listing = ListingCandidate(
        platform="Craigslist",
        source_id="studio-separate",
        title="Studio apartment in Mission",
        original_url="https://example.test/studio-separate",
        price=2400,
        neighborhood="Mission",
        summary="Studio apartment in a 12-unit building.",
    )
    repository.upsert_listing(listing, score_listing(listing, preferences))

    with TestClient(application) as client:
        room_page = client.get("/")
        unit_page = client.get("/?housing=whole_unit")

    assert "Studio apartment in Mission" not in room_page.text
    assert "Studios worth a look" in unit_page.text
    assert "Studio apartment in Mission" in unit_page.text
    assert ">Studio<" in unit_page.text
    assert "12-unit building" in unit_page.text
    # The tab is the size now, so a separate size dropdown would repeat it.
    assert 'name="unit_type"' not in unit_page.text


def test_each_split_size_has_its_own_tab_with_total_and_per_person_price(tmp_path: Path) -> None:
    settings = app_settings(tmp_path)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    repository = application.state.repository
    preferences = load_preferences(settings.preferences_path)
    listing = ListingCandidate(
        platform="Craigslist",
        source_id="two-bedroom-separate",
        title="2 bedroom apartment in NOPA",
        original_url="https://example.test/two-bedroom-separate",
        price=5200,
        neighborhood="NOPA",
        summary="Entire 2 bedroom apartment in a 14-unit building.",
    )
    three_bedroom_listing = ListingCandidate(
        platform="Craigslist",
        source_id="three-bedroom-separate",
        title="3 bedroom apartment in NOPA",
        original_url="https://example.test/three-bedroom-separate",
        price=7350,
        neighborhood="NOPA",
        summary="Entire 3 bedroom apartment in a 9-unit building.",
    )
    repository.upsert_listing(listing, score_listing(listing, preferences))
    repository.upsert_listing(
        three_bedroom_listing, score_listing(three_bedroom_listing, preferences)
    )

    with TestClient(application) as client:
        room_page = client.get("/")
        small_unit_page = client.get("/?housing=whole_unit")
        two_bedroom_page = client.get("/?housing=two_bedroom")
        only_two_bedrooms = client.get("/?housing=two_bedroom")
        only_three_bedrooms = client.get("/?housing=three_bedroom")

    assert "2 bedroom apartment in NOPA" not in room_page.text
    assert "2 bedroom apartment in NOPA" not in small_unit_page.text
    assert "2 bedrooms worth a look" in two_bedroom_page.text
    assert "2 bedroom apartment in NOPA" in two_bedroom_page.text
    assert "$5,200" in two_bedroom_page.text
    assert "$2,600/person" in two_bedroom_page.text
    assert "14-unit building" in two_bedroom_page.text
    # Each size has its own tab instead of hiding inside a combined dropdown.
    assert 'housing=three_bedroom' in two_bedroom_page.text
    # Each size keeps to its own tab, so a three-bedroom never pads the
    # two-bedroom results the way the combined view used to.
    assert "3 bedroom apartment in NOPA" not in two_bedroom_page.text
    assert "$2,450/person" in only_three_bedrooms.text
    assert "3 bedroom apartment in NOPA" not in only_two_bedrooms.text
    assert "2 bedroom apartment in NOPA" in only_two_bedrooms.text
    assert "3 bedroom apartment in NOPA" in only_three_bedrooms.text
    assert "2 bedroom apartment in NOPA" not in only_three_bedrooms.text


def test_two_bedroom_priority_filter_distinguishes_strong_and_secondary_areas(tmp_path: Path) -> None:
    settings = app_settings(tmp_path)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    repository = application.state.repository
    preferences = load_preferences(settings.preferences_path)
    strong = ListingCandidate(
        platform="Craigslist",
        source_id="strong-two-bed",
        title="2 bedroom apartment in NOPA",
        original_url="https://example.test/strong-two-bed",
        price=4000,
        neighborhood="NOPA",
        summary="Entire 2-bedroom apartment in a 4-unit building.",
    )
    secondary = ListingCandidate(
        platform="Craigslist",
        source_id="secondary-two-bed",
        title="2 bedroom apartment in Bernal Heights",
        original_url="https://example.test/secondary-two-bed",
        price=4000,
        neighborhood="Bernal Heights",
        summary="Entire 2-bedroom apartment in a 4-unit building.",
    )
    repository.upsert_listing(strong, score_listing(strong, preferences))
    repository.upsert_listing(secondary, score_listing(secondary, preferences))

    with TestClient(application) as client:
        all_targets = client.get("/?housing=two_bedroom")
        priority_only = client.get("/?housing=two_bedroom&area_priority=dream_strong")

    assert all_targets.status_code == 200
    assert "Area priority" in all_targets.text
    assert "Strong area" in all_targets.text
    assert "Secondary area" in all_targets.text
    assert "2 bedroom apartment in NOPA" in priority_only.text
    assert "2 bedroom apartment in Bernal Heights" not in priority_only.text
    assert "Dream and strong target areas only" in priority_only.text


def test_alert_setup_page_explains_local_connection(tmp_path: Path) -> None:
    settings = app_settings(tmp_path)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)

    with TestClient(application) as client:
        response = client.get("/alerts")

    assert response.status_code == 200
    assert already_works_headline(response.text) in response.text
    assert "nothing on this page is required" in response.text
    assert "Before you can connect Gmail" in response.text
    assert "Google OAuth Web client JSON" in response.text
    # The promise, not the sentence: whatever the wording, the page has to say
    # the Facebook login is never involved.
    assert "Facebook login is never used or stored" in response.text
    # The prepared Zillow searches this used to check are gone, and so is the
    # guide that displayed them: Zillow's own search page answers an ordinary
    # request, so it runs unattended and nobody is asked to save a search on
    # it. What remains is the promise that it is now one of the sources that
    # simply work.
    assert "zillow.com" not in response.text
    assert "Zillow" in already_works_headline(response.text) or ">Zillow<" in response.text
    # Facebook has its own block further down and is deliberately not offered
    # here as a second, email-shaped way to reach the same listings.
    assert "(3BR)" not in response.text


def test_alerts_explain_why_a_new_user_returns_to_the_deal(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    settings = Settings(
        data_dir=data_dir,
        preferences_path=data_dir / "config" / "preferences.yaml",
        database_path=data_dir / "housing.sqlite3",
        log_path=data_dir / "test.log",
    )
    application = create_app(settings=settings, sources=[], enable_scheduler=False)

    with TestClient(application) as client:
        response = client.get("/alerts", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/preferences?welcome=1&message=Finish+your+deal+first")


def test_the_alerts_page_no_longer_prepares_zillow_searches(tmp_path: Path) -> None:
    """This used to check that a saved Zillow search URL from the profile was
    rendered into Zillow's setup guide. Both are gone: Zillow's own search page
    answers an ordinary request, so it runs unattended and there is no guide to
    put a link in. The builders behind those links went with it."""
    settings = app_settings(tmp_path)
    settings.preferences_path.write_text(
        TEST_PREFERENCES
        + """
zillow_searches:
  - name: Exact Noe Valley room search
    url: https://www.zillow.com/noe-valley-san-francisco-ca/rentals/?saved=yes
""",
        encoding="utf-8",
    )
    application = create_app(settings=settings, sources=[], enable_scheduler=False)

    with TestClient(application) as client:
        page = client.get("/alerts").text

    assert "Exact Noe Valley room search" not in page
    assert "zillow.com" not in page


def test_apify_token_can_be_connected_from_alerts(tmp_path: Path) -> None:
    settings = app_settings(tmp_path)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)

    with scan_requests(application), TestClient(application) as client:
        saved = client.post(
            "/alerts/apify/token",
            data={"apify_token": "apify_api_abcdefghijklmnopqrstuvwxyz123456"},
            follow_redirects=False,
        )
        page = client.get("/alerts")

    assert saved.status_code == 303
    assert "Checking now" in page.text, (
        "saving the token starts the check itself, and the card has to say so"
    )
    assert 'name="apify_token"' in page.text
    assert "Replace token" in page.text, "and offers a way to change it"


def test_plain_language_deal_form_updates_profile_and_preserves_advanced_settings(tmp_path: Path) -> None:
    settings = app_settings(tmp_path)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)

    with TestClient(application) as client:
        response = client.post(
            "/preferences/profile",
            data={
                "minimum_score": 65,
                "min_monthly": 800,
                "sweet_spot_min": 1200,
                "ideal_monthly": 1500,
                "sweet_spot_max": 1800,
                "max_monthly": 2300,
                "ideal_neighborhoods": "Duboce Triangle, Potrero Hill",
                "preferred_neighborhoods": "Mission Dolores, Bernal Heights",
                "acceptable_neighborhoods": "NOPA, Lower Haight",
                "ideal_min_months": 3,
                "ideal_max_months": 8,
                "max_months": 12,
                "ideal_people": 3,
                "max_people": 5,
            },
            follow_redirects=False,
        )

    saved = load_preferences(settings.preferences_path)
    assert response.status_code == 303
    assert saved.minimum_score == 65
    assert saved.list_value("ideal_neighborhoods") == ["Duboce Triangle", "Potrero Hill"]
    assert saved.section("sources")["max_results_per_source"] == 120
    assert saved.section("two_bedroom") == {
        "enabled": True,
        "occupants": 2,
        "max_per_person": 2700,
        "max_building_units": 50,
    }
    assert saved.section("three_bedroom") == {
        "enabled": True,
        "occupants": 3,
        "max_per_person": 2500,
        "max_building_units": 50,
    }


# --------------------------------------------------------------------------
# an empty shortlist has to say why it is empty
# --------------------------------------------------------------------------


def narrow_profile() -> str:
    """One area, a high bar: a deal that collects plenty and shortlists none."""
    import yaml

    return yaml.safe_dump(
        {
            "profile_version": 1,
            "profile": {
                "state": "active",
                "enabled_paths": ["private_room"],
                "budgets": {"private_room": {"maximum_monthly": 1200}},
                "geography": {"anywhere_in_sf": False, "dream": ["Sea Cliff"]},
                "room_household": {"private_room_required": True},
            },
            "technical": {"minimum_score": 85},
        }
    )


def seed_rooms(repository: Repository, count: int = 6) -> None:
    for index in range(count):
        repository.upsert_listing(
            ListingCandidate(
                platform="Craigslist",
                source_id=f"x{index}",
                title=f"Private room {index}",
                original_url=f"https://sfbay.craigslist.org/roo/d/x/{index}.html",
                price=2600 + index,
                neighborhood="Tenderloin",
                listing_type="Room/share",
                summary="A private room in a shared home.",
            ),
            ScoreResult(70, ["fits"], "check", {}),
        )


def test_an_empty_shortlist_names_what_held_the_homes_back(tmp_path: Path) -> None:
    """An empty list with no explanation reads as a broken search. It almost
    never is: the deal is narrow, and the page should say which limit cost what.
    """
    settings = app_settings(tmp_path)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.preferences_path.write_text(narrow_profile(), encoding="utf-8")
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    seed_rooms(application.state.repository)

    with TestClient(application) as client:
        page = client.get("/?housing=room").text

    from sf_housing.models import CHECK_EXCLUSION_PHRASES

    assert "No homes made the shortlist yet" in page
    assert "Of 6 homes collected for this tab" in page
    known = set(CHECK_EXCLUSION_PHRASES.values()) | {"below your 85 match cut-off"}
    assert any(phrase in page for phrase in known), "the reason has to be one it can name"
    assert 'href="/preferences"' in page, "and offers the way out"
    # The archive rather than near matches: these homes were held back by a
    # hard limit, and a near match now means one that came close to the
    # cut-off. Sending somebody to a view that excludes them is worse than
    # not linking at all.
    assert "view=all" in page, "and the homes themselves are still reachable"


def test_the_reasons_are_counted_not_guessed(tmp_path: Path) -> None:
    settings = app_settings(tmp_path)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.preferences_path.write_text(narrow_profile(), encoding="utf-8")
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    repository = application.state.repository
    seed_rooms(repository, count=7)

    with TestClient(application) as client:
        client.get("/?housing=room")
        summary = repository.exclusion_summary(85, "room", ())

    assert summary, "seven collected homes have to be accounted for"
    assert sum(item["count"] for item in summary) == 7, "every held-back home is counted once"


def test_a_healthy_shortlist_pays_nothing_for_the_explanation(tmp_path: Path) -> None:
    """The breakdown is only computed when the list is thin, and never shown
    when there is a real shortlist to read."""
    settings = app_settings(tmp_path)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    repository = application.state.repository
    for index in range(6):
        add_listing(repository, "Craigslist", f"good{index}", f"Sunny room {index}", 1500, "NOPA")

    with TestClient(application) as client:
        page = client.get("/").text

    assert "collected for this tab" not in page


def test_nothing_collected_at_all_does_not_pretend_to_explain(tmp_path: Path) -> None:
    settings = app_settings(tmp_path)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.preferences_path.write_text(narrow_profile(), encoding="utf-8")
    application = create_app(settings=settings, sources=[], enable_scheduler=False)

    with TestClient(application) as client:
        page = client.get("/?housing=room")

    assert page.status_code == 200
    assert "collected for this tab" not in page.text


# --------------------------------------------------------------------------
# "there should be more listings than this"
# --------------------------------------------------------------------------


def test_a_short_shortlist_says_what_the_deal_held_back(tmp_path: Path) -> None:
    """A list far shorter than what was collected reads as a broken scraper.
    It almost never is, and the page had no way of saying which limit cost what.
    """
    settings = app_settings(tmp_path)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.preferences_path.write_text(narrow_profile(), encoding="utf-8")
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    repository = application.state.repository
    # One home that fits, and plenty that the deal holds back.
    repository.upsert_listing(
        ListingCandidate(
            platform="Craigslist", source_id="fits", title="Private room in Sea Cliff",
            original_url="https://sfbay.craigslist.org/roo/d/x/fits.html", price=1100,
            neighborhood="Sea Cliff", listing_type="Room/share",
            summary="A private room in a shared home, available now.",
        ),
        ScoreResult(92, ["fits"], "", {}),
    )
    seed_rooms(repository, count=9)

    with TestClient(application) as client:
        page = client.get("/?housing=room").text

    assert "listing-row" in page, "this shortlist is not empty"
    assert "were collected and held back" in page, "and still explains what is missing"
    # The count and the reason, not only the sentence around them: the note is
    # there to say which limit cost what.
    assert "9 more homes were collected and held back" in page
    assert "below your" in page and "match cut-off" in page
    assert 'href="/preferences"' in page


def test_a_shortlist_holding_everything_collected_says_nothing(tmp_path: Path) -> None:
    """No exclusions, no explanation. The note is context, not decoration."""
    settings = app_settings(tmp_path)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    for index in range(4):
        add_listing(application.state.repository, "Craigslist", f"g{index}", f"Room {index}", 1500, "NOPA")

    with TestClient(application) as client:
        page = client.get("/").text

    assert "were collected and held back" not in page


# --------------------------------------------------------------------------
# the Posted column
# --------------------------------------------------------------------------


def test_the_posted_column_shows_the_date_the_source_published(tmp_path: Path) -> None:
    """The column people scan for. It has to be the source's own date, not the
    day this app happened to notice the home."""
    import re

    settings = app_settings(tmp_path)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    repository = application.state.repository
    repository.upsert_listing(
        ListingCandidate(
            platform="Craigslist",
            source_id="dated",
            title="Sunny room in NOPA",
            original_url="https://sfbay.craigslist.org/roo/d/x/dated.html",
            price=1500,
            neighborhood="NOPA",
            listing_type="Room/share",
            summary="A private room in a shared home.",
            metadata={"listing_timestamp": "2026-08-28T14:11:54-07:00"},
        ),
        ScoreResult(88, ["fits"], "", {}),
    )

    with TestClient(application) as client:
        page = client.get("/?view=all&housing=room").text

    assert '<th scope="col" class="cell-found"' in page, "the column exists"
    cell = re.search(r'<td class="cell-found">(.*?)</td>', page, re.S).group(1)
    assert "Aug 28" in cell, cell
    assert "Found" not in cell, "a real posted date is not a found date"


def test_a_home_whose_source_never_states_a_date_says_so(tmp_path: Path) -> None:
    """SpareRoom publishes no dates at all. Showing the day we noticed it as if
    it were the posting date would be inventing a fact."""
    import re

    settings = app_settings(tmp_path)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    add_listing(application.state.repository, "SpareRoom", "undated", "Room in NOPA", 1500, "NOPA")

    with TestClient(application) as client:
        page = client.get("/?view=all&housing=room").text

    cell = re.search(r'<td class="cell-found">(.*?)</td>', page, re.S).group(1)
    assert "Found" in cell, "say it is a found date, not a posted one"
    assert "does not publish a date" in cell, "and why"


def test_the_posted_column_survives_a_narrow_window(tmp_path: Path) -> None:
    """It used to be one of the first two columns hidden below 1560px, which is
    most laptops, so the date people were looking for was never on screen."""
    import re

    css = (Path("sf_housing/static/style.css")).read_text(encoding="utf-8")
    start = css.index("@media (max-width: 1560px)")
    narrow = css[start : css.index("\n}", start)]

    assert "cell-found" not in narrow, "the Posted column must not hide at laptop width"
    # The Source column used to be the one hidden here, on the grounds that the
    # listing line already names the source. That reasoning held at every
    # width, so the column is gone and the line carries both the name and when
    # the source last showed the home.
    assert "cell-source" not in css, "the Source column is gone; nothing should style it"
    listing_line = (Path("sf_housing/templates/index.html")).read_text(encoding="utf-8")
    assert 'class="meta-source"' in listing_line and "meta-checked" in listing_line, (
        "the listing line has to carry the source and when it was last seen"
    )
    # And it has to be visible at every width. The meta line stands in for
    # columns that exist at full width, so most of it is hidden there -- but
    # the source has no column to stand in for any more, and hiding it left
    # the widest windows naming no source at all.
    hidden = [
        rule for rule in css.split("}")
        if "display: none" in rule and ".listing-compact-meta .meta-source" in rule
    ]
    assert not hidden, f"the source is hidden at some width: {hidden}"


def test_the_score_cell_no_longer_repeats_the_check_column(tmp_path: Path) -> None:
    import re

    settings = app_settings(tmp_path)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.preferences_path.write_text(narrow_profile(), encoding="utf-8")
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    seed_rooms(application.state.repository, count=3)

    with TestClient(application) as client:
        page = client.get("/?view=all&housing=room").text

    score_cell = re.search(r'<td class="cell-score">(.*?)</td>', page, re.S).group(1)
    assert "verification-label" not in score_cell
    assert "Check " not in score_cell
    assert "% evidence" in score_cell, "what the cell does keep"


# --------------------------------------------------------------------------
# a configuration value nobody can parse must not take the page down
# --------------------------------------------------------------------------


def test_a_null_budget_setting_does_not_500_the_dashboard(tmp_path: Path) -> None:
    """Readers wrote int(section.get(key, default)), where the default only
    applies when the key is absent. A key present and null reached int() and
    raised, which on the dashboard is a 500 on the page the app opens to."""
    import yaml

    from tests.conftest import TEST_PREFERENCES

    settings = app_settings(tmp_path)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    document = yaml.safe_load(TEST_PREFERENCES)
    document.setdefault("budget", {})["min_monthly"] = None
    document["budget"]["max_monthly"] = None
    settings.preferences_path.write_text(yaml.safe_dump(document), encoding="utf-8")
    application = create_app(settings=settings, sources=[], enable_scheduler=False)

    with TestClient(application) as client:
        for path in ("/", "/preferences", "/?housing=room", "/?view=near_matches"):
            assert client.get(path).status_code == 200, path


def test_an_unreadable_setting_falls_back_instead_of_raising() -> None:
    from sf_housing.preferences import setting_int

    assert setting_int(None, 800) == 800
    assert setting_int("", 800) == 800
    assert setting_int("nonsense", 800) == 800
    assert setting_int([], 800) == 800
    assert setting_int("1200", 800) == 1200
    assert setting_int(1200, 800) == 1200
    assert setting_int(1200.0, 800) == 1200


def test_the_nav_asks_for_an_action_not_a_noun(tmp_path: Path) -> None:
    """"Sources" reads as a status page, and people treated it as one. The tab
    exists so someone adds a source, so it says that."""
    settings = app_settings(tmp_path)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)

    with TestClient(application) as client:
        page = client.get("/").text

    assert '>Add sources</a>' in page
    assert '>Sources</a>' not in page, "the bare noun is gone from the nav"


def main_nav(page: str) -> list[str]:
    """The header tab labels, in the order the header puts them in."""
    import re

    start = page.index('<nav aria-label="Main navigation">')
    nav = page[start : page.index("</nav>", start)]
    return [text.strip() for text in re.findall(r">([^<>]+)</a>", nav)]


def test_the_nav_runs_in_the_order_somebody_uses_it(tmp_path: Path) -> None:
    """Look at what fits, keep some, adjust the deal that produced those
    results, and only then go looking for more places to read. Add sources sat
    ahead of Your deal, which put the rarest tab in front of the one people
    come back to."""
    settings = app_settings(tmp_path)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)

    with TestClient(application) as client:
        page = client.get("/").text

    assert main_nav(page) == ["Shortlist", "Saved", "Your deal", "Add sources", "Support"]
    # The theme control is a button, not a destination, so it comes after all
    # of them rather than sitting in the run of tabs.
    assert page.index("</nav>") < page.index("data-theme-toggle")


def test_the_logo_is_drawn_into_the_page_so_it_can_follow_the_theme(tmp_path: Path) -> None:
    """currentColor does not cross a document boundary. Served as an <img> the
    artwork keeps its own dark green, which is the one colour the dark theme
    cannot show it in -- so it is inline, and the accent it inherits is what
    makes the same drawing work on paper and on charcoal."""
    settings = app_settings(tmp_path)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)

    with TestClient(application) as client:
        page = client.get("/").text

    header = page[page.index('<header class="site-header">') : page.index("</header>")]
    assert '<svg class="brand-lockup"' in header, "the logo is not drawn into the page"
    assert "<img" not in header, "an image cannot take the colour of the page around it"

    style = stylesheet()
    assert "fill: currentColor" in block_body(style, ".brand-lockup, .brand-mark { ")
    assert "color: var(--accent)" in block_body(style, ".brand { ")


def test_a_narrow_window_keeps_the_bridge_when_the_wordmark_will_not_fit(
    tmp_path: Path,
) -> None:
    """The lockup is seven to one. Below 760px it would push the tabs off the
    row, so the bridge stands in -- and the link keeps its name either way,
    because the name is on the link and never on the artwork."""
    settings = app_settings(tmp_path)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)

    with TestClient(application) as client:
        page = client.get("/").text

    header = page[page.index('<header class="site-header">') : page.index("</header>")]
    assert '<svg class="brand-mark"' in header
    assert header.count('aria-hidden="true"') >= 2, "neither drawing should be read out"
    assert 'aria-label="SF Home Finder home"' in header, "the link carries the name"

    # The stylesheet has several 760px blocks; this is the one the swap is in.
    style = stylesheet()
    opener = "@media (max-width: 760px) {"
    narrow = block_body(style[style.rindex(opener, 0, style.index(".brand-lockup { display: none")):], opener)
    assert ".brand-lockup { display: none; }" in narrow
    assert ".brand-mark { display: block; }" in narrow


def test_the_tab_shows_the_logo_rather_than_a_blank_page_icon(tmp_path: Path) -> None:
    """A local app lives in a pinned tab for weeks. It had no icon at all."""
    settings = app_settings(tmp_path)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)

    with TestClient(application) as client:
        page = client.get("/").text
        assert client.get("/static/favicon.svg").status_code == 200
        assert client.get("/static/favicon.png").status_code == 200

    assert 'rel="icon"' in page and "favicon.svg" in page
    assert 'rel="alternate icon"' in page and "favicon.png" in page, "for a browser too old for SVG"

    import re

    icon = Path("sf_housing/static/favicon.svg").read_text(encoding="utf-8")
    drawing = re.sub(r"<!--.*?-->", "", icon, flags=re.S)
    # Painted outside the page, so it has no currentColor to inherit and has to
    # answer the browser's own setting itself.
    assert "currentColor" not in drawing, "a tab icon inherits nothing"
    assert "fill: #" in drawing, "so it has to name its own colour"
    assert "prefers-color-scheme: dark" in drawing, "dark green on a dark tab bar is invisible"
    assert "<script" not in icon and "javascript:" not in icon


def test_moving_between_pages_is_one_thing_changing(tmp_path: Path) -> None:
    """Following a link used to be a blank frame. The transition is a
    progressive enhancement -- no JavaScript, and a browser without it
    navigates exactly as before -- so what has to hold is the CSS: it is asked
    for, it is skipped for somebody who wants less motion, and the header and
    footer are lifted out of the page snapshot so only the body travels."""
    style = stylesheet()

    motion = block_body(style, "@media (prefers-reduced-motion: no-preference) {\n  @view-transition")
    assert "navigation: auto" in motion, motion
    # Cancelling it with an override underneath would still snapshot the page
    # for somebody who asked for no motion. It has to be the at-rule that is
    # never declared.
    declarations = style.count("@view-transition")
    assert declarations == 1, f"{declarations} view-transition at-rules; only the gated one belongs"

    for name in ("site-header", "site-footer"):
        assert f".site-{name.split('-')[1]} {{ view-transition-name: {name}; }}" in style
        # A name used twice aborts the whole transition, silently.
        assert style.count(f"view-transition-name: {name};") == 1, name


def test_every_page_animation_names_a_keyframe_that_exists() -> None:
    """A view transition whose animation names nothing renders as an instant
    swap and reports no error anywhere. Only reading both halves catches it."""
    import re

    style = stylesheet()
    named = set(re.findall(r"::view-transition-(?:old|new)\([^)]+\) \{ animation: ([a-z-]+)", style))
    assert named, "the page transition has no animations at all"
    for name in named:
        assert f"@keyframes {name} {{" in style, f"{name} is animated but never defined"


def test_the_page_it_opens_says_the_same_thing(tmp_path: Path) -> None:
    """A tab called one thing that opens a page called another is how people
    decide they are in the wrong place and leave."""
    settings = app_settings(tmp_path)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)

    with TestClient(application) as client:
        page = client.get("/alerts").text

    assert "<title>Add sources" in page
    assert '<p class="eyebrow">Add sources</p>' in page


# --------------------------------------------------------------------------
# something to read while the scan runs
# --------------------------------------------------------------------------


def scan_progress_script() -> str:
    return (Path(__file__).resolve().parents[1] / "sf_housing/static/scan-progress.js").read_text(
        encoding="utf-8"
    )


def rotating_notes() -> list[str]:
    """The message table as written, one entry per line."""
    script = scan_progress_script()
    block = script[script.index("const NOTES = [") : script.index("];", script.index("const NOTES = ["))]
    return [line.strip() for line in block.splitlines() if line.strip().startswith("(")]


def test_the_scan_panel_carries_a_line_that_changes_while_it_runs(tmp_path: Path) -> None:
    """A three-minute wait needs something to read. The bar moving says the
    scan is alive; it does not say what it is doing."""
    settings = app_settings(tmp_path)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)

    with TestClient(application) as client:
        response = client.post("/scan", follow_redirects=False)
        page = client.get(response.headers["location"]).text

    assert "data-scan-note" in page


def test_the_changing_line_is_hidden_from_screen_readers(tmp_path: Path) -> None:
    """It sits inside an aria-live region. Announced, it would interrupt with
    a new sentence every eight seconds for three minutes, while the real
    status -- the heading and the counts -- is already spoken."""
    settings = app_settings(tmp_path)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)

    with TestClient(application) as client:
        client.post("/scan", follow_redirects=False)
        page = client.get("/").text

    note = page[page.index("data-scan-note") - 60 : page.index("data-scan-note") + 60]
    assert 'aria-hidden="true"' in note, note


def test_there_are_enough_messages_to_last_a_whole_scan() -> None:
    """A scan runs about 176 seconds, which is twenty-two changes at eight
    seconds each. Too few and the reader watches the same three sentences
    cycle four times over."""
    assert len(rotating_notes()) >= 8


def test_each_message_says_something_the_scan_is_really_doing() -> None:
    """These describe the run in progress rather than filling space, so the
    numbers in them have to come from the live payload rather than being
    baked in when the page loaded."""
    notes = rotating_notes()
    live = [note for note in notes if "${p." in note]

    # One, not three. Three of these used to restate the counters in the line
    # directly above them -- the current source, the sources done, the
    # listings seen -- which is the same fact twice on one small panel. The
    # numbers belong there once; what is left here says what they cannot.
    assert len(live) >= 1, "no message reports anything about the actual scan"
    for note in notes:
        assert "TODO" not in note and "..." not in note


def test_the_line_changes_on_its_own_clock_not_on_every_poll() -> None:
    """The poll runs twice a second. Re-rendering the same sentence restarts
    its fade, which reads as a flicker rather than a change."""
    script = scan_progress_script()

    assert "Math.floor(elapsed / 8) % NOTES.length" in script
    assert "if (index === shownNote) return;" in script


def test_the_fade_is_dropped_for_a_reader_who_asked_for_no_motion() -> None:
    style = (Path(__file__).resolve().parents[1] / "sf_housing/static/style.css").read_text(
        encoding="utf-8"
    )
    fade = style[style.index(".scan-progress-note") :]

    assert "prefers-reduced-motion: no-preference" in fade[: fade.index("@keyframes")]


def test_the_line_holds_its_row_so_the_panel_does_not_jump() -> None:
    """The messages are different lengths. Without a reserved row a shorter
    one shortens the panel and everything below it moves."""
    style = (Path(__file__).resolve().parents[1] / "sf_housing/static/style.css").read_text(
        encoding="utf-8"
    )
    rule = style[style.index(".scan-progress-note {") : style.index("}", style.index(".scan-progress-note {"))]

    assert "min-height" in rule


def test_the_source_chips_tighten_rather_than_taking_a_third_line() -> None:
    """Seventeen chips are about 1,940px laid end to end, so two lines needs a
    little over 970px. The panel is a reassurance -- "these already work" --
    and a three-line block of names reads as a chore instead."""
    style = (Path(__file__).resolve().parents[1] / "sf_housing/static/style.css").read_text(
        encoding="utf-8"
    )

    assert "@media (max-width: 1300px)" in style
    narrow = style[style.index("@media (max-width: 1300px)") :]
    assert ".already-list .source-chip" in narrow[:400]
    assert "font-size: 0.72rem" in narrow[:400]


# --------------------------------------------------------------------------
# the source panel says the state once
# --------------------------------------------------------------------------


class HealthyDashboardSource:
    platform = "Movoto"
    mode = "automatic"
    search_url = "https://example.test/movoto"
    manual_reason = None


def panel_with_sources(tmp_path: Path, *, seen: int = 1957, stale: bool = False) -> str:
    """One source, checked once, rendered into the System status panel."""
    settings = app_settings(tmp_path)
    source = StaleDashboardSource() if stale else HealthyDashboardSource()
    application = create_app(settings=settings, sources=[source], enable_scheduler=False)
    repository = application.state.repository
    run_id = repository.begin_scan("manual")
    source_run = repository.begin_source_run(
        run_id, source.platform, source.search_url, source_key=type(source).__name__
    )
    repository.finish_source_run(source_run, "success", seen=seen)
    repository.finish_scan(run_id, "completed")
    if stale:
        old = (datetime.now(UTC) - FRESHNESS_WINDOW - timedelta(minutes=1)).isoformat()
        with repository.connection() as connection:
            connection.execute(
                "UPDATE source_runs SET finished_at = ? WHERE id = ?", (old, source_run)
            )
            connection.commit()
    with TestClient(application) as client:
        return client.get("/").text


def sources_panel(page: str) -> str:
    start = page.index('aria-labelledby="sources-title"')
    return page[start : page.index("</section>", start)]


def test_a_working_source_never_restates_its_own_name(tmp_path: Path) -> None:
    """The name is already the first thing in the row. "Movoto is current" one
    column away from "Movoto" is the same word twice, and it was long enough to
    wrap the badges into each other."""
    panel = sources_panel(panel_with_sources(tmp_path))

    assert "Movoto" in panel
    assert "Movoto is current" not in panel


def test_a_working_source_is_one_row_of_name_and_number(tmp_path: Path) -> None:
    """What is worth knowing about a source that worked is what it brought
    back, not a sentence saying it worked."""
    panel = sources_panel(panel_with_sources(tmp_path, seen=1957))

    assert "1,957" in panel, "the count carries the row"
    assert "This is a valid result, not a failure." not in panel
    assert "The latest source check completed at" not in panel


def test_a_source_with_nothing_to_show_says_so_in_words(tmp_path: Path) -> None:
    """The Honest State Rule: colour is never the only evidence."""
    panel = sources_panel(panel_with_sources(tmp_path, seen=0))

    assert "No matches" in panel


def test_only_a_source_needing_a_person_gets_the_recovery_sentence(
    tmp_path: Path,
) -> None:
    """Every row carrying its recovery text is twenty-two sentences telling
    somebody to do nothing."""
    healthy = sources_panel(panel_with_sources(tmp_path))

    assert "source-alerts" not in healthy, "nothing is wrong, so nothing is raised"
    assert "Nothing to do" not in healthy


def test_a_source_time_is_stated_in_the_zone_the_rest_of_the_page_uses() -> None:
    """The checks beside this are headed "Pacific time" and the two daily runs
    are Pacific. Source health answering in UTC left the reader converting.

    Asserted on the formatter, because only a paused source quotes a time in
    the panel and a stale one never reaches this sentence at all.
    """
    from sf_housing.freshness import _format_time

    # The same instant the panel used to render as "Sep 8 at 3:13 AM UTC".
    assert _format_time(datetime(2026, 9, 8, 3, 13, tzinfo=UTC)) == "Sep 7 at 8:13 PM"


def test_a_long_source_name_shortens_rather_than_taking_a_second_line() -> None:
    """A roster is only scannable while every row is the same height, and
    "Abacus (small buildings)" is wider than half of a narrow panel."""
    style = (Path(__file__).resolve().parents[1] / "sf_housing/static/style.css").read_text(
        encoding="utf-8"
    )
    start = style.index(".source-roster a {")
    rule = style[start : style.index("}", start)]

    assert "white-space: nowrap" in rule, rule
    assert "text-overflow: ellipsis" in rule, rule


def test_a_broken_source_says_what_went_wrong_not_only_what_to_do(
    tmp_path: Path,
) -> None:
    """The panel named the state and the retry time and never the cause, so
    the one question it reliably provoked was the one it did not answer."""
    settings = app_settings(tmp_path)
    source = StaleDashboardSource()
    application = create_app(settings=settings, sources=[source], enable_scheduler=False)
    repository = application.state.repository
    run_id = repository.begin_scan("manual")
    for _ in range(3):
        run = repository.begin_source_run(
            run_id, source.platform, source.search_url, source_key="StaleDashboardSource"
        )
        repository.finish_source_run(
            run, "error", message="SourceError: Craigslist turned away an unattended request (HTTP 403)."
        )
    repository.finish_scan(run_id, "completed_with_errors")

    with TestClient(application) as client:
        alerts = sources_panel(client.get("/").text)

    # Dropping the platform from the front leaves the next word starting the
    # sentence, so it is capitalised.
    assert "Turned away an unattended request (HTTP 403)." in alerts
    # The row is headed "Craigslist" already.
    assert "Craigslist turned away" not in alerts


# --------------------------------------------------------------------------
# the views a person actually moves between
# --------------------------------------------------------------------------


def view_tabs(page: str) -> list[str]:
    """The tab labels, in the order the page puts them in."""
    import re

    start = page.index('class="view-tabs"')
    nav = page[start : page.index("</nav>", start)]
    return [text.strip() for text in re.findall(r">([^<>]+)</a>", nav)]


def dashboard(tmp_path: Path) -> str:
    application = create_app(settings=app_settings(tmp_path), sources=[], enable_scheduler=False)
    with TestClient(application) as client:
        return client.get("/").text


def test_the_two_views_a_person_moves_between_come_first(tmp_path: Path) -> None:
    """What fits, and what nearly did. Saved, Passed and Archive are history,
    and history was sitting between the two live questions."""
    tabs = view_tabs(dashboard(tmp_path))

    assert tabs[:2] == ["Shortlist", "Near matches"]
    assert set(tabs[2:]) == {"Saved", "Passed", "Archive"}


def test_those_two_read_as_a_pair_and_the_rest_recede(tmp_path: Path) -> None:
    """Subtly, not loudly: the three behind them are still one click away."""
    page = dashboard(tmp_path)
    style = stylesheet()

    assert page.count('class="lead-view') == 2, "exactly the two live views are marked"
    lead = block_body(style, ".view-tabs a.lead-view {")
    base = block_body(style, ".view-tabs a {")
    assert "var(--ink)" in lead and "font-weight" in lead, lead
    assert "var(--muted)" in base, "the history tabs have to stay quieter"


def test_a_tab_is_laid_out_as_a_box_so_it_cannot_grow_into_the_line_above() -> None:
    """An anchor is inline by default, so min-height and vertical padding did
    nothing to its line box and the pills overlapped the sentence above."""
    base = block_body(stylesheet(), ".view-tabs a {")

    assert "display: inline-flex" in base, base
    assert "min-height" in base


# --------------------------------------------------------------------------
# the shortlist, as a spreadsheet
# --------------------------------------------------------------------------


def seeded_app(tmp_path: Path, rows):
    """An app holding real homes, so the export has something to disagree on."""
    from sf_housing.models import ListingCandidate, ScoreResult

    settings = app_settings(tmp_path)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    repository = application.state.repository
    for index, (price, neighborhood, title) in enumerate(rows):
        repository.upsert_listing(
            ListingCandidate(
                platform="Craigslist",
                source_id=f"csv{index}",
                title=title,
                original_url=f"https://sfbay.craigslist.org/roo/d/csv/{index}.html",
                price=price,
                neighborhood=neighborhood,
                listing_type="Room/share",
            ),
            ScoreResult(90, ["fits"], "check", {}),
        )
    return application


def csv_rows(text: str) -> list[list[str]]:
    import csv as _csv
    import io as _io

    return list(_csv.reader(_io.StringIO(text)))


def csv_column(text: str, heading: str) -> list[str]:
    """Every value under one heading, found by the heading rather than by
    counting columns, so adding one does not break tests about quoting."""
    import csv as _csv
    import io as _io

    return [row[heading] for row in _csv.DictReader(_io.StringIO(text))]


def test_the_file_holds_the_homes_the_page_was_showing(tmp_path: Path) -> None:
    """The whole promise. Two routes each doing their own filtering would drift
    apart while both went on returning plausible homes, which is the one
    failure nobody can see by reading either of them."""
    import re

    application = seeded_app(
        tmp_path,
        [(2100, "Bernal Heights", "Room one"), (2200, "Mission District", "Room two")],
    )
    query = "?housing=room&view=active&sort=price&neighborhood=Bernal+Heights"
    with TestClient(application) as client:
        page = client.get("/" + query).text
        export = client.get("/listings.csv" + query)

    on_page = len(re.findall(r'<tr class="listing-row', page))
    in_file = len(csv_rows(export.text)) - 1  # less the header

    assert in_file == on_page, f"page showed {on_page}, file holds {in_file}"
    assert on_page == 1, "the neighborhood filter did not narrow the page"


def test_an_empty_shortlist_still_downloads_a_usable_file(tmp_path: Path) -> None:
    """A spreadsheet that opens to a header and no rows is a clear answer. A
    404, or an empty file, reads as the feature being broken."""
    application = seeded_app(tmp_path, [])
    with TestClient(application) as client:
        export = client.get("/listings.csv?housing=room&view=saved")

    rows = csv_rows(export.text)
    assert export.status_code == 200
    assert len(rows) == 1, rows
    assert rows[0][0] == "Score"


def test_a_title_with_a_comma_survives_the_round_trip(tmp_path: Path) -> None:
    """Craigslist titles are full of them."""
    application = seeded_app(tmp_path, [(2100, "Bernal Heights", 'Sunny room, quiet st, "big"')])
    with TestClient(application) as client:
        export = client.get("/listings.csv?housing=room&view=active")

    addresses = csv_column(export.text, "Address")
    assert addresses == ['Sunny room, quiet st, "big"'], addresses


def test_nothing_a_source_wrote_can_become_a_spreadsheet_formula() -> None:
    """A title beginning "=" is evaluated on open by Excel, Numbers and Sheets
    alike. This app did not write these strings and cannot vouch for them."""
    from sf_housing.app import listings_csv

    text = listings_csv([{"title": "=1+1", "note": "@SUM(A1)", "neighborhood": "-Mission"}])

    assert csv_column(text, "Address") == ["'=1+1"], text
    assert csv_column(text, "Note") == ["'@SUM(A1)"], text
    assert csv_column(text, "Neighborhood") == ["'-Mission"], text


def test_a_price_stays_a_number_a_spreadsheet_can_sort(tmp_path: Path) -> None:
    """Exported as "$3,045" every reader treats the column as text, and
    sorting by price silently stops working."""
    application = seeded_app(tmp_path, [(3045, "Bernal Heights", "Room one")])
    with TestClient(application) as client:
        export = client.get("/listings.csv?housing=room&view=active")

    price = csv_column(export.text, "Price")
    assert price == ["3045"], price


def test_the_file_arrives_as_a_download_named_for_the_day(tmp_path: Path) -> None:
    """These get saved and compared, so the name has to say which day it is."""
    application = seeded_app(tmp_path, [(2100, "Bernal Heights", "Room one")])
    with TestClient(application) as client:
        export = client.get("/listings.csv?housing=room&view=active")

    disposition = export.headers["content-disposition"]
    assert disposition.startswith('attachment; filename="sf-homes-')
    assert export.headers["content-type"].startswith("text/csv")


def test_the_export_button_submits_the_filters_rather_than_a_link(tmp_path: Path) -> None:
    """A hand-built link is a second copy of the filter vocabulary, and the day
    a filter is added the export stops honouring it while still returning
    plausible homes. Submitting the form the filters already live in cannot
    drift, because the browser sends whatever they currently say."""
    application = seeded_app(tmp_path, [(2100, "Bernal Heights", "Room one")])
    with TestClient(application) as client:
        page = client.get("/?housing=room&view=active").text

    start = page.index('class="filters"')
    form = page[start : page.index("</form>", start)]
    assert 'formaction="/listings.csv"' in form, "the export is not inside the filter form"
    assert 'href="/listings.csv' not in page, "the export is a hand-built link"


def test_the_export_carries_the_score_floor_the_shortlist_is_defined_by(
    tmp_path: Path,
) -> None:
    """The active view is what clears the deal's minimum score. An export that
    ignored it would hand back homes the page is deliberately holding out."""
    import inspect

    from sf_housing.app import select_listings

    body = inspect.getsource(select_listings)
    assert "minimum_score=preferences.minimum_score" in body


def test_the_bar_says_how_much_longer_from_what_the_sources_really_take() -> None:
    """The weights already are the seconds each source took on its own recent
    runs, so what is left of them is the answer. A second estimate kept
    alongside the first is a second thing to drift."""
    import inspect

    from sf_housing.scanner import Scanner

    body = inspect.getsource(Scanner.progress.fget)

    assert "seconds_remaining" in body
    assert "weight_total - weight_done - running" in body


def test_no_estimate_is_offered_before_there_is_anything_to_base_one_on(
    tmp_path: Path,
) -> None:
    """A number invented for the very first scan, when no source has ever been
    timed, is worse than saying nothing."""
    from sf_housing.database import Repository
    from sf_housing.preferences import parse_preferences
    from sf_housing.scanner import Scanner
    from tests.conftest import TEST_PREFERENCES

    repository = Repository(tmp_path / "housing.sqlite3")
    repository.initialize()
    scanner = Scanner(repository, lambda: parse_preferences(TEST_PREFERENCES), [])

    assert scanner.progress["seconds_remaining"] is None


def test_a_finished_scan_stops_predicting(tmp_path: Path) -> None:
    """"About a minute left" beside a finished scan is the bar lying."""
    from sf_housing.database import Repository
    from sf_housing.preferences import parse_preferences
    from sf_housing.scanner import Scanner
    from tests.conftest import TEST_PREFERENCES

    repository = Repository(tmp_path / "housing.sqlite3")
    repository.initialize()
    scanner = Scanner(repository, lambda: parse_preferences(TEST_PREFERENCES), [])
    scanner.run_scan("manual")

    assert scanner.progress["running"] is False
    assert scanner.progress["seconds_remaining"] is None


def test_the_estimate_is_rounded_rather_than_counted_to_the_second() -> None:
    """A scan is not predictable to the second. "63s left" counting unevenly
    reads as broken where "about a minute left" reads as honest."""
    script = scan_progress_script()

    from sf_housing.scanner import _remaining_label

    assert _remaining_label(None, running=False) == ""
    assert _remaining_label(125, running=False) == "", "a finished scan predicts nothing"
    assert _remaining_label(5, running=True) == "finishing up"
    assert _remaining_label(30, running=True) == "about half a minute left"
    assert _remaining_label(61, running=True) == "about 1 minute left"
    assert _remaining_label(125, running=True) == "about 2 minutes left"
    # A first scan has no history to estimate from, and used to get its own
    # separate line for that -- which then sat beside the real estimate on
    # every scan after it.
    assert _remaining_label(None, running=True) == "a couple of minutes, probably"
    # Worded once, in Python. The first paint is server-rendered and every
    # update after it is not, so a second copy of this phrasing in the script
    # would disagree for the half second before the first poll -- and then
    # quietly forever.
    assert "remaining_label" in script
    # Scoped to the function that renders it: the word "minute" appears in a
    # comment elsewhere, and a test that cannot tell those apart fails for
    # reasons nobody can act on.
    start = script.index("const remainingLabel")
    renderer = script[start : script.index("};", start)]
    assert "minute" not in renderer, "the script is wording the estimate itself"
    assert "progress.remaining_label" in renderer


def test_the_first_paint_says_the_same_thing_as_every_update_after_it() -> None:
    """The page renders the line once on the server and the script rewrites it
    twice a second. If only one of them knows about the estimate, the number
    appears half a second late on every scan."""
    from pathlib import Path as _Path

    page = (_Path(__file__).resolve().parents[1] / "sf_housing/templates/index.html").read_text(
        encoding="utf-8"
    )
    line = page[page.index("data-scan-detail") : page.index("</span>", page.index("data-scan-detail"))]

    assert "remaining_label" in line, line


def test_a_running_scan_says_how_many_seconds_are_left(tmp_path: Path) -> None:
    """The estimate has to be a number while a scan is running, not merely
    absent at the right moments. Read from the weights directly, because a
    real scan of real sources cannot be held at a known fraction."""
    from sf_housing.database import Repository
    from sf_housing.preferences import parse_preferences
    from sf_housing.scanner import Scanner
    from tests.conftest import TEST_PREFERENCES

    repository = Repository(tmp_path / "housing.sqlite3")
    repository.initialize()
    scanner = Scanner(repository, lambda: parse_preferences(TEST_PREFERENCES), [])
    with scanner._progress_lock:
        scanner._progress_state.update(
            {
                "running": True,
                "status": "running",
                "weight_total": 100.0,
                "weight_done": 40.0,
                "weight_current": 0.0,
                "current_started_monotonic": None,
            }
        )

    # Sixty of the hundred seconds these sources usually take are still ahead.
    assert scanner.progress["seconds_remaining"] == 60
    assert scanner.progress["percent"] == 40


def test_the_panel_says_how_much_longer_in_exactly_one_place(tmp_path: Path) -> None:
    """It carried two: a live estimate from what the sources really take, and
    beside it a static "usually about 152s" that was there first. One of them
    was always stale, and on a small panel that reads as the app not knowing."""
    # With no sources the scan finishes before the page can be fetched, so the
    # panel this test is about is legitimately absent and the run fails on
    # timing rather than on the thing it checks. Hold the scan open instead.
    started, release = threading.Event(), threading.Event()
    application = create_app(
        settings=app_settings(tmp_path),
        sources=[WaitingDashboardSource(started, release)],
        enable_scheduler=False,
    )
    with TestClient(application) as client:
        client.post("/scan", follow_redirects=False)
        assert started.wait(timeout=2)
        page = client.get("/").text
        release.set()

    assert "data-scan-estimate" not in page, "the second estimate is back"
    assert "Usually about" not in page
    assert page.count("data-scan-detail") == 1


def test_no_rotating_message_repeats_a_number_shown_beside_it() -> None:
    """The panel is small. A sentence saying "4 sources done" under a line
    already reading "4 of 23 sources" is the same fact twice, and the reader
    has to check whether it is really the same."""
    notes = rotating_notes()
    joined = " ".join(notes)

    assert "sources_completed" not in joined, "a message restates the source counter"
    assert "sources_total" not in joined
    assert "current_source" not in joined, "the heading already names it"


def test_a_facebook_test_lands_back_on_the_card_that_started_it(tmp_path: Path) -> None:
    """The result is written to the connector card, so that is where the
    person has to end up. Sending them to the dashboard instead is what made
    a finished test look like a test that never reported."""
    settings = app_settings(tmp_path)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)

    with scan_requests(application), TestClient(application) as client:
        client.post(
            "/alerts/apify/token",
            data={"apify_token": "apify_api_abcdefghijklmnopqrstuvwxyz123456"},
            follow_redirects=False,
        )
        started = client.post("/alerts/apify/test", follow_redirects=False)

    assert started.status_code == 303
    destination = started.headers["location"]
    assert destination.startswith("/alerts"), "not the dashboard"
    assert "open=facebook" in destination, "with the card already open"


def test_connector_status_json_says_whether_a_check_has_settled(tmp_path: Path) -> None:
    """The page polls this to replace Testing with what actually happened."""
    settings = app_settings(tmp_path)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)

    with TestClient(application) as client:
        repository = application.state.repository
        repository.set_connector_state(
            "apify", "checking", message="Running one bounded Facebook check.", attempted=True
        )
        mid = client.get("/alerts/connectors.json").json()["apify"]
        assert mid["settled"] is False
        assert "Checking now" in mid["next_step"]

        repository.set_connector_state(
            "apify",
            "working",
            message="Facebook Marketplace returned 7 listing(s).",
            observed_items=7,
            attempted=True,
            succeeded=True,
        )
        done = client.get("/alerts/connectors.json").json()["apify"]

    assert done["settled"] is True
    assert done["label"] == "Working"
    assert done["observed_items"] == 7
    assert "returned 7" in done["message"], "what the check found"
    assert "Nothing more to do" in done["next_step"], "and where that leaves you"


def test_a_group_with_no_token_is_told_what_is_missing(tmp_path: Path) -> None:
    settings = app_settings(tmp_path)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)

    with TestClient(application) as client:
        client.post(
            "/alerts/facebook-groups",
            data={"group_url": "https://www.facebook.com/groups/1105487206638421/"},
            follow_redirects=False,
        )
        page = client.get("/alerts?open=facebook&open_groups=1")

    assert "Connect Apify above before these groups can be checked." in page.text


def test_saving_a_token_starts_the_check_without_a_second_click(tmp_path: Path) -> None:
    """Nobody wants a saved token; they want Facebook homes. The token was a
    first step that silently waited for a second one nobody knew to take."""
    settings = app_settings(tmp_path)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)

    with scan_requests(application), TestClient(application) as client:
        saved = client.post(
            "/alerts/apify/token",
            data={"apify_token": "apify_api_abcdefghijklmnopqrstuvwxyz123456"},
            follow_redirects=False,
        )
        state = application.state.repository.connector_state("apify")

    assert saved.status_code == 303
    assert "open=facebook" in saved.headers["location"], "and lands on the open card"
    assert state is not None and state.state == "checking", (
        "the check is already running, not merely offered"
    )


def test_a_check_is_marked_before_it_starts_not_after(tmp_path: Path) -> None:
    """The scan runs in a daemon thread and can finish first. Marking the
    connector after start_scan therefore overwrites the result the scan just
    wrote, stranding the card on Testing until the next scheduled run -- the
    exact failure this card was changed to prevent. Assert the ordering
    itself rather than racing it."""
    settings = app_settings(tmp_path)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)

    with scan_requests(application) as asked, TestClient(application) as client:
        client.post(
            "/alerts/apify/token",
            data={"apify_token": "apify_api_abcdefghijklmnopqrstuvwxyz123456"},
            follow_redirects=False,
        )

    assert [state for state, _ in asked] == ["checking"], (
        "the connector must already read checking when the scan is handed off, "
        f"or the scan's own result can be overwritten by it; saw {asked}"
    )


def test_a_refused_start_does_not_leave_a_check_claiming_to_run(tmp_path: Path) -> None:
    settings = app_settings(tmp_path)
    started, release = threading.Event(), threading.Event()
    application = create_app(
        settings=settings,
        sources=[WaitingDashboardSource(started, release)],
        enable_scheduler=False,
    )

    with TestClient(application) as client:
        client.post("/scan", follow_redirects=False)
        assert started.wait(timeout=2)
        # A scan is already running, so this one cannot start.
        client.post(
            "/alerts/apify/token",
            data={"apify_token": "apify_api_abcdefghijklmnopqrstuvwxyz123456"},
            follow_redirects=False,
        )
        state = application.state.repository.connector_state("apify")
        release.set()

    assert state is not None and state.state == "configured_unverified", (
        "a check that never started must not report itself as running"
    )


def test_an_abandoned_check_stops_claiming_to_be_running(tmp_path: Path) -> None:
    """A quit or a crash mid-scan never writes the result, so the checking row
    it leaves behind would read Testing for as long as the row survives."""
    from sf_housing.connectors import ConnectorStatus, resolve_stalled_check

    now = datetime.now(UTC)
    fresh = ConnectorStatus(
        key="apify", state="checking",
        last_attempt_at=(now - timedelta(minutes=2)).isoformat(),
    )
    abandoned = ConnectorStatus(
        key="apify", state="checking",
        last_attempt_at=(now - timedelta(hours=3)).isoformat(),
    )

    assert resolve_stalled_check(fresh, now=now).state == "checking", "still plausible"
    settled = resolve_stalled_check(abandoned, now=now)
    assert settled.state == "degraded", "no longer plausible"
    assert "interrupted" in settled.message


def test_the_facebook_check_scans_facebook_and_not_the_whole_board(tmp_path: Path) -> None:
    """Every scan shares one time budget. Running all eighteen sources to test
    one of them regularly cut Facebook for time, so the card reported "Not
    checked this time" for the source the button exists to check."""
    settings = app_settings(tmp_path)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)

    with scan_requests(application) as asked, TestClient(application) as client:
        client.post(
            "/alerts/apify/token",
            data={"apify_token": "apify_api_abcdefghijklmnopqrstuvwxyz123456"},
            follow_redirects=False,
        )
        client.post("/alerts/apify/test", follow_redirects=False)

    assert asked, "the check has to actually start a scan"
    for _, platforms in asked:
        assert platforms == ["Facebook Marketplace"], (
            f"a bounded Facebook check must ask for Facebook alone; asked for {platforms}"
        )


def test_a_second_press_does_not_overwrite_a_check_already_running(tmp_path: Path) -> None:
    """Pressing twice used to write "a scan was already running" over a check
    that was genuinely in flight, reporting a dead end for work about to
    succeed -- and hiding the answer the first press was already fetching."""
    settings = app_settings(tmp_path)
    started, release = threading.Event(), threading.Event()
    application = create_app(
        settings=settings,
        sources=[WaitingDashboardSource(started, release)],
        enable_scheduler=False,
    )
    repository = application.state.repository

    with TestClient(application) as client:
        with scan_requests(application):
            client.post(
                "/alerts/apify/token",
                data={"apify_token": "apify_api_abcdefghijklmnopqrstuvwxyz123456"},
                follow_redirects=False,
            )
        # A check of our own is in flight...
        repository.set_connector_state(
            "apify", "checking", message="Running one bounded Facebook check.", attempted=True
        )
        client.post("/scan", follow_redirects=False)
        assert started.wait(timeout=2)
        # ...and an impatient second press must leave it alone.
        client.post("/alerts/apify/test", follow_redirects=False)
        state = repository.connector_state("apify")
        release.set()

    assert state is not None and state.state == "checking", (
        "a running check must survive an impatient second press"
    )
    assert "Running one bounded Facebook check." in state.message


def test_a_source_error_reaches_the_card_as_a_sentence(tmp_path: Path) -> None:
    """"SourceError: Apify rejected the API token" reads like a crash report.
    The sentence was already written for the person; the class name in front
    of it was for the log."""
    from sf_housing.apify import ApifyTokenStore
    from sf_housing.sources import SourceError

    settings = app_settings(tmp_path)
    # Without a token the app reconciles this connector back to "not
    # configured" and the failure message never survives to be read.
    ApifyTokenStore(settings.data_dir / "apify-token.txt").save(
        "apify_api_abcdefghijklmnopqrstuvwxyz123456"
    )

    class RefusingSource:
        platform = "Facebook Marketplace"
        mode = "automatic"
        search_url = "https://example.test/facebook"
        manual_reason = None
        detail_budget = 0
        connector_key = "apify"

        def search(self, client, preferences):
            raise SourceError("Apify rejected the API token; reconnect it from Alerts.")

        def enrich(self, client, listing):
            return listing

    application = create_app(
        settings=settings, sources=[RefusingSource()], enable_scheduler=False
    )
    with TestClient(application) as client:
        client.post("/scan", follow_redirects=False)
        # The seeded state is already settled, so wait for the scan's own
        # attempt rather than for "not checking", which is true immediately.
        for _ in range(200):
            state = application.state.repository.connector_state("apify")
            if state is not None and state.last_attempt_at is not None:
                break
            time.sleep(0.05)

    assert state is not None and state.last_attempt_at is not None, "the scan ran"
    assert state.message == "Apify rejected the API token; reconnect it from Alerts."
    assert "SourceError" not in state.message


def test_the_held_back_row_offers_the_way_to_the_near_matches(tmp_path: Path) -> None:
    """The row that counts what was held back is where the way to the nearest
    of them belongs. The link has to be the one the tab itself uses -- a
    hand-written view name renders a page with no homes on it."""
    import pathlib as _p

    page = _p.Path("sf_housing/templates/index.html").read_text(encoding="utf-8")
    tab = 'view=near_matches&amp;sort=closeness'

    assert 'class="near-pill"' in page
    pill = page[page.index('class="near-pill"'):]
    pill = pill[: pill.index("</a>")]
    assert tab in pill, f"the pill must use the tab's own URL; got {pill}"
    assert "Check near matches" in pill


def test_near_matches_ends_with_two_ways_on_rather_than_a_dead_end(tmp_path: Path) -> None:
    """The shortlist's footer sends people to near matches; near matches has to
    send them somewhere in turn, or the last list on the board simply stops."""
    import pathlib as _p

    page = _p.Path("sf_housing/templates/index.html").read_text(encoding="utf-8")
    footer = page[page.index('class="near-footer"') :]
    footer = footer[: footer.index("</div>\n  {% endif %}")]

    assert "Still couldn&rsquo;t find what you were looking for?" in footer
    assert 'href="/?housing=other&amp;view=active&amp;sort=score"' in footer
    assert ">Check other homes</a>" in footer
    assert 'href="/preferences"' in footer
    assert ">Update your deal</a>" in footer
    # Only where there is a list to reach the end of, and only on that tab.
    opening = page[: page.index('class="near-footer"')]
    assert opening.rstrip().endswith("{% if listings and view == 'near_matches' %}") is False
    assert "{% if listings and view == 'near_matches' %}" in opening
    # The Other homes tab is conditional, so the link to it has to be too.
    assert "{% if show_other_tab %}" in footer

    style = stylesheet()
    assert ".near-footer {" in style
    assert ".near-footer-actions {" in style


def test_starring_flips_in_place_instead_of_rebuilding_the_board(tmp_path: Path) -> None:
    """A star used to cost a redirect and a full re-render of a list of
    hundreds -- about three seconds where nothing said the click had landed.

    Starring does not change which homes belong on the page, so the button can
    flip and the post can happen behind it. The form is still a real form for
    anyone without JavaScript.
    """
    application = create_app(settings=app_settings(tmp_path), sources=[], enable_scheduler=False)
    repository = application.state.repository
    add_listing(repository, "Craigslist", "starred", "Mission room", 1500, "Mission")
    listing_id = repository.query_listings(0, view="all")[0]["id"]

    with TestClient(application) as client:
        as_json = client.post(
            f"/listings/{listing_id}/status",
            data={"status": "saved", "return_to": "/"},
            headers={"Accept": "application/json"},
        )
        as_form = client.post(
            f"/listings/{listing_id}/status",
            data={"status": "active", "return_to": "/"},
            follow_redirects=False,
        )

    assert as_json.status_code == 200
    assert as_json.json() == {"status": "saved"}
    # The plain form post still redirects, so the page works without scripts.
    assert as_form.status_code == 303

    import pathlib as _p

    board = _p.Path("sf_housing/templates/index.html").read_text(encoding="utf-8")
    card = _p.Path("sf_housing/templates/_listing.html").read_text(encoding="utf-8")
    script = _p.Path("sf_housing/static/listing-actions.js").read_text(encoding="utf-8")

    assert 'action="/listings/{{ listing.id }}/status" data-star-form' in board
    # The starred list is the one place a star does change the page: un-starring
    # removes the card, which only a reload can honestly show.
    assert "star_in_place=false" in board
    assert '{% if star_in_place %} data-star-form{% endif %}' in card
    assert 'form.matches("[data-star-form]")' in script
    assert "const payload = new FormData(form);" in script, (
        "the hidden field is flipped below, so the body has to be read first"
    )
    assert 'headers: { Accept: "application/json" }' in script


def test_a_starred_control_does_not_vanish_into_the_row_it_marks() -> None:
    """A starred row is tinted with --accent-soft. The button that says so was
    --accent-soft too, so the one control on the page that mattered turned
    invisible exactly when it turned on."""
    style = stylesheet()

    row = block_body(style, ".listing-table tbody tr.saved-listing td { ")
    button = block_body(style, ".save-button.saved { ")
    assert "var(--accent-soft)" in row
    assert "var(--accent-soft)" not in button, button
    assert "background: var(--accent-hover)" in button
    assert "color: var(--surface)" in button


def test_the_starred_cards_are_separated_and_not_a_column_of_one_block() -> None:
    """Cards flush against the panel and each other read as one slab. They
    also ran far taller than the facts on them needed."""
    style = stylesheet()

    listing = block_body(style, ".saved-list { ")
    assert "padding: 14px" in listing, "the cards need room inside the panel"
    assert "gap: 14px" in listing
    assert "background: var(--band)" in listing, "space between cards has to read as space"

    card = block_body(style, ".listing-card { ")
    assert "padding: 15px 18px" in card
    assert "box-shadow: var(--shadow-low)" in card


def test_passing_leaves_the_list_in_place_where_that_is_honest(tmp_path: Path) -> None:
    """Pass posted and took a redirect, so it cost a full board render.

    Measured in-process: 951ms for the round trip, of which the write is about
    20ms and the rest is drawing a list of hundreds again. A passed home leaves
    the list being read and changes exactly one other thing on the page -- the
    count in the heading, which drops by one -- so the row can go and the post
    can happen behind it.

    Only where that is the whole truth. On a view of more than one page a home
    from the next page moves up into the gap and only the server knows which,
    and on the Archive a passed home does not leave at all. There the form
    stays an ordinary form.
    """
    application = create_app(settings=app_settings(tmp_path), sources=[], enable_scheduler=False)
    repository = application.state.repository
    add_listing(repository, "Craigslist", "passing", "Mission room", 1500, "Mission")
    listing_id = repository.query_listings(0, view="all")[0]["id"]

    with TestClient(application) as client:
        board = client.get("/?housing=room&view=active&sort=score").text
        archive = client.get("/?view=all&sort=score").text
        as_json = client.post(
            f"/listings/{listing_id}/status",
            data={"status": "dismissed", "return_to": "/"},
            headers={"Accept": "application/json"},
        )
        as_form = client.post(
            f"/listings/{listing_id}/status",
            data={"status": "active", "return_to": "/"},
            follow_redirects=False,
        )

    assert as_json.status_code == 200 and as_json.json() == {"status": "dismissed"}
    # Still a real form for anyone without scripts, and for the plain path.
    assert as_form.status_code == 303

    assert "data-pass-form" in board, "the shortlist is one page here"
    assert "data-pass-form" not in archive, "a passed home does not leave the Archive"
    # The heading carries the number and the noun, because the browser has to
    # write the same sentence the server would.
    assert 'data-result-count="1"' in board
    assert 'data-result-noun="room"' in board

    import pathlib as _p

    script = _p.Path("sf_housing/static/listing-actions.js").read_text(encoding="utf-8")
    assert 'form.matches("[data-pass-form]")' in script
    assert "const payload = new FormData(form);" in script, (
        "the body must be read before the row leaves the document"
    )
    assert "anchor.replaceWith(row)" in script, "a refused pass has to put the home back"
    template = _p.Path("sf_housing/templates/index.html").read_text(encoding="utf-8")
    assert "view in ['active', 'near_matches'] and pages == 1" in template
