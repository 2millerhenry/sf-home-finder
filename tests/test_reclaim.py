"""Giving back the disk an upgrade leaves behind, without giving back anything else.

Every upgrade builds a fresh Python environment in runtimes/<version> and none
were ever removed: five versions had accumulated 772MB of environments nobody
could run. These run the real script against a real directory tree, because
the whole risk here is deleting the wrong thing.
"""

from __future__ import annotations

import pathlib
import subprocess

RECLAIM = pathlib.Path(__file__).resolve().parents[1] / "release_assets/payload/tools/reclaim.sh"


def install(root: pathlib.Path, versions: list[str], current: str | None) -> None:
    """A believable app root: some runtimes, a database, and backups of it."""
    for version in versions:
        runtime = root / "runtimes" / version / "bin"
        runtime.mkdir(parents=True)
        (runtime / "python").write_text("#!/bin/sh\n", encoding="utf-8")
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "data" / "housing.sqlite3").write_text("the listings", encoding="utf-8")
    (root / "data" / "config").mkdir(exist_ok=True)
    (root / "data" / "config" / "preferences.yaml").write_text("the deal", encoding="utf-8")
    (root / "backups").mkdir(exist_ok=True)
    (root / "backups" / "housing-20260901.sqlite3").write_text("last week", encoding="utf-8")
    (root / "cache").mkdir(exist_ok=True)
    (root / "cache" / "archive-v0").mkdir(exist_ok=True)
    (root / "cache" / "archive-v0" / "big").write_text("x" * 4096, encoding="utf-8")
    if current:
        (root / "current").symlink_to(root / "runtimes" / current)


def reclaim(root: pathlib.Path) -> str:
    done = subprocess.run(
        ["/bin/bash", str(RECLAIM)],
        env={"SF_HOUSING_APP_ROOT": str(root), "HOME": str(root), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout


def runtimes(root: pathlib.Path) -> list[str]:
    return sorted(p.name for p in (root / "runtimes").iterdir())


def test_the_running_version_is_never_removed(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "app"
    root.mkdir()
    install(root, ["0.3.5", "0.3.6", "0.3.9"], current="0.3.9")

    reclaim(root)

    assert "0.3.9" in runtimes(root)
    assert (root / "current" / "bin" / "python").exists(), "the app can still be started"


def test_one_version_is_kept_to_roll_back_to(tmp_path: pathlib.Path) -> None:
    """The only reason to keep a runtime at all: an upgrade that goes bad."""
    root = tmp_path / "app"
    root.mkdir()
    install(root, ["0.3.5", "0.3.6", "0.3.7", "0.3.8", "0.3.9"], current="0.3.9")

    reclaim(root)

    assert runtimes(root) == ["0.3.8", "0.3.9"], "kept the running one and the newest spare"


def test_a_rollback_copy_survives_even_when_it_is_not_the_newest(tmp_path: pathlib.Path) -> None:
    """Running an older version after a rollback must not then delete the newer
    one it might go back to."""
    root = tmp_path / "app"
    root.mkdir()
    install(root, ["0.3.5", "0.3.8", "0.3.9"], current="0.3.8")

    reclaim(root)

    assert runtimes(root) == ["0.3.8", "0.3.9"]


def test_nothing_a_person_typed_or_collected_is_ever_touched(tmp_path: pathlib.Path) -> None:
    """The listings, the deal and every backup of the database. These live
    outside the two directories this script may reach, and that is the point."""
    root = tmp_path / "app"
    root.mkdir()
    install(root, ["0.3.5", "0.3.9"], current="0.3.9")

    reclaim(root)

    assert (root / "data" / "housing.sqlite3").read_text() == "the listings"
    assert (root / "data" / "config" / "preferences.yaml").read_text() == "the deal"
    assert (root / "backups" / "housing-20260901.sqlite3").read_text() == "last week"


def test_the_download_cache_is_emptied_but_still_there(tmp_path: pathlib.Path) -> None:
    """Everything in it came from the network and goes back to the network on
    the next upgrade, which already needs it."""
    root = tmp_path / "app"
    root.mkdir()
    install(root, ["0.3.9"], current="0.3.9")

    reclaim(root)

    assert (root / "cache").is_dir(), "uv is handed a directory that exists"
    assert list((root / "cache").iterdir()) == []


def test_nothing_is_removed_when_the_running_version_cannot_be_identified(
    tmp_path: pathlib.Path,
) -> None:
    """Without a current symlink this script cannot tell which environment is
    being served, and a guess here uninstalls the app."""
    root = tmp_path / "app"
    root.mkdir()
    install(root, ["0.3.5", "0.3.8", "0.3.9"], current=None)

    output = reclaim(root)

    assert runtimes(root) == ["0.3.5", "0.3.8", "0.3.9"], "it deleted without knowing"
    assert "could not be identified" in output


def test_a_single_runtime_install_is_left_exactly_as_it_is(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "app"
    root.mkdir()
    install(root, ["0.3.9"], current="0.3.9")

    reclaim(root)

    assert runtimes(root) == ["0.3.9"]


def installer() -> str:
    return (
        pathlib.Path(__file__).resolve().parents[1] / "release_assets/payload/install.sh"
    ).read_text(encoding="utf-8")


def test_an_upgrade_gives_the_disk_back_on_its_own() -> None:
    """Nobody is going to find a maintenance script. If reclaiming only
    happens when it is asked for, it never happens."""
    script = installer()

    # The guard as well as the call: "if false" leaves the name in the file
    # and reclaims nothing.
    assert 'if [ -x "$TOOLS_DIR/reclaim.sh" ]; then' in script
    assert '"$TOOLS_DIR/reclaim.sh"' in script


def test_a_healthy_start_is_actually_recognised_as_one() -> None:
    """The gate has to be able to pass, not only to fail safely.

    A gate that never sets its flag is still safe -- nothing is reclaimed --
    but every install then ends by saying it has not answered yet, on an app
    that is running perfectly. Safe and wrong is still wrong.
    """
    script = installer()

    # The matcher and the flag live apart: a function that answers "yes" when
    # the app does, and the one place that records it. Both have to be real,
    # because either one missing leaves a gate that can only fail.
    answers = script[script.index("answers() {") : script.index("start_it() {")]
    assert '"app":"sf-home-finder"' in answers and "return 0 ;;" in answers, (
        "a healthy response never returns success"
    )
    # Written against what the step returns rather than a particular shape of
    # shell. Starting the service and waiting for its first answer became one
    # step, so the flag is set by the arm of a case that reads the step's
    # code, and a check for the old one-line `if` failed an installer that was
    # working -- the same "safe and wrong" this test exists to catch.
    marked = script.index("0) HEALTHY=1 ;;")
    assert script.index('step "Starting it up"') < marked < script.index('if [ "$HEALTHY" != 1 ]; then')


def test_nothing_is_reclaimed_until_the_new_version_has_served_a_request() -> None:
    """Until the new runtime answers, the old one is the way back. Reclaiming
    first would delete the rollback copy on exactly the upgrades that need it.

    This used to be an ordering check against the wording of a failure message,
    which broke the moment that message changed -- and it changed because
    calling a finished install a failure was itself the bug. The guarantee is
    now structural: a start that never answered leaves before it can reach the
    reclaim, so the test looks for that exit rather than for a sentence.
    """
    script = installer()

    assert 'if [ "$HEALTHY" != 1 ]; then' in script, "there is no health gate at all"
    unhealthy_exit = script.index('if [ "$HEALTHY" != 1 ]; then')
    early_exit = script.index("exit 0", unhealthy_exit)

    assert script.index("reclaim.sh") > early_exit, (
        "the disk is reclaimed before the new version is known to work"
    )
