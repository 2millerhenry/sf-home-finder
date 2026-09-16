#!/usr/bin/env python3
"""Build the exact downloadable macOS release artifact."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent.parent
VERSION = "0.5.5"
RELEASE_NAME = f"SF-Home-Finder-{VERSION}-macOS-arm64"
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
        env={**os.environ, "UV_CACHE_DIR": str(cache)},
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


def make_zip(source_dir: Path, destination: Path) -> None:
    epoch = (2026, 1, 1, 0, 0, 0)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(source_dir.rglob("*")):
            relative = Path(source_dir.name) / path.relative_to(source_dir)
            info = zipfile.ZipInfo(relative.as_posix(), date_time=epoch)
            mode = path.stat().st_mode
            if path.is_dir():
                info.filename += "/"
                info.external_attr = ((stat.S_IFDIR | 0o755) & 0xFFFF) << 16
                archive.writestr(info, b"")
            else:
                executable = bool(mode & stat.S_IXUSR)
                info.external_attr = ((stat.S_IFREG | (0o755 if executable else 0o644)) & 0xFFFF) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, path.read_bytes())


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
        checksum = sha256(destination)
        destination.with_suffix(".zip.sha256").write_text(
            f"{checksum}  {destination.name}\n", encoding="utf-8"
        )
        print(destination)
        print(checksum)


if __name__ == "__main__":
    main()
