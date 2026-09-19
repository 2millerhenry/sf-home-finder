"""Whether a newer release exists, and what is allowed to be said about it.

This is the only outbound request the app makes for its own sake, on an app
whose whole claim is that it collects nothing, so what it may do is narrow: ask
a public page, store the answer, never send anything about anybody, and stop
entirely when told to. It is also the only route by which a bug already on
somebody's machine can ever be fixed, since nothing here reports anything and
nobody would otherwise learn that a fix exists.

The failure this is built to survive is silence. GitHub being unreachable, rate
limited, slow, or answering with a login page are all ordinary, and none of
them may reach a person or hold up a page.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from sf_housing import update_check
from sf_housing.update_check import (
    UpdateStatus,
    cache_path,
    is_newer,
    read_status,
    refresh_status,
    release_series,
    write_status,
)


NOON = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)


def at(moment: datetime):
    return lambda: moment


def test_versions_are_compared_as_numbers_not_as_text() -> None:
    """The bug this exists to prevent. "0.10.0" sorts before "0.9.0" as text, so
    a string comparison works for exactly nine minor releases and then stops
    offering upgrades forever, on every machine, silently."""
    assert is_newer("0.10.0", "0.9.0")
    assert not is_newer("0.9.0", "0.10.0")
    assert is_newer("1.0.0", "0.99.99")
    assert is_newer("0.5.10", "0.5.9")


def test_the_same_version_is_not_an_update() -> None:
    assert not is_newer("0.5.0", "0.5.0")
    assert not is_newer("v0.5.0", "0.5.0")


def test_an_older_release_is_never_offered_as_an_upgrade() -> None:
    """Which happens routinely while developing: the machine is ahead of
    anything published, and being invited to "update" backwards to the last
    release would replace the work in progress."""
    assert not is_newer("0.4.9", "0.5.0")


def test_a_pre_release_is_not_offered_to_anybody() -> None:
    """A release candidate is not something to push at somebody who only wants
    their housing search to keep working."""
    assert release_series("v0.6.0-rc1") is None
    assert not is_newer("v0.6.0-rc1", "0.5.0")
    assert not is_newer("v1.0.0b2", "0.5.0")


@pytest.mark.parametrize("tag", ["", None, "latest", "v", "0.5", "0.5.0.1", "vv0.5.0", "0.5.x"])
def test_a_tag_that_is_not_a_release_is_not_news(tag) -> None:
    assert release_series(tag) is None
    assert not is_newer(tag, "0.5.0")


def test_a_version_this_cannot_read_is_left_alone() -> None:
    """Someone running from a checkout rather than a release."""
    assert not is_newer("0.6.0", "0.6.0.dev1+local")


def test_an_answer_survives_being_written_and_read_back(tmp_path: Path) -> None:
    write_status(tmp_path, UpdateStatus(checked_at=NOON, latest="v9.9.9", available=True))

    stored = read_status(tmp_path)

    assert stored is not None
    assert stored.latest == "v9.9.9"
    assert stored.available
    assert stored.checked_at == NOON


def test_whether_an_update_exists_is_worked_out_again_on_every_read(tmp_path: Path) -> None:
    """The stored flag was true for whatever was installed when it was written,
    and upgrading does not remove the file. Trusting it would leave a freshly
    upgraded machine advertising the release it is already running."""
    write_status(
        tmp_path,
        # What the file looks like after an upgrade to this very version.
        UpdateStatus(checked_at=NOON, latest=update_check.__version__, available=True),
    )

    stored = read_status(tmp_path)

    assert stored is not None
    assert not stored.available, "still offering the version already installed"


@pytest.mark.parametrize(
    "contents",
    ["", "   ", "not json at all", "[]", '"a string"', "{}", '{"checked_at": "never"}'],
)
def test_a_damaged_stored_answer_is_simply_no_news(tmp_path: Path, contents: str) -> None:
    """Half a file is what a process killed mid-write leaves behind. Every read
    of it has to be an absence of news rather than a crash on the dashboard."""
    cache_path(tmp_path).write_text(contents, encoding="utf-8")

    assert read_status(tmp_path) is None


def test_no_stored_answer_at_all_is_no_news(tmp_path: Path) -> None:
    assert read_status(tmp_path) is None


def test_a_fresh_answer_is_not_asked_for_again(tmp_path: Path) -> None:
    """Sixty unauthenticated requests an hour are shared with everything else on
    the machine that talks to GitHub. Asking once a day leaves it untouched."""
    write_status(tmp_path, UpdateStatus(checked_at=NOON, latest="v0.5.0"))
    asked = []

    refresh_status(
        tmp_path,
        fetch=lambda: asked.append(1) or "v9.9.9",
        now=at(NOON + timedelta(hours=23)),
    )

    assert not asked, "asked again within the day"


def test_a_stale_answer_is_asked_for_again(tmp_path: Path) -> None:
    write_status(tmp_path, UpdateStatus(checked_at=NOON, latest="v0.5.0"))

    status = refresh_status(
        tmp_path, fetch=lambda: "v9.9.9", now=at(NOON + timedelta(hours=25))
    )

    assert status is not None and status.latest == "v9.9.9"
    assert read_status(tmp_path).latest == "v9.9.9", "the new answer was not stored"


def test_silence_from_github_keeps_the_last_good_answer(tmp_path: Path) -> None:
    """Offline, rate limited, or handed a captive-portal login page. None of
    those mean there is no update; they mean nobody knows, and forgetting what
    was already known would be a strictly worse answer."""
    write_status(tmp_path, UpdateStatus(checked_at=NOON, latest="v9.9.9"))

    status = refresh_status(
        tmp_path, fetch=lambda: None, now=at(NOON + timedelta(days=7))
    )

    assert status is not None and status.latest == "v9.9.9"
    assert status.available


def test_silence_does_not_move_the_clock_so_it_tries_again_tomorrow(tmp_path: Path) -> None:
    """If a failed attempt counted as an attempt, a machine offline for an hour
    would then wait a full day before trying again."""
    write_status(tmp_path, UpdateStatus(checked_at=NOON, latest="v9.9.9"))
    refresh_status(tmp_path, fetch=lambda: None, now=at(NOON + timedelta(days=7)))

    assert read_status(tmp_path).checked_at == NOON


def test_a_fetch_that_raises_never_escapes(tmp_path: Path) -> None:
    """This runs on a scheduler, where an exception is a log line nobody reads
    and a job that quietly stops being scheduled."""
    def explode():
        raise RuntimeError("DNS is having a day")

    assert refresh_status(tmp_path, fetch=explode, now=at(NOON)) is None


def test_asking_not_to_be_checked_up_on_stops_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The promise is that nothing leaves the machine. Somebody who wants that
    absolutely gets it absolutely: no request, and nothing displayed from one
    made earlier."""
    write_status(tmp_path, UpdateStatus(checked_at=NOON, latest="v9.9.9", available=True))
    monkeypatch.setenv(update_check.DISABLE_VARIABLE, "1")
    asked = []

    assert refresh_status(tmp_path, fetch=lambda: asked.append(1) or "v9.9.9", now=at(NOON)) is None
    assert not asked, "asked GitHub after being told not to"
    assert read_status(tmp_path) is None, "showed an answer after being told not to"


def test_the_answer_is_never_left_half_written(tmp_path: Path) -> None:
    """Written beside the target and renamed over it. A reader that arrives
    mid-write sees the old answer or the new one, never a truncated file."""
    write_status(tmp_path, UpdateStatus(checked_at=NOON, latest="v9.9.9"))

    leftovers = [p.name for p in tmp_path.iterdir() if p.name != update_check.CACHE_NAME]

    assert not leftovers, f"left {leftovers} behind"
    assert json.loads(cache_path(tmp_path).read_text())["latest"] == "v9.9.9"


def test_a_disk_that_cannot_be_written_to_is_not_a_crash(tmp_path: Path) -> None:
    """A full or read-only disk is not worth taking a background job down for."""
    unwritable = tmp_path / "nope"
    unwritable.write_text("I am a file, not a directory", encoding="utf-8")

    write_status(unwritable, UpdateStatus(checked_at=NOON, latest="v9.9.9"))

    assert read_status(unwritable) is None


def test_nothing_about_the_person_is_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    """The request carries no query, no body, and not even the installed
    version: the comparison happens after the answer comes back, so the only
    thing GitHub learns is that somebody read a public page."""
    seen = {}

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"tag_name": "v9.9.9"}

    def capture(url, **kwargs):
        seen["url"] = url
        seen["kwargs"] = kwargs
        return Response()

    monkeypatch.setattr(update_check.httpx, "get", capture)

    assert update_check.fetch_latest_tag() == "v9.9.9"
    assert "?" not in seen["url"], f"a query string was sent: {seen['url']}"
    assert seen["kwargs"].get("params") is None
    assert seen["kwargs"].get("content") is None and seen["kwargs"].get("json") is None
    assert update_check.__version__ not in seen["url"]
    headers = seen["kwargs"]["headers"]
    assert set(headers) == {"Accept", "User-Agent"}, f"extra headers: {sorted(headers)}"
    # The headers are part of the request too. A User-Agent naming the release
    # tells GitHub exactly which version is installed on that address, which is
    # the one thing the README promises this request does not say -- and the
    # assertions above all read the URL, so a version in a header passed them.
    for name, value in headers.items():
        assert update_check.__version__ not in str(value), (
            f"the {name} header sends the installed version: {value}"
        )


@pytest.mark.parametrize("status_code", [301, 401, 403, 404, 429, 500, 503])
def test_github_refusing_to_answer_is_not_news(
    monkeypatch: pytest.MonkeyPatch, status_code: int
) -> None:
    """403 is what a rate limit looks like, 404 is a repository with no releases
    yet, and a captive portal can return anything at all."""
    class Response:
        def __init__(self):
            self.status_code = status_code

        @staticmethod
        def json():
            return {"tag_name": "v9.9.9"}

    monkeypatch.setattr(update_check.httpx, "get", lambda url, **kwargs: Response())

    assert update_check.fetch_latest_tag() is None


def test_an_answer_that_is_not_json_is_not_news(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        status_code = 200

        @staticmethod
        def json():
            raise ValueError("that was HTML")

    monkeypatch.setattr(update_check.httpx, "get", lambda url, **kwargs: Response())

    assert update_check.fetch_latest_tag() is None


def test_a_request_that_never_finishes_is_not_news(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    def timeout(url, **kwargs):
        raise httpx.ConnectTimeout("no route to host")

    monkeypatch.setattr(update_check.httpx, "get", timeout)

    assert update_check.fetch_latest_tag() is None


def test_the_first_check_on_a_new_machine_is_stored(tmp_path: Path) -> None:
    status = refresh_status(tmp_path, fetch=lambda: "v9.9.9", now=at(NOON))

    assert status is not None and status.available
    assert read_status(tmp_path).latest == "v9.9.9"


def test_a_failed_write_does_not_destroy_the_answer_already_stored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Why it is written beside the target and renamed rather than written over
    it. Writing in place means a failure part way through has already destroyed
    the good answer that was there, and the next read finds wreckage."""
    write_status(tmp_path, UpdateStatus(checked_at=NOON, latest="v9.9.9"))
    original = Path.write_text

    def fail_on_the_new_answer(self, *args, **kwargs):
        if self.name.startswith(update_check.CACHE_NAME):
            raise OSError("no space left on device")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_on_the_new_answer)
    write_status(tmp_path, UpdateStatus(checked_at=NOON, latest="v9.9.10"))
    monkeypatch.undo()

    survivor = read_status(tmp_path)
    assert survivor is not None, "the stored answer was destroyed by a failed write"
    assert survivor.latest == "v9.9.9"


# --- What the app does with the answer ---------------------------------------


def build_app(tmp_path: Path):
    from sf_housing.app import create_app
    from sf_housing.settings import Settings

    data = tmp_path / "data"
    (data / "config").mkdir(parents=True, exist_ok=True)
    settings = Settings(
        data_dir=data,
        preferences_path=data / "config" / "preferences.yaml",
        database_path=data / "housing.sqlite3",
        log_path=data / "housing.log",
    )
    return create_app(settings=settings, sources=[], enable_scheduler=False), data


def test_health_says_nothing_before_anything_has_been_checked(tmp_path: Path) -> None:
    """The installer polls /health and requires ok to be true. A field that is
    absent, null or present must never change that."""
    from fastapi.testclient import TestClient

    application, _ = build_app(tmp_path)
    with TestClient(application) as client:
        payload = client.get("/health").json()

    assert payload["ok"] is True
    assert payload["update"] is None


def test_health_passes_on_what_was_last_learned(tmp_path: Path) -> None:
    """Which is how the terminal command knows: it reads what the app stored
    rather than asking GitHub a second time from a second place."""
    from fastapi.testclient import TestClient

    application, data = build_app(tmp_path)
    write_status(data, UpdateStatus(checked_at=NOON, latest="v9.9.9", available=True))
    with TestClient(application) as client:
        payload = client.get("/health").json()

    assert payload["update"]["available"] is True
    assert payload["update"]["latest"] == "v9.9.9"


def test_the_page_mentions_a_newer_version_quietly(tmp_path: Path) -> None:
    """For everybody who installed from the ZIP and never put the command on
    their PATH, this is the only way they would ever find out."""
    from fastapi.testclient import TestClient

    application, data = build_app(tmp_path)
    write_status(data, UpdateStatus(checked_at=NOON, latest="v9.9.9", available=True))
    with TestClient(application) as client:
        page = client.get("/preferences").text

    assert "v9.9.9 is available" in page
    assert "homefinder update" in page


def test_the_page_says_nothing_when_there_is_nothing_to_say(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    application, data = build_app(tmp_path)
    write_status(data, UpdateStatus(checked_at=NOON, latest=update_check.__version__))
    with TestClient(application) as client:
        page = client.get("/preferences").text

    assert "is available" not in page


def test_rendering_a_page_never_reaches_the_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reason the check is a scheduled job and not something the page does.
    A dashboard that waited on GitHub would hang for as long as GitHub felt
    like taking, on a machine whose whole promise is that it works offline.
    """
    from fastapi.testclient import TestClient

    application, data = build_app(tmp_path)
    # Deliberately old, so that anything which decided to refresh would have to
    # go and ask. A stored answer written moments ago would make this pass by
    # being too fresh to act on rather than by never asking.
    long_ago = datetime.now(timezone.utc) - timedelta(days=30)
    write_status(data, UpdateStatus(checked_at=long_ago, latest="v9.9.9", available=True))
    asked = []

    # Recorded rather than raised: refresh_status swallows everything a fetch
    # throws, on purpose, so an exception here would be absorbed and the test
    # would pass while the render sat on a socket.
    monkeypatch.setattr(update_check.httpx, "get", lambda *a, **k: asked.append(a) or None)
    with TestClient(application) as client:
        assert "v9.9.9 is available" in client.get("/preferences").text
        assert client.get("/health").json()["update"]["available"] is True

    assert not asked, "a page render asked GitHub for something"


def test_turning_the_check_off_reaches_the_page_and_the_health_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Off means off everywhere, including anything learned before it was
    switched off."""
    from fastapi.testclient import TestClient

    application, data = build_app(tmp_path)
    write_status(data, UpdateStatus(checked_at=NOON, latest="v9.9.9", available=True))
    monkeypatch.setenv(update_check.DISABLE_VARIABLE, "1")
    with TestClient(application) as client:
        assert "is available" not in client.get("/preferences").text
        assert client.get("/health").json()["update"] is None


class NoScans:
    """Enough of a scanner for the scheduler to hang jobs off."""

    def run_scan(self, *args, **kwargs):
        raise AssertionError("no scan should run from a scheduling test")


def test_the_first_check_happens_at_startup_not_six_hours_later() -> None:
    """A job whose first firing is six hours away never runs at all on a Mac
    that is shut before then, and the most useful moment to learn a release
    exists is the one just after somebody opened the app."""
    from sf_housing.scheduling import PACIFIC, UPDATE_CHECK_JOB_ID, build_scheduler

    scheduler = build_scheduler(NoScans(), update_check=lambda: None)
    scheduler.start(paused=True)
    try:
        job = scheduler.get_job(UPDATE_CHECK_JOB_ID)
        assert job is not None, "nothing ever asks whether a newer version exists"
        waiting = job.next_run_time - datetime.now(PACIFIC)
        assert waiting < timedelta(minutes=1), f"the first check waits {waiting}"
    finally:
        scheduler.shutdown(wait=False)


def test_a_build_that_never_asks_still_schedules_the_searches() -> None:
    """The check is an addition. Leaving it out has to leave everything the app
    is actually for exactly as it was."""
    from sf_housing.scheduling import SCAN_JOB_ID, UPDATE_CHECK_JOB_ID, build_scheduler

    scheduler = build_scheduler(NoScans())

    assert scheduler.get_job(UPDATE_CHECK_JOB_ID) is None
    assert scheduler.get_job(SCAN_JOB_ID) is not None


def test_the_startup_check_survives_the_app_taking_its_time_to_start() -> None:
    """The regression, and it was invisible until this was installed and
    watched.

    The job is scheduled for the moment the scheduler is built, and the app
    does not start the scheduler until the database is open and the profile is
    read. APScheduler forgives one second of lateness by default and silently
    drops anything later, so the check meant to run at startup never ran at
    all: every launch went straight to waiting six hours, and a Mac closed
    before then never asked at all.
    """
    from datetime import datetime as real_datetime

    from sf_housing.scheduling import PACIFIC, UPDATE_CHECK_JOB_ID, build_scheduler

    ran = threading.Event()
    scheduler = build_scheduler(NoScans(), update_check=ran.set)
    # What a slow startup looks like, without making the suite wait for one.
    scheduler.modify_job(
        UPDATE_CHECK_JOB_ID,
        next_run_time=real_datetime.now(PACIFIC) - timedelta(minutes=5),
    )
    scheduler.start()
    try:
        assert ran.wait(timeout=10), "the check was dropped for being late"
    finally:
        scheduler.shutdown(wait=False)


def test_a_windows_reader_is_not_told_to_run_a_mac_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """There is no homefinder command on Windows. The folder of shortcuts is
    the way in there, and getting a new version means downloading it and
    running Install again, which is what the page has to say."""
    from fastapi.testclient import TestClient

    import sys as real_sys

    # The globals are decided when the app is built, so this has to be in place
    # before build_app below.
    monkeypatch.setattr(real_sys, "platform", "win32")
    application, data = build_app(tmp_path)
    write_status(data, UpdateStatus(checked_at=NOON, latest="v9.9.9", available=True))
    with TestClient(application) as client:
        page = client.get("/preferences").text

    assert "v9.9.9 is available" in page
    assert "homefinder" not in page, "told a Windows reader to run a command macOS has"
    assert "releases/latest" in page, "gave no way to actually get it"


def test_a_mac_reader_is_given_the_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient
    import sys as real_sys

    monkeypatch.setattr(real_sys, "platform", "darwin")
    application, data = build_app(tmp_path)
    write_status(data, UpdateStatus(checked_at=NOON, latest="v9.9.9", available=True))
    with TestClient(application) as client:
        page = client.get("/preferences").text

    assert "homefinder update" in page


def test_the_request_says_nothing_about_this_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    """The regression: a User-Agent of "sf-home-finder/0.5.6" told GitHub the
    exact installed version, against a README that promises the request sends
    "no query, no identifier, not even which version you are running". GitHub
    refuses a request with no User-Agent at all, so it names the app and stops
    there."""
    import httpx

    captured: dict[str, object] = {}

    def record(url, **kwargs):
        captured["url"] = str(url)
        captured["headers"] = dict(kwargs.get("headers") or {})
        return httpx.Response(200, json={"tag_name": "v9.9.9"}, request=httpx.Request("GET", url))

    monkeypatch.setattr(update_check.httpx, "get", record)
    assert update_check.fetch_latest_tag() == "v9.9.9"

    sent = " ".join([str(captured["url"]), *(f"{k}: {v}" for k, v in captured["headers"].items())])
    assert update_check.__version__ not in sent, (
        f"the installed version was sent to GitHub: {sent}"
    )
    assert "?" not in str(captured["url"]), "a query string was added"
