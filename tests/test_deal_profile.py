from __future__ import annotations

from pathlib import Path

import json
import pytest
import yaml
from fastapi.testclient import TestClient

from sf_housing.app import create_app
from sf_housing.deal_profile import (
    DealProfileError,
    deal_profile_from_form,
)
from sf_housing.models import ListingCandidate, ScoreResult
from sf_housing.preferences import ensure_preferences, load_preferences, parse_preferences
from sf_housing.scoring import score_listing
from sf_housing.settings import Settings
from tests.conftest import TEST_PREFERENCES


def settings_for(tmp_path: Path) -> Settings:
    data = tmp_path / "data"
    return Settings(
        data_dir=data,
        preferences_path=data / "config" / "preferences.yaml",
        database_path=data / "housing.sqlite3",
        log_path=data / "housing.log",
    )


def valid_form() -> dict[str, object]:
    return {
        "housing_paths": ["private_room", "studio", "two_bedroom"],
        "private_room_maximum": "2400",
        "private_room_ideal": "2100",
        "studio_maximum": "2800",
        "studio_ideal": "2600",
        "studio_building_units": "50",
        "two_bedroom_maximum": "2500",
        "two_bedroom_occupants": "2",
        "two_bedroom_building_units": "50",
        "areas_dream": ["Noe Valley"],
        "areas_strong": ["Mission Dolores"],
        "areas_okay": ["Bernal Heights"],
        "move_in_flexible": "1",
        "lease_min_months": "6",
        "lease_max_months": "12",
        "household_maximum": "5",
        "preference_natural_light": "important",
        "preference_laundry": "nice",
        "preference_outdoor_space": "nice",
        "preference_furnished": "ignore",
        "preference_pets": "ignore",
        "preference_parking": "ignore",
        "preference_household_feel": "important",
    }


class MultiForm(dict):
    def getlist(self, name: str) -> list[object]:
        value = self.get(name, [])
        return list(value) if isinstance(value, list) else [value]


def test_missing_profile_becomes_neutral_draft_without_personal_answers(tmp_path: Path) -> None:
    path = tmp_path / "preferences.yaml"
    preferences = ensure_preferences(path)

    assert path.exists()
    assert preferences.profile_active is False
    assert preferences.deal_profile.enabled_paths == ()
    text = path.read_text(encoding="utf-8")
    assert "profile_version: 1" in text
    assert "Henry" not in text
    assert "henrymiller" not in text


def test_legacy_profile_migrates_in_memory_without_changing_the_file() -> None:
    preferences = parse_preferences(TEST_PREFERENCES)

    assert preferences.profile_active is True
    assert preferences.canonical is None
    assert preferences.deal_profile.budgets["private_room"].maximum_monthly == 2000
    assert preferences.deal_profile.areas["strong"] == ("NOPA", "Inner Richmond", "Mission")


def test_canonical_deal_prevents_cross_tier_area_duplicates() -> None:
    form = valid_form()
    form["areas_okay"] = ["Noe Valley"]

    try:
        deal_profile_from_form(MultiForm(form))
    except DealProfileError as exc:
        assert "appears in both" in str(exc)
    else:
        raise AssertionError("A duplicate area tier must not activate")


def test_split_summary_uses_one_authoritative_per_person_pair() -> None:
    profile = deal_profile_from_form(MultiForm(valid_form()))

    assert profile.budgets["two_bedroom"].total_maximum == 5000
    assert "$5,000 total ($2,500 each for 2)" in profile.summary()


def test_blank_app_routes_to_onboarding_and_does_not_record_manual_scan(tmp_path: Path) -> None:
    application = create_app(settings=settings_for(tmp_path), sources=[], enable_scheduler=False)

    with TestClient(application) as client:
        home = client.get("/", follow_redirects=False)
        scan = client.post("/scan", follow_redirects=False)
        onboarding = client.get("/preferences")

    assert home.status_code == 303
    assert home.headers["location"] == "/preferences?welcome=1"
    assert scan.status_code == 303
    assert "Finish+Your+deal" in scan.headers["location"]
    assert "Build the search once" in onboarding.text
    assert application.state.repository.recent_scans() == []


def test_blank_onboarding_uses_bounded_neighborhood_dropdowns(tmp_path: Path) -> None:
    application = create_app(settings=settings_for(tmp_path), sources=[], enable_scheduler=False)

    with TestClient(application) as client:
        onboarding = client.get("/preferences?welcome=1")

    assert onboarding.status_code == 200
    assert onboarding.text.count('data-area-limit="5"') == 4
    assert onboarding.text.count('data-area-add=') == 4
    assert "Add a neighborhood" in onboarding.text
    assert "Hold Command" not in onboarding.text
    assert "Outer Sunset" in onboarding.text
    assert "Visitacion Valley" in onboarding.text
    assert onboarding.text.count('class="path-row"') == 6, "five sizes plus the four-bedroom split"
    # The five home types share one set of column headers instead of repeating
    # a budget label inside every card.
    assert onboarding.text.count("Monthly maximum") == 1
    assert onboarding.text.count("Sharing with") == 1
    assert onboarding.text.count('data-path-card=') == 6
    assert "Tick a home type to use its budget" in onboarding.text
    script = (Path(__file__).resolve().parent.parent / "sf_housing" / "static" / "deal-form.js").read_text()
    assert "Saving deal and starting your first check" in script
    assert 'classList.toggle("is-selected", toggle.checked)' in script


def test_real_form_activation_persists_canonical_profile_and_starts_first_discovery(
    tmp_path: Path,
) -> None:
    settings = settings_for(tmp_path)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)

    with TestClient(application) as client:
        response = client.post("/preferences/deal", data=valid_form(), follow_redirects=False)

    saved = load_preferences(settings.preferences_path)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/?message=Your+deal+is+ready")
    assert "scan=starting" in response.headers["location"]
    assert saved.profile_active is True
    assert saved.canonical is not None
    assert saved.deal_profile.budgets["studio"].maximum_monthly == 2800
    assert saved.section("two_bedroom")["max_per_person"] == 2500


def test_scoring_separates_fit_confidence_and_hard_eligibility() -> None:
    preferences = parse_preferences(TEST_PREFERENCES)
    sparse = ListingCandidate(
        "Example",
        "sparse",
        "Private room in Mission",
        "https://example.test/sparse",
        price=1500,
        neighborhood="Mission",
        summary="Private room available.",
    )
    outside = ListingCandidate(
        "Example",
        "outside",
        "Private room in Oakland",
        "https://example.test/outside",
        price=1500,
        neighborhood="Oakland",
        summary="Private room available in Oakland.",
    )

    sparse_result = score_listing(sparse, preferences)
    outside_result = score_listing(outside, preferences)

    assert sparse_result.score > outside_result.score
    assert sparse_result.confidence < 100
    assert sparse_result.eligibility == "needs_verification"
    assert outside_result.eligibility == "ineligible"
    assert outside_result.eligibility_reasons


def test_partial_onboarding_draft_survives_a_restart(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    partial = {
        "housing_paths": "studio",
        "areas_dream": "Noe Valley",
        "move_in_flexible": "1",
    }

    with TestClient(application) as client:
        saved = client.post("/preferences/draft", data=partial)

    restarted = create_app(settings=settings, sources=[], enable_scheduler=False)
    with TestClient(restarted) as client:
        page = client.get("/preferences")

    draft = load_preferences(settings.preferences_path).deal_profile
    assert saved.status_code == 200
    assert draft.active is False
    assert draft.enabled_paths == ("studio",)
    assert draft.budgets == {}
    assert 'value="studio" data-path-toggle="studio" checked' in page.text


def test_anywhere_in_sf_is_a_real_area_rule_and_disabled_paths_fail() -> None:
    form = valid_form()
    form["housing_paths"] = ["studio"]
    form["anywhere_in_sf"] = "1"
    form["areas_dream"] = []
    form["areas_strong"] = []
    form["areas_okay"] = []
    profile = deal_profile_from_form(MultiForm(form))
    preferences = parse_preferences(
        yaml.safe_dump(
            {"profile_version": 1, "profile": profile.to_dict(), "technical": {}},
            sort_keys=False,
        )
    )
    studio = ListingCandidate(
        "Example", "studio", "Studio in the Castro", "https://example.test/studio",
        price=2500, neighborhood="Castro", summary="Sunny studio apartment in San Francisco.",
    )
    room = ListingCandidate(
        "Example", "room", "Private room in the Castro", "https://example.test/room",
        price=1500, neighborhood="Castro", summary="Private room available in San Francisco.",
    )

    studio_result = score_listing(studio, preferences)
    room_result = score_listing(room, preferences)

    assert studio_result.details["neighborhood"]["value"] == 1.0
    assert studio_result.eligibility != "ineligible"
    assert room_result.eligibility == "ineligible"
    assert "not enabled" in room_result.concern


# --------------------------------------------------------------------------
# a price floor nobody asked for
# --------------------------------------------------------------------------


def profile_with_room_budget(maximum: int, minimum: int | None = None) -> str:
    budget = {"maximum_monthly": maximum, "ideal_monthly": 1500}
    if minimum is not None:
        budget["minimum_monthly"] = minimum
    return yaml.safe_dump(
        {
            "profile_version": 1,
            "profile": {
                "state": "active",
                "enabled_paths": ["private_room"],
                "budgets": {"private_room": budget},
                "geography": {"anywhere_in_sf": True},
                "room_household": {"private_room_required": True},
            },
        }
    )


def test_a_budget_ceiling_does_not_invent_a_floor_underneath_it() -> None:
    """Raising the most you would pay must never hide cheaper homes.

    The floor was derived as 35% of the ceiling, so a $5,000 cap invented a
    $1,750 minimum. That went to Craigslist as min_price and cut its room search
    from 241 results to 49, and the deal form has no minimum field, so nobody
    could see the number or undo it.
    """
    preferences = parse_preferences(profile_with_room_budget(5000))
    budget = preferences.section("budget")

    # Absent, not None: every reader asks with .get("min_monthly", <default>),
    # and a present-but-None key defeats the default and crashes on int().
    assert "min_monthly" not in budget
    assert budget.get("min_monthly", 800) == 800
    assert budget["max_monthly"] == 5000


def test_every_page_renders_for_a_deal_with_no_stated_minimum(tmp_path: Path) -> None:
    """The regression this file's first version caused.

    Leaving the key present but None satisfied the profile tests and took the
    whole dashboard down with a TypeError on int(None), because the readers all
    supply a default that a None value quietly defeats.
    """
    settings = settings_for(tmp_path)
    settings.preferences_path.parent.mkdir(parents=True, exist_ok=True)
    settings.preferences_path.write_text(profile_with_room_budget(5000), encoding="utf-8")
    application = create_app(settings=settings, sources=[], enable_scheduler=False)

    with TestClient(application) as client:
        for path in ("/", "/?housing=room", "/?view=near_matches", "/preferences", "/alerts", "/support"):
            assert client.get(path).status_code == 200, path


def test_a_minimum_the_user_actually_stated_is_still_honoured() -> None:
    preferences = parse_preferences(profile_with_room_budget(5000, minimum=1200))

    assert preferences.section("budget")["min_monthly"] == 1200


def test_raising_the_ceiling_never_narrows_the_search() -> None:
    """The property the old derivation broke: a bigger budget searched less."""
    from sf_housing.sources import CraigslistSource

    source = CraigslistSource()
    urls = {
        cap: source._room_url(parse_preferences(profile_with_room_budget(cap)))
        for cap in (2000, 3000, 5000)
    }
    for cap, url in urls.items():
        assert "min_price" not in url, f"a ${cap} ceiling still imposed a floor: {url}"
        assert f"max_price={cap}" in url


def test_a_stated_minimum_reaches_the_search(tmp_path: Path) -> None:
    from sf_housing.sources import CraigslistSource

    url = CraigslistSource()._room_url(parse_preferences(profile_with_room_budget(5000, minimum=1200)))

    assert "min_price=1200" in url


def test_a_cheap_room_is_no_longer_scored_as_out_of_budget() -> None:
    """The floor also reached scoring, so an affordable room was marked as
    failing a limit the user never set."""
    preferences = parse_preferences(profile_with_room_budget(5000))
    listing = ListingCandidate(
        platform="Craigslist",
        source_id="cheap",
        title="Private room in a shared flat",
        original_url="https://sfbay.craigslist.org/roo/d/x/1.html",
        price=900,
        neighborhood="Potrero Hill",
        listing_type="Room/share",
        summary="A private room in a shared home, available now.",
    )

    result = score_listing(listing, preferences)
    price = result.details["price"]

    assert price["value"] > 0, "a $900 room is inside a $5,000 budget"
    assert "below" not in (price.get("mismatch") or "").lower()


def test_a_profile_with_no_technical_section_loads_instead_of_recursing() -> None:
    """parse_preferences converts a profile to a legacy view and re-parses it,
    and takes the profile branch whenever it sees "profile_version". The
    fallback that guessed which keys were technical carried that key through, so
    a document without a "technical" section recursed until the interpreter gave
    up. Anything this app writes has the section; a hand-edited file does not.
    """
    document = yaml.safe_dump(
        {
            "profile_version": 1,
            "profile": {
                "state": "active",
                "enabled_paths": ["private_room"],
                "budgets": {"private_room": {"maximum_monthly": 2500}},
                "geography": {"anywhere_in_sf": True},
                "room_household": {"private_room_required": True},
            },
        }
    )

    preferences = parse_preferences(document)

    assert preferences.profile_active
    assert preferences.section("budget")["max_monthly"] == 2500


def test_technical_settings_never_reports_the_profile_as_a_technical_setting() -> None:
    from sf_housing.deal_profile import technical_settings

    carried = technical_settings({"profile_version": 1, "profile": {"state": "active"}, "minimum_score": 70})

    assert "profile_version" not in carried
    assert "profile" not in carried
    assert carried["minimum_score"] == 70, "genuine technical settings still come through"


def wait_until_idle(application, timeout: float = 10.0) -> None:
    """Block until no source check is running, the way a person would wait.

    Saving the deal is refused while a check runs, so a test that saves twice
    has to respect the same rule a person does.
    """
    import time

    scanner = application.state.scanner
    deadline = time.monotonic() + timeout
    while scanner.is_running and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not scanner.is_running, "a scan never finished"


def test_the_deal_form_can_set_and_clear_a_price_floor(tmp_path: Path) -> None:
    """The other half of the range. The floor existed in the model and in the
    search, but no field ever set it, so the only floor anyone had was one the
    code invented."""
    settings = settings_for(tmp_path)
    settings.preferences_path.parent.mkdir(parents=True, exist_ok=True)
    # Already active, so saving is not a first activation and no discovery scan
    # starts underneath the test.
    settings.preferences_path.write_text(profile_with_room_budget(5000), encoding="utf-8")
    application = create_app(settings=settings, sources=[], enable_scheduler=False)

    form = valid_form()
    form["private_room_minimum"] = "1200"
    with TestClient(application) as client:
        wait_until_idle(application)
        assert client.post("/preferences/deal", data=form, follow_redirects=False).status_code == 303
        page = client.get("/preferences").text

        assert load_preferences(settings.preferences_path).section("budget")["min_monthly"] == 1200
        assert 'name="private_room_minimum"' in page, "the field has to be on the page"
        assert 'value="1200"' in page, "and has to show what was saved"

        # Clearing it removes the floor rather than leaving the old one behind.
        wait_until_idle(application)
        form["private_room_minimum"] = ""
        assert client.post("/preferences/deal", data=form, follow_redirects=False).status_code == 303

    assert "min_monthly" not in load_preferences(settings.preferences_path).section("budget")


def test_a_floor_above_the_ceiling_is_refused(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    settings.preferences_path.parent.mkdir(parents=True, exist_ok=True)
    settings.preferences_path.write_text(profile_with_room_budget(5000), encoding="utf-8")
    application = create_app(settings=settings, sources=[], enable_scheduler=False)

    form = valid_form()
    form["private_room_minimum"] = "99000"
    with TestClient(application) as client:
        response = client.post("/preferences/deal", data=form, follow_redirects=True)

    assert "must be below its maximum" in response.text


def test_a_stated_floor_reaches_the_craigslist_search(tmp_path: Path) -> None:
    """A floor is only useful if it actually narrows the search."""
    from sf_housing.sources import CraigslistSource

    settings = settings_for(tmp_path)
    settings.preferences_path.parent.mkdir(parents=True, exist_ok=True)
    settings.preferences_path.write_text(profile_with_room_budget(5000), encoding="utf-8")
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    form = valid_form()
    form["private_room_minimum"] = "1200"
    with TestClient(application) as client:
        client.post("/preferences/deal", data=form, follow_redirects=False)

    url = CraigslistSource()._room_url(load_preferences(settings.preferences_path))

    assert "min_price=1200" in url


# --------------------------------------------------------------------------
# how close a match has to be
# --------------------------------------------------------------------------


def test_the_shortlist_cut_off_can_be_moved_from_the_deal_page(tmp_path: Path) -> None:
    """It was fixed at 60 with no control anywhere, so someone happy to look at
    a 50 had no way to say so."""
    settings = settings_for(tmp_path)
    settings.preferences_path.parent.mkdir(parents=True, exist_ok=True)
    settings.preferences_path.write_text(profile_with_room_budget(5000), encoding="utf-8")
    application = create_app(settings=settings, sources=[], enable_scheduler=False)

    form = valid_form()
    form["minimum_score"] = "50"
    with TestClient(application) as client:
        wait_until_idle(application)
        assert client.post("/preferences/deal", data=form, follow_redirects=False).status_code == 303
        page = client.get("/preferences").text

    assert load_preferences(settings.preferences_path).minimum_score == 50
    assert 'name="minimum_score"' in page, "the control has to be on the page"
    assert 'type="range"' in page and 'value="50"' in page, "and has to show what was saved"
    # Dragging must not be the only way to read the number.
    assert '<output class="cutoff-value"' in page
    assert ">50</output>" in page


def test_lowering_the_cut_off_puts_more_homes_on_the_shortlist(tmp_path: Path) -> None:
    """The point of the control, counted on the page a person actually reads.

    The listings are seeded in a real area from the saved deal and scored by the
    scorer rather than by hand, because saving the deal rescores everything.
    """
    import re

    settings = settings_for(tmp_path)
    settings.preferences_path.parent.mkdir(parents=True, exist_ok=True)
    settings.preferences_path.write_text(profile_with_room_budget(5000), encoding="utf-8")
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    repository = application.state.repository
    for index, price in enumerate((2100, 2200, 2300, 1500, 900)):
        repository.upsert_listing(
            ListingCandidate(
                platform="Craigslist",
                source_id=f"r{index}",
                title=f"Private room {index} in Bernal Heights",
                original_url=f"https://sfbay.craigslist.org/roo/d/x/{index}.html",
                price=price,
                neighborhood="Bernal Heights",
                listing_type="Room/share",
                summary="A private room in a shared home, available now, laundry on site.",
            ),
            ScoreResult(70, ["fits"], "check", {}),
        )

    def rows_on_the_shortlist(client) -> int:
        page = client.get("/?housing=room").text
        return len(re.findall(r'<tr class="listing-row', page))

    form = valid_form()
    with TestClient(application) as client:
        wait_until_idle(application)
        form["minimum_score"] = "80"
        client.post("/preferences/deal", data=form, follow_redirects=False)
        strict = rows_on_the_shortlist(client)

        wait_until_idle(application)
        form["minimum_score"] = "40"
        client.post("/preferences/deal", data=form, follow_redirects=False)
        loose = rows_on_the_shortlist(client)

    assert loose > strict, f"a lower line has to show more homes (80 -> {strict}, 40 -> {loose})"


def test_the_slider_says_how_many_homes_each_stop_would_show(tmp_path: Path) -> None:
    """A number on its own is abstract. The counts ride with the control so the
    readout can answer "and how many is that?" while it is being dragged."""
    import json
    import re

    settings = settings_for(tmp_path)
    settings.preferences_path.parent.mkdir(parents=True, exist_ok=True)
    settings.preferences_path.write_text(profile_with_room_budget(5000), encoding="utf-8")
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    repository = application.state.repository
    for index, score in enumerate((92, 81, 74, 68, 55, 41, 33)):
        repository.upsert_listing(
            ListingCandidate(
                platform="Craigslist",
                source_id=f"s{index}",
                title=f"Room {index}",
                original_url=f"https://sfbay.craigslist.org/roo/d/x/{index}.html",
                price=1500,
                neighborhood="Bernal Heights",
                listing_type="Room/share",
                summary="A private room.",
            ),
            ScoreResult(score, ["fits"], "check", {}),
        )

    with TestClient(application) as client:
        page = client.get("/preferences").text

    raw = re.search(r"data-cutoff-counts='([^']*)'", page)
    assert raw, "the counts have to reach the page"
    counts = json.loads(raw.group(1))

    assert counts, "and cannot be empty"
    # Monotonic by construction: a higher bar can never show more homes.
    stops = sorted(int(key) for key in counts)
    values = [counts[str(stop)] for stop in stops]
    assert values == sorted(values, reverse=True)
    assert counts[str(stops[0])] >= counts[str(stops[-1])]


def test_the_cut_off_still_saves_without_javascript(tmp_path: Path) -> None:
    """The readout needs a script; choosing a value must not."""
    settings = settings_for(tmp_path)
    settings.preferences_path.parent.mkdir(parents=True, exist_ok=True)
    settings.preferences_path.write_text(profile_with_room_budget(5000), encoding="utf-8")
    application = create_app(settings=settings, sources=[], enable_scheduler=False)

    form = valid_form()
    form["minimum_score"] = "70"
    with TestClient(application) as client:
        wait_until_idle(application)
        assert client.post("/preferences/deal", data=form, follow_redirects=False).status_code == 303

    assert load_preferences(settings.preferences_path).minimum_score == 70


@pytest.mark.parametrize("requested,expected", [("0", 30), ("999", 95), ("", 60), ("abc", 60), ("55", 55)])
def test_an_impossible_cut_off_is_brought_back_into_range(
    tmp_path: Path, requested: str, expected: int
) -> None:
    """A hand-edited form must not be able to empty the shortlist permanently or
    crash the page."""
    settings = settings_for(tmp_path)
    settings.preferences_path.parent.mkdir(parents=True, exist_ok=True)
    settings.preferences_path.write_text(profile_with_room_budget(5000), encoding="utf-8")
    application = create_app(settings=settings, sources=[], enable_scheduler=False)

    form = valid_form()
    form["minimum_score"] = requested
    with TestClient(application) as client:
        wait_until_idle(application)
        assert client.post("/preferences/deal", data=form, follow_redirects=False).status_code == 303
        assert client.get("/").status_code == 200

    assert load_preferences(settings.preferences_path).minimum_score == expected


def test_saving_the_deal_does_not_disturb_other_technical_settings(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    settings.preferences_path.parent.mkdir(parents=True, exist_ok=True)
    document = yaml.safe_load(profile_with_room_budget(5000))
    document["technical"] = {"minimum_score": 60, "sources": {"craigslist_pages": 3}}
    settings.preferences_path.write_text(yaml.safe_dump(document), encoding="utf-8")
    application = create_app(settings=settings, sources=[], enable_scheduler=False)

    form = valid_form()
    form["minimum_score"] = "70"
    with TestClient(application) as client:
        wait_until_idle(application)
        client.post("/preferences/deal", data=form, follow_redirects=False)

    saved = yaml.safe_load(settings.preferences_path.read_text(encoding="utf-8"))
    assert saved["technical"]["minimum_score"] == 70
    assert saved["technical"]["sources"] == {"craigslist_pages": 3}, "unrelated settings must survive"


# --------------------------------------------------------------------------
# the form asks for what it needs, and no more
# --------------------------------------------------------------------------


def visible_control_count(markup: str) -> tuple[int, int]:
    """Controls in the form, and the ones a reader actually meets first."""
    import re

    total = len(re.findall(r"<(input|select|textarea)\b", markup))
    folded = sum(
        len(re.findall(r"<(input|select|textarea)\b", match.group(0)))
        for match in re.finditer(
            r'<details class="deal-fold"(?![^>]*\bopen\b).*?</details>', markup, re.S
        )
    )
    optional = (
        0
        if "show-ranges" in markup
        else len(re.findall(r'name="\w+_(?:minimum|ideal)"', markup))
    )
    return total, total - folded - optional


def test_a_new_deal_asks_for_far_less_than_it_can_hold(tmp_path: Path) -> None:
    """Everything used to be asked at once and at equal weight, so the three
    answers that decide what you see sat among two dozen refinements."""
    settings = settings_for(tmp_path)
    settings.preferences_path.parent.mkdir(parents=True, exist_ok=True)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)

    with TestClient(application) as client:
        page = client.get("/preferences?welcome=1").text

    total, visible = visible_control_count(page)
    # A floor, not a count: it catches a section going missing. It came down by
    # one when five hidden building-size fields became four visible choices,
    # which is a question being asked rather than a question being dropped.
    assert total >= 55, "a section has gone missing from the form"
    assert visible <= total * 0.55, f"{visible} of {total} controls still meet the reader at once"


def test_a_refinement_you_already_set_is_never_hidden_from_you(tmp_path: Path) -> None:
    """A value you cannot see is a value you cannot correct."""
    settings = settings_for(tmp_path)
    settings.preferences_path.parent.mkdir(parents=True, exist_ok=True)
    settings.preferences_path.write_text(profile_with_room_budget(5000), encoding="utf-8")
    application = create_app(settings=settings, sources=[], enable_scheduler=False)

    form = valid_form()
    form["private_room_minimum"] = "1200"
    form["lease_min_months"] = "6"
    form["preference_laundry"] = "important"
    with TestClient(application) as client:
        wait_until_idle(application)
        client.post("/preferences/deal", data=form, follow_redirects=False)
        page = client.get("/preferences").text

    assert "show-ranges" in page, "a price range you set comes back on show"
    assert page.count('<details class="deal-fold" open') == 2, "and both folds open themselves"
    assert 'value="1200"' in page


def test_a_deal_saves_with_every_optional_answer_left_blank(tmp_path: Path) -> None:
    """The fields inside a closed fold still submit; blank has to mean blank
    rather than an error or a silently invented value."""
    settings = settings_for(tmp_path)
    settings.preferences_path.parent.mkdir(parents=True, exist_ok=True)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)

    form = {
        "housing_paths": ["private_room"],
        "private_room_maximum": "2200",
        "private_room_minimum": "",
        "private_room_ideal": "",
        "areas_dream": ["Mission District"],
        "move_in_flexible": "1",
        "minimum_score": "60",
        "lease_min_months": "",
        "lease_max_months": "",
        "household_maximum": "",
    }
    with TestClient(application) as client:
        assert client.post("/preferences/deal", data=form, follow_redirects=False).status_code == 303
        page = client.get("/preferences").text

    preferences = load_preferences(settings.preferences_path)
    assert preferences.profile_active
    assert preferences.section("budget")["max_monthly"] == 2200
    assert "min_monthly" not in preferences.section("budget")
    assert preferences.deal_profile.lease_min_months is None
    assert preferences.deal_profile.household_maximum is None
    assert "show-ranges" not in page, "and the form stays as simple as it was"


def test_nothing_the_form_can_set_was_dropped(tmp_path: Path) -> None:
    """Simplifying must mean tucking away, never removing."""
    import re

    settings = settings_for(tmp_path)
    settings.preferences_path.parent.mkdir(parents=True, exist_ok=True)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    with TestClient(application) as client:
        page = client.get("/preferences").text

    for field in (
        "housing_paths", "private_room_maximum", "private_room_minimum", "private_room_ideal",
        "two_bedroom_occupants", "three_bedroom_occupants", "four_bedroom_occupants",
        "anywhere_in_sf", "move_in_flexible", "earliest_move_in", "preferred_by",
        "lease_min_months", "lease_max_months", "household_maximum",
        "preference_laundry", "preference_furnished", "preference_pets",
        "preference_parking", "minimum_score",
    ):
        assert f'name="{field}"' in page, f"{field} disappeared from the form"
    # Area chips only exist once an area is chosen, so the picker is the control
    # to look for on a blank deal.
    for tier in ("dream", "strong", "okay", "avoid"):
        assert f'data-area-add="{tier}"' in page, f"the {tier} area picker disappeared"


def test_the_slider_count_matches_the_rows_the_tabs_will_show(tmp_path: Path) -> None:
    """The number under the slider is a promise about the next page you will
    see. It counted every home shape in the database, so a deal for rooms alone
    counted whole units the tabs would never display.

    Every home the deal lets through is on exactly one tab now -- the Other
    homes tab holds whatever no size tab does -- so the promise is the sum of
    the tabs. Scored for real: a rooms-only deal rules the studios out itself
    ("Studio is not enabled in your deal"), so they are on no tab and in no
    count."""
    import re

    from sf_housing.models import ListingCandidate
    from sf_housing.scoring import score_listing

    settings = settings_for(tmp_path)
    settings.preferences_path.parent.mkdir(parents=True, exist_ok=True)
    settings.preferences_path.write_text(profile_with_room_budget(5000), encoding="utf-8")
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    repository = application.state.repository
    deal = load_preferences(settings.preferences_path)

    def store(source_id: str, kind: str, listing_type: str) -> None:
        listing = ListingCandidate(
            platform="Craigslist",
            source_id=source_id,
            title=f"A {listing_type} in NOPA",
            original_url=f"https://sfbay.craigslist.org/x/{source_id}.html",
            price=1500,
            neighborhood="NOPA",
            listing_type=listing_type,
            summary="Available now.",
            housing_kind=kind,
        )
        repository.upsert_listing(listing, score_listing(listing, deal))

    for index in range(4):
        store(f"room{index}", "room", "Room/share")
    for index in range(3):
        store(f"unit{index}", "whole_unit", "Studio")

    with TestClient(application) as client:
        wait_until_idle(application)
        page = client.get("/preferences").text
        rows = sum(
            len(re.findall(r'<tr class="listing-row', client.get(f"/?housing={tab}").text))
            for tab in ("room", "other")
        )

    counts = json.loads(re.search(r"data-cutoff-counts='([^']+)'", page).group(1))
    cutoff = deal.minimum_score
    assert counts[str(cutoff)] == rows, (
        f"slider says {counts[str(cutoff)]}, the tabs show {rows}"
    )
    assert counts[str(cutoff)] == 4, "the three studios are not on this deal's tabs"


# --------------------------------------------------------------------------
# the ideal target, and what it is worth
# --------------------------------------------------------------------------


def test_the_ideal_target_is_on_the_form_without_being_asked_for(tmp_path: Path) -> None:
    """It changes the order of results, so it is a column rather than something
    folded away with the refinements."""
    settings = settings_for(tmp_path)
    settings.preferences_path.parent.mkdir(parents=True, exist_ok=True)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    with TestClient(application) as client:
        page = client.get("/preferences?welcome=1").text

    assert 'name="private_room_ideal"' in page
    ideal = page[page.index('name="private_room_ideal"') - 400 : page.index('name="private_room_ideal"')]
    assert "is-optional" not in ideal, "the ideal is not hidden behind the price-range toggle"
    assert "Ideal target" in page


def test_a_home_at_the_ideal_outranks_one_that_merely_fits(tmp_path: Path) -> None:
    """The whole reason the field exists."""
    import yaml

    from sf_housing.models import ListingCandidate
    from sf_housing.preferences import parse_preferences
    from sf_housing.scoring import score_listing

    document = {
        "profile_version": 1,
        "profile": {
            "state": "active",
            "enabled_paths": ["private_room"],
            "budgets": {"private_room": {"maximum_monthly": 3500, "ideal_monthly": 2500}},
            "geography": {"anywhere_in_sf": True},
            "room_household": {"private_room_required": True},
        },
    }
    preferences = parse_preferences(yaml.safe_dump(document))

    def score(price: int) -> int:
        return score_listing(
            ListingCandidate(
                platform="Craigslist",
                source_id=str(price),
                title="Private room",
                original_url=f"https://sfbay.craigslist.org/x/{price}.html",
                price=price,
                neighborhood="NOPA",
                listing_type="Room/share",
                summary="A private room in a shared home.",
            ),
            preferences,
        ).score

    assert score(2400) > score(3400), "at or under the ideal has to rank higher"
    assert score(2500) > score(2600)


def test_the_sweet_spot_is_a_band_not_a_single_price(tmp_path: Path) -> None:
    """Both ends used to be the ideal itself, so only a home priced at exactly
    $2,500 counted, and $2,499 scored the same as one at the top of the
    budget."""
    from sf_housing.deal_profile import DealProfile, legacy_view

    profile = DealProfile.from_dict(
        {
            "state": "active",
            "enabled_paths": ["private_room"],
            "budgets": {"private_room": {"maximum_monthly": 3500, "ideal_monthly": 2500}},
            "geography": {"anywhere_in_sf": True},
        }
    )
    budget = legacy_view(profile, {})["budget"]

    assert budget["sweet_spot_max"] == 2500
    assert budget["sweet_spot_min"] < 2500, "the band has to have room in it"


def test_a_stated_floor_becomes_the_bottom_of_the_band() -> None:
    """Nothing below a floor is silently promoted into the sweet spot."""
    from sf_housing.deal_profile import DealProfile, legacy_view

    profile = DealProfile.from_dict(
        {
            "state": "active",
            "enabled_paths": ["private_room"],
            "budgets": {
                "private_room": {
                    "maximum_monthly": 3500,
                    "ideal_monthly": 2500,
                    "minimum_monthly": 1500,
                }
            },
            "geography": {"anywhere_in_sf": True},
        }
    )
    budget = legacy_view(profile, {})["budget"]

    assert budget["sweet_spot_min"] == 1500
    assert budget["sweet_spot_max"] == 2500


def test_no_ideal_is_never_invented_from_the_maximum() -> None:
    """The page told people $3,500 was "exactly your ideal price" when $3,500
    was the most they were willing to pay."""
    from sf_housing.deal_profile import DealProfile, legacy_view

    profile = DealProfile.from_dict(
        {
            "state": "active",
            "enabled_paths": ["private_room"],
            "budgets": {"private_room": {"maximum_monthly": 3500}},
            "geography": {"anywhere_in_sf": True},
        }
    )
    budget = legacy_view(profile, {})["budget"]

    assert "ideal_monthly" not in budget


def test_an_ideal_above_the_maximum_is_refused(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    settings.preferences_path.parent.mkdir(parents=True, exist_ok=True)
    settings.preferences_path.write_text(profile_with_room_budget(5000), encoding="utf-8")
    application = create_app(settings=settings, sources=[], enable_scheduler=False)

    form = valid_form()
    form["private_room_ideal"] = "99000"
    with TestClient(application) as client:
        wait_until_idle(application)
        response = client.post("/preferences/deal", data=form, follow_redirects=True)

    assert "cannot exceed its maximum" in response.text


# --------------------------------------------------------------------------
# something to read while the deal saves
# --------------------------------------------------------------------------


def deal_form_script() -> str:
    return (Path(__file__).resolve().parents[1] / "sf_housing/static/deal-form.js").read_text(
        encoding="utf-8"
    )


def saving_notes() -> list[str]:
    """The message table as written, one entry per element.

    An entry sits at the array's own indent; anything deeper is a
    continuation of it, because the first sentence is a two-branch ternary
    spread over three lines.
    """
    script = deal_form_script()
    start = script.index("const savingNotes = (homes) => [")
    block = script[start : script.index("];", start)]
    indent = " " * 4
    entries: list[str] = []
    for line in block.splitlines()[1:]:
        if not line.strip() or line.strip().startswith("//"):
            continue
        if line.startswith(indent) and not line.startswith(indent + " "):
            entries.append(line.strip())
        elif entries:
            entries[-1] += " " + line.strip()
    return entries


def stylesheet() -> str:
    return (Path(__file__).resolve().parents[1] / "sf_housing/static/style.css").read_text(
        encoding="utf-8"
    )


def block_body(style: str, opener: str) -> str:
    """The text inside the braces opened by `opener`, brace-matched.

    Slicing between two landmarks was how the reduced-motion test passed while
    the animation had been moved out of the media query it was meant to sit in.
    """
    start = style.index(opener) + len(opener)
    depth = 1
    for offset in range(start, len(style)):
        if style[offset] == "{":
            depth += 1
        elif style[offset] == "}":
            depth -= 1
            if depth == 0:
                return style[start:offset]
    raise AssertionError(f"{opener!r} is never closed")


MOTION_QUERY = "@media (prefers-reduced-motion: no-preference) {"


def motion_gated(style: str) -> list[str]:
    """Every no-preference block in the sheet, not just the first one.

    The stylesheet gates several unrelated animations this way, so a test that
    reads only the first block is reading the donate panel's.
    """
    bodies: list[str] = []
    at = 0
    while (found := style.find(MOTION_QUERY, at)) != -1:
        body = block_body(style[found:], MOTION_QUERY)
        bodies.append(body)
        at = found + len(MOTION_QUERY) + len(body)
    return bodies


def deal_page(tmp_path: Path, prices: tuple[int, ...] = ()) -> str:
    settings = settings_for(tmp_path)
    settings.preferences_path.parent.mkdir(parents=True, exist_ok=True)
    settings.preferences_path.write_text(profile_with_room_budget(5000), encoding="utf-8")
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    repository = application.state.repository
    for index, price in enumerate(prices):
        repository.upsert_listing(
            ListingCandidate(
                platform="Craigslist",
                source_id=f"saving{index}",
                title=f"Private room {index} in Bernal Heights",
                original_url=f"https://sfbay.craigslist.org/roo/d/saving/{index}.html",
                price=price,
                neighborhood="Bernal Heights",
                listing_type="Room/share",
            ),
            ScoreResult(70, ["fits"], "check", {}),
        )
    with TestClient(application) as client:
        wait_until_idle(application)
        return client.get("/preferences").text


def test_the_deal_page_carries_a_panel_for_the_wait_that_follows_saving(tmp_path: Path) -> None:
    """Saving reranks every stored home before the page can answer. Without
    this the only sign anything is happening is a spinning browser tab."""
    assert "data-deal-saving" in deal_page(tmp_path)


def test_the_panel_stays_out_of_the_way_until_the_deal_is_submitted(tmp_path: Path) -> None:
    """It explains a wait. Before there is a wait it is one more thing to
    read on a page that already asks a lot of questions."""
    import re

    page = deal_page(tmp_path)
    tag = re.search(r"<p[^>]*\bdata-deal-saving-note\b[^>]*>", page)

    assert tag, "the line is not on the page at all"
    assert re.search(r"(?<!-)\bhidden\b", tag.group(0)), tag.group(0)


def test_the_wait_is_measured_in_the_homes_actually_stored(tmp_path: Path) -> None:
    """"Reranking 5,235 homes" is a wait somebody can sit through. A number
    baked in when the page was written is a number that goes stale, so the
    count has to come from the database on every render."""
    page = deal_page(tmp_path, prices=(2100, 2200, 2300))

    assert 'data-listing-count="3"' in page


def test_the_changing_line_is_hidden_from_screen_readers(tmp_path: Path) -> None:
    """It sits inside an aria-live region. Announced, it would interrupt with
    a new sentence every eight seconds, over the heading that says what is
    actually happening."""
    page = deal_page(tmp_path)
    note = page[page.index("data-deal-saving-note") - 60 : page.index("data-deal-saving-note") + 90]

    assert 'aria-hidden="true"' in note, note


def test_there_are_enough_messages_to_cover_the_rerank() -> None:
    """At eight seconds each, five sentences cover forty seconds before the
    reader starts seeing repeats."""
    assert len(saving_notes()) >= 5


def test_the_first_message_counts_the_homes_the_server_reported() -> None:
    """The rest of the sentences are true whatever the pool holds. The first
    one carries the number, and it has to be the live one."""
    notes = saving_notes()

    assert "homes.toLocaleString()" in notes[0]


def test_an_empty_pool_is_never_described_as_zero_homes() -> None:
    """A first-run deal has nothing stored yet. "Rescoring 0 homes" reads as
    a bug in the sentence that is supposed to be the reassurance."""
    first = saving_notes()[0]

    assert "Rescoring the homes already collected." in first
    assert first.startswith("homes ?"), "an empty pool has to take the other branch"


def test_the_line_changes_on_a_timer_the_reader_can_follow() -> None:
    """There is no progress to poll -- the save is one ordinary form post --
    so the sentences advance on their own eight-second beat."""
    script = deal_form_script()
    panel = script[script.index("const startSavingPanel") :]

    assert "window.setInterval(rotate, 8000)" in panel


def test_the_panel_is_only_revealed_once_the_deal_is_going_to_be_saved() -> None:
    """A form that fails validation never posts. Showing "saving your deal"
    over a form the browser just refused would be a lie about what happened."""
    script = deal_form_script()
    handler = script[script.index('form.addEventListener("submit"') :]

    assert handler.count("startSavingPanel(") == 1, "only one place may reveal the panel"
    assert handler.index("startSavingPanel(") > handler.rindex("event.preventDefault()")


def test_the_button_says_which_of_the_two_waits_this_is() -> None:
    """A first deal starts a check of every source afterwards; a later one
    only reranks. They take different amounts of time, so they say so on the
    button -- the one thing here that holds a spinner."""
    handler = deal_form_script()
    handler = handler[handler.index('form.addEventListener("submit"') :]

    assert "firstActivation" in handler
    assert "starting your first check" in handler
    assert "Saving and reranking homes" in handler


def test_the_line_holds_its_row_so_the_panel_does_not_jump() -> None:
    """The sentences are different lengths. Without a reserved row a shorter
    one shortens the panel and the submit button moves under the cursor."""
    import re

    rule = block_body(stylesheet(), ".deal-saving-note {")
    reserved = re.search(r"min-height:\s*([\d.]+)(\w+)", rule)

    assert reserved, rule
    assert float(reserved.group(1)) > 0, "a row of zero height reserves nothing"


def test_the_sentence_swaps_without_a_fade_for_a_reader_who_asked_for_no_motion() -> None:
    """The sentence changing is the whole signal. Someone who asked for
    stillness gets the new sentence, not a cross-fade to it."""
    style = stylesheet()
    gated = motion_gated(style)
    ungated = style
    for body in gated:
        ungated = ungated.replace(body, "")

    assert any("transition: opacity" in body for body in gated), "the fade is not gated at all"
    assert "transition" not in block_body(
        ungated, ".deal-saving-note {"
    ), "the sentence fades for a reader who asked it not to"


def test_one_wait_gets_one_loading_indicator(tmp_path: Path) -> None:
    """The button already holds a spinner and already says what is happening.
    A bordered panel under it, with its own indeterminate bar and its own copy
    of that sentence, was a second loading indicator for a single wait."""
    page = deal_page(tmp_path)

    assert "deal-saving-track" not in page, "a second bar is back"
    assert "deal-saving-heading" not in page, "a second heading is back"
    assert 'role="progressbar"' not in page, "nothing here knows how far along it is"
    assert "data-deal-saving-note" in page, "the rotating line still has to be there"


def test_the_clear_button_posts_the_very_form_the_panel_lives_in(tmp_path: Path) -> None:
    """The reason the guard below has to exist. If Start over ever moves out
    of this form, the guard is dead code and should go with it."""
    page = deal_page(tmp_path)

    assert 'formaction="/preferences/deal/reset"' in page
    assert "data-deal-reset" in page


def test_clearing_the_deal_is_never_dressed_up_as_saving_it() -> None:
    """Start over shares the deal form, so without a guard it fell straight
    through the save branch: wiping a deal announced that it was being saved
    and reranked, and a deal missing a home type could not be cleared at all
    because the save gate refused the submit."""
    script = deal_form_script()
    handler = script[script.index('form.addEventListener("submit"') :]
    guard = handler.index("data-deal-reset")

    assert guard < handler.index("event.preventDefault()"), "the guard runs before the gate"
    assert guard < handler.index("startSavingPanel("), "the guard runs before the panel"


def test_the_number_the_panel_quotes_is_the_number_that_gets_rescored(
    tmp_path: Path,
) -> None:
    """The sentence promises a rescore of exactly this many homes. The count
    and the rescore read the table through different methods, so nothing but
    this stops one of them from quietly starting to mean something else."""
    from sf_housing.scanner import Scanner

    settings = settings_for(tmp_path)
    settings.preferences_path.parent.mkdir(parents=True, exist_ok=True)
    settings.preferences_path.write_text(profile_with_room_budget(5000), encoding="utf-8")
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    repository = application.state.repository
    for index in range(4):
        repository.upsert_listing(
            ListingCandidate(
                platform="Craigslist",
                source_id=f"tie{index}",
                title=f"Private room {index} in Bernal Heights",
                original_url=f"https://sfbay.craigslist.org/roo/d/tie/{index}.html",
                price=2000 + index,
                neighborhood="Bernal Heights",
                listing_type="Room/share",
            ),
            ScoreResult(70, ["fits"], "check", {}),
        )

    preferences = load_preferences(settings.preferences_path)
    scanner = Scanner(repository, lambda: preferences, [])

    assert repository.count_listings() == 4
    assert scanner.rescore_all(preferences) == repository.count_listings()


def test_a_second_submit_never_starts_a_second_clock() -> None:
    """Pressing Return while the first save is in flight ran rotate twice on
    one sentence, which reads as a stutter rather than a rotation."""
    script = deal_form_script()
    panel = script[script.index("const startSavingPanel") :]

    assert "savingTimer = window.setInterval" in panel, "the clock has to be held to be stopped"
    assert "savingTimer) return" in panel[: panel.index("savingNote.hidden = false")]


def test_coming_back_with_the_back_button_leaves_a_form_you_can_use() -> None:
    """The browser restores this page exactly as it was abandoned: panel
    sweeping, submit button dead. Without this the only way out is a reload."""
    script = deal_form_script()

    assert 'window.addEventListener("pageshow"' in script
    restore = script[script.index('window.addEventListener("pageshow"') :]
    assert "event.persisted" in restore, "only a cached restore needs undoing"
    assert "stopSavingPanel()" in restore[: restore.index("});")]

    stop = script[script.index("const stopSavingPanel") : script.index('window.addEventListener("pageshow"')]
    assert "clearInterval" in stop
    assert "submitButton.disabled = false" in stop


def test_the_sentences_name_the_words_the_screen_actually_uses() -> None:
    """The tiers are labelled Dream, Strong and Secondary on this very page.
    "okay" is the key underneath them, and naming it told the reader about a
    word they have never been shown."""
    notes = " ".join(saving_notes())

    assert "Secondary" in notes and "Dream" in notes
    assert "marked okay" not in notes
    assert "Near matches" in notes, "the shortlist calls that view Near matches"


def test_the_sentence_fades_out_before_the_next_one_fades_in() -> None:
    """Swapping the text outright is a pop. The fade is the whole reason this
    reads as one calm wait rather than a line stuttering under the button."""
    script = deal_form_script()
    panel = script[script.index("const startSavingPanel") :]

    assert 'opacity = "0"' in panel, "nothing ever fades out"
    assert "setTimeout(show" in panel, "the swap does not wait for the fade"


# --------------------------------------------------------------------------
# the budget table on a narrow window
# --------------------------------------------------------------------------


def css_rules(style: str):
    """Every (selector, declarations, enclosing at-rule) in the stylesheet.

    Enough of a parser for the questions below: one level of at-rule nesting,
    which is all this file uses.
    """
    import re

    style = re.sub(r"/\*.*?\*/", "", style, flags=re.S)

    def walk(text: str, condition: str):
        at = 0
        while True:
            opened = text.find("{", at)
            if opened == -1:
                return
            prelude = text[at:opened].split("}")[-1].strip()
            depth, close = 1, opened + 1
            while depth:
                if text[close] == "{":
                    depth += 1
                elif text[close] == "}":
                    depth -= 1
                close += 1
            body = text[opened + 1 : close - 1]
            if prelude.startswith("@"):
                yield from walk(body, prelude)
            else:
                yield prelude, body, condition
            at = close

    return list(walk(style, ""))


def specificity(selector: str) -> tuple[int, int, int]:
    """(ids, classes, elements) for one selector.

    Counting, not a parser -- the selectors it is asked about are plain
    class and element chains, and the test below refuses anything else.
    """
    import re

    assert ":" not in selector and "[" not in selector, f"too clever for this: {selector}"
    return (
        selector.count("#"),
        selector.count("."),
        len(re.findall(r"(?:^|[\s>+~])([a-z][a-z0-9-]*)", selector)),
    )


def test_the_column_track_is_chosen_in_exactly_one_place() -> None:
    """Every rule that gives a budget row its columns has to read the variable,
    because that is what leaves the narrow layout one thing to override instead
    of a list that can grow behind its back. The list had grown: the desktop
    track was written plainly and again under .show-ranges, and the reflow
    answered only the plain one."""
    for selector, body, _ in css_rules(stylesheet()):
        if "path-row-fields" in selector:
            continue  # the fields have their own two-up track on a narrow window
        if "path-row" not in selector and "path-ledger-head" not in selector:
            continue
        for declaration in body.split(";"):
            name, _, value = declaration.partition(":")
            if name.strip() == "grid-template-columns":
                assert value.strip() == "var(--ledger-columns)", f"{selector} {{{declaration} }}"


def test_the_narrow_layout_can_reach_that_place() -> None:
    """A media query carries no specificity of its own, so the reflow only
    happens if it is at least as strong as what it is undoing. It was not.
    Anybody who had asked for a price range kept the five-column desktop grid
    on a phone: the table measured 591px inside a 430px window, the form clips
    to its own rounded corners, and nine fields -- every required maximum among
    them -- sat past the edge of the screen with nothing able to scroll to
    them."""
    wide, narrow = [], []
    for selector, body, condition in css_rules(stylesheet()):
        if "--ledger-columns:" not in body:
            continue
        for one in (part.strip() for part in selector.split(",")):
            (narrow if "max-width" in condition else wide).append((one, condition))

    assert wide, "nothing sets the column track any more"
    assert narrow, "the narrow window no longer changes it"
    strongest_narrow = max(specificity(one) for one, _ in narrow)
    strongest_wide = max(specificity(one) for one, _ in wide)

    # A tie is enough: the narrow block is further down the file.
    assert strongest_narrow >= strongest_wide, (
        f"the strongest narrow rule {strongest_narrow} cannot beat the strongest "
        f"wide rule {strongest_wide}; wide={wide} narrow={narrow}"
    )


def narrow_rules():
    """The rules that only apply once the window is too narrow for the table."""
    return [
        (selector, body)
        for selector, body, condition in css_rules(stylesheet())
        if "max-width: 760px" in condition
    ]


def declares(body: str, name: str) -> str:
    for declaration in body.split(";"):
        key, _, value = declaration.partition(":")
        if key.strip() == name:
            return value.strip()
    return ""


def test_the_stacked_row_reads_as_a_labelled_form_not_a_headless_table() -> None:
    """With the column headings gone, every field has to say what it is, the
    fields have to fit two to a line, and the placeholder that fills the
    Sharing with column on a wide screen has no column left to fill."""
    rules = narrow_rules()
    assert rules, "the narrow layout is gone"

    def display_of(selector: str) -> str:
        return next(
            (declares(body, "display") for sel, body in rules if sel.strip() == selector), ""
        )

    assert display_of(".path-ledger-head") == "none", "the headings stay and the labels do not"
    assert display_of(".cell-label") == "block", "then nothing says which number is which"
    assert display_of(".cell-empty") == "none", '"Just you" has no column to fill here'

    # Named against the fields themselves: three other things in this block lay
    # out two-up as well, so looking for the bare track proved nothing.
    fields = [body for selector, body in rules if "path-row-fields" in selector]
    assert fields, "the fields keep the desktop track"
    for body in fields:
        assert declares(body, "grid-template-columns") == "repeat(2, minmax(0, 1fr))", body


def test_revealing_the_optional_column_keeps_the_cell_a_grid() -> None:
    """The reveal sets display on a class the cell shares with a heading, and
    block there costs the 4px gap between a label and its field. Nothing shows
    it on a wide screen, where the labels are hidden -- it appeared the moment
    the narrow layout displayed them, four pixels out from the field beside
    it."""
    style = stylesheet()

    assert ".money-cell { min-width: 0; display: grid;" in style
    assert ".path-ledger.show-ranges .money-cell.is-optional { display: grid; }" in style
