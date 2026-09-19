"""The macOS installer's safety copy, run -- not read -- against a throwaway app.

Every install and every update used to make its copy of the housing database
with the *old* runtime's Python, and only when that runtime still worked: so
reinstalling over a broken app -- the commonest reason to reinstall -- took no
copy at all. Nothing ever pruned, so every update added a full-size copy for
good (one real install had 26 of them, 634 MB). Both now go through
``sf_housing.backups protect``: the same rules as Repair and the Windows
installer, run with the new runtime's Python, and fatal when a copy cannot be
taken.

The real ``install.sh`` runs here. ``uv`` is a stand-in that builds a real
virtual environment and unpacks a wheel built from this repo into it, and the
health check is answered by a server in this process, so the install runs to
the end without the internet or a login service.
"""

from __future__ import annotations

import hashlib
import http.server
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import zipfile
from pathlib import Path

import pytest

from sf_housing.backups import BACKUP_PATTERN, GOOD, classify

ROOT = Path(__file__).resolve().parents[1]
PAYLOAD = ROOT / "release_assets" / "payload"
VERSION = re.search(r'^VERSION="([^"]+)"', (PAYLOAD / "install.sh").read_text(encoding="utf-8"), re.M).group(1)

pytestmark = pytest.mark.skipif(
    not (sys.platform == "darwin" and platform.machine() == "arm64"),
    reason="the installer runs on Apple Silicon Macs only",
)

# Builds a real virtual environment and installs the wheel into it by
# unpacking, which is all install.sh needs from uv. Its own Pythons run
# isolated, because the real uv is not Python and no PYTHONPATH reaches it.
FAKE_UV = """#!/bin/bash
echo "uv $*" >> "$FAKE_LOG"
case "$1 $2" in
  "python install") exit 0 ;;
  "venv "*) exec "$FAKE_UV_PYTHON" -I -m venv --without-pip "$2" ;;
  "pip sync") exit 0 ;;
  "pip install")
    exec "$5" -I -c 'import sys, sysconfig, zipfile; zipfile.ZipFile(sys.argv[1]).extractall(sysconfig.get_paths()["purelib"])' "$3" ;;
esac
exit 0
"""

# Starting the app is not what is under test, and the real open.sh would try.
FAKE_OPEN = '#!/bin/bash\necho "open.sh $*" >> "$FAKE_LOG"\n'


class _Healthy(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - the stdlib's name
        body = b'{"app":"sf-home-finder","ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def health_port():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Healthy)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def _board(path: Path, homes: list[str]) -> Path:
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


def _good_backups(app: Path) -> list[Path]:
    """Recovery points that read, oldest first -- not evidence, whatever it reads as."""
    return sorted(
        (p for p in (app / "backups").iterdir() if BACKUP_PATTERN.match(p.name) and classify(p) == GOOD),
        key=lambda p: (BACKUP_PATTERN.match(p.name).group(1), int(BACKUP_PATTERN.match(p.name).group(2) or 1)),
    )


def _fingerprint(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Mac:
    """An account with an older SF Home Finder installed, in a temporary folder."""

    def __init__(self, tmp_path: Path, port: int) -> None:
        self.tmp = tmp_path
        self.port = port
        self.app = tmp_path / "app"
        self.live = self.app / "data" / "housing.sqlite3"
        self.log = tmp_path / "calls.log"
        self.release = tmp_path / "release"
        (self.app / "data").mkdir(parents=True)
        (self.app / "backups").mkdir()

        # What the previous install left: a runtime of its own, pointed at by
        # `current`, which this install is about to replace.
        old = self.app / "runtimes" / "0.5.5"
        subprocess.run([sys.executable, "-I", "-m", "venv", "--without-pip", str(old)], check=True)
        (old / "OLD-RUNTIME").write_text("the version being replaced", encoding="utf-8")
        (self.app / "current").symlink_to(old)

        payload = self.release / "payload"
        (payload / "tools").mkdir(parents=True)
        shutil.copy(PAYLOAD / "install.sh", payload / "install.sh")
        for tool in (PAYLOAD / "tools").glob("*.sh"):
            shutil.copy(tool, payload / "tools" / tool.name)
        (payload / "tools" / "open.sh").write_text(FAKE_OPEN, encoding="utf-8")
        (payload / "uv").write_text(FAKE_UV, encoding="utf-8")
        for script in (payload / "uv", payload / "install.sh", *(payload / "tools").glob("*.sh")):
            script.chmod(0o755)
        (payload / "requirements.lock").write_text("", encoding="utf-8")
        with zipfile.ZipFile(payload / f"sf_home_finder-{VERSION}-py3-none-any.whl", "w") as wheel:
            for name in ("__init__.py", "backups.py"):
                wheel.write(ROOT / "sf_housing" / name, f"sf_housing/{name}")
        lines = [
            f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(payload).as_posix()}"
            for p in sorted(p for p in payload.rglob("*") if p.is_file())
        ]
        (payload / "checksums.sha256").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def install(self, **extra: str) -> subprocess.CompletedProcess[str]:
        # SF_HOUSING_NO_LAUNCH_AGENT keeps the installer away from launchctl,
        # and a test must never let it near: `bootout` on a plist stops the
        # real app by the label inside it. Checked after every run as well.
        env = {
            "PATH": f"/usr/bin:/bin:/usr/sbin:/sbin:{self.app / 'bin'}",
            "HOME": str(self.tmp / "home"),
            "SF_HOUSING_APP_ROOT": str(self.app),
            "SF_HOUSING_NO_LAUNCH_AGENT": "1",
            "SF_HOUSING_NO_BROWSER": "1",
            "SF_HOUSING_PORT": str(self.port),
            "FAKE_LOG": str(self.log),
            "FAKE_UV_PYTHON": sys.executable,
            **extra,
        }
        # From a folder with no sf_housing in it, as a person runs it -- not
        # from this repo, whose own package would answer for a Python that
        # looks in the current folder, and hide exactly the fault under test.
        result = subprocess.run(
            ["/bin/bash", str(self.release / "payload" / "install.sh"), str(self.release)],
            capture_output=True, text=True, env=env, timeout=180, cwd=self.tmp,
        )
        assert not (self.tmp / "home" / "Library" / "LaunchAgents").exists(), "the install reached for a login service"
        return result

    def still_on_the_old_version(self) -> bool:
        return (self.app / "current" / "OLD-RUNTIME").exists()


@pytest.fixture
def mac(tmp_path: Path, health_port: int) -> Mac:
    return Mac(tmp_path, health_port)


def test_installing_takes_a_safety_copy_and_bounds_the_folder(mac: Mac) -> None:
    """The regression: every install added a full-size copy and nothing ever
    removed one. Bounded now, by the same rules as Repair; the newest copy has
    everything, including what was only in the write-ahead log; and a file in
    the folder the app did not name is left alone."""
    _board(mac.live, ["found-today"])
    for day in range(1, 26):
        _board(mac.app / "backups" / f"housing-202608{day:02d}-120000.sqlite3", [f"home-{day}"])
    theirs = _board(mac.app / "backups" / "my-own-copy.sqlite3", ["mine"])
    writer = sqlite3.connect(mac.live)
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("INSERT INTO homes VALUES ('only-in-the-log', 'written a moment ago')")
    writer.commit()
    try:
        result = mac.install()
    finally:
        writer.close()

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Done. SF Home Finder is running." in result.stdout
    good = _good_backups(mac.app)
    assert len(good) == 10, [p.name for p in good]
    assert "only-in-the-log" in _homes(good[-1]), "the safety copy lost what was only in the write-ahead log"
    assert theirs.exists(), "a file the app did not name was pruned"
    assert _homes(mac.live) == {"found-today", "only-in-the-log"}
    assert not mac.still_on_the_old_version(), "the install did not finish"


def test_installing_over_a_broken_app_still_takes_a_copy(mac: Mac) -> None:
    """The regression: the copy was made with the old runtime's Python and
    silently skipped when it did not work -- which is exactly when somebody
    reinstalls."""
    (mac.app / "current" / "bin" / "python").unlink()
    _board(mac.live, ["found-today"])

    result = mac.install()

    assert result.returncode == 0, result.stdout + result.stderr
    good = _good_backups(mac.app)
    assert len(good) == 1 and _homes(good[0]) == {"found-today"}


def test_an_install_that_cannot_take_a_copy_changes_nothing(mac: Mac) -> None:
    """No copy, no install: stopped before the old version is touched, with
    the reason on screen."""
    _board(mac.live, ["found-today"])
    shutil.rmtree(mac.app / "backups")
    (mac.app / "backups").write_text("a file where the folder should be", encoding="utf-8")
    before = _fingerprint(mac.live)

    result = mac.install()

    assert result.returncode != 0, result.stdout
    assert "could not be backed up first, so nothing was changed" in result.stdout
    assert "Could not take a safety copy" in result.stdout, "the reason is not on screen"
    assert mac.still_on_the_old_version(), "the old version was replaced anyway"
    assert _fingerprint(mac.live) == before
    assert "open.sh" not in (mac.log.read_text(encoding="utf-8") if mac.log.exists() else "")


def test_installing_is_not_fooled_by_a_python_setting_in_the_environment(mac: Mac, tmp_path: Path) -> None:
    """A PYTHONPATH carrying its own ``sf_housing`` -- a developer's checkout,
    say -- could answer "done" in place of the real rules without copying
    anything, and the install would go on with no copy taken."""
    _board(mac.live, ["found-today"])
    decoy = tmp_path / "decoy"
    (decoy / "sf_housing").mkdir(parents=True)
    (decoy / "sf_housing" / "__init__.py").write_text(f'__version__ = "{VERSION}"\n', encoding="utf-8")
    (decoy / "sf_housing" / "backups.py").write_text("raise SystemExit(0)\n", encoding="utf-8")

    result = mac.install(PYTHONPATH=str(decoy))

    assert result.returncode == 0, result.stdout + result.stderr
    good = _good_backups(mac.app)
    assert len(good) == 1 and _homes(good[0]) == {"found-today"}


def test_a_first_install_has_nothing_to_copy(mac: Mac) -> None:
    result = mac.install()

    assert result.returncode == 0, result.stdout + result.stderr
    assert not list((mac.app / "backups").glob("housing-*.sqlite3"))
