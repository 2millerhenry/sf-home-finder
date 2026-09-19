from __future__ import annotations

import json
import shutil
import subprocess
import tomllib
import zipfile
from pathlib import Path

import pytest

from scripts.build_release import scan_release, validate_gmail_client
from sf_housing.app import FURNISHED_FINDER_BRIDGE_VERSION


ROOT = Path(__file__).resolve().parent.parent
EXTENSION = ROOT / "furnished_finder_chrome_bridge"
PACKAGED_EXTENSION = ROOT / "sf_housing" / "static" / "furnished-finder-bridge.zip"


def test_packaged_chrome_bridge_matches_source_exactly() -> None:
    expected = {
        "manifest.json",
        "service-worker.js",
        "content.js",
        "popup.html",
        "popup.js",
        "popup.css",
    }
    with zipfile.ZipFile(PACKAGED_EXTENSION) as archive:
        assert set(archive.namelist()) == expected
        for name in expected:
            assert archive.read(name) == (EXTENSION / name).read_bytes()


def test_bridge_versions_cannot_drift() -> None:
    manifest = json.loads((EXTENSION / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["version"] == FURNISHED_FINDER_BRIDGE_VERSION
    assert F'const BRIDGE_VERSION = chrome.runtime.getManifest().version;' in (
        EXTENSION / "service-worker.js"
    ).read_text(encoding="utf-8")


def test_release_assets_are_generic_and_preserve_private_data_by_default() -> None:
    release_assets = ROOT / "release_assets"
    text = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in release_assets.rglob("*")
        if path.is_file()
    )

    assert "/Users/henrymiller" not in text
    assert "Library/Application Support/SF Housing Monitor" in text
    # The promise that matters on each platform: setup never asks for an
    # administrator. Asserted where it is made rather than anywhere in the
    # folder, so rewording the page cannot quietly drop it.
    assert "never uses sudo" in (release_assets / "1 START HERE.txt").read_text()
    assert "no admin rights" in (release_assets / "windows" / "1 START HERE.txt").read_text()
    assert "Your private data remains" in text
    assert 'if [ "$CONFIRMATION" = "DELETE" ]' in text
    assert "Verify SF Home Finder.command" in (release_assets / "1 START HERE.txt").read_text()
    assert (release_assets / "Verify SF Home Finder.command").stat().st_mode & 0o100
    assert (release_assets / "payload" / "tools" / "doctor.sh").stat().st_mode & 0o100
    assert 'support/report.json' in (release_assets / "payload" / "tools" / "doctor.sh").read_text()
    assert "SF_HOUSING_NO_LAUNCH_AGENT" in (
        release_assets / "payload" / "tools" / "doctor.sh"
    ).read_text()
    assert "SF_HOUSING_NO_LAUNCH_AGENT" in (
        release_assets / "payload" / "tools" / "open.sh"
    ).read_text()
    assert '"ok": true' in (release_assets / "payload" / "tools" / "open.sh").read_text()
    for command in (
        "3 Open SF Home Finder.command",
        "Verify SF Home Finder.command",
        "Repair SF Home Finder.command",
        "Uninstall SF Home Finder.command",
    ):
        assert "SF_HOUSING_APP_ROOT" in (release_assets / command).read_text()


def test_release_wheel_never_packages_a_profile_or_mutable_data() -> None:
    """A release starts blank. The wheel may carry read-only reference data
    built from public records, and nothing that belongs to a person: no
    profile, no database, no log, no credential."""
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    project = tomllib.loads(text)

    assert 'include = ["sf_housing", "sf_housing.*"]' in text
    assert "config/preferences.yaml" not in text

    patterns = sorted(
        {glob for globs in project["tool"]["setuptools"]["package-data"].values() for glob in globs}
    )
    assert patterns == [
        "*.css",
        "*.html",
        "*.js",
        # The tab icon, in both the form a current browser wants and the one an
        # older browser can still read.
        "*.png",
        "*.svg",
        "*.zip",
        "data/*.json",
        # Each source's own icon, fetched once and served locally rather than
        # hotlinked. Public brand marks, identical in every install.
        "source-icons/*.png",
    ]

    # Those two globs sit at the top of the static directory, so they are named
    # file by file for the same reason everything else here is: a glob is only
    # safe while somebody is watching what lands under it.
    top = sorted(
        path.name
        for path in (ROOT / "sf_housing" / "static").iterdir()
        if path.is_file() and path.suffix in {".png", ".svg"}
    )
    assert top == ["favicon.png", "favicon.svg"], top

    # And that directory is named file by file too, for the same reason the
    # data directory is: a glob is only safe while somebody is watching what
    # lands under it.
    icons = sorted(p.name for p in (ROOT / "sf_housing" / "static" / "source-icons").iterdir())
    assert all(name.endswith(".png") for name in icons), icons
    assert len(icons) == 22, icons

    # The shipped data directory is named file by file, because anything else
    # appearing there would be packaged silently. Each one has to be reference
    # data a stranger could rebuild from public information -- never a profile,
    # a database, a log or a credential.
    shipped = sorted(p.name for p in (ROOT / "sf_housing" / "data").iterdir())
    assert shipped == ["appfolio_managers.json", "sf_streets.json"], shipped

    table = json.loads((ROOT / "sf_housing" / "data" / "sf_streets.json").read_text(encoding="utf-8"))
    assert table["source"].startswith("DataSF"), "the shipped table must be public city data"

    roster = json.loads((ROOT / "sf_housing" / "data" / "appfolio_managers.json").read_text(encoding="utf-8"))
    assert roster["managers"], "the roster must list managers"
    for manager in roster["managers"]:
        # A company subdomain and a company name. Nothing about anybody.
        assert set(manager) == {"subdomain", "name"}, manager
        assert manager["subdomain"].isascii() and manager["subdomain"].islower()


def test_the_street_table_is_committed_and_not_swallowed_by_an_ignore_rule() -> None:
    """The wheel builds from the working tree, so a table that exists locally
    but is untracked would pass every other check here and still leave a fresh
    clone shipping no neighbourhoods at all."""
    table = ROOT / "sf_housing" / "data" / "sf_streets.json"
    result = subprocess.run(
        ["git", "ls-files", "--error-unmatch", str(table.relative_to(ROOT))],
        cwd=ROOT,
        capture_output=True,
    )
    if result.returncode != 0 and b"not a git repository" in result.stderr.lower():
        pytest.skip("not a git checkout")
    assert result.returncode == 0, "sf_housing/data/sf_streets.json is not tracked by git"


def test_release_accepts_only_a_loopback_owner_gmail_client(tmp_path: Path) -> None:
    valid = tmp_path / "valid-client.json"
    valid.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "owner.apps.googleusercontent.com",
                    "client_secret": "owner-build-secret",
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": ["http://localhost"],
                }
            }
        ),
        encoding="utf-8",
    )
    remote = tmp_path / "remote-client.json"
    remote.write_text(
        json.dumps(
            {
                "web": {
                    "client_id": "owner.apps.googleusercontent.com",
                    "client_secret": "owner-build-secret",
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": ["https://example.test/callback"],
                }
            }
        ),
        encoding="utf-8",
    )

    assert validate_gmail_client(valid)
    with pytest.raises(SystemExit, match="loopback"):
        validate_gmail_client(remote)


def test_release_privacy_scan_allows_only_explicit_client_injection_never_a_token(
    tmp_path: Path,
) -> None:
    payload = tmp_path / "payload"
    payload.mkdir()
    client = payload / "gmail-client-secret.json"
    client.write_text('{"installed":{"client_secret":"build-fixture"}}', encoding="utf-8")

    scan_release(tmp_path, gmail_injected=True)
    with pytest.raises(SystemExit, match="unexpected OAuth client material"):
        scan_release(tmp_path, gmail_injected=False)

    client.unlink()
    (payload / "gmail-token.json").write_text('{"refresh_token":"never"}', encoding="utf-8")
    with pytest.raises(SystemExit, match="forbidden file"):
        scan_release(tmp_path, gmail_injected=True)


def test_macos_installer_clears_the_download_quarantine_once() -> None:
    """Without this every command in the release re-prompts Gatekeeper separately.

    The build is unsigned by design (notarization needs a paid Apple account),
    so the goal is one approval rather than five.
    """
    installer = (ROOT / "release_assets" / "payload" / "install.sh").read_text(encoding="utf-8")

    assert "xattr -dr com.apple.quarantine" in installer
    assert '"$RELEASE_ROOT"' in installer
    # It must never be able to fail the installation.
    quarantine_line = next(
        line for line in installer.splitlines() if "xattr -dr com.apple.quarantine" in line
    )
    assert quarantine_line.rstrip().endswith("|| true")
    # And it must run before the payload is verified and used.
    assert installer.index("xattr -dr") < installer.index("shasum -a 256 -c")


def test_both_releases_ship_the_license_next_to_the_installer() -> None:
    """An MIT project must hand the licence to whoever downloads the ZIP."""
    mac = (ROOT / "scripts" / "build_release.py").read_text(encoding="utf-8")
    windows = (ROOT / "scripts" / "build_windows_release.py").read_text(encoding="utf-8")

    assert (ROOT / "LICENSE").is_file()
    assert "MIT License" in (ROOT / "LICENSE").read_text(encoding="utf-8")
    for script in (mac, windows):
        assert 'shutil.copy2(ROOT / "LICENSE", release_root / "LICENSE.txt")' in script


def test_project_metadata_declares_the_license() -> None:
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert 'license = "MIT"' in project
    assert 'license-files = ["LICENSE"]' in project


def test_the_source_logos_travel_in_the_wheel() -> None:
    """They are served from the app's own static directory, so a wheel without
    them shows a page of broken images -- and only in an installed copy, never
    when running from the repository, which is the worst way to find out.

    source-icons is a directory of assets rather than a package, so nothing
    picks it up unless the sub-path is listed by hand."""
    import tomllib

    with open(ROOT / "pyproject.toml", "rb") as handle:
        config = tomllib.load(handle)
    static = config["tool"]["setuptools"]["package-data"]["sf_housing.static"]

    assert "source-icons/*.png" in static


def test_the_tab_icon_travels_in_the_wheel() -> None:
    """Same failure as the source logos, one directory up: static package-data
    listed css, js and zip, so a top-level asset would be missing from an
    installed copy and present in every run from the repository."""
    import tomllib

    with open(ROOT / "pyproject.toml", "rb") as handle:
        config = tomllib.load(handle)
    static = config["tool"]["setuptools"]["package-data"]["sf_housing.static"]

    assert "*.svg" in static and "*.png" in static, static
    for asset in ("favicon.svg", "favicon.png"):
        assert (ROOT / "sf_housing/static" / asset).is_file(), asset


def test_every_source_the_page_names_has_the_icon_it_asks_for() -> None:
    """The template maps a source to an icon file. A name in that map without
    a file on disk is a broken image on the page, and a file nobody maps is
    weight in the wheel for nothing."""
    import re

    page = (ROOT / "sf_housing/templates/alerts.html").read_text(encoding="utf-8")
    block = page[page.index("{% set source_icons = {") : page.index("} %}", page.index("{% set source_icons = {"))]
    mapped = set(re.findall(r"':\s*'([a-z0-9-]+)'", block))
    on_disk = {path.stem for path in (ROOT / "sf_housing/static/source-icons").glob("*.png")}

    assert mapped, "the icon map is empty"
    assert mapped - on_disk == set(), f"named with no file: {sorted(mapped - on_disk)}"
    assert on_disk - mapped == set(), f"shipped but never used: {sorted(on_disk - mapped)}"


def test_a_source_without_an_icon_still_gets_a_mark() -> None:
    """Zumper serves a bot wall to every request, icon included, so it has no
    logo and must fall back rather than render an empty chip."""
    page = (ROOT / "sf_housing/templates/alerts.html").read_text(encoding="utf-8")

    assert "{%- if icon -%}" in page
    assert "source-mark" in page.split("{%- else -%}")[1][:400]


def visible_release_files(command_files, extras) -> list[str]:
    """What the downloaded folder shows, in the order a file browser shows it."""
    return sorted([*command_files, *extras])


def test_the_folder_reads_itself_top_to_bottom() -> None:
    """A file browser sorts alphabetically, which put START_HERE.txt sixth of
    eight -- the page telling somebody what to do, below the release notes and
    beside Uninstall. Numbering the first-run path is the whole fix, so it is
    worth a test that fails if the numbers come off."""
    import sys

    sys.path.insert(0, str(ROOT / "scripts"))
    from build_release import COMMAND_FILES

    shown = visible_release_files(
        COMMAND_FILES, ["1 START HERE.txt", "RELEASE_NOTES.txt", "LICENSE.txt"]
    )

    assert shown[0] == "1 START HERE.txt", shown
    assert shown[1].startswith("2 Install"), shown
    assert shown[2].startswith("3 Open"), shown
    # The destructive one must never be mistaken for a step.
    assert not any(name[0].isdigit() for name in shown if "Uninstall" in name)


def test_neither_builder_writes_the_page_back_under_its_buried_name() -> None:
    """The ordering only holds if the builders copy it out under the numbered
    name. Nothing else in the release would notice if they stopped: the file
    would still be there, still be read, and still sort sixth."""
    for builder in ("build_release.py", "build_windows_release.py"):
        source = (ROOT / "scripts" / builder).read_text(encoding="utf-8")
        assert '"1 START HERE.txt"' in source, builder
        assert '"START_HERE.txt"' not in source, f"{builder} still writes the buried name"


def test_the_version_cannot_drift_between_the_places_that_state_it() -> None:
    """Six files name the version and the builder copies install.sh verbatim,
    so nothing reconciles them at build time.

    When they last disagreed the installer went looking for a wheel filename
    that the build had not produced, and the download was broken for everybody
    who tried it. Every one of these has to be bumped together.
    """
    declared = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = declared["project"]["version"]

    installer = (ROOT / "release_assets" / "payload" / "install.sh").read_text(encoding="utf-8")
    stated = {
        "sf_housing/__init__.py": f'__version__ = "{version}"',
        "scripts/build_release.py": f'VERSION = "{version}"',
    }
    for path, expected in stated.items():
        assert expected in (ROOT / path).read_text(encoding="utf-8"), (
            f"{path} does not say {version}"
        )
    for expected in (
        f'VERSION="{version}"',
        # The name the build actually writes. A mismatch here is the one that
        # breaks the download rather than merely looking untidy.
        f"sf_home_finder-{version}-py3-none-any.whl",
        f'assert sf_housing.__version__ == "{version}"',
    ):
        assert expected in installer, f"install.sh does not say {expected}"

    # Windows states it too, and was not covered here: it sat at 0.3.9 for
    # eleven releases while the builder took its version from pyproject, so a
    # Windows build made on any of those days shipped a current wheel beside an
    # installer looking for a file that had never existed.
    windows = (ROOT / "release_assets" / "windows" / "payload" / "install.ps1").read_text(
        encoding="utf-8"
    )
    assert f"$Version = '{version}'" in windows, f"install.ps1 does not say {version}"


def test_the_release_notes_open_on_the_version_being_shipped() -> None:
    """The notes are the first thing a person reads on the download page, and
    the builder does not write them, so a forgotten section ships a release
    describing the one before it."""
    declared = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = declared["project"]["version"]
    notes = (ROOT / "release_assets" / "RELEASE_NOTES.txt").read_text(encoding="utf-8")

    windows_notes = (ROOT / "release_assets" / "windows" / "RELEASE_NOTES.txt").read_text(
        encoding="utf-8"
    )
    assert windows_notes.startswith(f"SF Home Finder {version} for Windows x64\n"), (
        f"the Windows notes open with {windows_notes.splitlines()[0]!r}"
    )
    assert notes.startswith(f"SF Home Finder {version}\n"), (
        f"the notes open with {notes.splitlines()[0]!r}, not SF Home Finder {version}"
    )


def test_every_powershell_script_in_the_windows_release_parses() -> None:
    """The macOS command is checked with `bash -n` on every run. Nothing checked
    the PowerShell, so a syntax error would have been found by the first person
    to double-click Install on a machine nobody here owns.
    """
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if not powershell:
        pytest.skip("no PowerShell available to parse with")
    scripts = sorted((ROOT / "release_assets" / "windows").rglob("*.ps1"))
    assert scripts, "no PowerShell scripts found to check"
    for script in scripts:
        result = subprocess.run(
            [
                powershell, "-NoProfile", "-Command",
                "$e = $null; "
                f"$null = [System.Management.Automation.Language.Parser]::ParseFile('{script}', [ref]$null, [ref]$e); "
                "if ($e) { $e | ForEach-Object { Write-Output $_.Message }; exit 1 }",
            ],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, f"{script.name} does not parse: {result.stdout}"


def test_the_update_check_switch_reaches_the_background_service() -> None:
    """The regression: the README said "set SF_HOUSING_NO_UPDATE_CHECK=1 and it
    never asks", but the check runs inside a launchd service whose environment
    is the plist and nothing else, so a shell export could not turn it off. It
    has to be written into the plist, and kept there when a later upgrade is run
    without the variable set."""
    installer = (ROOT / "release_assets" / "payload" / "install.sh").read_text(encoding="utf-8")

    assert "$NO_UPDATE_CHECK_ENTRY" in installer, "the plist does not carry the switch"
    assert (
        '<key>SF_HOUSING_NO_UPDATE_CHECK</key><string>1</string>' in installer
    ), "the switch is not written as a plist entry"
    assert (
        'grep -q "SF_HOUSING_NO_UPDATE_CHECK" "$PLIST_PATH"' in installer
    ), "an upgrade would silently turn the update check back on"
    # And the README must not promise the route that cannot work.
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "in front of the install command" in readme


def _stub_release(tmp_path: Path, uv_script: str) -> Path:
    """A release root the installer will accept, with a stand-in for uv.

    Everything before uv runs is real -- the arch check, the file check and the
    checksum verification -- so what this exercises is the installer's own
    handling of a runtime step that fails.
    """
    import hashlib
    import re

    installer = (ROOT / "release_assets" / "payload" / "install.sh").read_text(encoding="utf-8")
    version = re.search(r'^VERSION="([^"]+)"', installer, re.M).group(1)

    release = tmp_path / "release"
    payload = release / "payload"
    (payload / "tools").mkdir(parents=True)
    shutil.copy(ROOT / "release_assets" / "payload" / "install.sh", payload / "install.sh")
    for tool in (ROOT / "release_assets" / "payload" / "tools").glob("*.sh"):
        shutil.copy(tool, payload / "tools" / tool.name)
    (payload / "uv").write_text(uv_script, encoding="utf-8")
    (payload / "uv").chmod(0o755)
    (payload / "requirements.lock").write_text("", encoding="utf-8")
    (payload / f"sf_home_finder-{version}-py3-none-any.whl").write_bytes(b"")

    lines = []
    for path in sorted(p for p in payload.rglob("*") if p.is_file()):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f"{digest}  {path.relative_to(payload).as_posix()}")
    (payload / "checksums.sha256").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return release


@pytest.mark.skipif(
    not (ROOT / "release_assets" / "payload" / "install.sh").exists(), reason="no macOS installer"
)
def test_a_failed_runtime_download_tells_the_person_what_happened(tmp_path: Path) -> None:
    """The regression: the install ended in silence when the network dropped.

    The four uv steps are the only ones that need the internet, so they are the
    ones that routinely fail -- and `uv --quiet` prints nothing at all on a
    failed download. Under `set -e` alone the script simply stopped, leaving the
    last progress line on screen, no error, no next step and no app. Somebody on
    hotel wifi had no way to tell a slow install from a dead one.
    """
    silent_failure = "#!/bin/bash\nexit 1\n"
    release = _stub_release(tmp_path, silent_failure)

    result = subprocess.run(
        ["/bin/bash", str(release / "payload" / "install.sh"), str(release)],
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(tmp_path / "home"),
            "SF_HOUSING_APP_ROOT": str(tmp_path / "app"),
            "SF_HOUSING_NO_LAUNCH_AGENT": "1",
            "SF_HOUSING_NO_BROWSER": "1",
            "SF_HOUSING_PORT": "9987",
        },
    )

    assert result.returncode != 0, "a failed install reported success"
    output = result.stdout + result.stderr
    assert "Installation stopped" in output, f"the install ended without saying so:\n{output!r}"
    assert "internet connection" in output, "no cause is named"
    assert "run this again" in output, "no way forward is offered"
    assert not (tmp_path / "app" / "current").exists(), "a half-installed app was left behind"
