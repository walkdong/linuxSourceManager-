"""Interaction seam between the CLI and whichever frontend is driving it.

The command handlers in :mod:`srcmgr.cli.commands` need to ask questions ("remove this
source?") and print results.  A one-shot CLI answers on stdin/stdout; the TUI answers
inside its own prompt and renders into a transcript; a future GUI would use a dialog.

Instead of teaching every handler about frontends, the *frontend* installs two hooks here
for the duration of a command:

* :data:`ask_hook` - ``(question, default, assume_yes) -> bool``
* :data:`output_hook` - an :class:`~srcmgr.cli.output.Output` replacement (the TUI hands
  the command a plain-text output object, one-shot CLI uses the terminal one)

Both are context variables, so nesting (a command run from inside the TUI that itself
runs a subcommand) behaves predictably and nothing leaks between commands.
"""

from __future__ import annotations

import contextlib
from contextvars import ContextVar
from typing import Any, Callable, Iterator

#: ``(question, default, assume_yes) -> bool``; ``None`` means "ask the real terminal".
ask_hook: ContextVar[Callable[[str, bool, bool], bool] | None] = ContextVar(
    "srcmgr_ask_hook", default=None
)

#: Frontend-provided output object; ``None`` means "the one-shot terminal output".
output_hook: ContextVar[Any | None] = ContextVar("srcmgr_output_hook", default=None)

#: Name of the frontend currently driving the engine ("cli", "tui", "gui").
frontend: ContextVar[str] = ContextVar("srcmgr_frontend", default="cli")


def current_frontend() -> str:
    return frontend.get()


@contextlib.contextmanager
def interactive(
    *,
    ask: Callable[[str, bool, bool], bool] | None = None,
    output: Any | None = None,
    name: str = "cli",
) -> Iterator[None]:
    """Install frontend hooks for the duration of a command."""
    tokens = []
    if ask is not None:
        tokens.append((ask_hook, ask_hook.set(ask)))
    if output is not None:
        tokens.append((output_hook, output_hook.set(output)))
    tokens.append((frontend, frontend.set(name)))
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


__all__ = [
    "ask_hook",
    "current_frontend",
    "frontend",
    "interactive",
    "output_hook",
]
