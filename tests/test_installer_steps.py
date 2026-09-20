"""Every second of the install belongs to a step that is counting.

The installer draws one line per step: a spinner while it runs, a tick and
the seconds it took when it is done. Work done between two steps is drawn by
neither, so the screen holds a finished tick and stops moving -- which is
what an installer that has hung looks like, at the moment somebody is
watching it hardest.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INSTALLER = ROOT / "release_assets" / "payload" / "install.sh"


def _function_span(text: str, name: str) -> tuple[int, int]:
    """Where ``name``'s body starts and ends, by matching its braces.

    An empty span when there is no such function, so a caller asking "is this
    line inside it" gets "no" rather than an error about the helper.
    """
    if f"{name}() {{" not in text:
        return -1, -1
    start = text.index(f"{name}() {{")
    depth, position = 0, start
    for position in range(start, len(text)):
        if text[position] == "{":
            depth += 1
        elif text[position] == "}":
            depth -= 1
            if depth == 0:
                return start, position
    raise AssertionError(f"{name} is never closed")


def test_starting_the_login_service_is_inside_the_step_that_times_it() -> None:
    """The install froze on a finished tick for seconds at a time.

    Stopping and starting the login service is a few seconds of real work,
    and it ran between "Getting it ready to start" and "Starting it up" --
    outside both. The owner read the pause as the installer having stopped.
    It now runs inside the step whose seconds are on screen.
    """
    installer = INSTALLER.read_text(encoding="utf-8")
    opens, closes = _function_span(installer, "start_it")

    assert "launchctl bootstrap" in installer[opens:closes]
    assert 'step "Starting it up" "$(( FIRST_ANSWER_SECONDS + 60 ))" start_it' in installer


def test_every_launchctl_call_runs_inside_a_step() -> None:
    """The guard, rather than the two cases that prompted it.

    Talking to launchd is the slow part of this installer that is not a
    download: stopping the running app waits for it to go, and starting the
    service waits for launchd. Both used to sit between steps. Anything added
    back into either gap would be drawn by no step and bring the pause with
    it, so the rule is the whole rule -- no launchctl outside a step.
    """
    installer = INSTALLER.read_text(encoding="utf-8")
    inside = [_function_span(installer, name) for name in ("stop_the_old_copy", "start_it")]

    found = 0
    for offset, _ in enumerate(installer):
        if not installer.startswith("/bin/launchctl", offset):
            continue
        found += 1
        assert any(opens <= offset <= closes for opens, closes in inside), (
            "a launchctl call runs outside any step, where nothing draws it: "
            f"{installer[offset:installer.index(chr(10), offset)]!r}"
        )
    assert found >= 5, "the installer stopped talking to launchd; this guard is measuring nothing"


def test_the_step_that_stops_the_old_copy_never_fails_the_install() -> None:
    """A copy that will not stop is the port check's problem, and it passed.

    ``step`` returns what its command returned, so wrapping the stop in one
    put a new way to fail in front of an install that used to shrug this off.
    """
    installer = INSTALLER.read_text(encoding="utf-8")
    assert 'step "Stopping the copy you have" 120 stop_the_old_copy || true' in installer


def test_the_service_refusing_still_stops_the_install() -> None:
    """Folding the start into a step must not swallow its failure.

    ``step`` runs its command in the background, so the ``fail`` that used to
    sit beside ``launchctl bootstrap`` would exit that subshell and let the
    install carry on. The refusal comes back as its own code instead, told
    apart from the wait simply running out -- which is the friendlier advice
    further down and is usually the right one.
    """
    installer = INSTALLER.read_text(encoding="utf-8")
    opens, closes = _function_span(installer, "start_it")

    assert 'return "$SERVICE_REFUSED"' in installer[opens:closes]
    assert "fail " not in installer[opens:closes], "fail in a backgrounded step exits only the subshell"
    assert '"$SERVICE_REFUSED") fail "macOS could not start the login service.' in installer
