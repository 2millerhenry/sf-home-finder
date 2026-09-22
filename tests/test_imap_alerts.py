"""Reading alert mail over IMAP with an app password.

The Gmail API path needs an owner-supplied OAuth client because gmail.readonly is
a restricted scope. IMAP needs a 16-character app password and nothing else, so
these tests cover the translation layer, the fetch behaviour, and — the real
proof — that the existing alert sources cannot tell which backend they are on.
"""

from __future__ import annotations

import email.message
import imaplib
import json
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sf_housing.gmail_alerts import AlertEmail
from sf_housing.imap_alerts import (
    AlertMailboxRouter,
    ImapAlertError,
    ImapAlertMailbox,
    host_for_address,
    message_to_alert,
    translate_query,
)
from sf_housing.sources import ZillowAlertSource

def already_works_headline(html: str) -> str:
    """The page's own claim about how many sources need no setup, checked
    against the chips it actually renders beside it.

    The sentence used to be a literal that said "six" while seven sources ran.
    Counting the chips is what makes the claim true for whatever list the app
    was built with, rather than true only for the default one."""
    import re as _re

    from sf_housing.app import spelled_count

    headline = _re.search(r"<h2 id=\"already-title\">([^<]+)</h2>", html)
    assert headline, "the page no longer states what already works"
    section = html[html.index('id="already-title"') : html.index("</section>", html.index('id="already-title"'))]
    chips = _re.findall(r'class="source-chip"', section)
    assert headline.group(1) == f"{spelled_count(len(chips))} sources already work", (
        f"headline {headline.group(1)!r} disagrees with the {len(chips)} sources listed beside it"
    )
    return headline.group(1)



NOW = datetime(2026, 9, 3, tzinfo=UTC)

ZILLOW_HTML = (
    '<table><tr><td><a href="https://www.zillow.com/homedetails/'
    '123-NOPA-San-Francisco-CA-94107/123_zpid/">123 NOPA, San Francisco</a>'
    "<p>$1,500/mo · Sunny rental</p></td></tr></table>"
)


# --------------------------------------------------------------------------
# query translation
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query,expected",
    [
        ("from:(zillow.com) newer_than:90d", ["FROM", "zillow.com", "SINCE", "05-Jun-2026"]),
        ("from:(hotpads.com) newer_than:90d", ["FROM", "hotpads.com", "SINCE", "05-Jun-2026"]),
        ("from:(roomies.com) newer_than:90d", ["FROM", "roomies.com", "SINCE", "05-Jun-2026"]),
        ("from:(apartments.com) newer_than:90d", ["FROM", "apartments.com", "SINCE", "05-Jun-2026"]),
        (
            "from:(facebookmail.com) Marketplace newer_than:90d",
            ["FROM", "facebookmail.com", "SINCE", "05-Jun-2026", "TEXT", "Marketplace"],
        ),
    ],
)
def test_every_shipped_query_translates(query: str, expected: list[str]) -> None:
    """These five are what the product actually sends; a mistake here reads as 'no alerts'."""
    assert translate_query(query, now=NOW) == expected


def test_an_empty_query_is_not_silently_everything_unbounded() -> None:
    assert translate_query("", now=NOW) == ["ALL"]


def test_unknown_gmail_operators_are_dropped_not_sent_as_text() -> None:
    """`has:attachment` as a literal TEXT term would match nothing at all."""
    assert translate_query("from:(x.com) has:attachment label:inbox", now=NOW) == ["FROM", "x.com"]


def test_relative_windows_other_than_days_are_understood() -> None:
    assert translate_query("newer_than:1y", now=NOW)[:1] == ["SINCE"]
    assert translate_query("newer_than:2m", now=NOW)[:1] == ["SINCE"]


def test_a_bare_search_term_becomes_a_text_match() -> None:
    assert translate_query("Marketplace", now=NOW) == ["TEXT", "Marketplace"]


# --------------------------------------------------------------------------
# message parsing
# --------------------------------------------------------------------------


def build_message(html: str = ZILLOW_HTML, text: str = "plain body", *, attachment: bool = False) -> bytes:
    message = email.message.EmailMessage()
    message["Subject"] = "New listing for your NOPA search"
    message["From"] = "alerts@zillow.com"
    message["Message-ID"] = "<zillow-1@example.test>"
    message.set_content(text)
    message.add_alternative(html, subtype="html")
    if attachment:
        message.add_attachment(b"not listing content", maintype="application", subtype="pdf", filename="x.pdf")
    return message.as_bytes()


def test_a_multipart_alert_becomes_an_alert_email() -> None:
    alert = message_to_alert(build_message(), "1")

    assert alert.message_id == "<zillow-1@example.test>"
    assert "NOPA" in alert.subject
    assert "zillow.com/homedetails" in alert.html
    assert "plain body" in alert.text


def test_attachments_are_not_treated_as_listing_content() -> None:
    alert = message_to_alert(build_message(attachment=True), "1")

    assert "not listing content" not in alert.html
    assert "not listing content" not in alert.text


def test_an_encoded_subject_is_decoded() -> None:
    raw = (
        b"Subject: =?utf-8?q?Sunny_room_in_NOPA?=\r\n"
        b"Message-ID: <a@b>\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n\r\nbody\r\n"
    )

    assert message_to_alert(raw, "1").subject == "Sunny room in NOPA"


def test_a_message_with_no_id_falls_back_to_the_server_identifier() -> None:
    raw = b"Subject: x\r\nContent-Type: text/plain\r\n\r\nbody\r\n"

    assert message_to_alert(raw, "42").message_id == "42"


# --------------------------------------------------------------------------
# a fake IMAP server
# --------------------------------------------------------------------------


class FakeIMAP:
    """Enough of imaplib.IMAP4_SSL to drive the real code paths."""

    instances: list["FakeIMAP"] = []

    def __init__(self, host, timeout=None, ssl_context=None, messages=None, login_error=False):
        self.host = host
        self.messages_by_id = messages or {b"1": build_message()}
        self.login_error = login_error
        self.commands: list[tuple] = []
        self.logged_out = False
        FakeIMAP.instances.append(self)

    def login(self, user, password):
        self.commands.append(("login", user))
        if self.login_error:
            raise imaplib.IMAP4.error("AUTHENTICATIONFAILED")
        return "OK", [b"logged in"]

    def select(self, folder, readonly=False):
        self.commands.append(("select", folder, readonly))
        return "OK", [b"1"]

    def search(self, charset, *criteria):
        self.commands.append(("search", criteria))
        return "OK", [b" ".join(self.messages_by_id)]

    def fetch(self, identifier, spec):
        self.commands.append(("fetch", identifier, spec))
        return "OK", [(b"1 (BODY[] {n}", self.messages_by_id[identifier])]

    def close(self):
        self.commands.append(("close",))

    def logout(self):
        self.logged_out = True


def connected_mailbox(tmp_path: Path, **kwargs) -> ImapAlertMailbox:
    FakeIMAP.instances.clear()
    mailbox = ImapAlertMailbox(
        tmp_path / "imap-credential.json",
        connector=lambda host, **kw: FakeIMAP(host, **kw, **kwargs),
    )
    mailbox.save_credential("someone@gmail.com", "abcd efgh ijkl mnop")
    return mailbox


# --------------------------------------------------------------------------
# the mailbox
# --------------------------------------------------------------------------


def test_a_saved_credential_counts_as_connected(tmp_path: Path) -> None:
    mailbox = ImapAlertMailbox(tmp_path / "c.json")
    assert mailbox.is_connected is False

    mailbox.save_credential("someone@gmail.com", "abcd efgh ijkl mnop")

    assert mailbox.is_connected is True
    assert mailbox.credential.host == "imap.gmail.com", "gmail's server should not have to be typed"


def test_the_password_is_written_only_for_its_owner(tmp_path: Path) -> None:
    path = tmp_path / "imap-credential.json"
    ImapAlertMailbox(path).save_credential("someone@gmail.com", "secret-app-password")

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text())["password"] == "secret-app-password"


def test_outlook_is_refused_with_the_actual_reason(tmp_path: Path) -> None:
    with pytest.raises(ImapAlertError, match="no longer allow app passwords"):
        ImapAlertMailbox(tmp_path / "c.json").save_credential("someone@outlook.com", "pw")


def test_an_unknown_provider_asks_for_its_server(tmp_path: Path) -> None:
    with pytest.raises(ImapAlertError, match="mail server"):
        ImapAlertMailbox(tmp_path / "c.json").save_credential("someone@example.test", "pw")

    ImapAlertMailbox(tmp_path / "c.json").save_credential(
        "someone@example.test", "pw", "imap.example.test"
    )


@pytest.mark.parametrize("address,password", [("nope", "pw"), ("someone@gmail.com", "  ")])
def test_incomplete_details_are_rejected(tmp_path: Path, address: str, password: str) -> None:
    with pytest.raises(ImapAlertError):
        ImapAlertMailbox(tmp_path / "c.json").save_credential(address, password)


def test_messages_never_mark_the_users_mail_as_read(tmp_path: Path) -> None:
    """Reading someone's alerts must not change their inbox."""
    mailbox = connected_mailbox(tmp_path)

    mailbox.messages("from:(zillow.com) newer_than:90d")

    server = FakeIMAP.instances[-1]
    fetches = [c for c in server.commands if c[0] == "fetch"]
    assert fetches and all(spec == "(BODY.PEEK[])" for _, _, spec in fetches)
    assert ("select", "INBOX", True) in server.commands, "the folder must be opened read-only"


def test_messages_sends_the_translated_search(tmp_path: Path) -> None:
    mailbox = connected_mailbox(tmp_path)

    mailbox.messages("from:(zillow.com) newer_than:90d")

    criteria = next(c[1] for c in FakeIMAP.instances[-1].commands if c[0] == "search")
    assert criteria[0] == "FROM" and criteria[1] == "zillow.com"


def test_messages_respects_the_result_cap(tmp_path: Path) -> None:
    many = {str(index).encode(): build_message() for index in range(1, 40)}
    mailbox = connected_mailbox(tmp_path, messages=many)

    assert len(mailbox.messages("from:(zillow.com)", max_results=5)) == 5


def test_the_connection_is_always_closed(tmp_path: Path) -> None:
    mailbox = connected_mailbox(tmp_path)

    mailbox.messages("from:(zillow.com)")

    assert FakeIMAP.instances[-1].logged_out is True


def test_a_refused_password_says_what_to_check(tmp_path: Path) -> None:
    mailbox = connected_mailbox(tmp_path, login_error=True)

    with pytest.raises(ImapAlertError, match="two-step verification"):
        mailbox.messages("from:(zillow.com)")


def test_reading_without_a_credential_is_an_error_not_an_empty_list(tmp_path: Path) -> None:
    with pytest.raises(ImapAlertError, match="No mail account"):
        ImapAlertMailbox(tmp_path / "c.json").messages("from:(zillow.com)")


def test_disconnect_removes_the_stored_password(tmp_path: Path) -> None:
    mailbox = connected_mailbox(tmp_path)

    assert mailbox.disconnect() is True
    assert mailbox.is_connected is False
    assert mailbox.disconnect() is False


# --------------------------------------------------------------------------
# the point of the whole exercise
# --------------------------------------------------------------------------


def test_an_alert_source_cannot_tell_which_backend_it_is_on(tmp_path, preferences) -> None:
    """The real proof: the same source, the same result, over IMAP instead of Gmail."""
    mailbox = connected_mailbox(tmp_path)

    listings = ZillowAlertSource(mailbox).search(None, preferences)

    assert len(listings) == 1
    assert listings[0].platform == "Zillow"
    assert listings[0].price == 1500
    assert "zillow.com/homedetails" in listings[0].original_url


def test_host_lookup_covers_the_providers_we_promise(tmp_path: Path) -> None:
    assert host_for_address("a@gmail.com") == "imap.gmail.com"
    assert host_for_address("a@icloud.com") == "imap.mail.me.com"
    assert host_for_address("a@fastmail.com") == "imap.fastmail.com"
    assert host_for_address("a@example.test") is None


# --------------------------------------------------------------------------
# the router
# --------------------------------------------------------------------------


class StubGmail:
    is_connected = False

    def messages(self, query, max_results=100):
        return [AlertEmail(message_id="gmail", subject="via gmail", html="", text="")]


def test_the_router_prefers_a_saved_app_password(tmp_path: Path) -> None:
    imap = connected_mailbox(tmp_path)
    router = AlertMailboxRouter(imap, StubGmail())

    assert router.backend == "imap"
    assert router.is_connected is True
    assert router.messages("from:(zillow.com)")[0].message_id != "gmail"


def test_the_router_falls_back_to_google_when_no_password_is_saved(tmp_path: Path) -> None:
    gmail = StubGmail()
    gmail.is_connected = True
    router = AlertMailboxRouter(ImapAlertMailbox(tmp_path / "none.json"), gmail)

    assert router.backend == "gmail"
    assert router.messages("from:(zillow.com)")[0].message_id == "gmail"


def test_connecting_takes_effect_without_a_restart(tmp_path: Path) -> None:
    """The backend is chosen per call, not once at startup."""
    imap = ImapAlertMailbox(
        tmp_path / "c.json", connector=lambda host, **kw: FakeIMAP(host, **kw)
    )
    router = AlertMailboxRouter(imap, StubGmail())
    assert router.backend == "gmail"

    imap.save_credential("someone@gmail.com", "abcd efgh ijkl mnop")

    assert router.backend == "imap"


# --------------------------------------------------------------------------
# the connect / disconnect routes
# --------------------------------------------------------------------------


def app_with_fake_imap(tmp_path: Path, monkeypatch, **kwargs):
    from fastapi.testclient import TestClient

    from sf_housing import app as app_module
    from sf_housing.settings import Settings
    from tests.conftest import TEST_PREFERENCES

    FakeIMAP.instances.clear()
    original = app_module.ImapAlertMailbox

    def factory(path):
        return original(path, connector=lambda host, **kw: FakeIMAP(host, **kw, **kwargs))

    monkeypatch.setattr(app_module, "ImapAlertMailbox", factory)

    data = tmp_path / "data"
    preferences = tmp_path / "preferences.yaml"
    preferences.write_text(TEST_PREFERENCES, encoding="utf-8")
    settings = Settings(
        data_dir=data,
        preferences_path=preferences,
        database_path=data / "housing.sqlite3",
        log_path=data / "test.log",
        imap_credential_path=data / "imap-credential.json",
    )
    application = app_module.create_app(settings=settings, sources=[], enable_scheduler=False)
    return TestClient(application), settings


def test_connecting_email_saves_and_verifies(tmp_path: Path, monkeypatch) -> None:
    client, settings = app_with_fake_imap(tmp_path, monkeypatch)

    with client:
        response = client.post(
            "/alerts/email/connect",
            data={"address": "someone@gmail.com", "password": "abcd efgh ijkl mnop"},
            headers={"Origin": "http://testserver"},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert "error" not in response.headers["location"]
    assert settings.imap_credential_path.is_file()
    # It must actually have talked to the server rather than trusting the paste.
    assert FakeIMAP.instances, "saving a password must be proved with a real search"


def test_a_password_that_does_not_work_is_not_kept(tmp_path: Path, monkeypatch) -> None:
    """A saved-but-broken credential would silently disable every alert source."""
    client, settings = app_with_fake_imap(tmp_path, monkeypatch, login_error=True)

    with client:
        response = client.post(
            "/alerts/email/connect",
            data={"address": "someone@gmail.com", "password": "wrong"},
            headers={"Origin": "http://testserver"},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    assert not settings.imap_credential_path.exists(), "a refused password must not be stored"


def test_outlook_is_refused_by_the_route_with_a_useful_message(tmp_path: Path, monkeypatch) -> None:
    client, settings = app_with_fake_imap(tmp_path, monkeypatch)

    with client:
        response = client.post(
            "/alerts/email/connect",
            data={"address": "someone@outlook.com", "password": "pw"},
            headers={"Origin": "http://testserver"},
            follow_redirects=False,
        )

    assert "error=" in response.headers["location"]
    assert "app%20passwords" in response.headers["location"]
    assert not settings.imap_credential_path.exists()


def test_disconnecting_email_removes_the_credential(tmp_path: Path, monkeypatch) -> None:
    client, settings = app_with_fake_imap(tmp_path, monkeypatch)

    with client:
        client.post(
            "/alerts/email/connect",
            data={"address": "someone@gmail.com", "password": "abcd efgh ijkl mnop"},
            headers={"Origin": "http://testserver"},
            follow_redirects=False,
        )
        assert settings.imap_credential_path.is_file()

        client.post(
            "/alerts/email/disconnect",
            headers={"Origin": "http://testserver"},
            follow_redirects=False,
        )

    assert not settings.imap_credential_path.exists()


def test_the_alerts_page_asks_for_a_password_not_a_cloud_project(tmp_path: Path, monkeypatch) -> None:
    client, _ = app_with_fake_imap(tmp_path, monkeypatch)

    with client:
        page = client.get("/alerts")

    assert page.status_code == 200
    assert 'action="/alerts/email/connect"' in page.text
    assert "App password" in page.text
    # Google Cloud must not be the first thing a stranger is asked for, and the
    # page must lead with what already works rather than with a setup request.
    assert page.text.index(already_works_headline(page.text)) < page.text.index("App password")
    assert page.text.index("App password") < page.text.index("can&rsquo;t make app passwords")


def test_the_password_is_never_echoed_back_to_the_page(tmp_path: Path, monkeypatch) -> None:
    client, _ = app_with_fake_imap(tmp_path, monkeypatch)

    with client:
        client.post(
            "/alerts/email/connect",
            data={"address": "someone@gmail.com", "password": "sup3r-s3cret-value"},
            headers={"Origin": "http://testserver"},
            follow_redirects=False,
        )
        page = client.get("/alerts")

    assert "sup3r-s3cret-value" not in page.text
    assert "someone@gmail.com" in page.text, "the address is safe to confirm back"


# --------------------------------------------------------------------------
# the one place someone follows instructions on a different website
# --------------------------------------------------------------------------


def test_every_auto_detected_provider_has_its_own_steps() -> None:
    """The page claims a provider is recognised from the address alone. Each one
    it claims has to come with instructions someone can actually follow."""
    import pathlib
    import re

    from sf_housing.imap_alerts import KNOWN_HOSTS

    page = pathlib.Path("sf_housing/templates/alerts.html").read_text(encoding="utf-8")
    # The app-password guides, not the alert-setup ones further down the page.
    start = page.index("How do I get an app password?")
    guides = page[start : page.index("Using a different provider?", start)]

    for domain in KNOWN_HOSTS:
        assert domain in guides, f"{domain} is auto-detected but has no instructions"


def test_the_steps_name_the_buttons_people_will_be_looking_at() -> None:
    """Vague instructions are the failure mode here: "create an app password in
    the security section" is not something a person can follow on a page they
    have never seen."""
    import pathlib

    page = pathlib.Path("sf_housing/templates/alerts.html").read_text(encoding="utf-8")

    for phrase in (
        "myaccount.google.com/apppasswords",
        "2-Step Verification",
        "Sign-In and Security",
        "App-Specific Passwords",
        "Generate app password",
        "Mail (IMAP/POP/SMTP)",
    ):
        assert phrase in page, f"the steps no longer name {phrase!r}"


def test_an_unsupported_provider_is_told_plainly() -> None:
    """Outlook cannot work this way. Saying so, and saying what to do instead,
    beats leaving someone to fail at step three."""
    import pathlib

    page = pathlib.Path("sf_housing/templates/alerts.html").read_text(encoding="utf-8")

    assert "no longer issues app passwords" in page
    assert "different mailbox" in page, "say what they can do instead"


def test_the_other_provider_form_is_not_a_second_copy_of_the_first() -> None:
    """Two identical Connect forms on one page read as a mistake. The second one
    exists for providers the address cannot identify, and has to say so."""
    import pathlib

    page = pathlib.Path("sf_housing/templates/alerts.html").read_text(encoding="utf-8")

    assert "Using a different provider?" in page
    assert 'name="host"' in page, "and it is the one that asks for a mail server"


def test_every_named_source_carries_a_mark(tmp_path) -> None:
    """A page that lists a dozen service names as running text is a wall. Each
    one gets a mark so it can be found at a glance.

    The free sources are read from ``default_sources`` rather than named here,
    because the page renders that same list: a source added to the app but not
    to the mark table would otherwise ship wearing its first letter in another
    source's colour, and a hardcoded list here could not notice."""
    import pathlib
    import re

    from sf_housing.connectors import GMAIL_PROVIDERS
    from sf_housing.sources import default_sources

    page = pathlib.Path("sf_housing/templates/alerts.html").read_text(encoding="utf-8")
    marks = re.search(r"\{% set source_marks = \{(.*?)\} %\}", page, re.S).group(1)

    for _, name in GMAIL_PROVIDERS:
        assert f"'{name}'" in marks, f"{name} is offered on this page but has no mark"
    free = [
        source.platform
        for source in default_sources()
        if getattr(source, "mode", "") == "automatic"
        and not getattr(source, "connector_key", None)
    ]
    assert len(free) >= 9, "the page's own free-source list should not have shrunk"
    for name in free:
        assert f"'{name}'" in marks, f"{name} runs for free but has no mark"


def test_no_source_mark_reaches_outside_this_computer() -> None:
    """The page must keep working offline and must tell no third party it was
    opened.

    This used to say "no <img> at all", which was the same thing while the only
    way to show a site's logo was to hotlink it. The chips carry real logos
    now, fetched once and served from this app's own static directory, so the
    check is the property itself: every image on the chip comes from here, and
    no absolute URL to anyone else appears anywhere in it. Hotlinking would
    tell twenty-two companies each time this page is opened and leave the page
    blank on a train."""
    import pathlib
    import re

    page = pathlib.Path("sf_housing/templates/alerts.html").read_text(encoding="utf-8")
    chip = re.search(r'<span class="source-chip"[^>]*>.*?\{% endmacro %\}', page, re.S).group(0)

    assert "http://" not in chip and "https://" not in chip, chip
    assert "//" not in chip.replace("https://", "").replace("http://", ""), chip
    for source in re.findall(r'src="([^"]*)"', chip):
        assert source.startswith("{{ url_for('static'"), source

    # And the files those paths point at are really in the repository, not
    # fetched at render time from somewhere.
    icons = pathlib.Path("sf_housing/static/source-icons")
    assert icons.is_dir() and list(icons.glob("*.png"))


def test_the_gmail_steps_lead_with_the_thing_that_blocks_people() -> None:
    """2-Step Verification is the actual obstacle: without it the app-password
    page does not offer what the steps promise. It comes before step one, not
    buried inside it."""
    import pathlib

    page = pathlib.Path("sf_housing/templates/alerts.html").read_text(encoding="utf-8")
    first = page.index("app passwords do not exist until 2-Step Verification")
    steps = page.index("myaccount.google.com/apppasswords")

    assert first < steps, "the prerequisite has to come first"
    assert "setup-first" in page


def test_every_source_the_page_names_is_in_the_mark_table() -> None:
    """source_chip falls back to the first letter and one shared hue for a name
    it does not know, which is how Furnished Finder came to wear an "F" in the
    housing portal's green. Reading the table alone cannot catch that."""
    import pathlib
    import re

    from sf_housing.sources import default_sources

    page = pathlib.Path("sf_housing/templates/alerts.html").read_text(encoding="utf-8")
    block = re.search(r"\{% set source_marks = \{(.*?)\} %\}", page, re.S).group(1)
    known = set(re.findall(r"'([^']+)':", block))
    used = set(re.findall(r"source_chip\('([^']+)'\)", page))
    # The free sources reach the macro through a context variable, so the names
    # cannot be read out of the markup. They come from the same call the page
    # renders from.
    used |= {
        source.platform
        for source in default_sources()
        if getattr(source, "mode", "") == "automatic"
        and not getattr(source, "connector_key", None)
    }

    missing = sorted(used - known)
    assert not missing, f"these fall back to a letter and a shared hue: {missing}"

    # The fallback hue must belong to no source, or an unlisted name would be
    # indistinguishable from a listed one rather than merely plain.
    fallback = re.search(r"source_marks\.get\(name, \(name\[0\], '(\d+)', '(\d+)'\)\)", page).groups()
    taken = set(re.findall(r"\('[^']+',\s*'(\d+)',\s*'(\d+)'\)", block))
    assert fallback not in taken, "an unlisted name must not wear a listed source's colour"


def test_no_two_sources_share_a_colour() -> None:
    """The mark exists so a source can be found at a glance, which two sources
    in the same colour defeats.

    Hue alone stopped being enough at seventeen names: a circle holds eighteen
    at the twenty degrees it takes to tell two apart. Lightness is the second
    dimension, so the rule is now that no two share *both*, and that within one
    lightness the hues are still twenty apart -- which is the thing an eye
    actually does."""
    import pathlib
    import re

    page = pathlib.Path("sf_housing/templates/alerts.html").read_text(encoding="utf-8")
    block = re.search(r"\{% set source_marks = \{(.*?)\} %\}", page, re.S).group(1)
    marks = [
        (int(hue), int(light))
        for _, hue, light in re.findall(r"\('([^']+)',\s*'(\d+)',\s*'(\d+)'\)", block)
    ]
    assert marks, "the mark table must parse"

    assert len(marks) == len(set(marks)), "two sources share a hue and a lightness"
    by_light: dict[int, list[int]] = {}
    for hue, light in marks:
        by_light.setdefault(light, []).append(hue)
    for light, hues in by_light.items():
        ordered = sorted(hues)
        gaps = [b - a for a, b in zip(ordered, ordered[1:])]
        assert len(hues) == len(set(hues)), f"two sources share hue at lightness {light}"
        if gaps:
            assert min(gaps) >= 20, f"two hues are {min(gaps)} degrees apart at lightness {light}"


def test_the_wheel_has_room_to_grow() -> None:
    """The point of the second dimension. Eighteen positions at two lightnesses
    is thirty-six sources, and a source keeps its colour for good rather than
    every mark shifting each time one is added."""
    import pathlib
    import re

    page = pathlib.Path("sf_housing/templates/alerts.html").read_text(encoding="utf-8")
    block = re.search(r"\{% set source_marks = \{(.*?)\} %\}", page, re.S).group(1)
    marks = [
        (int(hue), int(light))
        for _, hue, light in re.findall(r"\('([^']+)',\s*'(\d+)',\s*'(\d+)'\)", block)
    ]
    lightnesses = {light for _, light in marks}
    positions = {hue for hue, _ in marks}

    assert len(positions) <= 18, "the wheel is eighteen positions wide"
    assert all(hue % 20 == 10 for hue in positions), "every mark sits on a fixed position"
    assert len(marks) <= 18 * 2, "past thirty-six, lightness needs a third tier"
    assert lightnesses <= {54, 33}, f"unexpected lightness tier: {lightnesses}"


# --------------------------------------------------------------------------
# the two optional connectors
# --------------------------------------------------------------------------


def alerts_markup() -> str:
    import pathlib

    return pathlib.Path("sf_housing/templates/alerts.html").read_text(encoding="utf-8")


def test_facebook_comes_before_furnished_finder() -> None:
    """The one most people would want is the one they should meet first."""
    page = alerts_markup()

    assert page.index("Facebook Marketplace')") < page.index("Furnished Finder')")


def test_both_optional_connectors_say_what_they_cost() -> None:
    """"Needs a paid Apify account" was not true: the runs are bounded to stay
    inside the free monthly credit, and the numbers are in the code."""
    page = alerts_markup()

    # The real caps, which are what the code enforces: monthly_run_limit is 60
    # and monthly_post_limit is 400. The page said 300 while the code allowed
    # 400, so "the numbers are in the code" was only half true.
    assert "60 checks" in page and "400 group posts" in page, "state the real caps"
    assert "paid Apify account" not in page, "it was never true"
    assert "No card is asked for" in page


def test_both_optional_connectors_say_what_they_never_touch() -> None:
    page = alerts_markup()

    assert "Facebook login is never used or stored" in page
    assert "No Furnished Finder password is asked for or stored" in page


def test_the_furnished_finder_steps_match_what_the_extension_can_do() -> None:
    """The add-on only runs on Furnished Finder housing pages, so the steps have
    to send someone to one."""
    import json
    import pathlib

    manifest = json.loads(
        pathlib.Path("release_assets/furnished-finder-bridge/manifest.json").read_text(encoding="utf-8")
    )
    matches = manifest["content_scripts"][0]["matches"]
    assert any("/housing/" in pattern for pattern in matches), matches

    page = alerts_markup()
    assert "furnishedfinder.com/housing/" in page, "send them to a page the add-on runs on"
    assert "chrome://extensions" in page
    assert "Developer mode" in page
    assert "Load unpacked" in page
    assert "Save this search &amp; sync" in page, "the button the popup actually shows"


def test_the_page_says_what_connecting_an_inbox_would_add() -> None:
    """Somebody who has connected nothing needs to know what the other four
    are and what they cost before deciding. "Here is how to add more" in the
    heading and nothing under the chips left the offer unstated."""
    import pathlib

    page = pathlib.Path("sf_housing/templates/alerts.html").read_text(encoding="utf-8")
    tease = page[page.index('class="already-more"') : page.index("</p>", page.index('class="already-more"'))]

    assert "{% for name in email_providers %}" in tease, "the four are named from the real list"
    assert "two minutes" in tease, "say what it costs"
    assert "either way" in tease, "say it is optional"
    assert 'href="#gmail-title"' in tease, "and where to start"

    # Only offered to somebody who has not already done it.
    before = page[: page.index('class="already-more"')]
    assert "{% if not email_connected and not gmail_connected %}" in before[-260:]


def test_the_page_never_miscounts_the_sources_that_run_on_their_own() -> None:
    """Two sentences said "the 6 sources that run on their own" beside a
    heading saying seventeen do. Six was how many had delivered a home in the
    window, which is a different fact and a good one -- it just is not that
    one."""
    import pathlib
    import re

    page = pathlib.Path("sf_housing/templates/alerts.html").read_text(encoding="utf-8")

    for phrase in ("sources that run on their own", "sources that need no setup"):
        for match in re.finditer(re.escape(phrase), page):
            before = page[max(0, match.start() - 90) : match.start()]
            assert "no_setup_count_word" in before, (
                f'"{phrase}" is counted with something other than the no-setup list'
            )


def test_the_email_card_reports_a_check_the_way_the_others_do() -> None:
    """Check for alerts now starts a scan whose result lands on this card, so
    the card has to be able to say so. Without these hooks the badge sat on
    whatever was true when the page rendered until somebody reloaded, which is
    the fault the Facebook card was rebuilt to remove."""
    import pathlib

    page = pathlib.Path("sf_housing/templates/alerts.html").read_text(encoding="utf-8")

    assert 'data-connector-card="gmail"' in page, "the page watches this card"
    assert 'data-connector-badge="gmail"' in page, "and repaints its badge"
    assert 'data-connector-progress="gmail"' in page, "with real progress while it runs"
    assert 'data-connector-when="gmail"' in page, "and says when it last ran"
    # The provider tiles settle with it; leaving four of them stale until a
    # reload is the same fault in a smaller place.
    assert 'data-connector-badge="{{ provider.state.key }}"' in page


def test_the_email_check_stays_on_the_card_when_a_scan_is_running() -> None:
    import pathlib

    app = pathlib.Path("sf_housing/app.py").read_text(encoding="utf-8")
    gmail_test = app[app.index('@application.post("/alerts/gmail/test")'):]
    gmail_test = gmail_test[: gmail_test.index("@application.post", 10)]

    assert '"/?message=Scan+already+running"' not in gmail_test, "not the dashboard"
    assert "/alerts?message=A+check+is+already+running" in gmail_test
