"""The report that says what an install did, rather than what it meant to do.

Somebody deciding whether to run an unsigned installer wants to know what it
will leave on their machine. Every answer a project gives to that is prose
its own author wrote, so this one is a diff taken by a GitHub runner either
side of the install and published beside the download.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import install_report  # noqa: E402


def file_at(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_a_line_added_to_a_file_that_already_existed_is_reported(tmp_path) -> None:
    """The surprise worth catching is not a new file.

    During this project's own development a test appended four lines to the
    author's ~/.zshrc and nothing noticed until he read the diff by hand. A
    shell profile keeps its name and its place; only its contents move, so
    only a hash sees it.
    """
    profile = file_at(tmp_path / ".zshrc", "export PATH=/usr/bin\n")
    before = {"paths": {str(profile): install_report._record(profile, read_bytes=True)}, "agents": []}
    profile.write_text("export PATH=/usr/bin\neval \"$(something)\"\n", encoding="utf-8")
    after = {"paths": {str(profile): install_report._record(profile, read_bytes=True)}, "agents": []}

    written = install_report.report(before, after, "0.0.1")

    assert "**1** existing file changed" in written
    assert ".zshrc" in written


def test_a_report_with_nothing_changed_says_so_plainly(tmp_path) -> None:
    """The good case has to read as the good case, not as an empty heading."""
    empty = {"paths": {}, "agents": []}

    written = install_report.report(empty, empty, "0.0.1")

    assert "altered nothing that was" in written
    assert "**0** files created" in written


def test_counts_of_one_are_not_written_as_plurals() -> None:
    """A report full of "1 files" reads like a bug in the thing it exists to
    make somebody trust."""
    before = {"paths": {}, "agents": []}
    after = {"paths": {"/tmp/x": {"size": 1, "mode": "0o644", "link": None, "sha": None}},
             "agents": ["com.sfhousing.monitor"]}

    written = install_report.report(before, after, "0.0.1")

    assert "**1** file created" in written and "1 files" not in written
    assert "**1** login item registered" in written


def test_a_login_item_the_install_registered_is_named() -> None:
    """The thing that makes it start on its own, and the thing somebody most
    wants told about."""
    before = {"paths": {}, "agents": ["com.apple.thing"]}
    after = {"paths": {}, "agents": ["com.apple.thing", "com.sfhousing.monitor"]}

    written = install_report.report(before, after, "0.0.1")

    assert "com.sfhousing.monitor" in written
    assert "com.apple.thing" not in written, "an agent that was already there is not news"


def test_another_program_s_files_are_counted_but_never_opened(tmp_path, monkeypatch) -> None:
    """A snapshot walks past every other app's private data.

    It has to notice that a file is there and whether its size moved. It has
    no business reading what is inside it, and a report generated on
    somebody's own Mac would otherwise hash their mail.
    """
    stranger = file_at(tmp_path / "Some Other App" / "secrets.db", "private")
    ours = file_at(tmp_path / "SF Home Finder" / "tools" / "open.sh", "#!/bin/bash")

    assert install_report._record(stranger)["sha"] is None, "it read a stranger's file"
    assert install_report._record(ours)["sha"], "it did not read its own file"


def test_the_walk_does_not_descend_into_another_program_s_folders(tmp_path) -> None:
    """Read whole, one developer's Application Support was 915,410 paths and
    a 170MB snapshot. Bounded, it is 246 paths -- and a new folder appearing
    anywhere is still seen, which is the part that matters."""
    file_at(tmp_path / "Some Other App" / "deep" / "deeper" / "buried.db", "x")
    file_at(tmp_path / "SF Home Finder" / "deep" / "deeper" / "ours.txt", "x")

    walked = {str(p) for p in install_report._walk(tmp_path)}

    assert str(tmp_path / "Some Other App") in walked, "a new folder must still be seen"
    assert str(tmp_path / "Some Other App" / "deep" / "deeper" / "buried.db") not in walked
    assert str(tmp_path / "SF Home Finder" / "deep" / "deeper" / "ours.txt") in walked


def test_the_report_says_how_to_undo_the_install() -> None:
    """Whatever else it says, it ends with the way out."""
    empty = {"paths": {}, "agents": []}

    assert "homefinder uninstall" in install_report.report(empty, empty, "0.0.1")


def test_the_app_is_recognised_by_the_name_its_folder_actually_has() -> None:
    """The product is SF Home Finder; the folder is "SF Housing Monitor".

    It was renamed years after the directory was, and a check that knew only
    the new name walked straight past the entire installation -- the report
    from a real runner said three files had been created.
    """
    from pathlib import Path as P

    assert install_report._ours(P("~/Library/Application Support/SF Housing Monitor/tools").expanduser())
    assert install_report._ours(P("/Users/x/.local/bin/homefinder"))
    assert not install_report._ours(P("/Users/x/Library/Application Support/Some Other App"))


def test_the_operating_system_s_own_agents_are_not_reported_as_ours() -> None:
    """Seventeen mdworkers came and went between two snapshots a second
    apart. Listing those buries the one line that matters."""
    before = {"paths": {}, "agents": []}
    after = {"paths": {}, "agents": ["com.sfhousing.monitor"]}

    written = install_report.report(before, after, "0.0.1")

    assert "**1** login item registered" in written
    assert "com.sfhousing.monitor" in written
