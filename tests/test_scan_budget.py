"""One check by hand a day, and no limit on the ones the clock asks for.

Every source is somebody else's site read by a program, and the ones that mind
say so by blocking. Per-source floors already space single reads apart, but the
button had no limit at all: held down while somebody waited for results, it
could spend an account's welcome in an afternoon.

The scheduled checks, the nightly sweep and the catch-ups are bounded by their
own cron and are the whole point of the app, so they are never refused -- an
earlier version of this counted them against the same allowance, and the effect
was that the button stopped working on the nights the sweep ran. These pin the
limit to the scans a person actually asks for.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from sf_housing.preferences import parse_preferences
from sf_housing.scanner import Scanner
from sf_housing.scheduling import (
    MANUAL_SCANS_PER_DAY,
    PACIFIC,
    manual_scan_allowed,
    manual_scans_today,
    next_manual_scan_allowed,
)
from tests.conftest import TEST_PREFERENCES


def at(hour: int, minute: int = 0, day: int = 9) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=PACIFIC)


def ran(hour: int, trigger: str, status: str = "completed", day: int = 9) -> dict:
    return {"trigger": trigger, "status": status, "started_at": at(hour, 0, day).isoformat()}


NOON = at(12)


def test_the_limit_is_one_check_by_hand_a_day() -> None:
    assert MANUAL_SCANS_PER_DAY == 1


def test_the_first_check_by_hand_of_the_day_is_allowed() -> None:
    assert manual_scan_allowed([], "manual", NOON) is True


def test_a_second_check_by_hand_is_refused() -> None:
    assert manual_scan_allowed([ran(9, "manual")], "manual", NOON) is False


def test_the_command_line_spends_the_same_one_check() -> None:
    """It reads the same sites the button does, so it is the same allowance."""
    assert manual_scan_allowed([ran(9, "command_line")], "manual", NOON) is False
    assert manual_scan_allowed([ran(9, "manual")], "command_line", NOON) is False


@pytest.mark.parametrize(
    "trigger", ["scheduled", "catch_up", "deep_sweep", "startup_catchup", "initial_discovery"]
)
def test_the_clock_is_never_refused_however_the_day_has_gone(trigger: str) -> None:
    """The scheduled checks are the product. Refusing one to protect a button
    would be exactly the wrong way round, and refusing the first scan of a new
    deal would break setting one up."""
    spent = [ran(9, "manual")]

    assert manual_scan_allowed(spent, trigger, NOON) is True


def test_a_full_day_of_automatic_runs_leaves_the_check_by_hand_intact() -> None:
    """The regression this replaced: counting the sweep and both scheduled
    checks against the same allowance left nothing for the button."""
    busy = [ran(3, "deep_sweep"), ran(10, "scheduled"), ran(18, "scheduled")]

    assert manual_scans_today(busy, NOON) == 0
    assert manual_scan_allowed(busy, "manual", NOON) is True


def test_a_check_that_failed_still_spent_the_day() -> None:
    """Being turned away is still a request, and refusals are what a block is
    built out of."""
    assert manual_scans_today([ran(9, "manual", "failed")], NOON) == 1


def test_a_check_that_never_started_is_free() -> None:
    assert manual_scans_today([ran(9, "manual", "skipped")], NOON) == 0


def test_yesterdays_check_does_not_count_against_today() -> None:
    assert manual_scans_today([ran(9, "manual", day=8)], NOON) == 0
    assert manual_scan_allowed([ran(9, "manual", day=8)], "manual", NOON) is True


def test_a_check_at_the_stroke_of_midnight_belongs_to_the_new_day() -> None:
    """The boundary is inclusive at the bottom. Exclusive, a check started on
    the exact second the day turned would be charged to neither day."""
    assert manual_scans_today([ran(0, "manual")], NOON) == 1


def test_the_next_one_is_named_so_a_refusal_is_an_answer() -> None:
    assert next_manual_scan_allowed(NOON) == at(0, day=10)


def scanner_for(repository, allowed: bool) -> Scanner:
    return Scanner(
        repository,
        lambda: parse_preferences(TEST_PREFERENCES),
        [],
        scan_allowed=lambda trigger, sources: allowed,
    )


def test_the_scanner_refuses_a_full_scan_when_the_day_is_spent(repository) -> None:
    assert scanner_for(repository, allowed=False).start_scan("manual") is False


def test_the_scanner_runs_a_full_scan_when_the_day_is_free(repository) -> None:
    assert scanner_for(repository, allowed=True).start_scan("manual") is True


def test_testing_one_connector_is_never_charged_against_the_day(repository) -> None:
    """One read of one site, asked for by somebody sitting in front of the
    setup page. Refusing it would break setting the source up at all."""
    scanner = scanner_for(repository, allowed=False)

    assert scanner._within_daily_budget("facebook_backfill", sources=[object()]) is True
    assert scanner._within_daily_budget("manual", sources=None) is False


def test_a_scanner_with_no_limit_wired_in_is_unlimited(repository) -> None:
    """The default is what the tests about scanning itself want; the app always
    passes one."""
    scanner = Scanner(repository, lambda: parse_preferences(TEST_PREFERENCES), [])

    assert scanner._within_daily_budget("manual", sources=None) is True


def test_a_check_that_reached_nothing_because_the_laptop_was_offline_is_not_spent() -> None:
    """An outage spent the day's one check without sending a single request.

    The budget exists so the sites this reads are not hammered, and a scan run
    with the wifi off asks them nothing at all -- every source fails in the
    resolver, before a socket is opened. Counting it meant somebody who pressed
    the button on a train, or behind a captive portal, was told "You have had
    your check for today" once they were back online, and had no way to refresh
    until the next clocked check hours later.

    A scan that was merely turned away is different and still spends: a 403 is
    a request that arrived, and refusals are what a block is built out of.
    """
    offline = ran(9, "manual")
    offline["sources_reached"] = 0

    assert manual_scans_today([offline], NOON) == 0
    assert manual_scan_allowed([offline], "manual", NOON) is True


def test_a_check_the_sites_turned_away_still_spends_the_day() -> None:
    """Being blocked is still contact, and is exactly what the limit is for."""
    blocked = ran(9, "manual")
    blocked["sources_reached"] = 3

    assert manual_scans_today([blocked], NOON) == 1
    assert manual_scan_allowed([blocked], "manual", NOON) is False
