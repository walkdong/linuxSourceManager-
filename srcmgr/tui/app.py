"""Interactive terminal UI.

Design notes
------------
* The UI is a **frontend**, not a second implementation: every line the user types is
  turned into a CLI argument list and executed by :func:`srcmgr.cli.main._run_cli`.  So
  the TUI can never drift from the CLI, and anything the CLI can do the TUI can do
  (``apt add tuna``, ``health --watch 30``, ``plugins install ./x --trust``, …).
* Commands are the *same words* as the CLI (``apt list``), because muscle memory matters;
  ``:``-prefixed words are UI-only meta commands.
* Confirmation questions are routed into the TUI through
  :mod:`srcmgr.core.interaction`, so the prompt works even though output is captured.
* Nothing here is imported unless the TUI actually starts (``srcmgr/cli/main.py`` imports
  it lazily), so plain CLI runs keep working without prompt_toolkit installed for
  anything but the TUI path.
"""

from __future__ import annotations

import io
import os
import shlex
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style

from .. import APP_NAME, __version__
from ..cli.main import _run_cli
from ..cli.output import Output, resolve_color
from ..core import interaction
from ..core.config import Config
from ..core.errors import SourceManagerError, SourceNotFound
from ..core.paths import config_file, data_home, describe_path
from ..core.runtime import SourceManagerRuntime
from .completion import Session, SrcmgrCompleter

PROMPT = [("class:prompt", "srcmgr"), ("class:prompt-arrow", " ❯ ")]

STYLE = Style.from_dict(
    {
        "prompt": "bold ansicyan",
        "prompt-arrow": "ansibrightblack",
        "banner": "bold",
        "banner-dim": "ansibrightblack",
        "ok": "ansigreen",
        "warn": "ansiyellow",
        "err": "ansired",
        "meta": "ansibrightblack",
        "cmd": "bold ansimagenta",
    }
)

HELP_TEXT = """\
Commands (same words as the CLI - Tab completes, Up/Down recalls history)

  managers                     what exists on this machine and what it uses
  sources [--available]        every ecosystem's sources in one table
  <manager>                    e.g. apt, npm, docker, cargo ...
  <manager> list               installed sources of that ecosystem
  <manager> list --available   mirrors you could switch to (--china to filter)
  <manager> test [--all]       latency / HTTP / TLS of each source
  <manager> choose             measure mirrors and switch to the fastest
  <manager> add <name>         add a registry mirror (e.g. apt add tuna)
  <manager> enable|disable <name>
  <manager> remove <name>
  <manager> rollback           undo the last change (snapshot is automatic)
  health [--watch N]           health of every ecosystem, optionally refreshing
  fallback [--apply]           show / apply the opt-in failover plan
  history | backup --list      what changed, and the snapshots to go back to
  plugins | providers          the two levels of the plugin system
  doctor                       diagnose plugins, permissions, providers

UI commands

  :help            this text              :clear     clear the screen
  :refresh         reload plugins/sources :stats     session counters
  :quit / :q       leave (also Ctrl-D)    Ctrl-C     abandon the current line

Tip: append --json to any command for machine-readable output, or --dry-run to see what
     a change would do without writing anything.
"""

BANNER = """\
{banner}Source Manager {version}{banner_dim} - one place for every ecosystem's sources{banner_dim}
{meta}managing: {managers}
{meta}config:   {config}
{meta}state:    {state}
{meta}type {cmd}:help{meta} for commands, Tab to complete, {cmd}:q{meta} to leave{meta}"""


@dataclass
class Stats:
    """Small session counters shown by ``:stats``."""

    commands: int = 0
    failures: int = 0
    started: float = field(default_factory=time.monotonic)

    @property
    def uptime(self) -> str:
        seconds = int(time.monotonic() - self.started)
        minutes, seconds = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours}h{minutes:02d}m"
        if minutes:
            return f"{minutes}m{seconds:02d}s"
        return f"{seconds}s"


class Console:
    """Runs commands for the TUI and collects their output.

    ``confirm`` questions are answered through prompt_toolkit, ``--json``/``--porcelain``
    are neutralised (a transcript is human-facing) and every run is bounded so a
    long-running command can be interrupted with Ctrl-C.
    """

    def __init__(self, session: PromptSession, runtime: SourceManagerRuntime) -> None:
        self.prompt_session = session
        self.runtime = runtime
        self.stats = Stats()

    # -- running -------------------------------------------------------------
    def run_line(self, line: str) -> str:
        """Execute one command line; return the text to print (may be empty)."""
        argv = _split(line)
        if not argv:
            return ""
        buffer = self.capture(argv)
        return buffer

    def capture(self, argv: Sequence[str], *, color: bool = False) -> str:
        buffer = io.StringIO()
        out = Output(json_mode=False, porcelain=False, color=color, verbose=False, quiet=False)

        def ask(question: str, default: bool, assume_yes: bool) -> bool:
            return self._ask(question, default=default)

        code = 0
        with _redirect_stdout(buffer):
            with interaction.interactive(ask=ask, output=out, name="tui"):
                try:
                    code = _run_cli(list(argv), out=out)
                except KeyboardInterrupt:
                    print("^C interrupted")
                    code = 130
                except SystemExit as exc:  # argparse --help/--version inside the TUI
                    code = int(exc.code or 0)
                except SourceNotFound as exc:
                    print(f"error: {exc.message}")
                    code = 2
                except SourceManagerError as exc:
                    print(f"error: {exc.message}")
                    code = 2
                except Exception as exc:  # noqa: BLE001 - never kill the REPL
                    print(f"error: {exc!r}")
                    code = 1
        self.stats.commands += 1
        if code not in (0,):
            self.stats.failures += 1
        text = buffer.getvalue().rstrip("\n")
        return text

    def _ask(self, question: str, *, default: bool = False) -> bool:
        suffix = " [Y/n] " if default else " [y/N] "
        try:
            answer = self.prompt_session.prompt(
                HTML(f"<b>{question}</b>{suffix}"), style=STYLE
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        if not answer:
            return default
        return answer in {"y", "yes"}


class _redirect_stdout:
    """Context manager replacing ``sys.stdout`` (prompt_toolkit needs the real one)."""

    def __init__(self, buffer: io.StringIO) -> None:
        self.buffer = buffer
        self.saved: Any = None

    def __enter__(self):
        self.saved = sys.stdout
        sys.stdout = self.buffer
        return self.buffer

    def __exit__(self, *exc_info: object) -> None:
        sys.stdout = self.saved


def _split(line: str) -> list[str]:
    """Split a TUI line into argv, tolerating quotes and rejecting shell operators.

    The TUI is not a shell: it must never pipe, redirect or expand ``$VAR``.  Quotes are
    still honoured so paths with spaces work.
    """
    text = line.strip()
    if not text:
        return []
    if text.startswith(":"):
        return [text]
    try:
        argv = shlex.split(text, posix=True)
    except ValueError:
        argv = text.split()
    for token in argv:
        if token in {"|", "||", "&&", ";", ">", ">>", "<"}:
            raise SourceManagerError(
                f"'{token}' is not supported in the interactive UI",
                hint="run it in a real shell, or use the CLI: srcmgr --cli ...",
            )
    expanded = os.path.expanduser(text)
    return shlex.split(expanded, posix=True) if expanded != text else argv


class Tui:
    """The REPL itself, split from :func:`run` so tests can drive it directly."""

    def __init__(
        self,
        *,
        runtime: SourceManagerRuntime | None = None,
        config: Config | None = None,
        session: Any | None = None,
        history_path: Path | None = None,
        banner: bool = True,
        color: bool | None = None,
    ) -> None:
        self.config = config or Config.load()
        self.runtime = runtime or SourceManagerRuntime(config=self.config)
        self.banner_enabled = banner
        self.color = resolve_color(self.config.get("general.color", "auto")) if color is None else color
        self.session_state = Session()
        self._history_path = history_path
        self._session = session
        self.console: Console | None = None

    # -- setup ---------------------------------------------------------------
    def make_session(self) -> PromptSession:
        history = None
        try:
            path = self._history_path or (data_home() / "tui_history")
            path.parent.mkdir(parents=True, exist_ok=True)
            history = FileHistory(str(path))
        except OSError:  # pragma: no cover - read-only home
            history = None
        bindings = KeyBindings()

        @bindings.add("c-c")
        def _(event):  # noqa: ANN001 - abort the current line, keep the REPL alive
            event.app.current_buffer.reset()
            event.app.exit(exception=KeyboardInterrupt)

        @bindings.add("c-d")
        def _(event):  # noqa: ANN001 - exit on an empty line
            if not event.app.current_buffer.text:
                event.app.exit(exception=EOFError)
            else:
                event.app.current_buffer.delete()

        return PromptSession(
            history=history,
            completer=SrcmgrCompleter(self.session_state),
            auto_suggest=AutoSuggestFromHistory(),
            key_bindings=bindings,
            style=STYLE,
            complete_while_typing=True,
            enable_open_in_editor=True,
        )

    @property
    def session(self) -> PromptSession:
        if self._session is None:
            self._session = self.make_session()
        return self._session

    def refresh(self) -> None:
        self.session_state.refresh(self.runtime)

    def refresh_in_background(self) -> "threading.Thread":
        """Read the command surface without blocking the first prompt.

        Completion needs the ecosystem CLIs to be queried; on a machine with a slow or
        wedged tool (snapd is a known offender) that must not delay the prompt.
        """
        thread = threading.Thread(
            target=self.session_state.refresh, args=(self.runtime,), name="srcmgr-refresh",
            daemon=True,
        )
        thread.start()
        return thread

    # -- output helpers ------------------------------------------------------
    def print(self, text: str = "") -> None:
        if text:
            print(text, flush=True)
        else:
            print()

    def banner_text(self) -> str:
        """Render the banner from already-known state only.

        Detection (``status()``) shells out to ecosystem CLIs, so it happens on the
        background refresh; the banner shows every loaded manager meanwhile and switches
        to the detected list as soon as that lands.
        """
        if self.session_state.loaded:
            managers = self.session_state.detected or self.session_state.managers
        else:
            managers = self.runtime.manager_names()
        return BANNER.format(
            banner="",
            banner_dim="",
            meta="",
            cmd="",
            version=__version__,
            managers=", ".join(managers) or "none detected",
            config=describe_path(config_file()),
            state=describe_path(data_home()),
        )

    def show_banner(self) -> None:
        self.print(self.banner_text())
        self.print()

    # -- meta commands -------------------------------------------------------
    def handle_meta(self, line: str) -> bool:
        """Handle a ``:command``; return False when the REPL should stop."""
        parts = line[1:].split()
        name = (parts[0] if parts else "").lower()
        if name in {"q", "quit", "exit"}:
            self.print("bye")
            return False
        if name in {"h", "help", "?"}:
            self.print(HELP_TEXT)
            return True
        if name == "clear":
            print("\033[2J\033[H", end="", flush=True)
            self.show_banner()
            return True
        if name == "refresh":
            self.session_state.refresh(self.runtime)
            self.print(f"reloaded {len(self.session_state.managers)} manager(s)")
            return True
        if name == "stats":
            stats = self.console.stats if self.console else Stats()
            self.print(
                f"commands: {stats.commands}   failures: {stats.failures}   "
                f"uptime: {stats.uptime}   managers: {len(self.session_state.managers)}"
            )
            return True
        if name == "config":
            self.print(describe_path(config_file()))
            return True
        if name == "history":
            for record in self.runtime.history_records(limit=10):
                self.print(f"{record.when}  {record.action:8s} {record.manager:9s} {record.source}")
            return True
        if name in {"health", "status"}:
            self.print(self.console.capture(["managers"]) if self.console else "")
            return True
        self.print(f"unknown UI command ':{name}' - try :help")
        return True

    # -- main loop -----------------------------------------------------------
    def run_once(self, line: str) -> bool:
        """Process one input line; return False when the REPL should stop."""
        text = line.strip()
        if not text:
            return True
        if text.startswith(":"):
            return self.handle_meta(text)
        if text.split()[0] in {"quit", "exit"}:
            return self.handle_meta(":" + text.split()[0])
        if text.split()[0] in {"help", "?"}:
            return self.handle_meta(":help")
        if text.split()[0] == "clear":
            return self.handle_meta(":clear")
        if text.split()[0] == "refresh" and len(text.split()) == 1:
            return self.handle_meta(":refresh")
        try:
            argv = _split(text)
        except SourceManagerError as exc:
            self.print(f"error: {exc.message}")
            return True
        if not argv:
            return True
        if argv[0] in {"--tui", "--gui", "--cli"}:
            self.print(
                f"'{argv[0]}' is a start-up option, not a command inside the UI "
                "(you are already here)"
            )
            return True
        output = self.console.capture(argv) if self.console else ""
        if output:
            self.print(output)
        return True

    def loop(self, *, max_lines: int | None = None) -> int:
        self.console = Console(self.session, self.runtime)
        if self.banner_enabled:
            self.show_banner()
        # The first refresh shells out to every ecosystem CLI; do it off the main thread
        # so the prompt appears immediately (Tab completion fills in a moment later).
        self.refresh_in_background()
        lines = 0
        while max_lines is None or lines < max_lines:
            try:
                line = self.session.prompt(PROMPT, style=STYLE)
            except KeyboardInterrupt:
                self.print("^C  (type :q or press Ctrl-D to leave)")
                continue
            except EOFError:
                self.print("bye")
                return 0
            lines += 1
            if not self.run_once(line):
                return 0
        return 0


def run(argv: Sequence[str] = ()) -> int:
    """Entry point used by ``srcmgr`` / ``srcmgr --tui``."""
    if not sys.stdout.isatty() or not sys.stdin.isatty():
        from ..cli.output import Output, resolve_color

        out = Output(color=resolve_color("auto"))
        out.error(
            "the interactive UI needs a terminal",
            hint=(
                "use one-shot commands instead: srcmgr --cli managers"
                if not sys.stdin.isatty()
                else "re-run from an interactive shell"
            ),
        )
        return 2
    extra = [a for a in argv if a not in {"--tui"}]
    if extra and extra[0] in {"-h", "--help"}:
        print(
            f"{APP_NAME} {__version__} - interactive UI\n\n"
            "  srcmgr                 open the interactive UI (TUI)\n"
            "  srcmgr --tui           same, explicit\n"
            "  srcmgr --cli <command> run one command and exit\n"
            "  srcmgr --gui           graphical UI (planned)\n\n"
            "Inside the UI type :help for the command list."
        )
        return 0
    if extra:
        from ..cli.output import Output, resolve_color

        Output(color=resolve_color("auto")).error(
            f"unexpected argument(s) for the interactive UI: {' '.join(extra)}",
            hint="start it with 'srcmgr --tui', then type commands inside it",
        )
        return 2
    return Tui().loop()


__all__ = ["Console", "Stats", "Tui", "run"]
