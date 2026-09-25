"""Interactive frontends.

* :mod:`srcmgr.tui.app` - the full-screen menu UI (bare ``srcmgr`` / ``--tui``), aimed at
  users who have never opened a terminal.
* :mod:`srcmgr.tui.shell` - the line-based command REPL (``srcmgr --shell``).

**Do not import either module at package import time**: ``app`` pulls in Textual and
``shell`` pulls in prompt_toolkit, and neither should be needed for ``srcmgr --cli``.
Use :func:`run_menu` / :func:`run_shell` instead.
"""

from __future__ import annotations


def run_menu(argv=(), **kwargs):
    """Start the full-screen menu UI (imports Textual lazily)."""
    from .app import run

    return run(argv, **kwargs)


def run_shell(argv=()):
    """Start the line-based REPL (imports prompt_toolkit lazily)."""
    from .shell import run

    return run(argv)


__all__ = ["run_menu", "run_shell"]
