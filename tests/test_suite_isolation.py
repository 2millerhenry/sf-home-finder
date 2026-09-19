"""The test suite never touches this repository's own ``data/`` folder.

``sf_housing/app.py`` builds the app the moment it is imported -- uvicorn is
pointed at ``sf_housing.app:app`` -- and with no ``SF_HOUSING_DATA_DIR`` that
app opens, migrates and can re-score ``data/housing.sqlite3`` beside the code,
writes a preferences file there and appends to its log. So every test run
that imported the app did all of that to the board the development servers
in this same folder were using, before a single test had started. Found by
watching ``data/`` appear during collection in a copy of the tree.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import sf_housing.app as app_module
from sf_housing.settings import PROJECT_ROOT


REPOSITORY_DATA = (PROJECT_ROOT / "data").resolve()


def _inside_the_repository_data(path: Path) -> bool:
    resolved = Path(path).resolve()
    return resolved == REPOSITORY_DATA or REPOSITORY_DATA in resolved.parents


def test_the_app_built_on_import_is_pointed_away_from_the_repository_s_data() -> None:
    settings = app_module.app.state.settings
    written = {
        "data folder": settings.data_dir,
        "database": settings.database_path,
        "preferences": settings.preferences_path,
        "log": settings.log_path,
        "open board": app_module.app.state.repository.path,
    }
    touched = {name: str(path) for name, path in written.items() if _inside_the_repository_data(path)}
    assert not touched, f"importing the app during the tests used the repository's own data: {touched}"


def test_programs_the_tests_start_are_pointed_away_from_it_too() -> None:
    """Subprocesses inherit the environment, and several tests start the app's
    own command line; they must land in the same throwaway place."""
    where = subprocess.run(
        [sys.executable, "-c", "from sf_housing.settings import Settings; print(Settings.from_environment().data_dir)"],
        capture_output=True, text=True, check=True, env=dict(os.environ),
    ).stdout.strip()
    assert not _inside_the_repository_data(Path(where)), f"a program started by the tests would use {where}"
