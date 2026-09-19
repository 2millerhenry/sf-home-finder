from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx

from sf_housing.database import Repository
from sf_housing.freshness import FRESHNESS_WINDOW, evaluate_source_freshness, source_is_in_backoff, source_key
from sf_housing.models import ListingCandidate
from sf_housing.scanner import Scanner
from sf_housing.sources import SourceError


class CurrentSource:
    platform = "Craigslist"
    mode = "automatic"
    provider = "public"
    search_url = "https://example.test/craigslist"
    manual_reason = None
    detail_budget = 0

    def search(self, client, preferences):
        return [
            ListingCandidate(
                platform=self.platform,
                source_id="current-1",
                title="Private room in NOPA",
                original_url="https://example.test/craigslist/current-1",
                price=1500,
                neighborhood="NOPA",
            )
        ]

    def enrich(self, client, listing):
        return listing


class EmptySource(CurrentSource):
    platform = "SpareRoom"
    search_url = "https://example.test/spareroom"

    def search(self, client, preferences):
        return []


class FlakySource(CurrentSource):
    platform = "Craigslist"
    search_url = "https://example.test/flaky"

    def __init__(self) -> None:
        self.calls = 0
        self.fail = True

    def search(self, client, preferences):
        self.calls += 1
        if self.fail:
            raise SourceError("Craigslist returned no recognizable result cards; its page format may have changed.")
        return super().search(client, preferences)


class FacebookGmailSource(CurrentSource):
    platform = "Facebook Marketplace"
    provider = "gmail_alerts"
    source_key = "FacebookMarketplaceSource"
    search_url = "https://www.facebook.com/marketplace/you/alerts/"


def record_source_run(
    repository: Repository,
    source: CurrentSource,
    status: str,
    *,
    seen: int = 0,
    message: str | None = None,
    **counts: int,
) -> int:
    scan_id = repository.begin_scan("fixture")
    source_id = repository.begin_source_run(
        scan_id,
        source.platform,
        source.search_url,
        provider=source.provider,
        source_key=source_key(source),
    )
    repository.finish_source_run(source_id, status, seen=seen, message=message, **counts)
    repository.finish_scan(scan_id, "completed" if status == "success" else "completed_with_errors")
    return source_id


def test_successful_zero_inventory_is_current_not_a_failure(repository: Repository) -> None:
    source = EmptySource()
    record_source_run(repository, source, "success", seen=0)

    health = evaluate_source_freshness(repository, source)

    assert health.status == "working_zero"
    assert "valid result" in health.explanation
    assert health.needs_attention is False


def test_stale_success_is_visible_even_when_old_listings_are_preserved(repository: Repository) -> None:
    source = CurrentSource()
    source_id = record_source_run(repository, source, "success", seen=3)
    old = (datetime.now(UTC) - FRESHNESS_WINDOW - timedelta(minutes=1)).isoformat()
    with repository.connection() as connection:
        connection.execute(
            "UPDATE source_runs SET finished_at = ? WHERE id = ?", (old, source_id)
        )
        connection.commit()

    health = evaluate_source_freshness(repository, source)

    assert health.status == "stale"
    assert "Existing listings remain available" in health.explanation
    assert "Check for new homes now" in health.action


def test_parser_drift_after_a_good_run_is_attention_not_silent(repository: Repository) -> None:
    source = CurrentSource()
    record_source_run(repository, source, "success", seen=2)
    record_source_run(
        repository,
        source,
        "error",
        message="Craigslist returned no recognizable result cards; its page format may have changed.",
    )

    health = evaluate_source_freshness(repository, source)

    assert health.status == "attention"
    assert health.failure_streak == 1
    assert "last good result is preserved" in health.explanation.casefold()
    assert "format may have changed" in health.explanation


def test_repeated_failures_back_off_automatic_scans_but_manual_retry_recovers(
    repository: Repository, preferences
) -> None:
    flaky = FlakySource()
    healthy = EmptySource()
    scanner = Scanner(repository, lambda: preferences, [flaky, healthy], detail_delay_seconds=0)

    first = scanner.run_scan("scheduled")
    second = scanner.run_scan("scheduled")
    third = scanner.run_scan("scheduled")

    assert first.sources_failed == 1
    assert second.sources_failed == 1
    assert third.status == "completed"
    assert flaky.calls == 2
    statuses = {run["platform"]: run for run in repository.latest_source_runs()}
    assert statuses["Craigslist"]["status"] == "backoff"
    assert statuses["SpareRoom"]["status"] == "success"
    paused = evaluate_source_freshness(repository, flaky)
    assert paused.status == "backoff"
    assert paused.failure_streak == 2
    assert "Retries automatically after" in paused.action
    # The panel used to say a source was paused and when it would retry, and
    # never what had gone wrong.
    assert paused.reason, "a paused source has to say what failed"
    assert paused.reason in paused.panel_note
    assert paused.action in paused.panel_note
    # The deferral writes its own notice as that run's message, so reading the
    # newest run quoted "Retries automatically after ..." back as the cause.
    assert "Retries automatically" not in paused.reason
    assert "recognizable result cards" in paused.reason, "quote the failure itself"
    # The row is already headed by the platform, so the cause does not repeat it.
    assert not paused.reason.startswith("Craigslist")

    flaky.fail = False
    recovered = scanner.run_scan("manual")
    health = evaluate_source_freshness(repository, flaky)

    assert recovered.status == "completed"
    assert flaky.calls == 3
    assert health.status == "working"
    assert health.failure_streak == 0


def test_freshness_survives_restart_and_new_source_identity_does_not_merge_providers(tmp_path) -> None:
    path = tmp_path / "housing.sqlite3"
    repository = Repository(path)
    repository.initialize()
    source = CurrentSource()
    record_source_run(repository, source, "error", message="network one")
    record_source_run(repository, source, "error", message="network two")
    # A different provider may share a human platform name, but must not erase
    # this source's repeated-failure history.
    scan_id = repository.begin_scan("fixture")
    other = repository.begin_source_run(
        scan_id,
        "Craigslist",
        "https://example.test/other",
        provider="gmail",
        source_key="GmailCraigslistFixture",
    )
    repository.finish_source_run(other, "success", seen=1)
    repository.finish_scan(scan_id, "completed")

    restarted = Repository(path)
    restarted.initialize()
    health = evaluate_source_freshness(restarted, source)

    assert health.status == "backoff"
    assert health.failure_streak == 2
    latest = {run["source_key"]: run for run in restarted.latest_source_runs()}
    assert latest["CurrentSource::public"]["status"] == "error"
    assert latest["GmailCraigslistFixture"]["status"] == "success"


def test_gmail_and_apify_facebook_history_stay_separate(repository: Repository) -> None:
    gmail = FacebookGmailSource()
    record_source_run(repository, gmail, "error", message="Gmail alert parser found no supported direct link.")
    scan_id = repository.begin_scan("fixture")
    apify_id = repository.begin_source_run(
        scan_id,
        "Facebook Marketplace",
        "https://example.test/apify",
        provider="apify",
        source_key="FacebookMarketplaceSource::apify",
    )
    repository.finish_source_run(apify_id, "success", seen=3)
    repository.finish_scan(scan_id, "completed_with_errors", failed=1)

    gmail_health = evaluate_source_freshness(repository, gmail)
    latest = {run["source_key"]: run for run in repository.latest_source_runs()}

    assert gmail_health.status == "stale"
    assert gmail_health.failure_streak == 1
    assert latest["FacebookMarketplaceSource::gmail_alerts"]["status"] == "error"
    assert latest["FacebookMarketplaceSource::apify"]["status"] == "success"


def test_being_offline_is_explained_in_words_a_renter_understands() -> None:
    """The panel quoted the C library at people whose wifi had dropped.

    Losing the network is the most ordinary failure this app has -- a laptop
    opened away from home fails every source at once, and the author's own
    database shows five sources going down together on one evening in
    September. What the panel said was "[Errno 8] nodename nor servname
    provided, or not known", which names no cause a person can act on and
    reads like a crash. The source is fine and so is the app; there is simply
    no internet, and the next check will pick it up.
    """
    from sf_housing.freshness import _first_sentence

    offline = [
        "ConnectError: [Errno 8] nodename nor servname provided, or not known",
        "ConnectError: [Errno -2] Name or service not known",
        "ConnectError: [Errno -3] Temporary failure in name resolution",
    ]
    for message in offline:
        said = _first_sentence(message, "Redfin")
        assert "Errno" not in said, f"the errno reached the page: {said}"
        assert "servname" not in said and "name resolution" not in said, said
        assert "no internet" in said.casefold(), (
            f"it does not say what is actually wrong: {said}"
        )

    # A site refusing or timing out is a different thing and must keep saying so,
    # rather than blaming the person's connection for the site's own behaviour.
    refused = _first_sentence(
        "SourceError: Redfin answered HTTP 202 rather than a page of results", "Redfin"
    )
    assert "no internet" not in refused.casefold(), refused
    assert "202" in refused, refused


def test_an_unreachable_network_is_named_however_the_socket_failed() -> None:
    """Only the DNS spelling of "offline" was translated.

    A laptop loses the internet in more ways than one. With wifi off macOS
    fails to resolve and says "nodename nor servname", which this already
    turned into a sentence. With a VPN dropped, a proxy still configured or a
    cable pulled, the name resolves and the socket does not open, and what
    came back was "[Errno 61] Connection refused". Running the app behind a
    dead proxy printed that verbatim on seventeen source rows at once.
    """
    from sf_housing.freshness import _first_sentence

    for message in (
        "ConnectError: [Errno 61] Connection refused",
        "ConnectError: [Errno 51] Network is unreachable",
        "ConnectError: [Errno 65] No route to host",
        "SourceError: All Craigslist searches failed: three_bedroom: [Errno 61] "
        "Connection refused; whole_unit: [Errno 61] Connection refused",
    ):
        said = _first_sentence(message, "Craigslist")
        assert "Errno" not in said, f"the errno reached the page: {said}"
        assert "no internet" in said.casefold(), said


def test_the_ready_check_says_what_the_dashboard_says_about_the_same_failure(
    repository: Repository,
) -> None:
    """Support printed the raw exception the dashboard had already translated.

    Both pages read one SourceFreshness. The panel used the cleaned-up reason;
    the page headed "Is everything working?" interpolated the stored message
    straight into its explanation, so the reader least able to act on it was
    the one shown "ConnectError: [Errno 8] nodename nor servname provided, or
    not known".
    """
    source = CurrentSource()
    record_source_run(
        repository,
        source,
        "error",
        message="ConnectError: [Errno 8] nodename nor servname provided, or not known",
    )

    health = evaluate_source_freshness(repository, source)

    assert "Errno" not in health.explanation, health.explanation
    assert "ConnectError" not in health.explanation, health.explanation
    assert "no internet" in health.explanation.casefold(), health.explanation
    assert health.reason in health.explanation


def test_a_source_that_worked_last_week_is_not_called_one_that_never_has(
    repository: Repository,
) -> None:
    """Redfin worked eighteen times and Support said it never had.

    Source history is read through a bounded window, and a failing source
    fills it fastest: every deferred automatic attempt writes its own backoff
    row. Redfin last succeeded on Sep 8; by Sep 17 its twelve most recent rows
    were backoff and error alone, and the live Ready Check read "No successful
    result has been recorded yet" about a source that had produced 1,616
    listings that fortnight.
    """
    source = CurrentSource()
    record_source_run(repository, source, "success", seen=7)
    for _ in range(14):
        record_source_run(repository, source, "error", message="SourceError: HTTP 202.")

    health = evaluate_source_freshness(repository, source)

    assert health.last_success_at is not None
    assert "No successful result has been recorded yet" not in health.explanation
    assert "last good result is preserved" in health.explanation


def test_an_alert_source_with_no_alert_yet_does_not_claim_to_be_working(
    repository: Repository,
) -> None:
    """Seventy zero runs in a row read as "a valid result, not a failure".

    HotPads, Apartments.com and Roomies arrive through saved-search alert
    email. With Gmail connected but no saved search on the far side, each one
    completes, imports nothing, and was reported as working with no matches --
    which tells the reader those sites have no homes, rather than that the
    alert feeding them was never set up. The connector record already said
    waiting_first_alert; nothing read it.
    """
    source = EmptySource()
    source.connector_key = "gmail:spareroom"
    repository.set_connector_state(
        "gmail:spareroom",
        "waiting_first_alert",
        message="No SpareRoom saved-search alert has arrived yet.",
    )
    record_source_run(repository, source, "success", seen=0)

    health = evaluate_source_freshness(repository, source)

    assert health.status == "waiting_first_alert"
    assert "valid result" not in health.explanation
    assert "alert" in health.explanation.casefold()


def test_a_check_that_ran_out_of_time_says_how_many_sources_it_missed(
    repository: Repository,
) -> None:
    """A scan that reached one source of three was displayed as clean.

    Source runs the scan never got to are marked "skipped" and the scan still
    finishes "completed" with zero errors. The real database holds a check on
    Sep 8 that skipped twenty-one of twenty-two sources and rendered as a green
    row reading "186 checked . 0 errors".
    """
    scan_id = repository.begin_scan("fixture")
    reached = repository.begin_source_run(scan_id, "Craigslist", "https://example.test/c")
    repository.finish_source_run(reached, "success", seen=4)
    for platform in ("Zillow", "Trulia"):
        missed = repository.begin_source_run(scan_id, platform, "https://example.test/x")
        repository.finish_source_run(
            missed, "skipped", message="Skipped to keep this scan within the 240-second time limit."
        )
    repository.finish_scan(scan_id, "completed", failed=0)

    summary = repository.recent_scans(1)[0]

    assert summary["sources_skipped"] == 2


def test_a_source_the_last_scan_never_opened_does_not_report_its_own_count_as_zero(
    repository: Repository,
) -> None:
    """A skipped run erased the count from the check that actually ran.

    ``latest`` is the newest source_run of any status, and a run the scan
    dropped for time is written with ``listings_seen = 0``.  Reading the count
    from it turned "Zillow checked 983 listings eight hours ago" into the badge
    "No matches" and the sentence "found no matching listings.  This is a valid
    result, not a failure."  The real database has Rent.com doing exactly this
    on Sep 15 and Sep 16, 90 homes one check and "No matches" the next.
    """
    source = CurrentSource()
    record_source_run(repository, source, "success", seen=983)
    record_source_run(
        repository,
        source,
        "skipped",
        message="Skipped to keep this scan within the 240-second time limit.",
    )

    health = evaluate_source_freshness(repository, source)

    assert health.listings_seen == 983
    assert "valid result" not in health.explanation


def test_a_source_the_last_scan_never_opened_says_so(repository: Repository) -> None:
    """Being skipped for time was indistinguishable from being checked.

    The watchdog counted only "success" and "error" as terminal, so a scan that
    ran out of its 240 seconds and never opened a source left that source
    reading exactly as it had before -- while the scan list beside it said one
    source was not reached.  The two panels disagreed, and the one naming the
    source was the one that was wrong.
    """
    source = CurrentSource()
    record_source_run(repository, source, "success", seen=12)
    record_source_run(
        repository,
        source,
        "skipped",
        message="Skipped to keep this scan within the 240-second time limit.",
    )

    health = evaluate_source_freshness(repository, source)

    assert health.status == "not_reached"
    assert health.short_label == "Not checked this time"
    assert "not read" in health.label.casefold()
    # The reason the scan wrote down, quoted rather than assumed.
    assert "within the 240-second time limit" in health.explanation
    assert "valid result" not in health.explanation
    assert health.needs_attention is False


def _age(repository: Repository, run_id: int, hours: float) -> None:
    """Move a recorded run back in time."""
    moment = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
    with repository.connection() as connection:
        connection.execute(
            "UPDATE source_runs SET started_at = ?, finished_at = ? WHERE id = ?", (moment, moment, run_id)
        )
        connection.commit()


def _redfin_since_the_eighth(repository: Repository, source: CurrentSource, failures: int) -> None:
    """Failures the way a paused source really piles them up: each automatic
    check it sits out writes a backoff row of its own between them."""
    for _ in range(failures):
        record_source_run(
            repository, source, "error", message="SourceError: Redfin answered HTTP 202 rather than a page of results."
        )
        for _ in range(2):
            record_source_run(
                repository, source, "backoff", message="Retries automatically after Sep 19 at 10:55 AM."
            )


def test_a_source_failing_for_longer_than_the_window_is_told_how_long(
    repository: Repository,
) -> None:
    """Redfin had failed fourteen checks since 8 September and Support said "4
    consecutive checks failed": the history read is twelve rows, and the backoff
    rows written between the failures had pushed the other ten out of it. The
    last good result was already found beyond the window; the count was not."""
    source = CurrentSource()
    # Failures before the last success are a streak that already ended.
    for _ in range(3):
        record_source_run(repository, source, "error", message="SourceError: an older failure.")
    record_source_run(repository, source, "success", seen=7)
    _redfin_since_the_eighth(repository, source, 14)

    health = evaluate_source_freshness(repository, source)

    assert health.failure_streak == 14
    assert health.explanation.startswith("14 consecutive checks failed."), health.explanation
    assert "The last good result is preserved from" in health.explanation
    assert "202" in health.reason and "Retries automatically" not in health.reason


def test_a_source_that_has_never_worked_is_not_confused_with_one_that_stopped(
    repository: Repository,
) -> None:
    """The other half of the same window: with no success anywhere in it, a
    source that has never once worked has to say so -- and say for how long --
    rather than borrowing the wording of one that worked last week."""
    source = CurrentSource()
    _redfin_since_the_eighth(repository, source, 14)

    health = evaluate_source_freshness(repository, source)

    assert health.last_success_at is None
    assert health.failure_streak == 14
    assert "No successful result has been recorded yet." in health.explanation
    assert "last good result" not in health.explanation


def test_failures_behind_a_run_of_skipped_checks_are_not_forgotten(
    repository: Repository,
) -> None:
    """A source that failed and was then passed over twelve checks running
    read "has not run yet" -- a claim about a source that had run, twice, and
    failed both times."""
    source = CurrentSource()
    for _ in range(2):
        run = record_source_run(repository, source, "error", message="SourceError: Craigslist returned no recognizable result cards.")
        _age(repository, run, 30)
    for _ in range(12):
        record_source_run(repository, source, "skipped", message="Skipped to keep this scan within the 240-second time limit.")

    health = evaluate_source_freshness(repository, source)

    assert health.status != "not_run", health.explanation
    assert health.failure_streak == 2
    assert "No successful result has been recorded yet." in health.explanation
    # What failed, not what the newest row happens to say.
    assert "recognizable result cards" in health.reason, health.reason


def test_a_source_passed_over_for_its_own_floor_is_not_said_to_have_run_out_of_time(
    repository: Repository,
) -> None:
    """Zillow asks not to be read again within fifteen minutes, so pressing
    Check just after the scheduled one passes it over on purpose. The panel
    said the check "ran out of time before opening" it, and that pressing
    Check again would reach it immediately -- both false. It now quotes the
    reason the scan wrote down when it decided."""
    source = CurrentSource()
    record_source_run(repository, source, "success", seen=983)
    floor = (
        "Checked less than 8 minute(s) ago. Reading it again this soon is what gets a source "
        "to refuse us, and its homes are already collected."
    )
    record_source_run(repository, source, "skipped", message=floor)

    health = evaluate_source_freshness(repository, source)

    assert health.status == "not_reached"
    assert floor in health.explanation
    assert "ran out of time" not in health.explanation
    assert "immediately" not in health.action
    assert health.listings_seen == 983


def test_a_source_one_check_passed_over_is_not_listed_as_something_to_fix(
    repository: Repository,
) -> None:
    """Support's list of things to fix took every state it did not recognise,
    so "not checked this time" -- whose own advice is "Nothing to do" -- sat
    there beside the sources that really were broken, under a headline saying
    something needed a look."""
    from sf_housing.diagnostics import _source_freshness_checks

    source = CurrentSource()
    record_source_run(repository, source, "success", seen=12)
    record_source_run(
        repository, source, "skipped", message="Skipped to keep this scan within the 240-second time limit."
    )

    (check,) = _source_freshness_checks(repository, [source], datetime.now(UTC))

    assert check.status == "pass", (check.status, check.label)


def test_a_check_cut_short_that_kept_homes_is_not_called_stale_or_simply_failed(
    repository: Repository,
) -> None:
    """A read that stopped part way stores what it read and is recorded as the
    failure it was, with the counts of what it wrote. Its last complete check
    being two days old made it "stale", and the page said "The latest check
    failed" about a check that had just brought in ninety homes."""
    source = CurrentSource()
    complete = record_source_run(repository, source, "success", seen=240)
    _age(repository, complete, 48)
    record_source_run(
        repository,
        source,
        "error",
        seen=90,
        message="SourceError: Craigslist answered HTTP 503, which is the site having trouble rather than a page of results; the next check tries again.",
        added=12,
        updated=78,
    )

    health = evaluate_source_freshness(repository, source)

    assert health.status == "attention", health.status
    assert health.explanation.startswith("The latest check was cut short.")
    assert "The 90 listing(s) read before that were kept." in health.explanation
    assert "Its last complete check was" in health.explanation
    assert health.reason in health.explanation


class OfflineSource(CurrentSource):
    """Craigslist asked with no network: the resolver fails before anything leaves the machine."""

    def search(self, client, preferences):
        raise httpx.ConnectError("[Errno 8] nodename nor servname provided, or not known")


def test_checks_that_reached_no_site_do_not_pause_it(repository: Repository, preferences) -> None:
    """Pre-launch audit: a Mac that wakes before its Wi-Fi runs the missed
    check at once, and every source fails in the resolver. Two of those in a
    row paused every source for hours, and the checks after the network came
    back read nothing. Nothing reached any site: there is nothing to back off
    from."""
    offline = OfflineSource()
    scanner = Scanner(repository, lambda: preferences, [offline], detail_delay_seconds=0)

    for _ in range(3):
        scanner.run_scan("scheduled")

    assert source_is_in_backoff(repository, offline) is None
    health = evaluate_source_freshness(repository, offline)
    assert "No internet connection" in health.explanation, health.explanation


def test_a_long_offline_spell_does_not_pause_a_source_and_a_site_turning_it_away_still_does(
    repository: Repository,
) -> None:
    """The same past the bounded history, where failures since the last
    success are counted in the database: a week of checks with no network is
    no strike against the site, and two real refusals after it still are."""
    source = CurrentSource()
    for _ in range(14):
        record_source_run(
            repository, source, "error", message="ConnectError: [Errno 8] nodename nor servname provided, or not known"
        )

    assert source_is_in_backoff(repository, source) is None

    for _ in range(2):
        record_source_run(repository, source, "error", message="SourceError: Craigslist answered HTTP 503.")

    assert source_is_in_backoff(repository, source) is not None


def test_after_a_success_checks_that_reached_no_site_do_not_pause_it_and_refusals_do(
    repository: Repository,
) -> None:
    """Within the recent history, counted run by run: offline checks after a
    good one are no strike, and two refusals after them still pause it."""
    source = CurrentSource()
    record_source_run(repository, source, "success", seen=1)
    for _ in range(3):
        record_source_run(
            repository, source, "error", message="ConnectError: [Errno 51] Network is unreachable"
        )

    assert source_is_in_backoff(repository, source) is None

    for _ in range(2):
        record_source_run(repository, source, "error", message="SourceError: Craigslist answered HTTP 503.")

    assert source_is_in_backoff(repository, source) is not None
