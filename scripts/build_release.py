#!/usr/bin/env python3
"""Build the exact downloadable macOS release artifact."""

from __future__ import annotations

import argparse
import calendar
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent.parent
VERSION = "0.5.10"
RELEASE_NAME = f"SF-Home-Finder-{VERSION}-macOS-arm64"
# Every entry in both archives is pinned to this one instant. A real
# modification time is the thing that would otherwise make two builds of one
# commit differ, which would mean a published checksum nobody else can
# reproduce.
EPOCH = (2026, 1, 1, 0, 0, 0)
# The same instant as seconds, because tar records a timestamp where zip
# records a date. Shared rather than restated so the two archives cannot drift
# to different dates. Read as UTC, since a zip date is read as the unpacking
# machine's local time and a build must not depend on the builder's timezone.
EPOCH_SECONDS = calendar.timegm((*EPOCH, 0, 0, 0))
UV_SOURCE = Path.home() / ".local" / "bin" / "uv"
EXTENSION_FILES = (
    "manifest.json",
    "service-worker.js",
    "content.js",
    "popup.html",
    "popup.js",
    "popup.css",
)
COMMAND_FILES = (
    "2 Install SF Home Finder.command",
    "3 Open SF Home Finder.command",
    "Repair SF Home Finder.command",
    "Verify SF Home Finder.command",
    "Uninstall SF Home Finder.command",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_extension_zip() -> Path:
    source = ROOT / "furnished_finder_chrome_bridge"
    target = ROOT / "sf_housing" / "static" / "furnished-finder-bridge.zip"
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in EXTENSION_FILES:
            data = (source / name).read_bytes()
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o644 & 0xFFFF) << 16
            archive.writestr(info, data)
    return target


def validate_gmail_client(path: Path) -> bytes:
    raw = path.read_bytes()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Gmail OAuth file is not valid JSON: {exc}") from exc
    config = payload.get("installed") or payload.get("web")
    if not isinstance(config, dict):
        raise SystemExit("Gmail OAuth file must contain an installed or web client configuration")
    redirects = config.get("redirect_uris", [])
    if not any(
        urlparse(str(uri)).scheme == "http"
        and urlparse(str(uri)).hostname in {"127.0.0.1", "localhost", "::1"}
        for uri in redirects
    ):
        raise SystemExit("Gmail OAuth client must allow a loopback redirect URI")
    required = {"client_id", "client_secret", "auth_uri", "token_uri"}
    if not required.issubset(config):
        raise SystemExit("Gmail OAuth client is missing required client fields")
    return raw


def run_checked(*args: str) -> None:
    cache = ROOT / "build" / "uv-cache"
    cache.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        args,
        cwd=ROOT,
        check=True,
        # The wheel is itself a zip, and left alone setuptools stamps it with
        # the moment it was built -- which made two builds of one commit
        # produce different wheels, a different checksums.sha256 and so two
        # different archives, however carefully the archives themselves were
        # pinned. SOURCE_DATE_EPOCH is the standard way to say otherwise, and
        # it is the same instant everything else in the release is pinned to.
        env={
            **os.environ,
            "UV_CACHE_DIR": str(cache),
            "SOURCE_DATE_EPOCH": str(EPOCH_SECONDS),
        },
    )


def scan_release(root: Path, gmail_injected: bool) -> None:
    forbidden_names = {
        "housing.sqlite3",
        "gmail-token.json",
        "gmail-oauth-state.json",
        "apify-token.txt",
        ".env",
    }
    forbidden_text = (
        b"/Users/henrymiller",
        b"BEGIN PRIVATE KEY",
        b"ghp_",
    )
    findings: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if path.name in forbidden_names:
            findings.append(f"forbidden file: {relative}")
        data = path.read_bytes()
        for marker in forbidden_text:
            if marker in data:
                findings.append(f"forbidden content {marker.decode(errors='replace')!r}: {relative}")
        if b'"client_secret"' in data and not (gmail_injected and relative.endswith("payload/gmail-client-secret.json")):
            # Application code necessarily names the OAuth field. Only a loose
            # JSON credential file outside the one explicitly injected slot is
            # forbidden.
            if path.suffix == ".json":
                findings.append(f"unexpected OAuth client material: {relative}")
    if findings:
        raise SystemExit("Release privacy scan failed:\n" + "\n".join(findings))


def released_mode(path: Path) -> int:
    """The permissions a file leaves in, which are the ones it needs to work
    rather than whatever the builder's umask gave it.

    Shared by both archive writers: the two are published side by side and a
    file that is executable out of one and not the other is a release that
    installs or not depending on which link somebody clicked.
    """
    if path.is_dir():
        return 0o755
    return 0o755 if path.stat().st_mode & stat.S_IXUSR else 0o644


def write_checksum(archive: Path) -> str:
    """Publish an archive's checksum beside it, named for the whole archive.

    By appending rather than by replacing a suffix: with_suffix reads
    "X.tar.xz" as a name ending in ".xz", so deriving this name would publish
    the tarball's checksum as "X.tar.sha256" -- a misnamed asset nobody would
    notice until somebody tried to check a download with it.
    """
    digest = sha256(archive)
    archive.with_name(archive.name + ".sha256").write_text(
        f"{digest}  {archive.name}\n", encoding="utf-8"
    )
    return digest


def make_zip(source_dir: Path, destination: Path) -> None:
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(source_dir.rglob("*")):
            relative = Path(source_dir.name) / path.relative_to(source_dir)
            info = zipfile.ZipInfo(relative.as_posix(), date_time=EPOCH)
            if path.is_dir():
                info.filename += "/"
                info.external_attr = ((stat.S_IFDIR | released_mode(path)) & 0xFFFF) << 16
                archive.writestr(info, b"")
            else:
                info.external_attr = ((stat.S_IFREG | released_mode(path)) & 0xFFFF) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, path.read_bytes())


def make_tar_xz(source_dir: Path, destination: Path) -> None:
    """The same tree as the zip, in the container that compresses it well.

    Deflate does badly on the 44 MB uv binary that is most of the release;
    xz at its highest preset takes a third off the download. This mirrors
    make_zip entry for entry on purpose -- same order, same names, same modes,
    same fixed timestamp -- because the two archives hold the same release and
    have to expand to the same tree.

    Everything tar would otherwise record about the machine that built it is
    overwritten: the builder's account name and numeric ids leak an identity
    into a published file and make two builds of one commit differ. Entries
    are written by hand rather than through gettarinfo for the same reason the
    zip writer does it: nothing about the local filesystem should reach the
    archive except a file's bytes and whether it is executable.
    """
    with tarfile.open(destination, "w:xz", format=tarfile.PAX_FORMAT, preset=9) as archive:
        for path in sorted(source_dir.rglob("*")):
            relative = Path(source_dir.name) / path.relative_to(source_dir)
            info = tarfile.TarInfo(relative.as_posix())
            info.mtime = EPOCH_SECONDS
            info.mode = released_mode(path)
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            if path.is_dir():
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
            else:
                data = path.read_bytes()
                info.type = tarfile.REGTYPE
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gmail-client-json", type=Path, help="Optional owner-controlled OAuth client JSON")
    parser.add_argument("--uv-bin", type=Path, default=UV_SOURCE)
    args = parser.parse_args()

    if sys.platform != "darwin" or os.uname().machine != "arm64":
        raise SystemExit("This builder currently produces the tested macOS arm64 release only")
    if not args.uv_bin.is_file():
        raise SystemExit(f"Pinned uv binary not found: {args.uv_bin}")
    version = subprocess.check_output([args.uv_bin, "--version"], text=True).strip()
    if version != "uv 0.10.8 (Homebrew 2026-03-20)" and not version.startswith("uv 0.10.8"):
        raise SystemExit(f"Expected uv 0.10.8, found {version}")

    write_extension_zip()
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="sf-housing-build-") as temporary:
        temp = Path(temporary)
        wheel_dir = temp / "wheel"
        wheel_dir.mkdir()
        run_checked(str(args.uv_bin), "build", "--wheel", "--out-dir", str(wheel_dir))
        wheel = wheel_dir / f"sf_home_finder-{VERSION}-py3-none-any.whl"
        if not wheel.is_file():
            raise SystemExit(f"Expected wheel was not built: {wheel}")

        release_root = temp / RELEASE_NAME
        payload = release_root / "payload"
        tools = payload / "tools"
        tools.mkdir(parents=True)
        assets = ROOT / "release_assets"
        shutil.copy2(assets / "1 START HERE.txt", release_root / "1 START HERE.txt")
        shutil.copy2(assets / "RELEASE_NOTES.txt", release_root / "RELEASE_NOTES.txt")
        # .txt so it opens with a double-click on a machine with no editor set up.
        shutil.copy2(ROOT / "LICENSE", release_root / "LICENSE.txt")
        for name in COMMAND_FILES:
            shutil.copy2(assets / name, release_root / name)
            (release_root / name).chmod(0o755)
        shutil.copy2(assets / "requirements.lock", payload / "requirements.lock")
        shutil.copy2(assets / "payload" / "install.sh", payload / "install.sh")
        (payload / "install.sh").chmod(0o755)
        for script in (assets / "payload" / "tools").glob("*.sh"):
            shutil.copy2(script, tools / script.name)
            (tools / script.name).chmod(0o755)
        bridge = payload / "furnished-finder-bridge"
        bridge.mkdir()
        for name in EXTENSION_FILES:
            shutil.copy2(ROOT / "furnished_finder_chrome_bridge" / name, bridge / name)
        shutil.copy2(wheel, payload / wheel.name)
        shutil.copy2(args.uv_bin, payload / "uv")
        (payload / "uv").chmod(0o755)
        gmail_injected = bool(args.gmail_client_json)
        if args.gmail_client_json:
            (payload / "gmail-client-secret.json").write_bytes(validate_gmail_client(args.gmail_client_json))
            (payload / "gmail-client-secret.json").chmod(0o600)

        checksummed = sorted(
            path for path in payload.rglob("*") if path.is_file() and path.name != "checksums.sha256"
        )
        lines = [f"{sha256(path)}  {path.relative_to(payload).as_posix()}" for path in checksummed]
        (payload / "checksums.sha256").write_text("\n".join(lines) + "\n", encoding="utf-8")
        scan_release(release_root, gmail_injected)

        destination = dist / f"{RELEASE_NAME}.zip"
        make_zip(release_root, destination)
        print(destination)
        print(write_checksum(destination))

        # Beside the zip rather than instead of it. The zip is what a
        # double-click gets and what every release published so far carries,
        # so it stays exactly as it was; the tarball is only what the one-line
        # install reaches for first. Compressing it takes about forty seconds
        # on this payload, which is a release build working rather than a hang.
        tarball = dist / f"{RELEASE_NAME}.tar.xz"
        make_tar_xz(release_root, tarball)
        print(tarball)
        print(write_checksum(tarball))


if __name__ == "__main__":
    main()
