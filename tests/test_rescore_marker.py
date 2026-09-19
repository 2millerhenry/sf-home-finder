"""Start-up must not re-rank a board that cannot have changed, and must re-rank one that can.

Opening the app re-scored every stored listing before the server bound its port:
on the author's own board of 8,680 homes that is twelve seconds idle and closer
to fifty on a busy machine, and it ran on every service start rather than only
after somebody changed their deal. Nothing answered until it finished.

Scoring is a pure function of a listing and a set of preferences, so a board
already scored by this code with these answers cannot score differently now.
These tests pin both halves of that: the pass is skipped when, and only when,
skipping it cannot change what anybody sees. Every uncertainty -- no mark, a
damaged mark, an unwritable disk, a source tree that cannot be hashed -- has to
resolve to scoring again, because a slow start-up is the cost of being wrong in
that direction and a stale shortlist is the cost of being wrong in the other.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
import yaml

from sf_housing import rescore_marker
from sf_housing.database import Repository
from sf_housing.models import ListingCandidate
from sf_housing.preferences import Preferences, parse_preferences
from sf_housing.rescore_marker import (
    SCORING_MODULES,
    current_fingerprint,
    deal_fingerprint,
    rescore_is_needed,
    scoring_fingerprint,
)
from sf_housing.scanner import Scanner
from sf_housing.scoring import score_listing
from tests.conftest import TEST_PREFERENCES


def _board(repository: Repository, preferences: Preferences, count: int = 3) -> None:
    for index in range(count):
        listing = ListingCandidate(
            platform="Craigslist",
            source_id=f"marker-{index}",
            title=f"Sunny one-bedroom number {index} in NOPA",
            original_url=f"https://sfbay.craigslist.org/marker-{index}.html",
            price=2800 + index,
            neighborhood="NOPA",
            summary="An entire one-bedroom with laundry in the building.",
            listing_type="Apartment",
        )
        repository.upsert_listing(listing, score_listing(listing, preferences))


def _mark(repository: Repository, preferences: Preferences) -> None:
    """Record a pass without running one, for tests about the mark itself."""
    with repository.connection() as connection:
        repository.set_scoring_mark(connection, current_fingerprint(preferences))
        connection.commit()


def _scanner(repository: Repository, preferences: Preferences) -> Scanner:
    return Scanner(repository=repository, preference_loader=lambda: preferences, sources=[])


# --------------------------------------------------------------------------
# What the mark is keyed on
# --------------------------------------------------------------------------


def test_the_mark_covers_the_whole_of_what_scoring_reads() -> None:
    """Keyed on the deal profile alone, a changed weight kept every stale score.

    Scoring reads ``preferences.section(...)`` and ``preferences.weights``, and
    those come from ``legacy_view(profile, technical_settings(document))`` -- so
    the technical block reaches them too. A fingerprint that watched only the
    profile would answer "nothing has changed" to somebody who had just changed
    how much a feature is worth.
    """
    document = yaml.safe_load(TEST_PREFERENCES)
    before = parse_preferences(yaml.safe_dump(document))

    louder = yaml.safe_load(TEST_PREFERENCES)
    louder.setdefault("technical", {})["minimum_score"] = 42
    after = parse_preferences(yaml.safe_dump(louder))

    assert before.deal_profile == after.deal_profile, "the deal itself is unchanged"
    assert deal_fingerprint(before) != deal_fingerprint(after), (
        "a technical setting that reaches scoring did not reach the fingerprint"
    )


def test_the_listed_scoring_modules_are_the_real_import_closure() -> None:
    """Adding an import to scoring must not quietly widen what can change a score.

    The fingerprint hashes a named list of modules. If ``scoring`` grows a
    dependency that is not on it, a change in that dependency would move every
    score while the mark said nothing had happened. Rather than trusting the
    list, recompute it from the source and compare.
    """
    package = Path(rescore_marker.__file__).resolve().parent
    reached: set[str] = set()
    pending = ["scoring.py"]
    while pending:
        name = pending.pop()
        if name in reached:
            continue
        reached.add(name)
        tree = ast.parse((package / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
                candidate = f"{node.module}.py"
                if (package / candidate).exists():
                    pending.append(candidate)

    assert reached <= set(SCORING_MODULES), (
        "scoring.py now reaches a module the fingerprint does not watch: "
        f"{sorted(reached - set(SCORING_MODULES))}"
    )
    # The other way round is a separate question: sources.py is on the list for
    # a reason scoring's imports cannot show, so only the modules that are on it
    # for *no* reason are stale.
    extra = set(SCORING_MODULES) - reached - {"sources.py", "database.py"}
    assert not extra, f"SCORING_MODULES carries modules nothing reaches: {sorted(extra)}"


def test_the_helpers_a_pass_applies_before_scoring_are_covered_too() -> None:
    """A pass is not only ``score_listing``; it normalises two areas first.

    ``Scanner._rescore`` rewrites a Facebook Marketplace listing's neighbourhood
    through ``facebook_coordinate_neighborhood`` and backfills a Furnished
    Finder one through ``visible_sf_area_hint`` before scoring either. Both live
    in ``sources.py``, so a change to either moves the scores a pass would
    write while every module the fingerprint watched stayed untouched -- and the
    mark would go on saying the board was current. Both are self-contained in
    that file, so hashing it covers them.
    """
    package = Path(rescore_marker.__file__).resolve().parent
    tree = ast.parse((package / "scanner.py").read_text(encoding="utf-8"))

    origins = {
        alias.asname or alias.name: f"{node.module}.py"
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module
        for alias in node.names
    }
    rescore = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_rescore"
    )
    called = {
        node.func.id
        for node in ast.walk(rescore)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    used_from_the_package = {origins[name] for name in called if name in origins}

    assert used_from_the_package, "the scan of _rescore found nothing; the parsing above is wrong"
    assert used_from_the_package <= set(SCORING_MODULES), (
        "a pass applies helpers from a module the fingerprint does not watch: "
        f"{sorted(used_from_the_package - set(SCORING_MODULES))}"
    )


def test_editing_any_scoring_module_invalidates_every_stored_mark(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scoring change must not leave the whole world on yesterday's scores.

    This is the failure the fingerprint exists to prevent, and the reason it is
    derived from the source rather than declared as a number somebody has to
    remember to bump: forgetting would be completely silent.
    """
    preferences = parse_preferences(TEST_PREFERENCES)
    _mark(repository, preferences)
    assert not rescore_is_needed(repository, preferences)

    for module in SCORING_MODULES:
        real = Path(rescore_marker._PACKAGE_ROOT / module).read_bytes()

        def edited(self: Path, _module=module, _real=real) -> bytes:
            return _real + b"\n# a change that moves scores\n" if self.name == _module else _real

        monkeypatch.setattr(Path, "read_bytes", edited)
        assert rescore_is_needed(repository, preferences), (
            f"a change to {module} left the stored mark looking current"
        )
        monkeypatch.undo()


def test_a_source_tree_that_cannot_be_hashed_means_score_again(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unknowable is not the same as unchanged."""
    preferences = parse_preferences(TEST_PREFERENCES)
    _mark(repository, preferences)
    assert not rescore_is_needed(repository, preferences)

    def unreadable(self: Path) -> bytes:
        raise OSError("no source here")

    monkeypatch.setattr(Path, "read_bytes", unreadable)
    assert scoring_fingerprint() is None
    assert current_fingerprint(preferences) is None
    assert rescore_is_needed(repository, preferences)


# --------------------------------------------------------------------------
# Damage, absence and disks that will not take a write
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# The pass itself
# --------------------------------------------------------------------------


def test_a_completed_pass_earns_a_mark_and_a_second_one_is_not_owed(
    repository: Repository, preferences: Preferences, tmp_path: Path
) -> None:
    _board(repository, preferences)
    scanner = _scanner(repository, preferences)

    assert rescore_is_needed(repository, preferences)
    assert scanner.rescore_all(preferences) == 3
    assert not rescore_is_needed(repository, preferences)


def test_a_pass_that_fails_part_way_leaves_no_mark(
    repository: Repository, preferences: Preferences, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A half-scored board must be scored again, not vouched for.

    The mark is written on the same transaction as the scores, so a pass that
    dies between the first listing and the last commits neither. This kills the
    pass *inside* the loop, after any number of rows have been written and
    after the point a mark could have been recorded, because that is the moment
    the two could come apart.
    """
    _board(repository, preferences, count=6)
    scanner = _scanner(repository, preferences)
    real = Repository.update_score
    seen: list[int] = []

    def fail_on_the_fourth(self, listing_id, *args, **kwargs):
        seen.append(listing_id)
        if len(seen) == 4:
            raise RuntimeError("killed part way through the board")
        return real(self, listing_id, *args, **kwargs)

    monkeypatch.setattr(Repository, "update_score", fail_on_the_fourth)
    with pytest.raises(RuntimeError):
        scanner.rescore_all(preferences)
    monkeypatch.undo()

    assert len(seen) == 4, "the pass did not get far enough to be a partial one"
    assert repository.scoring_mark() is None, (
        "a board scored only part way through carries a mark vouching for all of it"
    )
    assert rescore_is_needed(repository, preferences)


def test_a_draft_deal_is_never_vouched_for(repository: Repository) -> None:
    """Nothing is scored before the deal is finished, so nothing may be marked.

    A mark here would let the pass owed the moment the deal goes active be
    skipped, and the first board somebody ever sees would be unranked.
    """
    document = yaml.safe_load(TEST_PREFERENCES)
    document["profile_version"] = 1
    document["profile"] = {"state": "draft", "enabled_paths": [], "budgets": {}}
    draft = parse_preferences(yaml.safe_dump(document))
    assert draft.profile_active is False, "the fixture has to actually be inactive"
    scanner = _scanner(repository, draft)

    assert scanner.rescore_all(draft) == 0
    assert repository.scoring_mark() is None


def test_changing_the_deal_owes_a_new_pass(
    repository: Repository, preferences: Preferences
) -> None:
    _board(repository, preferences)
    scanner = _scanner(repository, preferences)
    scanner.rescore_all(preferences)
    assert not rescore_is_needed(repository, preferences)

    # The legacy document TEST_PREFERENCES uses feeds scoring through the same
    # `Preferences.data`, so raising its ceiling is a real change of deal.
    document = yaml.safe_load(TEST_PREFERENCES)
    document["budget"]["max_monthly"] = int(document["budget"]["max_monthly"]) + 700
    changed = parse_preferences(yaml.safe_dump(document))
    assert changed.data != preferences.data, "the fixture has to actually differ"

    assert rescore_is_needed(repository, changed), "a changed budget did not owe a pass"
    _scanner(repository, changed).rescore_all(changed)
    assert not rescore_is_needed(repository, changed)
    assert rescore_is_needed(repository, preferences), "the old deal must not still be marked"




def test_installing_any_version_retracts_the_mark() -> None:
    """A version that predates the mark cannot maintain it, so install must retract it.

    Start-up trusts the mark to mean "this code and this deal already scored
    this board". Roll back to a release that has never heard of it, let that
    release re-score the board under its own rules -- which it does on every
    start, correctly, because that is what it has always done -- and come
    forward again: the board now holds that release's scores under this
    release's mark, and nothing in the app can tell.

    The installer is the one place both versions are guaranteed to run, so it
    is where the mark is retracted. The cost is one re-score per install
    against the one per launch this whole mechanism removes.
    """
    installer = (
        Path(rescore_marker.__file__).resolve().parent.parent
        / "release_assets"
        / "payload"
        / "install.sh"
    ).read_text(encoding="utf-8")

    assert "DELETE FROM scoring_state" in installer, (
        "the installer does not retract the mark, so a downgrade-then-upgrade "
        "leaves this version trusting another version's scores"
    )
    assert "rescore_fingerprint" in installer, "the retraction names the wrong row"
    # Never fatal: a failure here costs a slow first start, not an install.
    clear = installer[installer.index("DELETE FROM scoring_state") :]
    assert "|| true" in clear[: clear.index("\nfi")], (
        "a failure to retract the mark must not fail the install"
    )
    # After the old service is told to stop, never before. A running app
    # mid-scan holds the write lock; the delete would time out, and `|| true`
    # would swallow it, silently skipping the step that makes a downgrade safe.
    assert installer.index("launchctl bootout") < installer.index("DELETE FROM scoring_state"), (
        "the mark is retracted while the old app may still be holding the database"
    )
    # And before the new runtime is moved into place, while the Python it uses
    # is still where the retraction says it is.
    assert installer.index("DELETE FROM scoring_state") < installer.index('/bin/mv "$STAGE/runtime"'), (
        "the retraction runs after its own interpreter has been moved away"
    )
    retraction = installer[installer.rindex("sqlite3.connect", 0, installer.index("DELETE FROM scoring_state")) :]
    assert "timeout=30" in retraction[: retraction.index("DELETE FROM scoring_state")], (
        "a process still shutting down would outlast SQLite's five-second default"
    )


def test_a_pass_that_ran_beside_a_scan_does_not_claim_the_board(
    repository: Repository, preferences: Preferences
) -> None:
    """A scan in flight is still writing scores from the deal it started with.

    Saving a new deal re-scores everything the board holds *at that moment*. A
    scan already running loaded the old answers when it started and goes on
    writing rows scored against them, so the board ends up part new and part
    old -- and a mark claiming all of it would make that permanent, because the
    next start would skip the pass that would have fixed it.

    Reproduced before the fix: a room at $2,700 stored 48 from the old deal
    while the new one scores it 66, and ``rescore_is_needed`` answered False.
    """
    _board(repository, preferences)
    scanner = _scanner(repository, preferences)

    scanner._scan_lock.acquire()
    try:
        assert scanner.is_running, "the fixture has to actually look like a running scan"
        scanner.rescore_all(preferences)
    finally:
        scanner._scan_lock.release()

    assert repository.scoring_mark() is None, (
        "a pass that ran beside a scan claimed rows the scan may still overwrite"
    )
    assert rescore_is_needed(repository, preferences)


def test_a_pass_with_no_scan_running_does_claim_the_board(
    repository: Repository, preferences: Preferences
) -> None:
    """The guard above must not be so cautious that nothing is ever marked."""
    _board(repository, preferences)
    scanner = _scanner(repository, preferences)
    assert not scanner.is_running

    scanner.rescore_all(preferences)

    assert repository.scoring_mark() is not None
    assert not rescore_is_needed(repository, preferences)


def test_bumping_the_scoring_version_by_hand_invalidates_every_mark(
    repository: Repository, preferences: Preferences, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hand-bumped constant has to actually reach the fingerprint.

    It is the escape hatch for a change that moves scores without editing any
    watched module -- new reference data, say. Folded in but never tested, it
    could quietly stop being folded in and nothing would notice.
    """
    scanner = _scanner(repository, preferences)
    _board(repository, preferences)
    scanner.rescore_all(preferences)
    assert not rescore_is_needed(repository, preferences)

    monkeypatch.setattr(rescore_marker, "SCORING_VERSION", rescore_marker.SCORING_VERSION + 1)

    assert rescore_is_needed(repository, preferences)


def test_a_pass_retracts_the_old_mark_before_it_starts_rewriting(
    repository: Repository, preferences: Preferences, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A board being rewritten must not go on carrying the mark it had.

    Listings commit one at a time -- ``Repository._update_score`` ends with its
    own commit -- so a pass is not one transaction and a kill part way through
    leaves the board half rewritten. Whatever mark it was carrying described
    the board as it was before, so it is retracted before the first row moves
    and recorded again only once the last one has. Otherwise the guarantee
    holds by luck: it happens that a pass only runs when the mark already fails
    to match, and luck is not what the next reader of this should inherit.
    """
    _board(repository, preferences, count=6)
    scanner = _scanner(repository, preferences)
    scanner.rescore_all(preferences)
    assert repository.scoring_mark() is not None, "the fixture needs a mark to retract"

    real = Repository.update_score
    seen: list[int] = []

    def fail_on_the_fourth(self, listing_id, *args, **kwargs):
        seen.append(listing_id)
        if len(seen) == 4:
            raise RuntimeError("killed part way through the board")
        return real(self, listing_id, *args, **kwargs)

    monkeypatch.setattr(Repository, "update_score", fail_on_the_fourth)
    with pytest.raises(RuntimeError):
        scanner.rescore_all(preferences)
    monkeypatch.undo()

    assert repository.scoring_mark() is None, (
        "a board left half rewritten still carries the mark it had before"
    )
    assert rescore_is_needed(repository, preferences)


def test_a_deal_field_the_legacy_view_drops_still_owes_a_pass() -> None:
    """Scoring reads the canonical profile too, not only the view built from it.

    ``Preferences.data`` is ``legacy_view(profile, technical)``, and that view
    does not carry every field of the profile. ``scoring`` reads the profile
    directly in places -- ``preferences.deal_profile.budgets`` decides a
    canonical budget, ``anywhere_in_sf`` decides an area, ``enabled_paths``
    decides a path -- so a field the view drops can move a score while the view
    stays byte for byte identical.

    Measured against the author's own deal, seven fields do exactly that,
    ``ideal_monthly`` among them: somebody changing the price they would ideally
    pay would have kept every stale score, with nothing to say so.
    """
    document = yaml.safe_load(TEST_PREFERENCES)
    document["profile_version"] = 1
    document["profile"] = {
        "state": "active",
        "enabled_paths": ["one_bedroom"],
        "budgets": {"one_bedroom": {"maximum_monthly": 3000, "ideal_monthly": 2000}},
        "geography": {"anywhere_in_sf": True},
    }
    before = parse_preferences(yaml.safe_dump(document))

    document["profile"]["budgets"]["one_bedroom"]["ideal_monthly"] = 2600
    after = parse_preferences(yaml.safe_dump(document))

    assert before.data == after.data, "the fixture must be invisible to the legacy view"
    assert before.deal_profile != after.deal_profile, "the fixture must change the profile"
    assert deal_fingerprint(before) != deal_fingerprint(after), (
        "a change to the deal that scoring reads did not reach the fingerprint"
    )


def test_a_lane_still_running_also_stops_the_board_being_claimed(
    repository: Repository, preferences: Preferences
) -> None:
    """A scan's lane outlives the scan lock, and it is still a writer.

    ``is_running`` asks whether the scan lock is held, and a lane thread keeps
    storing listings after the main line has released it. So a deal saved in
    that window found ``is_running`` False, marked the board, and the lane then
    wrote homes scored against the deal that had just been replaced -- the same
    permanent staleness the running-scan guard exists to prevent, through a
    door the guard did not cover.
    """
    _board(repository, preferences)
    scanner = _scanner(repository, preferences)

    import threading

    still_going = threading.Event()
    lane = threading.Thread(target=still_going.wait, daemon=True)
    lane.start()
    scanner._lane_thread = lane
    try:
        assert not scanner.is_running, "the scan lock is free; only the lane is left"
        scanner.rescore_all(preferences)
        assert repository.scoring_mark() is None, (
            "the board was claimed while a lane was still storing listings"
        )
    finally:
        still_going.set()
        lane.join(timeout=5)

    # And once the lane is done, a pass may claim it again.
    scanner.rescore_all(preferences)
    assert repository.scoring_mark() is not None
    assert not rescore_is_needed(repository, preferences)


def test_a_scan_in_another_process_also_stops_the_board_being_claimed(
    repository: Repository, preferences: Preferences
) -> None:
    """The scan lock is a file precisely so another process can hold it.

    ``python -m sf_housing scan``, or a second server pointed at the same data
    directory, takes the cross-process file lock and never this process's
    ``_scan_lock`` -- so ``is_running`` answered False while that scan went on
    storing homes scored against whatever deal it loaded when it began. A deal
    saved here in that window would have claimed a board the other process was
    still writing old scores into. ``filelock.py`` says what the file is for:
    keeping scans from overlapping across processes. The guard has to honour
    the same boundary.
    """
    from sf_housing.filelock import release as release_file_lock, try_acquire

    _board(repository, preferences)
    scanner = _scanner(repository, preferences)

    # Another process's scan: a separate open file description holding the lock.
    lock_path = repository.path.parent / "scan.lock"
    other = lock_path.open("a+", encoding="utf-8")
    assert try_acquire(other), "the fixture could not take the file lock"
    try:
        assert not scanner.is_running, "only the other process is scanning"
        scanner.rescore_all(preferences)
        assert repository.scoring_mark() is None, (
            "the board was claimed while another process held the scan lock"
        )
    finally:
        release_file_lock(other)
        other.close()

    # With the other process gone, a pass may claim it.
    scanner.rescore_all(preferences)
    assert repository.scoring_mark() is not None


def test_the_repository_a_pass_writes_through_is_covered_too() -> None:
    """A pass writes through ``self.repository``, and that code decides what lands.

    ``Repository._update_score`` is not a passive writer: it classifies the
    listing again and merges the classified metadata into the row, and a later
    pass reads that metadata back -- ``verified_inactive`` caps a score at 49.
    So the module that defines the repository decides what a pass stores as
    surely as scoring does. The walk over ``_rescore`` above follows plain
    function calls; this follows the method calls made on the repository,
    which are the ones that would otherwise go unwatched.
    """
    package = Path(rescore_marker.__file__).resolve().parent
    tree = ast.parse((package / "scanner.py").read_text(encoding="utf-8"))
    rescore = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_rescore"
    )
    writes_through_the_repository = any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "repository"
        for node in ast.walk(rescore)
    )
    assert writes_through_the_repository, "the walk found no repository call; the parsing is wrong"

    defining_module = f"{Repository.__module__.rsplit('.', 1)[-1]}.py"
    assert defining_module in SCORING_MODULES, (
        f"a pass writes through {defining_module}, which the fingerprint does not watch"
    )


def test_a_deal_that_cannot_be_serialised_scores_again_rather_than_failing() -> None:
    """Unknowable must mean "score again", never "stop the app".

    ``sort_keys`` cannot order a mapping whose keys are of mixed types, and a
    preferences file can reach here having parsed, validated and scored
    perfectly well. Raising here would stop ``create_app`` before the server
    bound its port -- the dashboard would simply never open -- over a question
    whose safe answer is only a slower start.
    """
    preferences = parse_preferences(TEST_PREFERENCES)
    object.__setattr__(preferences, "data", {**preferences.data, 1: "an integer key beside string ones"})

    assert deal_fingerprint(preferences) is None
    assert current_fingerprint(preferences) is None
