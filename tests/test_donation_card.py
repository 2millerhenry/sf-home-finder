from __future__ import annotations

import json
import pathlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from sf_housing.app import create_app
from sf_housing.diagnostics import DiagnosticCheck, DiagnosticReport, nothing_is_broken
from sf_housing.donation_prompt import (
    ASK_LIMIT,
    FIRST_ASK_AFTER,
    SECOND_ASK_AFTER,
    PromptState,
    ask_is_due,
    installed_at,
    read_state,
    record_ask,
    record_thanks,
    resolve_first_seen,
    state_path,
    write_state,
)
from sf_housing.settings import Settings
from tests.conftest import TEST_PREFERENCES


DONATE_URL = "https://ko-fi.com/example"

NOW = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)


def app_settings(tmp_path: Path) -> Settings:
    data_dir = tmp_path / "data"
    preferences_path = tmp_path / "preferences.yaml"
    preferences_path.write_text(TEST_PREFERENCES, encoding="utf-8")
    return Settings(
        data_dir=data_dir,
        preferences_path=preferences_path,
        database_path=data_dir / "housing.sqlite3",
        log_path=data_dir / "test.log",
    )


def check(category: str, status: str, key: str = "check") -> DiagnosticCheck:
    return DiagnosticCheck(
        key=key, category=category, status=status, label=key, explanation="", action=""
    )


# What the Ready Check actually says on a working board, copied from the
# author's own install while it held nine thousand homes: the app itself is
# fine, one site is paused, another wants attention, and the last scan is
# flagged for review because of those two. Every route test below runs against
# this rather than against a spotless report, because a spotless one is not a
# thing a scraper reading twenty-three sites ever produces.
ORDINARY_TUESDAY = (
    check("Application", "pass", "local_port"),
    check("Application", "pass", "storage"),
    check("Your deal", "pass", "deal_profile"),
    check("Scanning", "attention", "scanner"),
    check("Sources", "attention", "public_sources"),
    check("Sources", "attention", "source:Zillow"),
    check("Optional connectors", "not_applicable", "gmail"),
)


def diagnosing(
    monkeypatch: pytest.MonkeyPatch,
    *checks: DiagnosticCheck,
    overall: str = "needs_attention",
) -> None:
    """Answer the Ready Check with a report of exactly these checks.

    A bare install in a temporary folder has never scanned and has no mail
    connected, so its own report describes that rather than anything these
    tests are about.
    """
    report = DiagnosticReport(
        generated_at="", overall=overall, headline="", summary="", checks=tuple(checks)
    )
    monkeypatch.setattr("sf_housing.app.run_diagnostics", lambda *args, **kwargs: report)


def working(monkeypatch: pytest.MonkeyPatch) -> None:
    diagnosing(monkeypatch, *ORDINARY_TUESDAY)


def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, donate_url: str = DONATE_URL):
    monkeypatch.setattr("sf_housing.app.DONATE_URL", donate_url)
    settings = app_settings(tmp_path)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return settings, create_app(settings=settings, sources=[], enable_scheduler=False)


def installed_on(data_dir: Path, moment: datetime, **rest: object) -> None:
    """Put an install's history on disk as the app itself would write it."""
    state_path(data_dir).write_text(
        json.dumps({"first_seen_at": moment.isoformat(), **rest}), encoding="utf-8"
    )


def stylesheet() -> str:
    return (
        pathlib.Path(__file__).resolve().parents[1] / "sf_housing/static/style.css"
    ).read_text(encoding="utf-8")


def script(name: str) -> str:
    return (
        pathlib.Path(__file__).resolve().parents[1] / f"sf_housing/static/{name}"
    ).read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# how often, and how many times ever
# --------------------------------------------------------------------------


def test_nothing_is_asked_of_an_install_in_its_first_three_days() -> None:
    """Somebody who has had this app for a day has not yet decided to keep it,
    and an app that asks for money before it has done anything is a trial."""
    fresh = PromptState(first_seen_at=NOW)

    assert ask_is_due(fresh, now=NOW) is False
    assert ask_is_due(fresh, now=NOW + timedelta(days=2, hours=23)) is False
    assert ask_is_due(fresh, now=NOW + FIRST_ASK_AFTER) is True


def test_the_window_is_counted_from_the_hour_not_from_the_date() -> None:
    """Three dates turning over is not three days. Installed on Friday evening,
    a count of calendar days would ask on Monday morning -- two nights in."""
    friday_evening = datetime(2026, 5, 1, 21, 30, tzinfo=UTC)
    state = PromptState(first_seen_at=friday_evening)

    monday_morning = datetime(2026, 5, 4, 9, 0, tzinfo=UTC)
    assert ask_is_due(state, now=monday_morning) is False
    assert ask_is_due(state, now=friday_evening + timedelta(days=3)) is True


def test_the_two_windows_are_the_ones_that_were_asked_for() -> None:
    """Three days, then a week, then silence. Pinned in a test because they are
    a decision about how this app treats people rather than an implementation
    detail -- a card arriving sooner is the thing the module exists to stop,
    and nothing else in the suite would notice the numbers being edited."""
    assert FIRST_ASK_AFTER == timedelta(days=3)
    assert SECOND_ASK_AFTER == timedelta(days=7)
    assert ASK_LIMIT == 2


def test_a_card_that_went_unanswered_comes_back_a_week_later_and_then_never() -> None:
    """Two asks in the life of an install is the whole promise. A third would
    make a liar of the first two."""
    shown = NOW + timedelta(days=3)
    asked_once = record_ask(PromptState(first_seen_at=NOW), now=shown)

    assert ask_is_due(asked_once, now=shown + timedelta(days=6, hours=23)) is False
    assert ask_is_due(asked_once, now=shown + timedelta(days=7)) is True

    asked_twice = record_ask(asked_once, now=shown + timedelta(days=7))
    assert ask_is_due(asked_twice, now=shown + timedelta(days=3650)) is False


def test_going_to_the_donation_page_ends_the_asking_for_good() -> None:
    """Whether or not anything was given. Somebody who opened the page has
    answered the question, and asking again asks them to answer it twice."""
    answered = record_thanks(PromptState(first_seen_at=NOW))

    assert ask_is_due(answered, now=NOW + FIRST_ASK_AFTER) is False
    assert ask_is_due(answered, now=NOW + timedelta(days=3650)) is False


def test_a_clock_that_was_wrong_does_not_retire_the_card_forever(tmp_path: Path) -> None:
    """A machine whose clock read 2031 when the file was written would carry a
    first_seen_at five years out, and every window counted from it would still
    be open in 2031. The stamp is clamped to now, which costs an ask three days
    after the clock is fixed rather than costing the feature."""
    installed_on(tmp_path, datetime(2031, 1, 1, tzinfo=UTC))

    state = read_state(tmp_path, now=NOW)

    assert state.first_seen_at == NOW
    assert ask_is_due(state, now=NOW + FIRST_ASK_AFTER) is True


@pytest.mark.parametrize(
    "damage", ["", "{", "null", "[]", '{"asks": "many", "first_seen_at": 7}']
)
def test_a_damaged_record_reads_as_never_asked_rather_than_as_finished(
    tmp_path: Path, damage: str
) -> None:
    """The failure has to fall this way round. Read as "already asked twice", a
    truncated file would silently retire the card on a machine that had never
    shown it, and nothing would ever look wrong."""
    state_path(tmp_path).write_text(damage, encoding="utf-8")

    state = read_state(tmp_path, now=NOW)

    assert state == PromptState()
    assert ask_is_due(resolve_first_seen(tmp_path, state), now=NOW) is False


def test_an_ask_that_could_not_be_written_down_is_not_made(tmp_path: Path) -> None:
    """A folder that cannot be written means the count never moves, and a card
    shown against a count that never moves is a card on every page load."""
    blocked = tmp_path / "blocked"
    blocked.write_text("a file where the data folder should be", encoding="utf-8")

    assert write_state(blocked, PromptState()) is False
    # And nothing half-written is left behind for the next read to inherit.
    assert list(tmp_path.glob("*.tmp")) == []


def test_an_install_is_dated_from_its_folder_not_from_the_day_this_shipped(
    tmp_path: Path,
) -> None:
    """Counting from first run would treat every install that already exists as
    brand new and make all of them wait three more days for a feature they have
    already had the app long enough to deserve."""
    folder = tmp_path / "data"
    folder.mkdir()
    created = folder.stat()

    dated = installed_at(folder)

    birth = getattr(created, "st_birthtime", None) or created.st_ctime
    assert abs(dated.timestamp() - birth) < 1
    # A folder that is not there yet cannot be dated, and answering "now" holds
    # the first ask for the full three days rather than firing it immediately.
    assert abs((installed_at(tmp_path / "absent") - datetime.now(UTC)).total_seconds()) < 5


def test_the_date_an_install_is_counted_from_is_settled_once(tmp_path: Path) -> None:
    """Linux has no creation time, and a directory's ctime there moves every
    time a file lands in it -- so a date derived on every read would walk
    forward with the data folder and never reach three days old."""
    folder = tmp_path / "data"
    folder.mkdir()

    settled = resolve_first_seen(folder, read_state(folder))
    (folder / "something.sqlite3").write_text("touched", encoding="utf-8")

    assert resolve_first_seen(folder, read_state(folder)).first_seen_at == settled.first_seen_at
    assert settled.first_seen_at is not None


# --------------------------------------------------------------------------
# the two refusals that are about the moment rather than the count
# --------------------------------------------------------------------------


def test_the_card_is_never_offered_over_a_running_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The progress panel is on the page the card would cover. Asking for money
    across a scan somebody is watching is the worst moment it could pick, and a
    scan started in another tab is invisible to the browser that would ask."""
    settings, application = board(tmp_path, monkeypatch)
    working(monkeypatch)
    installed_on(settings.data_dir, NOW - timedelta(days=30))
    application.state.scanner._progress_state.update({"running": True, "status": "running"})

    with TestClient(application) as client:
        assert client.post("/donation-card").json() == {"due": False}

    # Refused, not spent: the ask is still owed once the check is over.
    assert read_state(settings.data_dir).asks == 0


def test_the_card_is_never_offered_while_something_is_broken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An app asking for money on a morning it is not working is asking for a
    toll. The Support page already keeps its own ask underneath "Everything is
    working"; this is the same rule for the card that arrives uninvited."""
    settings, application = board(tmp_path, monkeypatch)
    diagnosing(monkeypatch, check("Application", "attention", "storage"))
    installed_on(settings.data_dir, NOW - timedelta(days=30))

    with TestClient(application) as client:
        assert client.post("/donation-card").json() == {"due": False}

    assert read_state(settings.data_dir).asks == 0


def test_a_listings_site_having_a_bad_morning_is_not_the_app_being_broken() -> None:
    """The first gate here was ``overall == "ready"``, and a real board showed
    what that meant: "ready" needs every one of thirty-odd checks to pass, five
    of them per-source, and on a working install with nine thousand homes
    Zillow wanted attention, RentSFNow was paused, and the last scan was
    flagged for review because of those two. The card would never have appeared
    once, on any machine, and nothing would have looked wrong."""
    ordinary = DiagnosticReport(
        generated_at="", overall="needs_attention", headline="", summary="", checks=ORDINARY_TUESDAY
    )

    assert ordinary.overall != "ready"
    assert nothing_is_broken(ordinary) is True


@pytest.mark.parametrize(
    "broken",
    [
        check("Application", "attention", "local_port"),
        check("Application", "unknown", "storage"),
        check("Your deal", "attention", "deal_profile"),
        check("Startup", "attention", "login_item"),
        check("Sources", "blocked", "source:Craigslist"),
    ],
    ids=["port", "storage", "deal", "startup", "blocked-anywhere"],
)
def test_the_app_s_own_machinery_failing_does_silence_the_card(
    broken: DiagnosticCheck,
) -> None:
    """The line is the app itself, not the weather outside it. Anything blocked
    is a required check failing wherever it lives; everything else has to be in
    the app, the deal or the way it starts before it counts as broken."""
    report = DiagnosticReport(
        generated_at="",
        overall="needs_attention",
        headline="",
        summary="",
        checks=(*ORDINARY_TUESDAY, broken),
    )

    assert nothing_is_broken(report) is False


def test_an_unfinished_deal_is_not_yet_an_app_to_be_asked_about() -> None:
    """The dashboard sends somebody without a saved deal to the setup page, so
    a card over the board is a card over a page they are not on."""
    report = DiagnosticReport(
        generated_at="", overall="setup_incomplete", headline="", summary="", checks=()
    )

    assert nothing_is_broken(report) is False


def test_a_fork_that_cleared_the_donation_url_is_never_asked_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Clearing the link is what a fork does first, and it has to silence the
    card as completely as it silences the footer button."""
    settings, application = board(tmp_path, monkeypatch, donate_url="")
    working(monkeypatch)
    installed_on(settings.data_dir, NOW - timedelta(days=30))

    with TestClient(application) as client:
        assert client.post("/donation-card").json() == {"due": False}
        assert "data-thanks-card" not in client.get("/").text
        assert client.get("/static/donation-card.js").status_code == 200
        assert "donation-card.js" not in client.get("/").text


# --------------------------------------------------------------------------
# the route
# --------------------------------------------------------------------------


def test_showing_the_card_is_counted_by_the_request_that_allows_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Counted on a second call from the browser, a closed tab or a script that
    threw in between would leave the card shown and never recorded -- and an
    ask that is never recorded is an ask on every page load from then on."""
    settings, application = board(tmp_path, monkeypatch)
    working(monkeypatch)
    installed_on(settings.data_dir, NOW - timedelta(days=30))

    with TestClient(application) as client:
        assert client.post("/donation-card").json() == {"due": True}
        recorded = read_state(settings.data_dir)
        assert recorded.asks == 1
        assert recorded.asked_at is not None
        # The second is a week out, so reloading the dashboard cannot produce
        # another one this morning.
        assert client.post("/donation-card").json() == {"due": False}


def test_the_thanks_route_is_the_end_of_the_asking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, application = board(tmp_path, monkeypatch)
    working(monkeypatch)
    installed_on(settings.data_dir, NOW - timedelta(days=30))

    with TestClient(application) as client:
        assert client.post("/donation-card").json() == {"due": True}
        assert client.post("/donation-card/thanks").status_code == 204

    state = read_state(settings.data_dir)
    assert ask_is_due(state, now=datetime.now(UTC) + timedelta(days=3650)) is False


def test_the_footer_button_also_ends_the_asking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The promise is "went to the donation page", not "used this card". The
    footer's button is the way to give at every other moment, and somebody who
    has just used it must not be shown a card asking them to."""
    panel = script("donate-panel.js")
    click = panel[panel.index("[data-donate-open]") : panel.index('document.addEventListener("donate-open"')]
    assert "thanked()" in click, "a cmd-click opens Ko-fi in a tab and would go unrecorded"


def test_every_way_the_panel_opens_records_the_arrival() -> None:
    """Driven in a browser against the real widget, this was wrong: the card
    hands over to the panel by dispatching an event, so a recording hung on the
    footer's own click handler never ran for it. The panel opened, Ko-fi's card
    loaded, somebody could have given -- and the app went on planning a second
    ask a week later. The recording belongs to opening the panel, not to any
    one of the controls that can open it."""
    panel = script("donate-panel.js")
    opener = panel[panel.index("const open = () => {") :]
    opener = opener[: opener.index("\n  };")]

    assert "thanked()" in opener
    assert "/donation-card/thanks" in panel


# --------------------------------------------------------------------------
# the card on the page
# --------------------------------------------------------------------------


def test_the_card_is_on_the_board_and_on_no_other_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """"Every so often when you open it" means the board. Somebody part-way
    through editing their deal or connecting a mailbox is in the middle of
    something, and the footer's button is already there if they want it."""
    _, application = board(tmp_path, monkeypatch)

    with TestClient(application) as client:
        assert "data-thanks-card" in client.get("/").text
        for path in ("/preferences", "/alerts", "/support"):
            assert "data-thanks-card" not in client.get(path).text, path


def test_the_card_leaves_the_rest_of_the_pages_with_two_donation_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One in the footer, one inside the panel as the way out when Ko-fi's
    frame will not render. The card adds a third, and it may only add it to the
    page it is actually on."""
    _, application = board(tmp_path, monkeypatch)

    with TestClient(application) as client:
        assert client.get("/preferences").text.count(f'href="{DONATE_URL}"') == 2
        assert client.get("/").text.count(f'href="{DONATE_URL}"') == 3


def test_the_card_says_the_app_s_name_even_on_a_phone() -> None:
    """The header trades the wordmark for the bridge below 760px, because seven
    to one will not fit beside the tabs. Inside a card nothing is competing for
    the room, and a note from an app that never names itself is a note from
    nobody."""
    style = stylesheet()

    assert ".thanks-brand .brand-lockup { display: block;" in style
    assert ".thanks-brand .brand-mark { display: none; }" in style


def test_the_card_draws_the_real_bridge_rather_than_a_second_copy_of_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The artwork was written out in full in both of the header's drawings and
    was about to be written out a third time. One partial holds it now, and a
    change to the logo has to reach every place it is drawn."""
    brand = (
        pathlib.Path(__file__).resolve().parents[1] / "sf_housing/templates/_brand.html"
    ).read_text(encoding="utf-8")
    assert brand.count('{% include "_bridge.html" %}') == 2
    assert "M2905 2785" not in brand, "the bridge path is written out here again"

    _, application = board(tmp_path, monkeypatch)
    with TestClient(application) as client:
        page = client.get("/").text

    card = page[page.index("thanks-card") :]
    assert 'class="thanks-watermark"' in card
    assert "M2905 2785" in card, "the watermark is not the logo"


def test_the_card_hands_over_to_the_one_panel_rather_than_opening_a_second(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ko-fi is loaded in one place and only when asked. A card that opened its
    own frame would fetch from Ko-fi twice on one page, and the promise the
    footer makes above it is that opening the dashboard fetches nothing."""
    card = script("donation-card.js")
    panel = script("donate-panel.js")

    assert 'CustomEvent("donate-open")' in card
    assert 'document.addEventListener("donate-open"' in panel
    assert "iframe" not in card and "ko-fi.com" not in card.lower()
    # The hand-over waits for the card's own exit, so the two surfaces are
    # never both on screen. Written as a then, not as a call beside it.
    assert 'leave("yes").then(' in card

    _, application = board(tmp_path, monkeypatch)
    with TestClient(application) as client:
        page = client.get("/").text
    assert page.count("data-donate-frame") == 1
    assert 'src="about:blank"' in page


def test_the_panel_opens_from_every_control_that_asks_for_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It answered ``querySelector``, so it was wired to whichever trigger came
    first in the document and any second one did nothing at all."""
    panel = script("donate-panel.js")

    assert 'querySelectorAll("[data-donate-open]")' in panel
    assert 'querySelector("[data-donate-open]")' not in panel

    _, application = board(tmp_path, monkeypatch)
    with TestClient(application) as client:
        page = client.get("/").text
    # The embed url belongs to the panel, not to whichever control opened it.
    dialog = page[page.index("<dialog class=\"donate-dialog\"") :]
    assert "data-donate-embed" in dialog[: dialog.index(">")]


def test_the_card_declines_itself_while_a_check_is_running() -> None:
    """The route refuses this too, but the browser holds the one fact the route
    cannot see quickly: the progress panel is on this very page, and the card
    would be laid straight over it."""
    card = script("donation-card.js")

    assert "[data-scan-progress]" in card
    assert card.index("[data-scan-progress]") < card.index("SETTLE_MS"), (
        "the check happens after the card has already been scheduled"
    )


def test_the_card_waits_for_somebody_to_be_looking_at_the_page() -> None:
    """A dashboard restored into a background tab would otherwise spend one of
    two lifetime asks on a card nobody ever saw."""
    card = script("donation-card.js")

    assert "visibilityState" in card and "visibilitychange" in card


def test_the_card_never_moves_for_somebody_who_asked_for_less_motion() -> None:
    """It arrives unasked and covers the whole board. Of everything on this
    page it is the one thing that must not animate at anybody who has told
    their system they do not want that."""
    style = stylesheet()
    guarded = style[style.index("@media (prefers-reduced-motion: no-preference) {", style.index(".thanks-card {")) :]

    for keyframes in (
        "thanks-in",
        "thanks-rise",
        "thanks-out",
        "thanks-onward",
        "thanks-veil",
        "thanks-veil-out",
    ):
        # The brace matters: "thanks-veil" is a prefix of "thanks-veil-out".
        declaration = f"@keyframes {keyframes} {{"
        assert declaration in guarded, keyframes
        assert style.count(declaration) == 1, keyframes
    # And the script does not sit waiting for an animation that will never run.
    assert "prefers-reduced-motion: reduce" in script("donation-card.js")


def test_the_card_is_taken_down_by_its_own_exit_rather_than_by_escape() -> None:
    """Escape closes a modal dialog outright, which cut the exit off at its
    first frame -- the card vanished where every other way out of it faded."""
    card = script("donation-card.js")

    cancel = card[card.index('addEventListener("cancel"') :]
    assert "preventDefault" in cancel[:200]
    # An animation that never reports back must not leave the card on the board.
    assert "EXIT_CEILING_MS" in card and "animationend" in card
