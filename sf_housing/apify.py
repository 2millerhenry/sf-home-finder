from __future__ import annotations

import re
import json
from datetime import UTC, datetime
from pathlib import Path

from .gmail_alerts import write_private


class ApifyTokenError(ValueError):
    pass


class ApifyTokenStore:
    """Keep the optional Apify API token in a local private file."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.usage_path = self.path.with_name("apify-usage.json")
        # Its own file rather than a key in the usage record, which is wiped
        # every month: filling the board is a once-per-install event.
        self.first_scan_path = self.path.with_name("apify-first-scan-done")
        self.first_group_scan_path = self.path.with_name("apify-first-group-scan-done")

    @property
    def token(self) -> str | None:
        try:
            value = self.path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return value if self._valid(value) else None

    @property
    def is_configured(self) -> bool:
        return self.token is not None

    def save(self, token: str) -> None:
        value = token.strip()
        if not self._valid(value):
            raise ApifyTokenError("Paste a valid Apify API token beginning with apify_api_.")
        write_private(self.path, value)

    def _first_scan_path(self, kind: str) -> Path:
        return self.first_group_scan_path if kind == "groups" else self.first_scan_path

    def first_scan_pending(self, kind: str = "marketplace") -> bool:
        """Whether this source has yet to fill the board once.

        An empty board is the worst moment to be frugal: the first read is
        what makes the source look like it works at all. Every read after it
        only has to catch what is new, which is a far smaller job. Groups keep
        their own marker, because connecting them is its own separate moment.
        """
        return not self._first_scan_path(kind).exists()

    def mark_first_scan_done(self, kind: str = "marketplace") -> None:
        write_private(self._first_scan_path(kind), datetime.now(UTC).isoformat(timespec="seconds"))

    def reserve_monthly_run(self, limit: int = 60) -> bool:
        """Reserve one actor call before sending it; fail closed at the cap."""
        month = datetime.now(UTC).strftime("%Y-%m")
        usage: dict[str, object] = {"month": month, "runs": 0, "group_posts": 0}
        if self.usage_path.is_file():
            try:
                stored = json.loads(self.usage_path.read_text(encoding="utf-8"))
                if not isinstance(stored, dict):
                    return False
                usage.update(stored)
                if usage.get("month") == month:
                    count = int(usage.get("runs", 0))
                else:
                    count = 0
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                return False
        else:
            count = 0
        if count >= limit:
            return False
        usage.update({"month": month, "runs": count + 1})
        write_private(self.usage_path, json.dumps(usage))
        return True

    def reserve_monthly_group_posts(self, posts: int, limit: int = 300) -> bool:
        """Reserve a tiny, free-tier-safe number of public group posts locally."""
        return self._reserve_monthly_items("group_posts", posts, limit)

    def reserve_monthly_furnished_finder_listings(self, listings: int, limit: int = 300) -> bool:
        """Keep the optional community Furnished Finder connector within free-credit scale."""
        return self._reserve_monthly_items("furnished_finder_listings", listings, limit)

    def usage_summary(self) -> dict[str, object]:
        """Return bounded, non-secret local allowance facts for Ready Check."""
        month = datetime.now(UTC).strftime("%Y-%m")
        summary: dict[str, object] = {
            "month": month,
            "runs": 0,
            "group_posts": 0,
            "furnished_finder_listings": 0,
            "valid": True,
        }
        if not self.usage_path.is_file():
            return summary
        try:
            stored = json.loads(self.usage_path.read_text(encoding="utf-8"))
            if not isinstance(stored, dict):
                raise ValueError("usage record is not an object")
            if stored.get("month") != month:
                return summary
            for key in ("runs", "group_posts", "furnished_finder_listings"):
                summary[key] = max(0, int(stored.get(key, 0)))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            summary["valid"] = False
        return summary

    def _reserve_monthly_items(self, key: str, items: int, limit: int) -> bool:
        if items < 1 or limit < items:
            return False
        month = datetime.now(UTC).strftime("%Y-%m")
        usage: dict[str, object] = {
            "month": month,
            "runs": 0,
            "group_posts": 0,
            "furnished_finder_listings": 0,
        }
        if self.usage_path.is_file():
            try:
                stored = json.loads(self.usage_path.read_text(encoding="utf-8"))
                if not isinstance(stored, dict):
                    return False
                usage.update(stored)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                return False
        if usage.get("month") != month:
            usage = {
                "month": month,
                "runs": 0,
                "group_posts": 0,
                "furnished_finder_listings": 0,
            }
        try:
            used = int(usage.get(key, 0))
        except (TypeError, ValueError):
            return False
        if used + items > limit:
            return False
        usage[key] = used + items
        write_private(self.usage_path, json.dumps(usage))
        return True

    @staticmethod
    def _valid(value: str) -> bool:
        return bool(re.fullmatch(r"apify_api_[A-Za-z0-9_-]{20,}", value))
