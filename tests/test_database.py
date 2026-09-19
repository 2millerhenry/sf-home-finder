from __future__ import annotations

from sf_housing.database import Repository
from sf_housing.models import ListingCandidate, ScoreResult


def result(score: int = 75) -> ScoreResult:
    return ScoreResult(score, ["Good price"], "Unknown: lease terms.", {"price": {"known": True}})


def test_storage_deduplicates_repeated_urls_and_preserves_review_state(repository: Repository) -> None:
    first = ListingCandidate(
        platform="SpareRoom",
        source_id="100",
        title="Private room",
        original_url="https://example.test/room/100?listing_click=1",
        price=1500,
        neighborhood="Mission",
    )
    listing_id, created = repository.upsert_listing(first, result(), "2026-07-19T12:00:00+00:00")
    assert created is True
    assert repository.set_listing_status(listing_id, "saved")
    assert repository.set_listing_note(listing_id, "Contact tomorrow")

    repeated = ListingCandidate(
        platform="SpareRoom",
        source_id="changed-id",
        title="Private room — updated",
        original_url="https://example.test/room/100?utm_source=email",
        price=1450,
        neighborhood="Mission",
    )
    repeated_id, created_again = repository.upsert_listing(
        repeated, result(82), "2026-07-20T12:00:00+00:00"
    )

    stored = repository.query_listings(minimum_score=0, view="all")
    assert repeated_id == listing_id
    assert created_again is False
    assert len(stored) == 1
    assert stored[0]["first_found"] == "2026-07-19T12:00:00+00:00"
    assert stored[0]["last_seen"] == "2026-07-20T12:00:00+00:00"
    assert stored[0]["status"] == "saved"
    assert stored[0]["note"] == "Contact tomorrow"
    assert stored[0]["price"] == 1450


def test_database_status_and_note_validation(repository: Repository) -> None:
    listing_id, _ = repository.upsert_listing(
        ListingCandidate("Test", "1", "Room", "https://example.test/1"), result()
    )
    assert repository.set_listing_status(listing_id, "dismissed")
    assert repository.set_listing_note(listing_id, "Pass")
    assert repository.query_listings(0, view="active") == []
    assert repository.query_listings(0, view="dismissed")[0]["note"] == "Pass"


def test_database_tracks_when_a_listing_is_opened_without_changing_its_status(repository: Repository) -> None:
    listing_id, _ = repository.upsert_listing(
        ListingCandidate("Test", "opened", "Room", "https://example.test/opened"), result()
    )
    assert repository.set_listing_status(listing_id, "saved")
    assert repository.mark_listing_opened(listing_id)

    stored = repository.query_listings(0, view="saved")[0]
    assert stored["status"] == "saved"
    assert stored["opened_at"] is not None
    assert repository.open_listing_url(listing_id) == "https://example.test/opened"


def test_database_can_sort_unopened_listings_before_opened_ones(repository: Repository) -> None:
    unopened_id, _ = repository.upsert_listing(
        ListingCandidate("Test", "unopened", "Untouched room", "https://example.test/unopened"),
        result(score=70),
    )
    opened_id, _ = repository.upsert_listing(
        ListingCandidate("Test", "opened-first", "Opened room", "https://example.test/opened-first"),
        result(score=90),
    )
    assert repository.mark_listing_opened(opened_id)

    listings = repository.query_listings(0, view="all", sort="unopened")

    assert [listing["id"] for listing in listings] == [unopened_id, opened_id]


def test_database_can_filter_home_style_and_sort_by_explicit_move_in_date(repository: Repository) -> None:
    early = ListingCandidate(
        "Furnished Finder",
        "early-house",
        "Private room in house",
        "https://example.test/early-house",
        listing_type="Private room · House",
    )
    unknown = ListingCandidate(
        "Furnished Finder",
        "unknown-flat",
        "Private room in apartment",
        "https://example.test/unknown-flat",
        listing_type="Private room · Apartment",
    )
    late = ListingCandidate(
        "Furnished Finder",
        "late-house",
        "Private room in house",
        "https://example.test/late-house",
        listing_type="Private room · House",
    )
    early_result = ScoreResult(
        75,
        [],
        "Unknown: lease.",
        {"availability": {"available_on": "2026-08-01"}, "home_facts": {"primary": "Private room · House"}},
    )
    late_result = ScoreResult(
        80,
        [],
        "Unknown: lease.",
        {"availability": {"available_on": "2026-08-20"}, "home_facts": {"primary": "Private room · House"}},
    )
    unknown_result = ScoreResult(
        90,
        [],
        "Unknown: lease.",
        {"home_facts": {"primary": "Private room · Shared flat"}},
    )
    repository.upsert_listing(early, early_result)
    repository.upsert_listing(unknown, unknown_result)
    repository.upsert_listing(late, late_result)

    soonest = repository.query_listings(0, view="all", sort="available")
    houses = repository.query_listings(0, view="all", home_style="house")

    assert [row["source_id"] for row in soonest] == ["early-house", "late-house", "unknown-flat"]
    assert [row["source_id"] for row in houses] == ["late-house", "early-house"]
    assert soonest[0]["available_on"] == "2026-08-01"
    assert soonest[0]["home_facts"]["primary"] == "Private room · House"


def test_the_listing_identity_lookup_is_indexed(repository: Repository) -> None:
    """`find_listing` matches on (platform, source_id) OR canonical_url. With
    only the URL half indexed SQLite cannot use its OR optimisation and scans
    the whole table -- once per listing read, which for a source bringing two
    thousand homes against a five-thousand-row table is ten million row
    reads."""
    with repository.connection() as connection:
        plan = " ".join(
            str(row["detail"])
            for row in connection.execute(
                """EXPLAIN QUERY PLAN
                   SELECT * FROM listings
                    WHERE (platform = ? AND source_id = ?) OR canonical_url = ?
                    LIMIT 1""",
                ("Movoto", "abc", "https://example.test/x"),
            )
        )

    # Both halves of the OR must be index-driven for SQLite to use its
    # multi-index optimisation. One unindexed half turns the whole thing into
    # a table scan, and the UNIQUE constraints are what currently supply both
    # -- which is why an explicit index on (platform, source_id) was measured,
    # found redundant, and not kept.
    assert "SCAN" not in plan.upper(), f"the identity lookup scans the table: {plan}"
    assert plan.upper().count("SEARCH") == 2, plan


def test_source_durations_ignore_runs_that_did_not_do_the_work(
    repository: Repository,
) -> None:
    """A skipped source finishes instantly and a stalled one is abandoned.
    Counted as durations they drag the estimate down and the progress bar
    starts promising a scan far shorter than the one running."""
    run_id = repository.begin_scan("test")
    for status, platform in (("success", "Real"), ("skipped", "Skipped")):
        source_run = repository.begin_source_run(run_id, platform, search_url="https://example.test")
        repository.finish_source_run(source_run, status)

    durations = repository.typical_source_seconds()

    assert "Skipped" not in durations


def test_the_neighborhood_filter_omits_city_wide_values(repository: Repository) -> None:
    """A dropdown listing both "San Francisco" and "city of san francisco".

    Neighborhoods reach the filter straight from whatever a source wrote, so
    city-level text arrived as if it named a neighborhood -- twice over, once
    capitalised and once not, sitting in the list beside Castro and Nob Hill.
    Scoring already knows these are not neighborhoods; the filter did not.
    """
    for index, (where, expected) in enumerate(
        [
            ("Castro", True),
            ("Nob Hill", True),
            ("San Francisco", False),
            ("city of san francisco", False),
            ("SF", False),
            ("san francisco, ca", False),
        ]
    ):
        repository.upsert_listing(
            ListingCandidate(
                platform="Craigslist",
                source_id=str(index),
                title=f"Room in {where}",
                original_url=f"https://example.test/{index}",
                price=1500,
                neighborhood=where,
            ),
            result(),
            "2026-09-17T12:00:00+00:00",
        )

    neighborhoods, _ = repository.filter_options(minimum_score=0)

    assert "Castro" in neighborhoods
    assert "Nob Hill" in neighborhoods
    for city_wide in ("San Francisco", "city of san francisco", "SF", "san francisco, ca"):
        assert city_wide not in neighborhoods, f"{city_wide!r} is a city, not a neighborhood"


def test_a_scan_that_only_failed_in_the_resolver_reached_nothing(repository: Repository) -> None:
    """The by-hand budget charged for checks that asked nobody anything.

    With the wifi off every source fails before a socket opens, and SpareRoom
    still files a "success" it produced without leaving the machine, because
    private rooms are not in this deal. One phantom row was enough to spend the
    day's one check, so somebody back on a network was refused a real one.
    """
    scan_id = repository.begin_scan("manual")
    for platform in ("Zillow", "Craigslist"):
        run = repository.begin_source_run(scan_id, platform, "https://example.test/x")
        repository.finish_source_run(
            run, "error", message="ConnectError: [Errno 8] nodename nor servname provided, or not known"
        )
    phantom = repository.begin_source_run(scan_id, "SpareRoom", "https://example.test/s")
    repository.finish_source_run(
        phantom, "success", seen=0, message="Skipped because private rooms are not enabled in Your deal."
    )
    repository.finish_scan(scan_id, "completed_with_errors", failed=2)

    assert repository.recent_scans(1)[0]["sources_reached"] == 0


def test_a_scan_the_sites_turned_away_did_reach_them(repository: Repository) -> None:
    """An HTTP refusal is a request that arrived, and still spends the budget."""
    scan_id = repository.begin_scan("manual")
    blocked = repository.begin_source_run(scan_id, "Trulia", "https://example.test/t")
    repository.finish_source_run(
        blocked, "error", message="SourceError: Trulia turned away an unattended request (HTTP 403)."
    )
    repository.finish_scan(scan_id, "completed_with_errors", failed=1)

    assert repository.recent_scans(1)[0]["sources_reached"] == 1
