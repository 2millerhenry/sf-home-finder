"""A single, derived truth about whether a listing source is still current.

This module intentionally stores nothing.  A source's durable scan history and
connector state already exist in SQLite; the watchdog only turns those facts
into a clear user-facing state and a safe retry decision.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from .connectors import ConnectorStatus

if TYPE_CHECKING:
    from .database import Repository
    from .sources import ListingSource


# The scanner normally runs at 10:00 and 18:00 Pacific.  Twenty-six hours
# leaves room for sleep, a missed slot, and normal network variance, while
# still surfacing a truly stale source within one day.
FRESHNESS_WINDOW = timedelta(hours=26)
BACKOFF_AFTER_FAILURES = 2
BACKOFF_BASE = timedelta(hours=6)
BACKOFF_MAX = timedelta(hours=24)


def source_key(source: "ListingSource") -> str:
    base = str(getattr(source, "source_key", source.__class__.__name__))
    # Display labels are not identities.  Gmail alerts and an optional Apify
    # fallback can both call themselves Facebook Marketplace, but must never
    # be allowed to overwrite each other's source history.
    return f"{base}::{source_provider(source)}"


def source_provider(source: "ListingSource") -> str:
    return str(getattr(source, "last_provider", getattr(source, "provider", source.__class__.__name__)))


def source_connector_state_key(source: "ListingSource") -> str | None:
    value = getattr(source, "connector_state_key", None)
    if value:
        return str(value)
    value = getattr(source, "connector_key", None)
    return str(value) if value else None


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


# Everything else this product says a time in -- the two daily checks, the list
# of recent checks beside this one -- is Pacific. Source health alone answered
# in UTC, so the panel put "9:13 AM UTC" next to a column headed "Pacific time"
# and left the reader to do the arithmetic. Defined here because this is the
# leaf; scheduling re-exports it.
PACIFIC = ZoneInfo("America/Los_Angeles")


# Errors reach the page as "SourceError: Trulia turned away ..." or
# "ReadTimeout: The read operation timed out". The class name in front is the
# one part of that a reader gains nothing from.
_RAISED_BY = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception|Timeout|TimedOut):\s*")


# Losing the network fails every source at once, and what the resolver hands
# back is C library vocabulary: "[Errno 8] nodename nor servname provided, or
# not known" on macOS, "Name or service not known" on Linux. Printed as the
# reason a source is paused it reads like a crash in the app, when the app and
# the site are both fine and there is simply nothing to reach. Matched on the
# resolver's own wording rather than the exception class, because httpx wraps
# the same failure in ConnectError, ConnectTimeout and ProxyError depending on
# how the machine is configured.
#
# Resolution is only the first way a laptop fails to reach the internet. A VPN
# that drops, a proxy the machine is still pointed at, a cable pulled, a
# firewall that answers for the network -- each of those resolves the name and
# then cannot open the socket, and hands back "[Errno 61] Connection refused",
# "[Errno 51] Network is unreachable" or "[Errno 65] No route to host". Those
# reached the panel verbatim, seventeen rows of them, while the DNS spelling of
# the same outage read as one plain sentence. A site that is up does not refuse
# the connection; it answers with an HTTP status, and that path is untouched.
#
# Kept as a list rather than a bare pattern because the scan budget asks the
# same question of the same strings -- "did anything actually leave this
# machine?" -- and two spellings of that would drift apart.
NO_NETWORK_PHRASES = (
    "nodename nor servname",
    "name or service not known",
    "temporary failure in name resolution",
    "failed to resolve",
    "getaddrinfo failed",
    "no address associated with hostname",
    "connection refused",
    "network is unreachable",
    "no route to host",
    "network is down",
)
_NO_NETWORK = re.compile("|".join(re.escape(phrase) for phrase in NO_NETWORK_PHRASES), re.IGNORECASE)


def _first_sentence(message: str, platform: str = "") -> str:
    """The cause, without the traceback vocabulary or the paragraph after it.

    The platform is dropped from the front for the same reason the labels no
    longer carry it: the row it appears in is already headed by that name, and
    "Trulia: Trulia turned away ..." is the name twice.
    """
    text = _RAISED_BY.sub("", (message or "").strip())
    if _NO_NETWORK.search(text):
        return "No internet connection reached this site."
    head = text.split(". ")[0].strip().rstrip(".")
    if platform and head.startswith(f"{platform} "):
        head = head[len(platform) + 1 :]
        head = head[:1].upper() + head[1:]
    return f"{head}." if head else ""


def _format_time(value: datetime | None) -> str:
    return value.astimezone(PACIFIC).strftime("%b %-d at %-I:%M %p") if value else "an unknown time"


# Sentence case throughout, and never a bare colour: each of these is the whole
# of what a healthy row says, so it has to say it in words.
SHORT_LABELS = {
    "working": "Current",
    "working_zero": "No matches",
    "waiting_first_alert": "Waiting for the first alert",
    "checking": "Checking",
    "attention": "Needs attention",
    "stale": "Stale",
    "backoff": "Paused",
    "not_reached": "Not checked this time",
    "not_needed": "Not needed for your deal",
    "manual": "Needs setup",
    "optional": "Optional",
    "not_run": "Not run yet",
}


@dataclass(frozen=True, slots=True)
class SourceFreshness:
    """Read-only source health, derived from the app's existing evidence."""

    key: str
    platform: str
    provider: str
    status: str
    label: str
    explanation: str
    action: str
    latest_run: dict[str, Any] | None
    last_success_at: str | None
    failure_streak: int = 0
    next_retry_at: str | None = None
    listings_seen: int = 0
    reason: str = ""

    @property
    def panel_note(self) -> str:
        """Why this source is not working, for the panel that shows the state.

        The panel said a source was paused and when it would retry, and never
        once said what had gone wrong -- so the only question it reliably
        provoked was the one it did not answer.
        """
        return " ".join(part for part in (self.reason, self.action) if part)

    @property
    def short_label(self) -> str:
        """The state on its own, for a list that already shows the name.

        The System status panel prints the platform beside this, so the long
        label read "Craigslist is current" one column away from "Craigslist" --
        and the repetition was long enough to wrap the badges into each other.
        The long labels stay as they are: the Ready Check lists checks flat,
        where each one has to name its own subject.
        """
        return SHORT_LABELS.get(self.status, self.status.replace("_", " ").capitalize())

    @property
    def needs_attention(self) -> bool:
        return self.status in {"attention", "stale", "backoff"}

    @property
    def is_current(self) -> bool:
        return self.status in {"working", "working_zero", "waiting_first_alert"}


def _backoff_until(last_error: datetime | None, failure_streak: int) -> datetime | None:
    if last_error is None or failure_streak < BACKOFF_AFTER_FAILURES:
        return None
    multiplier = 2 ** min(failure_streak - BACKOFF_AFTER_FAILURES, 2)
    return last_error + min(BACKOFF_BASE * multiplier, BACKOFF_MAX)


def _optional_result(
    source: "ListingSource", key: str, provider: str, connector: ConnectorStatus | None
) -> SourceFreshness | None:
    connector_key = getattr(source, "connector_key", None)
    mode = str(getattr(source, "mode", "setup"))
    platform = str(getattr(source, "platform", "Source"))
    if not connector_key:
        if mode == "automatic":
            return None
        return SourceFreshness(
            key,
            platform,
            provider,
            "manual",
            f"{platform} needs setup",
            getattr(source, "manual_reason", None)
            or f"{platform} is a manual source and is not part of automatic checks.",
            "Open this source directly when you want its additional coverage.",
            None,
            None,
        )
    if mode == "automatic":
        return None
    # Connector setup itself has one aggregate Doctor check.  Individual
    # providers become visible once the connector is actually able to scan;
    # otherwise a fresh install would show six copies of the same Gmail action.
    return SourceFreshness(
        key,
        platform,
        provider,
        "optional",
        f"{platform} is optional",
        getattr(source, "manual_reason", None)
        or f"{platform} is not configured and does not block your public-source shortlist.",
        "Open Sources only if you want this additional coverage.",
        None,
        None,
    )


def evaluate_source_freshness(
    repository: "Repository", source: "ListingSource", *, now: datetime | None = None
) -> SourceFreshness:
    """Derive a truthful source state without doing network work or mutating data."""
    current = (now or datetime.now(UTC)).astimezone(UTC)
    key = source_key(source)
    platform = str(getattr(source, "platform", "Source"))
    provider = source_provider(source)
    connector_key = source_connector_state_key(source)
    connector = repository.connector_state(connector_key) if connector_key else None
    optional = _optional_result(source, key, provider, connector)
    if optional is not None:
        return optional

    history = repository.source_run_history(source_key=key, platform=platform)
    latest = history[0] if history else None
    terminal = [run for run in history if str(run.get("status") or "") in {"success", "error"}]
    window_success = next((run for run in terminal if run.get("status") == "success"), None)
    last_success = window_success
    if last_success is None:
        # The history read above is deliberately bounded, and a source that is
        # failing fills it fastest: every deferred automatic attempt writes its
        # own "backoff" row, so two a day push the last good run out of the
        # window within a few days. Redfin worked eighteen times and last
        # succeeded on Sep 8; by Sep 17 the window held nothing but backoff and
        # error, and Support told the reader "No successful result has been
        # recorded yet" about a source that had been working the week before.
        # Ask the database for the last success directly rather than inferring
        # its absence from a window that cannot see that far back.
        last_success = repository.last_successful_source_run(source_key=key, platform=platform)
    last_success_at = str(last_success.get("finished_at") or "") or None if last_success else None

    failure_streak = 0
    last_error_at: datetime | None = None
    last_error: dict[str, Any] | None = None
    # Only a failure a site gave counts towards pausing it. With no network
    # nothing reached the site, and there is nothing to back off from: a Mac
    # waking before its Wi-Fi, twice, paused every source for hours, and the
    # checks after the network came back read nothing. The streak shown still
    # counts every failure.
    site_failures = 0
    last_site_error_at: datetime | None = None
    for run in terminal:
        if run.get("status") != "error":
            break
        failure_streak += 1
        when = _parse_time(run.get("finished_at") or run.get("started_at"))
        if last_error_at is None:
            last_error_at = when
            last_error = run
        if not _NO_NETWORK.search(str(run.get("message") or "")):
            site_failures += 1
            if last_site_error_at is None:
                last_site_error_at = when
    if window_success is None:
        # A window that reaches back to no success cannot say how long the
        # failing has gone on either, for the reason above: Redfin had failed
        # fourteen checks since 8 September and was described as "4
        # consecutive checks failed", because backoff rows had pushed the other
        # ten out of sight. Every failure since the last success is counted
        # instead -- the whole history for a source that has never had one.
        failure_streak, newest_error = repository.source_failures_since(
            source_key=key,
            platform=platform,
            after_id=int(last_success.get("id") or 0) if last_success else 0,
        )
        if newest_error is not None:
            last_error = newest_error
            last_error_at = _parse_time(newest_error.get("finished_at") or newest_error.get("started_at"))
        site_failures, newest_site_error = repository.source_failures_since(
            source_key=key,
            platform=platform,
            after_id=int(last_success.get("id") or 0) if last_success else 0,
            reached_only=True,
        )
        last_site_error_at = (
            _parse_time(newest_site_error.get("finished_at") or newest_site_error.get("started_at"))
            if newest_site_error is not None
            else None
        )
    retry_at = _backoff_until(last_site_error_at, site_failures)
    latest_status = str(latest.get("status") or "") if latest else ""
    success_time = _parse_time(last_success_at)

    # A newly inserted source-run is intentionally visible before network work
    # begins.  It must not erase the persisted repeated-failure evidence used
    # to decide whether this automatic attempt should be deferred.
    if latest_status == "running" and not failure_streak:
        return SourceFreshness(
            key,
            platform,
            provider,
            "checking",
            f"{platform} is checking",
            "This source is part of the active scan. Its last known listing results remain available.",
            "Wait for the current check to finish.",
            latest,
            last_success_at,
            failure_streak,
            retry_at.isoformat() if retry_at else None,
        )

    if latest_status == "not_needed":
        # Whatever this source did before, the deal no longer has a use for it
        # and it is not being asked, which is the one thing worth saying. Not a
        # failure, not "no matches", and not a count from an older deal.
        reason = str((latest or {}).get("message") or "").strip()
        return SourceFreshness(
            key,
            platform,
            provider,
            "not_needed",
            f"{platform} is not needed for your deal",
            reason or f"Your deal has no use for anything {platform} lists, so it was not asked.",
            "Nothing to do. It is read again once Your deal includes what it lists.",
            latest,
            last_success_at,
            reason=reason,
        )

    if failure_streak:
        # Deliberately the last run that actually failed, not the last run.
        # A source deferred by backoff records its own deferral notice as that
        # run's message, so reading "latest" here quoted "Retries automatically
        # after ..." back as the reason the source was failing.
        failed = last_error or latest or {}
        raw = str(failed.get("message") or "The latest source request did not complete.")
        # The panel already reads this through _first_sentence; the Ready Check
        # printed the same field raw, so the one page headed "Is everything
        # working?" was where "ConnectError: [Errno 8] nodename nor servname
        # provided, or not known" was shown to the person least able to act on
        # it -- while the dashboard, from the same run, said "No internet
        # connection reached this site."
        message = _first_sentence(raw, platform) or raw
        # A read that stopped part way stores what it had read and is then
        # recorded as the failure it was (PartialReadError), with the counts of
        # what it wrote. "The latest check failed" hid those homes, and they are
        # the difference between a site that is down and one rationing us.
        kept = int(failed.get("listings_added") or 0) + int(failed.get("listings_updated") or 0)
        if kept:
            message = f"{message} The {kept} listing(s) read before that were kept."
            last_good = (
                f" Its last complete check was {_format_time(success_time)}."
                if success_time
                else " It has not completed a check yet."
            )
        else:
            last_good = (
                f" The last good result is preserved from {_format_time(success_time)}."
                if success_time
                else " No successful result has been recorded yet."
            )
        if retry_at and current < retry_at:
            return SourceFreshness(
                key,
                platform,
                provider,
                "backoff",
                f"{platform} is paused briefly",
                f"{failure_streak} consecutive checks failed. {message}{last_good}",
                f"Retries automatically after {_format_time(retry_at)}, or use Check for new homes now.",
                latest,
                last_success_at,
                failure_streak,
                retry_at.isoformat(),
                reason=message,
            )
        return SourceFreshness(
            key,
            platform,
            provider,
            # Not stale when its latest check brought homes in, however long
            # ago its last complete one was.
            "attention"
            if kept or (success_time and current - success_time <= FRESHNESS_WINDOW)
            else "stale",
            f"{platform} needs attention",
            f"The latest check {'was cut short' if kept else 'failed'}. {message}{last_good}",
            "Use Check for new homes once. The next scheduled check will also retry this source without blocking the others.",
            latest,
            last_success_at,
            failure_streak,
            retry_at.isoformat() if retry_at else None,
            reason=message,
        )

    if success_time:
        age = current - success_time
        # From the run this state is actually describing, never from ``latest``.
        # A run the scan dropped for time is written with listings_seen = 0, so
        # reading the count from the newest row of any status turned "Rent.com
        # checked 90 homes" into the badge "No matches" the moment the next
        # scan ran out of its 240 seconds before reaching it.
        seen = int((last_success or latest or {}).get("listings_seen") or 0)
        if age > FRESHNESS_WINDOW:
            return SourceFreshness(
                key,
                platform,
                provider,
                "stale",
                f"{platform} is stale",
                f"Its last successful result was {_format_time(success_time)}, more than {int(FRESHNESS_WINDOW.total_seconds() // 3600)} hours ago. Existing listings remain available but may be old.",
                "Use Check for new homes now. If it stays stale, open Sources for the direct source link and run the Ready Check again.",
                latest,
                last_success_at,
            )
        if latest_status == "skipped":
            # Only "success" and "error" counted as terminal above, so a source
            # the scan never opened kept whatever it said before -- while the
            # scan list beside it was already reporting one source not reached.
            # Not an alert: nothing is broken. It simply must not claim a check
            # that did not happen.
            #
            # Nor may it guess why. A check passes a source over for time, but
            # also because it was read minutes ago (Zillow's quarter-hour floor)
            # or is still being read by the check before, and this said "ran
            # out of time" for all three -- false for the other two, and the
            # floor is the one somebody meets by pressing Check twice. The scan
            # wrote down its reason when it made the decision; that is what is
            # quoted.
            reason = str((latest or {}).get("message") or "").strip()
            return SourceFreshness(
                key,
                platform,
                provider,
                "not_reached",
                f"{platform} was not read by the last check",
                f"The last check did not read {platform}."
                + (f" {reason}" if reason else "")
                + f" Its most recent successful check was {_format_time(success_time)}"
                + (f" and found {seen} listing(s)." if seen else " and found no matching listings."),
                "Nothing to do. The next check tries it again.",
                latest,
                last_success_at,
                listings_seen=seen,
                reason=reason,
            )
        if seen == 0:
            # "Working, no matches" is a true and useful thing to say about a
            # source that reads a page and finds nothing today. It is not true
            # of an alert source that has never once had an alert to read: the
            # connector record for it says waiting_first_alert, and HotPads,
            # Apartments.com and Roomies each sat on seventy-odd consecutive
            # zero runs saying "This is a valid result, not a failure" -- which
            # invites the reader to conclude those sites have nothing, when
            # what is missing is the saved search that feeds them.
            if connector is not None and connector.state == "waiting_first_alert":
                return SourceFreshness(
                    key,
                    platform,
                    provider,
                    "waiting_first_alert",
                    f"{platform} is waiting for its first alert",
                    f"The check ran at {_format_time(success_time)}, but no {platform} alert email has arrived yet, so this source has contributed nothing so far. Save a {platform} search with email alerts turned on.",
                    "Open Sources to finish setting up this alert, or ignore it if you do not want this source.",
                    latest,
                    last_success_at,
                    listings_seen=seen,
                )
            return SourceFreshness(
                key,
                platform,
                provider,
                "working_zero",
                f"{platform} is working, no matches",
                f"The latest source check completed at {_format_time(success_time)} and found no matching listings. This is a valid result, not a failure.",
                "Nothing to do. The next scheduled check will look again.",
                latest,
                last_success_at,
                listings_seen=seen,
            )
        return SourceFreshness(
            key,
            platform,
            provider,
            "working",
            f"{platform} is current",
            f"The latest source check completed at {_format_time(success_time)} and checked {seen} listing(s).",
            "Nothing to do.",
            latest,
            last_success_at,
            listings_seen=seen,
        )

    return SourceFreshness(
        key,
        platform,
        provider,
        "not_run",
        f"{platform} has not run yet",
        "No source result has been recorded yet.",
        "Use Check for new homes to run the first bounded check.",
        latest,
        None,
    )


def source_is_in_backoff(repository: "Repository", source: "ListingSource", *, now: datetime | None = None) -> SourceFreshness | None:
    """Return an active automatic backoff, if any. Manual checks intentionally bypass it."""
    health = evaluate_source_freshness(repository, source, now=now)
    return health if health.status == "backoff" else None
