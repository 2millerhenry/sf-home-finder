"""Opening the app must not re-rank a board that cannot have changed.

``create_app`` re-scored every stored listing before the server bound its port,
on every service start -- a login, an automatic upgrade, a restart after a
crash -- and not only after somebody changed their deal, despite the log line
saying otherwise. On the author's own board of 8,680 homes that is twelve
seconds on an idle machine and closer to fifty on a busy one, with the dashboard
answering nothing throughout; the Open script's own timeout was short enough
that it told people the app had failed while it was working.

These are the end-to-end halves of the fix: a second start with the same deal
and the same code writes no scores at all, and a start after a real change still
writes every one of them.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from sf_housing.app import create_app
from sf_housing.database import Repository
from sf_housing.models import ListingCandidate
from sf_housing.preferences import parse_preferences
from sf_housing.scoring import score_listing
from sf_housing.settings import Settings
from tests.conftest import TEST_PREFERENCES


def _settings(tmp_path: Path, document: str = TEST_PREFERENCES) -> Settings:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    preferences_path = tmp_path / "preferences.yaml"
    preferences_path.write_text(document, encoding="utf-8")
    return Settings(
        data_dir=data_dir,
        preferences_path=preferences_path,
        database_path=data_dir / "housing.sqlite3",
        log_path=data_dir / "test.log",
    )


def _stock_the_board(settings: Settings, count: int = 5) -> None:
    repository = Repository(settings.database_path)
    repository.initialize()
    preferences = parse_preferences(settings.preferences_path.read_text(encoding="utf-8"))
    for index in range(count):
        listing = ListingCandidate(
            platform="Craigslist",
            source_id=f"startup-{index}",
            title=f"Sunny one-bedroom number {index} in NOPA",
            original_url=f"https://sfbay.craigslist.org/startup-{index}.html",
            price=2800 + index,
            neighborhood="NOPA",
            summary="An entire one-bedroom with laundry in the building.",
            listing_type="Apartment",
        )
        repository.upsert_listing(listing, score_listing(listing, preferences))


@pytest.fixture
def count_score_writes(monkeypatch: pytest.MonkeyPatch):
    """Count every score actually written, wherever it is written from."""
    written: list[int] = []
    real = Repository.update_score

    def counting(self, listing_id, *args, **kwargs):
        written.append(listing_id)
        return real(self, listing_id, *args, **kwargs)

    monkeypatch.setattr(Repository, "update_score", counting)
    return written


def test_the_first_start_scores_the_board(tmp_path: Path, count_score_writes) -> None:
    """Nothing is marked yet, so the work is owed and must happen."""
    settings = _settings(tmp_path)
    _stock_the_board(settings)
    count_score_writes.clear()

    with TestClient(create_app(settings=settings, sources=[], enable_scheduler=False)):
        pass

    assert len(count_score_writes) == 5, "the first start did not score the board"
    assert Repository(settings.database_path).scoring_mark() is not None


def test_starting_again_with_the_same_deal_scores_nothing(
    tmp_path: Path, count_score_writes
) -> None:
    """The whole point: a restart cannot change a score, so it must not spend time proving it."""
    settings = _settings(tmp_path)
    _stock_the_board(settings)

    with TestClient(create_app(settings=settings, sources=[], enable_scheduler=False)):
        pass
    count_score_writes.clear()

    with TestClient(create_app(settings=settings, sources=[], enable_scheduler=False)):
        pass

    assert count_score_writes == [], (
        f"a restart rewrote {len(count_score_writes)} scores that could not have changed"
    )


def test_a_changed_deal_scores_the_board_again(tmp_path: Path, count_score_writes) -> None:
    settings = _settings(tmp_path)
    _stock_the_board(settings)
    with TestClient(create_app(settings=settings, sources=[], enable_scheduler=False)):
        pass
    count_score_writes.clear()

    document = yaml.safe_load(TEST_PREFERENCES)
    document["budget"]["max_monthly"] = int(document["budget"]["max_monthly"]) + 700
    settings.preferences_path.write_text(yaml.safe_dump(document), encoding="utf-8")

    with TestClient(create_app(settings=settings, sources=[], enable_scheduler=False)):
        pass

    assert len(count_score_writes) == 5, "a changed deal did not rescore the board"


def test_a_mark_from_another_board_makes_the_next_start_score(
    tmp_path: Path, count_score_writes
) -> None:
    """A mark that does not match this code and this deal is not a mark at all."""
    settings = _settings(tmp_path)
    _stock_the_board(settings)
    with TestClient(create_app(settings=settings, sources=[], enable_scheduler=False)):
        pass

    board = Repository(settings.database_path)
    with board.connection() as connection:
        board.set_scoring_mark(connection, "a mark from some other board")
        connection.commit()
    count_score_writes.clear()

    with TestClient(create_app(settings=settings, sources=[], enable_scheduler=False)):
        pass

    assert len(count_score_writes) == 5


def test_a_draft_deal_starts_exactly_as_it_did_before(
    tmp_path: Path, count_score_writes
) -> None:
    """Nothing is scored before the deal is finished, and nothing is marked.

    The pass owed the moment the deal goes active must still happen, so this
    also proves the draft start left nothing behind that could skip it.
    """
    document = yaml.safe_load(TEST_PREFERENCES)
    document["profile_version"] = 1
    document["profile"] = {"state": "draft", "enabled_paths": [], "budgets": {}}
    settings = _settings(tmp_path, yaml.safe_dump(document))
    _stock_the_board(settings)
    count_score_writes.clear()

    with TestClient(create_app(settings=settings, sources=[], enable_scheduler=False)):
        pass

    assert count_score_writes == [], "a draft deal scored a board it has no answers for"
    assert Repository(settings.database_path).scoring_mark() is None, "a draft deal left a mark it cannot vouch for"


MINIMAL_DEAL = {
    "housing_paths": "private_room",
    "private_room_maximum": "2000",
    "anywhere_in_sf": "on",
    "move_in_flexible": "on",
}

BLANK_PROFILE = """profile_version: 1
profile:
  state: draft
  enabled_paths: []
  budgets: {}
technical: {}
"""


def test_saving_a_deal_through_the_page_rescores_and_the_next_start_trusts_it(
    tmp_path: Path, count_score_writes
) -> None:
    """The real route, end to end: save a deal, restart, restart after a change.

    Everything else here edits the preferences file and restarts, which proves
    the start-up half. This is the other half, through the form a person
    actually submits: a save must still re-rank the whole board, must leave a
    mark the next start trusts, and a second save with different answers must
    re-rank again and move the mark with it.
    """
    settings = _settings(tmp_path, BLANK_PROFILE)
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    application.state.scanner.start_scan = lambda *args, **kwargs: True  # no network
    with TestClient(application) as client:
        response = client.post("/preferences/deal", data=MINIMAL_DEAL, follow_redirects=False)
    assert response.status_code == 303, response.text

    _stock_the_board(settings)
    board = Repository(settings.database_path)

    # First save of a real deal: the board is ranked and marked.
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    application.state.scanner.start_scan = lambda *args, **kwargs: True
    with TestClient(application) as client:
        count_score_writes.clear()
        response = client.post("/preferences/deal", data=MINIMAL_DEAL, follow_redirects=False)
    assert response.status_code == 303, response.text
    assert len(count_score_writes) == 5, "saving the deal did not re-rank the board"
    first_mark = board.scoring_mark()
    assert first_mark is not None, "saving the deal left no mark"

    # A restart with the same deal trusts that mark.
    count_score_writes.clear()
    with TestClient(create_app(settings=settings, sources=[], enable_scheduler=False)):
        pass
    assert count_score_writes == [], "a restart after a save re-ranked the board anyway"

    # A save with different answers re-ranks again and moves the mark.
    cheaper = dict(MINIMAL_DEAL, private_room_maximum="1400")
    application = create_app(settings=settings, sources=[], enable_scheduler=False)
    application.state.scanner.start_scan = lambda *args, **kwargs: True
    with TestClient(application) as client:
        count_score_writes.clear()
        response = client.post("/preferences/deal", data=cheaper, follow_redirects=False)
    assert response.status_code == 303, response.text
    assert len(count_score_writes) == 5, "a changed deal was saved without re-ranking"
    assert board.scoring_mark() not in (None, first_mark), "the mark did not follow the new deal"


def test_a_skipped_start_says_so_in_the_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The skip must be visible to whoever reads the log after a complaint.

    Otherwise "why is my shortlist ranked like that" starts with a question the
    log cannot answer: did this start re-rank the board, or trust the mark it
    already carried?
    """
    import logging

    settings = _settings(tmp_path)
    _stock_the_board(settings)
    with TestClient(create_app(settings=settings, sources=[], enable_scheduler=False)):
        pass

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="sf_housing.app"):
        with TestClient(create_app(settings=settings, sources=[], enable_scheduler=False)):
            pass

    assert any("re-ranking skipped" in record.getMessage() for record in caplog.records), (
        "a start that trusted the mark left no trace of having done so"
    )
