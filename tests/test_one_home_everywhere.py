"""One real home behaves as one home: stored, passed, starred and kept.

WO-3 todos 1-3. The same flat on Zillow, Movoto and Redfin was three rows that
nothing connected, so passing it on one site left it on the shortlist from the
other two. And a site reusing an id silently rewrote a starred home into a
different apartment -- reproduced on the real board as a saved row with the
note "great light, viewing Tuesday 6pm" becoming another address at $7,200,
star and note intact.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pytest

from sf_housing.database import SCHEMA_VERSION, Repository
from sf_housing.models import ListingCandidate, ScoreResult


def card(platform: str, source_id: str, url: str, address: str | None, *, price: int | None = 3200,
         unit_type: str | None = "one_bedroom", title: str | None = None, summary: str | None = None,
         **metadata) -> ListingCandidate:
    return ListingCandidate(
        platform=platform,
        source_id=source_id,
        title=title or address or "A home",
        original_url=url,
        price=price,
        neighborhood="Hayes Valley",
        listing_type="Apartment",
        summary=summary or "Entire one bedroom apartment, 1 bed 1 bath, in-unit laundry.",
        metadata={**({"address": address} if address else {}), **metadata},
        housing_kind="whole_unit",
        unit_type=unit_type,
    )


def put(repository: Repository, listing: ListingCandidate) -> int:
    # A stored verdict rather than a scored one: these tests are about which
    # rows are one home, and the test deal is for rooms.
    listing_id, _ = repository.upsert_listing(listing, ScoreResult(80, ["Stored"], "", {}, eligibility="eligible"))
    return listing_id


def rows(repository: Repository, where: str = "1", *parameters) -> list[sqlite3.Row]:
    with repository.connection() as connection:
        return connection.execute(f"SELECT * FROM listings WHERE {where} ORDER BY id", parameters).fetchall()


def zillow(zpid: str, address: str, **kwargs) -> ListingCandidate:
    return card("Zillow", zpid, f"https://www.zillow.com/homedetails/x/{zpid}_zpid/", address, **kwargs)


def movoto(slug: str, address: str, **kwargs) -> ListingCandidate:
    return card("Movoto", slug, f"https://www.movoto.com/san-francisco-ca/{slug}/for-rent/", address, **kwargs)


def redfin(rid: str, address: str, unit: str, **kwargs) -> ListingCandidate:
    return card("Redfin", rid, f"https://www.redfin.com/CA/San-Francisco/x/unit-{unit}/home/{rid}", address, **kwargs)


@pytest.fixture
def repository(tmp_path: Path) -> Repository:
    instance = Repository(tmp_path / "housing.sqlite3")
    instance.initialize()
    return instance


# --------------------------------------------------------------------------
# a decision about a home is a decision about every copy of it
# --------------------------------------------------------------------------


def test_passing_one_copy_passes_the_home_on_every_site(repository: Repository) -> None:
    """WO-3 todo 2: passing touched one row, so the flat came back from the
    other sites on the next page load."""
    a = put(repository, zillow("1001", "295 Buchanan St #105"))
    b = put(repository, movoto("m105", "295 Buchanan St", unit="APT 105"))
    c = put(repository, redfin("r105", "295 Buchanan St Unit 105", "105"))
    elsewhere = put(repository, zillow("1002", "295 Buchanan St #106"))

    assert repository.set_listing_status(b, "dismissed")

    statuses = {row["id"]: (row["status"], row["status_reason"]) for row in rows(repository)}
    assert statuses[a] == statuses[b] == statuses[c] == ("dismissed", "user")
    assert statuses[elsewhere] == ("active", None), "the flat next door is another home"
    shortlist = repository.query_listings(housing_kind="whole_unit", minimum_score=0, view="active")
    assert [item["id"] for item in shortlist] == [elsewhere]


def test_a_star_and_a_restore_reach_every_copy_too(repository: Repository) -> None:
    a = put(repository, zillow("1001", "295 Buchanan St #105"))
    b = put(repository, movoto("m105", "295 Buchanan St", unit="APT 105"))
    repository.set_listing_status(a, "saved")
    assert {row["status"] for row in rows(repository)} == {"saved"}
    repository.set_listing_status(b, "dismissed")
    repository.set_listing_status(a, "active")
    assert {(row["status"], row["status_reason"]) for row in rows(repository)} == {("active", "user")}


def test_a_copy_found_after_the_pass_arrives_passed(repository: Repository) -> None:
    a = put(repository, zillow("1001", "295 Buchanan St #105"))
    repository.set_listing_status(a, "dismissed")

    late = put(repository, redfin("r105", "295 Buchanan St Unit 105", "105"))

    assert rows(repository, "id = ?", late)[0]["status"] == "dismissed"
    assert repository.query_listings(housing_kind="whole_unit", minimum_score=0, view="active") == []


def test_a_new_copy_of_a_starred_home_is_starred(repository: Repository) -> None:
    a = put(repository, zillow("1001", "295 Buchanan St #105"))
    repository.set_listing_status(a, "saved")
    late = put(repository, movoto("m105", "295 Buchanan St #105"))
    assert rows(repository, "id = ?", late)[0]["status"] == "saved"


def test_a_status_for_a_listing_that_does_not_exist_says_so(repository: Repository) -> None:
    assert repository.set_listing_status(424242, "saved") is False


def test_opening_one_copy_marks_the_home_opened(repository: Repository) -> None:
    a = put(repository, zillow("1001", "295 Buchanan St #105"))
    b = put(repository, movoto("m105", "295 Buchanan St #105"))
    repository.open_listing_url(a)
    assert rows(repository, "id = ?", b)[0]["opened_at"] is not None


# --------------------------------------------------------------------------
# a reused id never moves the user's work onto another home
# --------------------------------------------------------------------------


def _reuse(repository: Repository, first: ListingCandidate, second: ListingCandidate, *, own):
    stored = put(repository, first)
    own(stored)
    before = dict(rows(repository, "id = ?", stored)[0])
    put(repository, second)
    return stored, before


@pytest.mark.parametrize(
    "own",
    [
        lambda repository: (lambda listing_id: repository.set_listing_status(listing_id, "saved")),
        lambda repository: (lambda listing_id: repository.set_listing_note(listing_id, "great light, viewing Tuesday 6pm")),
        lambda repository: (lambda listing_id: repository.set_listing_status(listing_id, "dismissed")),
    ],
    ids=["starred", "noted", "passed"],
)
def test_a_site_reusing_an_id_for_another_flat_leaves_the_user_s_home_untouched(
    repository: Repository, own
) -> None:
    """WO-3 todo 3, as reproduced: a Zumper id handed to a different flat.

    The user's row keeps its address, rent, title, star and note; the other
    flat becomes a row of its own."""
    first = card("Zumper", "29583305", "https://www.zumper.com/listings/29583305p/x", "405 Laguna St #2",
                 price=3200, title="405 Laguna St #2")
    second = card("Zumper", "29583305", "https://www.zumper.com/listings/29583305p/x", "409 Laguna St #4C",
                  price=7200, title="409 Laguna St #4C")

    stored, before = _reuse(repository, first, second, own=own(repository))

    after = dict(rows(repository, "id = ?", stored)[0])
    for field in ("title", "price", "status", "note", "summary", "status_reason"):
        assert after[field] == before[field], field
    kept, was = json.loads(after["metadata_json"]), json.loads(before["metadata_json"])
    assert kept["address"] == was["address"] == "405 Laguna St #2"
    # The page shows another flat now, so the flat that was here is gone from
    # it -- said on the user's row rather than left looking current.
    assert kept["verified_inactive"] is True
    other = rows(repository, "id <> ?", stored)
    assert len(other) == 1
    assert other[0]["title"] == "409 Laguna St #4C" and other[0]["price"] == 7200
    assert other[0]["status"] == "active" and other[0]["note"] == ""


def test_for_a_user_s_home_a_card_that_drops_the_unit_is_proof_only_on_the_same_page(
    repository: Repository,
) -> None:
    """Strict for starred rows: "405 Laguna St" could be any flat in the
    building -- unless it is the very page the star was put on, when it is only
    a thinner card, and splitting the home in two would bring a passed flat
    back and leave a starred one frozen."""
    stored = put(repository, zillow("555", "405 Laguna St #2", price=3200))
    repository.set_listing_status(stored, "saved")

    # The same id on another page: not shown to be the same flat.
    put(repository, card("Zillow", "555", "https://www.zillow.com/homedetails/elsewhere/555_zpid/",
                         "405 Laguna St", price=5400))
    assert rows(repository, "id = ?", stored)[0]["price"] == 3200
    assert len(rows(repository)) == 2

    # Its own page, thinner: the same flat.
    other = put(repository, zillow("777", "405 Laguna St #2", price=3100))
    repository.set_listing_status(other, "dismissed")
    put(repository, zillow("777", "405 Laguna St", price=3150))
    passed = rows(repository, "id = ?", other)[0]
    assert (passed["status"], passed["price"]) == ("dismissed", 3150)
    assert len(rows(repository)) == 3, "no second copy of the passed flat"


def test_for_a_row_nobody_owns_a_reused_id_is_still_a_new_home(repository: Repository) -> None:
    put(repository, zillow("555", "405 Laguna St #2", price=3200))
    put(repository, zillow("555", "409 Laguna St #4C", price=7200))
    assert sorted(row["price"] for row in rows(repository)) == [3200, 7200]


def test_an_id_that_alternates_between_two_flats_keeps_two_rows(repository: Repository) -> None:
    """A slot that shows one flat, then another, then the first again must not
    mint a new row each time: the set-aside row is taken back."""
    a = card("Zumper", "7", "https://www.zumper.com/listings/7p/x", "405 Laguna St #2", price=3200)
    b = card("Zumper", "7", "https://www.zumper.com/listings/7p/x", "409 Laguna St #4C", price=7200)
    for listing in (a, b, a, b, a):
        put(repository, listing)
    assert len(rows(repository)) == 2
    attached = rows(repository, "source_id = '7'")
    assert len(attached) == 1 and attached[0]["price"] == 3200


def test_the_same_page_under_a_new_id_updates_the_one_row(repository: Repository) -> None:
    """Movoto re-issues MLS ids for one page; the row count stays one."""
    url = "https://www.movoto.com/san-francisco-ca/642-alvarado-202/for-rent/"
    for sid in ("hcpxk7rve0bb", "hggqdcr3fyab", "hcpxk7rve0bb"):
        put(repository, card("Movoto", sid, url, "642 Alvarado St #202", price=3595))
    assert len(rows(repository)) == 1


def test_an_id_on_one_row_and_a_page_on_another_no_longer_fails_the_write(repository: Repository) -> None:
    """The three-way collision (WO-4 todo 4): the id matched one row and the
    URL another, and rewriting the first with the second's URL raised
    ``UNIQUE constraint failed`` and abandoned the rest of the source."""
    put(repository, card("Zumper", "a", "https://www.zumper.com/listings/1p/x", "1 Main St #1"))
    put(repository, card("Zumper", "b", "https://www.zumper.com/listings/2p/x", "1 Main St #2"))

    put(repository, card("Zumper", "a", "https://www.zumper.com/listings/2p/x", "1 Main St #2"))

    assert len(rows(repository)) == 2
    assert len(rows(repository, "source_id = 'a'")) == 1


def test_a_card_found_by_another_site_s_page_stays_that_site_s_row(repository: Repository) -> None:
    """Otherwise the next card from the first site finds nothing under its own
    id and stores the home a second time."""
    url = "https://www.zillow.com/homedetails/x/1001_zpid/"
    first = put(repository, card("Zillow", "1001", url, "295 Buchanan St #105"))
    put(repository, card("Zillow alerts", "alert-1", url, "295 Buchanan St #105", price=3300))
    after_alert = rows(repository)
    assert [(row["id"], row["platform"], row["source_id"], row["price"]) for row in after_alert] == [
        (first, "Zillow", "1001", 3300)
    ]
    put(repository, card("Zillow", "1001", url, "295 Buchanan St #105"))
    assert [row["id"] for row in rows(repository)] == [first]


def test_find_listing_answers_exactly_as_the_write_will(repository: Repository) -> None:
    """The scan merges what it finds into the card before storing it. Finding
    a starred home under a reused id would carry that home's address onto
    another flat, and the write would then think them the same."""
    stored = put(repository, zillow("555", "405 Laguna St #2"))
    repository.set_listing_status(stored, "saved")
    other = zillow("555", "409 Laguna St #4C")
    assert repository.find_listing(other.platform, other.source_id, other.original_url, other) is None
    same = zillow("555", "405 Laguna St Unit 2")
    assert repository.find_listing(same.platform, same.source_id, same.original_url, same)["id"] == stored


def test_a_star_racing_reused_id_writes_neither_fails_nor_moves_the_home(repository: Repository) -> None:
    """Lookup, decision and write are one BEGIN IMMEDIATE transaction."""
    stored = put(repository, zillow("555", "405 Laguna St #2", price=3200))
    errors: list[BaseException] = []

    def star() -> None:
        try:
            for _ in range(20):
                repository.set_listing_status(stored, "saved")
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    thread = threading.Thread(target=star)
    thread.start()
    for index in range(20):
        put(repository, zillow("555", "409 Laguna St #4C", price=7200 + index))
    thread.join()
    assert not errors
    row = rows(repository, "id = ?", stored)[0]
    # Whichever landed first, the flat at 405 was never rewritten into 409.
    assert (row["status"], row["title"], row["price"]) == ("saved", "405 Laguna St #2", 3200)


# --------------------------------------------------------------------------
# the real cases the rules were measured on
# --------------------------------------------------------------------------


def home_count(repository: Repository) -> int:
    with repository.connection() as connection:
        return connection.execute(
            "SELECT COUNT(DISTINCT COALESCE(NULLIF(home_key, ''), 'row:' || id)) FROM listings"
        ).fetchone()[0]


def test_1114_sutter_is_two_homes_not_five(repository: Repository) -> None:
    """The building's "1 bed from $2,895" on four sites, and Zillow's own page
    for APT 201. (Uloop's card states no size, so it stays its own.)"""
    for platform, sid, url in (
        ("ApartmentGuide", "6589544", "https://www.apartmentguide.com/a/1114-Sutter-St-6589544/"),
        ("Rent.com", "lc6589544", "https://www.rent.com/apartment/1114-sutter-st-lc6589544"),
        ("Redfin", "570892", "https://www.redfin.com/CA/San-Francisco/1114-Sutter-St-94109/apartment/570892"),
        ("Movoto", "bqd3xnj87sab", "https://www.movoto.com/rental/1114-sutter-street/pid_bqd3xnj87sab/"),
    ):
        put(repository, card(platform, sid, url, "1114 Sutter St", price=2895, title="1114 Sutter St."))
    put(repository, zillow("2099090461", "1114 Sutter St APT 201", price=2895))
    assert home_count(repository) == 2


def test_mission_rock_s_two_sizes_stay_two_homes(repository: Repository) -> None:
    """Rent.com lc6586744 (3 beds, $14,895) is the twin of ApartmentGuide
    6586744 (1 bed, $5,550): a complex card describes whichever floorplan."""
    put(repository, card("ApartmentGuide", "6586744", "https://www.apartmentguide.com/a/Mission-Rock-6586744/",
                         "1023 3rd St", price=5550, title="Mission Rock"))
    put(repository, card("Rent.com", "lc6586744", "https://www.rent.com/apartment/mission-rock-lc6586744",
                         "1023 3rd St", price=14895, unit_type="three_bedroom", title="Mission Rock",
                         summary="Three bedroom apartments at Mission Rock, 3 bed 2 bath."))
    put(repository, card("Redfin", "195166787", "https://www.redfin.com/CA/San-Francisco/Mission-Rock/apartment/195166787",
                         "1023 3rd St", price=14895, unit_type="three_bedroom", title="Mission Rock",
                         summary="Three bedroom apartments at Mission Rock, 3 bed 2 bath."))
    keys = {row["unit_type"]: row["home_key"] for row in rows(repository)}
    assert home_count(repository) == 2
    assert len({row["home_key"] for row in rows(repository, "unit_type = 'three_bedroom'")}) == 1


def test_a_rentpath_twin_that_names_nothing_joins_the_one_that_does(repository: Repository) -> None:
    """108 Langton unit B: ApartmentGuide states the rent, Rent.com's copy of
    the same record states neither rent nor size."""
    put(repository, card("ApartmentGuide", "LV3299974248", "https://www.apartmentguide.com/rent/108-Langton-LV3299974248/",
                         "108 Langton St unit B", price=3750))
    put(repository, card("Rent.com", "lv3299974248", "https://www.rent.com/r/108-langton-lv3299974248",
                         None, price=None, unit_type=None))
    assert home_count(repository) == 1


def test_a_zillow_alert_email_joins_the_page_it_came_from(repository: Repository) -> None:
    put(repository, zillow("465332690", "606 Masonic Ave", price=10500, unit_type="four_bedroom"))
    put(repository, card("Zillow", "465332690_zpid", "https://www.zillow.com/homedetails/465332690_zpid/?utm=alert",
                         None, price=10500, unit_type="four_bedroom"))
    assert home_count(repository) == 1


def test_the_twin_that_arrives_first_is_re_keyed_when_the_other_arrives(repository: Repository) -> None:
    rent = put(repository, card("Rent.com", "lv3299974248", "https://www.rent.com/r/108-langton-lv3299974248",
                                None, price=None, unit_type=None))
    guide = put(repository, card("ApartmentGuide", "LV3299974248", "https://www.apartmentguide.com/rent/x-LV3299974248/",
                                 "108 Langton St unit B", price=3750))
    movo = put(repository, movoto("langton-b", "108 Langton St Unit B", price=3750))
    keys = {row["id"]: row["home_key"] for row in rows(repository)}
    assert keys[rent] == keys[guide] == keys[movo] == "unit:108|LANGTON ST|B"


# --------------------------------------------------------------------------
# migrating a board from before
# --------------------------------------------------------------------------


def _pre_wo3_board(path: Path) -> None:
    """A board as the previous version left it: no identity columns."""
    repository = Repository(path)
    repository.initialize()
    for listing in (
        zillow("1001", "295 Buchanan St #105"),
        movoto("m105", "295 Buchanan St #105"),
        zillow("1002", "10 Oak St #3"),
        card("Craigslist", "cl1", "https://sfbay.craigslist.org/x/cl1.html", None, price=2100),
    ):
        put(repository, listing)
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE listings SET status = 'saved', note = 'call Tuesday' WHERE source_id = 'm105'")
        connection.execute("UPDATE listings SET status = 'dismissed', neighborhood = 'hayes valley' WHERE source_id = '1002'")
        # Nor anything the version after it added (WO-5), which names them.
        for trigger in ("listings_version_insert", "listings_version_update", "listings_version_delete"):
            connection.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        connection.execute("DROP TABLE IF EXISTS listings_version")
        connection.execute("DROP INDEX IF EXISTS idx_listings_home_shape")
        connection.execute("DROP INDEX IF EXISTS idx_listings_status_seen")
        connection.execute("DROP INDEX IF EXISTS idx_listings_seen")
        connection.execute("DROP INDEX IF EXISTS idx_source_runs_scan")
        connection.execute("DROP INDEX IF EXISTS idx_source_runs_searched")
        connection.execute("ALTER TABLE listings DROP COLUMN aged_at")
        connection.execute("DROP INDEX IF EXISTS idx_listings_home")
        for column in ("home_key", "status_reason", "copy_rank"):
            connection.execute(f"ALTER TABLE listings DROP COLUMN {column}")
        connection.execute("DELETE FROM scoring_state WHERE name = 'identity_version'")
        connection.execute("PRAGMA user_version = 3")


def test_a_board_from_before_keeps_every_star_pass_and_note(tmp_path: Path) -> None:
    path = tmp_path / "housing.sqlite3"
    _pre_wo3_board(path)

    repository = Repository(path)
    repository.initialize()

    by_sid = {row["source_id"]: row for row in rows(repository)}
    assert by_sid["m105"]["status"] == "saved" and by_sid["m105"]["note"] == "call Tuesday"
    assert by_sid["m105"]["status_reason"] == "user"
    # The star was about the home, so its other copy is starred with it.
    assert by_sid["1001"]["status"] == "saved"
    assert by_sid["1002"]["status"] == "dismissed" and by_sid["1002"]["status_reason"] == "user"
    assert by_sid["1002"]["neighborhood"] == "Hayes Valley"
    assert by_sid["cl1"]["status"] == "active" and by_sid["cl1"]["status_reason"] is None
    assert by_sid["1001"]["home_key"] == by_sid["m105"]["home_key"] == "unit:295|BUCHANAN ST|105"
    assert by_sid["cl1"]["home_key"] == ""
    assert repository.schema_version() == SCHEMA_VERSION


def test_migrating_twice_changes_nothing(tmp_path: Path) -> None:
    path = tmp_path / "housing.sqlite3"
    _pre_wo3_board(path)
    Repository(path).initialize()
    first = [dict(row) for row in rows(Repository(path))]
    Repository(path).initialize()
    assert [dict(row) for row in rows(Repository(path))] == first


def test_two_starts_racing_on_one_board_migrate_it_once(tmp_path: Path) -> None:
    path = tmp_path / "housing.sqlite3"
    _pre_wo3_board(path)
    errors: list[BaseException] = []

    def start() -> None:
        try:
            Repository(path).initialize()
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=start) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert {row["status"] for row in rows(Repository(path), "source_id IN ('1001', 'm105')")} == {"saved"}


def test_rows_an_older_version_wrote_after_a_downgrade_are_keyed_on_the_next_start(tmp_path: Path) -> None:
    path = tmp_path / "housing.sqlite3"
    repository = Repository(path)
    repository.initialize()
    put(repository, zillow("1001", "295 Buchanan St #105"))
    aged = put(repository, zillow("1003", "12 Elm St #1"))
    with sqlite3.connect(path) as connection:
        # What the older version does: inserts without the new columns, and
        # stars a row this version had aged out.
        connection.execute(
            "INSERT INTO listings (platform, source_id, canonical_url, title, concern, score, first_found, "
            "last_seen, original_url, housing_kind, unit_type, price, metadata_json) VALUES "
            "('Movoto', 'm105', 'https://www.movoto.com/san-francisco-ca/m105/for-rent', '295 Buchanan St #105', "
            "'', 80, '2026-09-01', '2026-09-01', 'https://www.movoto.com/san-francisco-ca/m105/for-rent/', "
            "'whole_unit', 'one_bedroom', 3200, '{\"address\": \"295 Buchanan St #105\"}')"
        )
        connection.execute("UPDATE listings SET status = 'dismissed', status_reason = 'aged' WHERE id = ?", (aged,))
        connection.execute("UPDATE listings SET status = 'saved' WHERE id = ?", (aged,))
        # And, like every version since the first, stamps its own schema on
        # the way in -- which is how the next start knows it was here.
        connection.execute("PRAGMA user_version = 3")
    repository.initialize()
    keys = {row["source_id"]: row["home_key"] for row in rows(repository)}
    assert keys["m105"] == keys["1001"] == "unit:295|BUCHANAN ST|105"
    assert rows(repository, "id = ?", aged)[0]["status_reason"] == "user"


def test_copies_the_old_version_kept_apart_that_disagree_keep_the_star(tmp_path: Path) -> None:
    """Joined into one home by the migration, a copy starred and a copy
    passed cannot say which came later. The star wins: it hides nothing."""
    path = tmp_path / "housing.sqlite3"
    _pre_wo3_board(path)
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE listings SET status = 'dismissed' WHERE source_id = '1001'")

    repository = Repository(path)
    repository.initialize()

    assert {(row["status"], row["status_reason"]) for row in rows(repository, "source_id IN ('1001', 'm105')")} == {
        ("saved", "user")
    }


def test_a_rescore_of_the_board_recomputes_which_rows_are_one_home(repository: Repository, preferences) -> None:
    """Keys are recomputed once for the whole board at the end of a rescore,
    not row by row -- which doubled a rescore of the real board to 30s."""
    from sf_housing.scanner import Scanner

    put(repository, zillow("1001", "295 Buchanan St #105"))
    put(repository, movoto("m105", "295 Buchanan St #105"))
    with repository.connection() as connection:
        connection.execute("UPDATE listings SET home_key = 'stale:' || id")
        connection.commit()

    Scanner(repository, lambda: preferences, [], detail_delay_seconds=0.0).rescore_all(preferences)

    assert {row["home_key"] for row in rows(repository)} == {"unit:295|BUCHANAN ST|105"}


# --------------------------------------------------------------------------
# found by the adversarial review of this change
# --------------------------------------------------------------------------


def test_passing_one_door_of_a_ranged_building_leaves_the_other_door(repository: Repository) -> None:
    lower = put(repository, redfin("r25", "25-27 Dore St #25", "25"))
    upper = put(repository, redfin("r27", "25-27 Dore St #27", "27"))
    repository.set_listing_status(lower, "dismissed")
    assert rows(repository, "id = ?", upper)[0]["status"] == "active"
    assert repository.elsewhere(upper)["copies"] == []


def _rentpath(platform: str, sid: str, address: str | None, **kwargs) -> ListingCandidate:
    url = (f"https://www.rent.com/r/x-{sid}" if platform == "Rent.com"
           else f"https://www.apartmentguide.com/rent/x-{sid}/")
    return card(platform, sid, url, address, **kwargs)


def test_an_id_re_let_to_another_flat_never_carries_a_star_through_its_twin(repository: Repository) -> None:
    """Rent.com lvABC and ApartmentGuide LVABC are one record. When Rent.com
    re-lets lvABC to another flat, the starred ApartmentGuide copy must not
    follow it -- or the new flat arrives starred and the note moves with it."""
    put(repository, _rentpath("Rent.com", "lvabc", "1114 Sutter St #503", price=2900))
    guide = put(repository, _rentpath("ApartmentGuide", "LVABC", None, price=None, unit_type=None))
    repository.set_listing_status(guide, "saved")
    repository.set_listing_note(guide, "great light, viewing Tuesday 6pm")

    put(repository, _rentpath("Rent.com", "lvabc", "900 Bush St #210", price=5100))

    new = rows(repository, "source_id = 'lvabc'")[0]
    assert (new["title"], new["status"]) == ("900 Bush St #210", "active"), "a flat the user never starred"
    kept = rows(repository, "id = ?", guide)[0]
    assert kept["home_key"] == "unit:1114|SUTTER ST|503"
    assert (kept["status"], kept["note"]) == ("saved", "great light, viewing Tuesday 6pm")


def test_a_zillow_alert_copy_never_carries_a_pass_onto_a_re_let_page(repository: Repository) -> None:
    put(repository, zillow("1001", "295 Buchanan St #105"))
    alert = put(repository, card("Zillow", "1001_zpid", "https://www.zillow.com/homedetails/1001_zpid/?alert=1",
                                 None, price=None))
    repository.set_listing_status(alert, "dismissed")

    put(repository, zillow("1001", "295 Buchanan St #402", price=7200))

    new = rows(repository, "source_id = '1001'")[0]
    assert (new["title"], new["status"]) == ("295 Buchanan St #402", "active")


def test_a_starred_building_card_whose_featured_unit_rotates_stays_one_row(repository: Repository) -> None:
    url = "https://www.apartmentlist.com/ca/san-francisco/405-laguna"
    listing_id = put(repository, card("Apartment List", "405-laguna", url, "405 Laguna St", price=2895))
    repository.set_listing_status(listing_id, "saved")
    for unit in ("5", "7", "9"):
        put(repository, card("Apartment List", "405-laguna", url, f"405 Laguna St #{unit}", price=2895))
    put(repository, card("Apartment List", "405-laguna", url, "405 Laguna St #11", price=2995))

    assert len(rows(repository)) == 1
    row = rows(repository)[0]
    assert (row["status"], row["price"]) == ("saved", 2995)


def test_a_starred_complex_card_now_describing_another_floorplan_is_left_alone(repository: Repository) -> None:
    """The same Rent.com id, once the starred one-bedroom has gone, describes
    a two-bedroom at $4,700."""
    url = "https://www.rent.com/apartment/1114-sutter-st-lc6589544"
    listing_id = put(repository, card("Rent.com", "lc6589544", url, "1114 Sutter St", price=3200))
    repository.set_listing_status(listing_id, "saved")
    repository.set_listing_note(listing_id, "toured the 1BR, applying Friday")

    put(repository, card("Rent.com", "lc6589544", url, "1114 Sutter St", price=4700, unit_type="two_bedroom",
                         summary="Two bedroom apartments at 1114 Sutter, 2 bed 1 bath."))

    starred = rows(repository, "id = ?", listing_id)[0]
    assert (starred["unit_type"], starred["price"], starred["status"], starred["note"]) == (
        "one_bedroom", 3200, "saved", "toured the 1BR, applying Friday"
    )
    assert len(rows(repository)) == 2


def test_taking_a_star_off_leaves_an_ordinary_home_that_ages_out(repository: Repository) -> None:
    listing_id = put(repository, zillow("1001", "295 Buchanan St #105"))
    repository.set_listing_status(listing_id, "saved")
    repository.set_listing_status(listing_id, "active")
    assert (rows(repository)[0]["status"], rows(repository)[0]["status_reason"]) == ("active", None)
    with repository.connection() as connection:
        connection.execute("UPDATE listings SET first_found = '2026-01-01T00:00:00+00:00'")
        connection.commit()
    assert repository.archive_stale_listings() == 1


def test_restoring_a_passed_home_is_a_decision_that_keeps_it(repository: Repository) -> None:
    listing_id = put(repository, zillow("1001", "295 Buchanan St #105"))
    repository.set_listing_status(listing_id, "dismissed")
    repository.set_listing_status(listing_id, "active")
    assert rows(repository)[0]["status_reason"] == "user"


def test_a_noted_copy_still_listed_outranks_a_passed_copy_when_they_join(tmp_path: Path) -> None:
    """Before this version the only way to hide a duplicate was to pass it:
    the user passed the Zillow copy and wrote "viewing Tuesday" on Movoto's.
    Joined into one home, the home the user is viewing must stay listed."""
    path = tmp_path / "housing.sqlite3"
    _pre_wo3_board(path)
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE listings SET status = 'dismissed' WHERE source_id = '1001'")
        connection.execute("UPDATE listings SET status = 'active', note = 'viewing Tuesday 6pm' WHERE source_id = 'm105'")

    repository = Repository(path)
    repository.initialize()

    by_sid = {row["source_id"]: row for row in rows(repository)}
    assert by_sid["m105"]["status"] == by_sid["1001"]["status"] == "active"
    assert by_sid["m105"]["note"] == "viewing Tuesday 6pm"


def test_a_teaser_rent_cannot_lift_a_home_when_the_dearer_copy_is_outside_the_deal(repository: Repository) -> None:
    put(repository, zillow("1001", "295 Buchanan St #105", price=3000))
    put(repository, movoto("m105", "295 Buchanan St #105", price=3700))
    with repository.connection() as connection:
        connection.execute("UPDATE listings SET score = 85 WHERE source_id = '1001'")
        connection.execute("UPDATE listings SET score = 20, eligibility = 'ineligible' WHERE source_id = 'm105'")
        connection.commit()
    other = put(repository, zillow("2002", "10 Oak St #3"))

    order = [row["id"] for row in repository.query_listings(housing_kind="whole_unit", minimum_score=60)]

    assert order[0] == other


def test_a_home_is_on_one_tab_even_when_its_copies_disagree_about_its_size(repository: Repository) -> None:
    put(repository, zillow("1001", "295 Buchanan St #105"))
    put(repository, movoto("m105", "295 Buchanan St #105", unit_type=None))
    with repository.connection() as connection:
        connection.execute("UPDATE listings SET unit_type = NULL WHERE source_id = 'm105'")
        connection.commit()
    tabs = (False, ("one_bedroom",))
    one_bed = repository.query_listings(minimum_score=60, housing_kind="whole_unit", unit_types=("one_bedroom",))
    other = repository.query_listings(minimum_score=60, housing_kind="other", outside_tabs=tabs)
    assert len(one_bed) == 1 and other == []


def test_a_row_with_a_malformed_date_is_left_alone_even_beside_an_old_copy(repository: Repository) -> None:
    put(repository, zillow("1001", "295 Buchanan St #105"))
    odd = put(repository, movoto("m105", "295 Buchanan St #105"))
    with repository.connection() as connection:
        connection.execute("UPDATE listings SET first_found = '2026-01-01T00:00:00+00:00' WHERE source_id = '1001'")
        connection.execute("UPDATE listings SET first_found = 'not a date' WHERE id = ?", (odd,))
        connection.commit()
    assert repository.archive_stale_listings() == 1
    assert rows(repository, "id = ?", odd)[0]["status"] == "active"


def test_re_keying_the_whole_board_never_moves_a_starred_twin(repository: Repository) -> None:
    """The same rule as a single write, applied by the pass that re-keys the
    whole board after a rescore."""
    put(repository, _rentpath("Rent.com", "lvabc", "1114 Sutter St #503", price=2900))
    guide = put(repository, _rentpath("ApartmentGuide", "LVABC", None, price=None, unit_type=None))
    repository.set_listing_status(guide, "saved")
    put(repository, _rentpath("Rent.com", "lvabc", "900 Bush St #210", price=5100))

    with repository.connection() as connection:
        repository.refresh_identities(connection)

    assert rows(repository, "id = ?", guide)[0]["home_key"] == "unit:1114|SUTTER ST|503"
    assert rows(repository, "source_id = 'lvabc'")[0]["status"] == "active"


def test_a_passed_record_joins_its_twin_when_the_twin_first_names_the_address(repository: Repository) -> None:
    """The other half of the re-let rule: an ApartmentGuide record the user
    passed while it named no address is still that record when its Rent.com
    twin arrives naming one -- or the passed home comes back from Rent.com."""
    guide = put(repository, _rentpath("ApartmentGuide", "LVXYZ", None, price=None, unit_type=None))
    repository.set_listing_status(guide, "dismissed")

    twin = put(repository, _rentpath("Rent.com", "lvxyz", "108 Langton St unit B", price=3750))

    assert rows(repository, "id = ?", twin)[0]["status"] == "dismissed"
    assert rows(repository, "id = ?", guide)[0]["home_key"] == "unit:108|LANGTON ST|B"
    assert repository.query_listings(housing_kind="whole_unit", minimum_score=0) == []


def test_a_re_let_id_never_takes_a_starred_twin_that_was_only_its_id(repository: Repository) -> None:
    """Neither copy named a unit, so both were keyed by the record's id. When
    Rent.com re-lets the id to another flat, the starred ApartmentGuide copy
    stays with the home it was starred as."""
    put(repository, _rentpath("Rent.com", "lvabc", "1114 Sutter St", price=2900))
    guide = put(repository, _rentpath("ApartmentGuide", "LVABC", None, price=None, unit_type=None))
    assert rows(repository, "id = ?", guide)[0]["home_key"] == "rentpath:lvabc"
    repository.set_listing_status(guide, "saved")

    put(repository, _rentpath("Rent.com", "lvabc", "900 Bush St #210", price=5100))

    new = rows(repository, "source_id = 'lvabc'")[0]
    kept = rows(repository, "id = ?", guide)[0]
    assert new["status"] == "active"
    # Still one home with the Rent.com row that was set aside -- the flat the
    # user starred -- and never the re-let flat's.
    assert kept["home_key"] == rows(repository, "source_id LIKE 'lvabc#%'")[0]["home_key"] != new["home_key"]


def test_a_board_busy_through_start_up_is_reported_busy_not_damaged(tmp_path: Path, monkeypatch) -> None:
    """The migration takes the write lock. A scan holding it the whole time
    must not send the reader to Repair, which restores a backup over a board
    that was never damaged."""
    from sf_housing.database import DatabaseBusyError, DatabaseUnreadableError

    path = tmp_path / "housing.sqlite3"
    _pre_wo3_board(path)
    holder = sqlite3.connect(path)
    holder.execute("BEGIN IMMEDIATE")
    try:
        repository = Repository(path)
        monkeypatch.setattr(repository, "BUSY_SECONDS", 0.05)
        with pytest.raises(DatabaseBusyError) as raised:
            repository.initialize()
        assert not isinstance(raised.value, DatabaseUnreadableError)
        assert "Repair" not in str(raised.value)
    finally:
        holder.rollback()
        holder.close()
    Repository(path).initialize()
    assert rows(Repository(path), "source_id = 'm105'")[0]["status"] == "saved", "and it migrates once free"


def test_a_starred_twin_follows_its_record_when_the_rent_changes(repository: Repository) -> None:
    """The same record moving is not a re-let: when Rent.com's card for the
    complex changes its rent, the starred ApartmentGuide copy of it -- which
    names no address of its own -- stays one home with it."""
    url = "https://www.rent.com/apartment/1114-sutter-st-lc6589544"
    put(repository, card("Rent.com", "lc6589544", url, "1114 Sutter St", price=2895))
    guide = put(repository, card("ApartmentGuide", "6589544", "https://www.apartmentguide.com/a/x-6589544/",
                                 None, price=None))
    assert rows(repository, "id = ?", guide)[0]["home_key"] == "offer:1114|SUTTER ST|one_bedroom|2895"
    repository.set_listing_status(guide, "saved")

    put(repository, card("Rent.com", "lc6589544", url, "1114 Sutter St", price=2995))

    keys = {row["home_key"] for row in rows(repository)}
    assert keys == {"offer:1114|SUTTER ST|one_bedroom|2995"}
    assert {row["status"] for row in rows(repository)} == {"saved"}
    with repository.connection() as connection:
        repository.refresh_identities(connection)
    assert {row["home_key"] for row in rows(repository)} == keys


def test_a_starred_twin_written_again_after_a_re_let_stays_with_its_home(repository: Repository) -> None:
    """The next ApartmentGuide card for the starred record -- still naming no
    address -- must not adopt the flat Rent.com re-let the id to."""
    put(repository, _rentpath("Rent.com", "lvabc", "1114 Sutter St #503", price=2900))
    guide = put(repository, _rentpath("ApartmentGuide", "LVABC", None, price=None, unit_type=None))
    repository.set_listing_status(guide, "saved")
    put(repository, _rentpath("Rent.com", "lvabc", "900 Bush St #210", price=5100))

    put(repository, _rentpath("ApartmentGuide", "LVABC", None, price=None, unit_type=None))

    assert rows(repository, "id = ?", guide)[0]["home_key"] == "unit:1114|SUTTER ST|503"
    assert rows(repository, "source_id = 'lvabc'")[0]["status"] == "active"


def test_a_starred_row_that_never_named_an_address_is_not_rewritten_by_one_that_does(
    repository: Repository,
) -> None:
    """An alert-email card names no address. The same page later naming one
    may be the page re-let to another flat: the user's row is left alone."""
    url = "https://www.zillow.com/homedetails/555_zpid/"
    stored = put(repository, card("Zillow", "555_zpid", url, None, price=3200, title="A Zillow alert"))
    repository.set_listing_status(stored, "saved")

    put(repository, card("Zillow", "555_zpid", url, "409 Laguna St #4C", price=7200))

    kept = rows(repository, "id = ?", stored)[0]
    assert (kept["title"], kept["price"], kept["status"]) == ("A Zillow alert", 3200, "saved")


def test_a_note_that_kept_a_home_listed_is_not_a_permanent_decision(tmp_path: Path) -> None:
    path = tmp_path / "housing.sqlite3"
    _pre_wo3_board(path)
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE listings SET status = 'dismissed' WHERE source_id = '1001'")
        connection.execute("UPDATE listings SET status = 'active', note = 'viewing Tuesday' WHERE source_id = 'm105'")
        connection.execute("UPDATE listings SET first_found = '2026-01-01T00:00:00+00:00'")
    repository = Repository(path)
    repository.initialize()
    noted = rows(repository, "source_id = 'm105'")[0]["id"]
    assert {row["status_reason"] for row in rows(repository, "source_id IN ('1001', 'm105')")} == {None}

    repository.set_listing_note(noted, "")
    repository.archive_stale_listings()

    assert {row["status"] for row in rows(repository, "source_id IN ('1001', 'm105')")} == {"dismissed"}


def test_the_price_order_uses_a_home_s_dearest_quote(repository: Repository) -> None:
    # Movoto's copy is the one the home is shown by, and it quotes the teaser.
    put(repository, movoto("m105", "295 Buchanan St #105", price=1900))
    put(repository, zillow("1001", "295 Buchanan St #105", price=2600))
    single = put(repository, zillow("2002", "10 Oak St #3", price=2200))
    shown = repository.query_listings(housing_kind="whole_unit", minimum_score=60, sort="price")
    assert {row["price"] for row in shown} == {1900, 2200}

    order = [row["id"] for row in repository.query_listings(housing_kind="whole_unit", minimum_score=60, sort="price")]

    assert order[0] == single


def test_other_doors_of_a_ranged_building_are_other_homes_at_the_address(repository: Repository) -> None:
    lower = put(repository, redfin("r25", "25-27 Dore St #25", "25"))
    put(repository, card("AvalonBay", "a27", "https://www.avaloncommunities.com/unit/a27", "25-27 Dore St #27"))
    assert repository.elsewhere(lower) == {"copies": [], "building_sources": ["AvalonBay"]}


def test_a_home_held_together_by_a_re_let_id_keeps_the_user_s_decision_on_later_scans(
    repository: Repository,
) -> None:
    """Both feeds name only the building, so the home is held together by the
    record id alone. Rent.com re-lets the id; then, on the next scan,
    ApartmentGuide's card for the record arrives unchanged. The starred home
    stays the flat that was starred, and the re-let flat -- even one that
    names no unit, and so takes the same id key -- is never starred."""
    put(repository, _rentpath("Rent.com", "lvabc", "1114 Sutter St", price=2900))
    guide = put(repository, _rentpath("ApartmentGuide", "LVABC", None, price=None, unit_type=None))
    repository.set_listing_status(guide, "saved")
    repository.set_listing_note(guide, "great light, viewing Tuesday 6pm")

    put(repository, _rentpath("Rent.com", "lvabc", "900 Bush St", price=5100))
    put(repository, _rentpath("ApartmentGuide", "LVABC", None, price=None, unit_type=None))
    put(repository, _rentpath("Rent.com", "lvabc", "900 Bush St", price=5100))

    new = rows(repository, "source_id = 'lvabc'")[0]
    kept = rows(repository, "id = ?", guide)[0]
    assert new["status"] == "active", "the re-let flat was never starred"
    assert kept["status"] == "saved" and kept["note"] == "great light, viewing Tuesday 6pm"
    assert kept["home_key"] != new["home_key"]
    old = rows(repository, "source_id LIKE 'lvabc#%'")[0]
    assert old["home_key"] == kept["home_key"], "the flat that was starred is still one home"
    with repository.connection() as connection:
        repository.refresh_identities(connection)
    assert rows(repository, "id = ?", guide)[0]["home_key"] == kept["home_key"]


def test_a_zumper_card_that_named_no_street_is_not_rewritten_by_the_flat_the_id_is_re_let_to(
    repository: Repository,
) -> None:
    """Zumper stores an empty address when its data has no street, and its
    URLs name a size and a neighbourhood, not the flat."""
    url = "https://www.zumper.com/listings/29583305p/1-bedroom-dogpatch-san-francisco-ca"
    stored = put(repository, card("Zumper", "29583305", url, "", price=3200, title="1 bedroom Dogpatch"))
    repository.set_listing_status(stored, "saved")

    put(repository, card("Zumper", "29583305", url, "409 Laguna St #4C", price=7200))

    kept = rows(repository, "id = ?", stored)[0]
    assert (kept["title"], kept["price"], kept["status"]) == ("1 bedroom Dogpatch", 3200, "saved")
    assert len(rows(repository)) == 2


def test_a_copy_set_aside_by_a_re_let_stays_a_copy_of_its_home(repository: Repository) -> None:
    """Nobody starred anything. Rent.com's row named no unit and was one home
    with ApartmentGuide's page for #503 through their shared record; the id is
    re-let. The row set aside is still that flat's listing, so the flat stays
    one row on the page rather than two."""
    put(repository, _rentpath("Rent.com", "lvabc", "1114 Sutter St", price=2900))
    put(repository, _rentpath("ApartmentGuide", "LVABC", "1114 Sutter St #503", price=2900))
    put(repository, _rentpath("Rent.com", "lvabc", "900 Bush St #210", price=5100))

    homes = [row["title"] for row in repository.query_listings(housing_kind="whole_unit", minimum_score=60)]
    archive = [row["title"] for row in repository.query_listings(housing_kind="", view="all")]

    assert sorted(homes) == ["1114 Sutter St #503", "900 Bush St #210"]
    # Even where every copy is listed, the set-aside row is a copy of its home.
    assert sorted(archive) == ["1114 Sutter St #503", "900 Bush St #210"]


def test_a_flat_whose_page_is_re_let_leaves_the_shortlist(repository: Repository) -> None:
    """Zumper's page for 3150 18th St #2 now lists 3152 18th St #7. The old
    flat is gone from it: it must not stay on the board as a live home for
    somebody to email the landlord about."""
    url = "https://www.zumper.com/listings/29583305p/1-bedroom-mission"
    old = put(repository, card("Zumper", "29583305", url, "3150 18th St #2", price=2800))

    put(repository, card("Zumper", "29583305", url, "3152 18th St #7", price=2700))

    gone = rows(repository, "id = ?", old)[0]
    assert json.loads(gone["metadata_json"])["verified_inactive"] is True
    assert gone["eligibility"] == "ineligible" and gone["score"] <= 49
    titles = [row["title"] for row in repository.query_listings(housing_kind="whole_unit", minimum_score=60)]
    assert titles == ["3152 18th St #7"]


def test_a_starred_home_whose_page_shows_an_unreadable_address_stays_one_home(repository: Repository) -> None:
    stored = put(repository, zillow("111", "2400 Mission St #5", price=2600))
    repository.set_listing_status(stored, "saved")

    put(repository, zillow("111", "Address not disclosed", price=2550))

    assert len(rows(repository)) == 1
    row = rows(repository)[0]
    assert (row["status"], row["price"]) == ("saved", 2550)


def test_a_starred_record_with_no_key_at_all_never_follows_a_re_let_id(repository: Repository) -> None:
    """Complex cards that state no size earn no key of any kind, so there is
    nothing to pin. The re-let flat's card, which does state one, must still
    not take the starred ApartmentGuide copy with it."""
    sizeless = "Apartments to let at this complex."
    put(repository, card("Rent.com", "lc123", "https://www.rent.com/apartment/lc123", None, price=None,
                         unit_type=None, summary=sizeless))
    guide = put(repository, card("ApartmentGuide", "123", "https://www.apartmentguide.com/a/x-123/", None,
                                 price=None, unit_type=None, summary=sizeless))
    assert rows(repository, "id = ?", guide)[0]["home_key"] == ""
    repository.set_listing_status(guide, "saved")

    put(repository, card("Rent.com", "lc123", "https://www.rent.com/apartment/lc123", "900 Bush St", price=4100))

    assert rows(repository, "id = ?", guide)[0]["home_key"] == ""
    assert rows(repository, "source_id = 'lc123'")[0]["status"] == "active"
