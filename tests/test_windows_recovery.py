"""The Windows Repair and installer, run -- not read -- against a throwaway app.

Windows Repair used to be one line: run the installer. It never restored
anything, while the app's own error told people it would restore the most
recent backup. And the Windows installer's backup was a copy with the old
runtime's Python whose exit code nobody read: skipped whenever that runtime was
the broken thing, carried on regardless when it failed, and never pruned.

Nobody working on this owns a Windows PC, so these run the real scripts under
PowerShell with the parts of Windows a Mac does not have standing in:
``schtasks.exe`` and ``uv.exe`` are small shell scripts that log what they were
asked to do, and the health check answers whatever the test says. Everything
that touches the data -- the scripts' own logic and ``sf_housing.backups`` --
is the real code that ships.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from sf_housing.backups import BACKUP_PATTERN, DAMAGED, GOOD, classify

ROOT = Path(__file__).resolve().parents[1]
WINDOWS_PAYLOAD = ROOT / "release_assets" / "windows" / "payload"
PWSH = shutil.which("pwsh")
VERSION = re.search(
    r"^\$Version = '([^']+)'", (WINDOWS_PAYLOAD / "install.ps1").read_text(encoding="utf-8"), re.M
).group(1)

pytestmark = [
    pytest.mark.skipif(PWSH is None, reason="needs PowerShell to run the Windows scripts"),
    pytest.mark.skipif(sys.platform == "win32", reason="the stand-ins are shell scripts"),
]


# --------------------------------------------------------------------------
# A Windows machine, near enough
# --------------------------------------------------------------------------

# schtasks.exe: logs the verb and a fingerprint of the live database at that
# moment, so a test can tell whether the app was stopped before the file was
# swapped. /End complains on stderr the way the real one does for a task that
# is not running, which Windows PowerShell 5.1 turns into an error.
FAKE_SCHTASKS = """#!/bin/sh
db="$LOCALAPPDATA/SF Housing Monitor/data/housing.sqlite3"
state=missing
[ -f "$db" ] && state=$(/usr/bin/shasum -a 256 "$db" | /usr/bin/cut -c1-16)
echo "schtasks $1 $state" >> "$FAKE_LOG"
if [ "$1" = "/End" ]; then
  echo 'ERROR: The scheduled task "SF Housing Monitor" is not running.' >&2
  exit 1
fi
exit 0
"""

# uv.exe: builds a real virtual environment and installs the wheel into it by
# unpacking, which is all the installer needs from it here. Its own Pythons run
# isolated, because the real uv is not Python and no PYTHONPATH reaches it.
FAKE_UV = """#!/bin/sh
echo "uv $*" >> "$FAKE_LOG"
case "$1 $2" in
  "python install") [ "${FAKE_UV_OFFLINE:-0}" = 1 ] && exit 1; exit 0 ;;
  "venv "*)
    "$FAKE_UV_PYTHON" -I -m venv --without-pip "$2" || exit 1
    /bin/mkdir -p "$2/Scripts"
    /bin/cp "$FAKE_PYTHON_EXE" "$2/Scripts/python.exe"
    exit 0 ;;
  "pip sync") exit 0 ;;
  "pip install")
    exec "$5" -I -c 'import sys, sysconfig, zipfile; zipfile.ZipFile(sys.argv[1]).extractall(sysconfig.get_paths()["purelib"])' "$3" ;;
esac
exit 0
"""

# Scripts\\python.exe in a venv, as a script that finds the venv's own Python.
PYTHON_EXE = '#!/bin/sh\nexec "$(dirname "$0")/../bin/python" "$@"\n'

# The installer, when all a test wants to know is whether Repair reached it,
# and what the database looked like when it did.
FAKE_INSTALLER = r"""param([string]$ReleaseRoot)
$db = [IO.Path]::Combine($env:LOCALAPPDATA, 'SF Housing Monitor', 'data', 'housing.sqlite3')
$state = if (Test-Path -LiteralPath $db) { (Get-FileHash -LiteralPath $db -Algorithm SHA256).Hash.Substring(0, 16).ToLowerInvariant() } else { 'missing' }
Add-Content -LiteralPath $env:FAKE_LOG -Value "install $state"
exit [int]$env:FAKE_INSTALL_EXIT
"""

# Runs install.ps1 with the cmdlets a Mac cannot answer for standing in: the
# health check says what FAKE_HEALTHY says, and nothing sleeps.
INSTALL_DRIVER = r"""param([string]$Installer, [string]$ReleaseRoot)
function global:Invoke-RestMethod {
  param($Uri, $TimeoutSec)
  if ($env:FAKE_HEALTHY -eq '1') { return [pscustomobject]@{ app = 'sf-home-finder'; ok = $true } }
  throw 'nothing is listening'
}
function global:Start-Sleep { param($Seconds, $Milliseconds) }
function global:Unblock-File { [CmdletBinding()] param([Parameter(ValueFromPipeline = $true)]$InputObject) process { } }
try {
  & $Installer -ReleaseRoot $ReleaseRoot
  exit 0
} catch {
  Write-Host $_.Exception.Message
  exit 1
}
"""


def _fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16] if path.exists() else "missing"


def _board(path: Path, homes: list[str]) -> Path:
    """A WAL-mode board holding these homes, big enough that damage lands on data."""
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE IF NOT EXISTS homes (name TEXT PRIMARY KEY, note TEXT)")
    connection.executemany(
        "INSERT OR REPLACE INTO homes VALUES (?, ?)",
        [(name, f"note on {name}") for name in homes + [f"filler-{n}" for n in range(400)]],
    )
    connection.commit()
    connection.close()
    return path


def _homes(path: Path) -> set[str]:
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        return {row[0] for row in connection.execute("SELECT name FROM homes") if not row[0].startswith("filler-")}
    finally:
        connection.close()


def _damage(path: Path) -> None:
    size = path.stat().st_size
    with path.open("r+b") as handle:
        handle.seek(size // 2)
        handle.write(os.urandom(4096))
    assert classify(path, live=True) == DAMAGED, "the fixture did not actually damage the board"


def _backup(app: Path, stamp: str, homes: list[str]) -> Path:
    path = app / "backups" / f"housing-{stamp}.sqlite3"
    _board(path, homes)
    return path


def _decoy(tmp_path: Path) -> Path:
    """A folder like one a developer or another app leaves on PYTHONPATH.

    Its ``sitecustomize`` runs at the start of any Python that reads the
    environment and ends it, and its ``sf_housing.backups`` claims success
    without copying anything -- so only a Python run isolated from the
    environment gets the real rules, and gets to run them at all.
    """
    decoy = tmp_path / "decoy"
    (decoy / "sf_housing").mkdir(parents=True)
    (decoy / "sitecustomize.py").write_text("raise SystemExit(99)\n", encoding="utf-8")
    (decoy / "sf_housing" / "__init__.py").write_text(f'__version__ = "{VERSION}"\n', encoding="utf-8")
    (decoy / "sf_housing" / "backups.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    return decoy


def _good_backups(app: Path) -> list[Path]:
    """Recovery points that read, oldest first -- not evidence, whatever it reads as."""
    return sorted(
        (p for p in (app / "backups").iterdir() if BACKUP_PATTERN.match(p.name) and classify(p) == GOOD),
        key=lambda p: (BACKUP_PATTERN.match(p.name).group(1), int(BACKUP_PATTERN.match(p.name).group(2) or 1)),
    )


@pytest.fixture(scope="module")
def old_python(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A Python like the one an older install left behind.

    A real virtual environment with an ``sf_housing`` of its own that predates
    ``sf_housing.backups`` -- so a Repair that let the installed app's package
    answer, instead of the release's wheel, fails every test here.
    """
    venv = tmp_path_factory.mktemp("installed-runtime") / "venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True)
    purelib = subprocess.run(
        [str(venv / "bin" / "python"), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    package = Path(purelib) / "sf_housing"
    package.mkdir()
    (package / "__init__.py").write_text('__version__ = "0.5.5"\n', encoding="utf-8")
    probe = subprocess.run(
        [str(venv / "bin" / "python"), "-I", "-c", "import sf_housing.backups"],
        capture_output=True, text=True, cwd=tmp_path_factory.getbasetemp(),
    )
    assert probe.returncode != 0, "the stand-in for an older install already has the new rules"
    return venv / "bin" / "python"


def _app_process(runtime: Path) -> subprocess.Popen[bytes]:
    """A running process that looks, to Get-Process, like this install's app.

    Get-Process finds the app by name and by where its program lives, so the
    stand-in has to be a real program copied under the app folder -- a copy of
    this Python, with the library it loads beside it. (A copy of a system
    program like /bin/sleep is killed by macOS on sight, and a test built on
    one passed without anything ever running.)
    """
    import sysconfig

    program = runtime / "Scripts" / "python"
    program.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(os.path.realpath(sys.executable), program)
    libdir = Path(sysconfig.get_config_var("LIBDIR") or Path(sys.base_prefix) / "lib")
    (runtime / "lib").mkdir(exist_ok=True)
    for library in libdir.glob("libpython*"):
        (runtime / "lib" / library.name).symlink_to(library)
    app = subprocess.Popen(
        [str(program), "-c", "import time; time.sleep(120)"],
        env={"PYTHONHOME": sys.base_prefix, "PATH": "/usr/bin:/bin"},
    )
    try:
        app.wait(timeout=1)
    except subprocess.TimeoutExpired:
        return app
    raise AssertionError(f"the stand-in for the app did not stay up (exit {app.returncode})")


class Machine:
    """A Windows account with SF Home Finder installed, in a temporary folder."""

    def __init__(self, tmp_path: Path, old_python: Path) -> None:
        self.tmp = tmp_path
        self.local = tmp_path / "LocalAppData"
        self.app = self.local / "SF Housing Monitor"
        self.live = self.app / "data" / "housing.sqlite3"
        self.log = tmp_path / "calls.log"
        self.bin = tmp_path / "bin"
        self.release = tmp_path / "SF-Home-Finder-Windows"
        self.old_python = old_python
        (self.app / "data").mkdir(parents=True)
        (self.app / "backups").mkdir()
        self.bin.mkdir()
        self._script(self.bin / "schtasks.exe", FAKE_SCHTASKS)
        self._script(tmp_path / "python.exe.template", PYTHON_EXE)
        self.install_runtime()

        payload = self.release / "payload"
        shutil.copytree(WINDOWS_PAYLOAD / "tools", payload / "tools")
        with zipfile.ZipFile(payload / f"sf_home_finder-{VERSION}-py3-none-any.whl", "w") as wheel:
            for name in ("__init__.py", "backups.py"):
                wheel.write(ROOT / "sf_housing" / name, f"sf_housing/{name}")
        (payload / "requirements.lock").write_text("", encoding="utf-8")
        (tmp_path / "driver.ps1").write_text(INSTALL_DRIVER, encoding="utf-8")

    @staticmethod
    def _script(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        path.chmod(0o755)

    def install_runtime(self) -> None:
        """What an earlier install left in ``current``: a working Python."""
        self._script(self.app / "current" / "Scripts" / "python.exe", f'#!/bin/sh\nexec "{self.old_python}" "$@"\n')
        (self.app / "current" / "OLD-RUNTIME").write_text("the version being replaced", encoding="utf-8")

    def use_fake_installer(self) -> None:
        (self.release / "payload" / "install.ps1").write_text(FAKE_INSTALLER, encoding="utf-8")

    def use_real_installer(self) -> None:
        payload = self.release / "payload"
        shutil.copy(WINDOWS_PAYLOAD / "install.ps1", payload / "install.ps1")
        self._script(self.app / "uv" / "0.10.8" / "uv.exe", FAKE_UV)
        lines = []
        for path in sorted(p for p in payload.rglob("*") if p.is_file() and p.name != "checksums.sha256"):
            lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(payload).as_posix()}")
        (payload / "checksums.sha256").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def env(self, **extra: str) -> dict[str, str]:
        return {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": str(self.tmp / "home"),
            "LOCALAPPDATA": str(self.local),
            "PROCESSOR_ARCHITECTURE": "AMD64",
            "SF_HOUSING_NO_BROWSER": "1",
            "FAKE_LOG": str(self.log),
            "FAKE_INSTALL_EXIT": "0",
            "FAKE_HEALTHY": "1",
            "FAKE_UV_PYTHON": sys.executable,
            "FAKE_PYTHON_EXE": str(self.tmp / "python.exe.template"),
            **extra,
        }

    # Both run from a folder with no sf_housing in it, as a person runs them --
    # not from this repo, whose own package would answer for any Python that
    # looks in the current folder, and hide the fault the tests are after.
    def repair(self, **extra: str) -> subprocess.CompletedProcess[str]:
        # Given the way the .cmd launcher gives it: the release folder with
        # "\\." on the end, so a trailing backslash never meets a quote.
        return subprocess.run(
            [PWSH, "-NoLogo", "-NoProfile", "-File",
             str(self.release / "payload" / "tools" / "repair.ps1"), "-ReleaseRoot", f"{self.release}/."],
            capture_output=True, text=True, env=self.env(**extra), timeout=120, cwd=self.tmp,
        )

    def install(self, **extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [PWSH, "-NoLogo", "-NoProfile", "-File", str(self.tmp / "driver.ps1"),
             "-Installer", str(self.release / "payload" / "install.ps1"), "-ReleaseRoot", str(self.release)],
            capture_output=True, text=True, env=self.env(**extra), timeout=180, cwd=self.tmp,
        )

    def calls(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []


@pytest.fixture
def machine(tmp_path: Path, old_python: Path) -> Machine:
    return Machine(tmp_path, old_python)


# --------------------------------------------------------------------------
# Repair
# --------------------------------------------------------------------------


def test_windows_repair_puts_the_last_good_backup_back(machine: Machine) -> None:
    """The regression: the app's error says Repair restores the most recent
    backup, and on Windows Repair only reinstalled -- somebody who followed the
    instruction got the same dead app back."""
    machine.use_fake_installer()
    _board(machine.live, ["found-this-week"])
    _backup(machine.app, "20260901-120000", ["saved-home"])
    _damage(machine.live)
    damaged = _fingerprint(machine.live)

    result = machine.repair()

    assert result.returncode == 0, result.stdout + result.stderr
    assert _homes(machine.live) == {"saved-home"}
    assert "Restored the backup from housing-20260901-120000.sqlite3" in result.stdout
    kept = list((machine.app / "backups").glob("housing-unreadable-*.sqlite3"))
    assert len(kept) == 1 and _fingerprint(kept[0]) == damaged, "the unreadable file was not kept as it was"
    # Stopped before the file was swapped, and reinstalled after it.
    assert machine.calls()[0] == f"schtasks /End {damaged}", machine.calls()
    assert machine.calls()[-1] == f"install {_fingerprint(machine.live)}", machine.calls()


def test_windows_repair_leaves_a_working_board_alone(machine: Machine) -> None:
    """Repair is what people run when anything at all is wrong; a backup put
    back over a board that reads would throw away every home found since."""
    machine.use_fake_installer()
    _board(machine.live, ["found-today"])
    _backup(machine.app, "20260101-000000", ["stale"])
    writer = sqlite3.connect(machine.live)
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("INSERT INTO homes VALUES ('only-in-the-log', 'written a moment ago')")
    writer.commit()
    try:
        result = machine.repair()
    finally:
        writer.close()

    assert result.returncode == 0, result.stdout + result.stderr
    assert _homes(machine.live) == {"found-today", "only-in-the-log"}
    newest = _good_backups(machine.app)[-1]
    assert "only-in-the-log" in _homes(newest), "the safety copy lost what was only in the write-ahead log"
    assert not list((machine.app / "backups").glob("housing-unreadable-*"))


def test_windows_repair_runs_this_release_s_rules_not_the_installed_app_s(machine: Machine) -> None:
    """The installed runtime here has an ``sf_housing`` that predates the
    backup rules, so the restore can only have come from the release's wheel."""
    machine.use_fake_installer()
    _board(machine.live, ["x"])
    _backup(machine.app, "20260901-120000", ["saved-home"])
    _damage(machine.live)

    # And nothing in the environment gets to stand in for it either.
    result = machine.repair(PYTHONPATH=str(_decoy(machine.tmp)))

    assert result.returncode == 0, result.stdout + result.stderr
    assert _homes(machine.live) == {"saved-home"}


def test_windows_repair_passes_over_a_python_that_does_not_work(machine: Machine) -> None:
    """A broken ``current`` is one of the commonest reasons to run Repair, so
    its Python is tried and not trusted: here it fails the way a venv launcher
    does when its base interpreter has gone, and Repair falls back to the
    private Python the installer downloaded -- past the venv launcher template
    inside it, which fails the same way."""
    machine.use_fake_installer()
    broken = "#!/bin/sh\necho 'failed to locate pyvenv.cfg' >&2\nexit 101\n"
    Machine._script(machine.app / "current" / "Scripts" / "python.exe", broken)
    managed = machine.app / "python" / "cpython-3.12.10-windows-x86_64-none"
    Machine._script(managed / "Lib" / "venv" / "scripts" / "nt" / "python.exe", broken)
    Machine._script(managed / "python.exe", f'#!/bin/sh\nexec "{machine.old_python}" "$@"\n')
    _board(machine.live, ["x"])
    _backup(machine.app, "20260901-120000", ["saved-home"])
    _damage(machine.live)

    result = machine.repair()

    assert result.returncode == 0, result.stdout + result.stderr
    assert _homes(machine.live) == {"saved-home"}


def test_windows_repair_with_no_way_to_run_its_rules_leaves_the_data_exactly_as_it_is(
    machine: Machine,
) -> None:
    machine.use_fake_installer()
    shutil.rmtree(machine.app / "current")
    _board(machine.live, ["x"])
    _backup(machine.app, "20260901-120000", ["saved-home"])
    _damage(machine.live)
    before = _fingerprint(machine.live)
    backups_before = sorted(p.name for p in (machine.app / "backups").iterdir())

    result = machine.repair()

    assert "left exactly as it is" in result.stdout
    assert _fingerprint(machine.live) == before
    assert sorted(p.name for p in (machine.app / "backups").iterdir()) == backups_before
    assert machine.calls() == [f"install {before}"], "the reinstall should still run"


def test_windows_repair_says_so_when_checking_the_data_fails(machine: Machine) -> None:
    """A crash in the rules is not "your data is fine". It is said, the data is
    left alone, and the app files are still repaired."""
    machine.use_fake_installer()
    wheel = next((machine.release / "payload").glob("sf_home_finder-*.whl"))
    with zipfile.ZipFile(wheel, "w") as broken:
        broken.writestr("sf_housing/__init__.py", "")
        broken.writestr("sf_housing/backups.py", "def main(argv):\n    raise RuntimeError('boom')\n")
    _board(machine.live, ["x"])
    _damage(machine.live)
    before = _fingerprint(machine.live)

    result = machine.repair()

    assert "stopped with an error" in result.stdout, result.stdout + result.stderr
    assert _fingerprint(machine.live) == before
    assert machine.calls()[-1] == f"install {before}", machine.calls()


def test_windows_repair_that_cannot_take_a_safety_copy_goes_no_further(machine: Machine) -> None:
    """No copy, no restore and no reinstall -- and the app it stopped is
    started again rather than left off until the next sign-in."""
    machine.use_fake_installer()
    _board(machine.live, ["x"])
    shutil.rmtree(machine.app / "backups")
    (machine.app / "backups").write_text("a file where the folder should be", encoding="utf-8")
    before = _fingerprint(machine.live)

    result = machine.repair()

    assert result.returncode == 2, result.stdout + result.stderr
    assert "Could not take a safety copy" in result.stdout
    assert _fingerprint(machine.live) == before
    assert [call.split()[1] for call in machine.calls()] == ["/End", "/Run"], machine.calls()


def test_windows_repair_waits_for_the_app_itself_to_stop_before_touching_the_data(machine: Machine) -> None:
    """schtasks /End asks and does not wait, and an app started some other way
    has no task to end. A process of this install's that will not go is ended,
    as the installer ends one, before the data is touched. (Slow on purpose:
    Repair gives it ten seconds to go by itself first.)"""
    machine.use_fake_installer()
    _board(machine.live, ["x"])
    _backup(machine.app, "20260901-120000", ["saved-home"])
    _damage(machine.live)
    # Started some other way: there is no task for schtasks.exe to end.
    (machine.bin / "schtasks.exe").unlink()
    app = _app_process(machine.app / "current")
    try:
        result = machine.repair()
        assert app.wait(timeout=5) is not None, "the app was left running"
    finally:
        if app.poll() is None:
            app.kill()
    assert result.returncode == 0, result.stdout + result.stderr
    assert _homes(machine.live) == {"saved-home"}


def test_windows_repair_starts_the_app_again_when_the_reinstall_fails(machine: Machine) -> None:
    machine.use_fake_installer()
    _board(machine.live, ["x"])

    result = machine.repair(FAKE_INSTALL_EXIT="1")

    assert result.returncode == 1
    steps = ["install" if call.startswith("install") else call.split()[1] for call in machine.calls()]
    assert steps[-2:] == ["install", "/Run"], machine.calls()


def test_windows_repair_clicked_again_and_again_keeps_the_way_back(machine: Machine) -> None:
    """A board that keeps breaking: every click snapshots the damage, and the
    one good backup from before the trouble must still be there, and still be
    what is restored, every time -- while the folder stays bounded."""
    machine.use_fake_installer()
    _board(machine.live, ["x"])
    for day in range(1, 16):
        _backup(machine.app, f"202608{day:02d}-120000", [f"home-{day}"])
    for click in range(3):
        _damage(machine.live)
        result = machine.repair()
        assert result.returncode == 0, f"click {click + 1}: {result.stdout}{result.stderr}"
        assert _homes(machine.live) == {"home-15"}, f"click {click + 1} restored the wrong copy"

    assert len(_good_backups(machine.app)) == 10
    assert (machine.app / "backups" / "housing-20260815-120000.sqlite3").exists()


# --------------------------------------------------------------------------
# The installer
# --------------------------------------------------------------------------


def test_windows_install_takes_a_safety_copy_and_bounds_the_folder(machine: Machine) -> None:
    """The regression: every install added a full-size copy and nothing ever
    removed one. Bounded now, by the same rules as Repair; the newest copy has
    everything, including what was still in the write-ahead log; and a file in
    the folder the app did not name is left alone."""
    machine.use_real_installer()
    _board(machine.live, ["found-today"])
    for day in range(1, 26):
        _backup(machine.app, f"202608{day:02d}-120000", [f"home-{day}"])
    theirs = machine.app / "backups" / "my-own-copy.sqlite3"
    _board(theirs, ["mine"])
    writer = sqlite3.connect(machine.live)
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("INSERT INTO homes VALUES ('only-in-the-log', 'written a moment ago')")
    writer.commit()
    try:
        result = machine.install()
    finally:
        writer.close()

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Installed." in result.stdout
    good = _good_backups(machine.app)
    assert len(good) == 10, [p.name for p in good]
    assert "only-in-the-log" in _homes(good[-1]), "the safety copy lost what was only in the write-ahead log"
    assert theirs.exists(), "a file the app did not name was pruned"
    assert _homes(machine.live) == {"found-today", "only-in-the-log"}
    assert not (machine.app / "current" / "OLD-RUNTIME").exists(), "the install did not finish"
    verbs = [call.split()[1] for call in machine.calls() if call.startswith("schtasks")]
    assert verbs == ["/End", "/Create", "/Run"], machine.calls()


def test_windows_install_is_not_fooled_by_a_python_setting_in_the_environment(machine: Machine) -> None:
    """A PYTHONPATH left by other software reached the safety copy: its own
    ``sf_housing.backups`` could answer "done" without copying anything, and the
    install would go on to replace the app with no copy taken."""
    machine.use_real_installer()
    _board(machine.live, ["found-today"])

    result = machine.install(PYTHONPATH=str(_decoy(machine.tmp)))

    assert result.returncode == 0, result.stdout + result.stderr
    good = _good_backups(machine.app)
    assert len(good) == 1 and _homes(good[0]) == {"found-today"}


def test_windows_install_takes_a_copy_even_when_the_old_runtime_is_broken(machine: Machine) -> None:
    """The regression: the copy was made with the *old* runtime's Python and
    skipped when it was missing -- which is exactly when somebody reinstalls."""
    machine.use_real_installer()
    shutil.rmtree(machine.app / "current")
    _board(machine.live, ["found-today"])

    result = machine.install()

    assert result.returncode == 0, result.stdout + result.stderr
    good = _good_backups(machine.app)
    assert len(good) == 1 and _homes(good[0]) == {"found-today"}


def test_windows_install_that_cannot_back_up_changes_nothing(machine: Machine) -> None:
    """The regression: a failed copy was not even noticed. Now it stops the
    install before the old version is touched, says why, and starts the app
    it stopped again."""
    machine.use_real_installer()
    _board(machine.live, ["found-today"])
    shutil.rmtree(machine.app / "backups")
    (machine.app / "backups").write_text("a file where the folder should be", encoding="utf-8")
    before = _fingerprint(machine.live)

    result = machine.install()

    assert result.returncode == 1
    assert "could not be backed up first, so nothing was changed" in result.stdout
    assert (machine.app / "current" / "OLD-RUNTIME").exists(), "the old version was replaced anyway"
    assert _fingerprint(machine.live) == before
    verbs = [call.split()[1] for call in machine.calls() if call.startswith("schtasks")]
    assert verbs == ["/End", "/Run"], machine.calls()


def test_an_upgrade_that_fails_offline_starts_the_old_app_again(machine: Machine) -> None:
    """Windows stops the running app before downloading anything, so an
    upgrade on a dropped connection used to leave nothing running until the
    next sign-in. The old version is untouched at that point; it is started
    again."""
    machine.use_real_installer()
    _board(machine.live, ["found-today"])

    result = machine.install(FAKE_UV_OFFLINE="1")

    assert result.returncode == 1
    assert "Check your internet connection" in result.stdout
    assert (machine.app / "current" / "OLD-RUNTIME").exists()
    verbs = [call.split()[1] for call in machine.calls() if call.startswith("schtasks")]
    assert verbs == ["/End", "/Run"], machine.calls()


def test_a_first_install_has_nothing_to_copy_and_copies_nothing(machine: Machine) -> None:
    machine.use_real_installer()
    shutil.rmtree(machine.app / "current")

    result = machine.install(FAKE_HEALTHY="1")

    assert result.returncode == 0, result.stdout + result.stderr
    assert not list((machine.app / "backups").glob("housing-*.sqlite3"))


# --------------------------------------------------------------------------
# What only reading can check
# --------------------------------------------------------------------------


def test_no_windows_script_lets_a_complaint_on_stderr_end_it() -> None:
    """Windows PowerShell 5.1 -- what a double-clicked .cmd runs -- throws on
    any stderr output from a program whose stderr is redirected while
    ``$ErrorActionPreference`` is Stop. PowerShell 7, which runs these tests,
    does not, so this is the one thing here that has to be read: every
    redirected call is in a scope that relaxes it, or inside a ``try``.

    The case that matters: ``schtasks.exe`` saying the task does not exist,
    which on 5.1 ended Uninstall before it started, Open before it waited, and
    would have ended Repair before it looked at the data.
    """
    offenders = []
    for script in sorted((ROOT / "release_assets" / "windows").rglob("*.ps1")):
        lines = script.read_text(encoding="utf-8").splitlines()
        in_quiet_helper = False
        for number, line in enumerate(lines):
            if line.startswith("function Invoke-Quietly"):
                in_quiet_helper = True
            elif in_quiet_helper and line.startswith("}"):
                in_quiet_helper = False
            if "2>" not in line:
                continue
            before = [earlier.strip() for earlier in lines[max(0, number - 2):number]]
            relaxed = (
                "$ErrorActionPreference = 'Continue'" in line
                or in_quiet_helper
                or "try {" in before
                or "$ErrorActionPreference = 'Continue'" in before
            )
            if not relaxed:
                offenders.append(f"{script.name}:{number + 1}: {line.strip()}")
    assert not offenders, "redirected stderr under Stop ends the script on 5.1:\n" + "\n".join(offenders)
    repair = (WINDOWS_PAYLOAD / "tools" / "repair.ps1").read_text(encoding="utf-8")
    assert "function Invoke-Quietly" in repair, "nothing relaxes stderr handling for the programs Repair runs"
    helper = repair[repair.index("function Invoke-Quietly"):]
    helper = helper[: helper.index("\n}\n")]
    assert "$ErrorActionPreference = 'Continue'" in helper, "the helper Repair trusts to relax stderr does not"


def test_the_windows_launchers_never_end_a_quoted_folder_in_a_backslash() -> None:
    """``"%~dp0"`` ends in a backslash, and by the rules every Windows program
    uses to split its command line, a backslash before a closing quote escapes
    it: the script receives the folder with a stray ``"`` on the end, and
    finds nothing inside it. ``"%~dp0."`` names the same folder safely."""
    for launcher in (ROOT / "release_assets" / "windows").glob("*.cmd"):
        text = launcher.read_text(encoding="utf-8")
        assert '"%~dp0"' not in text, f'{launcher.name} passes "%~dp0" as an argument'
