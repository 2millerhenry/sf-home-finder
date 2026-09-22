from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any


CONNECTOR_STATES = {
    "not_configured",
    "configured_unverified",
    "checking",
    "working",
    "working_zero",
    "waiting_first_alert",
    "degraded",
    "authorization_expired",
    "quota_blocked",
    "disabled",
}

GMAIL_PROVIDERS = (
    ("gmail:zillow", "Zillow"),
    ("gmail:hotpads", "HotPads"),
    ("gmail:apartments-com", "Apartments.com"),
    ("gmail:zumper", "Zumper"),
    ("gmail:roomies", "Roomies"),
    ("gmail:facebook-marketplace", "Facebook Marketplace"),
)


def gmail_provider_key(platform: str) -> str:
    for key, label in GMAIL_PROVIDERS:
        if platform.casefold() == label.casefold():
            return key
    normalized = platform.casefold().replace(".", "")
    slug = "-".join(part for part in normalized.replace("/", " ").split() if part)
    return f"gmail:{slug}"


@dataclass(frozen=True, slots=True)
class ConnectorStatus:
    key: str
    state: str
    configured_at: str | None = None
    last_attempt_at: str | None = None
    last_success_at: str | None = None
    observed_items: int = 0
    message: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def working(self) -> bool:
        return self.state in {"working", "working_zero", "waiting_first_alert"}

    @property
    def next_step(self) -> str:
        """What the person should do about this connector, in their words.

        The message field records what the last check found. This records
        where that leaves them, which is the question a status badge alone
        never answers.
        """
        return {
            "not_configured": "Not set up yet. Follow the steps on this card to start.",
            "configured_unverified": "Saved, but nothing has been checked yet. Run the test on this card.",
            "checking": "Checking now. This usually takes under a minute, and the result appears here on its own.",
            "working": "Working. Nothing more to do.",
            "working_zero": "Working. The check reached the source and found nothing matching your deal, which is an answer rather than a failure.",
            "waiting_first_alert": "Connected. Waiting for the first alert to arrive.",
            "degraded": "The last check did not finish. Run it again; if it keeps failing, the line above says why.",
            "authorization_expired": "The connection is no longer accepted. Set it up again on this card.",
            "quota_blocked": "This month's allowance is used up. It resets at the start of next month.",
            "disabled": "Turned off.",
        }[self.state]

    @property
    def needs_action(self) -> bool:
        """Whether the next step asks the reader to do something.

        A working connector's next step only restates its result, and two
        lines saying the same thing read as a page unsure of its own answer.
        """
        return self.state not in {"working", "working_zero", "waiting_first_alert", "checking"}

    @property
    def label(self) -> str:
        return {
            "not_configured": "Optional",
            "configured_unverified": "Ready to test",
            "checking": "Testing",
            "working": "Working",
            "working_zero": "Working, no matches",
            "waiting_first_alert": "Waiting for first alert",
            "degraded": "Attention",
            "authorization_expired": "Reconnect",
            "quota_blocked": "Allowance reached",
            "disabled": "Optional",
        }[self.state]


# A scan interrupted by a quit or a crash never writes its result, so the
# "checking" row it left behind would claim to be testing for good.
CHECK_STALE_AFTER_SECONDS = 20 * 60


def resolve_stalled_check(status: ConnectorStatus, *, now: datetime) -> ConnectorStatus:
    """Report an abandoned check as unfinished rather than still running."""
    if status.state != "checking" or not status.last_attempt_at:
        return status
    try:
        started = datetime.fromisoformat(status.last_attempt_at)
    except ValueError:
        return status
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    if (now - started).total_seconds() <= CHECK_STALE_AFTER_SECONDS:
        return status
    return replace(
        status,
        state="degraded",
        message="The last check was interrupted before it reported anything. Run it again.",
    )


def connector_state_for_error(message: str) -> str:
    normalized = message.casefold()
    if any(term in normalized for term in ("quota", "allowance", "monthly cap", "credit")):
        return "quota_blocked"
    if any(term in normalized for term in ("authorization", "oauth", "expired", "reconnect")):
        return "authorization_expired"
    return "degraded"


def aggregate_gmail_status(
    provider_states: dict[str, ConnectorStatus],
) -> tuple[str, str, int, dict[str, str]]:
    """Summarize provider truth without erasing provider-specific recovery state."""
    states = {key: status.state for key, status in provider_states.items()}
    observed = sum(status.observed_items for status in provider_states.values())
    if not states:
        return "configured_unverified", "Gmail is authorized and ready for one bounded test.", 0, {}
    if any(state == "checking" for state in states.values()):
        return "checking", "Testing saved-search providers through Gmail.", observed, states
    if any(state == "authorization_expired" for state in states.values()):
        return "authorization_expired", "Gmail access expired or was revoked. Reconnect once.", observed, states
    failures = [state for state in states.values() if state in {"degraded", "quota_blocked"}]
    if failures:
        return (
            "degraded",
            "Gmail is connected, but one or more saved-search providers need attention.",
            observed,
            states,
        )
    if any(state == "working" for state in states.values()):
        return "working", "Gmail is importing supported saved-search alerts.", observed, states
    if any(state == "working_zero" for state in states.values()):
        return (
            "working_zero",
            "Gmail is connected; supported alerts were checked with no current matching listings.",
            observed,
            states,
        )
    if any(state == "waiting_first_alert" for state in states.values()):
        return (
            "waiting_first_alert",
            "Gmail is connected and waiting for the first supported provider alert.",
            observed,
            states,
        )
    return "configured_unverified", "Gmail is authorized and ready for one bounded test.", observed, states
