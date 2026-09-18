"""The one-line install, run for real against a stand-in GitHub.

    curl -fsSL .../install.sh | bash

Nothing about this script is covered by the app's own tests, and it is the
only code every single user runs. What is exercised here is the whole of it:
which asset it picks out of a release, how it unpacks that asset, how it finds
the installer inside, and what it says when any of that fails. Only GitHub is
faked -- the archives are built by the real release writers and unpacked by
the real /usr/bin/tar and /usr/bin/unzip.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.build_release import make_tar_xz, make_zip


ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = ROOT / "install.sh"

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin", reason="the installer runs on macOS and unpacks with macOS tooling"
)


def build_archives(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A release, archived both ways, with an installer that records its call.

    The payload installer is a stand-in -- what is under test here is the
    download and unpack that happens before it, and the handover to it. It
    sits at the depth the real one does, because the script finds it by
    searching for that path.
    """
    release = tmp_path / "source" / "SF-Home-Finder-9.9.9-macOS-arm64"
    (release / "payload" / "tools").mkdir(parents=True)
    receipt = tmp_path / "receipt.txt"
    (release / "payload" / "install.sh").write_text(
        f'#!/bin/bash\nprintf "%s" "$1" > {receipt}\nexit "${{STUB_INSTALL_STATUS:-0}}"\n',
        encoding="utf-8",
    )
    (release / "payload" / "install.sh").chmod(0o755)
    (release / "payload" / "uv").write_bytes(b"\x7fELF stand-in" * 100)
    (release / "payload" / "uv").chmod(0o755)
    (release / "1 START HERE.txt").write_text("read me\n", encoding="utf-8")

    zipped = tmp_path / "SF-Home-Finder-9.9.9-macOS-arm64.zip"
    tarred = tmp_path / "SF-Home-Finder-9.9.9-macOS-arm64.tar.xz"
    make_zip(release, zipped)
    make_tar_xz(release, tarred)
    return zipped, tarred, receipt


def write_stub_curl(tmp_path: Path, release_json: Path, assets: dict[str, Path]) -> Path:
    """A curl that answers from disk, so the test never leaves the machine.

    It records the URL it was asked for, which is how a test can tell which
    asset the script chose rather than inferring it from what installed.
    """
    stub_dir = tmp_path / "stub-bin"
    stub_dir.mkdir(exist_ok=True)
    cases = "\n".join(
        f'    *{suffix}) cp "{path}" "$out" ;;' for suffix, path in assets.items()
    )
    (stub_dir / "curl").write_text(
        f"""#!/bin/bash
out=""
url=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    -*) shift ;;
    *) url="$1"; shift ;;
  esac
done
printf '%s\\n' "$url" >> "{tmp_path}/requested.txt"
if [ -n "$out" ]; then
  case "$url" in
{cases}
    *) exit 22 ;;
  esac
else
  cat "{release_json}"
fi
""",
        encoding="utf-8",
    )
    (stub_dir / "curl").chmod(0o755)
    return stub_dir


def release_json(tmp_path: Path, *names: str) -> Path:
    """A releases API answer carrying exactly the assets named.

    Shaped like GitHub's, because the script reads it with sed rather than a
    JSON parser and so is sensitive to the shape rather than to the meaning.
    """
    path = tmp_path / "release.json"
    path.write_text(
        json.dumps(
            {
                "tag_name": "v9.9.9",
                "assets": [
                    {
                        "name": name,
                        "browser_download_url": (
                            f"https://github.com/2millerhenry/sf-home-finder/releases/download/v9.9.9/{name}"
                        ),
                    }
                    for name in names
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def run_install(stub_dir: Path, tmp_path: Path, **environment: str) -> subprocess.CompletedProcess:
    # The PATH a stock Mac has, plus the stub curl. No Homebrew: if the script
    # ever needs a tool that is not already on a Mac, this is where it fails.
    return subprocess.run(
        ["/bin/bash", str(INSTALL_SH)],
        capture_output=True,
        text=True,
        timeout=120,
        env={"PATH": f"{stub_dir}:/usr/bin:/bin", "HOME": str(tmp_path / "home"), **environment},
    )


def test_it_installs_from_the_tarball_when_a_release_has_one(tmp_path: Path) -> None:
    """The change this file exists for: the tarball is a third smaller than
    the zip and holds the same folder, so the one-line install should take it.
    Asserted on the URL that was actually downloaded, because an install that
    succeeds says nothing about which asset paid for it."""
    zipped, tarred, receipt = build_archives(tmp_path)
    listing = release_json(
        tmp_path,
        "SF-Home-Finder-9.9.9-macOS-arm64.zip",
        "SF-Home-Finder-9.9.9-macOS-arm64.tar.xz",
    )
    stub = write_stub_curl(tmp_path, listing, {".tar.xz": tarred, ".zip": zipped})

    result = run_install(stub, tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    requested = (tmp_path / "requested.txt").read_text(encoding="utf-8").splitlines()
    assert requested[-1].endswith("macOS-arm64.tar.xz"), requested
    # And the installer inside really ran, handed the release root it found.
    assert receipt.is_file(), result.stdout + result.stderr
    assert receipt.read_text(encoding="utf-8").endswith("SF-Home-Finder-9.9.9-macOS-arm64")


def test_it_still_installs_from_the_zip_for_every_release_published_so_far(tmp_path: Path) -> None:
    """The regression that would hit everyone at once: all eighteen published
    releases are zip-only, so the fallback is not a nicety -- it is the path
    that runs until the next release ships. A resolver that only knows about
    the tarball breaks the install command for every existing version."""
    zipped, tarred, receipt = build_archives(tmp_path)
    listing = release_json(tmp_path, "SF-Home-Finder-9.9.9-macOS-arm64.zip")
    # The tarball is not offered at all: asking for it must fail, the way a
    # 404 from GitHub would.
    stub = write_stub_curl(tmp_path, listing, {".zip": zipped})

    result = run_install(stub, tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    requested = (tmp_path / "requested.txt").read_text(encoding="utf-8").splitlines()
    assert requested[-1].endswith("macOS-arm64.zip"), requested
    assert receipt.read_text(encoding="utf-8").endswith("SF-Home-Finder-9.9.9-macOS-arm64")


def test_it_unpacks_each_format_with_the_tool_that_ships_with_macos(tmp_path: Path) -> None:
    """The regression a new dependency would be: the script runs on a Mac
    nobody has set up, so xz from Homebrew is not available to it. Both
    extractors are named by absolute path for the same reason -- what is on a
    person's PATH is not something an install command gets to assume."""
    script = INSTALL_SH.read_text(encoding="utf-8")

    assert "/usr/bin/tar -xJf" in script
    assert "/usr/bin/unzip -q" in script
    # Not a bare `tar`, and nothing that would need installing.
    assert "\ntar " not in script and " xz " not in script
    assert "brew" not in script


def test_a_truncated_download_stops_with_something_a_person_can_act_on(tmp_path: Path) -> None:
    """The regression: a download cut short by a dropped connection leaves an
    archive that is a valid file and an invalid archive. Before this the
    failure surfaced as unzip's own output; what somebody needs is to be told
    to run it again."""
    zipped, tarred, _ = build_archives(tmp_path)
    half = tmp_path / "half.tar.xz"
    half.write_bytes(tarred.read_bytes()[: tarred.stat().st_size // 2])
    listing = release_json(tmp_path, "SF-Home-Finder-9.9.9-macOS-arm64.tar.xz")
    stub = write_stub_curl(tmp_path, listing, {".tar.xz": half})

    result = run_install(stub, tmp_path)

    assert result.returncode != 0
    assert "Stopped: the download was incomplete. Run it again." in result.stderr


def test_it_says_where_to_look_when_a_release_offers_neither_archive(tmp_path: Path) -> None:
    """The regression: with no asset matched the script used to carry an empty
    URL into curl, which failed with curl's own words about a malformed URL.
    A release that is still uploading its assets is a real state, and the way
    out of it is the releases page."""
    listing = release_json(tmp_path, "SF-Home-Finder-9.9.9-Windows-x64.zip")
    stub = write_stub_curl(tmp_path, listing, {})

    result = run_install(stub, tmp_path)

    assert result.returncode != 0
    assert "could not find the download" in result.stderr
    assert "releases" in result.stderr


def test_it_says_so_plainly_when_github_cannot_be_reached(tmp_path: Path) -> None:
    """The regression: offline, the release lookup returned nothing and the
    script carried on to fail later and less clearly. The first thing it does
    needs the network, so that is where being offline should be reported."""
    stub_dir = tmp_path / "stub-bin"
    stub_dir.mkdir()
    (stub_dir / "curl").write_text("#!/bin/bash\nexit 6\n", encoding="utf-8")
    (stub_dir / "curl").chmod(0o755)

    result = run_install(stub_dir, tmp_path)

    assert result.returncode != 0
    assert "could not reach GitHub" in result.stderr


def test_it_refuses_a_machine_the_release_does_not_run_on(tmp_path: Path) -> None:
    """The regression: the release is a macOS arm64 binary bundle. Run under
    Rosetta, on an Intel Mac or piped into bash on Linux it would otherwise
    download 12 MB before failing somewhere deep inside the payload."""
    zipped, tarred, _ = build_archives(tmp_path)
    listing = release_json(tmp_path, "SF-Home-Finder-9.9.9-macOS-arm64.tar.xz")
    stub = write_stub_curl(tmp_path, listing, {".tar.xz": tarred})
    (stub / "uname").write_text(
        '#!/bin/bash\n[ "${1:-}" = "-m" ] && echo x86_64 || echo Darwin\n', encoding="utf-8"
    )
    (stub / "uname").chmod(0o755)

    result = run_install(stub, tmp_path)

    assert result.returncode != 0
    assert "Apple Silicon" in result.stderr
    assert not (tmp_path / "requested.txt").exists(), "it downloaded before checking the machine"


def test_the_installer_it_hands_off_to_decides_whether_the_install_succeeded(
    tmp_path: Path,
) -> None:
    """The regression: this script used to exec the payload installer, so its
    EXIT trap never fired and roughly 60 MB of archive and unpacked release
    stayed in /tmp after every install. Running it instead means the exit code
    has to be carried out by hand, and a swallowed one would report a failed
    install as a success."""
    zipped, tarred, _ = build_archives(tmp_path)
    listing = release_json(tmp_path, "SF-Home-Finder-9.9.9-macOS-arm64.tar.xz")
    stub = write_stub_curl(tmp_path, listing, {".tar.xz": tarred})

    result = run_install(stub, tmp_path, STUB_INSTALL_STATUS="7")

    assert result.returncode == 7, result.stdout + result.stderr


def test_it_leaves_nothing_of_a_12_mb_download_behind(tmp_path: Path) -> None:
    """The regression named above, asserted rather than assumed: every temp
    directory the script made is gone whether the install worked or not."""
    zipped, tarred, _ = build_archives(tmp_path)
    listing = release_json(tmp_path, "SF-Home-Finder-9.9.9-macOS-arm64.tar.xz")
    stub = write_stub_curl(tmp_path, listing, {".tar.xz": tarred})
    before = set(Path("/var/folders").glob("*/*/T/tmp*")) if Path("/var/folders").exists() else set()

    for status in ("0", "7"):
        run_install(stub, tmp_path, STUB_INSTALL_STATUS=status)

    after = set(Path("/var/folders").glob("*/*/T/tmp*")) if Path("/var/folders").exists() else set()
    assert after <= before, f"left behind: {sorted(after - before)}"
