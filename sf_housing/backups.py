"""Safety copies of the one file that holds somebody's search, and the way back to them.

Months of stars, notes and dismissals live in ``data/housing.sqlite3`` and
nowhere else -- no account, no server, no cloud copy. Every other failure in
this app is recoverable; losing that file is not. So everything that can
replace, copy or delete it goes through here, on both platforms, and every
decision fails toward keeping data rather than toward tidiness.

This module is imported by the macOS and Windows installers and by both Repair
scripts. Repair runs it straight out of the release's own wheel, with whatever
Python the machine already has, because the reason somebody is running Repair
may be that the installed app is broken -- and because repairing an older
install has to run *this* version of these rules, not the one being repaired.
That is why it imports nothing but the standard library: it must load before
anything else in the package can be trusted to.

Two kinds of copy live in ``backups/``. ``housing-<time>.sqlite3`` is a
consistent copy of a board that read cleanly -- a recovery point.
``housing-unreadable-<time>.sqlite3`` is a byte-for-byte copy of one that did
not, sidecars and all -- evidence, kept for what it may still hold, and never
restored from. Anything else in the folder is somebody else's and is never
touched.

Each of these rules is here because its absence lost data, in the Repair this
replaced or in a review of the first version of this module:

* Pruning deleted the last good copy: damaged copies took recovery places.
  Good and damaged are counted separately, and the newest good copy is never
  removed.
* A burst of copies of an empty board -- the app makes a fresh one if the file
  goes missing, and somebody whose homes have vanished clicks Repair again and
  again -- pushed every real backup out. The copy holding the most homes is
  now kept until a newer one holds as many, and a copy identical to the one
  before it is not kept twice.
* A clock set back made a fresh copy the "oldest", and it was pruned the moment
  it was taken. Copies are now named so each sorts after every copy before it,
  whatever the clock says.
* A restore moved the damaged file aside before putting the backup in place,
  so a crash or a full disk between the two left no database at all and the
  next start made an empty one. The live file is now never moved: a complete
  copy of it is taken first, and the backup replaces it in one atomic rename.
* The damaged file's write-ahead log was deleted. It is copied with the rest.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path


DATABASE_NAME = "housing.sqlite3"

# Enough recovery points to reach back past a problem nobody noticed for a
# while, and few enough that the folder cannot grow without end. Counted among
# the copies that can actually be restored from: a damaged copy is evidence,
# not a recovery point, and letting it take a place here is exactly how the
# good ones used to be pushed out.
KEEP_GOOD = 10

# Evidence is kept for what it can tell somebody about how the damage happened
# and for whatever a specialist might still recover from it. Two is enough to
# compare; more is only disk.
KEEP_DAMAGED = 2

# The two names this module writes. It only ever prunes files named like these.
BACKUP_PATTERN = re.compile(r"^housing-(\d{8}-\d{6})(?:-(\d+))?\.sqlite3$")
EVIDENCE_PATTERN = re.compile(r"^housing-unreadable-(\d{8}-\d{6})(?:-(\d+))?\.sqlite3$")
_BACKUP_PREFIX = "housing-"
_EVIDENCE_PREFIX = "housing-unreadable-"

GOOD, DAMAGED, UNKNOWN = "good", "damaged", "unknown"

_SIDECARS = ("-wal", "-shm", "-journal")

# SQLite's own ways of saying a file's contents are bad, as opposed to saying
# it could not get at them (locked, busy, permission, I/O). The words are for a
# Python too old to report the code; "unsupported file format" comes back as a
# plain SQLITE_ERROR, so it can only be recognised by its words.
_DAMAGE_CODES = ("SQLITE_CORRUPT", "SQLITE_NOTADB")
_DAMAGE_WORDS = ("not a database", "malformed", "corrupt", "unsupported file format")


class BackupError(RuntimeError):
    """A safety copy could not be made, so nothing that depends on one may go ahead."""


def database_path(app_root: Path) -> Path:
    return Path(app_root) / "data" / DATABASE_NAME


def backups_dir(app_root: Path) -> Path:
    return Path(app_root) / "backups"


def _open(path: Path, *, live: bool) -> sqlite3.Connection:
    """Open a database for reading only, the right way for what it is.

    A *backup* is opened ``immutable``: SQLite reads the one file and nothing
    else, takes no locks and creates no ``-wal`` or ``-shm`` beside it, so
    looking at a copy cannot change the copy or clutter the folder. Every
    recovery point this module makes is complete on its own, so nothing is
    missed.

    The *live* database is never opened that way, and it matters. After a crash
    the correct version of a page can exist only in the write-ahead log while
    the main file holds a torn one; ``immutable`` ignores the log, would see the
    torn page, call a recoverable board damaged, and let Repair restore a backup
    over it -- losing everything written since. So the live file is opened
    ``mode=ro``, which reads through the log. SQLite may refresh the
    shared-memory index beside it to do so; that index is rebuilt from the log
    and holds nothing of its own, and the log itself is never written.
    """
    uri = Path(path).resolve().as_uri()
    return sqlite3.connect(f"{uri}?{'mode=ro' if live else 'immutable=1'}", uri=True, timeout=30)


def _says_damaged(exc: sqlite3.Error) -> bool:
    name = getattr(exc, "sqlite_errorname", "") or ""
    if name.startswith(_DAMAGE_CODES):
        return True
    message = str(exc).casefold()
    return any(word in message for word in _DAMAGE_WORDS)


def classify(path: Path, *, live: bool = False) -> str:
    """Can this file be restored from? ``good``, ``damaged`` or ``unknown``.

    ``damaged`` only when SQLite itself says the contents are bad -- a file that
    is not a database, one in a format it does not recognise, or one whose
    integrity check does not come back clean. Anything else that goes wrong
    while looking -- a permission, a lock, an I/O error -- is ``unknown``, and
    an unknown file is never pruned: not being able to tell is not evidence
    that a copy is worthless.

    ``live`` for the database the app uses, read through its write-ahead log;
    otherwise the file alone, touching nothing. See ``_open`` for why the two
    must differ.
    """
    path = Path(path)
    try:
        if not path.is_file() or path.stat().st_size == 0:
            return DAMAGED if path.is_file() else UNKNOWN
    except OSError:
        return UNKNOWN
    try:
        connection = _open(path, live=live)
    except sqlite3.Error as exc:
        return DAMAGED if _says_damaged(exc) else UNKNOWN
    try:
        row = connection.execute("PRAGMA quick_check(1)").fetchone()
    except sqlite3.Error as exc:
        return DAMAGED if _says_damaged(exc) else UNKNOWN
    finally:
        connection.close()
    return GOOD if row is not None and str(row[0]).casefold() == "ok" else DAMAGED


def _weight(path: Path) -> int:
    """How much of a board a copy holds: its homes, or failing that all its rows."""
    try:
        connection = _open(path, live=False)
    except sqlite3.Error:
        return 0
    try:
        tables = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        if "listings" in tables:
            tables = ["listings"]
        total = 0
        for table in tables:
            quoted = table.replace('"', '""')
            total += int(connection.execute(f'SELECT COUNT(*) FROM "{quoted}"').fetchone()[0])
        return total
    except sqlite3.Error:
        return 0
    finally:
        connection.close()


def _copy_consistent(source: Path, target: Path, *, live: bool) -> None:
    """Copy a database without losing what is still in its write-ahead log.

    SQLite's own backup API. For the live file it reads through the log: a
    byte copy of the main file is current only as of the last checkpoint, and
    done by hand it dropped every home, star and note written since -- 50 of 51
    rows in the case that found it. Read-only on the source either way, so
    taking a copy can never alter the thing being copied.
    """
    source_connection = _open(source, live=live)
    try:
        target_connection = sqlite3.connect(str(target))
        try:
            source_connection.backup(target_connection)
        finally:
            target_connection.close()
    finally:
        source_connection.close()


def _copy_raw(source: Path, target: Path) -> None:
    """Byte-for-byte, the main file and whatever sidecars it has, kept as a set.

    For a file SQLite cannot read, where the backup API has nothing to work
    with. The sidecars travel with it under the same name, so the copy stays
    the set SQLite would need to try to open it again.
    """
    shutil.copy2(source, target)
    for suffix in _SIDECARS:
        sidecar = Path(f"{source}{suffix}")
        if sidecar.exists():
            shutil.copy2(sidecar, Path(f"{target}{suffix}"))


def _discard_set(path: Path) -> bool:
    """Remove a file and its sidecars as far as possible. True if the file is gone.

    Never raises: it runs on the way out of failures, where a second error
    would hide the first, and on pruning, where a copy that will not go is
    simply kept.
    """
    for candidate in (Path(f"{path}{suffix}") for suffix in _SIDECARS):
        try:
            candidate.unlink()
        except OSError:
            pass
    try:
        Path(path).unlink()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


def _digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            hasher.update(block)
    return hasher.hexdigest()


def _same_set(first: Path, second: Path) -> bool:
    """The same database byte for byte: the file and its log and journal.

    The shared-memory index is left out. SQLite rebuilds it from the log and it
    holds nothing of its own, and merely reading the live file through its log
    can refresh it -- comparing it would make every copy look new.
    """
    for suffix in ("", "-wal", "-journal"):
        a, b = Path(f"{first}{suffix}"), Path(f"{second}{suffix}")
        if a.exists() != b.exists():
            return False
        if a.exists() and (a.stat().st_size != b.stat().st_size or _digest(a) != _digest(b)):
            return False
    return True


def _stamp(now: float | None = None) -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.localtime(now if now is not None else time.time()))


def _key(match: re.Match[str]) -> tuple[str, int]:
    """Where a copy sorts: the time in its name, then its tiebreak."""
    return match.group(1), int(match.group(2) or 1)


def _named(folder: Path, pattern: re.Pattern[str]) -> list[tuple[tuple[str, int], Path]]:
    """Every file in the folder named by this pattern, newest first."""
    if not folder.is_dir():
        return []
    found = []
    for entry in folder.iterdir():
        match = pattern.match(entry.name)
        if match and entry.is_file():
            found.append((_key(match), entry))
    return sorted(found, key=lambda item: item[0], reverse=True)


def _next_name(folder: Path, pattern: re.Pattern[str], prefix: str, now: float | None) -> Path:
    """A name that sorts after every copy of its kind already in the folder.

    The time now, when the clock is ahead of every name there; otherwise the
    newest name's time with the next tiebreak after it. A clock set back -- by
    hand, by a flat battery, by a new time zone -- used to give a fresh copy a
    name older than the rest, and pruning by name removed it the moment it was
    taken. A name from the future is followed rather than trusted less: it was
    written by this module, and everything taken since is newer than it.
    """
    stamp = _stamp(now)
    existing = _named(folder, pattern)
    if existing and existing[0][0][0] >= stamp:
        stamp, tiebreak = existing[0][0][0], existing[0][0][1] + 1
    else:
        tiebreak = 1
    while True:
        suffix = "" if tiebreak == 1 else f"-{tiebreak}"
        candidate = folder / f"{prefix}{stamp}{suffix}.sqlite3"
        if not candidate.exists() and not Path(f"{candidate}.partial").exists():
            return candidate
        tiebreak += 1


def _take(folder: Path, *, evidence: bool, now: float | None, write) -> Path:
    """Write one copy under a ``.partial`` name and give it its real name once complete.

    A copy cut off by a full disk can never be mistaken for a finished one. A
    copy byte-for-byte the same as the newest of its kind is not kept a second
    time -- the one already there stands for it -- so copying a board that has
    not changed, however often, cannot push older copies out.
    """
    pattern, prefix = (EVIDENCE_PATTERN, _EVIDENCE_PREFIX) if evidence else (BACKUP_PATTERN, _BACKUP_PREFIX)
    target = _next_name(folder, pattern, prefix, now)
    partial = Path(f"{target}.partial")
    moved: list[Path] = []
    try:
        write(partial)
        existing = _named(folder, pattern)
        if existing and _same_set(partial, existing[0][1]):
            _discard_set(partial)
            return existing[0][1]
        for suffix in _SIDECARS:
            sidecar = Path(f"{partial}{suffix}")
            if sidecar.exists():
                sidecar.replace(Path(f"{target}{suffix}"))
                moved.append(Path(f"{target}{suffix}"))
        partial.replace(target)
    except (OSError, sqlite3.Error) as exc:
        _discard_set(partial)
        for path in moved:
            try:
                path.unlink()
            except OSError:
                pass
        raise BackupError(f"could not copy the housing database: {exc}") from exc
    return target


def snapshot(app_root: Path, *, now: float | None = None) -> Path | None:
    """Take a safety copy of the live database, or raise ``BackupError``.

    Returns the copy that now stands for the live board -- new, or the newest
    one already there if nothing has changed since -- or None when there is no
    database to copy, as on a first install. Raises rather than returning
    quietly when there is one and it could not be copied, because every caller
    is about to do something that is only safe with a copy in hand, and the
    one correct response to not having one is to stop.

    A board that reads cleanly is copied consistently, as a recovery point. One
    that does not is copied byte for byte as evidence, which is never restored
    from. One that could not be judged -- locked, say -- is copied consistently
    if SQLite can read it through, and as evidence if not.
    """
    live = database_path(app_root)
    if not live.exists():
        return None
    folder = backups_dir(app_root)
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BackupError(f"could not create the backups folder: {exc}") from exc

    state = classify(live, live=True)
    if state != DAMAGED:
        try:
            return _take(folder, evidence=False, now=now, write=lambda partial: _copy_consistent(live, partial, live=True))
        except BackupError:
            if state == GOOD:
                raise
    return _take(folder, evidence=True, now=now, write=lambda partial: _copy_raw(live, partial))


@dataclass(frozen=True)
class _Copy:
    path: Path
    key: tuple[str, int]
    state: str
    weight: int = 0


def _backups(app_root: Path) -> list[_Copy]:
    """Every recovery point this module made, newest first, each checked.

    Ordered by the time in the name, which this module wrote and nothing else
    changes -- not by the file's modification time, which a copy, a restore
    from Time Machine or a sync tool can all rewrite.
    """
    copies = []
    for key, path in _named(backups_dir(app_root), BACKUP_PATTERN):
        state = classify(path)
        copies.append(_Copy(path, key, state, _weight(path) if state == GOOD else 0))
    return copies


def _evidence(app_root: Path) -> list[_Copy]:
    """Every evidence copy, newest first. Damaged by definition, whatever it reads as."""
    return [_Copy(path, key, DAMAGED) for key, path in _named(backups_dir(app_root), EVIDENCE_PATTERN)]


def prune(
    app_root: Path,
    *,
    keep_good: int = KEEP_GOOD,
    keep_damaged: int = KEEP_DAMAGED,
    keep: tuple[Path, ...] | list[Path] = (),
) -> list[Path]:
    """Remove old copies, conservatively. Returns what was removed.

    Keeps the ``keep_good`` newest recovery points and the ``keep_damaged``
    newest damaged copies and evidence, and never removes:

    * the newest recovery point, whatever the counts say;
    * the recovery point holding the most homes, until a newer one holds as
      many -- so a run of copies of an emptied board cannot push out the last
      one that had everything in it;
    * anything in ``keep`` -- the copy just taken, above all;
    * any copy whose state could not be established;
    * any file this module did not name.

    With no recovery point at all, nothing is removed -- not even evidence,
    since it may be the only material left to recover anything from.
    """
    if keep_good < 1:
        raise ValueError("keep_good must be at least 1: the newest good copy is never pruned")
    kept = {Path(path) for path in keep}
    copies = _backups(app_root)
    good = [copy for copy in copies if copy.state == GOOD]
    if not good:
        return []
    damaged = sorted(
        [copy for copy in copies if copy.state == DAMAGED] + _evidence(app_root),
        key=lambda copy: copy.key,
        reverse=True,
    )
    fullest = max(good, key=lambda copy: (copy.weight, copy.key))
    # ``good[keep_good:]`` with ``keep_good`` at least 1 can never reach
    # ``good[0]``, so the newest recovery point is outside this list by
    # construction; an ``unknown`` copy is in neither list and so is never here.
    doomed = [copy for copy in good[keep_good:] if copy.path != fullest.path and copy.path not in kept]
    doomed += [copy for copy in damaged[max(keep_damaged, 0):] if copy.path not in kept]
    return [copy.path for copy in doomed if _discard_set(copy.path)]


@dataclass(frozen=True)
class Restoration:
    """What ``restore_if_unreadable`` did, in words it can say to a person."""

    outcome: str  # "healthy", "absent", "restored", "no_good_backup", "failed"
    restored_from: Path | None = None
    kept_damaged_as: Path | None = None
    detail: str = ""


def restore_if_unreadable(app_root: Path, *, now: float | None = None) -> Restoration:
    """Put the newest good backup back -- only if the live database cannot be read.

    Never touches a database that reads. Repair is what people run when anything
    at all is wrong, and a backup restored over a working board would throw away
    every home found since it was taken.

    When it does act, the live file is never moved and never deleted. The
    backup is copied beside it and proved to read; a complete byte-for-byte copy
    of the unreadable set -- file, log and all -- is kept as evidence in the
    backups folder; the unreadable file's own log is cleared so it cannot be
    paired with the restored file; and the proven copy replaces the file in one
    atomic rename. There is no moment at which the app could start, find no
    database and make an empty one, and if any step fails everything needed to
    put the set back is still there.

    The caller must stop the app first. A process still writing could otherwise
    add to the old file after its copy was taken.
    """
    live = database_path(app_root)
    if not live.exists():
        return Restoration("absent")
    if classify(live, live=True) != DAMAGED:
        # Healthy, or impossible to judge: in neither case is replacing it safe.
        return Restoration("healthy")

    candidates = [copy for copy in _backups(app_root) if copy.state == GOOD]
    if not candidates:
        return Restoration(
            "no_good_backup",
            detail="The housing database could not be read and no readable backup was found. "
            f"Nothing was deleted. Keep {live} and ask for help before removing anything.",
        )
    source = candidates[0].path
    staged = live.with_name(f"{DATABASE_NAME}.restoring")
    _discard_set(staged)
    try:
        _copy_consistent(source, staged, live=False)
        if classify(staged) != GOOD:
            raise BackupError("the restored copy did not read back cleanly")
    except (OSError, sqlite3.Error, BackupError) as exc:
        _discard_set(staged)
        return Restoration("failed", detail=f"The backup could not be restored ({exc}). Nothing was changed.")

    try:
        # The copy snapshot() has just made, when nothing has changed since.
        kept = _take(backups_dir(app_root), evidence=True, now=now, write=lambda partial: _copy_raw(live, partial))
    except BackupError as exc:
        _discard_set(staged)
        return Restoration(
            "failed", detail=f"A copy of the unreadable file could not be kept ({exc}). Nothing was changed."
        )

    cleared: list[str] = []
    try:
        for suffix in _SIDECARS:
            sidecar = Path(f"{live}{suffix}")
            if sidecar.exists():
                sidecar.unlink()
                cleared.append(suffix)
        staged.replace(live)
    except OSError as exc:
        # Put the log back beside the file it belongs to, from the copy. Even if
        # that fails, nothing is lost: the copy holds the whole set.
        for suffix in cleared:
            try:
                shutil.copy2(Path(f"{kept}{suffix}"), Path(f"{live}{suffix}"))
            except OSError:
                pass
        _discard_set(staged)
        return Restoration(
            "failed",
            detail=f"The backup could not be put in place ({exc}). Nothing was lost: the unreadable "
            f"database is still at {live}, and a complete copy of it is at {kept}.",
        )

    return Restoration("restored", restored_from=source, kept_damaged_as=kept)


def _say(message: str) -> None:
    print(message, flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m sf_housing.backups",
        description="Safety copies of the housing database, and the way back to them.",
    )
    parser.add_argument("command", choices=("protect", "repair"))
    parser.add_argument("app_root", type=Path)
    args = parser.parse_args(argv)

    # Both commands start with a safety copy, and neither goes on without one --
    # whatever the reason it could not be taken.
    try:
        taken = snapshot(args.app_root)
    except Exception as exc:  # noqa: BLE001 - no copy, for any reason, means stop
        reason = str(exc) if isinstance(exc, BackupError) else f"{type(exc).__name__}: {exc}"
        _say(f"Could not take a safety copy of your data ({reason}). Nothing was changed.")
        return 2

    status = 0
    kept: list[Path] = [taken] if taken is not None else []
    if args.command == "repair":
        try:
            result = restore_if_unreadable(args.app_root)
        except Exception as exc:  # noqa: BLE001 - said, never swallowed into "all fine"
            result = Restoration(
                "failed", detail=f"The backup could not be restored ({type(exc).__name__}: {exc}). Nothing was deleted."
            )
        if result.outcome == "restored":
            _say(f"The housing database could not be read. Restored the backup from {result.restored_from.name}.")
            _say(f"A copy of the unreadable file was kept at backups/{result.kept_damaged_as.name}")
            kept += [result.restored_from, result.kept_damaged_as]
        elif result.outcome in {"no_good_backup", "failed"}:
            _say(result.detail)
            status = 3

    # Pruning only beside a copy just taken of a board that is still there. With
    # no live database the backups are the only copies there are, and any one
    # of them might hold something none of the others does.
    if taken is not None:
        try:
            removed = prune(args.app_root, keep=kept)
        except Exception as exc:  # noqa: BLE001 - tidying is never worth failing over
            _say(f"Old backups could not be tidied this time ({type(exc).__name__}: {exc}). Nothing else was affected.")
        else:
            if removed:
                _say(f"Kept the newest backups and removed {len(removed)} older ones.")
    return status


if __name__ == "__main__":
    sys.exit(main())
