#!/usr/bin/env python3
"""How many people have installed this, as closely as GitHub can say.

The one-line curl install downloads the same file the ZIP link does -- it asks
the API for the latest release and fetches that release's macOS zip -- so
GitHub's own download counter already counts both ways in. Nothing has to be
added to the app to learn this, and nothing about the person installing is
collected or could be: this reads a tally GitHub keeps whether anybody looks
at it or not.

What it is not is a count of people. Every upgrade is a second download by
somebody already counted, the developer's own testing is in there, and mirrors
and crawlers pull releases nobody ever runs. It is a ceiling on installs.

The useful part is the trend, and that is the part GitHub does not keep -- the
API reports a running total and no history at all, so a number that has been
43 for a month and a number that reached 43 yesterday are indistinguishable
unless somebody writes them down. Each run records a dated snapshot and
reports what moved since the last one. Run it weekly and those deltas are the
closest honest answer to "is anybody new showing up".

A download is an integer with nobody attached, so none of it can be turned
into a count of people. GitHub does keep one real per-person number -- unique
visitors to the repository page, deduplicated by address -- and discards it
after fourteen days, so that is read with a token when one is available and
accumulated here. It counts visitors rather than installers, which is a
different and smaller claim, but it is the only honest people-number available
without asking the app to report on whoever is running it.

    uv run python scripts/installs.py             # totals, and what moved since last run
    uv run python scripts/installs.py --dry-run   # the same, recording nothing
    uv run python scripts/installs.py --history   # every snapshot taken so far
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
REPO = "2millerhenry/sf-home-finder"
HISTORY = HERE / "installs.json"
API = f"https://api.github.com/repos/{REPO}/releases?per_page=100"
TRAFFIC = f"https://api.github.com/repos/{REPO}/traffic"

REQUEST_TIMEOUT_SECONDS = 20.0


# Credentials for the install counter, for when the environment does not carry
# them. An alias only exists in a shell that has read the profile defining it,
# which is why this has to work without one: a scheduled run, a fresh tab, a
# script called from anywhere.
CONFIG = Path.home() / ".config" / "sf-home-finder" / "counter.env"


def configured(name: str) -> str:
    """A setting from the environment, falling back to the config file.

    The environment wins, so a one-off run can override what is stored without
    editing anything.
    """
    value = os.environ.get(name, "").strip()
    if value:
        return value
    try:
        for line in CONFIG.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, stored = line.partition("=")
            if key.strip() == name:
                return stored.strip().strip("'\"")
    except OSError:
        return ""
    return ""


def github_token() -> str | None:
    """A token, from the environment or from whoever is already signed in.

    The people count needs one and the download counts do not, so the cost of
    a missing token is a smaller report rather than a failure. Asking the gh
    CLI is what makes this a single command: the traffic window GitHub keeps
    is fourteen days, and a run skipped because the incantation was forgotten
    loses those days permanently.
    """
    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        token = os.environ.get(name)
        if token:
            return token
    try:
        finished = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        # No gh, or it hung. Neither is worth failing the run over.
        return None
    token = finished.stdout.strip()
    return token if finished.returncode == 0 and token else None


def platform_of(asset_name: str) -> str | None:
    """Which download this is, or None if it is not one.

    A checksum file sits beside each archive for people who verify by hand. It
    is a few dozen bytes, not an install, and counting it would turn one
    careful manual download into two.

    From 0.5.7 the Mac release is two archives, and the .tar.xz is the one the
    one-line install fetches -- so it is where most Mac downloads will land.
    Counting only zips would have made the busiest path the invisible one.
    """
    if not asset_name.endswith((".zip", ".tar.xz")):
        return None
    if "macOS" in asset_name:
        return "macOS"
    if "Windows" in asset_name:
        return "Windows"
    return "other"


def fetch_releases() -> list[dict]:
    """Every release, newest first, following pagination to the end.

    Unauthenticated works and is what a fresh clone gets -- download counts are
    public. GITHUB_TOKEN is used when it is set, purely for the larger rate
    limit; it changes nothing about the answer.
    """
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "sf-home-finder-installs",
    }
    token = github_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    releases: list[dict] = []
    url: str | None = API
    with httpx.Client(
        timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=True, headers=headers
    ) as client:
        while url:
            response = client.get(url)
            response.raise_for_status()
            page = response.json()
            if not isinstance(page, list):
                break
            releases.extend(page)
            next_link = response.links.get("next")
            url = next_link.get("url") if next_link else None
    return releases


def fetch_traffic() -> dict[str, dict[str, dict[str, int]]]:
    """Unique visitors and cloners per day, or empty if GitHub will not say.

    This is the only count of *people* GitHub offers -- a download is an
    integer with nobody attached, but a visitor is deduplicated by address. It
    is also the one number GitHub throws away: the window is fourteen days and
    older days are gone for good, which is the whole reason this is recorded
    here rather than read when wanted.

    Needs a token with push access, because traffic is the owner's to see.
    Without one this is simply absent -- the download counts above are public
    and do not depend on it.
    """
    token = github_token()
    if not token:
        return {}
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "sf-home-finder-installs",
        "Authorization": f"Bearer {token}",
    }
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out: dict[str, dict[str, dict[str, int]]] = {}
    with httpx.Client(
        timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=True, headers=headers
    ) as client:
        for kind, key in (("views", "views"), ("clones", "clones")):
            try:
                response = client.get(f"{TRAFFIC}/{kind}")
                response.raise_for_status()
                payload = response.json()
            except (httpx.HTTPError, ValueError):
                continue
            days = payload.get(key) if isinstance(payload, dict) else None
            if not isinstance(days, list):
                continue
            # The window total is deduplicated across all fourteen days, which
            # the day series is not -- one person visiting on three days is
            # three daily uniques and one window unique. Only this field
            # answers "how many people", so it is kept as its own series,
            # stamped with the day it was read.
            out.setdefault("window", {})[f"{kind}:{today}"] = {
                "uniques": int(payload.get("uniques") or 0),
                "count": int(payload.get("count") or 0),
            }
            out[kind] = {
                str(day.get("timestamp", ""))[:10]: {
                    "count": int(day.get("count") or 0),
                    "uniques": int(day.get("uniques") or 0),
                }
                for day in days
                if isinstance(day, dict) and day.get("timestamp")
            }
    return out


def merge_traffic(stored: dict, fetched: dict) -> dict:
    """Keep every day either side knows about.

    Fetched days win on conflict: a day still inside the window may have been
    partial when it was last recorded.
    """
    merged: dict[str, dict[str, dict[str, int]]] = {}
    for kind in set(stored) | set(fetched):
        days = dict(stored.get(kind) or {})
        days.update(fetched.get(kind) or {})
        merged[kind] = dict(sorted(days.items()))
    return merged


def fetch_counter() -> dict[str, dict[str, int]] | None:
    """Installs per day from the counter, or None if it is not configured.

    None and {} are different answers and the page says different things
    about them: nothing set up yet, versus set up and waiting for the first
    install. Collapsing them tells somebody who just deployed the counter to
    go and deploy the counter.

    This is the only number here that counts installs rather than downloads:
    one per address per day, taken as the install script is served. Set
    SF_INSTALL_COUNTER_URL and SF_INSTALL_COUNTER_TOKEN to the deployed worker
    and its stats token; without them everything else still reports.
    """
    base = configured("SF_INSTALL_COUNTER_URL").rstrip("/")
    token = configured("SF_INSTALL_COUNTER_TOKEN")
    if not base or not token:
        return None
    try:
        response = httpx.get(
            f"{base}/stats",
            timeout=REQUEST_TIMEOUT_SECONDS,
            follow_redirects=True,
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        return None
    if not isinstance(payload, dict):
        return {}
    days = payload.get("days")
    installs = (
        {
            str(date): {
                "unique": int((entry or {}).get("unique") or 0),
                "hits": int((entry or {}).get("hits") or 0),
            }
            for date, entry in days.items()
            if isinstance(entry, dict)
        }
        if isinstance(days, dict)
        else {}
    )
    donated = payload.get("donations")
    return {"days": installs, "donations": donated if isinstance(donated, dict) else {}}


def merge_counter(stored: dict, fetched: dict) -> dict:
    """Every day either side knows about, fetched winning on conflict.

    The worker forgets a day after ninety days. This file is what makes the
    record outlive that, so a day that has aged out upstream is kept rather
    than dropped.
    """
    merged = dict(stored or {})
    merged.update(fetched or {})
    return dict(sorted(merged.items()))


def donations_report(donations: dict) -> str:
    """Total raised and how many people, or nothing if none have arrived."""
    totals = (donations or {}).get("total") or {}
    count = int((donations or {}).get("count") or 0)
    if not count:
        return ""
    amounts = "  ".join(
        f"{code} {cents / 100:,.2f}" for code, cents in sorted(totals.items())
    )
    days = (donations or {}).get("days") or {}
    recent = max(days) if days else "—"
    lines = ["", "  Donations (Ko-fi, since the webhook was connected)"]
    lines.append(f"    Raised          {amounts:>10}")
    lines.append(f"    Donations       {count:>10}   most recent {recent}")
    return "\n".join(lines)


def installs_report(counter: dict) -> str:
    """Installs per day, and what they add up to."""
    if not counter:
        return ""
    days = sorted(counter)
    unique = sum(int(counter[d].get("unique") or 0) for d in days)
    hits = sum(int(counter[d].get("hits") or 0) for d in days)
    active = [d for d in days if int(counter[d].get("unique") or 0) > 0]

    lines = ["", "  Installs (one per address per day, counted at the script)"]
    lines.append(f"    People installing  {unique:>4}   over {len(days)} days recorded")
    lines.append(
        f"    Times fetched      {hits:>4}   at least; retries a minute apart merge"
    )
    if active:
        recent = active[-7:]
        week = sum(int(counter[d].get("unique") or 0) for d in recent)
        lines.append(f"    Last 7 active days {week:>4}   most recent {active[-1]}")
    return "\n".join(lines)


def tally(releases: list[dict]) -> dict:
    """Downloads per release and per platform, plus the totals."""
    by_release: dict[str, int] = {}
    by_platform: dict[str, int] = {}
    published: dict[str, str] = {}
    detail: dict[str, dict[str, int]] = {}

    for release in releases:
        # A draft is visible only to whoever holds a token, so counting one
        # would make this report say something different depending on whether
        # GITHUB_TOKEN happened to be set. Nobody can install a draft anyway.
        if release.get("draft"):
            continue
        tag = str(release.get("tag_name") or "")
        if not tag:
            continue
        stamp = release.get("published_at")
        if isinstance(stamp, str):
            published[tag] = stamp[:10]
        for asset in release.get("assets") or []:
            platform = platform_of(str(asset.get("name") or ""))
            if platform is None:
                continue
            count = int(asset.get("download_count") or 0)
            by_release[tag] = by_release.get(tag, 0) + count
            by_platform[platform] = by_platform.get(platform, 0) + count
            detail.setdefault(tag, {})
            detail[tag][platform] = detail[tag].get(platform, 0) + count

    return {
        "taken": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "total": sum(by_release.values()),
        "by_platform": by_platform,
        "by_release": by_release,
        "published": published,
        "detail": detail,
    }


def read_store() -> dict:
    """Everything recorded so far: the snapshots, and the traffic day series.

    A missing, empty or hand-edited file is an absence of history rather than
    an error: the point of this script is the number, and losing the trend is
    not a reason to refuse to print it.
    """
    empty: dict = {"snapshots": [], "traffic": {}, "counter": {}, "donations": {}}
    try:
        raw = json.loads(HISTORY.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty
    if not isinstance(raw, dict):
        return empty
    snapshots = raw.get("snapshots")
    traffic = raw.get("traffic")
    counter = raw.get("counter")
    return {
        "snapshots": [
            s for s in snapshots if isinstance(s, dict) and s.get("taken")
        ]
        if isinstance(snapshots, list)
        else [],
        "traffic": traffic if isinstance(traffic, dict) else {},
        "counter": counter if isinstance(counter, dict) else {},
        "donations": raw.get("donations") if isinstance(raw.get("donations"), dict) else {},
        "counting": bool(raw.get("counting")),
    }


def write_store(store: dict) -> None:
    """Store the record, all at once or not at all.

    Written beside the target and renamed over it: a run killed mid-write
    would otherwise leave a half-written file, and this file is the only copy
    of days GitHub has already forgotten.
    """
    temporary = HISTORY.with_name(f"{HISTORY.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(store, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        temporary.replace(HISTORY)
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def record(snapshot: dict, snapshots: list[dict]) -> list[dict]:
    """Today's snapshot, replacing an earlier one from the same day.

    Running this three times in an afternoon should leave one row for the
    afternoon, not three -- otherwise the deltas between consecutive rows stop
    meaning "what changed since last time I looked".
    """
    kept = [s for s in snapshots if s.get("taken") != snapshot["taken"]]
    kept.append(snapshot)
    kept.sort(key=lambda s: str(s.get("taken")))
    return kept


def previous(snapshots: list[dict], today: str) -> dict | None:
    """The most recent snapshot from before today, if there is one."""
    earlier = [s for s in snapshots if str(s.get("taken")) < today]
    return earlier[-1] if earlier else None


def change(now: int, before: int | None) -> str:
    if before is None:
        return ""
    return f"{now - before:+d}"


def report(
    snapshot: dict,
    prior: dict | None,
    traffic: dict | None = None,
    counter: dict | None = None,
    donations: dict | None = None,
    limit: int = 8,
) -> str:
    lines: list[str] = []
    since = f" since {prior['taken']}" if prior else ""

    lines.append(f"SF Home Finder downloads — {snapshot['taken']}")
    lines.append("")

    prior_total = int(prior["total"]) if prior else None
    delta = change(snapshot["total"], prior_total)
    lines.append(f"  All releases      {snapshot['total']:>6}   {delta}{since if delta else ''}")

    by_release = snapshot["by_release"]
    published = snapshot.get("published", {})
    if by_release:
        newest = max(by_release, key=lambda tag: (published.get(tag, ""), tag))
        prior_release = (prior or {}).get("by_release", {}).get(newest) if prior else None
        delta = change(by_release[newest], prior_release)
        lines.append(
            f"  Newest {newest:<10} {by_release[newest]:>6}   {delta}{since if delta else ''}"
        )

    lines.append("")
    for platform, count in sorted(snapshot["by_platform"].items()):
        prior_platform = (prior or {}).get("by_platform", {}).get(platform) if prior else None
        lines.append(f"  {platform:<16} {count:>6}   {change(count, prior_platform)}")

    lines.append("")
    lines.append("  Per release")
    ordered = sorted(
        by_release, key=lambda tag: (published.get(tag, ""), tag), reverse=True
    )
    for tag in ordered[:limit]:
        detail = snapshot.get("detail", {}).get(tag, {})
        spread = ", ".join(f"{n} {p}" for p, n in sorted(detail.items()))
        lines.append(
            f"    {tag:<10} {published.get(tag, '—'):<12} {by_release[tag]:>4}   {spread}"
        )
    if len(ordered) > limit:
        lines.append(f"    … and {len(ordered) - limit} older")

    installs = installs_report(counter or {})
    if installs:
        lines.append(installs)

    people = people_report(traffic or {})
    if people:
        lines.append(people)

    money = donations_report(donations or {})
    if money:
        lines.append(money)

    if prior is None:
        lines.append("")
        lines.append(
            "  First snapshot — nothing to compare against yet. Run it again in a"
        )
        lines.append("  week and this reports what moved.")

    return "\n".join(line.rstrip() for line in lines)


def people_report(traffic: dict) -> str:
    """What the traffic series says about people, with its limits stated.

    Three numbers that are easy to mistake for each other. GitHub's window
    figure is the only true count of people: it deduplicates across the whole
    fourteen days, so somebody who came back on five of them is one visitor.
    Summing the day series instead counts that person five times, which is why
    the lifetime figure below is labelled a ceiling rather than a count --
    nothing links a Tuesday visitor to a Thursday one once the window has
    moved on. The gap between the two is roughly how much people come back.

    All of it is visitors to the repository page, which is not the same as
    installers: some who look never install, and anyone arriving by a direct
    link to the release file is never a visitor at all.
    """
    views = (traffic or {}).get("views") or {}
    window = (traffic or {}).get("window") or {}
    if not views and not window:
        return ""

    lines = ["", "  People (repo visitors, the only per-person count GitHub keeps)"]

    view_windows = sorted(k for k in window if k.startswith("views:"))
    if view_windows:
        latest = window[view_windows[-1]]
        asof = view_windows[-1].split(":", 1)[1]
        lines.append(
            f"    Unique visitors    {int(latest.get('uniques') or 0):>4}"
            f"   deduplicated, 14 days to {asof}"
        )

    if views:
        days = sorted(views)
        lifetime = sum(int(views[d].get("uniques") or 0) for d in days)
        active = [d for d in days if int(views[d].get("uniques") or 0) > 0]
        lines.append(
            f"    Lifetime ceiling   {lifetime:>4}"
            f"   counts returns again, over {len(days)} days kept"
        )
        if active:
            lines.append(
                f"    Days anyone came   {len(active):>4}   most recent {active[-1]}"
            )
    return "\n".join(lines)


def history_report(snapshots: list[dict]) -> str:
    if not snapshots:
        return "No snapshots recorded yet."
    lines = ["Recorded snapshots", ""]
    running: int | None = None
    for snapshot in snapshots:
        total = int(snapshot.get("total") or 0)
        lines.append(
            f"  {snapshot.get('taken')}   {total:>6}   {change(total, running)}".rstrip()
        )
        running = total
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the numbers without recording a snapshot",
    )
    parser.add_argument(
        "--history",
        action="store_true",
        help="print every snapshot recorded so far and stop",
    )
    parser.add_argument(
        "--page",
        action="store_true",
        help="also write reach.html and open it",
    )
    args = parser.parse_args(argv)

    store = read_store()
    snapshots = store["snapshots"]

    if args.history:
        print(history_report(snapshots))
        return 0

    try:
        releases = fetch_releases()
    except httpx.HTTPError as error:
        print(f"Could not reach GitHub: {error}", file=sys.stderr)
        return 1

    # Absent rather than fatal. Traffic needs a token and the download counts
    # do not, so a missing one costs the people section and nothing else.
    traffic = merge_traffic(store["traffic"], fetch_traffic())
    live = fetch_counter()
    counter = merge_counter(store["counter"], (live or {}).get("days") or {})
    # Ko-fi is the record for donations; the worker only relays it. Last answer
    # wins rather than merging, so a refund or correction there shows here.
    donations = (live or {}).get("donations") or store.get("donations") or {}

    snapshot = tally(releases)
    prior = previous(snapshots, snapshot["taken"])
    print(report(snapshot, prior, traffic, counter, donations))

    if not traffic:
        print(
            "\n  (No people count. It needs a token: sign in with `gh auth login`,"
            "\n   or set GITHUB_TOKEN. The download counts above are public and fine.)"
        )

    updated = {
        "snapshots": record(snapshot, snapshots),
        "traffic": traffic,
        "counter": counter,
        "donations": donations,
        "counting": live is not None or bool(store.get("counting")),
    }
    if not args.dry_run:
        write_store(updated)

    if args.page:
        # Imported here so the everyday run does not pay for the renderer.
        sys.path.insert(0, str(HERE))
        import installs_page

        target = installs_page.write(updated, ROOT / "reach.html")
        print(f"\n  Page written to {target}")
        # Opened only when a person is watching. The scheduled run exists to
        # keep the record from going stale, and a browser window arriving on
        # its own every morning is how somebody comes to resent it.
        interactive = sys.stdout.isatty()
        if interactive and sys.platform == "darwin":
            subprocess.run(["open", str(target)], check=False)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
