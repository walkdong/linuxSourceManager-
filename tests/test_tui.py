"""Tests for the entry-point model and the typing shell (``srcmgr --shell``).

The M5 menu UI lives in ``tests/test_menu.py``; this module covers the frontend dispatch
(bare ``srcmgr`` = menu, ``--cli`` = one-shot, ``--shell`` = REPL, ``--gui`` = reserved)
and drives the REPL with a fake prompt session.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from srcmgr.cli.main import main
from srcmgr.core.config import Config
from srcmgr.core.runtime import SourceManagerRuntime
from srcmgr.tui.shell import Tui
from srcmgr.tui.completion import Session, completion_words

UBUNTU_SOURCES = """\
Types: deb
URIs: http://archive.ubuntu.com/ubuntu/
Suites: resolute
Components: main restricted universe multiverse
"""


class FakeSession:
    """Minimal stand-in for ``prompt_toolkit.PromptSession``."""

    def __init__(self, answers: list[str] | None = None) -> None:
        self.answers = list(answers or [])
        self.asked: list[str] = []

    def prompt(self, message=None, **kwargs) -> str:  # noqa: ANN001, ARG002
        if isinstance(message, str):
            self.asked.append(message)
        else:
            self.asked.append(str(message))
        if not self.answers:
            raise EOFError
        answer = self.answers.pop(0)
        if answer == "<EOF>":
            raise EOFError
        if answer == "<C-C>":
            raise KeyboardInterrupt
        return answer


@pytest.fixture
def apt_tree(isolated_env) -> Path:
    root = Path(isolated_env["SRCMGR_APT_ROOT"])
    dropins = root / "sources.list.d"
    dropins.mkdir(parents=True, exist_ok=True)
    (dropins / "ubuntu.sources").write_text(UBUNTU_SOURCES, encoding="utf-8")
    (root / "sources.list").write_text("# empty\n", encoding="utf-8")
    return root


def make_tui(session: FakeSession | None = None, **kwargs) -> Tui:
    tui = Tui(session=session or FakeSession(), banner=False, color=False, **kwargs)
    tui.refresh()
    from srcmgr.tui.shell import Console

    tui.console = Console(tui.session, tui.runtime)
    return tui


# ---------------------------------------------------------------------------
# entry model
# ---------------------------------------------------------------------------
def test_bare_invocation_without_a_tty_explains_itself(capsys, monkeypatch):
    """`srcmgr` in a pipe must not start an interactive UI; it must point at --cli."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    code = main([])
    captured = capsys.readouterr()
    assert code == 2
    assert "--cli" in captured.err


def test_old_style_invocation_is_refused_with_the_right_hint(capsys):
    """`srcmgr apt list` (no --cli) is no longer valid: the UI owns the bare name."""
    code = main(["apt", "list"])
    captured = capsys.readouterr()
    assert code == 2
    assert "missing --cli" in captured.err
    assert "srcmgr --cli apt list" in captured.err


def test_misplaced_cli_flag_is_explained(capsys):
    code = main(["apt", "list", "--cli"])
    captured = capsys.readouterr()
    assert code == 2
    assert "before the command" in captured.err


def test_gui_is_reserved_and_reports_clearly(capsys):
    code = main(["--gui"])
    captured = capsys.readouterr()
    assert code == 3
    assert "graphical UI is not implemented" in captured.err


def test_cli_frontend_still_works(capsys):
    assert main(["--cli", "managers"]) == 0
    assert "apt" in capsys.readouterr().out
    assert main(["--cli", "version"]) == 0


def test_tui_help_flag_documents_every_frontend(capsys, monkeypatch):
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    assert main(["--tui", "--help"]) == 0
    out = capsys.readouterr().out
    for flag in ("--cli", "--shell", "--gui", "--tui"):
        assert flag in out, flag


def test_tui_rejects_stray_arguments(capsys, monkeypatch):
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    code = main(["--tui", "apt", "list"])
    assert code == 2
    assert "不接受参数" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# the REPL
# ---------------------------------------------------------------------------
def test_repl_runs_cli_commands(apt_tree, capsys):
    tui = make_tui(FakeSession(["managers", "apt list", ":q"]))
    assert tui.loop() == 0
    output = capsys.readouterr().out
    assert "apt" in output
    assert "archive.ubuntu.com" in output
    assert "bye" in output


def test_repl_meta_commands(capsys):
    tui = make_tui(FakeSession([":help", ":stats", ":refresh", ":config", ":q"]))
    assert tui.loop() == 0
    output = capsys.readouterr().out
    assert "Commands" in output
    assert "commands:" in output
    assert "reloaded" in output
    assert "config.yaml" in output


def test_repl_unknown_meta_command_is_harmless(capsys):
    tui = make_tui()
    assert tui.run_once(":nonsense") is True
    assert "unknown UI command" in capsys.readouterr().out


def test_repl_remaps_startup_options(capsys):
    tui = make_tui()
    assert tui.run_once("--cli managers") is True
    assert "start-up option" in capsys.readouterr().out


def test_repl_rejects_shell_syntax(capsys):
    tui = make_tui()
    assert tui.run_once("apt list | grep tuna") is True
    assert "not supported in the interactive UI" in capsys.readouterr().out


def test_repl_empty_line_is_ignored(capsys):
    tui = make_tui()
    assert tui.run_once("   ") is True
    assert capsys.readouterr().out == ""


def test_repl_survives_ctrl_c_and_eof(capsys):
    tui = make_tui(FakeSession(["<C-C>", "managers", "<EOF>"]))
    assert tui.loop() == 0
    output = capsys.readouterr().out
    assert "^C" in output
    assert "bye" in output


def test_repl_reports_failures_without_dying(apt_tree, capsys):
    """A declined destructive command must not kill the REPL, and the next command runs."""
    tui = make_tui(FakeSession(["apt remove tuna", "n", "managers", ":q"]))
    assert tui.loop() == 0
    output = capsys.readouterr().out
    assert "aborted" in output
    assert "apt" in output, "the REPL should still answer the next command"
    assert "bye" in output
    dropins = apt_tree / "sources.list.d"
    assert "ubuntu.sources" in {p.name for p in dropins.iterdir()}


def test_repl_honours_yes_for_destructive_commands(apt_tree, capsys, monkeypatch):
    """Inside the TUI a confirmation is asked through the TUI prompt, not stdin."""
    import os

    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    session = FakeSession(["y", ":q"])
    tui = make_tui(session)
    tui.run_once("apt disable tuna")
    assert session.asked, "the TUI should have asked for confirmation"
    assert tui.console.stats.commands == 1


def test_repl_declines_confirmation(apt_tree, capsys, monkeypatch):
    import os

    monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
    session = FakeSession(["n"])
    tui = make_tui(session)
    output = tui.console.capture(["apt", "disable", "tuna"])
    assert "aborted" in output
    # nothing was parked
    dropins = Path(os.environ["SRCMGR_APT_ROOT"]) / "sources.list.d"
    assert (dropins / "tuna.list").exists() is False  # fixture has no tuna entry
    assert "ubuntu.sources" in {p.name for p in dropins.iterdir()}


def test_loop_stops_at_max_lines(capsys):
    tui = make_tui(FakeSession(["managers", "managers", "managers"]))
    assert tui.loop(max_lines=2) == 0


# ---------------------------------------------------------------------------
# completion
# ---------------------------------------------------------------------------
def test_completion_offers_commands_and_managers(apt_tree):
    session = Session()
    session.refresh(SourceManagerRuntime(config=Config.defaults()))
    commands = completion_words(session, "")
    assert "managers" in commands and "apt" in commands and "doctor" in commands
    actions = completion_words(session, "apt ")
    assert {"list", "add", "enable", "test", "choose"} <= set(actions)
    installed = completion_words(session, "apt enable ")
    assert installed, "installed sources should complete"
    registry = completion_words(session, "apt add ")
    assert registry, "registry sources should complete"


def test_completion_meta_commands(apt_tree):
    session = Session()
    session.refresh(SourceManagerRuntime(config=Config.defaults()))
    metas = completion_words(session, ":")
    assert ":help" in metas and ":quit" in metas


def test_completion_works_before_the_runtime_is_loaded(apt_tree):
    """Static surface (commands, manager actions) completes even without a refresh."""
    session = Session()  # never refreshed
    assert "managers" in completion_words(session, "")
    assert "list" in completion_words(session, "apt ")
    assert completion_words(session, "apt enable ") == []  # no sources known yet
    assert completion_words(session, "apt en") == ["enable"]


def test_completer_yields_prompt_toolkit_completions(apt_tree):
    from prompt_toolkit.document import Document

    from srcmgr.tui.completion import SrcmgrCompleter

    session = Session()
    session.refresh(SourceManagerRuntime(config=Config.defaults()))
    completer = SrcmgrCompleter(session)
    words = [c.text for c in completer.get_completions(Document("ap", 2), None)]
    assert "apt" in words
    assert all(word.startswith("ap") for word in words)
    partial = [c.text for c in completer.get_completions(Document("apt en", 6), None)]
    assert partial == [] or all(word.startswith("en") for word in partial)


# ---------------------------------------------------------------------------
# robustness: no plugin may freeze the UI
# ---------------------------------------------------------------------------
def test_plugin_subprocess_timeout_is_capped_and_stdin_detached(monkeypatch):
    """A wedged vendor CLI (snapd blocks without a terminal) must not hang the UI."""
    import subprocess

    from srcmgr.core import helpers

    captured: dict[str, object] = {}

    def fake_run(command, **kwargs):
        captured.update(kwargs)
        captured["command"] = command
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(helpers.subprocess, "run", fake_run)
    monkeypatch.setenv("SRCMGR_PLUGIN_TIMEOUT", "2.5")
    helpers.run(["snap", "get", "system", "proxy"], timeout=30.0)
    assert captured["timeout"] == 2.5, "a plugin must not be able to exceed the cap"
    assert captured["stdin"] is subprocess.DEVNULL, "stdin must be detached"


def test_subprocess_cap_defaults_and_ignores_bad_values(monkeypatch):
    from srcmgr.core import helpers

    monkeypatch.delenv("SRCMGR_PLUGIN_TIMEOUT", raising=False)
    assert helpers.subprocess_cap() == helpers.DEFAULT_SUBPROCESS_CAP
    monkeypatch.setenv("SRCMGR_PLUGIN_TIMEOUT", "not-a-number")
    assert helpers.subprocess_cap() == helpers.DEFAULT_SUBPROCESS_CAP
    monkeypatch.setenv("SRCMGR_PLUGIN_TIMEOUT", "-4")
    assert helpers.subprocess_cap() == helpers.DEFAULT_SUBPROCESS_CAP


def test_startup_refresh_runs_off_the_main_thread(apt_tree):
    """The banner and prompt must appear without waiting for the ecosystem CLIs."""
    import threading

    tui = Tui(session=FakeSession(), banner=False, color=False)
    thread = tui.refresh_in_background()
    assert isinstance(thread, threading.Thread)
    thread.join(timeout=30)
    assert not thread.is_alive()
    assert tui.session_state.loaded
    assert "apt" in tui.session_state.managers


def test_banner_never_shells_out_before_refresh(apt_tree, monkeypatch, capsys):
    """banner_text() must be pure formatting: detection happens on the worker thread."""
    tui = Tui(session=FakeSession(), banner=False, color=False)
    calls: list[str] = []
    monkeypatch.setattr(tui.runtime, "status", lambda name: calls.append(name))
    text = tui.banner_text()
    assert calls == [], "the banner must not trigger manager detection"
    assert "apt" in text
