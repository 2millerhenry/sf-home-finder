"""The page a stranger lands on when they follow the download link.

The repository's website field points at the latest release, so this page is
the front door for anybody who did not arrive through the README. It has to
lead with what the app is and how to get it, and it has to stay that way
between releases rather than being rewritten by hand each time and drifting.
"""

from __future__ import annotations

import hashlib
import pathlib
import subprocess
import sys

import pytest


ROOT = pathlib.Path(__file__).resolve().parent.parent
NOTES = ROOT / "release_assets" / "RELEASE_NOTES.txt"


def current_version() -> str:
    import tomllib

    return tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]


def page() -> str:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "release_page.py"), current_version()],
        capture_output=True, text=True, cwd=ROOT,
    )
    if result.returncode:
        pytest.skip(f"release not built: {result.stderr.strip()}")
    return result.stdout


def test_it_leads_with_what_the_app_is_not_with_the_changelog() -> None:
    """Somebody arriving here has usually never seen the app. A version's own
    changes are the least interesting thing on the page to them.

    Anchored on the sentence that says what the app does rather than on the
    opening line. The opener is the one line on the page most likely to be
    rewritten, and a test that goes red when the copy is improved is a test
    people learn to edit rather than read -- this one had already drifted.
    """
    text = page()
    first = text.index("SF Home Finder watches")
    changed = text.index("What changed in")

    assert first < text.index("## Install") < changed


def test_the_checksum_and_the_size_describe_the_file_the_command_downloads() -> None:
    """The regression a second archive invites: the install command now takes
    the .tar.xz, and the page went on printing the zip's checksum and the zip's
    size. Somebody verifying their download against the only digest on the page
    would have found it wrong, and the number under the command described a
    file that command does not fetch."""
    version = current_version()
    tarball = ROOT / "dist" / f"SF-Home-Finder-{version}-macOS-arm64.tar.xz"
    zipped = ROOT / "dist" / f"SF-Home-Finder-{version}-macOS-arm64.zip"
    if not tarball.is_file():
        pytest.skip(f"not built: {tarball}")
    text = page()

    digest = hashlib.sha256(tarball.read_bytes()).hexdigest()
    assert f"{digest}  {tarball.name}" in text, "the tarball's checksum is not published"
    assert f"*{round(tarball.stat().st_size / 1_000_000)} MB ·" in text, text[:2000]
    assert f"*{round(zipped.stat().st_size / 1_000_000)} MB ·" not in text, "it is still the zip's size"
    # The zip is still published and still double-clicked, so its checksum has
    # to stay on the page too rather than being replaced by the tarball's.
    assert f"{hashlib.sha256(zipped.read_bytes()).hexdigest()}  {zipped.name}" in text


def test_the_two_ways_back_in_are_both_named() -> None:
    """Somebody who installed with the one-line command has no shortcuts, so the
    command and the address both have to be written down somewhere they will
    see them."""
    text = page()

    assert "homefinder" in text
    assert "http://127.0.0.1:8000" in text
    assert "bookmark" in text


def test_the_changelog_and_checksum_are_folded_away() -> None:
    text = page()

    assert "<details>" in text
    assert text.index("<details>") < text.index("What changed in")


def test_the_changes_come_from_the_notes_file() -> None:
    """Retyping them onto the release page is how the two drift apart."""
    version = current_version()
    entry = NOTES.read_text().split(f"SF Home Finder {version}\n", 1)[1]
    sentence = " ".join(entry.strip().split("\n\n")[0].split())[:60]

    assert sentence in page()


def test_hard_wrapping_does_not_survive_into_the_page() -> None:
    """The notes file is wrapped for a terminal. GitHub honours those breaks
    literally, which turned a paragraph into a column of short lines."""
    text = page()
    body = text[text.index("What changed in"):]
    paragraphs = [p for p in body.split("\n\n") if p.strip() and not p.startswith(("```", "<", "#"))]

    assert paragraphs, "no prose found to check"
    assert max(len(p.splitlines()) for p in paragraphs) == 1, "a paragraph is still hard-wrapped"
