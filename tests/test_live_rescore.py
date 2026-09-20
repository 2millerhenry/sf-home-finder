"""WO-5: scoring the board again scores the homes still in play, not a year of the archive.

On a year-scale board -- 213,408 rows -- the first start after any app update
(installers clear the scoring mark) and every saved deal re-scored every row:
666 seconds measured, the dashboard unreachable throughout, longer than the
installer's 150-second wait, and clicking Open during it restarted the app
mid-scoring. Now a pass scores every copy of every home still in play --
on the shortlist, starred, passed or restored -- and a home every copy of
which the archive aged out is left as it is when the deal changes, until the
user restores, stars, passes or notes it, which scores it first.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sf_housing.app import create_app
from sf_housing.classification import classify_listing
from sf_housing.database import Repository
from sf_housing.models import ListingCandidate, ScoreResult
from sf_housing.preferences import Preferences, parse_preferences
from sf_housing.rescore_marker import current_fingerprint
from sf_housing.scanner import Scanner
from sf_housing.scoring import score_listing
from sf_housing.settings import Settings
from tests.conftest import TEST_PREFERENCES

# A verdict the test deal gives none of these homes, standing in for one an
# older deal gave: a pass that scores a row replaces it, and one that skips the
# row leaves it.
OLD_VERDICT = (11, "ineligible")


def room(slug: str) -> ListingCandidate:
    """A room the test deal shortlists."""
    return ListingCandidate(
        platform="Craigslist", source_id=slug, title="Sunny private room in NOPA",
        original_url=f"https://sfbay.craigslist.org/x/{slug}.html", price=1500, neighborhood="NOPA",
        listing_type="Room/share", summary="Private room in a shared home, flexible lease.",
    )


def card(platform: str, source_id: str, url: str, address: str | None, *, price: int | None = 1900,
         unit_type: str | None = "one_bedroom") -> ListingCandidate:
    """One site's card for a one-bedroom inside the test deal's budget and areas."""
    return ListingCandidate(
        platform=platform, source_id=source_id, title=address or "A home", original_url=url, price=price,
        neighborhood="Mission", listing_type="Apartment",
        summary="Entire one bedroom apartment in a Victorian, sunny.",
        metadata={"address": address} if address else {}, housing_kind="whole_unit", unit_type=unit_type,
    )


def flat(platform: str, slug: str, address: str = "295 Buchanan St #105") -> ListingCandidate:
    url = {
        "Zillow": f"https://www.zillow.com/homedetails/x/{slug}_zpid/",
        "Movoto": f"https://www.movoto.com/san-francisco-ca/{slug}/for-rent/",
    }[platform]
    return card(platform, slug, url, address)


def put(repository: Repository, listing: ListingCandidate) -> int:
    listing_id, _ = repository.upsert_listing(listing, ScoreResult(80, ["Stored"], "", {}, eligibility="eligible"))
    return listing_id


def aged_out(repository: Repository, *listing_ids: int) -> None:
    """Moved to the Archive by the 21-day sweep itself: found a month ago, and nobody acted."""
    month_ago = (datetime.now(UTC) - timedelta(days=30)).isoformat(timespec="seconds")
    with repository.connection() as connection:
        connection.executemany(
            "UPDATE listings SET first_found = ? WHERE id = ?", [(month_ago, listing_id) for listing_id in listing_ids]
        )
        connection.commit()
    assert repository.archive_stale_listings() == len(listing_ids)


def scored_by_an_old_deal(repository: Repository, *listing_ids: int) -> None:
    with repository.connection() as connection:
        connection.executemany(
            "UPDATE listings SET score = ?, eligibility = ? WHERE id = ?",
            [(*OLD_VERDICT, listing_id) for listing_id in listing_ids],
        )
        connection.commit()


def stored_verdict(repository: Repository, listing_id: int) -> tuple[int, str]:
    with repository.connection() as connection:
        row = connection.execute("SELECT score, eligibility FROM listings WHERE id = ?", (listing_id,)).fetchone()
    return int(row["score"]), str(row["eligibility"])


def deal_verdict(listing: ListingCandidate) -> tuple[int, str]:
    """What the deal in hand makes of this listing."""
    result = score_listing(classify_listing(listing), parse_preferences(TEST_PREFERENCES))
    assert (result.score, result.eligibility) != OLD_VERDICT, "the stand-in must be a verdict the deal never gives"
    return result.score, result.eligibility


def state(repository: Repository, listing_id: int) -> tuple[str, str | None]:
    with repository.connection() as connection:
        row = connection.execute("SELECT status, status_reason FROM listings WHERE id = ?", (listing_id,)).fetchone()
    return row["status"], row["status_reason"]


def key(repository: Repository, listing_id: int) -> str:
    with repository.connection() as connection:
        return connection.execute("SELECT home_key FROM listings WHERE id = ?", (listing_id,)).fetchone()[0]


def scanner(repository: Repository, preferences: Preferences) -> Scanner:
    return Scanner(repository, lambda: preferences, [], detail_delay_seconds=0)


def settings_for(tmp_path: Path) -> Settings:
    data_dir = tmp_path / "data"
    preferences_path = tmp_path / "preferences.yaml"
    preferences_path.write_text(TEST_PREFERENCES, encoding="utf-8")
    return Settings(data_dir=data_dir, preferences_path=preferences_path,
                    database_path=data_dir / "housing.sqlite3", log_path=data_dir / "test.log")


@pytest.fixture
def app_and_repository(tmp_path: Path):
    """The app, started on an empty board: it scores nothing and marks the board
    scored with the test deal, so the rows each test puts keep what they hold."""
    settings = settings_for(tmp_path)
    repository = Repository(settings.database_path)
    repository.initialize()
    return create_app(settings=settings, sources=[], enable_scheduler=False), repository


@pytest.fixture
def score_writes(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Every listing a score is written for, wherever it is written from."""
    written: list[int] = []
    real = Repository.update_score

    def counting(self, listing_id, *args, **kwargs):
        written.append(listing_id)
        return real(self, listing_id, *args, **kwargs)

    monkeypatch.setattr(Repository, "update_score", counting)
    return written


# --------------------------------------------------------------------------
# a pass scores what is in play
# --------------------------------------------------------------------------


def test_a_rescore_scores_every_home_in_play_and_leaves_the_aged_out_archive_as_it_was(
    repository: Repository, preferences: Preferences
) -> None:
    ordinary = put(repository, room("ordinary"))
    starred = put(repository, room("starred"))
    passed = put(repository, room("passed"))
    restored = put(repository, room("restored"))
    archived = put(repository, room("archived"))
    shown = put(repository, flat("Zillow", "1001"))
    # Movoto has listed the same flat a month longer, so only its copy aged out.
    aged_copy = put(repository, flat("Movoto", "m105"))
    aged_out(repository, restored, archived, aged_copy)
    repository.set_listing_status(restored, "active")
    repository.set_listing_status(starred, "saved")
    repository.set_listing_status(passed, "dismissed")
    in_play = {
        ordinary: room("ordinary"), starred: room("starred"), passed: room("passed"),
        restored: room("restored"), shown: flat("Zillow", "1001"), aged_copy: flat("Movoto", "m105"),
    }
    scored_by_an_old_deal(repository, *in_play, archived)

    scored = scanner(repository, preferences).rescore_all(preferences)

    assert {listing_id: stored_verdict(repository, listing_id) for listing_id in in_play} == {
        listing_id: deal_verdict(listing) for listing_id, listing in in_play.items()
    }
    assert stored_verdict(repository, archived) == OLD_VERDICT, "an aged-out home was scored with the rest"
    assert scored == len(in_play), "the count is the rows scored"


# --------------------------------------------------------------------------
# acting on a home the archive aged out
# --------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["active", "saved", "dismissed"])
def test_restoring_starring_or_passing_an_aged_out_home_scores_every_copy_against_the_deal_in_hand(
    app_and_repository, status: str
) -> None:
    application, repository = app_and_repository
    copies = {put(repository, listing): listing for listing in (flat("Zillow", "1001"), flat("Movoto", "m105"))}
    aged_out(repository, *copies)
    scored_by_an_old_deal(repository, *copies)
    zillow, movoto = copies

    with TestClient(application) as client:
        response = client.post(
            f"/listings/{movoto}/status", data={"status": status, "return_to": "/"}, follow_redirects=False
        )

    assert response.status_code == 303
    assert {listing_id: stored_verdict(repository, listing_id) for listing_id in copies} == {
        listing_id: deal_verdict(listing) for listing_id, listing in copies.items()
    }, "the home came back holding a verdict from an older deal"
    assert state(repository, zillow)[0] == status


def test_a_note_on_an_aged_out_home_scores_it_and_an_empty_note_leaves_it_aged_out(app_and_repository) -> None:
    """A note brings an aged-out home back (see set_listing_note); a blank one
    does not, so it is still in the Archive and has nothing to be scored for."""
    application, repository = app_and_repository
    noted, blank = put(repository, room("noted")), put(repository, room("blank"))
    aged_out(repository, noted, blank)
    scored_by_an_old_deal(repository, noted, blank)

    with TestClient(application) as client:
        for listing_id, note in ((noted, "Worth a call after all"), (blank, "   ")):
            response = client.post(
                f"/listings/{listing_id}/note", data={"note": note, "return_to": "/"}, follow_redirects=False
            )
            assert response.status_code == 303

    assert (state(repository, noted), stored_verdict(repository, noted)) == (
        ("active", None), deal_verdict(room("noted"))
    )
    assert (state(repository, blank), stored_verdict(repository, blank)) == (("dismissed", "aged"), OLD_VERDICT)


def test_acting_on_a_home_already_in_play_scores_nothing_again(app_and_repository, score_writes) -> None:
    """Only a home every copy of which aged out can hold an older deal's
    verdict; a star, a note or a pass on any other is a write, not a pass.
    That includes a star on the one copy of a flat that aged out while another
    site still lists it: the home was scored with the rest."""
    application, repository = app_and_repository
    live = put(repository, room("live"))
    put(repository, flat("Zillow", "1001"))
    aged_copy = put(repository, flat("Movoto", "m105"))
    aged_out(repository, aged_copy)
    score_writes.clear()

    with TestClient(application) as client:
        for path, form in (
            (f"/listings/{live}/status", {"status": "saved"}),
            (f"/listings/{live}/note", {"note": "Call on Tuesday"}),
            (f"/listings/{live}/status", {"status": "dismissed"}),
            (f"/listings/{aged_copy}/status", {"status": "saved"}),
        ):
            assert client.post(path, data={**form, "return_to": "/"}, follow_redirects=False).status_code == 303

    assert score_writes == []


def test_a_restore_whose_scoring_fails_is_still_a_restore(app_and_repository, monkeypatch) -> None:
    """The restore is saved before the home is scored, and a pass that cannot
    finish -- a board busy past its wait -- must not turn it into an error page."""
    application, repository = app_and_repository
    home = put(repository, room("r1"))
    aged_out(repository, home)

    def busy(self, *args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(Scanner, "rescore_home", busy)
    with TestClient(application) as client:
        response = client.post(
            f"/listings/{home}/status", data={"status": "active", "return_to": "/"}, follow_redirects=False
        )

    assert response.status_code == 303
    assert state(repository, home) == ("active", "user")


def test_a_restore_whose_scoring_fails_takes_back_the_board_s_mark(app_and_repository, monkeypatch) -> None:
    """Failing before its own pass could retract it, the scoring left the mark
    claiming a board that now holds a home scored by an older deal -- and the
    next start trusted it and scored nothing. Taken back, the next start scores
    every home in play, the restored one with them."""
    application, repository = app_and_repository
    home = put(repository, room("r1"))
    aged_out(repository, home)
    assert repository.scoring_mark() is not None, "the app marked the board scored when it started"

    def busy(self, *args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(Scanner, "rescore_home", busy)
    with TestClient(application) as client:
        client.post(f"/listings/{home}/status", data={"status": "active", "return_to": "/"}, follow_redirects=False)

    assert repository.scoring_mark() is None


# --------------------------------------------------------------------------
# the mark
# --------------------------------------------------------------------------


@pytest.mark.parametrize("mark", ["from an older deal", None, "the deal in hand"])
def test_scoring_one_home_puts_the_board_s_mark_back_as_it_was(
    repository: Repository, preferences: Preferences, mark: str | None
) -> None:
    """One home now agrees with whatever the mark said. Cleared, the next start
    would score the whole board again; claimed, a mark already out of date
    would skip the pass the rest of the board is owed."""
    kept = current_fingerprint(preferences) if mark == "the deal in hand" else mark
    home = put(repository, room("r1"))
    aged_out(repository, home)
    scored_by_an_old_deal(repository, home)
    with repository.connection() as connection:
        repository.set_scoring_mark(connection, kept)
        connection.commit()

    scored = scanner(repository, preferences).rescore_home(home, preferences)

    assert (scored, stored_verdict(repository, home)) == (1, deal_verdict(room("r1")))
    assert repository.scoring_mark() == kept


# --------------------------------------------------------------------------
# start-up and a saved deal
# --------------------------------------------------------------------------


def test_the_first_start_after_an_update_scores_only_the_homes_in_play(tmp_path: Path, score_writes) -> None:
    settings = settings_for(tmp_path)
    repository = Repository(settings.database_path)
    repository.initialize()
    live = [put(repository, room(f"live-{index}")) for index in range(3)]
    repository.set_listing_status(live[1], "saved")
    repository.set_listing_status(live[2], "dismissed")
    archived = [put(repository, room(f"archived-{index}")) for index in range(5)]
    aged_out(repository, *archived)
    scored_by_an_old_deal(repository, *live, *archived)
    # What the last start left, and what the installer does to it.
    deal = parse_preferences(TEST_PREFERENCES)
    with repository.connection() as connection:
        repository.set_scoring_mark(connection, current_fingerprint(deal))
        connection.commit()
        connection.execute("DELETE FROM scoring_state WHERE name = 'rescore_fingerprint'")
        connection.commit()
    score_writes.clear()

    with TestClient(create_app(settings=settings, sources=[], enable_scheduler=False)) as client:
        assert client.get("/").status_code == 200

    assert sorted(score_writes) == sorted(live)
    assert [stored_verdict(repository, listing_id) for listing_id in archived] == [OLD_VERDICT] * 5
    assert repository.scoring_mark() == current_fingerprint(deal), "the next start would score them all again"


def test_saving_a_deal_scores_only_the_homes_in_play(app_and_repository, score_writes) -> None:
    application, repository = app_and_repository
    live = put(repository, room("live"))
    archived = [put(repository, room(f"archived-{index}")) for index in range(2)]
    aged_out(repository, *archived)
    scored_by_an_old_deal(repository, live, *archived)
    score_writes.clear()

    deal = {
        "housing_paths": "private_room",
        "private_room_maximum": "1800",
        "anywhere_in_sf": "on",
        "move_in_flexible": "on",
    }
    with TestClient(application) as client:
        response = client.post("/preferences/deal", data=deal, follow_redirects=False)

    assert response.status_code == 303, response.text
    assert "1+stored+listings+were+reranked" in response.headers["location"]
    assert score_writes == [live]
    assert [stored_verdict(repository, listing_id) for listing_id in archived] == [OLD_VERDICT] * 2


# --------------------------------------------------------------------------
# which rows a pass re-keys
# --------------------------------------------------------------------------


def test_a_rescore_re_keys_the_aged_twin_of_a_card_it_scored_and_no_other_aged_home(
    repository: Repository, preferences: Preferences
) -> None:
    """ApartmentGuide's card for 1114 Sutter was stored before its size could be
    read, so it was a home of its own; the archive has since aged out Rent.com's
    copy of the same record, which states the size. The pass reads the size
    from the card's own text, and with it the two are one offer -- so the aged
    twin has to follow, though the pass never chose it. An aged-out home that is
    nobody's twin is neither scored nor rewritten: re-keying the whole board
    read and rewrote every row of a year of listings."""
    guide = put(repository, card("ApartmentGuide", "6589544",
                                 "https://www.apartmentguide.com/a/1114-Sutter-St-6589544/", "1114 Sutter St"))
    twin = put(repository, card("Rent.com", "lc6589544",
                                "https://www.rent.com/apartment/1114-sutter-st-lc6589544", None))
    elsewhere = put(repository, flat("Zillow", "2001", "10 Oak St #3"))
    with repository.connection() as connection:
        connection.execute("UPDATE listings SET unit_type = NULL WHERE id = ?", (guide,))
        connection.commit()
        repository.refresh_identities(connection)
    aged_out(repository, twin, elsewhere)
    # A key no row would earn under today's rules, so a rewrite of it shows.
    with repository.connection() as connection:
        connection.execute("UPDATE listings SET home_key = 'zpid:2001' WHERE id = ?", (elsewhere,))
        connection.commit()
    assert key(repository, guide) != key(repository, twin), "the twin must be a home of its own to begin with"

    scanner(repository, preferences).rescore_all(preferences)

    assert key(repository, twin) == key(repository, guide) == "offer:1114|SUTTER ST|one_bedroom|1900"
    assert key(repository, elsewhere) == "zpid:2001", "an aged-out home the pass never scored was re-keyed"


def test_a_twin_the_re_key_joins_to_a_home_in_play_is_scored_with_it(
    repository: Repository, preferences: Preferences
) -> None:
    """The aged Rent.com copy of 1114 Sutter was not in play when the pass chose
    its rows, and joined the scored card's home only when the pass re-keyed it.
    Left with an older deal's verdict and confirmed today, it held the home
    down -- the page ranks a home by its least favourable current copy -- and
    the mark then said the board was scored."""
    guide_card = card("ApartmentGuide", "6589544", "https://www.apartmentguide.com/a/1114-Sutter-St-6589544/",
                      "1114 Sutter St")
    twin_card = card("Rent.com", "lc6589544", "https://www.rent.com/apartment/1114-sutter-st-lc6589544", None)
    guide = put(repository, guide_card)
    twin = put(repository, twin_card)
    with repository.connection() as connection:
        connection.execute("UPDATE listings SET unit_type = NULL WHERE id = ?", (guide,))
        connection.commit()
        repository.refresh_identities(connection)
    aged_out(repository, twin)
    scored_by_an_old_deal(repository, twin)
    assert key(repository, guide) != key(repository, twin), "the twin must be a home of its own to begin with"

    scored = scanner(repository, preferences).rescore_all(preferences)

    assert key(repository, twin) == key(repository, guide), "the re-key joined them"
    assert stored_verdict(repository, twin) == deal_verdict(twin_card), "the joined twin kept an older deal's verdict"
    assert scored == 2, "the count is every row the pass scored, the twin included"


def test_a_re_key_among_some_rows_reaches_them_and_their_twins_and_only_a_full_one_reaches_the_rest(
    repository: Repository,
) -> None:
    guide = put(repository, card("ApartmentGuide", "LV3299974248",
                                 "https://www.apartmentguide.com/rent/108-Langton-LV3299974248/",
                                 "108 Langton St unit B"))
    twin = put(repository, card("Rent.com", "lv3299974248", "https://www.rent.com/r/108-langton-lv3299974248",
                                None, price=None, unit_type=None))
    elsewhere = put(repository, flat("Zillow", "2001", "10 Oak St #3"))
    with repository.connection() as connection:
        # The key the twin carried before the guide's card said where the flat
        # is, and one the unrelated row would not earn under today's rules.
        connection.execute("UPDATE listings SET home_key = 'rentpath:lv3299974248' WHERE id = ?", (twin,))
        connection.execute("UPDATE listings SET home_key = 'zpid:2001' WHERE id = ?", (elsewhere,))
        connection.commit()

        repository.refresh_identities(connection, among=[guide])
        assert key(repository, twin) == key(repository, guide) == "unit:108|LANGTON ST|B"
        assert key(repository, elsewhere) == "zpid:2001", "a row nobody asked about was re-keyed"

        repository.refresh_identities(connection)
    assert key(repository, elsewhere) == "unit:10|OAK ST|3"


def test_a_re_key_among_a_year_of_live_rows_reaches_every_one_of_them(repository: Repository) -> None:
    """A live board runs to thousands of rows, read back in chunks; a row past
    the first chunk left under an old key would be one home split in two."""
    first = put(repository, flat("Zillow", "3000", "100 Oak St #3"))
    with repository.connection() as connection:
        columns = [str(row["name"]) for row in connection.execute("PRAGMA table_info(listings)") if row["name"] != "id"]
        varied = {
            "source_id": "source_id || '-' || n.i",
            "canonical_url": "canonical_url || '-' || n.i",
            "original_url": "original_url || '-' || n.i",
            "metadata_json": "json_set(metadata_json, '$.address', (100 + n.i) || ' Oak St #3')",
        }
        connection.execute(
            f"""WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 1199)
                INSERT INTO listings ({', '.join(columns)})
                SELECT {', '.join(varied.get(column, column) for column in columns)}
                FROM listings, n WHERE listings.id = ?""",
            (first,),
        )
        connection.execute("UPDATE listings SET home_key = 'stale'")
        connection.commit()
        ids = [int(row[0]) for row in connection.execute("SELECT id FROM listings ORDER BY id")]

        repository.refresh_identities(connection, among=ids)

        left = connection.execute("SELECT COUNT(*) FROM listings WHERE home_key = 'stale'").fetchone()[0]
    assert len(ids) == 1200
    assert left == 0
    assert (key(repository, ids[0]), key(repository, ids[-1])) == ("unit:100|OAK ST|3", "unit:1299|OAK ST|3")


# --------------------------------------------------------------------------
# what the Archive says
# --------------------------------------------------------------------------


def test_the_archive_says_a_changed_deal_leaves_an_aged_out_home_until_it_is_restored(app_and_repository) -> None:
    """Its score may be from a deal since changed, and the page is where that is said."""
    application, _ = app_and_repository
    with TestClient(application) as client:
        page = client.get("/?view=all").text
    assert "Changing your deal does not rescore an aged-out home until you restore it" in page


def test_the_deal_page_counts_the_homes_a_save_will_score(app_and_repository) -> None:
    """"Rescoring 213,408 homes" on a year-scale board, for a save that scores
    the few thousand still in play: the wait explained by a number that is not
    the wait."""
    application, repository = app_and_repository
    [put(repository, room(f"live-{index}")) for index in range(3)]
    archived = [put(repository, room(f"archived-{index}")) for index in range(5)]
    aged_out(repository, *archived)

    with TestClient(application) as client:
        page = client.get("/preferences").text

    assert 'data-listing-count="3"' in page
    assert repository.count_live_listings() == 3 and repository.count_listings() == 8


@pytest.mark.parametrize("starred", [False, True], ids=["on the shortlist", "starred"])
def test_another_site_s_copy_a_re_key_joins_to_a_home_in_play_is_scored_with_it(
    repository: Repository, preferences: Preferences, starred: bool
) -> None:
    """Review of WO-5: the pass scored the rows it re-keyed and their
    syndicated twins, but a re-keyed row can join a home another site's copy
    was already filed under -- Redfin's aged card for 1114 Sutter, once
    Apartment List's card is read with its size. That copy kept an older
    deal's verdict inside a home in play (starred with it, if the home was),
    and the mark said the board was scored."""
    redfin_card = card("Redfin", "rf-1114", "https://www.redfin.com/CA/San-Francisco/1114-Sutter-St-94109/home/123",
                       "1114 Sutter St")
    listed_card = card("Apartment List", "al-1114", "https://www.apartmentlist.com/ca/san-francisco/1114-sutter",
                       "1114 Sutter St")
    aged = put(repository, redfin_card)
    listed = put(repository, listed_card)
    with repository.connection() as connection:
        # Stored before its size could be read: no size, no offer key, a home of its own.
        connection.execute("UPDATE listings SET unit_type = NULL WHERE id = ?", (listed,))
        connection.commit()
        repository.refresh_identities(connection)
    aged_out(repository, aged)
    scored_by_an_old_deal(repository, aged)
    if starred:
        repository.set_listing_status(listed, "saved")
    assert key(repository, listed) != key(repository, aged), "two homes to begin with"

    scanner(repository, preferences).rescore_all(preferences)

    assert key(repository, aged) == key(repository, listed), "the re-key joined them"
    assert stored_verdict(repository, aged) == deal_verdict(redfin_card), "the joined copy kept an older deal's verdict"
    if starred:
        assert state(repository, aged) == ("saved", "user")


class ApartmentGuideSearch:
    """ApartmentGuide as a scan asks it, returning one card."""

    platform = "ApartmentGuide"
    mode = "automatic"
    search_url = "https://www.apartmentguide.com/apartments/California/San-Francisco/"
    manual_reason = None
    detail_budget = 0

    def __init__(self, listing: ListingCandidate) -> None:
        self.listing = listing

    def search(self, client, preferences):
        return [self.listing]

    def enrich(self, client, listing):
        return listing


def test_a_scan_whose_new_card_joins_an_aged_copy_to_a_starred_home_scores_that_copy(
    repository: Repository, preferences: Preferences
) -> None:
    """Review of WO-5: after a deal change, a scan stored ApartmentGuide's card
    for 1114 Sutter, whose address joined Rent.com's aged twin to the home the
    user had starred. The star reached the twin; nothing scored it. As the
    user's own copy it was the one shown, with the verdict of a deal no longer
    in hand -- a flat the deal rules out could sit on the shortlist."""
    starred = put(repository, card("Redfin", "rf-1114",
                                   "https://www.redfin.com/CA/San-Francisco/1114-Sutter-St-94109/home/123",
                                   "1114 Sutter St"))
    repository.set_listing_status(starred, "saved")
    twin_card = card("Rent.com", "lc6589544", "https://www.rent.com/apartment/1114-sutter-st-lc6589544", None)
    twin = put(repository, twin_card)
    aged_out(repository, twin)
    scored_by_an_old_deal(repository, twin)
    scanner(repository, preferences).rescore_all(preferences)
    assert stored_verdict(repository, twin) == OLD_VERDICT, "the deal change leaves the aged twin as it was"
    assert key(repository, twin) != key(repository, starred)
    guide_card = card("ApartmentGuide", "6589544", "https://www.apartmentguide.com/a/1114-Sutter-St-6589544/",
                      "1114 Sutter St")

    outcome = Scanner(
        repository, lambda: preferences, [ApartmentGuideSearch(guide_card)], detail_delay_seconds=0
    ).run_scan("scheduled")

    assert outcome.status == "completed"
    assert key(repository, twin) == key(repository, starred), "the card joined the twin to the starred home"
    assert state(repository, twin) == ("saved", "user"), "and the home's star reached it"
    assert stored_verdict(repository, twin) == deal_verdict(twin_card), "a starred copy holds an older deal's verdict"


def test_a_stored_home_with_no_area_gets_one_from_the_address_it_already_carries(
    repository: Repository, preferences: Preferences
) -> None:
    """An area must not depend on the day a home was found.

    A card's area is worked out from its address when the card is first read,
    so every home stored before the street table could place its block kept a
    blank area for ever -- 44 of the 98 homes on the owner's shortlist, 36 of
    them Zillow's, each one carrying a street address the table can name. They
    sat there scoring half marks for an unknown area while the table knew the
    answer. Nothing is fetched: the address is already on the row.
    """
    listing_id = put(repository, card("Zillow", "sunset", "https://www.zillow.com/homedetails/x/s_zpid/",
                                      "2817 Pacheco St"))
    with repository.connection() as connection:
        connection.execute("UPDATE listings SET neighborhood = '' WHERE id = ?", (listing_id,))
        connection.commit()

    Scanner(repository, lambda: preferences, [], detail_delay_seconds=0).rescore_all(preferences)

    with repository.connection() as connection:
        area = connection.execute(
            "SELECT neighborhood FROM listings WHERE id = ?", (listing_id,)
        ).fetchone()["neighborhood"]
    assert area == "Outer Sunset"


def test_a_stored_home_the_table_cannot_place_is_left_without_an_area(
    repository: Repository, preferences: Preferences
) -> None:
    """The backfill fills blanks; it does not guess at them.

    A block the city splits between two areas answers nothing, and a home on
    one keeps its unknown area -- which costs it half the area score and keeps
    it on the board -- rather than being handed whichever neighbour won by a
    vote.
    """
    listing_id = put(repository, card("Zillow", "nowhere", "https://www.zillow.com/homedetails/x/n_zpid/",
                                      "1 Nonexistent Parkway"))
    with repository.connection() as connection:
        connection.execute("UPDATE listings SET neighborhood = '' WHERE id = ?", (listing_id,))
        connection.commit()

    Scanner(repository, lambda: preferences, [], detail_delay_seconds=0).rescore_all(preferences)

    with repository.connection() as connection:
        area = connection.execute(
            "SELECT neighborhood FROM listings WHERE id = ?", (listing_id,)
        ).fetchone()["neighborhood"]
    assert area == ""
