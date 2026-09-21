#!/usr/bin/env python3
"""Build the downloadable Windows x64 release from the platform-neutral wheel."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from build_release import (
    EXTENSION_FILES,
    ROOT,
    UV_SOURCE,
    VERSION,
    make_zip,
    scan_release,
    sha256,
    validate_gmail_client,
    write_extension_zip,
)


RELEASE_NAME = f"SF-Home-Finder-{VERSION}-Windows-x64"
COMMAND_FILES = (
    "2 Install SF Home Finder.cmd",
    "3 Open SF Home Finder.cmd",
    "Repair SF Home Finder.cmd",
    "Verify SF Home Finder.cmd",
    "Uninstall SF Home Finder.cmd",
)


def run_checked(*args: str) -> None:
    cache = ROOT / "build" / "uv-cache"
    cache.mkdir(parents=True, exist_ok=True)
    subprocess.run(args, cwd=ROOT, check=True, env={**os.environ, "UV_CACHE_DIR": str(cache)})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gmail-client-json", type=Path, help="Optional owner-controlled OAuth client JSON")
    parser.add_argument("--uv-bin", type=Path, default=UV_SOURCE)
    args = parser.parse_args()

    if not args.uv_bin.is_file():
        raise SystemExit(f"Pinned uv binary not found: {args.uv_bin}")
    version = subprocess.check_output([args.uv_bin, "--version"], text=True).strip()
    if not version.startswith("uv 0.10.8"):
        raise SystemExit(f"Expected uv 0.10.8, found {version}")

    write_extension_zip()
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    assets = ROOT / "release_assets"
    windows_assets = assets / "windows"
    with tempfile.TemporaryDirectory(prefix="sf-housing-windows-build-") as temporary:
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
        for name in ("1 START HERE.txt", "RELEASE_NOTES.txt"):
            shutil.copy2(windows_assets / name, release_root / name)
        shutil.copy2(ROOT / "LICENSE", release_root / "LICENSE.txt")
        for name in COMMAND_FILES:
            shutil.copy2(windows_assets / name, release_root / name)
        shutil.copy2(assets / "requirements.lock", payload / "requirements.lock")
        shutil.copy2(windows_assets / "payload" / "install.ps1", payload / "install.ps1")
        for path in (windows_assets / "payload" / "tools").iterdir():
            if path.is_file():
                shutil.copy2(path, tools / path.name)
        bridge = payload / "furnished-finder-bridge"
        bridge.mkdir()
        for name in EXTENSION_FILES:
            shutil.copy2(ROOT / "release_assets" / "furnished-finder-bridge" / name, bridge / name)
        shutil.copy2(wheel, payload / wheel.name)

        gmail_injected = bool(args.gmail_client_json)
        if args.gmail_client_json:
            (payload / "gmail-client-secret.json").write_bytes(validate_gmail_client(args.gmail_client_json))

        checksummed = sorted(path for path in payload.rglob("*") if path.is_file() and path.name != "checksums.sha256")
        lines = [f"{sha256(path)}  {path.relative_to(payload).as_posix()}" for path in checksummed]
        (payload / "checksums.sha256").write_text("\n".join(lines) + "\n", encoding="utf-8")
        scan_release(release_root, gmail_injected)

        destination = dist / f"{RELEASE_NAME}.zip"
        make_zip(release_root, destination)
        checksum = sha256(destination)
        destination.with_suffix(".zip.sha256").write_text(f"{checksum}  {destination.name}\n", encoding="utf-8")
        print(destination)
        print(checksum)


if __name__ == "__main__":
    main()
