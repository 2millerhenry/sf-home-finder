"""Making the homefinder command findable, in every shell that will look.

Two people installed this on their own Mac and typed homefinder; both were
told "command not found". The command is written to ~/.local/bin, which is not
on the PATH of a stock macOS shell, and a line appended to one profile is read
by some of the shells they will use and not others: a zsh script reads
.zprofile, an interactive zsh reads .zshrc, a login bash reads .bash_profile,
a non-login bash reads .bashrc, and fish reads neither.

So the line goes into every file the shell they actually use will read, once,
and the installer then checks whether a new shell can find the command rather
than telling them it can.
"""

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "release_assets" / "payload" / "tools" / "put-command-on-path.sh"


def run(home: Path, shell: str, cli_dir: Path) -> subprocess.CompletedProcess:
    environment = dict(os.environ, HOME=str(home), SHELL=shell)
    return subprocess.run(
        ["/bin/bash", str(TOOL), str(cli_dir)],
        capture_output=True, text=True, env=environment,
    )


def command_dir(home: Path) -> Path:
    directory = home / ".local" / "bin"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "homefinder").write_text("#!/bin/bash\necho hello\n")
    (directory / "homefinder").chmod(0o755)
    return directory


def test_it_is_valid_shell() -> None:
    assert subprocess.run(["/bin/bash", "-n", str(TOOL)], capture_output=True).returncode == 0


def test_zsh_is_given_both_the_files_it_reads(tmp_path: Path) -> None:
    """An interactive zsh reads .zshrc; a zsh script or ssh command reads
    .zprofile. Writing one of the two leaves the command missing from the
    other, which is the half of the problem nobody notices until a script
    fails."""
    home = tmp_path / "home"
    home.mkdir()
    run(home, "/bin/zsh", command_dir(home))

    for name in (".zshrc", ".zprofile"):
        assert ".local/bin" in (home / name).read_text(), f"{name} never learned the command"


def test_bash_is_given_both_the_files_it_reads(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    run(home, "/bin/bash", command_dir(home))

    for name in (".bash_profile", ".bashrc"):
        assert ".local/bin" in (home / name).read_text(), f"{name} never learned the command"


def test_fish_is_given_its_own_syntax(tmp_path: Path) -> None:
    """fish does not understand `export PATH=...`; a POSIX line in its config
    is a syntax error on every prompt, which is worse than the missing
    command."""
    home = tmp_path / "home"
    home.mkdir()
    run(home, "/usr/local/bin/fish", command_dir(home))

    config = home / ".config" / "fish" / "config.fish"
    assert config.exists(), "fish was left with nothing at all"
    text = config.read_text()
    assert "fish_add_path" in text and ".local/bin" in text
    assert "export PATH=" not in text, "POSIX syntax would break every fish prompt"


def test_running_it_again_changes_nothing(tmp_path: Path) -> None:
    """Installing twice is normal -- every upgrade runs this -- and a profile
    that grows a line each time is a profile somebody has to clean up."""
    home = tmp_path / "home"
    home.mkdir()
    directory = command_dir(home)
    run(home, "/bin/zsh", directory)
    once = (home / ".zshrc").read_text()
    run(home, "/bin/zsh", directory)

    assert (home / ".zshrc").read_text() == once, "the line was added twice"


def test_it_says_whether_a_new_shell_can_actually_find_the_command(tmp_path: Path) -> None:
    """The installer used to promise that a new window would find it. This
    checks instead of promising: it starts the shell the way the terminal will
    and asks it."""
    home = tmp_path / "home"
    home.mkdir()
    result = run(home, "/bin/zsh", command_dir(home))

    assert result.returncode == 0, result.stderr
    assert "homefinder" in result.stdout
    assert str(home / ".local" / "bin" / "homefinder") in result.stdout


def test_it_reports_a_different_homefinder_that_would_win(tmp_path: Path) -> None:
    """A leftover command from an isolated test install, or one somebody
    copied into /usr/local/bin, sits earlier on PATH and answers instead --
    quietly running a different app root than the one just installed."""
    home = tmp_path / "home"
    home.mkdir()
    directory = command_dir(home)
    intruder = tmp_path / "earlier"
    intruder.mkdir()
    (intruder / "homefinder").write_text("#!/bin/bash\necho other\n")
    (intruder / "homefinder").chmod(0o755)
    (home / ".zshrc").write_text(f'export PATH="{intruder}:$PATH"\n')

    result = run(home, "/bin/zsh", directory)

    assert str(intruder / "homefinder") in result.stdout + result.stderr, (
        "the command that would actually answer was never named"
    )


def test_the_installer_runs_it() -> None:
    """Writing the tool is not the same as anybody running it."""
    installer = (ROOT / "release_assets" / "payload" / "install.sh").read_text()
    assert "put-command-on-path.sh" in installer


def test_repair_fixes_a_missing_command_too() -> None:
    """Somebody whose terminal cannot find the command is exactly the person
    who will not fix it from a terminal. Repair is the door they have -- a
    double-click in the folder they downloaded -- and it re-runs the installer,
    which is where this work happens. So the fix reaches them without needing
    the command that is missing."""
    repair = (ROOT / "release_assets" / "payload" / "tools" / "repair.sh").read_text()
    assert '"$INSTALLER"' in repair, "Repair no longer re-runs the installer"


def test_the_tool_ships_in_the_release() -> None:
    """It lives beside the installer that calls it; a build that left it out
    would take the fix with it and say nothing."""
    builder = (ROOT / "scripts" / "build_release.py").read_text()
    assert '(assets / "payload" / "tools").glob("*.sh")' in builder
