from __future__ import annotations

import json
import os
import plistlib
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable

import httpx

from .apify import ApifyTokenStore
from .database import SCHEMA_VERSION, Repository
from .furnished_finder_bridge import BRIDGE_VERSION
from .liveness import next_run_label, schedule_health
from .freshness import evaluate_source_freshness, source_key as freshness_source_key
from .gmail_alerts import GmailAlertMailbox
from .preferences import PreferenceError, load_preferences
from .scanner import SLEPT_SCAN_MESSAGE, Scanner
from .scheduling import schedule_coverage
from .settings import Settings, _configured_path
from .sources import ListingSource


DIAGNOSTIC_STATUSES = {"pass", "attention", "blocked", "not_applicable", "unknown"}
_SECRET_KEY = re.compile(
    r"(?:authorization|client.?secret|oauth.?code|password|refresh.?token|access.?token|api.?token)",
    re.IGNORECASE,
)
_SECRET_VALUE = re.compile(
    r"(?:apify_api_[A-Za-z0-9_-]+|ya29\.[A-Za-z0-9._-]+|ghp_[A-Za-z0-9]+)",
    re.IGNORECASE,
)


def _now() -> datetime:
    return datetime.now(UTC)


def _redact_text(value: object) -> str:
    text = str(value)
    text = _SECRET_VALUE.sub("[redacted]", text)
    text = re.sub(r"([?&](?:code|state|token|key)=)[^&\s]+", r"\1[redacted]", text, flags=re.I)
    # A support report should be useful across Macs without exposing the local account name.
    text = re.sub(r"/Users/[^/\s]+", "/Users/[user]", text)
    return text[:1000]


def redact_metadata(value: object) -> object:
    """Recursively produce a small JSON-safe value with credential fields removed."""
    if isinstance(value, dict):
        clean: dict[str, object] = {}
        for key, item in value.items():
            name = str(key)[:80]
            clean[name] = "[redacted]" if _SECRET_KEY.search(name) else redact_metadata(item)
        return clean
    if isinstance(value, (list, tuple)):
        return [redact_metadata(item) for item in list(value)[:50]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact_text(value)


@dataclass(frozen=True, slots=True)
class DiagnosticCheck:
    key: str
    category: str
    status: str
    label: str
    explanation: str
    action: str
    owner: str = "You"
    metadata: dict[str, object] | None = None

    def __post_init__(self) -> None:
        if self.status not in DIAGNOSTIC_STATUSES:
            raise ValueError(f"Invalid diagnostic status: {self.status}")

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["metadata"] = redact_metadata(self.metadata or {})
        return result


@dataclass(frozen=True, slots=True)
class DiagnosticReport:
    generated_at: str
    overall: str
    headline: str
    summary: str
    checks: tuple[DiagnosticCheck, ...]

    @property
    def action_checks(self) -> tuple[DiagnosticCheck, ...]:
        return tuple(check for check in self.checks if check.status in {"blocked", "attention"})

    @property
    def passing_checks(self) -> tuple[DiagnosticCheck, ...]:
        return tuple(check for check in self.checks if check.status == "pass")

    @property
    def optional_checks(self) -> tuple[DiagnosticCheck, ...]:
        return tuple(
            check for check in self.checks if check.status in {"not_applicable", "unknown"}
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "generated_at": self.generated_at,
            "overall": self.overall,
            "headline": self.headline,
            "summary": self.summary,
            "checks": [check.to_dict() for check in self.checks],
        }


def _check(
    key: str,
    category: str,
    status: str,
    label: str,
    explanation: str,
    action: str,
    *,
    owner: str = "You",
    metadata: dict[str, object] | None = None,
) -> DiagnosticCheck:
    return DiagnosticCheck(
        key=key,
        category=category,
        status=status,
        label=label,
        explanation=_redact_text(explanation),
        action=_redact_text(action),
        owner=owner,
        metadata=metadata,
    )


def _application_checks(
    settings: Settings,
    repository: Repository,
    *,
    request_host: str,
    request_port: int,
    app_version: str,
) -> list[DiagnosticCheck]:
    checks: list[DiagnosticCheck] = []
    local = request_host.casefold() in {"127.0.0.1", "localhost", "::1", "testserver"}
    checks.append(
        _check(
            "loopback",
            "Application",
            "pass" if local else "blocked",
            "Private local dashboard" if local else "Dashboard is not local-only",
            (
                "The dashboard is reachable only from this Mac."
                if local
                else "The current request host is outside the supported local-only boundary."
            ),
            "Nothing to do." if local else "Run Repair, then open the dashboard from this Mac.",
            owner="Repair" if not local else "App",
            metadata={"host": request_host, "port": request_port},
        )
    )
    try:
        expected_port = int(os.environ.get("SF_HOUSING_PORT", "8000"))
    except ValueError:
        expected_port = 8000
    port_ok = request_port == expected_port
    checks.append(
        _check(
            "local_port",
            "Application",
            "pass" if port_ok else "attention",
            "Local dashboard port" if port_ok else "Dashboard opened on an unexpected port",
            (
                f"The verified SF Home Finder is answering on local port {request_port}."
                if port_ok
                else f"The app expected local port {expected_port}, but this page used {request_port}."
            ),
            "Nothing to do." if port_ok else "Close the other local app, then double-click Repair and Verify again.",
            owner="App" if port_ok else "Repair",
            metadata={"expected_port": expected_port, "observed_port": request_port},
        )
    )
    runtime_ok = sys.version_info >= (3, 11)
    checks.append(
        _check(
            "runtime",
            "Application",
            "pass" if runtime_ok else "blocked",
            "Application runtime" if runtime_ok else "Runtime needs repair",
            f"SF Home Finder {app_version} is running on Python {sys.version_info.major}.{sys.version_info.minor}.",
            "Nothing to do." if runtime_ok else "Double-click Repair SF Home Finder.",
            owner="Repair" if not runtime_ok else "App",
            metadata={"app_version": app_version, "python": f"{sys.version_info.major}.{sys.version_info.minor}"},
        )
    )

    try:
        data_exists = settings.data_dir.is_dir()
        writable = data_exists and os.access(settings.data_dir, os.R_OK | os.W_OK | os.X_OK)
        installed = (settings.data_dir.parent / "current" / "bin" / "python").is_file()
        mode = settings.data_dir.stat().st_mode & 0o777 if data_exists else 0
        private = not installed or mode & 0o077 == 0
    except OSError:
        data_exists = writable = private = False
    data_ok = data_exists and writable and private
    checks.append(
        _check(
            "private_storage",
            "Application",
            "pass" if data_ok else "blocked" if not writable else "attention",
            "Private local storage" if data_ok else "Local storage needs attention",
            (
                "Your profile, listings, decisions, and connector state stay in the app-owned data folder."
                if data_ok
                else "The app data folder is missing, unwritable, or broader than the installed privacy setting."
            ),
            "Nothing to do." if data_ok else "Double-click Repair SF Home Finder. Your data will be preserved.",
            owner="Repair" if not data_ok else "App",
            metadata={"readable": data_exists, "writable": writable, "private_permissions": private},
        )
    )

    try:
        integrity_ok, verdict = repository.integrity_check()
        schema = repository.schema_version()
    except Exception as exc:
        integrity_ok, verdict, schema = False, type(exc).__name__, -1
    database_ok = integrity_ok and schema == SCHEMA_VERSION
    checks.append(
        _check(
            "database",
            "Application",
            "pass" if database_ok else "blocked",
            "Housing history is healthy" if database_ok else "Housing history needs repair",
            (
                "SQLite passed its integrity check and is on the current schema."
                if database_ok
                else "The local database did not pass its integrity or migration check."
            ),
            "Nothing to do." if database_ok else "Stop using the app and run Repair before another scan.",
            owner="Repair" if not database_ok else "App",
            metadata={"integrity": verdict, "schema": schema, "expected_schema": SCHEMA_VERSION},
        )
    )
    return checks


def _profile_check(settings: Settings) -> DiagnosticCheck:
    try:
        preferences = load_preferences(settings.preferences_path)
    except (OSError, PreferenceError, ValueError) as exc:
        return _check(
            "deal_profile",
            "Your deal",
            "blocked",
            "Your deal could not be read",
            f"The saved deal is invalid: {type(exc).__name__}.",
            "Open Your deal and save it again. If that fails, run Repair.",
            owner="You",
        )
    if not preferences.profile_active:
        paths = len(preferences.deal_profile.enabled_paths)
        return _check(
            "deal_profile",
            "Your deal",
            "attention",
            "Finish Your deal",
            "The app is healthy, but scans remain paused until the housing deal is complete.",
            "Open Your deal, review the summary, and choose Save and find homes.",
            owner="You",
            metadata={"draft_paths": paths},
        )
    return _check(
        "deal_profile",
        "Your deal",
        "pass",
        "Your deal is active",
        "The same saved answers drive searches, eligibility, ranking, and explanations.",
        "Nothing to do.",
        owner="App",
        metadata={"enabled_paths": list(preferences.deal_profile.enabled_paths)},
    )


def _slot_label(slot: datetime) -> str:
    """A missed slot needs its date: "10am thu" is ambiguous across a week."""
    return slot.strftime("%-I%p %a %b %-d").replace("AM", "am").replace("PM", "pm")


def _schedule_check(
    repository: Repository,
    scanner: Scanner,
    scheduler: object,
    managed: bool,
    now: datetime,
) -> DiagnosticCheck:
    """Report scheduled checking from what the scheduler holds, never from a constant.

    The health endpoint used to print the schedule whether or not anything was
    keeping it, which meant a scheduler that died at start-up looked identical to
    a healthy one. This reads the same observation the dashboard shows.
    """
    # A week of slots needs a week of history, not the eight rows the state
    # check reads. Both come from one fetch so the two answers on this page can
    # never be computed from different facts.
    recent = repository.recent_scans(200)
    health = schedule_health(
        scheduler,
        recent[:8],
        scan_running=scanner.is_running,
        managed=managed,
        now=now,
    )
    coverage = schedule_coverage(recent, now=now)
    metadata = health.as_dict()
    metadata["coverage"] = {
        "due": coverage.due,
        "served": coverage.served,
        "missed": [slot.isoformat() for slot in coverage.missed],
        "since": coverage.since.isoformat(),
    }
    if health.state == "unmanaged":
        return _check(
            "schedule", "Scanning", "not_applicable",
            "Automatic checking is not run here",
            "This process was not asked to keep the twice-daily schedule.",
            "Nothing to do. The installed app keeps the schedule.",
            owner="App", metadata=metadata,
        )
    if health.state == "stopped":
        return _check(
            "schedule", "Scanning", "blocked",
            "Automatic checking is not running",
            "Nothing is scheduled, so the 10:00 and 18:00 checks will not happen "
            "and the shortlist will quietly stop updating.",
            "Double-click Repair SF Home Finder, then run this check again.",
            owner="Repair", metadata=metadata,
        )
    if health.state == "overdue":
        return _check(
            "schedule", "Scanning", "attention",
            "Checks are behind schedule",
            health.summary + " The Mac may have been asleep or offline at both times.",
            "Choose Check for new homes now. If it stays behind, double-click Repair.",
            owner="You", metadata=metadata,
        )
    if health.state == "not_yet":
        return _check(
            "schedule", "Scanning", "not_applicable",
            "Waiting for the first completed check",
            f"Automatic checking is scheduled; the next one runs at {next_run_label(health)}.",
            "Nothing to do.",
            owner="App", metadata=metadata,
        )
    # The schedule reports on itself rather than asking to be trusted. A run of
    # missed slots is the fault this whole mechanism exists to catch, and it is
    # invisible in "last checked an hour ago".
    if coverage.measurable and coverage.worth_raising(now=now):
        missed = ", ".join(_slot_label(slot) for slot in coverage.missed[:3])
        more = f" and {len(coverage.missed) - 3} more" if len(coverage.missed) > 3 else ""
        return _check(
            "schedule", "Scanning", "attention",
            "Some scheduled checks did not run",
            f"{coverage.summary()} Missed: {missed}{more}. "
            "This Mac was probably asleep or offline at those times.",
            "Nothing, if the Mac is often closed: a catch-up runs when it wakes. "
            "If it keeps happening while the Mac is awake, double-click Repair.",
            owner="You", metadata=metadata,
        )
    return _check(
        "schedule", "Scanning", "pass",
        "Checking on schedule",
        f"{health.summary} The next check runs at {next_run_label(health)}."
        + (f" {coverage.summary()}" if coverage.measurable else "")
        # A single old miss that has already been caught up is a fact worth
        # stating, not an alarm worth raising.
        + (
            f" The one missed was {_slot_label(coverage.missed[0])}, and it was caught up."
            if coverage.measurable and coverage.missed and not coverage.worth_raising(now=now)
            else ""
        ),
        "Nothing to do.",
        owner="App", metadata=metadata,
    )


def _scan_check(repository: Repository, scanner: Scanner, now: datetime) -> DiagnosticCheck:
    progress = scanner.progress
    if scanner.is_running or progress.get("running"):
        return _check(
            "scanner",
            "Scanning",
            "pass",
            "A source check is running",
            f"{progress.get('sources_completed', 0)} of {progress.get('sources_total', 0)} sources have finished.",
            "Leave this page open or return to the shortlist. Results save as they arrive.",
            owner="App",
            metadata={
                "status": progress.get("status"),
                "current_source": progress.get("current_source"),
                "percent": progress.get("percent"),
            },
        )
    scans = repository.recent_scans(1)
    if not scans:
        return _check(
            "scanner",
            "Scanning",
            "not_applicable",
            "No source check yet",
            "Your first check starts automatically after Your deal is saved.",
            "Finish Your deal. Optional connectors are not required.",
            owner="You",
        )
    latest = scans[0]
    if latest.get("status") == "running":
        try:
            started = datetime.fromisoformat(str(latest["started_at"]))
            if started.tzinfo is None:
                started = started.replace(tzinfo=UTC)
            stale = now - started.astimezone(UTC) > timedelta(minutes=10)
        except (KeyError, TypeError, ValueError):
            stale = True
        return _check(
            "scanner",
            "Scanning",
            "attention" if stale else "unknown",
            "An interrupted source check needs recovery" if stale else "Source check state is settling",
            "The database still records a running check, but no active worker is visible.",
            "Double-click Open. If this remains after one minute, run Repair.",
            owner="Repair",
            metadata={"scan_id": latest.get("id"), "started_at": latest.get("started_at")},
        )
    if latest.get("status") == "interrupted":
        # Quitting the app during a check is an ordinary thing to do, not a
        # fault to be repaired. Recovery settles the record on the way back up,
        # so by the time anyone reads this the only true statement left is that
        # the last check did not get to finish, and the next one is unaffected.
        return _check(
            "scanner",
            "Scanning",
            "pass",
            "Last check was interrupted",
            SLEPT_SCAN_MESSAGE
            if latest.get("message") == SLEPT_SCAN_MESSAGE
            else "The app was stopped before the last check finished. Nothing collected was lost.",
            "Nothing to do. The next check runs on schedule, or use Check for new homes now.",
            owner="App",
            metadata={"status": "interrupted", "scan_id": latest.get("id")},
        )
    failures = int(latest.get("sources_failed") or 0)
    completed = latest.get("status") in {"completed", "completed_with_errors"}
    return _check(
        "scanner",
        "Scanning",
        "pass" if completed and not failures else "attention",
        "Last source check completed" if completed and not failures else "Last check needs review",
        (
            f"The last check examined {int(latest.get('listings_seen') or 0)} listings with no source failures."
            if completed and not failures
            else f"The last check finished with {failures} source problem(s). Other sources still ran."
        ),
        "Nothing to do." if completed and not failures else "Review Source coverage below, then retry only the affected source.",
        owner="App" if completed and not failures else "Source",
        metadata={"status": latest.get("status"), "sources_failed": failures},
    )


def _no_account_sources(sources: Iterable[ListingSource]) -> str:
    """Name the sources that need no account, read from the list itself.

    Written by hand this sentence named four while seven ran, on the one screen
    a new install reads first.
    """
    names = [
        str(getattr(source, "platform", ""))
        for source in sources
        if getattr(source, "mode", "setup") == "automatic"
        and not getattr(source, "connector_key", None)
    ]
    if not names:
        return "The sources that need no account will be checked first."
    if len(names) == 1:
        return f"{names[0]} does not require an account."
    return f"{', '.join(names[:-1])} and {names[-1]} do not require an account."


def _source_check(repository: Repository, sources: Iterable[ListingSource] = ()) -> DiagnosticCheck:
    runs = repository.latest_source_runs()
    if not runs:
        return _check(
            "public_sources",
            "Sources",
            "not_applicable",
            "Public sources are ready for the first check",
            _no_account_sources(sources),
            "Finish Your deal to start the first check.",
            owner="You",
        )
    public = [
        run
        for run in runs
        if str(run.get("provider") or "").casefold()
        not in {"gmail", "gmail_alerts", "apify", "chrome_bridge"}
    ]
    failures = [run for run in public if run.get("status") == "error"]
    successes = [run for run in public if run.get("status") == "success"]
    if failures:
        names = [str(run.get("platform") or "Source") for run in failures[:4]]
        return _check(
            "public_sources",
            "Sources",
            "attention",
            "Some public sources need attention",
            f"{len(successes)} source(s) worked; {len(failures)} reported a problem.",
            "Open Sources for the exact error and retry action. Working sources remain usable.",
            owner="Source",
            metadata={"working": len(successes), "failed": names},
        )
    checked = sum(int(run.get("listings_seen") or 0) for run in successes)
    return _check(
        "public_sources",
        "Sources",
        "pass" if successes else "not_applicable",
        "Public sources are working" if successes else "Public sources have not run yet",
        (
            f"The latest successful source results checked {checked} listing(s). Zero inventory is reported separately from failure."
            if successes
            else "No public-source result has been recorded yet."
        ),
        "Nothing to do." if successes else "Run the first source check after saving Your deal.",
        owner="App" if successes else "You",
        metadata={"successful_sources": len(successes), "listings_checked": checked},
    )


def _source_freshness_checks(
    repository: Repository,
    sources: Iterable[ListingSource],
    now: datetime,
) -> list[DiagnosticCheck]:
    """Expose the existing source history one source at a time.

    The dashboard can keep showing useful last-known-good listings while one
    external site is unavailable.  These checks make that condition explicit
    without treating a successful zero-result scan as a failure.
    """
    checks: list[DiagnosticCheck] = []
    seen: set[str] = set()
    for source in sources:
        key = freshness_source_key(source)
        if key in seen:
            continue
        seen.add(key)
        health = evaluate_source_freshness(repository, source, now=now)
        # Setup-only connectors already have a single, clearer aggregate check
        # (Gmail/Apify/etc.).  Repeating six unconnected Gmail providers would
        # bury the public-source next action on a fresh install.
        if health.status == "optional":
            continue
        # A source one check passed over is still current -- anything older has
        # already become "stale" -- and its own action reads "Nothing to do", so
        # it is not listed among the things to fix. One the deal has no use for
        # is not a check that applies at all.
        status = (
            "pass"
            if health.status
            in {"working", "working_zero", "waiting_first_alert", "checking", "not_reached"}
            else "not_applicable"
            if health.status in {"not_run", "manual", "not_needed"}
            else "attention"
        )
        checks.append(
            _check(
                f"source:{key}",
                "Sources",
                status,
                health.label,
                health.explanation,
                health.action,
                owner="Source" if health.needs_attention else "App",
                metadata={
                    "platform": health.platform,
                    "provider": health.provider,
                    "last_success_at": health.last_success_at,
                    "failure_streak": health.failure_streak,
                    "next_retry_at": health.next_retry_at,
                    "latest_status": (health.latest_run or {}).get("status"),
                },
            )
        )
    return checks


def _connector_check(
    key: str,
    label: str,
    repository: Repository,
    *,
    configured: bool,
) -> DiagnosticCheck:
    state = repository.connector_state(key)
    if state is None or state.state == "disabled" or (
        not configured and state.state == "not_configured"
    ):
        return _check(
            key,
            "Optional connectors",
            "not_applicable",
            f"{label} is optional",
            f"{label} is not configured and does not block public-source results.",
            f"Open Sources only if you want {label} coverage.",
            owner="You",
        )
    if state and state.working:
        return _check(
            key,
            "Optional connectors",
            "pass",
            f"{label}: {state.label}",
            state.message or f"{label} completed its latest verification.",
            "Nothing to do." if state.state != "waiting_first_alert" else "Send or wait for the first matching saved-search alert.",
            owner="App" if state.state != "waiting_first_alert" else "You",
            metadata={
                "state": state.state,
                "last_attempt_at": state.last_attempt_at,
                "last_success_at": state.last_success_at,
                "observed_items": state.observed_items,
            },
        )
    return _check(
        key,
        "Optional connectors",
        "attention",
        f"{label}: {state.label if state else 'Ready to test'}",
        state.message if state and state.message else f"{label} setup needs one verification step.",
        f"Open Sources and follow the {label} recovery action.",
        owner="You",
        metadata={"state": state.state if state else "configured_unverified"},
    )


def _gmail_check(repository: Repository, mailbox: GmailAlertMailbox) -> DiagnosticCheck:
    configuration_error = mailbox.client_configuration_error
    if mailbox.client_secret_path.is_file() and configuration_error:
        return _check(
            "gmail",
            "Optional connectors",
            "attention",
            "Gmail owner setup needs repair",
            configuration_error,
            "Open Sources and replace the OAuth client with the owner-provided loopback client.",
            owner="You",
            metadata={"client_file_present": True, "token_file_present": mailbox.token_path.is_file()},
        )
    if mailbox.token_path.is_file() and not mailbox.is_connected:
        return _check(
            "gmail",
            "Optional connectors",
            "attention",
            "Gmail needs to reconnect",
            "A local authorization file exists, but it no longer proves read-only Gmail access.",
            "Open Sources, reconnect Gmail, then run Test saved-search alerts.",
            owner="You",
            metadata={"client_file_present": mailbox.has_client_secret, "token_file_present": True},
        )
    return _connector_check(
        "gmail",
        "Gmail alerts",
        repository,
        configured=mailbox.has_client_secret or mailbox.is_connected,
    )


def _apify_check(repository: Repository, tokens: ApifyTokenStore) -> DiagnosticCheck:
    base = _connector_check(
        "apify", "Facebook automation", repository, configured=tokens.is_configured
    )
    usage = tokens.usage_summary()
    metadata = {**(base.metadata or {}), "local_allowance": usage}
    if tokens.is_configured and not bool(usage.get("valid", True)):
        return _check(
            "apify",
            "Optional connectors",
            "attention",
            "Facebook allowance record needs repair",
            "The optional local allowance record is unreadable, so the app will fail closed instead of spending unexpectedly.",
            "Open Sources and retry the bounded Facebook test. Run Repair if it repeats.",
            owner="You",
            metadata=metadata,
        )
    allowance_reached = (
        int(usage.get("runs", 0)) >= 60
        or int(usage.get("group_posts", 0)) >= 300
        or int(usage.get("furnished_finder_listings", 0)) >= 300
    )
    if tokens.is_configured and allowance_reached:
        return _check(
            "apify",
            "Optional connectors",
            "attention",
            "Facebook allowance reached",
            "The local monthly cap stopped optional Apify work before another run could be sent.",
            "Wait for the next monthly window. Public sources and Gmail remain available.",
            owner="Source",
            metadata=metadata,
        )
    return _check(
        base.key,
        base.category,
        base.status,
        base.label,
        base.explanation,
        base.action,
        owner=base.owner,
        metadata=metadata,
    )


def _furnished_finder_check(settings: Settings, repository: Repository, now: datetime) -> DiagnosticCheck:
    bridge_dir = settings.data_dir.parent / "furnished-finder-bridge"
    state = repository.connector_state("furnished_finder")
    configured = state is not None and state.state not in {"not_configured", "disabled"}
    if not configured:
        return _connector_check(
            "furnished_finder", "Furnished Finder", repository, configured=False
        )
    manifest_path = bridge_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        installed_version = str(manifest.get("version") or "") if isinstance(manifest, dict) else ""
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        installed_version = ""
    observed_version = str((state.metadata or {}).get("version") or "") if state else ""
    if not bridge_dir.is_dir() or installed_version != BRIDGE_VERSION:
        return _check(
            "furnished_finder",
            "Optional connectors",
            "attention",
            "Furnished Finder bridge needs repair",
            "The configured Chrome bridge folder is missing or is not the bundled version.",
            "Double-click Repair, then reload the extension in Chrome and test it from Sources.",
            owner="Repair",
            metadata={"installed_version": installed_version or "missing", "expected_version": BRIDGE_VERSION},
        )
    if observed_version and observed_version != BRIDGE_VERSION:
        return _check(
            "furnished_finder",
            "Optional connectors",
            "attention",
            "Chrome is running an old bridge",
            "The last Chrome heartbeat came from a different bridge version.",
            "Reload the repaired extension in Chrome, then use Test bridge in Sources.",
            owner="You",
            metadata={"observed_version": observed_version, "expected_version": BRIDGE_VERSION},
        )
    if state and state.last_success_at:
        try:
            last_success = datetime.fromisoformat(state.last_success_at)
            if last_success.tzinfo is None:
                last_success = last_success.replace(tzinfo=UTC)
            stale = now - last_success.astimezone(UTC) > timedelta(hours=48)
        except (TypeError, ValueError):
            stale = True
        if stale:
            return _check(
                "furnished_finder",
                "Optional connectors",
                "attention",
                "Furnished Finder bridge has gone quiet",
                "The configured bridge has not completed a heartbeat in the last 48 hours.",
                "Open Chrome, open a saved Furnished Finder search, and choose Test bridge in Sources.",
                owner="You",
                metadata={"last_success_at": state.last_success_at, "version": observed_version},
            )
    return _connector_check(
        "furnished_finder", "Furnished Finder", repository, configured=True
    )


def _windows_startup_task_check(settings: Settings) -> DiagnosticCheck:
    """Verify the normal-user Windows task without claiming it is a service."""
    app_root = settings.data_dir.parent
    runtime = app_root / "current" / "Scripts" / "python.exe"
    if not runtime.is_file():
        return _check(
            "login_service",
            "Startup",
            "not_applicable",
            "Development session",
            "Windows startup checks apply to the installed friend release.",
            "Nothing to do in development.",
            owner="App",
        )
    try:
        result = subprocess.run(
            ["schtasks.exe", "/Query", "/TN", "SF Housing Monitor"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        installed = result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        installed = False
    return _check(
        "login_service",
        "Startup",
        "pass" if installed else "attention",
        "Starts when you sign in" if installed else "Windows startup needs verification",
        "The normal-user Windows startup task points to this private app runtime."
        if installed
        else "The app runtime exists, but Windows does not report its normal-user startup task.",
        "Nothing to do." if installed else "Double-click Repair SF Home Finder.",
        owner="App" if installed else "Repair",
    )


def _launch_agent_check(settings: Settings) -> DiagnosticCheck:
    if os.name == "nt":
        return _windows_startup_task_check(settings)
    app_root = settings.data_dir.parent
    installed = (app_root / "current" / "bin" / "python").is_file()
    if not installed:
        return _check(
            "login_service",
            "Startup",
            "not_applicable",
            "Development session",
            "Login-service checks apply to the installed friend release.",
            "Nothing to do in development.",
            owner="App",
        )
    # install.sh reads this as "${SF_HOUSING_LAUNCH_AGENTS_DIR:-$DEFAULT_LAUNCH_AGENTS_DIR}",
    # and the shell's ":-" falls back on an empty value as readily as on an absent one.
    # Reading it any other way here would leave plist_path relative and ask is_file()
    # about the directory the process happened to start in -- "/" under launchd -- and
    # report the login service missing on a Mac where it is installed and running, which
    # sends someone to Repair to fix a problem they do not have.
    launch_agents = _configured_path(
        "SF_HOUSING_LAUNCH_AGENTS_DIR", Path.home() / "Library" / "LaunchAgents"
    )
    plist_path = launch_agents / "com.sfhousing.monitor.plist"
    if not plist_path.is_file():
        return _check(
            "login_service",
            "Startup",
            "blocked",
            "Login service is missing",
            "The application runtime exists, but its macOS login service file is missing.",
            "Double-click Repair SF Home Finder.",
            owner="Repair",
        )
    try:
        with plist_path.open("rb") as handle:
            payload = plistlib.load(handle)
        arguments = payload.get("ProgramArguments", []) if isinstance(payload, dict) else []
        expected_runtime = (app_root / "current" / "bin" / "python").resolve()
        runtime_matches = any(
            Path(str(argument)).expanduser().resolve() == expected_runtime
            for argument in arguments
            if str(argument).startswith("/")
        )
        valid = (
            isinstance(arguments, list)
            and runtime_matches
            and "127.0.0.1" in arguments
        )
    except (OSError, plistlib.InvalidFileException, ValueError, TypeError):
        valid = False
    if not valid:
        return _check(
            "login_service",
            "Startup",
            "blocked",
            "Login service needs repair",
            "The generated macOS login service is invalid or points at the wrong runtime.",
            "Double-click Repair SF Home Finder.",
            owner="Repair",
        )
    if os.environ.get("SF_HOUSING_NO_LAUNCH_AGENT") == "1":
        return _check(
            "login_service",
            "Startup",
            "unknown",
            "Login start not exercised in this isolated check",
            "The generated service file is valid; isolated validation intentionally did not load it into a real login session.",
            "The normal installer loads this service. Run Verify after installing in the intended Mac account.",
            owner="App",
        )
    try:
        result = subprocess.run(
            ["/bin/launchctl", "print", f"gui/{os.getuid()}/com.sfhousing.monitor"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        loaded = result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        loaded = False
    return _check(
        "login_service",
        "Startup",
        "pass" if loaded else "attention",
        "Starts when you log in" if loaded else "Login start needs verification",
        "The macOS login service is loaded and points to the current runtime." if loaded else "The service file is valid but macOS does not report it loaded.",
        "Nothing to do." if loaded else "Double-click Repair SF Home Finder.",
        owner="App" if loaded else "Repair",
    )


def _probe_public_sources(sources: Iterable[ListingSource]) -> DiagnosticCheck:
    targets = []
    for source in sources:
        if getattr(source, "mode", "setup") != "automatic":
            continue
        if getattr(source, "connector_key", None):
            continue
        url = str(getattr(source, "search_url", ""))
        if url.startswith("https://"):
            targets.append((str(getattr(source, "platform", "Source")), url))
        if len(targets) >= 4:
            break
    if not targets:
        return _check(
            "public_connectivity",
            "Sources",
            "not_applicable",
            "No public probes available",
            "The current application composition has no account-free source URLs to probe.",
            "Use the normal source check instead.",
            owner="App",
        )
    outcomes: list[dict[str, object]] = []
    with httpx.Client(timeout=httpx.Timeout(2.0), follow_redirects=True) as client:
        for name, url in targets:
            try:
                response = client.get(url, headers={"Range": "bytes=0-4095"})
                reachable = response.status_code < 500
                outcomes.append({"source": name, "reachable": reachable, "status_code": response.status_code})
            except httpx.HTTPError:
                outcomes.append({"source": name, "reachable": False})
    working = sum(1 for outcome in outcomes if outcome["reachable"])
    status = "pass" if working == len(outcomes) else "attention" if working else "unknown"
    return _check(
        "public_connectivity",
        "Sources",
        status,
        "Public source connections responded" if status == "pass" else "Some public connections did not respond",
        f"{working} of {len(outcomes)} bounded source probes responded without a server failure.",
        "Nothing to do." if status == "pass" else "Check internet access, then run the normal source check. A probe does not change listings.",
        owner="Source",
        metadata={"probes": outcomes},
    )


# The app's own machinery, as opposed to the weather outside it. A listings
# site being paused says nothing about whether this app is working; its
# storage, its runtime, its deal and the way it starts say everything.
CORE_CATEGORIES = frozenset({"Application", "Your deal", "Startup"})


def nothing_is_broken(report: DiagnosticReport) -> bool:
    """Whether the app is working, in the sense that matters to somebody using it.

    Deliberately not ``report.overall == "ready"``, which was the first attempt
    and was wrong in a way only a real board showed. "Ready" means every one of
    the thirty-odd checks passed, and a third of them are per-source: measured
    against the author's own install -- nine thousand homes, working perfectly,
    twenty-three sites read twice a day -- Zillow wanted attention, RentSFNow
    was paused for the hour, and the last scan was flagged for review because
    of exactly those two. That is an ordinary Tuesday for a scraper, and a gate
    on "ready" would have been a gate that never opened.

    The report already draws the line this needs, and says so in its own words.
    Anything ``blocked`` is a required check failing. An unfinished deal means
    there is not yet an app here to have an opinion about. Everything else it
    calls attention, under a summary that reads "the core app remains usable".
    So: broken is this app's own machinery being broken, and a listings site
    having a bad morning is not that.
    """
    if report.overall == "setup_incomplete":
        return False
    return not any(
        check.status == "blocked"
        or (check.status in {"attention", "unknown"} and check.category in CORE_CATEGORIES)
        for check in report.checks
    )


def run_diagnostics(
    settings: Settings,
    repository: Repository,
    scanner: Scanner,
    mailbox: GmailAlertMailbox,
    apify_tokens: ApifyTokenStore,
    *,
    request_host: str = "127.0.0.1",
    request_port: int = 8000,
    app_version: str = "unknown",
    scheduler: object = None,
    scheduler_managed: bool = False,
    sources: Iterable[ListingSource] = (),
    include_connectivity: bool = False,
    now: datetime | None = None,
) -> DiagnosticReport:
    """Build one deterministic, read-only readiness report from existing state."""
    current = now or _now()
    source_list = tuple(sources)
    try:
        checks = _application_checks(
            settings,
            repository,
            request_host=request_host,
            request_port=request_port,
            app_version=app_version,
        )
    except Exception as exc:
        checks = [
            _check(
                "application",
                "Application",
                "unknown",
                "Application readiness could not be read",
                f"The check stopped safely at {type(exc).__name__}.",
                "Run Repair, then run the Ready Check again.",
                owner="Repair",
            )
        ]

    def append_guarded(
        factory: Callable[[], DiagnosticCheck],
        *,
        key: str,
        category: str,
        label: str,
        action: str,
        owner: str = "Repair",
    ) -> None:
        try:
            checks.append(factory())
        except Exception as exc:
            checks.append(
                _check(
                    key,
                    category,
                    "unknown",
                    label,
                    f"This read-only check stopped safely at {type(exc).__name__}; other checks continued.",
                    action,
                    owner=owner,
                )
            )

    append_guarded(
        lambda: _profile_check(settings),
        key="deal_profile",
        category="Your deal",
        label="Your deal could not be verified",
        action="Open Your deal and save it again. Run Repair if it repeats.",
        owner="You",
    )
    append_guarded(
        lambda: _schedule_check(repository, scanner, scheduler, scheduler_managed, current),
        key="schedule",
        category="Scanning",
        label="Automatic checking could not be verified",
        action="Run Repair, then retry the Ready Check.",
    )
    append_guarded(
        lambda: _scan_check(repository, scanner, current),
        key="scanner",
        category="Scanning",
        label="Scanner state could not be verified",
        action="Run Repair, then retry the Ready Check.",
    )
    append_guarded(
        lambda: _source_check(repository, source_list),
        key="public_sources",
        category="Sources",
        label="Public-source history could not be verified",
        action="Run Repair, then retry the public-source check.",
    )
    append_guarded(
        lambda: _gmail_check(repository, mailbox),
        key="gmail",
        category="Optional connectors",
        label="Gmail state could not be verified",
        action="Open Sources and reconnect Gmail only if you want email coverage.",
        owner="You",
    )
    try:
        checks.extend(_source_freshness_checks(repository, source_list, current))
    except Exception as exc:
        checks.append(
            _check(
                "source_freshness",
                "Sources",
                "unknown",
                "Individual source freshness could not be verified",
                f"This read-only source history check stopped safely at {type(exc).__name__}.",
                "Run the Ready Check again. Existing listings and other source checks were not changed.",
                owner="App",
            )
        )
    append_guarded(
        lambda: _apify_check(repository, apify_tokens),
        key="apify",
        category="Optional connectors",
        label="Facebook automation state could not be verified",
        action="Open Sources and retry only if you want Facebook coverage.",
        owner="You",
    )
    append_guarded(
        lambda: _furnished_finder_check(settings, repository, current),
        key="furnished_finder",
        category="Optional connectors",
        label="Furnished Finder state could not be verified",
        action="Open Sources and run Test bridge only if you want this coverage.",
        owner="You",
    )
    append_guarded(
        lambda: _launch_agent_check(settings),
        key="login_service",
        category="Startup",
        label="Login start could not be verified",
        action="Double-click Repair, then run Verify again.",
    )
    if include_connectivity:
        append_guarded(
            lambda: _probe_public_sources(source_list),
            key="public_connectivity",
            category="Sources",
            label="Public connections could not be verified",
            action="Check internet access and retry. No scan or allowance was consumed.",
            owner="Source",
        )

    blocking = [check for check in checks if check.status == "blocked"]
    attention = [check for check in checks if check.status == "attention"]
    uncertain_core = [
        check
        for check in checks
        if check.status == "unknown" and check.category in {"Application", "Your deal", "Scanning"}
    ]
    profile_incomplete = any(check.key == "deal_profile" and check.status == "attention" for check in checks)
    if blocking:
        overall = "needs_attention"
        headline = "Repair before the next scan"
        summary = f"{len(blocking)} required check(s) are blocked. Your existing data has not been changed."
    elif profile_incomplete:
        overall = "setup_incomplete"
        headline = "Finish Your deal"
        summary = "The application is healthy. Save the housing deal to start public-source discovery."
    elif attention:
        overall = "needs_attention"
        headline = "One setup needs attention"
        summary = f"The core app remains usable; {len(attention)} configured item(s) need a next step."
    elif uncertain_core:
        overall = "needs_attention"
        headline = "Run the ready check again"
        summary = "A required read-only check could not finish. Your data was not changed."
    else:
        overall = "ready"
        headline = "Ready to find homes"
        summary = "The application, housing deal, storage, and latest public-source state are ready."
    return DiagnosticReport(
        generated_at=current.astimezone(UTC).isoformat(timespec="seconds"),
        overall=overall,
        headline=headline,
        summary=summary,
        checks=tuple(checks),
    )


# The one place the support contact is configured.
#
# A failing check states its own next step, but some problems are not on that
# list, and a support page with no way to ask anybody anything is a dead end.
# Put your own address here when you fork this; leave it empty and the contact
# block does not render at all, so a release can never ship somebody else's
# inbox.
_DEFAULT_SUPPORT_EMAIL = "henrymil@usc.edu"

SUPPORT_EMAIL = os.environ.get("SF_HOUSING_SUPPORT_EMAIL", _DEFAULT_SUPPORT_EMAIL).strip()


def report_json(report: DiagnosticReport) -> str:
    return json.dumps(report.to_dict(), indent=2, ensure_ascii=False)
