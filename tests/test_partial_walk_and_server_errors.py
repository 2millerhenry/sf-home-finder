"""What a refusal part way through a paginated read is allowed to cost.

The guard the paginated sources share only ever caught ``SourceError``. ``_require_page`` says 403 and
429 in words and then calls ``raise_for_status``, so a 500, 502 or 503 left as
an ``httpx.HTTPStatusError``: outside every guard, so the pages already read
were lost, and in front of the reader as a class name with a link to MDN.

Zillow had no guard at all, and could not simply take the others': it blocks by
address for hours, so a refusal part way through has to reach the backoff,
which only hears a failure. On the author's own install a 503 on page four
threw away three pages of homes on 15 September, and a timeout on page five
threw away four on the 12th. Its reads now keep the pages and still fail.

Apartment List and Zumper read one search page each, and their building pages
were already guarded one at a time by the scan. What they lacked was the
shared refusal check on either: a 202 wall read as "its page format may have
changed", a 503 reached the reader as a class name, and an empty 202 building
page was parsed into a thinner record that the recheck pass then stamped as a
confirmation.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from sf_housing.database import Repository
from sf_housing.freshness import evaluate_source_freshness
from sf_housing.scanner import Scanner
from sf_housing.scoring import score_listing
from sf_housing.sources import (
    ApartmentListSource,
    PartialReadError,
    SourceError,
    ZillowSource,
    ZumperSource,
    _require_page,
)
from tests.test_zillow import FakeClient, FakeResponse, profile, renamed, search_page


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_a_server_error_is_said_in_words_like_every_other_refusal(status: int) -> None:
    """A source being down is the site's problem, not a stack trace for the reader."""
    response = httpx.Response(status, request=httpx.Request("GET", "https://example.test/"))

    with pytest.raises(SourceError) as caught:
        _require_page(response, "Redfin")

    assert str(status) in str(caught.value)
    assert "HTTPStatusError" not in str(caught.value)


def test_zillow_still_reports_a_source_that_refuses_from_the_first_page() -> None:
    """A quiet zero would read as "Zillow has nothing", which is a different claim."""
    with pytest.raises(SourceError):
        ZillowSource().search(FakeClient(FakeResponse("", 202)), profile("one_bedroom"))


# --------------------------------------------------------------------------
# Zillow: keep the pages read, and still report the failure
# --------------------------------------------------------------------------


def answer(url: str, status: int, text: str = "") -> httpx.Response:
    """A real response, so a status meets the checks it really would."""
    return httpx.Response(status, text=text, request=httpx.Request("GET", url))


class Scripted:
    """Answers each request with the next item in turn, raising any that is an error.

    The last one is repeated once the script runs out.
    """

    def __init__(self, *answers: object) -> None:
        self.answers = list(answers)
        self.requested: list[str] = []

    def get(self, url, **kwargs):
        self.requested.append(url)
        reply = self.answers[min(len(self.requested) - 1, len(self.answers) - 1)]
        if isinstance(reply, BaseException):
            raise reply
        return reply


def unpaced_zillow() -> ZillowSource:
    source = ZillowSource()
    source.PAGE_PAUSE_SECONDS = 0
    return source


def two_pages_then(failure: object) -> Scripted:
    """Two pages of three San Francisco homes each, then ``failure``."""
    return Scripted(
        FakeResponse(search_page()), FakeResponse(renamed(search_page(), "-2")), failure
    )


OFFLINE = "[Errno 8] nodename nor servname provided, or not known"


@pytest.mark.parametrize(
    "failure",
    [
        FakeResponse("", 403),
        FakeResponse("", 429),
        FakeResponse("", 503),
        FakeResponse("", 202),
        FakeResponse("   ", 200),
        httpx.ReadTimeout("The read operation timed out"),
        httpx.ConnectError(OFFLINE),
    ],
    ids=["403", "429", "503", "202", "blank", "timeout", "offline"],
)
def test_zillow_keeps_the_pages_it_read_when_a_later_one_fails(failure) -> None:
    """Page three failing cost pages one and two. Now they travel with the
    failure, and nothing more is asked of a site that has just failed."""
    client = two_pages_then(failure)

    with pytest.raises(PartialReadError) as cut:
        unpaced_zillow().search(client, profile("one_bedroom"))

    assert len({listing.source_id for listing in cut.value.listings}) == 6
    assert len(client.requested) == 3, "it went on asking after the failure"
    # Still the failure it was, in the same words as on a first page, so the
    # scan records it -- and the backoff hears it -- exactly as before.
    assert isinstance(cut.value, SourceError)
    assert str(cut.value) == str(cut.value.cause)
    if isinstance(failure, BaseException):
        assert cut.value.cause is failure


@pytest.mark.parametrize(
    "failure",
    [FakeResponse("", 403), FakeResponse("", 503), httpx.ConnectError(OFFLINE)],
    ids=["403", "503", "offline"],
)
def test_zillow_failing_on_its_first_page_is_reported_as_what_it_is(failure) -> None:
    """With nothing read there is nothing to keep. A source that is really
    blocked, or a laptop that is really offline, is reported as exactly that."""
    with pytest.raises((SourceError, httpx.HTTPError)) as caught:
        unpaced_zillow().search(Scripted(failure), profile("one_bedroom"))

    assert not isinstance(caught.value, PartialReadError)


@pytest.mark.parametrize("status", [400, 404, 410])
def test_a_page_zillow_does_not_have_is_the_end_of_the_search_not_a_failure(status) -> None:
    """Past its last page the plain search answers 400. A 404 or 410 there says
    the same thing, and read as a failure it would fail every read that ran
    out of pages -- and pause Zillow for doing its job."""
    url = "https://www.zillow.com/san-francisco-ca/rentals/2_p/"
    client = Scripted(FakeResponse(search_page()), answer(url, status))

    listings = unpaced_zillow().search(client, profile("one_bedroom"))

    assert len(listings) == 3


# --------------------------------------------------------------------------
# Apartment List and Zumper: a refusal is said as one
# --------------------------------------------------------------------------


@pytest.mark.parametrize("reader", [ApartmentListSource, ZumperSource], ids=["apartment-list", "zumper"])
@pytest.mark.parametrize(
    ("status", "text", "said"),
    [
        (403, "", "turned away an unattended request (HTTP 403)"),
        (429, "", "turned away an unattended request (HTTP 429)"),
        (503, "", "answered HTTP 503"),
        (202, "", "answered HTTP 202"),
        (200, "  ", "returned an empty page"),
        (200, "<html><title>Just a moment...</title></html>", "bot check"),
    ],
    ids=["403", "429", "503", "202", "blank", "captcha"],
)
def test_a_refused_first_page_is_said_as_a_refusal_not_a_changed_format(
    reader, status, text, said
) -> None:
    """Apartment List and Zumper went straight to raise_for_status. A 202 wall
    or a captcha then parsed into "its page format may have changed", which
    sends somebody to read a parser that is fine, and a 503 reached the reader
    as "HTTPStatusError: Server error ... developer.mozilla.org"."""
    client = Scripted(answer(reader.search_url, status, text))

    with pytest.raises(SourceError) as caught:
        reader().search(client, profile("one_bedroom"))

    message = str(caught.value)
    assert said in message, message
    assert "format may have changed" not in message
    assert "HTTPStatusError" not in message and "mozilla" not in message


@pytest.mark.parametrize("reader", [ApartmentListSource, ZumperSource], ids=["apartment-list", "zumper"])
def test_being_offline_is_left_for_the_panel_to_say_in_words(reader) -> None:
    """The resolver's own error travels untouched, which is what the panel
    recognises as "No internet connection reached this site"."""
    with pytest.raises(httpx.ConnectError):
        reader().search(Scripted(httpx.ConnectError(OFFLINE)), profile("one_bedroom"))


def test_an_apartment_list_building_page_that_refused_is_not_read_as_one() -> None:
    """An empty 202 passed raise_for_status and parsed into a record of the
    building with nothing in it -- thinner than the card it replaced."""
    card = ApartmentListSource().search(
        Scripted(answer(ApartmentListSource.search_url, 200, apartment_list_search())),
        profile("one_bedroom"),
    )[0]

    with pytest.raises(SourceError, match="HTTP 202"):
        ApartmentListSource().enrich(Scripted(answer(card.original_url, 202)), card)


def test_a_zumper_building_page_that_refused_is_said_as_a_refusal() -> None:
    """Zumper reads a building page only for a rent the search left out, and a
    refusal there is said in the same words as everywhere else."""
    from tests.test_free_sources import zumper_search

    unpriced = next(
        listing
        for listing in ZumperSource().search(
            Scripted(answer(ZumperSource.search_url, 200, zumper_search())), profile("one_bedroom")
        )
        if listing.price is None
    )

    with pytest.raises(SourceError, match=r"turned away an unattended request \(HTTP 429\)"):
        ZumperSource().enrich(Scripted(answer(unpriced.original_url, 429)), unpriced)


# --------------------------------------------------------------------------
# through the scanner: what is stored, what is recorded, what is said
# --------------------------------------------------------------------------


def apartment_list_search() -> str:
    from tests.test_free_sources import apartment_list_document

    return apartment_list_document()


class ZillowOverFakes(ZillowSource):
    """The real Zillow reader, answered by fakes instead of the scan's own client."""

    PAGE_PAUSE_SECONDS = 0
    # Several scans run back to back here; the quarter-hour floor has its own tests.
    min_seconds_between_reads = 0

    def __init__(self, client: Scripted) -> None:
        self.fake = client

    def search_for_trigger(self, client, preferences, trigger):
        return super().search_for_trigger(self.fake, preferences, trigger)


def zillow_run(repository: Repository) -> dict:
    return next(run for run in repository.latest_source_runs() if run["platform"] == "Zillow")


def test_a_scan_stores_what_zillow_served_before_refusing_and_records_the_refusal(
    repository: Repository,
) -> None:
    """The scan used to lose everything: the refusal was the only thing kept."""
    preferences = profile("one_bedroom")
    source = ZillowOverFakes(two_pages_then(FakeResponse("", 403)))
    scanner = Scanner(repository, lambda: preferences, [source], detail_delay_seconds=0)

    outcome = scanner.run_scan("scheduled")

    assert repository.count_listings() == 6, "the two pages read before the refusal were lost"
    assert outcome.listings_added == 6
    assert outcome.sources_failed == 1, "a refusal is still a failure"
    run = zillow_run(repository)
    assert run["status"] == "error"
    assert (run["listings_seen"], run["listings_added"]) == (6, 6)
    assert run["message"].startswith("SourceError: Zillow turned away an unattended request (HTTP 403)")

    health = evaluate_source_freshness(repository, source)
    assert health.status == "attention", "homes arrived, so it is not stale"
    assert health.explanation.startswith("The latest check was cut short.")
    assert "The 6 listing(s) read before that were kept." in health.explanation
    assert "It has not completed a check yet." in health.explanation
    assert "No successful result has been recorded yet" not in health.explanation
    assert health.reason in health.explanation and health.reason in health.panel_note


def test_a_scan_cut_short_by_the_network_says_so_and_keeps_the_homes(
    repository: Repository,
) -> None:
    preferences = profile("one_bedroom")
    source = ZillowOverFakes(two_pages_then(httpx.ConnectError(OFFLINE)))
    scanner = Scanner(repository, lambda: preferences, [source], detail_delay_seconds=0)

    scanner.run_scan("scheduled")

    health = evaluate_source_freshness(repository, source)
    assert repository.count_listings() == 6
    assert health.reason.startswith("No internet connection reached this site.")
    assert "Errno" not in health.explanation


def test_zillow_cut_short_twice_is_paused_and_a_clean_read_recovers_it(
    repository: Repository,
) -> None:
    """Why Zillow's refusals may not simply be absorbed as the other sources'
    are: it blocks by address, for hours, and the backoff that stops the app
    asking again only ever hears a failure. Kept pages must not cost it that."""
    preferences = profile("one_bedroom")
    source = ZillowOverFakes(two_pages_then(FakeResponse("", 403)))
    scanner = Scanner(repository, lambda: preferences, [source], detail_delay_seconds=0)

    scanner.run_scan("scheduled")
    source.fake = two_pages_then(FakeResponse("", 403))
    scanner.run_scan("scheduled")
    source.fake = two_pages_then(FakeResponse("", 403))
    scanner.run_scan("scheduled")

    assert source.fake.requested == [], "Zillow was asked again while paused"
    assert zillow_run(repository)["status"] == "backoff"
    paused = evaluate_source_freshness(repository, source)
    assert paused.status == "backoff"
    assert paused.failure_streak == 2
    assert paused.explanation.startswith("2 consecutive checks failed.")
    assert "read before that were kept" in paused.explanation

    source.fake = Scripted(FakeResponse(search_page()), FakeResponse(renamed(search_page(), "-2")))
    recovered = scanner.run_scan("manual")

    health = evaluate_source_freshness(repository, source)
    assert recovered.sources_failed == 0
    assert (health.status, health.failure_streak) == ("working", 0)


class CutShortButCanRecheck:
    """A source whose read stops part way, and which could recheck a home."""

    platform = "Craigslist"
    mode = "automatic"
    search_url = "https://example.test/cut-short"
    manual_reason = None
    detail_budget = 0

    def __init__(self) -> None:
        self.rechecked: list[str] = []

    def search(self, client, preferences):
        from sf_housing.models import ListingCandidate

        kept = ListingCandidate(
            platform=self.platform,
            source_id="kept-1",
            title="Sunny room in a NOPA flat",
            original_url="https://example.test/cut-short/kept-1",
            price=1500,
            neighborhood="NOPA",
            summary="A private room in a shared home, laundry on site, flexible lease.",
            listing_type="Room/share",
        )
        raise PartialReadError(
            SourceError("Craigslist turned away an unattended request (HTTP 403)."), [kept]
        )

    def enrich(self, client, listing):
        self.rechecked.append(listing.original_url)
        return listing.__class__(**{**listing.__dict__, "summary": "Still up."})


def test_a_read_cut_short_does_not_recheck_the_homes_it_never_reached(
    repository: Repository, preferences
) -> None:
    """The recheck pass looks again at shortlisted homes the search stopped
    returning. A search that stopped half way did not stop returning anything;
    it stopped. Rechecking would spend a request per home on a site that has
    just refused, for no evidence at all."""
    from sf_housing.models import ListingCandidate

    stale = (datetime.now(UTC) - timedelta(hours=72)).isoformat(timespec="seconds")
    absent = ListingCandidate(
        platform="Craigslist",
        source_id="absent-1",
        title="Large Noe Valley bedroom in a shared home",
        original_url="https://example.test/cut-short/absent-1",
        price=1500,
        neighborhood="NOPA",
        summary="A private room in a shared home, laundry on site, flexible lease.",
        listing_type="Room/share",
        metadata={"last_verified_at": stale},
    )
    result = score_listing(absent, preferences)
    assert result.score >= preferences.minimum_score, "the fixture home must be shortlisted"
    repository.upsert_listing(absent, result)
    source = CutShortButCanRecheck()
    scanner = Scanner(repository, lambda: preferences, [source], detail_delay_seconds=0)

    outcome = scanner.run_scan("scheduled")

    assert outcome.listings_added == 1, "what the read did return is stored"
    assert source.rechecked == [], source.rechecked


class ApartmentListOverFakes(ApartmentListSource):
    """The real Apartment List reader, answered by fakes instead of the scan's client."""

    def __init__(self, client) -> None:
        self.fake = client

    def search(self, client, preferences):
        return super().search(self.fake, preferences)

    def enrich(self, client, listing):
        return super().enrich(self.fake, listing)


class Routed:
    """Answers by URL."""

    def __init__(self, answers: dict[str, httpx.Response]) -> None:
        self.answers = answers
        self.requested: list[str] = []

    def get(self, url, **kwargs):
        self.requested.append(url)
        return self.answers[url]


def test_apartment_list_keeps_every_building_when_building_pages_fail(
    repository: Repository,
) -> None:
    """The search page is the collection and a building page only completes
    one entry in it. A refused, a rate-limited and a broken building page cost
    those three buildings their details and nothing else -- and the refused
    one keeps its card rather than a record of a page that said nothing."""
    from tests.test_free_sources import apartment_list_building

    base = "https://www.apartmentlist.com/ca/san-francisco"
    client = Routed(
        {
            ApartmentListSource.search_url: answer(base, 200, apartment_list_search()),
            f"{base}/parkmerced": answer(f"{base}/parkmerced", 200, apartment_list_building()),
            f"{base}/one-henry-adams": answer(f"{base}/one-henry-adams", 202),
            f"{base}/100-van-ness": answer(f"{base}/100-van-ness", 429),
            f"{base}/potrero-1010": answer(f"{base}/potrero-1010", 503),
        }
    )
    scanner = Scanner(
        repository, lambda: profile("one_bedroom"), [ApartmentListOverFakes(client)], detail_delay_seconds=0
    )

    outcome = scanner.run_scan("scheduled")

    with repository.connection() as connection:
        stored = {
            row["source_id"]: row["summary"]
            for row in connection.execute(
                "SELECT source_id, summary FROM listings WHERE platform = 'Apartment List'"
            )
        }
    assert set(stored) == {"parkmerced", "one-henry-adams", "100-van-ness", "potrero-1010"}
    assert outcome.sources_failed == 0
    assert "Cheapest available home" in stored["parkmerced"]
    for refused in ("one-henry-adams", "100-van-ness", "potrero-1010"):
        assert "unconfirmed" in stored[refused], (refused, stored[refused])
    run = next(run for run in repository.latest_source_runs() if run["platform"] == "Apartment List")
    assert (run["status"], run["message"]) == ("success", "Completed with 3 detail-page warning(s).")


def test_an_apartment_list_page_that_refused_confirms_nothing(tmp_path, preferences) -> None:
    """The recheck pass confirms a home by reading its page again. An empty 202
    was parsed as that page, and the home was stamped confirmed on the strength
    of a page that said nothing -- the fabricated confirmation date WO-3 took
    out of the recheck pass, arriving by another door."""
    from sf_housing.models import ListingCandidate

    repository = Repository(tmp_path / "db.sqlite3")
    repository.initialize()
    stale = (datetime.now(UTC) - timedelta(hours=72)).isoformat(timespec="seconds")
    url = "https://www.apartmentlist.com/ca/san-francisco/one-henry-adams"
    home = ListingCandidate(
        platform="Apartment List",
        source_id="one-henry-adams",
        title="Large Noe Valley bedroom in a shared home",
        original_url=url,
        price=1500,
        neighborhood="NOPA",
        summary="A private room in a shared home, laundry on site, flexible lease.",
        listing_type="Room/share",
        metadata={"last_verified_at": stale},
    )
    result = score_listing(home, preferences)
    assert result.score >= preferences.minimum_score, "the fixture home must be shortlisted"
    listing_id, _ = repository.upsert_listing(home, result)
    source = ApartmentListSource()
    scanner = Scanner(repository, lambda: preferences, [source], detail_delay_seconds=0)

    checked = scanner._recheck_absent(
        Scripted(answer(url, 202)),
        source,
        preferences,
        seen_source_ids=set(),
        deadline=scanner._clock() + 600,
    )

    assert checked == 0, "a refusal was counted as a confirmation"
    assert repository.listing(listing_id)["last_verified_at"] == stale
