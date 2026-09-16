"""The terminal command, and the one way it can be installed uselessly.

Somebody who installs with the one-line command never has the folder of
.command shortcuts the ZIP carries, so the only ways back in are the bookmark
and this. It goes to ~/.local/bin because that needs no administrator, which is
what keeps the whole install password-free -- but that directory is not on
everybody's PATH, and a command that cannot be found is worse than no command,
because nothing says so.
"""

from __future__ import annotations

import pathlib
import subprocess

import pytest


ROOT = pathlib.Path(__file__).resolve().parent.parent
CLI = ROOT / "release_assets" / "payload" / "tools" / "homefinder.sh"
INSTALLER = (ROOT / "release_assets" / "payload" / "install.sh").read_text()
UNINSTALLER = (ROOT / "release_assets" / "payload" / "tools" / "uninstall.sh").read_text()


def test_the_command_is_valid_shell() -> None:
    assert subprocess.run(["bash", "-n", str(CLI)], capture_output=True).returncode == 0


def test_it_is_installed_somewhere_that_needs_no_password() -> None:
    """The install advertises that it never asks for one. Writing the command to
    /usr/local/bin or anywhere else root-owned would quietly break that."""
    assert 'CLI_DIR="$HOME/.local/bin"' in INSTALLER
    assert "sudo" not in INSTALLER


def test_a_command_that_will_not_be_found_is_dealt_with() -> None:
    """Installing it onto a PATH that does not include it, and saying nothing,
    leaves somebody typing a command that does not exist and concluding the app
    is broken. That happened on the first machine other than this one."""
    assert 'case ":$PATH:" in' in INSTALLER, "PATH is not checked at all"
    # Either it is already reachable, or the installer makes it reachable.
    assert 'PROFILE=' in INSTALLER
    # ...and it only advertises the command when it will actually work today.
    assert '[ "${CLI_READY:-0}" = 1 ] && [ "${CLI_ON_PATH:-0}" = 1 ]' in INSTALLER


def test_the_command_can_be_used_in_the_window_that_installed_it() -> None:
    """A PATH line in a profile is read by the *next* shell, not this one, so
    "open a new Terminal window and it will work" is a step somebody has to
    remember minutes later. The first person to install this on their own Mac
    typed homefinder in the window they had just installed from, was told
    command not found, and stopped there. So the installer offers something
    that works in the window they are already looking at."""
    assert "Open a new Terminal window and it will work." not in INSTALLER, (
        "the only advice is still to open another window"
    )
    assert '"  In this window:     source $PROFILE"' in INSTALLER, (
        "no way to use the command in the window it was installed from"
    )
    assert '"$CLI_DIR/homefinder"' in INSTALLER, "the full path is never offered"


def test_uninstalling_takes_the_command_with_it() -> None:
    """It lives outside the app folder, so removing the folder alone would leave
    a command behind pointing at nothing."""
    assert 'CLI_PATH="$HOME/.local/bin/homefinder"' in UNINSTALLER
    assert '/bin/rm -f "$CLI_PATH"' in UNINSTALLER


def test_the_old_misspelling_is_cleaned_up() -> None:
    """0.4.3 shipped this as housefinder. The app has always been Home Finder,
    so an upgrade has to remove the stale command rather than leave two in the
    same directory, one of which is wrong."""
    assert '/bin/rm -f "$CLI_DIR/housefinder"' in INSTALLER
    assert 'CLI_PATH_OLD="$HOME/.local/bin/housefinder"' in UNINSTALLER


def test_every_advertised_command_is_handled() -> None:
    """The help is the contract. A verb listed there and missing from the case
    would fail with 'no such command' on something the app told you to type."""
    text = CLI.read_text()
    usage = text[text.index("homefinder -- your San Francisco"):text.index("USAGE")]
    advertised = {
        line.split()[1]
        for line in usage.splitlines()
        if line.strip().startswith("homefinder ") and len(line.split()) > 1
    }
    handled = set()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.endswith(")") and "|" in stripped or stripped.endswith(")"):
            head = stripped.rstrip(")").strip()
            if head and " " not in head and head not in {"fi", "esac", "*"}:
                handled.update(part.strip('"') for part in head.split("|"))
    missing = advertised - handled
    assert not missing, f"advertised but not handled: {sorted(missing)}"


def test_it_reads_json_with_the_app_s_own_python() -> None:
    """macOS ships no python3 of its own on a clean machine, and the app carries
    one. Reaching for a system interpreter would work on this laptop and fail on
    somebody else's."""
    text = CLI.read_text()
    assert 'PY="$APP_ROOT/current/bin/python"' in text
    assert "python3 -c" not in text


def test_checking_presents_the_local_origin() -> None:
    """The app refuses a POST that does not come from its own origin, which is
    what stops a page you happen to have open from driving your dashboard. The
    command is the local dashboard's origin, so it says so rather than having
    the guard relaxed for it -- found by running it and getting a 403.
    """
    text = CLI.read_text()

    assert '-H "Origin: http://127.0.0.1:$PORT"' in text


def test_a_refusal_is_read_rather_than_treated_as_a_failure() -> None:
    """curl -f exits before the reply can be looked at, and the reply is the
    point: "today's check is used up" and "finish your deal first" both arrive
    as redirects the app has already written for a person to read.
    """
    text = CLI.read_text()
    check = text[text.index("  check|scan)"):text.index("  logs)")]

    assert "-f" not in check.split("curl")[1].split("\n")[0], "-f would swallow the reason"
    assert "*message=*|*error=*)" in check, "only one of the two refusal shapes is read"


def test_the_help_names_the_port_it_is_actually_on() -> None:
    """It was a heredoc with quoted delimiter, so the address printed as
    127.0.0.1:8000 on an install that was not on 8000."""
    text = CLI.read_text()

    assert "cat <<USAGE" in text, "a quoted heredoc would print the variable name"
    usage = text[text.index("cat <<USAGE"):text.index("USAGE\n}")]
    assert "$URL" in usage, "the help does not name the address at all"
    assert "127.0.0.1:8000" not in usage, "the port is hard-coded rather than read"


def test_an_isolated_install_never_touches_the_real_account_s_command() -> None:
    """~/.local/bin is shared by every install on the machine, so a throwaway one
    writing there reaches into the installation somebody actually uses.

    Found by doing it: an isolated uninstall during testing deleted the real
    homefinder off this machine. The plist already had this rule and the
    comment explaining it -- "installing a second copy repointed the first
    one's login service at a temporary directory" -- and the command is the
    same shape of shared, account-level thing.
    """
    assert 'if [ "${SF_HOUSING_NO_LAUNCH_AGENT:-0}" = "1" ]; then\n  CLI_DIR="$APP_ROOT/bin"' in INSTALLER
    assert 'CLI_PATH="$APP_ROOT/bin/homefinder"' in UNINSTALLER
    # ...and the stale-name cleanup reaches into the same shared directory.
    assert '[ "${SF_HOUSING_NO_LAUNCH_AGENT:-0}" != "1" ] && [ -f "$CLI_DIR/housefinder" ]' in INSTALLER


def test_the_path_line_goes_into_the_shell_the_person_actually_uses() -> None:
    """The first person to install this on another Mac got "command not found".

    Their login shell was bash, and the installer told them to append to
    ~/.zshrc -- a file bash never opens. macOS has shipped zsh as the default
    for years, which is exactly what makes the assumption easy to miss and
    invisible to anybody testing on their own machine.
    """
    assert "*/bash) PROFILE=" in INSTALLER
    assert ".bash_profile" in INSTALLER
    assert '*)      PROFILE="$HOME/.zshrc"' in INSTALLER


def test_the_path_is_fixed_rather_than_described() -> None:
    """They did not run the line. Almost nobody runs the line. The step that is
    printed and skipped is the step that may as well not exist.
    """
    assert ">> \"$PROFILE\"" in INSTALLER, "the line is only printed, never written"
    # ...and never twice, on a re-run or an upgrade.
    assert "grep -qF '.local/bin'" in INSTALLER
    # ...and they are told the shell they are looking at will not see it. The
    # wording moved from "open a new Terminal window" to naming this window and
    # offering a way to use the command in it; what must not go is being told.
    assert "which this Terminal window" in INSTALLER
    assert "does not know about yet" in INSTALLER


def test_fish_is_told_rather_than_edited() -> None:
    """fish does not read a POSIX profile and its syntax is different, so
    appending that line would leave a broken config rather than a working
    command."""
    assert "*/fish) PROFILE=" in INSTALLER


# --- Behaviour, exercised by running the script rather than reading it --------

import os
import subprocess as sp
import tempfile


def run(args, root, port="9911", env=None, script=None):
    """Run the real command against a given app root."""
    # Belt as well as braces: the session fixture sets this too, but this is
    # the harness that can reach the installer, and an installer that writes
    # the real login service plist takes the author's own app down with it.
    environ = dict(
        os.environ,
        SF_HOUSING_APP_ROOT=str(root),
        SF_HOUSING_PORT=port,
        SF_HOUSING_NO_LAUNCH_AGENT="1",
    )
    environ.update(env or {})
    return sp.run(
        ["/bin/bash", str(script or CLI), *args], capture_output=True, text=True, env=environ
    )


@pytest.fixture
def missing(tmp_path):
    return tmp_path / "not-installed"


@pytest.fixture
def stub(tmp_path):
    """An app root that exists, with a stand-in for the tool this delegates to."""
    root = tmp_path / "app"
    (root / "tools").mkdir(parents=True)
    (root / "logs").mkdir(parents=True)
    opener = root / "tools" / "open.sh"
    opener.write_text("#!/bin/bash\necho opened \"$@\"\n")
    opener.chmod(0o755)
    (root / "logs" / "service.log").write_text("one\ntwo\nthree\n")
    return root


@pytest.mark.parametrize(
    "command", [[], ["status"], ["check"], ["logs"], ["restart"], ["repair"], ["uninstall"]]
)
def test_every_command_says_so_when_the_app_is_not_installed(command, missing) -> None:
    """The command lives outside the app, so it outlives it: a restored backup,
    an abandoned uninstall, a machine the folder was never on.

    It used to tell those people the app "may still be starting" and suggest
    waiting for something that was never coming.
    """
    result = run(command, missing)

    assert result.returncode != 0, f"{command or ['(bare)']} reported success with no app"
    assert "not installed" in result.stderr
    assert "install.sh | bash" in result.stderr, "no way forward is offered"


def test_failures_go_to_stderr(missing) -> None:
    """So somebody piping or logging this still sees them, and a script can
    tell an answer from a complaint."""
    result = run(["status"], missing)

    assert result.stdout.strip() == ""
    assert result.stderr.strip() != ""


def test_a_mistyped_line_count_is_answered_in_english(stub) -> None:
    """tail answers a typo with "illegal offset -- abc", which is a sentence
    about tail rather than about anything the person did."""
    result = run(["logs", "abc"], stub)

    assert result.returncode != 0
    assert "number of lines" in result.stderr
    assert "illegal offset" not in result.stderr + result.stdout


def test_the_line_count_is_honoured(stub) -> None:
    assert run(["logs", "2"], stub).stdout.splitlines() == ["two", "three"]


def test_opening_does_not_forward_the_verb(stub) -> None:
    """open.sh takes flags of its own. Handing it the word "open" works only
    because it happens to ignore anything that is not --no-browser."""
    assert run([], stub).stdout.strip() == "opened"
    assert run(["open"], stub).stdout.strip() == "opened"


def test_an_unknown_command_shows_the_help_and_fails(stub) -> None:
    result = run(["nonsense"], stub)

    assert result.returncode != 0
    assert "no 'nonsense' command" in result.stderr
    assert "homefinder status" in result.stderr, "the help is not offered"


def test_it_runs_under_the_bash_that_macos_ships() -> None:
    """macOS ships bash 3.2 as /bin/bash and that is what the shebang names.
    Anything written for bash 4 or 5 would work on the author's machine only if
    they had installed a newer one."""
    assert sp.run(["/bin/bash", "-n", str(CLI)], capture_output=True).returncode == 0


# --- Telling somebody a newer version exists ---------------------------------

import json
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class fake_app:
    """A stand-in for the running app, answering /health and nothing else.

    The command reads what the app already knows rather than asking GitHub
    itself, so what it does with that answer is the whole of its behaviour
    here and can be driven from a dictionary.
    """

    def __init__(self, payload: dict):
        self.port = free_port()
        body = json.dumps(payload).encode()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path != "/health":
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", self.port), Handler)

    def __enter__(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def stub_with_python(stub):
    """The stub app root, plus the Python the command reads JSON with.

    The command deliberately uses the interpreter the app carries rather than
    whatever the machine has, so a root without one can parse nothing -- which
    is its own test below.
    """
    interpreter = stub / "current" / "bin"
    interpreter.mkdir(parents=True)
    (interpreter / "python").symlink_to(sys.executable)
    return stub


def health_with(update):
    return {"ok": True, "app": "sf-home-finder", "version": "0.5.0",
            "scan_running": False, "update": update, "last_scan": None}


def test_a_newer_version_is_mentioned_once_after_the_app_opens(stub_with_python) -> None:
    """The whole point of the check: somebody who never visits the releases page
    still learns a fix exists. It comes after the app opens, not before, so it
    reads as a footnote rather than as something in the way."""
    with fake_app(health_with({"available": True, "latest": "v9.9.9"})) as app:
        result = run([], stub_with_python, port=str(app.port))

    assert result.returncode == 0
    assert "v9.9.9 is available" in result.stdout
    assert "homefinder update" in result.stdout
    assert result.stdout.index("opened") < result.stdout.index("v9.9.9"), (
        "the note came before the app opened"
    )
    assert result.stdout.count("is available") == 1


def test_nothing_is_said_when_there_is_nothing_to_say(stub_with_python) -> None:
    with fake_app(health_with({"available": False, "latest": "v0.5.0"})) as app:
        result = run([], stub_with_python, port=str(app.port))

    assert result.returncode == 0
    assert "available" not in result.stdout


def test_nothing_is_said_when_the_app_has_never_checked(stub_with_python) -> None:
    """A machine told not to check, or one that has never reached GitHub,
    reports null. That is an absence of news, not news."""
    with fake_app(health_with(None)) as app:
        result = run([], stub_with_python, port=str(app.port))

    assert result.returncode == 0
    assert "available" not in result.stdout


def test_an_app_that_is_not_answering_does_not_hold_up_opening(stub_with_python) -> None:
    """Nothing about a version is worth making somebody wait for. The port here
    has nobody on it, which is what a just-starting app looks like."""
    result = run([], stub_with_python, port=str(free_port()))

    assert result.returncode == 0
    assert "opened" in result.stdout
    assert "available" not in result.stdout


def test_opening_still_reports_its_own_failure(stub_with_python) -> None:
    """The note is an addition to opening, not a replacement for it.

    Two things have to survive no longer exec'ing the opener: its exit status,
    which is how the .command files and anything scripting this tell success
    from failure, and its silence about versions. Somebody whose app will not
    open is not helped by being told a newer one exists.
    """
    opener = stub_with_python / "tools" / "open.sh"
    opener.write_text("#!/bin/bash\necho 'could not open' >&2\nexit 3\n")
    opener.chmod(0o755)

    with fake_app(health_with({"available": True, "latest": "v9.9.9"})) as app:
        result = run([], stub_with_python, port=str(app.port))

    assert result.returncode == 3
    assert "could not open" in result.stderr
    assert "available" not in result.stdout, "nagged about a version while broken"


def test_updating_is_refused_while_a_check_is_running(stub_with_python) -> None:
    """Installing restarts the app. Doing that underneath a running check
    abandons it part way and spends the day's one manual check on nothing."""
    payload = health_with({"available": True, "latest": "v9.9.9"})
    payload["scan_running"] = True
    with fake_app(payload) as app:
        result = run(["update"], stub_with_python, port=str(app.port))

    assert result.returncode == 1
    assert "running right now" in result.stderr
    assert "Try again" in result.stderr


def test_updating_goes_through_the_one_published_installer() -> None:
    """There is one installer and this is it: it finds the newest release and
    checks every file against a checksum. A second download path here would be
    a second thing to keep correct, and the one nobody would test."""
    text = CLI.read_text()
    update_branch = text[text.index("  update)"):text.index("  repair)")]
    assert '/bin/bash -c "$INSTALL_LINE"' in update_branch
    assert "curl" not in update_branch, "a second way to download crept in"


def test_the_command_is_replaced_by_renaming_never_by_writing_over_it() -> None:
    """bash reads a script as it runs it. `homefinder update` is that script
    running, so copying the new one over the top would rewrite the file mid-read
    and carry on executing whatever bytes landed where it had got to. Renaming
    leaves the running command holding the file it started with.
    """
    assert '/bin/mv -f "$CLI_STAGED" "$CLI_PATH"' in INSTALLER
    assert '/bin/cp "$TOOLS_DIR/homefinder.sh" "$CLI_PATH"' not in INSTALLER, (
        "the command is copied straight onto itself again"
    )


def test_updating_confirms_the_new_version_is_the_one_answering() -> None:
    """Read rather than run: the branch this guards begins by downloading and
    installing a release, which a test suite must not do.

    What it guards is worth guarding. The installer restarts the service, and a
    restart that quietly did not happen -- launchctl refusing, or a copy whose
    login service was never registered -- leaves somebody running the exact
    version they just replaced, having been told it worked. It was seen: an
    isolated install reported success while the old process went on serving.
    So the closing word comes from the app rather than from the installer.
    """
    text = CLI.read_text()
    branch = text[text.index("  update)"):text.index("  repair)")]

    assert 'after="$(health | field version' in branch, "nothing checks what is running"
    # The three outcomes a person can actually be in.
    assert 'if [ -z "$after" ]' in branch, "not answering is not distinguished"
    assert '[ "$after" = "$before" ]' in branch, "an unchanged version is reported as new"
    assert "Now running" in branch


# --- Restarting the copy you are in, not the one somebody else is using ------


class fake_launchctl:
    """A stand-in for /bin/launchctl, and the copy of the command that reaches it.

    The command names /bin/launchctl by absolute path, which is right: PATH
    belongs to whoever is typing, and a login service is not the place to take
    what it offers. It also leaves a test nothing to intercept, because /bin is
    read-only on macOS. So what runs here is the command with that one absolute
    path relaxed to a bare name. The substitution is asserted, so a command that
    stopped calling launchctl that way is not quietly recorded as a pass.
    """

    def __init__(self, tmp_path):
        binaries = tmp_path / "fake-bin"
        binaries.mkdir()
        self.log = tmp_path / "launchctl-calls"
        stub = binaries / "launchctl"
        stub.write_text('#!/bin/bash\nprintf "%s\\n" "$*" >> "$LAUNCHCTL_LOG"\n')
        stub.chmod(0o755)

        source = CLI.read_text()
        assert "/bin/launchctl " in source, "the command no longer calls launchctl by that path"
        self.script = tmp_path / "homefinder"
        self.script.write_text(source.replace("/bin/launchctl ", "launchctl "))
        self.env = {
            "PATH": f"{binaries}:{os.environ['PATH']}",
            "LAUNCHCTL_LOG": str(self.log),
        }

    @property
    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []


@pytest.fixture
def launchctl(tmp_path):
    return fake_launchctl(tmp_path)


@pytest.fixture
def account_with_an_install(tmp_path):
    """A home directory that already has the real account's login service in it.

    Every test below runs against this rather than the author's own home, so
    nothing here can reach the plist the machine running the suite depends on --
    and the copy of the command under test cannot call /bin/launchctl at all.
    """
    home = tmp_path / "home"
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    (home / "Library" / "LaunchAgents" / "com.sfhousing.monitor.plist").write_text("")
    return home


# The three ways an install says it is not the account's own, written the way
# the environment writes them. "APP_AGENTS" and "HOME_AGENTS" stand in for
# directories only the test knows the path of.
@pytest.mark.parametrize(
    "isolation",
    [
        pytest.param({"SF_HOUSING_NO_LAUNCH_AGENT": "1"}, id="told-not-to"),
        pytest.param(
            {"SF_HOUSING_NO_LAUNCH_AGENT": "0", "SF_HOUSING_LAUNCH_AGENTS_DIR": "APP_AGENTS"},
            id="plist-kept-elsewhere",
        ),
        pytest.param(
            {"SF_HOUSING_NO_LAUNCH_AGENT": "1", "SF_HOUSING_LAUNCH_AGENTS_DIR": "HOME_AGENTS"},
            id="told-not-to-despite-the-directory",
        ),
    ],
)
def test_restarting_an_isolated_copy_leaves_the_shared_login_service_alone(
    isolation, stub, launchctl, account_with_an_install
) -> None:
    """launchd knows one com.sfhousing.monitor per account, and an isolated copy
    never registers it -- install.sh skips that step precisely so a throwaway
    install cannot reach the real account's. Kickstarting the label from here
    therefore restarts whichever installation did register it.

    Hit for real on 2026-09-15: running the isolated copy's own restart, on
    8011, bounced the live app on 8000 while it was serving. The plist is
    present here for the same reason it was present that day -- an isolated
    install writes one, it is only never loaded.

    The last case is the flag contradicting the directory. The flag wins, which
    is the rule uninstall.sh already follows before it goes near launchctl: it
    is the one signal that says outright that this copy's service was never
    loaded into the login session.
    """
    agents = {
        "APP_AGENTS": stub / "LaunchAgents",
        "HOME_AGENTS": account_with_an_install / "Library" / "LaunchAgents",
    }
    env = dict(launchctl.env, HOME=str(account_with_an_install), **isolation)
    configured = env.get("SF_HOUSING_LAUNCH_AGENTS_DIR")
    if configured:
        env["SF_HOUSING_LAUNCH_AGENTS_DIR"] = str(agents[configured])
    # Whatever directory this copy's plist belongs in, it is written there: the
    # bug is not a missing file, it is the wrong service being kicked.
    plist = agents.get(configured, stub / "LaunchAgents") / "com.sfhousing.monitor.plist"
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text("")

    result = run(["restart"], stub, script=launchctl.script, env=env)

    assert launchctl.calls == [], f"it reached the shared login service: {launchctl.calls}"
    assert result.returncode == 0, result.stderr
    assert "opened --no-browser" in result.stdout, "it stopped the copy without starting it again"


def test_restarting_an_isolated_copy_stops_the_process_that_was_serving(
    stub, launchctl, account_with_an_install
) -> None:
    """Starting a second one while the first still holds the port is not a
    restart. open.sh starts nothing while the port answers, so the old process
    would go on serving the old code and nothing would say otherwise."""
    victim = sp.Popen(["/bin/sleep", "30"])
    (stub / "service.pid").write_text(f"{victim.pid}\n")
    try:
        run(
            ["restart"],
            stub,
            script=launchctl.script,
            env=dict(launchctl.env, HOME=str(account_with_an_install)),
        )
        try:
            assert victim.wait(timeout=5) != 0
        except sp.TimeoutExpired:
            pytest.fail("the process this copy had running was left alone")
    finally:
        victim.kill()


def test_a_normal_install_is_still_restarted_through_its_login_service(
    stub, launchctl, account_with_an_install
) -> None:
    """The fix must not cost the ordinary Mac its restart: handing the service
    back to launchd is the only thing that restarts what launchd keeps alive.

    This is the one test in the file that runs with the flag off, so it is also
    the one that must not be able to reach a real login service. It cannot:
    HOME is a temporary directory, and the command it runs has no path to
    /bin/launchctl.
    """
    result = run(
        ["restart"],
        stub,
        script=launchctl.script,
        env=dict(
            launchctl.env,
            HOME=str(account_with_an_install),
            SF_HOUSING_NO_LAUNCH_AGENT="0",
        ),
    )

    assert result.returncode == 0, result.stderr
    assert launchctl.calls == [f"kickstart -k gui/{os.getuid()}/com.sfhousing.monitor"]


def test_a_normal_install_with_no_login_service_is_sent_to_repair(stub, launchctl) -> None:
    """Nothing to restart and nothing to kickstart. Saying so beats a silent
    success, which is what a bare `launchctl kickstart || true` would give."""
    result = run(
        ["restart"],
        stub,
        script=launchctl.script,
        env=dict(
            launchctl.env,
            HOME=str(stub / "empty-home"),
            SF_HOUSING_NO_LAUNCH_AGENT="0",
        ),
    )

    assert result.returncode != 0
    assert "homefinder repair" in result.stdout + result.stderr
    assert launchctl.calls == []


def test_updating_an_isolated_copy_restarts_it_rather_than_miscounting_it() -> None:
    """Read rather than run: the branch this guards downloads and installs a
    release, which a test suite must not do.

    The installer hands a normal install back to launchd, which starts it on the
    new files. An isolated copy has no launchd to be handed to, and the
    installer's own open.sh starts nothing while the old process is still
    answering -- so the files were upgraded underneath a process that went on
    serving the version it started with, and the version check below then
    reported "it was already the newest version" about an upgrade that had
    happened.
    """
    text = CLI.read_text()
    branch = text[text.index("  update)"):text.index("  repair)")]

    assert "restart_isolated" in branch, "an isolated copy is left serving the old code"
    assert branch.index('/bin/bash -c "$INSTALL_LINE"') < branch.index("restart_isolated"), (
        "it restarts before the new files are in place"
    )
    assert branch.index("restart_isolated") < branch.index('after="$(health | field version'), (
        "the version is read before the restart it is meant to confirm"
    )


# A stand-in for the running copy that does not let go of the port the instant
# it is asked to stop. Shutting down takes longer on slow machines and busy
# ones, which are the same machines where a restart that quietly did not happen
# is hardest to notice.
LINGERING_APP = """
import http.server, json, os, signal, sys, threading

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"ok": True, "app": "sf-home-finder"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass

signal.signal(signal.SIGTERM, lambda *_: threading.Timer(2.0, os._exit, [0]).start())
http.server.HTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
"""


def test_the_isolated_copy_is_started_again_only_once_the_old_one_has_let_go(
    stub, launchctl, account_with_an_install, tmp_path
) -> None:
    """open.sh starts nothing while the port still answers -- that is how it
    avoids two copies fighting over one port. So asking it to start again before
    the old process has finished going leaves the old code serving, having said
    "Restarting" and meaning it.
    """
    port = free_port()
    source = tmp_path / "lingering_app.py"
    source.write_text(LINGERING_APP)
    old = sp.Popen([sys.executable, str(source), str(port)])
    (stub / "service.pid").write_text(f"{old.pid}\n")

    witness = tmp_path / "port-when-started"
    opener = stub / "tools" / "open.sh"
    opener.write_text(
        "#!/bin/bash\n"
        'if /usr/bin/curl -fsS --max-time 2 "http://127.0.0.1:$SF_HOUSING_PORT/health" '
        ">/dev/null 2>&1; then\n"
        '  printf "still-serving\\n" > "$WITNESS"\n'
        "else\n"
        '  printf "port-free\\n" > "$WITNESS"\n'
        "fi\n"
        'echo opened "$@"\n'
    )
    opener.chmod(0o755)

    try:
        for _ in range(50):
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.1)
        else:
            pytest.fail("the stand-in app never came up")

        result = run(
            ["restart"],
            stub,
            port=str(port),
            script=launchctl.script,
            env=dict(
                launchctl.env,
                HOME=str(account_with_an_install),
                WITNESS=str(witness),
            ),
        )

        assert result.returncode == 0, result.stderr
        assert witness.read_text().strip() == "port-free", (
            "it was started again while the old copy still had the port"
        )
    finally:
        old.kill()
