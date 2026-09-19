from __future__ import annotations

import ipaddress
import os
import shutil
import socket
import tempfile
import traceback
from pathlib import Path
from types import SimpleNamespace

import pytest

# Everything the app writes goes to a throwaway folder for the whole run, set
# before anything can import the app. ``sf_housing/app.py`` builds the app the
# moment it is imported, and with no location named that app opens, migrates
# and can re-score ``data/housing.sqlite3`` in this repository -- the board the
# development servers here are using -- before a single test has started. Set
# even when the environment already names a folder, which might be somebody's
# real data. Programs the tests start inherit it too.
TEST_DATA_DIR = tempfile.mkdtemp(prefix="sf-housing-tests-")
os.environ["SF_HOUSING_DATA_DIR"] = TEST_DATA_DIR
os.environ.pop("SF_HOUSING_PREFERENCES", None)

from sf_housing.database import Repository  # noqa: E402 - after the folder is chosen
from sf_housing.preferences import Preferences, parse_preferences  # noqa: E402


def pytest_unconfigure(config: pytest.Config) -> None:
    shutil.rmtree(TEST_DATA_DIR, ignore_errors=True)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "real_network(reason): let this one test leave the machine; the reason is required",
    )
    config.addinivalue_line(
        "markers",
        "coverage_pass: run a scan's real coverage count, faking what it fetches in the test",
    )


class RealNetworkRefused(OSError):
    """What a test gets instead of a connection to anywhere but this machine.

    An OSError, so the code under test meets it exactly as it would meet being
    offline. That is also why raising it is not enough on its own: the app is
    written to shrug off being offline, and a count, a probe or an update check
    that swallows this would leave its test green. Every refusal is written
    down as well, and the test fails for it afterwards.
    """


REPOSITORY = Path(__file__).resolve().parents[1]
network = SimpleNamespace(refused=[], permitted=False)


def is_this_machine(host: object) -> bool:
    # None and "" are wildcard lookups, which is what binding a local server does.
    if host is None or host in ("", b""):
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if not isinstance(host, str):
        return False
    name = host.split("%", 1)[0].rstrip(".").lower()
    if name == "localhost" or name.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        return False
    return address.is_loopback or address.is_unspecified


def refuse(what: str) -> None:
    # Only the frames from this project: the fifty inside httpx and ssl say
    # nothing about which test or which piece of the app went looking.
    ours = [
        frame
        for frame in traceback.extract_stack()[:-2]
        if frame.filename.startswith((str(REPOSITORY / "sf_housing"), str(REPOSITORY / "tests")))
        and frame.filename != __file__
    ]
    network.refused.append(f"{what}, from:\n{''.join(traceback.format_list(ours[-6:]))}")
    raise RealNetworkRefused(f"a test tried to reach {what}; see tests/conftest.py")


@pytest.fixture(autouse=True, scope="session")
def never_reach_the_internet():
    """Make the suite incapable of sending a request off the machine running it.

    It did, for a long time and without anybody noticing. Every full scan ends
    by asking HotPads how many homes it is holding and no scanner test stubbed
    that, while the app's scheduler asked GitHub for the newest release: when
    this guard went in, ninety-one tests across thirteen files were sending
    real requests, most of them to hotpads.com -- Zillow Group, behind
    CloudFront -- from the developer's own home IP. The app rate-limits itself
    against exactly those sites because a block lands on that address and
    takes the real scans down with it. Those tests also waited on the network,
    and behaved differently offline.

    Guarded at the socket rather than at httpx because the app reaches out
    through imaplib and the Google client as well. The fakes the suite already
    uses -- httpx.MockTransport, fastapi's TestClient, stand-in clients with a
    get method, the HTTPServer on 127.0.0.1 in test_homefinder_cli.py -- never
    open a socket to anywhere else, so they are untouched. Lookups are guarded
    as well as connections so that the guard trips on a machine that is offline,
    where the lookup would fail before any connection was attempted.
    """
    connect = socket.socket.connect
    connect_ex = socket.socket.connect_ex
    getaddrinfo = socket.getaddrinfo

    def guarded_connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6) and not network.permitted:
            if not is_this_machine(address[0]):
                refuse(f"{address[0]}:{address[1]}")
        return connect(sock, address)

    def guarded_connect_ex(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6) and not network.permitted:
            if not is_this_machine(address[0]):
                refuse(f"{address[0]}:{address[1]}")
        return connect_ex(sock, address)

    def guarded_getaddrinfo(host, port, *args, **kwargs):
        if not network.permitted and not is_this_machine(host):
            refuse(f"{host}:{port}")
        return getaddrinfo(host, port, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(socket.socket, "connect", guarded_connect)
        patch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
        patch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
        yield


@pytest.fixture(autouse=True)
def reaching_for_the_internet_fails_the_test(request: pytest.FixtureRequest, never_reach_the_internet):
    """Fail the test that went looking, even when the app caught the refusal.

    A test that genuinely has to leave the machine says so with
    @pytest.mark.real_network(reason="..."); one that only forgot to stub
    something should stub it. A request from a background thread, such as a
    scheduler job, is charged to whichever test is running when it is made, so
    trust the frames in the message over the name of the test.
    """
    marker = request.node.get_closest_marker("real_network")
    if marker is not None and not (marker.kwargs.get("reason") or marker.args):
        pytest.fail("real_network needs a reason: what this must reach, and why a fake will not do", pytrace=False)
    network.refused.clear()
    network.permitted = marker is not None
    yield
    network.permitted = False
    refused = network.refused[:]
    network.refused.clear()
    if refused:
        pytest.fail(
            "This test tried to reach the internet. Stub whatever made the request.\n\n"
            + "\n".join(refused),
            pytrace=False,
        )


@pytest.fixture(autouse=True, scope="session")
def never_touch_the_real_installation():
    """Make the suite incapable of reaching the login service of the machine
    running it.

    A test that drives the installer, or a bug that lets one reach it, writes
    ~/Library/LaunchAgents/com.sfhousing.monitor.plist -- the real one, shared
    by every install on the account. It happened: a deliberately broken guard
    let a test run the installer, and the author's own login service was left
    pointing at a pytest temporary directory that was deleted seconds later.
    The app was down until it was reinstalled by hand.

    The installer already isolates itself completely when asked; nothing was
    asking. Setting it here means no test has to remember, and a test that
    forgets is harmless rather than destructive.
    """
    import os

    previous = os.environ.get("SF_HOUSING_NO_LAUNCH_AGENT")
    os.environ["SF_HOUSING_NO_LAUNCH_AGENT"] = "1"
    yield
    if previous is None:
        os.environ.pop("SF_HOUSING_NO_LAUNCH_AGENT", None)
    else:
        os.environ["SF_HOUSING_NO_LAUNCH_AGENT"] = previous


@pytest.fixture(autouse=True)
def scans_count_no_disconnected_source(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    """Stop every scan in the suite asking hotpads.com how many homes it holds.

    After its sources, a full scan counts what each disconnected Gmail provider
    is holding, and a fresh test database has every provider disconnected and
    no count on file -- so every unscoped run_scan and start_scan in the suite
    sent a real request to hotpads.com, from whoever's machine was running it.

    Tests about that count mark themselves coverage_pass and fake fetch_count
    or the transport beneath it; never_reach_the_internet catches any that
    forget.

    A stub only covers the scans that finish while it is in place, and
    start_scan runs its scan on a thread nothing waits for. A test that
    presses Check, finishes onboarding or starts the app with its scheduler
    can end before that scan does: one instrumented full run found six
    tests in six files leaving a scan running. Most of those scans end under
    the next test's stub. One that reaches its count in the instant between
    monkeypatch lifting this stub and the next test putting its own in place
    asks hotpads.com for real, and the next test is failed for it --
    test_killing_the_scheduler_degrades_health_and_the_ready_check, at
    teardown, for the catch-up scan that
    test_health_reports_the_real_scheduler_and_says_ok started. So every
    scan a test starts is waited for here, before monkeypatch takes anything
    away -- this stub, or the fakes a coverage_pass test counts through. This
    teardown runs before monkeypatch's own because it asked for monkeypatch.
    """
    from sf_housing.scanner import Scanner

    if request.node.get_closest_marker("coverage_pass") is None:
        monkeypatch.setattr(Scanner, "_measure_missing_coverage", lambda self, client, preferences: None)

    started: list[Scanner] = []
    start_scan = Scanner.start_scan

    def start_scan_and_remember(self, *args, **kwargs):
        started.append(self)
        return start_scan(self, *args, **kwargs)

    monkeypatch.setattr(Scanner, "start_scan", start_scan_and_remember)
    yield
    for scanner in started:
        if not scanner.wait_until_idle(timeout=30):
            pytest.fail(
                "A scan this test started was still running 30 seconds after the test ended. "
                "Let it finish inside the test, or the stubs it relies on come off under it.",
                pytrace=False,
            )


@pytest.fixture(autouse=True)
def the_app_never_asks_github_for_a_release(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop every app the suite builds asking api.github.com for a new release.

    create_app gives its scheduler a job that fetches the newest release tag,
    and the tests that start the real scheduler ran it for real -- the same
    leak as the HotPads count, found by the same guard.

    Stubbed where the app binds it rather than by setting
    SF_HOUSING_NO_UPDATE_CHECK, because that switch also blanks every stored
    answer: it would hide the update line from every page test and make
    test_update_check.py's own tests return early. Those call refresh_status
    directly with a fake fetch, and this leaves them alone.
    """
    monkeypatch.setattr("sf_housing.app.refresh_status", lambda data_dir, **kwargs: None)


TEST_PREFERENCES = """
minimum_score: 60
budget:
  min_monthly: 1000
  max_monthly: 2000
preferred_neighborhoods: [NOPA, Inner Richmond, Mission]
acceptable_neighborhoods: [Bernal Heights]
private_room: true
property_types: [house, victorian, shared flat]
lease:
  min_months: 3
  max_months: 12
  flexible: true
household:
  min_people: 2
  max_people: 6
features:
  sunlight: true
  park_access: true
  outdoor_space: true
lifestyle_keywords: [communal, quiet]
dealbreakers: [live-in aide]
weights:
  price: 25
  neighborhood: 16
  private_room: 12
  property_type: 10
  lease: 8
  household: 7
  sunlight: 6
  park_access: 6
  outdoor_space: 5
  lifestyle: 5
sources:
  max_results_per_source: 120
  craigslist_detail_pages_per_scan: 0
"""


@pytest.fixture
def preferences() -> Preferences:
    return parse_preferences(TEST_PREFERENCES)


@pytest.fixture
def repository(tmp_path: Path) -> Repository:
    instance = Repository(tmp_path / "housing.sqlite3")
    instance.initialize()
    return instance

