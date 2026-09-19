from __future__ import annotations

import argparse
import json
from dataclasses import asdict


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local SF Home Finder")
    subparsers = parser.add_subparsers(dest="command")
    serve = subparsers.add_parser("serve", help="Start the dashboard and scheduler")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", default=8000, type=int)
    subparsers.add_parser("scan", help="Run one scan now and exit")
    subparsers.add_parser(
        "prepare", help="Do the work a first start would do, then exit"
    )
    args = parser.parse_args()

    if args.command == "prepare":
        # Importing the app *is* the start-up: sf_housing.app builds its
        # application at import, which upgrades the board and re-ranks it when
        # a re-rank is owed. Nothing is served and no scan or scheduler runs,
        # because those begin with the server rather than the import.
        #
        # This exists for the installer to call. A first start after an install
        # always owes a re-rank -- the installer retracts the mark, so that a
        # downgrade and back cannot leave one version vouching for another's
        # scores -- and the port does not open until that finishes, on top of
        # macOS vetting a runtime it has never seen. Measured on a real
        # 9,615-home board: fifty seconds run plainly, seventy-three inside
        # the login service at the background priority launchd gives it, and
        # six minutes during an upgrade on a busy Mac. Run here it is the
        # installer's own visible step rather than silence from an app that
        # looks like it failed to start; the service then starts in three and
        # a half seconds.
        # Progress worth printing, rather than a bar that moves on a timer.
        # Almost all of this is the import: macOS vetting libraries it has not
        # seen and Python compiling them, neither of which reports anything.
        # What can be counted is how many modules have been asked for, and a
        # start-up asks for a stable number of them -- 870 when this was
        # written. Counting them is honest about where the time goes, and an
        # import that asks for more is simply held at 99 until it is done.
        import sys

        class _Counted:
            """A finder that only counts. It answers None, so the real ones
            still do the finding."""

            expected = 870

            def __init__(self) -> None:
                self.seen = 0
                self.said = -1

            def find_spec(self, name, path=None, target=None):
                self.seen += 1
                percent = min(99, self.seen * 100 // self.expected)
                if percent != self.said:
                    self.said = percent
                    print(f"PROGRESS {percent}", flush=True)
                return None

        sys.meta_path.insert(0, _Counted())
        from . import app as _started  # noqa: F401

        print("PROGRESS 100", flush=True)
        raise SystemExit(0)

    if args.command == "scan":
        from .apify import ApifyTokenStore
        from .database import Repository
        from .gmail_alerts import GmailAlertMailbox
        from .preferences import ensure_preferences, load_preferences
        from .scanner import Scanner
        from .scheduling import manual_scan_allowed
        from .settings import Settings
        from .sources import default_sources

        settings = Settings.from_environment()
        ensure_preferences(settings.preferences_path)
        repository = Repository(settings.database_path)
        repository.initialize()
        scanner = Scanner(
            repository,
            lambda: load_preferences(settings.preferences_path),
            default_sources(
                GmailAlertMailbox(
                    settings.gmail_client_secret_path or settings.data_dir / "gmail-client-secret.json",
                    settings.gmail_token_path or settings.data_dir / "gmail-token.json",
                    settings.gmail_pending_state_path or settings.data_dir / "gmail-oauth-state.json",
                ),
                ApifyTokenStore(settings.apify_token_path or settings.data_dir / "apify-token.txt"),
            ),
            timeout_seconds=settings.request_timeout_seconds,
            max_scan_seconds=settings.scan_max_seconds,
            deep_scan_max_seconds=settings.deep_scan_max_seconds,
            # A scan from the command line reads the same sites as one from the
            # button, so it counts as the same one check a day.
            scan_allowed=lambda trigger, sources: manual_scan_allowed(
                repository.recent_scans(40), trigger
            ),
        )
        outcome = scanner.run_scan("command_line")
        print(json.dumps(asdict(outcome), indent=2))
        raise SystemExit(0 if outcome.status in {"completed", "completed_with_errors"} else 1)

    import uvicorn

    uvicorn.run("sf_housing.app:app", host=getattr(args, "host", "127.0.0.1"), port=getattr(args, "port", 8000))


if __name__ == "__main__":
    main()
