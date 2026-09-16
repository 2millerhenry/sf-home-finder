from __future__ import annotations

import pathlib

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sf_housing.app import create_app
from sf_housing.settings import Settings
from tests.conftest import TEST_PREFERENCES


DONATE_URL = "https://ko-fi.com/example"


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


def test_an_unconfigured_donation_url_renders_no_link_at_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty has to mean silent.

    This repository now ships a real donation page, so the guarantee is no
    longer about the default being blank; it is about what happens to anyone who
    clears it, which is what a fork does first. A placeholder or an empty string
    must never put a dead link on every page of an app whose whole promise is
    that it does not send you anywhere.
    """
    monkeypatch.setattr("sf_housing.app.DONATE_URL", "")
    application = create_app(
        settings=app_settings(tmp_path), sources=[], enable_scheduler=False
    )
    with TestClient(application) as client:
        for path in ("/preferences", "/alerts", "/support"):
            page = client.get(path)
            assert page.status_code == 200, path
            assert "Say thanks" not in page.text, path
            assert "ko-fi" not in page.text.lower(), path
        # The footer itself stays, because it also carries version and licence.
        assert "site-footer" in client.get("/preferences").text


def test_a_configured_donation_url_appears_once_in_the_footer_of_every_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sf_housing.app.DONATE_URL", DONATE_URL)
    application = create_app(
        settings=app_settings(tmp_path), sources=[], enable_scheduler=False
    )
    with TestClient(application) as client:
        for path in ("/preferences", "/alerts"):
            page = client.get(path)
            assert page.status_code == 200, path
            footer = page.text[page.text.index("site-footer"):page.text.index("</footer>")]
            assert footer.count(f'href="{DONATE_URL}"') == 1, path
            assert "Say thanks" in page.text, path
            # One more lives inside the panel, as the way out when Ko-fi's
            # frame will not render. Two on the page, one of them in the
            # footer: a third would be a link nobody decided to add.
            assert page.text.count(f'href="{DONATE_URL}"') == 2, path


def test_the_donation_link_cannot_reach_back_into_the_dashboard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It is the one outbound link the app adds on its own initiative.

    A new tab opened without ``noopener`` keeps a handle on the page that
    opened it, so the destination could navigate the local dashboard.
    """
    monkeypatch.setattr("sf_housing.app.DONATE_URL", DONATE_URL)
    application = create_app(
        settings=app_settings(tmp_path), sources=[], enable_scheduler=False
    )
    with TestClient(application) as client:
        markup = client.get("/preferences").text

    anchor = markup[markup.index(f'href="{DONATE_URL}"') :]
    anchor = anchor[: anchor.index(">")]
    assert "noopener" in anchor
    assert "noreferrer" in anchor


def test_the_support_page_asks_only_once_everything_is_working(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Asking for money while the page says something is broken reads as a toll.

    The note is tied to the same report the heading is, so the ask can only
    appear underneath "Everything is working".
    """
    monkeypatch.setattr("sf_housing.app.DONATE_URL", DONATE_URL)
    application = create_app(
        settings=app_settings(tmp_path), sources=[], enable_scheduler=False
    )
    with TestClient(application) as client:
        page = client.get("/support")

    assert page.status_code == 200
    ready = "Everything is working" in page.text
    assert ("Keeping this working" in page.text) == ready


def test_the_shipped_donation_url_is_a_real_page_not_a_placeholder() -> None:
    """The opposite failure: a default nobody replaced. ko-fi.com/millerhenry
    was opened in a browser, logged out, and serves "Buy Henry Miller a Coffee"."""
    from sf_housing import DONATE_URL as SHIPPED

    if not SHIPPED:
        return  # a fork that cleared it is covered by the test above
    assert SHIPPED.startswith("https://"), SHIPPED
    for placeholder in ("yourname", "example.com", "yourhandle", "username"):
        assert placeholder not in SHIPPED, f"{SHIPPED} still looks like a placeholder"


# --------------------------------------------------------------------------
# the panel
# --------------------------------------------------------------------------


def test_the_panel_fetches_nothing_from_kofi_until_it_is_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The footer promises the app runs entirely on this computer. A widget that
    loads on every page view would tell Ko-fi each time the dashboard was
    opened, which is the one thing that promise rules out."""
    monkeypatch.setattr("sf_housing.app.DONATE_URL", DONATE_URL)
    application = create_app(settings=app_settings(tmp_path), sources=[], enable_scheduler=False)

    with TestClient(application) as client:
        page = client.get("/preferences").text

    assert 'src="about:blank"' in page, "the frame starts empty"
    assert f'src="{DONATE_URL}' not in page, "and the embed url is never a src on load"
    assert "data-donate-embed" in page, "it is held in an attribute until a click"
    assert "storage.ko-fi.com" not in page, "Ko-fi's own script never runs on this page"


def test_the_link_still_works_without_javascript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The panel is an enhancement. With scripts off, or if it fails to load,
    clicking has to go to Ko-fi exactly as it did before."""
    monkeypatch.setattr("sf_housing.app.DONATE_URL", DONATE_URL)
    application = create_app(settings=app_settings(tmp_path), sources=[], enable_scheduler=False)

    with TestClient(application) as client:
        page = client.get("/").text

    assert f'href="{DONATE_URL}" target="_blank"' in page
    assert 'rel="noopener noreferrer external"' in page


def test_no_panel_and_no_script_when_no_url_is_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sf_housing.app.DONATE_URL", "")
    application = create_app(settings=app_settings(tmp_path), sources=[], enable_scheduler=False)

    with TestClient(application) as client:
        page = client.get("/").text

    assert "donate-dialog" not in page
    assert "donate-panel.js" not in page


def test_the_panel_script_is_served(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sf_housing.app.DONATE_URL", DONATE_URL)
    application = create_app(settings=app_settings(tmp_path), sources=[], enable_scheduler=False)

    with TestClient(application) as client:
        assert client.get("/static/donate-panel.js").status_code == 200


def test_the_panel_has_one_edge_not_two(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A dialog that stays overflow:visible for its close button cannot clip the
    frame, so giving both a radius left a hairline of dialog showing at every
    corner. One surface owns the border, the radius and the clipping."""
    import pathlib
    import re

    css = pathlib.Path("sf_housing/static/style.css").read_text(encoding="utf-8")
    dialog = re.search(r"^\.donate-dialog \{([^}]*)\}", css, re.M).group(1)
    surface = re.search(r"^\.donate-surface \{([^}]*)\}", css, re.M).group(1)
    frame = re.search(r"^\.donate-dialog iframe \{([^}]*)\}", css, re.M).group(1)

    assert "border: 0" in dialog and "background: none" in dialog, dialog
    assert "border-radius" not in dialog, "the dialog is a positioning context, not a surface"
    assert "overflow: hidden" in surface and "border-radius: 18px" in surface
    assert "border-radius" not in frame, "the surface clips the frame instead"


def test_the_close_button_stays_on_screen_on_a_phone() -> None:
    """Hung outside the corner of a panel that is 94vw wide, it lands off the
    edge of a 375px display."""
    import pathlib
    import re

    css = pathlib.Path("sf_housing/static/style.css").read_text(encoding="utf-8")
    narrow = re.search(r"@media \(max-width: 540px\) \{(.*?)\n\}", css, re.S).group(1)

    assert ".donate-close" in narrow
    assert "top: 10px" in narrow and "right: 10px" in narrow, narrow


def test_the_close_button_is_a_circle_not_an_oval() -> None:
    """The shared button rule sets a 42px min-height and 9px 14px of padding for
    touch targets. A 32px round icon button inherits both and renders 31x41, so
    it has to opt out of each explicitly rather than only setting height."""
    import pathlib
    import re

    css = pathlib.Path("sf_housing/static/style.css").read_text(encoding="utf-8")
    rule = re.search(r"^\.donate-close \{([^}]*)\}", css, re.M).group(1)

    for declaration in ("width: 32px", "height: 32px", "min-width: 32px", "min-height: 32px", "padding: 0"):
        assert declaration in rule, f"{declaration} is missing, so the shared button rule wins"
    assert "border-radius: 50%" in rule


def test_the_footer_says_the_promise_in_full() -> None:
    """"Free to use" invites the question. Answering it in the same breath is
    the difference between a claim and a promise."""
    base = (
        pathlib.Path(__file__).resolve().parents[1] / "sf_housing/templates/base.html"
    ).read_text(encoding="utf-8")

    assert "Free to use, forever." in base


def test_the_footer_is_meant_to_be_noticed() -> None:
    """It is the one place the app asks for anything, and set to match the
    page it read as something already scrolled past: a shade darker than the
    page, and a size up from the rest of the small print."""
    style = (
        pathlib.Path(__file__).resolve().parents[1] / "sf_housing/static/style.css"
    ).read_text(encoding="utf-8")
    # One selector can carry more than one rule -- the footer is also named for
    # the page transition, further up the file -- so this looks for the rule
    # that paints it rather than for the first one that mentions it.
    blocks, at = [], style.find(".site-footer {")
    while at != -1:
        blocks.append(style[at : style.index("}", at) + 1])
        at = style.find(".site-footer {", at + 1)
    painted = [block for block in blocks if "background" in block]
    assert len(painted) == 1, f"{len(painted)} rules give the footer a background"
    footer = painted[0]
    paragraph = style[style.index(".footer-inner p {") : style.index("}", style.index(".footer-inner p {"))]
    size = float(paragraph.split("font-size:")[1].split("rem")[0].strip())

    assert "var(--band-strong)" in footer, footer
    assert size >= 0.9, f"the footer type is still small print at {size}rem"


def test_the_panel_shows_kofi_on_white_in_both_themes() -> None:
    """Ko-fi's widget has no dark mode: loaded with prefers-color-scheme dark
    it still renders a white card on a pale tint, measured in a browser at the
    panel's own size. Painting our surface with the app's colour therefore put
    a light card on a dark sheet at night, with the tint showing as a seam
    around it. The panel presents their card on white whatever the app's theme,
    which is the one combination that looks deliberate."""
    import pathlib
    import re

    css = pathlib.Path("sf_housing/static/style.css").read_text(encoding="utf-8")
    surface = re.search(r"^\.donate-surface \{([^}]*)\}", css, re.M).group(1)
    frame = re.search(r"^\.donate-dialog iframe \{([^}]*)\}", css, re.M).group(1)

    assert "var(--surface)" not in surface, "the panel follows the app's theme into the dark"
    assert "var(--surface)" not in frame, "the frame follows the app's theme into the dark"
    assert "#fff" in surface.lower(), surface
    assert "#fff" in frame.lower(), frame


def test_the_frame_is_the_height_the_widget_actually_needs() -> None:
    """Measured rather than guessed: at the panel's 464px width Ko-fi's card
    ends, including "Powered by Ko-fi", a little under 620px. The old 664 left
    a band of empty tint under it that read as a rendering fault."""
    import pathlib
    import re

    css = pathlib.Path("sf_housing/static/style.css").read_text(encoding="utf-8")
    frame = re.search(r"^\.donate-dialog iframe \{([^}]*)\}", css, re.M).group(1)
    height = int(re.search(r"height: (\d+)px", frame).group(1))

    assert 600 <= height <= 630, f"{height}px is not the height the widget renders at"


def test_there_is_a_way_out_when_the_widget_will_not_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ko-fi is somebody else's page in an iframe, and it can refuse to render
    inside one: driving it in a browser it answered "Captcha security check
    failed. Please try refreshing or using a different browser." A panel whose
    only content is that message is a dead end, so the dialog itself always
    offers the plain link."""
    monkeypatch.setattr("sf_housing.app.DONATE_URL", DONATE_URL)
    application = create_app(settings=app_settings(tmp_path), sources=[], enable_scheduler=False)

    with TestClient(application) as client:
        page = client.get("/").text

    dialog = page[page.index("donate-dialog"):page.index("donate-panel.js")]
    assert DONATE_URL in dialog, "the panel offers no way to reach Ko-fi if the frame fails"
    assert 'target="_blank"' in dialog


def test_the_panel_says_it_is_loading_rather_than_showing_a_blank_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ko-fi's widget took about ten seconds to appear when this was measured
    in a browser, and until it did the panel was an empty white rectangle --
    which is what a broken panel looks like too. It says what it is doing, and
    the frame covers the line when it arrives.

    Hidden on the frame's load event rather than a timer: the frame starts at
    about:blank, which fires load immediately, so the line is only armed once a
    real source has been set."""
    monkeypatch.setattr("sf_housing.app.DONATE_URL", DONATE_URL)
    application = create_app(settings=app_settings(tmp_path), sources=[], enable_scheduler=False)

    with TestClient(application) as client:
        page = client.get("/").text
        script = client.get("/static/donate-panel.js").text

    assert "data-donate-loading" in page, "nothing tells anybody the frame is on its way"
    assert "data-donate-loading" in script and '"load"' in script, (
        "the line is never taken down when the frame arrives"
    )
    # The frame is lazy: its empty about:blank settles a moment after the panel
    # opens, and that load event is not Ko-fi arriving. Taking the line down on
    # it put the blank rectangle straight back.
    uncover = script[script.index("const uncover"):]
    assert "about:blank" in uncover, (
        "the empty frame settling would be mistaken for Ko-fi arriving"
    )


def test_the_heart_breathes_slowly_and_only_when_motion_is_welcome() -> None:
    """It sits in small print at the foot of every page, so it has to be the
    quietest thing there: slow enough to read as breathing rather than
    beating, slight enough not to pull the eye off the page, and absent for
    anybody who has asked their system for less motion."""
    import pathlib
    import re

    css = pathlib.Path("sf_housing/static/style.css").read_text(encoding="utf-8")
    guarded = [
        block
        for block in re.findall(r"@media \(prefers-reduced-motion: no-preference\) \{(.*?)\n\}", css, re.S)
        if "donate-heart" in block
    ]
    assert guarded, "the heart moves even for somebody who asked it not to"

    block = guarded[0]
    seconds = float(re.search(r"donate-heart-breath (\d+(?:\.\d+)?)s", block).group(1))
    biggest = max(float(value) for value in re.findall(r"scale\((\d+(?:\.\d+)?)\)", block))
    assert seconds >= 3, f"{seconds}s is a pulse, not a breath"
    assert 1 < biggest <= 1.2, f"scale({biggest}) is too much for footer small print"
