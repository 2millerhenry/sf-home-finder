"""The two archives a macOS release publishes, and what has to stay true of both.

The one-line install downloads the `.tar.xz`; the releases page hands out the
`.zip`. They are built from one tree by two writers, so every property that
makes an install work -- what is in it, what is executable, and a checksum
somebody else can reproduce -- has to be asserted of each of them separately.
"""

from __future__ import annotations

import hashlib
import os
import random
import stat
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

from scripts.build_release import EPOCH, EPOCH_SECONDS, make_tar_xz, make_zip, write_checksum


ROOT = Path(__file__).resolve().parent.parent
# The two extractors the installer uses. Stock macOS paths on purpose: the
# whole point of choosing xz over a better compressor is that nobody has to
# install anything to unpack what we publish.
TAR = "/usr/bin/tar"
UNZIP = "/usr/bin/unzip"


def build_release_tree(parent: Path) -> Path:
    """A stand-in for the release root, shaped like the real one.

    Small enough to compress in milliseconds, but with everything the writers
    have to make a decision about: nested directories, a file that has to come
    out executable, a file that must not, and bytes that are not text.
    """
    release = parent / "SF-Home-Finder-0.0.0-macOS-arm64"
    payload = release / "payload"
    tools = payload / "tools"
    tools.mkdir(parents=True)
    (release / "1 START HERE.txt").write_text("read me first\n", encoding="utf-8")
    (payload / "install.sh").write_text("#!/bin/bash\necho installing\n", encoding="utf-8")
    (payload / "install.sh").chmod(0o755)
    (tools / "open.sh").write_text("#!/bin/bash\necho opening\n", encoding="utf-8")
    (tools / "open.sh").chmod(0o755)
    (payload / "uv").write_bytes(bytes(range(256)) * 64)
    (payload / "uv").chmod(0o755)
    (payload / "requirements.lock").write_text("flask==3.0.0\n", encoding="utf-8")
    (payload / "requirements.lock").chmod(0o644)
    return release


def extracted_tree(archive: Path, into: Path) -> dict[str, tuple[bytes | None, int]]:
    """Unpack with the tool the installer uses, and describe what landed.

    Described as content and permission bits rather than compared archive to
    archive: a zip entry and a tar entry are different records of the same
    file, and what has to match is the folder a person ends up with.
    """
    into.mkdir(parents=True, exist_ok=True)
    if archive.name.endswith(".tar.xz"):
        command = [TAR, "-xJf", str(archive), "-C", str(into)]
    else:
        command = [UNZIP, "-q", str(archive), "-d", str(into)]
    # Stock tooling only, proven rather than assumed: with this PATH there is
    # no Homebrew xz to fall back on.
    subprocess.run(command, check=True, env={"PATH": "/usr/bin:/bin"})
    tree: dict[str, tuple[bytes | None, int]] = {}
    for path in sorted(into.rglob("*")):
        name = path.relative_to(into).as_posix()
        content = None if path.is_dir() else path.read_bytes()
        tree[name] = (content, stat.S_IMODE(path.stat().st_mode))
    return tree


pytestmark = pytest.mark.skipif(
    sys.platform != "darwin", reason="the macOS release is built and unpacked with macOS tooling"
)


def test_the_tarball_and_the_zip_unpack_to_the_same_folder(tmp_path: Path) -> None:
    """The regression this forecloses: the two archives are published side by
    side and a person gets whichever one their download path reached for. Two
    writers over one tree can drift -- a missing directory entry, a dropped
    executable bit -- and the difference would only show up as an install that
    works or not depending on which link was clicked."""
    release = build_release_tree(tmp_path / "source")
    zipped = tmp_path / "release.zip"
    tarred = tmp_path / "release.tar.xz"
    make_zip(release, zipped)
    make_tar_xz(release, tarred)

    from_zip = extracted_tree(zipped, tmp_path / "from-zip")
    from_tar = extracted_tree(tarred, tmp_path / "from-tar")

    assert from_zip == from_tar
    # Written in the same fixed order as well as holding the same files. Two
    # writers walking the tree differently would still unpack the same folder
    # today and stop being reproducible the day the filesystem hands back a
    # different order.
    with zipfile.ZipFile(zipped) as opened:
        zip_order = [name.rstrip("/") for name in opened.namelist()]
    with tarfile.open(tarred) as opened_tar:
        tar_order = opened_tar.getnames()
    assert tar_order == zip_order == sorted(zip_order)
    # And it is the tree that went in, not an empty agreement between two
    # writers that both dropped everything.
    assert set(from_zip) == {
        "SF-Home-Finder-0.0.0-macOS-arm64",
        "SF-Home-Finder-0.0.0-macOS-arm64/1 START HERE.txt",
        "SF-Home-Finder-0.0.0-macOS-arm64/payload",
        "SF-Home-Finder-0.0.0-macOS-arm64/payload/install.sh",
        "SF-Home-Finder-0.0.0-macOS-arm64/payload/requirements.lock",
        "SF-Home-Finder-0.0.0-macOS-arm64/payload/tools",
        "SF-Home-Finder-0.0.0-macOS-arm64/payload/tools/open.sh",
        "SF-Home-Finder-0.0.0-macOS-arm64/payload/uv",
    }


def test_the_bundled_uv_comes_out_of_the_tarball_executable(tmp_path: Path) -> None:
    """The regression: the installer runs payload/uv directly. An archive that
    records it as 0644 -- which is what tar does with a file whose mode is
    copied from a builder's umask, or what a zip written without external_attr
    does -- produces a release that verifies, unpacks, and then stops with
    permission denied on its first real step."""
    release = build_release_tree(tmp_path / "source")
    tarred = tmp_path / "release.tar.xz"
    make_tar_xz(release, tarred)

    tree = extracted_tree(tarred, tmp_path / "out")

    assert tree["SF-Home-Finder-0.0.0-macOS-arm64/payload/uv"][1] == 0o755
    assert tree["SF-Home-Finder-0.0.0-macOS-arm64/payload/install.sh"][1] == 0o755
    assert tree["SF-Home-Finder-0.0.0-macOS-arm64/payload/tools/open.sh"][1] == 0o755
    # And the bit is not simply set on everything: a lock file that unpacks
    # executable is a build that stopped distinguishing.
    assert tree["SF-Home-Finder-0.0.0-macOS-arm64/payload/requirements.lock"][1] == 0o644
    assert tree["SF-Home-Finder-0.0.0-macOS-arm64/1 START HERE.txt"][1] == 0o644
    assert tree["SF-Home-Finder-0.0.0-macOS-arm64/payload"][1] == 0o755


def test_the_archives_carry_no_trace_of_the_machine_that_built_them(tmp_path: Path) -> None:
    """The regression tar brings and zip does not: TarInfo records the
    builder's account name, numeric ids and the file's real modification time
    unless every one of them is overwritten. That publishes an identity in a
    release asset, and it makes two builds of one commit differ -- so the
    checksum we publish is one nobody else can reproduce."""
    release = build_release_tree(tmp_path / "source")
    tarred = tmp_path / "release.tar.xz"
    make_tar_xz(release, tarred)

    with tarfile.open(tarred) as archive:
        members = archive.getmembers()

    assert members, "the tarball is empty"
    for member in members:
        assert member.uid == 0 and member.gid == 0, f"{member.name} carries a numeric owner"
        assert member.uname == "" and member.gname == "", f"{member.name} names its builder"
        assert member.mtime == EPOCH_SECONDS, f"{member.name} carries a real modification time"
    # The zip half of the same promise, since both are published.
    zipped = tmp_path / "release.zip"
    make_zip(release, zipped)
    with zipfile.ZipFile(zipped) as archive:
        for info in archive.infolist():
            assert info.date_time == EPOCH, f"{info.filename} carries a real modification time"


def test_two_builds_of_one_tree_produce_byte_identical_archives(tmp_path: Path) -> None:
    """The regression: a published .sha256 is a promise that anyone rebuilding
    the commit gets the same bytes. Timestamps are what breaks that, and they
    are invisible -- a build an hour later is still green, still installs, and
    silently has a different checksum. Touched between the two builds here
    because a same-second rebuild would pass with the pinning removed."""
    release = build_release_tree(tmp_path / "source")
    first = tmp_path / "first.tar.xz"
    first_zip = tmp_path / "first.zip"
    make_tar_xz(release, first)
    make_zip(release, first_zip)

    for path in sorted(release.rglob("*")):
        os.utime(path, (1_700_000_000, 1_700_000_000))
    second = tmp_path / "second.tar.xz"
    second_zip = tmp_path / "second.zip"
    make_tar_xz(release, second)
    make_zip(release, second_zip)

    assert hashlib.sha256(first.read_bytes()).hexdigest() == hashlib.sha256(
        second.read_bytes()
    ).hexdigest(), "two builds of one tree produced different tarballs"
    assert hashlib.sha256(first_zip.read_bytes()).hexdigest() == hashlib.sha256(
        second_zip.read_bytes()
    ).hexdigest(), "two builds of one tree produced different zips"


def test_the_checksum_file_is_named_after_the_whole_archive(tmp_path: Path) -> None:
    """The regression Path.with_suffix invites: it reads "X.tar.xz" as a name
    ending in ".xz", so deriving the checksum's name publishes "X.tar.sha256"
    beside a "X.tar.xz" nobody can check with it. A misnamed asset is not a
    failing build -- it ships, and is found by the one person who bothered to
    verify their download."""
    archive = tmp_path / "SF-Home-Finder-0.5.6-macOS-arm64.tar.xz"
    archive.write_bytes(b"not really compressed")

    digest = write_checksum(archive)

    published = tmp_path / "SF-Home-Finder-0.5.6-macOS-arm64.tar.xz.sha256"
    assert published.is_file(), sorted(path.name for path in tmp_path.iterdir())
    assert digest == hashlib.sha256(b"not really compressed").hexdigest()
    # Written the way shasum -c reads it: the digest, two spaces, and the name
    # of the file as it sits next to this one.
    assert published.read_text(encoding="utf-8") == f"{digest}  {archive.name}\n"
    assert not (tmp_path / "SF-Home-Finder-0.5.6-macOS-arm64.tar.sha256").exists()


def test_the_zip_keeps_the_name_and_shape_its_published_checksums_assume(tmp_path: Path) -> None:
    """The zip is the double-click path and every release published so far.
    The tarball was added beside it, so what needs asserting is that adding it
    changed nothing about the zip: same naming for its checksum, same entry
    order, same directory entries."""
    archive = tmp_path / "SF-Home-Finder-0.5.6-macOS-arm64.zip"
    release = build_release_tree(tmp_path / "source")
    make_zip(release, archive)
    write_checksum(archive)

    assert (tmp_path / "SF-Home-Finder-0.5.6-macOS-arm64.zip.sha256").is_file()
    with zipfile.ZipFile(archive) as opened:
        names = opened.namelist()
    assert names == sorted(names), "entries are no longer written in a fixed order"
    assert "SF-Home-Finder-0.0.0-macOS-arm64/payload/" in names, "directory entries were dropped"


def test_the_tarball_is_much_smaller_than_the_zip_on_binary_payload(tmp_path: Path) -> None:
    """The whole reason for the second archive. Deflate barely compresses the
    44 MB uv binary; xz at preset 9 takes nearly 40% off the real release. A
    preset or compressor regression would leave every test above green while
    quietly handing the download back its size, so the size is asserted."""
    release = build_release_tree(tmp_path / "source")
    # Incompressible bytes that repeat once, two megabytes apart. Deflate
    # cannot see that far -- its window is 32 KB -- and neither can xz at a
    # low preset, whose dictionary is 256 KB. Only xz configured the way this
    # release needs finds it, so dropping the preset shows up here as a
    # tarball the size of the zip. A merely repetitive payload would not:
    # everything compresses that, and the test would pass at any setting.
    blob = random.Random(20260101).randbytes(2_000_000)
    (release / "payload" / "uv").write_bytes(blob + blob)
    zipped = tmp_path / "release.zip"
    tarred = tmp_path / "release.tar.xz"
    make_zip(release, zipped)
    make_tar_xz(release, tarred)

    assert tarred.stat().st_size < zipped.stat().st_size * 0.75, (
        f"tarball {tarred.stat().st_size} vs zip {zipped.stat().st_size}"
    )
