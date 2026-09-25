#!/usr/bin/env python3
"""Build the text of a release page.

The release page is the download page -- the repository's website field points
at it -- so most people reading it have never seen the app. It leads with what
the app is and how to get it, and keeps the version's own changes and the
checksum folded away, because neither is what a first-time reader came for.

Written once here rather than by hand each release, so the page a stranger
lands on does not depend on which day it was published.

    python scripts/release_page.py 0.4.5 > body.md

Upload BOTH archives. install.sh prefers the .tar.xz and silently falls back to
the .zip, so a release published with only the zip costs every user the larger
download and nothing anywhere fails to say so. README's headline size figure is
prose, not computed -- when the first tarball release ships, that line changes
from 19 MB to the tarball's size.
    gh release create v0.4.5 dist/...tar.xz dist/...zip --notes-file body.md

Both macOS archives go up, and the tarball is not optional: the one-line
install looks for it first and falls back to the zip, so a release published
without it quietly hands every user the larger download.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO = "2millerhenry/sf-home-finder"
RAW = f"https://raw.githubusercontent.com/{REPO}/main/docs/screenshots"


def changes_for(version: str) -> str:
    """This version's entry from the release notes, without its heading.

    The notes file is the record; repeating it in the release page by hand is
    how the two drift apart.
    """
    notes = (ROOT / "release_assets" / "RELEASE_NOTES.txt").read_text()
    heading = f"SF Home Finder {version}\n"
    if heading not in notes:
        raise SystemExit(f"No entry for {version} in RELEASE_NOTES.txt")
    body = notes.split(heading, 1)[1]
    for line in body.splitlines():
        if line.startswith("SF Home Finder "):
            body = body.split(line, 1)[0]
            break
    # The notes file is hard-wrapped for a terminal; GitHub honours those breaks
    # literally and the result reads like a ransom note. Paragraphs are kept,
    # the wrapping inside them is not.
    return "\n\n".join(
        " ".join(paragraph.split()) for paragraph in body.strip().split("\n\n")
    )


def main() -> None:
    version = sys.argv[1] if len(sys.argv) > 1 else None
    if not version:
        raise SystemExit("usage: release_page.py VERSION")
    archive = ROOT / "dist" / f"SF-Home-Finder-{version}-macOS-arm64.zip"
    if not archive.is_file():
        raise SystemExit(f"Build it first: {archive} is missing")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()

    # The command below downloads the tarball, not the zip, so its checksum is
    # the one somebody checking that download needs -- and the size printed
    # under the command has to be the size of the file the command fetches.
    # Both fall back to the zip's, because a release built before the tarball
    # existed still has a page to publish.
    tarball = ROOT / "dist" / f"SF-Home-Finder-{version}-macOS-arm64.tar.xz"
    if tarball.is_file():
        mac_size = f"{round(tarball.stat().st_size / 1_000_000)} MB"
        mac_checksum = f"\n{hashlib.sha256(tarball.read_bytes()).hexdigest()}  {tarball.name}"
    else:
        mac_size = f"{round(archive.stat().st_size / 1_000_000)} MB"
        mac_checksum = ""

    # Named only when it is actually being published. A page that offers a
    # Windows download the release does not carry sends somebody to a 404, and
    # a page that stays silent about Windows when the zip is right there sends
    # them away for no reason.
    windows = ROOT / "dist" / f"SF-Home-Finder-{version}-Windows-x64.zip"
    if windows.is_file():
        windows_digest = hashlib.sha256(windows.read_bytes()).hexdigest()
        windows_install = f"""
### On Windows

Windows 10 or 11, 64-bit. Download **{windows.name}** from Assets below, unpack it, and double-click `2 Install SF Home Finder.cmd`.

Windows will say the publisher is unrecognised, because this is not code-signed. Choose **More info**, then **Run anyway**. It never asks for an administrator.
"""
        windows_checksum = f"\n{windows_digest}  {windows.name}"
    else:
        windows_install = """
### On Windows

Not in this release. The most recent Windows build is on an [earlier release](https://github.com/%s/releases).
""" % REPO
        windows_checksum = ""

    # The install script's own checksum, and a tag-pinned URL for it. Piping
    # a URL to bash asks somebody to run code they have not read, from a host
    # that could serve them something other than what it serves a reviewer.
    # Both are fair objections and neither needs answering with trust: the tag
    # is immutable, so the file read is the file run, and the digest says the
    # file is the one this release was cut from.
    installer = ROOT / "install.sh"
    installer_digest = hashlib.sha256(installer.read_bytes()).hexdigest() if installer.is_file() else ""
    verify = f"""
<details>
<summary>Rather not pipe a URL into bash? Read it first.</summary>

<br>

Fair. That command asks you to run code you have not read, served by a host you have no way to audit. Here is the same install with neither of those:

```
curl -fsSL -o install.sh https://raw.githubusercontent.com/{REPO}/v{version}/install.sh
less install.sh
bash install.sh
```

A tag is immutable, so the file you read is the file you run, and it comes straight from the repository with nothing in between. Its checksum for this release:

```
{installer_digest}  install.sh
```

The one-line command above is the same script, served through a Cloudflare worker that counts installs and changes nothing else ([its source is here](https://github.com/{REPO}/blob/v{version}/deploy/install-counter/worker.js)). Using the URL on this line instead costs you nothing and me one number.

Or skip scripts altogether: download the ZIP below, check it against its checksum, and read `payload/install.sh` before you run it. Nothing in this install needs a password or an administrator, and it only writes to `~/Library/Application Support/SF Home Finder` and `~/.local/bin`.

</details>

<details>
<summary>Check that this download was built from this source.</summary>

<br>

**GitHub built it, and signed a record saying so.** Nothing here was uploaded from anybody's laptop:

```
gh attestation verify SF-Home-Finder-{version}-macOS-arm64.tar.xz -R {REPO}
```

That names the workflow and the exact commit the archive was built from.

**And you can rebuild it yourself and get the same file.** Every timestamp in both archives is pinned, so the build is reproducible — a GitHub runner and a laptop building this commit produce byte-identical archives:

```
git clone --branch v{version} https://github.com/{REPO}
cd sf-home-finder && python scripts/build_release.py
shasum -a 256 dist/SF-Home-Finder-{version}-macOS-arm64.tar.xz
```

The digest matches the one above. Between the two, the chain from the source you can read to the file you run has no step that asks you to trust me.

</details>
""" if installer_digest else ""

    print(f"""SF housing is miserable. Finding a place feels impossible, and paying for it is even worse.

SF Home Finder watches 18 rental sites, from public housing listings to Zillow, and refreshes twice a day. It ranks every place by your budget, neighborhoods, and everything else you care about. Free forever. No ads. Runs entirely on your laptop.

<img src="{RAW}/shortlist.png" alt="The shortlist: homes ranked by how well they match, each row showing the score, rent, neighborhood and source">

## Install

Takes about three minutes and never asks for a password.

### On a Mac

Apple Silicon (M1 or later), macOS 15.6 or newer. Intel Macs are not supported yet. Paste this into Terminal:

```
curl -fsSL https://sf-home-finder-install.sfhomefinder.workers.dev/install.sh | bash
```

*{mac_size} · no password, no admin · your browser opens by itself when it is done*
{verify}{windows_install}
Once it opens, fill in **Your deal**, press save, and the first search starts.

## Opening it later

Open **http://127.0.0.1:8000** and bookmark it. On a Mac you can also type `homefinder` in a terminal; on Windows, use the **Open SF Home Finder** shortcut in the folder you unpacked. It runs on its own, so there is never anything to start.

## Free, private, and quiet

No account, no server, no subscription. It collects nothing about you and your search never leaves your laptop. It never emails a landlord or acts in your name — it reads what is already public and hands it to you.

<details>
<summary>Prefer to click? Download the ZIP below</summary>

<br>

Unpack it, then **Control-click** `2 Install SF Home Finder.command` and choose **Open**, twice. Control-click rather than double-click because the app is not code-signed; the command above has no such prompt, since macOS only marks what a browser downloaded.

</details>

<details>
<summary>What changed in {version}, and checksum</summary>

<br>

{changes_for(version)}

```
{digest}  {archive.name}{mac_checksum}{windows_checksum}
```

</details>""")


if __name__ == "__main__":
    main()
