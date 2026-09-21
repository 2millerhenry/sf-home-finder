#!/usr/bin/env python3
"""Render the recorded numbers as a page worth looking at.

The terminal report is for checking; this is for seeing. Everything comes
from the stored record rather than the network, so it opens instantly, works
offline, and carries no token -- it is a local file, not something to host.

Built around one question, because that is the one the counter was built to
answer: how many people installed this. That number is the headline even when
it is nought, and downloads, visitors and releases are supporting detail set
smaller. Labels are fragments rather than sentences; anything needing a
sentence to justify it probably should not be on the page.

Colours and type are the app's own, read from docs/design.md -- Presidio Forest on
Warm Paper, Pine Ink, Sage Divider -- so this looks like the thing it reports
on rather than a generic dashboard.

Written deliberately for the early state. For the first few weeks there is one
snapshot, no trend and possibly no installs, and a dashboard that looks broken
until it has data is one nobody opens twice. Empty places say what they wait
for rather than showing a zero.
"""

from __future__ import annotations

import html
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

CSS = """
:root {
  --canvas: oklch(97.5% 0.006 85);
  --surface: oklch(99.2% 0.004 85);
  --ink: oklch(21% 0.018 160);
  --muted: oklch(48% 0.014 150);
  --faint: oklch(62% 0.012 150);
  --line: oklch(89% 0.01 145);
  --hair: oklch(93% 0.008 145);
  --accent: oklch(43% 0.09 162);
  --accent-soft: oklch(95% 0.022 155);
  --accent-line: oklch(80% 0.04 155);
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --canvas: oklch(17% 0.012 160);
    --surface: oklch(20.5% 0.014 160);
    --ink: oklch(95% 0.008 145);
    --muted: oklch(70% 0.014 150);
    --faint: oklch(54% 0.012 150);
    --line: oklch(31% 0.014 155);
    --hair: oklch(26% 0.012 158);
    --accent: oklch(76% 0.1 158);
    --accent-soft: oklch(28% 0.035 158);
    --accent-line: oklch(40% 0.05 157);
  }
}
* { box-sizing: border-box; }
html, body { height: 100%; }
body {
  margin: 0; padding: 52px 44px 32px; background: var(--canvas); color: var(--ink);
  font: 400 15px/1.5 -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", sans-serif;
  -webkit-font-smoothing: antialiased; text-rendering: optimizeLegibility;
}
.wrap { max-width: 1060px; margin: 0 auto; min-height: 100%; display: flex; flex-direction: column; }

header { display: flex; align-items: baseline; justify-content: space-between; gap: 16px; flex-wrap: wrap; }
h1 {
  margin: 0; font-size: 20px; font-weight: 680; letter-spacing: -0.02em;
  font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "Segoe UI", sans-serif;
}
.stamp { font-size: 12px; color: var(--faint); font-variant-numeric: tabular-nums; }

.hero { display: grid; grid-template-columns: repeat(3, 1fr); margin: 44px 0 46px; }
.stat { padding: 0 32px; border-left: 1px solid var(--line); }
.stat:first-child { padding-left: 0; border-left: 0; }
.label { font-size: 12px; font-weight: 650; letter-spacing: 0.015em; color: var(--muted); }
.stat .value {
  font-size: 66px; line-height: 1.04; font-weight: 720; letter-spacing: -0.035em;
  margin: 14px 0 9px; font-variant-numeric: tabular-nums;
  font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "Segoe UI", sans-serif;
}
.stat.lead .value { color: var(--accent); }
.stat.waiting .value { font-size: 27px; letter-spacing: -0.02em; color: var(--faint); font-weight: 600; margin-top: 20px; }
.stat .note { font-size: 13px; color: var(--muted); }
.stat .note b { font-weight: 650; color: var(--ink); font-variant-numeric: tabular-nums; }

.strip { margin-bottom: 44px; }
.bars { display: flex; align-items: flex-end; gap: 4px; height: 58px; margin: 14px 0 7px; }
.bar { flex: 1; background: var(--accent-soft); border-radius: 3px 3px 0 0; min-height: 3px; position: relative; }
.bar.on { background: var(--accent); }
.scale { display: flex; justify-content: space-between; font-size: 11.5px; color: var(--faint); font-variant-numeric: tabular-nums; }

main { flex: 1; display: grid; grid-template-columns: 1.6fr 1fr; gap: 54px; align-items: start; }
h2 { font-size: 12px; font-weight: 650; letter-spacing: 0.015em; color: var(--muted); margin: 0 0 14px; }

table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
td { padding: 10px 0; border-top: 1px solid var(--hair); font-size: 14px; }
tr:first-child td { border-top: 0; }
td.right { text-align: right; color: var(--muted); font-size: 13px; }
.tag { font-size: 11px; font-weight: 650; color: var(--accent); margin-left: 8px; }

.meter { position: relative; width: 100%; }
.meter .fill { position: absolute; inset: 0 auto 0 0; background: var(--accent-soft); border-radius: 4px; }
.meter b { position: relative; padding: 0 9px; font-weight: 650; }

.quiet { color: var(--muted); font-size: 13.5px; margin: 0; }
.raised {
  font-size: 30px; font-weight: 700; letter-spacing: -0.028em; color: var(--accent);
  font-variant-numeric: tabular-nums; margin: 2px 0 4px;
  font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "Segoe UI", sans-serif;
}
.side { display: grid; gap: 34px; }
footer {
  margin-top: 40px; padding-top: 15px; border-top: 1px solid var(--hair);
  color: var(--faint); font-size: 11.5px; display: flex; justify-content: space-between;
  gap: 16px; flex-wrap: wrap;
}

@media (max-width: 860px) {
  body { padding: 32px 20px 26px; }
  .hero { grid-template-columns: 1fr; gap: 28px; margin: 30px 0 34px; }
  .stat { padding: 0; border-left: 0; }
  .stat .value { font-size: 50px; margin: 9px 0 7px; }
  main { grid-template-columns: 1fr; gap: 34px; }
}
"""


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _stat(label: str, value: object, note: str = "", kind: str = "") -> str:
    classes = " ".join(filter(None, ["stat", kind]))
    note_html = f'<div class="note">{note}</div>' if note else ""
    return (
        f'<div class="{classes}"><div class="label">{_esc(label)}</div>'
        f'<div class="value">{_esc(value)}</div>{note_html}</div>'
    )


def _installs_stat(counter: dict, counting: bool) -> str:
    """The headline. Nought is a real answer and keeps the same prominence."""
    if not counting:
        return _stat("People installing", "not counting yet", "counter not set up", "waiting")
    days = sorted(counter)
    people = sum(int(counter[d].get("unique") or 0) for d in days)
    if not people:
        return _stat("People installing", "nobody yet", "counter live, waiting", "waiting")
    fetches = sum(int(counter[d].get("hits") or 0) for d in days)

    # Where they came from, which is the only way a Windows audience shows up
    # as anything other than an unexplained gap between installs and
    # downloads. An address is attributed to whatever it fetched first that
    # day, so one person counts once, on one platform.
    where: dict[str, int] = {}
    for day in days:
        for source, n in (counter[day].get("by") or {}).items():
            where[source] = where.get(source, 0) + int(n or 0)
    label = {"script": "Mac one-liner", "mac": "Mac zip", "windows": "Windows"}
    spread = " · ".join(
        f"<b>{n}</b> {label.get(k, k)}" for k, n in sorted(where.items(), key=lambda kv: -kv[1])
    )
    note = f"one address per day · <b>{fetches}</b> fetches"
    return _stat("People installing", people, f"{note}<br>{spread}" if spread else note, "lead")


def _downloads_stat(snapshots: list[dict]) -> str:
    if not snapshots:
        return ""
    latest = snapshots[-1]
    total = int(latest.get("total") or 0)
    parts = [
        f"<b>{n}</b> {_esc(name)}"
        for name, n in sorted((latest.get("by_platform") or {}).items(), key=lambda kv: -kv[1])
        if n
    ]
    if len(snapshots) >= 2:
        moved = total - int(snapshots[-2].get("total") or 0)
        parts.insert(0, f"<b>{moved:+d}</b> since {_esc(snapshots[-2].get('taken'))}")
    else:
        parts.insert(0, "first snapshot")
    return _stat("Downloads", total, " · ".join(parts))


def _visitors_stat(traffic: dict) -> str:
    window = (traffic or {}).get("window") or {}
    reads = sorted(k for k in window if k.startswith("views:"))
    if not reads:
        return _stat("Unique visitors", "—", "needs <b>gh auth login</b>", "waiting")
    return _stat(
        "Unique visitors",
        int(window[reads[-1]].get("uniques") or 0),
        "repo page · 14 days",
    )


def _activity(traffic: dict, counter: dict) -> str:
    """Fourteen days of arrivals, so a flat run looks different from a quiet one.

    Installs where there are any, visitors otherwise -- one bar per day either
    way. A run of empty days is the point of drawing this: a single total
    cannot tell a steady trickle apart from one busy afternoon a fortnight ago.
    """
    views = {d: int(v.get("uniques") or 0) for d, v in ((traffic or {}).get("views") or {}).items()}
    installs = {d: int(v.get("unique") or 0) for d, v in (counter or {}).items()}
    series, kind = (installs, "installs") if any(installs.values()) else (views, "visitors")
    if not series:
        return ""

    today = datetime.now(timezone.utc).date()
    days = [(today - timedelta(days=n)).isoformat() for n in range(13, -1, -1)]
    peak = max((series.get(d, 0) for d in days), default=0)
    if not peak:
        return ""

    bars = "".join(
        f'<div class="bar{" on" if series.get(d, 0) else ""}"'
        f' style="height:{max(3, round(series.get(d, 0) / peak * 100))}%"'
        f' title="{_esc(d)}: {series.get(d, 0)}"></div>'
        for d in days
    )
    total = sum(series.get(d, 0) for d in days)
    return (
        f'<div class="strip"><div class="label">Last 14 days · {_esc(kind)} '
        f"· {total} total</div>"
        f'<div class="bars">{bars}</div>'
        f'<div class="scale"><span>{_esc(days[0])}</span><span>{_esc(days[-1])}</span></div></div>'
    )


def _releases(snapshots: list[dict], limit: int = 6) -> str:
    if not snapshots:
        return ""
    latest = snapshots[-1]
    by_release = latest.get("by_release") or {}
    if not by_release:
        return ""
    published = latest.get("published") or {}
    detail = latest.get("detail") or {}
    order = sorted(by_release, key=lambda tag: (published.get(tag, ""), tag), reverse=True)[:limit]
    peak = max((by_release[t] for t in order), default=0) or 1

    rows = []
    for tag in order:
        count = int(by_release[tag] or 0)
        # A platform with no downloads is not news, and "0 Windows" on every
        # row buries the rows where Windows actually did something.
        spread = ", ".join(f"{n} {p}" for p, n in sorted((detail.get(tag) or {}).items()) if n)
        newest = '<span class="tag">newest</span>' if tag == order[0] else ""
        rows.append(
            f"<tr><td>{_esc(tag)}{newest}</td>"
            f'<td class="right">{_esc(published.get(tag, "—"))}</td>'
            f'<td style="width:38%"><div class="meter">'
            f'<div class="fill" style="width:{max(7, round(count / peak * 100))}%"></div>'
            f"<b>{count}</b></div></td>"
            f'<td class="right">{_esc(spread)}</td></tr>'
        )
    return "<div><h2>By release</h2><table>" + "".join(rows) + "</table></div>"


def _donations(donations: dict) -> str:
    """What the app has raised, and from how many people.

    Ko-fi keeps the real ledger; this only shows what the webhook has relayed
    since it was connected, which is said plainly rather than implied, because
    a total that silently starts partway through is worse than no total.
    """
    totals = (donations or {}).get("total") or {}
    count = int((donations or {}).get("count") or 0)
    if not count:
        return (
            '<div><h2>Donations</h2><p class="quiet">'
            "Nothing yet, or the Ko-fi webhook is not connected.</p></div>"
        )

    days = (donations or {}).get("days") or {}
    headline = " · ".join(
        f"{code} {cents / 100:,.2f}" for code, cents in sorted(totals.items())
    )
    people = f"{count} donation{'' if count == 1 else 's'}"
    first = min(days) if days else None

    rows = "".join(
        f"<tr><td>{_esc(date)}</td>"
        f'<td class="right" style="color:var(--ink)">{(entry.get("cents") or 0) / 100:,.2f}</td>'
        f'<td class="right">{entry.get("count") or 0}</td></tr>'
        for date, entry in sorted(days.items(), reverse=True)[:6]
    )
    since = f"<p class=\"quiet\" style=\"margin-top:10px\">Since {_esc(first)}.</p>" if first else ""
    return (
        '<div><h2>Donations</h2>'
        f'<div class="raised">{_esc(headline)}</div>'
        f'<div class="label" style="margin-bottom:12px">{_esc(people)}</div>'
        f"<table>{rows}</table>{since}</div>"
    )


def _trend(snapshots: list[dict]) -> str:
    if len(snapshots) < 2:
        return (
            '<div><h2>Trend</h2><p class="quiet">'
            "GitHub keeps no history. Run this again next week and the change "
            "appears here.</p></div>"
        )
    rows = []
    previous = None
    for snapshot in snapshots[-8:]:
        total = int(snapshot.get("total") or 0)
        moved = "" if previous is None else f"{total - previous:+d}"
        rows.append(
            f"<tr><td>{_esc(snapshot.get('taken'))}</td>"
            f'<td class="right" style="color:var(--ink)">{total}</td>'
            f'<td class="right" style="color:var(--accent)">{_esc(moved)}</td></tr>'
        )
        previous = total
    return "<div><h2>Trend</h2><table>" + "".join(rows) + "</table></div>"


def render(store: dict) -> str:
    snapshots = store.get("snapshots") or []
    traffic = store.get("traffic") or {}
    counter = store.get("counter") or {}
    counting = bool(store.get("counting"))
    donations = store.get("donations") or {}
    generated = datetime.now(timezone.utc).strftime("%-d %B %Y, %H:%M UTC")

    hero = _installs_stat(counter, counting) + _downloads_stat(snapshots) + _visitors_stat(traffic)

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SF Home Finder — reach</title>
<style>{CSS}</style></head>
<body><div class="wrap">
<header><h1>SF Home Finder</h1><div class="stamp">{_esc(generated)}</div></header>
<div class="hero">{hero}</div>
{_activity(traffic, counter)}
<main>{_releases(snapshots)}<div class="side">{_trend(snapshots)}{_donations(donations)}</div></main>
<footer>
  <span>Installs count one address per day, storing none.</span>
  <span>Downloads include upgrades and your own testing.</span>
</footer>
</div></body></html>
"""


def write(store: dict, target: Path) -> Path:
    target.write_text(render(store), encoding="utf-8")
    return target
