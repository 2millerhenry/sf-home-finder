from __future__ import annotations

from dataclasses import dataclass
from typing import Callable
from datetime import UTC, datetime, time, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

import logging

from .scanner import DEEP_SWEEP_TRIGGER, SLEPT_SCAN_MESSAGE, Scanner


LOGGER = logging.getLogger(__name__)

from .freshness import PACIFIC  # one definition, re-exported for existing callers
SCHEDULE_HOURS = (10, 18)

# How often to ask whether a due slot went unserved. The cron job below is the
# normal path; this is what covers the cases it cannot see. Over the first four
# days of this app, eight scheduled runs were due and four happened -- a
# schedule that silently runs half the time is worse than a manual button,
# because it is trusted.
#
# Fifteen minutes is chosen against the cost of being wrong in either
# direction: a missed 10:00 check is served by 10:15 at the latest, and the
# question itself is one SQLite read of recent scans, so asking 96 times a day
# costs nothing.
CATCH_UP_INTERVAL_MINUTES = 15

# How late either net may be and still run. APScheduler allows a job one second
# of lateness by default and silently skips it beyond that, which is the wrong
# default for a job whose whole purpose is a Mac that was asleep: the wake is
# exactly when its run time is minutes or hours in the past. The real install's
# log shows fifty "was missed by 0:12:48" drops of the scan net and twenty-six
# of the sweep's in sixteen days -- every one of them on the wake it existed to
# serve. An hour is longer than either net has to survive, and being late costs
# nothing: each asks the database whether a run is owed before starting one.
CATCH_UP_MISFIRE_GRACE_SECONDS = 60 * 60

# Named so liveness can tell the two jobs apart. "Next check" means the next
# real scan; the heartbeat runs every fifteen minutes and is not one, so a
# dashboard that took the soonest job would have started promising a check at
# 12:07 instead of 18:00.
SCAN_JOB_ID = "housing-scans-pacific"
CATCH_UP_JOB_ID = "housing-catch-up"
DEEP_SWEEP_JOB_ID = "housing-deep-sweep"
DEEP_SWEEP_CATCH_UP_JOB_ID = "housing-deep-sweep-catch-up"
UPDATE_CHECK_JOB_ID = "housing-update-check"

# When the nightly deep sweep runs. Most sources are read in full on every
# scan because doing so costs seconds; two cannot be. Trulia and Redfin answer
# 403 and 202 once they have had enough, so they are read shallowly at 10:00
# and 18:00 -- when a shallow source that works beats a deep one that is turned
# away -- and to the bottom once a day at an hour where being refused costs a
# run nobody is watching. 03:20 rather than 03:00: the hour itself is when
# every other scheduled thing on a machine fires.
DEEP_SWEEP_HOUR = 3
DEEP_SWEEP_MINUTE = 20

# A sweep is worth catching up, but only once the day it belonged to is gone.
# The original reasoning against catching one up was that running eight hours
# late is worth less than the next one on time -- true, and it assumed there
# would be a next one. On a Mac that is asleep at 03:20 there never is: over
# this app's whole history the nightly sweep has run zero times. So the cron
# stays the normal path, and this is the floor under it: a day and a bit,
# so a machine that is awake at 03:20 always uses the cron and one that is
# not still gets a sweep rather than none.
# A day and a bit, so a machine awake at 03:20 always uses the cron and only
# one that is not falls through to the net under it.
DEEP_SWEEP_MAX_AGE = timedelta(hours=26)

# How many scans a person may ask for themselves in one Pacific day.
#
# Each source is somebody else's site being read by a program, and the ones
# that mind say so by blocking: Zillow by address for six hours or more,
# Trulia with a 403, Redfin with a 202 that never turns into a listing. The
# scheduled checks are bounded by their own cron and are the whole point of the
# app, so they are never refused. The button beside them was the part with no
# limit at all -- held down while somebody waited, it could spend an account's
# welcome in an afternoon. One a day is the limit on that, and on nothing else.
MANUAL_SCANS_PER_DAY = 1

# A run that reached the sources spent its share whether or not it came back
# with anything. Being turned away is still a request, and refusals are what a
# block is built out of, so only a run that never started is free.
BUDGET_SPENDING_STATUSES = frozenset({"completed", "completed_with_errors", "failed"})

# The scans a person asks for. Everything else -- the two scheduled checks, the
# nightly sweep, the catch-up that covers a slot the Mac slept through, the one
# that runs when a deal is first set up -- is the app running on its own clock,
# and is not what this limit is about.
BY_HAND_TRIGGERS = frozenset({"manual", "command_line"})

# How far back the schedule reports on itself. A week is long enough to expose a
# pattern and short enough that a fault shows up while it still matters.
COVERAGE_WINDOW = timedelta(days=7)

# A slot that has only just passed is not yet a miss: the cron fires on the hour
# and the catch-up follows within its interval, so a page loaded at 10:02 must
# not accuse the app of missing a check it is in the middle of running.
SLOT_GRACE = timedelta(minutes=CATCH_UP_INTERVAL_MINUTES + 10)


def latest_scheduled_time(now: datetime | None = None) -> datetime:
    """Return the most recent 10:00/18:00 Pacific slot at or before ``now``."""
    current = now or datetime.now(PACIFIC)
    current = current.astimezone(PACIFIC)
    candidates = [
        datetime.combine(current.date() - timedelta(days=day_offset), time(hour), tzinfo=PACIFIC)
        for day_offset in (0, 1)
        for hour in SCHEDULE_HOURS
    ]
    return max(candidate for candidate in candidates if candidate <= current)


@dataclass(frozen=True, slots=True)
class ScheduleCoverage:
    """How many due checks actually happened, over a recent window.

    "Every due slot gets served" was a claim about wall-clock time that nobody
    could see, so a schedule quietly running half the time looked exactly like
    one running properly. This makes it a number on a page.
    """

    due: int
    served: int
    missed: tuple[datetime, ...]
    since: datetime

    @property
    def complete(self) -> bool:
        return self.due > 0 and self.served == self.due

    @property
    def measurable(self) -> bool:
        """False on a fresh install, where there is nothing to report on yet."""
        return self.due > 0

    def worth_raising(self, *, now: datetime | None = None) -> bool:
        """Is this a fault to raise, or a fact to state?

        One slot missed days ago, already caught up, is not something to act on
        -- the advice for it is "nothing, if the Mac is often closed". Turning
        the whole Support page amber for that trains people to ignore the page,
        which costs more than the miss did. A pattern is different: two or more
        in a week, or one in the last day, is worth a look.
        """
        if not self.missed:
            return False
        if len(self.missed) >= 2:
            return True
        current = (now or datetime.now(UTC)).astimezone(UTC)
        return current - self.missed[-1].astimezone(UTC) < timedelta(days=1)

    def summary(self) -> str:
        if not self.measurable:
            return "Not enough history yet to report on the schedule."
        if self.complete:
            return f"Every one of the last {self.due} scheduled checks ran."
        return f"{self.served} of the last {self.due} scheduled checks ran."


def scheduled_slots(start: datetime, end: datetime) -> list[datetime]:
    """Every 10:00/18:00 Pacific slot in ``[start, end]``, oldest first."""
    first = start.astimezone(PACIFIC)
    last = end.astimezone(PACIFIC)
    slots: list[datetime] = []
    day = first.date() - timedelta(days=1)
    while day <= last.date():
        for hour in SCHEDULE_HOURS:
            slot = datetime.combine(day, time(hour), tzinfo=PACIFIC)
            if first <= slot <= last:
                slots.append(slot)
        day += timedelta(days=1)
    return sorted(slots)


def schedule_coverage(
    scans: list[dict],
    *,
    now: datetime | None = None,
    window: timedelta = COVERAGE_WINDOW,
) -> ScheduleCoverage:
    """Count the slots that were due against the ones a scan actually served.

    A slot counts as served by any completed scan that started between it and
    the next slot, whatever triggered it: the question is whether checking
    happened, not whether cron was the thing that caused it. A scan run by hand
    at 10:05 genuinely served the ten o'clock check.

    Slots before the app's own history begins are not counted, so a fresh
    install does not accuse itself of missing a week of checks it could not
    have run.
    """
    current = (now or datetime.now(UTC)).astimezone(UTC)
    completed = sorted(
        moment
        for moment in (
            _scan_started(scan)
            for scan in scans
            if str(scan.get("status")) in {"completed", "completed_with_errors"}
        )
        if moment is not None
    )
    if not completed:
        return ScheduleCoverage(0, 0, (), current.astimezone(PACIFIC))

    # Never report on time before this app was doing anything -- but floor to
    # the slot that first scan belongs to, not to its clock time. A scan at
    # 10:05 served the ten o'clock check, and flooring at 10:05 would put that
    # slot outside the window and score its own scan as serving nothing.
    since = max(current - window, latest_scheduled_time(completed[0]).astimezone(UTC))
    slots = [
        slot
        for slot in scheduled_slots(since, current)
        # The newest slot is still in flight until the catch-up has had its turn.
        if current - slot.astimezone(UTC) >= SLOT_GRACE
    ]
    if not slots:
        return ScheduleCoverage(0, 0, (), since.astimezone(PACIFIC))

    missed: list[datetime] = []
    served = 0
    for index, slot in enumerate(slots):
        opens = slot.astimezone(UTC)
        closes = (
            slots[index + 1].astimezone(UTC) if index + 1 < len(slots) else current
        )
        if any(opens <= moment < closes for moment in completed):
            served += 1
        else:
            missed.append(slot)
    return ScheduleCoverage(len(slots), served, tuple(missed), since.astimezone(PACIFIC))


def _scan_started(scan: dict) -> datetime | None:
    return _stamp(scan.get("started_at"))


def _stamp(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def scheduled_scan_due(recent_scans: list[dict], now: datetime | None = None) -> bool:
    """Has the most recent 10:00/18:00 slot gone by without a completed scan?

    Asked at start-up and then every quarter of an hour. Deliberately about the
    latest slot only: after three days asleep the answer is one scan, not six,
    so this can never queue up history.
    """
    latest_due_utc = latest_scheduled_time(now).astimezone(UTC)
    for scan in recent_scans:
        if scan.get("status") not in {"completed", "completed_with_errors"}:
            continue
        timestamp = scan.get("started_at")
        if not timestamp:
            continue
        try:
            started = datetime.fromisoformat(str(timestamp))
            if started.tzinfo is None:
                started = started.replace(tzinfo=UTC)
        except ValueError:
            continue
        if started.astimezone(UTC) >= latest_due_utc:
            return False
    return True


def deep_sweep_due(recent_scans: list[dict], now: datetime | None = None) -> bool:
    """Has it been more than a day since a deep sweep finished?

    Asked on the same heartbeat as the scheduled-slot check. Deliberately about
    age rather than about a slot: a sweep collects the long tail, so what
    matters is that one happened recently, not which night it belonged to.
    """
    current = (now or datetime.now(UTC)).astimezone(UTC)
    for scan in recent_scans:
        if str(scan.get("trigger") or "") != DEEP_SWEEP_TRIGGER:
            continue
        if scan.get("status") not in {"completed", "completed_with_errors"}:
            continue
        timestamp = scan.get("started_at")
        if not timestamp:
            continue
        try:
            started = datetime.fromisoformat(str(timestamp))
        except ValueError:
            continue
        if started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
        if current - started.astimezone(UTC) < DEEP_SWEEP_MAX_AGE:
            return False
    return True


def manual_scans_today(recent_scans: list[dict], now: datetime | None = None) -> int:
    """How many scans a person has asked for today, Pacific.

    The day is the Pacific calendar day: it is the day the schedule is written
    in, and "one check a day" is a sentence somebody can hold in their head and
    predict. A rolling twenty-four hours would be no safer here -- there is
    only ever one of these -- and much harder to explain.
    """
    current = (now or datetime.now(PACIFIC)).astimezone(PACIFIC)
    midnight = datetime.combine(current.date(), time(0), tzinfo=PACIFIC)
    spent = 0
    for scan in recent_scans:
        if str(scan.get("trigger") or "") not in BY_HAND_TRIGGERS:
            continue
        if scan.get("status") not in BUDGET_SPENDING_STATUSES:
            continue
        # Nothing to charge for. A scan run with the wifi off fails every source
        # in the resolver, so not one request arrives anywhere -- and the person
        # back on a network was told "You have had your check for today" about a
        # check that never happened. Absent (an older row, or a caller passing a
        # bare summary) is not zero: only a counted nought is free.
        if scan.get("sources_reached") == 0:
            continue
        timestamp = scan.get("started_at")
        if not timestamp:
            continue
        try:
            started = datetime.fromisoformat(str(timestamp))
        except ValueError:
            continue
        if started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
        if started.astimezone(PACIFIC) >= midnight:
            spent += 1
    return spent


def manual_scan_allowed(
    recent_scans: list[dict], trigger: str = "manual", now: datetime | None = None
) -> bool:
    """Whether this scan may start.

    Only a scan somebody asked for is ever refused. The scheduled checks, the
    nightly sweep and the catch-ups run on their own clock, which is its own
    limit, and refusing one of those to protect a button would be exactly the
    wrong way round.
    """
    if trigger not in BY_HAND_TRIGGERS:
        return True
    return manual_scans_today(recent_scans, now) < MANUAL_SCANS_PER_DAY


def next_scheduled_check(now: datetime | None = None) -> datetime:
    """The next clocked check, for telling somebody when the app looks next.

    A refusal that only says no is a fault report. This is what makes it an
    answer instead.
    """
    current = (now or datetime.now(PACIFIC)).astimezone(PACIFIC)
    upcoming = [
        datetime.combine(current.date() + timedelta(days=offset), time(hour), tzinfo=PACIFIC)
        for offset in (0, 1)
        for hour in SCHEDULE_HOURS
    ]
    return min(slot for slot in upcoming if slot > current)


def next_manual_scan_allowed(now: datetime | None = None) -> datetime:
    """When the next check by hand becomes available: the next midnight."""
    current = (now or datetime.now(PACIFIC)).astimezone(PACIFIC)
    return datetime.combine(current.date() + timedelta(days=1), time(0), tzinfo=PACIFIC)


# A check the computer slept through is owed again (``SLEPT_SCAN_MESSAGE``),
# but not in the next moment of darkness: a Mac asleep overnight wakes for a
# minute now and then, a catch-up begun in one is cut short by the next sleep,
# and every try asks the same first sites again. One try an hour still finds
# the morning.
SLEPT_RETRY_WAIT = timedelta(hours=1)


def slept_through_lately(recent_scans: list[dict], now: datetime | None = None) -> bool:
    """Whether the latest check stopped for sleep less than ``SLEPT_RETRY_WAIT`` ago."""
    latest = recent_scans[0] if recent_scans else None
    if not latest or latest.get("status") != "interrupted" or latest.get("message") != SLEPT_SCAN_MESSAGE:
        return False
    stopped = _stamp(latest.get("finished_at") or latest.get("started_at"))
    return stopped is not None and (now or datetime.now(UTC)).astimezone(UTC) - stopped < SLEPT_RETRY_WAIT


def catch_up_if_due(scanner: Scanner) -> bool:
    """Run one scan if a scheduled slot went unserved. Returns whether it did.

    The cron job is the normal path. It fires only while this process is alive
    and the Mac is awake, and APScheduler keeps its schedule in memory, so a
    restart after a missed slot recomputes the next run from now and that slot
    is gone. This asks the database instead of the scheduler, so it does not
    care why the slot was missed -- sleep, a crash, a reinstall, or a machine
    that was simply off.

    Every failure here is swallowed on purpose: a heartbeat that raises would be
    logged and dropped by APScheduler, and its job is to make scanning more
    reliable, never less.
    """
    try:
        if scanner.is_running:
            return False
        if not scanner.preference_loader().profile_active:
            return False
        recent = scanner.repository.recent_scans(20)
        if not scheduled_scan_due(recent) or slept_through_lately(recent):
            return False
        started = scanner.start_scan("catch_up")
        if started:
            LOGGER.info("Catch-up scan started for a scheduled slot that did not run")
        return started
    except Exception:  # pragma: no cover - defensive, see docstring
        LOGGER.warning("Catch-up check failed", exc_info=True)
        return False


def sweep_if_due(scanner: Scanner) -> bool:
    """Run the nightly sweep if a day has gone by without one.

    Its own job rather than part of the catch-up heartbeat, because the two
    answer different questions: that one is "is the shortlist current", this is
    "has the long tail been collected lately". Sharing a function would have
    made a fresh install's first heartbeat start a fifteen-minute sweep.

    Failures are swallowed for the same reason they are there: a heartbeat that
    raises is logged and dropped, and its job is to make scanning more
    reliable, never less.
    """
    try:
        if scanner.is_running:
            return False
        if not scanner.preference_loader().profile_active:
            return False
        recent = scanner.repository.recent_scans(40)
        # A current shortlist beats a complete tail: if an ordinary check is
        # owed, that runs first and this waits for the next heartbeat.
        if scheduled_scan_due(recent) or slept_through_lately(recent):
            return False
        if not deep_sweep_due(recent):
            return False
        started = scanner.start_scan(DEEP_SWEEP_TRIGGER)
        if started:
            LOGGER.info("Deep sweep started; the nightly one did not run")
        return started
    except Exception:  # pragma: no cover - defensive, see docstring
        LOGGER.warning("Deep sweep check failed", exc_info=True)
        return False


def build_scheduler(
    scanner: Scanner, update_check: Callable[[], object] | None = None
) -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone=PACIFIC)
    scheduler.add_job(
        scanner.run_scan,
        CronTrigger(hour="10,18", minute=0, timezone=PACIFIC),
        args=["scheduled"],
        id=SCAN_JOB_ID,
        name="Housing scans at 10:00 and 18:00 Pacific",
        replace_existing=True,
        # A sleeping Mac can miss one or both times. Run only the newest missed
        # occurrence after wake, while the startup catch-up covers a reboot.
        coalesce=True,
        max_instances=1,
        misfire_grace_time=18 * 60 * 60,
    )
    # Once a day, read the sources that cannot be read deeply the rest of the
    # time. Deliberately not caught up if it is missed: a sweep is how the
    # long tail is collected, not how anything stays current, so running one
    # eight hours late on wake is worth less than the next one on time.
    scheduler.add_job(
        scanner.run_scan,
        CronTrigger(
            hour=DEEP_SWEEP_HOUR, minute=DEEP_SWEEP_MINUTE, timezone=PACIFIC
        ),
        args=[DEEP_SWEEP_TRIGGER],
        id=DEEP_SWEEP_JOB_ID,
        name="Nightly deep sweep of the rate-limited sources",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=2 * 60 * 60,
    )
    # The safety net under that job. It asks the database what actually ran
    # rather than trusting the scheduler's own memory of what it meant to run.
    scheduler.add_job(
        catch_up_if_due,
        IntervalTrigger(minutes=CATCH_UP_INTERVAL_MINUTES, timezone=PACIFIC),
        args=[scanner],
        id=CATCH_UP_JOB_ID,
        name="Catch up a missed scheduled check",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=CATCH_UP_MISFIRE_GRACE_SECONDS,
    )
    # The same safety net under the sweep. Hourly rather than quarter-hourly:
    # what it is checking changes once a day.
    scheduler.add_job(
        sweep_if_due,
        IntervalTrigger(hours=1, timezone=PACIFIC),
        args=[scanner],
        id=DEEP_SWEEP_CATCH_UP_JOB_ID,
        name="Catch up a missed deep sweep",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=CATCH_UP_MISFIRE_GRACE_SECONDS,
    )
    if update_check is not None:
        # Six-hourly, though it asks GitHub at most once a day: the job is the
        # net, and the once-a-day rule lives in the check itself. A Mac asleep
        # at whatever moment a daily job would have fired is the ordinary case,
        # not the exception, and a check that silently never runs is the same
        # as not having one.
        #
        # It runs once at startup too, because the most valuable moment to
        # learn a release exists is the one just after somebody opened the app.
        scheduler.add_job(
            update_check,
            IntervalTrigger(hours=6, timezone=PACIFIC),
            id=UPDATE_CHECK_JOB_ID,
            name="Ask whether a newer release exists",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            next_run_time=datetime.now(PACIFIC),
            # The startup check is scheduled for the moment the scheduler is
            # built and does not run until the app starts it, which is after
            # the database is open and the preferences are read. APScheduler
            # allows a job one second of lateness by default and silently
            # drops it beyond that, so the check that was meant to happen at
            # startup never happened at all -- it went straight to waiting six
            # hours, on every launch, and only an install-and-watch found it.
            misfire_grace_time=60 * 60,
        )
    return scheduler
