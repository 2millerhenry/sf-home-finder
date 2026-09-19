"""The safety copies of somebody's search, and the way back to them.

``data/housing.sqlite3`` is the only copy of months of stars, notes and
dismissals. These tests hold ``sf_housing.backups`` -- which every installer and
both Repair scripts go through -- to the one rule that matters here: when in
doubt, keep the data.

Every failure that shaped the module is pinned by name below -- the three found
in the Repair it replaces (pruning that deleted the last good copy, a restore
that could leave no database at all, a damaged file's write-ahead log deleted)
and the ones an independent review then found in its first version (a burst of
copies of an emptied board pushing out every real one, a clock set back
pruning the copy just taken, damage SQLite words differently going unrecognised,
evidence used as a recovery point, and unbounded copies of the same damage).
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

import pytest

from sf_housing import backups
from sf_housing.backups import (
    DAMAGED,
    GOOD,
    UNKNOWN,
    BackupError,
    backups_dir,
    classify,
    database_path,
    main,
    prune,
    restore_if_unreadable,
    snapshot,
)


# --------------------------------------------------------------------------
# Building boards
# --------------------------------------------------------------------------


def _board(path: Path, homes: list[str]) -> Path:
    """A WAL-mode database holding the named homes, like the app's own."""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE IF NOT EXISTS homes (name TEXT PRIMARY KEY, note TEXT)")
    connection.executemany(
        "INSERT OR REPLACE INTO homes VALUES (?, ?)", [(name, f"note on {name}") for name in homes]
    )
    connection.commit()
    connection.close()
    return path


def _homes(path: Path) -> list[str]:
    connection = sqlite3.connect(path)
    try:
        return [row[0] for row in connection.execute("SELECT name FROM homes ORDER BY name")]
    finally:
        connection.close()


def _damage(path: Path) -> None:
    """Break the pages below the header, so the file still opens and then fails.

    The kind of damage that slips past a check of the header alone -- the app's
    own start-up check was once fooled by exactly this.
    """
    size = path.stat().st_size
    with path.open("r+b") as handle:
        handle.seek(size // 2 if size > 8192 else 4096)
        handle.write(os.urandom(4096))
    assert classify(path) == DAMAGED, "the fixture did not actually damage the file"


def _app(tmp_path: Path, homes: list[str] | None = None) -> Path:
    root = tmp_path / "app"
    (root / "data").mkdir(parents=True)
    if homes is not None:
        big = homes + [f"filler-{index}" for index in range(400)]
        _board(database_path(root), big)
    return root


def _backup(root: Path, stamp: str, homes: list[str] | None, *, damaged: bool = False) -> Path:
    """A backup file with a given timestamp in its name."""
    folder = backups_dir(root)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"housing-{stamp}.sqlite3"
    if homes is None:
        path.write_bytes(b"this is not a database at all")
    else:
        _board(path, homes + [f"filler-{index}" for index in range(400)])
        if damaged:
            _damage(path)
    return path


def _named(root: Path) -> list[str]:
    """The recovery points in the folder -- not the evidence, not anything else."""
    return sorted(p.name for p in backups_dir(root).iterdir() if backups.BACKUP_PATTERN.match(p.name))


def _evidence_named(root: Path) -> list[str]:
    return sorted(p.name for p in backups_dir(root).iterdir() if backups.EVIDENCE_PATTERN.match(p.name))


# --------------------------------------------------------------------------
# classify
# --------------------------------------------------------------------------


def test_a_readable_database_is_good(tmp_path: Path) -> None:
    assert classify(_board(tmp_path / "a.sqlite3", ["one"])) == GOOD


def test_a_file_that_is_not_a_database_is_damaged(tmp_path: Path) -> None:
    path = tmp_path / "junk.sqlite3"
    path.write_bytes(b"this is not a database at all")
    assert classify(path) == DAMAGED


def test_damage_below_the_header_is_damaged_not_good(tmp_path: Path) -> None:
    """Opens, answers a header check, and fails on the pages -- still damaged."""
    path = _board(tmp_path / "a.sqlite3", [f"home-{index}" for index in range(500)])
    _damage(path)
    assert classify(path) == DAMAGED


def test_an_empty_file_is_damaged(tmp_path: Path) -> None:
    path = tmp_path / "empty.sqlite3"
    path.write_bytes(b"")
    assert classify(path) == DAMAGED


def test_a_file_that_cannot_be_opened_is_unknown_not_damaged(tmp_path: Path) -> None:
    """Not being able to look is not evidence that a copy is worthless."""
    assert classify(tmp_path / "missing.sqlite3") == UNKNOWN


def test_a_board_whose_good_pages_are_still_in_its_log_is_not_called_damaged(tmp_path: Path) -> None:
    """Why the live file is never read the way a backup is.

    After a crash the correct copy of a page can exist only in the write-ahead
    log while the main file holds a torn one; SQLite reads the log first and the
    board is perfectly fine. Read without the log -- the way backups are read,
    to touch nothing -- the same file looks damaged, and Repair would restore a
    backup over a board that needed nothing, losing everything written since.
    """
    live = tmp_path / "live.sqlite3"
    connection = sqlite3.connect(live)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE homes (name TEXT, note TEXT)")
    connection.executemany(
        "INSERT INTO homes VALUES (?, ?)", [(f"home-{index}", "x" * 200) for index in range(3000)]
    )
    connection.commit()
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    writer = sqlite3.connect(live)
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("UPDATE homes SET note = ?", ("y" * 200,))
    writer.commit()
    try:
        size = live.stat().st_size
        with live.open("r+b") as handle:
            handle.seek(size // 2)
            handle.write(os.urandom(8192))

        assert classify(live) == DAMAGED, "the fixture must look damaged without the log"
        assert classify(live, live=True) == GOOD, "a recoverable board was called damaged"
    finally:
        writer.close()
        connection.close()


def test_checking_a_database_never_writes_to_it(tmp_path: Path) -> None:
    """A check must not be able to change the thing it is checking."""
    path = _board(tmp_path / "a.sqlite3", ["one"])
    before = path.read_bytes()
    classify(path)
    assert path.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.sqlite3"], "checking left sidecars behind"


# --------------------------------------------------------------------------
# snapshot
# --------------------------------------------------------------------------


def test_a_first_install_has_nothing_to_copy(tmp_path: Path) -> None:
    assert snapshot(_app(tmp_path)) is None


def test_a_snapshot_is_a_restorable_copy(tmp_path: Path) -> None:
    root = _app(tmp_path, ["saved-home"])
    taken = snapshot(root)
    assert taken is not None and classify(taken) == GOOD
    assert "saved-home" in _homes(taken)


def test_a_snapshot_keeps_what_is_still_in_the_write_ahead_log(tmp_path: Path) -> None:
    """A byte copy of the main file dropped 50 of 51 rows once.

    In WAL mode the main file is current only as of the last checkpoint; the
    newest writes live in the ``-wal`` file until then. The copy has to read
    through it.
    """
    root = _app(tmp_path, ["checkpointed"])
    writer = sqlite3.connect(database_path(root))
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("INSERT INTO homes VALUES ('only-in-the-wal', 'written a moment ago')")
    writer.commit()
    try:
        assert Path(f"{database_path(root)}-wal").stat().st_size > 0, "the row must still be in the WAL"
        taken = snapshot(root)
    finally:
        writer.close()
    assert "only-in-the-wal" in _homes(taken)


def test_a_board_whose_good_pages_are_in_its_log_is_copied_as_a_recovery_point(tmp_path: Path) -> None:
    """A snapshot of a recoverable board must itself be one you can restore from.

    Read without its log, a board whose correct pages sit only in the
    write-ahead log looks damaged, and would be copied byte for byte as evidence
    -- a copy that never counts as a recovery point. Read through the log it is
    copied consistently, with the newest writes in it.
    """
    root = _app(tmp_path)
    live = database_path(root)
    connection = sqlite3.connect(live)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE homes (name TEXT PRIMARY KEY, note TEXT)")
    connection.executemany(
        "INSERT INTO homes VALUES (?, ?)", [(f"home-{index}", "x" * 200) for index in range(3000)]
    )
    connection.commit()
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    writer = sqlite3.connect(live)
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("UPDATE homes SET note = 'written since the checkpoint'")
    writer.commit()
    try:
        size = live.stat().st_size
        with live.open("r+b") as handle:
            handle.seek(size // 2)
            handle.write(os.urandom(8192))

        taken = snapshot(root)
    finally:
        writer.close()
        connection.close()

    assert classify(taken) == GOOD, "a recoverable board was copied as damage instead of as a backup"
    check = sqlite3.connect(taken)
    try:
        notes = {row[0] for row in check.execute("SELECT DISTINCT note FROM homes")}
    finally:
        check.close()
    assert notes == {"written since the checkpoint"}, "the copy lost what was only in the log"


def test_taking_a_snapshot_never_changes_the_live_database(tmp_path: Path) -> None:
    root = _app(tmp_path, ["one"])
    live = database_path(root)
    before = live.read_bytes()
    snapshot(root)
    assert live.read_bytes() == before


def test_a_damaged_live_file_is_still_copied_as_evidence(tmp_path: Path) -> None:
    root = _app(tmp_path, ["one"])
    live = database_path(root)
    _damage(live)
    Path(f"{live}-wal").write_bytes(b"the newest writes, possibly")
    taken = snapshot(root)
    assert taken.read_bytes() == live.read_bytes(), "the evidence copy is not the damaged file"
    assert Path(f"{taken}-wal").read_bytes() == b"the newest writes, possibly", "its WAL was left behind"
    assert backups.EVIDENCE_PATTERN.match(taken.name), "evidence was named as if it could be restored from"


def test_two_snapshots_in_one_second_do_not_overwrite_each_other(tmp_path: Path) -> None:
    root = _app(tmp_path, ["one"])
    first = snapshot(root, now=1_700_000_000)
    _board(database_path(root), ["two"])
    second = snapshot(root, now=1_700_000_000)
    assert first != second and first.exists() and second.exists()
    assert "two" in _homes(second) and "two" not in _homes(first)


def test_a_board_that_has_not_changed_is_not_copied_twice(tmp_path: Path) -> None:
    """The copy already there stands for it, so copying an unchanged board --
    however many times Repair is clicked -- cannot push older copies out."""
    root = _app(tmp_path, ["one"])
    first = snapshot(root)
    again = [snapshot(root) for _ in range(5)]
    assert again == [first] * 5
    assert _named(root) == [first.name]


def test_a_snapshot_that_cannot_be_written_raises_and_leaves_nothing_half_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every caller is about to do something only safe with a copy in hand."""
    root = _app(tmp_path, ["one"])

    def full_disk(source, target, **kwargs):
        # Part of the copy lands before the disk fills, the way it really fails.
        Path(target).write_bytes(b"SQLite format 3\x00" + b"\x00" * 4096)
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(backups, "_copy_consistent", full_disk)
    with pytest.raises(BackupError):
        snapshot(root)
    leftovers = [p.name for p in backups_dir(root).iterdir()]
    assert not leftovers, f"a failed snapshot left {leftovers}"


# --------------------------------------------------------------------------
# prune
# --------------------------------------------------------------------------


def test_growth_is_bounded(tmp_path: Path) -> None:
    root = _app(tmp_path)
    for day in range(1, 31):
        _backup(root, f"202609{day:02d}-120000", [f"home-{day}"])
    prune(root)
    assert len(_named(root)) == backups.KEEP_GOOD


def test_the_newest_copies_are_the_ones_kept(tmp_path: Path) -> None:
    root = _app(tmp_path)
    for day in range(1, 16):
        _backup(root, f"202609{day:02d}-120000", [f"home-{day}"])
    prune(root, keep_good=3)
    assert _named(root) == [
        "housing-20260913-120000.sqlite3",
        "housing-20260914-120000.sqlite3",
        "housing-20260915-120000.sqlite3",
    ]


def test_twenty_repairs_on_a_broken_board_do_not_prune_the_last_good_copy(tmp_path: Path) -> None:
    """The regression, exactly: every Repair backed up the broken live file.

    The old rule kept the twenty newest copies of any kind. After twenty
    Repairs on a board that stayed broken, those twenty were all copies of the
    damage, and the one backup that could have fixed it had been pruned --
    before the restore step went looking for it.
    """
    root = _app(tmp_path)
    good = _backup(root, "20260801-090000", ["the-last-good-copy"])
    for index in range(25):
        _backup(root, f"20260901-{index:02d}0000", ["broken"], damaged=True)

    prune(root)

    assert good.exists(), "the only restorable copy was pruned"
    assert classify(good) == GOOD


def test_damaged_copies_never_occupy_a_recovery_place(tmp_path: Path) -> None:
    root = _app(tmp_path)
    for day in range(1, 6):
        _backup(root, f"202608{day:02d}-120000", [f"good-{day}"])
    for index in range(8):
        _backup(root, f"20260901-{index:02d}0000", None)

    prune(root, keep_good=5, keep_damaged=2)

    remaining = [classify(p) for p in backups_dir(root).glob("housing-*.sqlite3")]
    assert remaining.count(GOOD) == 5, "damaged copies pushed good ones out"
    assert remaining.count(DAMAGED) == 2


def test_with_no_restorable_copy_nothing_at_all_is_pruned(tmp_path: Path) -> None:
    """A damaged copy may be all that is left to recover anything from."""
    root = _app(tmp_path)
    for index in range(12):
        _backup(root, f"20260901-{index:02d}0000", None)
    assert prune(root) == []
    assert len(_named(root)) == 12


def test_a_copy_whose_state_cannot_be_read_is_never_pruned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _app(tmp_path)
    for day in range(1, 16):
        _backup(root, f"202609{day:02d}-120000", [f"home-{day}"])
    # More of them than either quota would keep, and the oldest of all, so that
    # counting them as good or as damaged would each prune some.
    unreadable = {backups_dir(root) / f"housing-202609{day:02d}-120000.sqlite3" for day in range(1, 7)}
    real = backups.classify

    def cannot_tell(path, **kwargs):
        return UNKNOWN if Path(path) in unreadable else real(path, **kwargs)

    monkeypatch.setattr(backups, "classify", cannot_tell)
    prune(root, keep_good=3, keep_damaged=1)
    missing = sorted(path.name for path in unreadable if not path.exists())
    assert not missing, f"copies that could not be checked were deleted: {missing}"


def test_files_it_did_not_name_are_never_touched(tmp_path: Path) -> None:
    root = _app(tmp_path)
    for day in range(1, 16):
        _backup(root, f"202609{day:02d}-120000", [f"home-{day}"])
    # Real, readable databases -- so if they were mistaken for this module's
    # copies they would count as good ones and be pruned, not waved through as
    # damaged evidence.
    theirs = [
        _board(backups_dir(root) / "my-own-copy.sqlite3", ["theirs"]),
        _board(backups_dir(root) / "housing-before-moving.sqlite3", ["theirs"]),
        _board(backups_dir(root) / "housing-2026.sqlite3", ["theirs"]),
    ]
    (backups_dir(root) / "notes.txt").write_text("theirs")
    prune(root, keep_good=2)
    theirs.append(backups_dir(root) / "notes.txt")
    assert all(path.exists() for path in theirs)


def test_newest_means_the_time_in_the_name_not_the_file_date(tmp_path: Path) -> None:
    """A copy, a Time Machine restore or a sync tool can all rewrite a file's date.

    The stamp in the name was written by this module when the copy was taken,
    and nothing else changes it.
    """
    root = _app(tmp_path)
    for day in range(1, 6):
        _backup(root, f"202609{day:02d}-120000", [f"home-{day}"])
    newest = backups_dir(root) / "housing-20260905-120000.sqlite3"
    ancient = time.time() - 400 * 86400
    os.utime(newest, (ancient, ancient))

    prune(root, keep_good=1)

    assert _named(root) == ["housing-20260905-120000.sqlite3"]


def test_a_pruned_copy_takes_its_sidecars_with_it(tmp_path: Path) -> None:
    root = _app(tmp_path)
    for day in range(1, 5):
        _backup(root, f"202609{day:02d}-120000", [f"home-{day}"])
    old = backups_dir(root) / "housing-20260901-120000.sqlite3"
    Path(f"{old}-wal").write_bytes(b"x")
    prune(root, keep_good=2)
    assert not old.exists() and not Path(f"{old}-wal").exists()


def test_keeping_no_good_copies_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        prune(_app(tmp_path), keep_good=0)


# --------------------------------------------------------------------------
# restore_if_unreadable
# --------------------------------------------------------------------------


def test_a_readable_database_is_never_replaced(tmp_path: Path) -> None:
    """Restoring over a working board throws away every home found since the backup."""
    root = _app(tmp_path, ["found-today"])
    _backup(root, "20260101-000000", ["stale"])
    before = database_path(root).read_bytes()

    assert restore_if_unreadable(root).outcome == "healthy"
    assert database_path(root).read_bytes() == before


def test_repair_never_restores_over_a_board_whose_good_pages_are_in_its_log(tmp_path: Path) -> None:
    """The end-to-end half of reading the live file through its log.

    A crash can leave the main file with torn pages whose correct copies sit
    only in the write-ahead log. That board is fine, and Repair must leave it
    alone: read the way a backup is read, it looks damaged, and a restore here
    would replace it with a backup and lose everything written since.
    """
    root = _app(tmp_path)
    live = database_path(root)
    connection = sqlite3.connect(live)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE homes (name TEXT, note TEXT)")
    connection.executemany(
        "INSERT INTO homes VALUES (?, ?)", [(f"home-{index}", "x" * 200) for index in range(3000)]
    )
    connection.commit()
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    writer = sqlite3.connect(live)
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("UPDATE homes SET note = 'written since the backup'")
    writer.commit()
    _backup(root, "20260101-000000", ["an-older-board"])
    try:
        size = live.stat().st_size
        with live.open("r+b") as handle:
            handle.seek(size // 2)
            handle.write(os.urandom(8192))

        result = restore_if_unreadable(root)

        assert result.outcome == "healthy", "a recoverable board was replaced by a backup"
        assert not _evidence_named(root)
    finally:
        writer.close()
        connection.close()


def test_no_database_is_not_a_reason_to_restore(tmp_path: Path) -> None:
    root = _app(tmp_path)
    _backup(root, "20260101-000000", ["one"])
    assert restore_if_unreadable(root).outcome == "absent"
    assert not database_path(root).exists()


def test_a_damaged_database_gets_the_newest_good_backup_back(tmp_path: Path) -> None:
    root = _app(tmp_path, ["whatever"])
    _backup(root, "20260801-000000", ["older-good"])
    _backup(root, "20260901-000000", ["newest-good", "saved-home"])
    _backup(root, "20260910-000000", ["broken"], damaged=True)
    _damage(database_path(root))

    result = restore_if_unreadable(root)

    assert result.outcome == "restored"
    assert result.restored_from.name == "housing-20260901-000000.sqlite3"
    assert classify(database_path(root)) == GOOD
    assert "saved-home" in _homes(database_path(root))


def test_the_damaged_file_and_its_write_ahead_log_are_kept_not_deleted(tmp_path: Path) -> None:
    """The regression: the damaged file's WAL was deleted.

    It can hold the newest writes and is the only evidence of what went wrong,
    so it moves aside with its database as a pair SQLite could still open. The
    log must arrive byte for byte. The shared-memory index moves with it too,
    but SQLite may refresh that while reading through the log -- it is rebuilt
    from the log and holds nothing of its own, so only its presence is checked.
    """
    root = _app(tmp_path, ["whatever"])
    _backup(root, "20260901-000000", ["good"])
    live = database_path(root)
    _damage(live)
    damaged_bytes = live.read_bytes()
    Path(f"{live}-wal").write_bytes(b"the newest writes")
    Path(f"{live}-shm").write_bytes(b"shared memory")

    result = restore_if_unreadable(root)

    kept = result.kept_damaged_as
    assert kept.read_bytes() == damaged_bytes
    assert Path(f"{kept}-wal").read_bytes() == b"the newest writes", "the log was altered or lost"
    assert Path(f"{kept}-shm").exists(), "the index was left behind"
    assert not Path(f"{live}-wal").exists(), "the old WAL was left to pair with the restored file"


def test_with_no_good_backup_nothing_is_changed(tmp_path: Path) -> None:
    root = _app(tmp_path, ["whatever"])
    _backup(root, "20260901-000000", None)
    live = database_path(root)
    _damage(live)
    before = live.read_bytes()

    result = restore_if_unreadable(root)

    assert result.outcome == "no_good_backup"
    assert live.read_bytes() == before
    assert "Nothing was deleted" in result.detail


def test_a_backup_that_does_not_read_back_cleanly_is_not_put_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _app(tmp_path, ["whatever"])
    _backup(root, "20260901-000000", ["good"])
    live = database_path(root)
    _damage(live)
    before = live.read_bytes()
    real = backups.classify

    def staged_copy_is_bad(path, **kwargs):
        return DAMAGED if Path(path).name.endswith(".restoring") else real(path, **kwargs)

    monkeypatch.setattr(backups, "classify", staged_copy_is_bad)
    result = restore_if_unreadable(root)

    assert result.outcome == "failed"
    assert live.read_bytes() == before, "an unverified copy replaced the database"
    assert not live.with_name("housing.sqlite3.restoring").exists()


def test_a_restore_that_fails_at_the_last_step_leaves_the_damaged_file_where_it_was(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression: the damaged file was moved aside before the copy landed.

    A full disk between the two left no database at all, and the next start
    created an empty one -- a board that looked like everything was gone. Now
    a failure at the final swap puts the damaged set straight back.
    """
    root = _app(tmp_path, ["whatever"])
    _backup(root, "20260901-000000", ["good"])
    live = database_path(root)
    _damage(live)
    Path(f"{live}-wal").write_bytes(b"the newest writes")
    before = live.read_bytes()
    real_replace = Path.replace

    def swap_fails(self, target):
        if str(self).endswith(".restoring"):
            raise OSError("no space left on device")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", swap_fails)
    result = restore_if_unreadable(root)
    monkeypatch.undo()

    assert result.outcome == "failed"
    assert live.exists(), "the swap failed and left no database at all"
    assert live.read_bytes() == before
    assert Path(f"{live}-wal").read_bytes() == b"the newest writes", "the log was not put back beside its file"
    assert "Nothing was lost" in result.detail
    assert not live.with_name("housing.sqlite3.restoring").exists()


# --------------------------------------------------------------------------
# The command both platforms run
# --------------------------------------------------------------------------


def test_protect_takes_a_copy_and_bounds_the_folder(tmp_path: Path) -> None:
    root = _app(tmp_path, ["one"])
    for day in range(1, 26):
        _backup(root, f"202608{day:02d}-120000", [f"home-{day}"])

    assert main(["protect", str(root)]) == 0
    assert len(_named(root)) == backups.KEEP_GOOD


def test_protect_changes_nothing_when_it_cannot_take_a_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No copy, no pruning: removing old backups is only safe with a new one in hand."""
    root = _app(tmp_path, ["one"])
    for day in range(1, 26):
        _backup(root, f"202608{day:02d}-120000", [f"home-{day}"])
    monkeypatch.setattr(backups, "snapshot", lambda root: (_ for _ in ()).throw(BackupError("disk full")))

    assert main(["protect", str(root)]) == 2
    assert len(_named(root)) == 25, "old backups were pruned without a new one taken"


def test_with_no_database_in_place_the_backups_are_all_kept(tmp_path: Path) -> None:
    """With the live file gone, the backups are the only copies there are.

    Pruning is only ever safe beside a copy just taken of a board that is still
    there. Without one, every older backup might hold something nothing else
    does -- so none of them is removed, however many there are.
    """
    root = _app(tmp_path)
    for day in range(1, 26):
        _backup(root, f"202608{day:02d}-120000", [f"home-{day}"])

    for command in ("protect", "repair"):
        assert main([command, str(root)]) == 0
        assert len(_named(root)) == 25, f"{command} pruned the only copies there are"


def test_repair_on_a_damaged_board_puts_the_homes_back(tmp_path: Path, capsys) -> None:
    root = _app(tmp_path, ["whatever"])
    _backup(root, "20260901-000000", ["saved-home"])
    _damage(database_path(root))

    assert main(["repair", str(root)]) == 0
    assert "saved-home" in _homes(database_path(root))
    assert "Restored the backup from housing-20260901-000000.sqlite3" in capsys.readouterr().out


def test_repair_with_nothing_to_restore_says_so_and_deletes_nothing(tmp_path: Path, capsys) -> None:
    root = _app(tmp_path, ["whatever"])
    live = database_path(root)
    _damage(live)
    before = live.read_bytes()

    assert main(["repair", str(root)]) == 3
    assert live.read_bytes() == before
    assert "Nothing was deleted" in capsys.readouterr().out


def test_repeated_repairs_on_a_board_that_will_not_heal_keep_the_way_back(tmp_path: Path) -> None:
    """The whole failure, end to end, through the command Repair actually runs.

    Somebody whose board keeps breaking clicks Repair again and again. Each
    click snapshots the damaged file. The one good backup from before the
    trouble must still be there -- and still be the one restored -- on the
    thirtieth click.
    """
    root = _app(tmp_path, ["whatever"])
    _backup(root, "20260101-000000", ["the-homes-from-before"])
    for click in range(30):
        _damage(database_path(root)) if classify(database_path(root)) == GOOD else None
        assert main(["repair", str(root)]) == 0, f"repair {click + 1} failed"
        assert "the-homes-from-before" in _homes(database_path(root))
    assert (backups_dir(root) / "housing-20260101-000000.sqlite3").exists()


# --------------------------------------------------------------------------
# What an independent review of the first version found
# --------------------------------------------------------------------------


def _listings_board(path: Path, count: int, *, log_rows: int = 0) -> Path:
    """A board shaped like the app's: homes in ``listings``, runs logged beside them."""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE IF NOT EXISTS listings (id INTEGER PRIMARY KEY, title TEXT, note TEXT)")
    connection.execute("CREATE TABLE IF NOT EXISTS scan_runs (id INTEGER PRIMARY KEY, at TEXT)")
    connection.executemany("INSERT INTO listings (title) VALUES (?)", [(f"home-{n}",) for n in range(count)])
    connection.executemany("INSERT INTO scan_runs (at) VALUES (?)", [(f"run-{n}",) for n in range(log_rows)])
    connection.commit()
    connection.close()
    return path


def _listing_count(path: Path) -> int:
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?immutable=1", uri=True)
    try:
        return connection.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
    finally:
        connection.close()


def test_copies_of_an_emptied_board_cannot_push_out_the_last_full_one(tmp_path: Path) -> None:
    """The regression: the file goes missing, the app makes a fresh empty board,
    and somebody whose homes have vanished clicks Repair again and again. Each
    click copied the empty board as a perfectly good backup, and within a few
    clicks every copy that still held their homes had been pruned."""
    root = _app(tmp_path)
    for day in range(1, 11):
        _listings_board(backups_dir(root) / f"housing-202608{day:02d}-120000.sqlite3", 300 + day)
    live = _listings_board(database_path(root), 0)
    for click in range(15):
        # The app writes a little between clicks, so no two copies are alike.
        connection = sqlite3.connect(live)
        connection.execute("INSERT INTO scan_runs (at) VALUES (?)", (f"click-{click}",))
        connection.commit()
        connection.close()
        assert main(["repair", str(root)]) == 0

    counts = [_listing_count(backups_dir(root) / name) for name in _named(root)]
    assert max(counts) == 310, f"every copy with the homes in it was pruned: {counts}"
    assert len(counts) <= backups.KEEP_GOOD + 1, "keeping the fullest copy must still be bounded"


def test_a_clock_set_back_never_makes_a_fresh_copy_the_oldest(tmp_path: Path) -> None:
    """The regression: with the clock behind the names already in the folder,
    the copy just taken sorted as the oldest and was pruned at once -- and the
    install went on to replace the app with no copy from before it."""
    root = _app(tmp_path, ["today"])
    for day in range(1, 11):
        _backup(root, f"202609{day:02d}-120000", [f"home-{day}"])
    long_ago = time.mktime((2026, 1, 1, 12, 0, 0, 0, 0, -1))

    taken = snapshot(root, now=long_ago)
    prune(root)

    assert taken.exists(), "the copy just taken was pruned"
    newest = backups._named(backups_dir(root), backups.BACKUP_PATTERN)[0][1]
    assert newest == taken, f"the fresh copy {taken.name} does not sort as the newest"
    _damage(database_path(root))
    assert "today" in _homes(backups_dir(root) / restore_if_unreadable(root).restored_from.name)


def test_a_copy_named_in_the_future_is_followed_not_restored_over_newer_ones(tmp_path: Path) -> None:
    """A copy taken while the clock ran fast is named after the real time. The
    next copy used to sort before it, so a Repair restored the stale one."""
    root = _app(tmp_path, ["found-today"])
    _backup(root, "20380101-000000", ["from-the-fast-clock"])

    taken = snapshot(root)
    _damage(database_path(root))
    result = restore_if_unreadable(root)

    assert result.restored_from == taken, f"restored {result.restored_from.name} over the copy just taken"
    assert "found-today" in _homes(database_path(root))


def test_a_file_in_a_format_sqlite_does_not_know_is_damage(tmp_path: Path) -> None:
    """SQLite says "unsupported file format" with a plain error code, so it was
    filed as "could not tell": Repair then restored nothing and said nothing,
    while the app told the person Repair would fix it."""
    path = _board(tmp_path / "a.sqlite3", ["one"])
    with path.open("r+b") as handle:
        handle.seek(44)
        handle.write(b"\x00\x00\x00\x09")
    assert classify(path) == DAMAGED
    assert classify(path, live=True) == DAMAGED


def test_a_board_that_is_only_busy_is_not_called_damaged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Locked, busy or unreachable is not damage: a restore over it would throw
    away a board that is fine."""
    path = _board(tmp_path / "a.sqlite3", ["one"])
    for message in ("database is locked", "disk I/O error", "unable to open database file"):
        def refuse(*args, message=message, **kwargs):
            raise sqlite3.OperationalError(message)

        monkeypatch.setattr(backups, "_open", refuse)
        assert classify(path) == UNKNOWN, message


def test_evidence_is_never_restored_from_even_when_its_file_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression: damage living only in the write-ahead log. The live board
    reads as damaged through its log, but its main file alone reads fine -- so
    its evidence copy was counted as the newest good backup and restored."""
    root = _app(tmp_path, ["found-today"])
    _backup(root, "20260801-000000", ["the-real-backup"])
    live = database_path(root)
    real = backups.classify
    monkeypatch.setattr(
        backups, "classify", lambda path, live=False: DAMAGED if live else real(path, live=live)
    )

    taken = snapshot(root)
    result = restore_if_unreadable(root)

    assert backups.EVIDENCE_PATTERN.match(taken.name)
    assert result.restored_from.name == "housing-20260801-000000.sqlite3"
    assert "the-real-backup" in _homes(live)


def test_the_same_damage_is_kept_once_however_often_it_is_copied(tmp_path: Path) -> None:
    """The regression: a damaged board with no good backup to restore from got a
    full-size copy on every install and every Repair, and none was ever pruned."""
    root = _app(tmp_path, ["one"])
    _damage(database_path(root))
    for _ in range(20):
        main(["protect", str(root)])
        main(["repair", str(root)])
    assert len(_evidence_named(root)) == 1


def test_a_restore_never_moves_or_deletes_the_live_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression: the damaged file was moved aside and the backup moved in
    after it, so for a moment there was no database -- a crash there, and the
    next start made an empty one. Now the backup replaces it in one rename."""
    root = _app(tmp_path, ["whatever"])
    _backup(root, "20260901-000000", ["saved-home"])
    live = database_path(root)
    _damage(live)
    touched: list[str] = []
    real_replace, real_rename, real_unlink = Path.replace, Path.rename, Path.unlink

    def watch(name, real):
        def wrapper(self, *args, **kwargs):
            if self == live:
                touched.append(name)
            return real(self, *args, **kwargs)
        return wrapper

    monkeypatch.setattr(Path, "replace", watch("replace", real_replace))
    monkeypatch.setattr(Path, "rename", watch("rename", real_rename))
    monkeypatch.setattr(Path, "unlink", watch("unlink", real_unlink))
    result = restore_if_unreadable(root)
    monkeypatch.undo()

    assert result.outcome == "restored"
    assert touched == [], f"the live file was {touched}"
    assert "saved-home" in _homes(live)


def test_a_crash_in_the_middle_of_a_restore_leaves_a_database_and_a_way_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A power cut at the worst moment: the log has been cleared, the swap has
    not happened. The unreadable file is still where the app looks, a complete
    copy of it is in the backups folder, and the next Repair finishes the job."""
    root = _app(tmp_path, ["whatever"])
    _backup(root, "20260901-000000", ["saved-home"])
    live = database_path(root)
    _damage(live)
    Path(f"{live}-wal").write_bytes(b"the newest writes")
    real_replace = Path.replace

    def power_cut(self, target):
        if str(self).endswith(".restoring"):
            raise KeyboardInterrupt("the power went")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", power_cut)
    with pytest.raises(KeyboardInterrupt):
        restore_if_unreadable(root)
    monkeypatch.undo()

    assert live.exists(), "the crash left no database for the app to find"
    evidence = backups_dir(root) / _evidence_named(root)[0]
    assert Path(f"{evidence}-wal").read_bytes() == b"the newest writes", "the log is gone"
    assert restore_if_unreadable(root).outcome == "restored"
    assert "saved-home" in _homes(live)


def test_a_restore_that_cannot_put_the_log_back_still_loses_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The double fault: the swap fails, and putting the log back fails too --
    antivirus holding a file, say. It used to end with no database and a
    message saying nothing had changed."""
    root = _app(tmp_path, ["whatever"])
    _backup(root, "20260901-000000", ["saved-home"])
    live = database_path(root)
    _damage(live)
    Path(f"{live}-wal").write_bytes(b"the newest writes")
    damaged = live.read_bytes()
    real_replace, real_copy = Path.replace, backups.shutil.copy2

    def swap_fails(self, target):
        if str(self).endswith(".restoring"):
            raise OSError("the file is in use")
        return real_replace(self, target)

    def put_back_fails(source, target, *args, **kwargs):
        if str(target).startswith(str(live)):
            raise OSError("the file is in use")
        return real_copy(source, target, *args, **kwargs)

    monkeypatch.setattr(Path, "replace", swap_fails)
    monkeypatch.setattr(backups.shutil, "copy2", put_back_fails)
    result = restore_if_unreadable(root)
    monkeypatch.undo()

    assert result.outcome == "failed"
    assert live.read_bytes() == damaged, "the database the app looks for is gone"
    evidence = backups_dir(root) / _evidence_named(root)[0]
    assert evidence.read_bytes() == damaged and Path(f"{evidence}-wal").read_bytes() == b"the newest writes"
    assert str(evidence) in result.detail, "the message does not say where the copy is"


def test_anything_unexpected_while_copying_stops_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Both Repairs carry on to the reinstall on any code but 2, so a crash in
    the copy must come out as 2 -- "no copy, nothing else" -- not as 1."""
    root = _app(tmp_path, ["one"])
    monkeypatch.setattr(backups, "snapshot", lambda root: (_ for _ in ()).throw(PermissionError("denied")))
    assert main(["repair", str(root)]) == 2
    assert "Nothing was changed" in capsys.readouterr().out


def test_anything_unexpected_while_restoring_is_said_not_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    root = _app(tmp_path, ["one"])
    monkeypatch.setattr(backups, "restore_if_unreadable", lambda root: (_ for _ in ()).throw(MemoryError()))
    assert main(["repair", str(root)]) == 3
    assert "Nothing was deleted" in capsys.readouterr().out


def test_a_failure_while_tidying_old_copies_changes_nothing_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    root = _app(tmp_path, ["one"])
    monkeypatch.setattr(backups, "prune", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("busy")))
    assert main(["protect", str(root)]) == 0
    assert "could not be tidied" in capsys.readouterr().out
    assert len(_named(root)) == 1


def test_evidence_that_differs_only_in_its_log_is_not_mistaken_for_a_copy_already_kept(tmp_path: Path) -> None:
    """The log is where the newest writes are. Two unreadable boards with the
    same main file and different logs are different evidence."""
    root = _app(tmp_path, ["one"])
    live = database_path(root)
    _damage(live)
    Path(f"{live}-wal").write_bytes(b"first writes")
    snapshot(root)
    Path(f"{live}-wal").write_bytes(b"second writes")
    snapshot(root)
    logs = sorted(Path(f"{backups_dir(root) / name}-wal").read_bytes() for name in _evidence_named(root))
    assert logs == [b"first writes", b"second writes"]


def test_a_board_that_cannot_be_judged_or_read_through_is_kept_as_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rather than stopping every install and every Repair: when SQLite can
    neither judge the board nor read it through, a byte-for-byte copy is still
    a copy, and nothing is restored from it."""
    root = _app(tmp_path, ["one"])
    real = backups.classify
    monkeypatch.setattr(backups, "classify", lambda path, live=False: UNKNOWN if live else real(path))

    def unreadable(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(backups, "_copy_consistent", unreadable)
    taken = snapshot(root)
    assert backups.EVIDENCE_PATTERN.match(taken.name)
    assert taken.read_bytes() == database_path(root).read_bytes()
