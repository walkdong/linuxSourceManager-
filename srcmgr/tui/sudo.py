"""Privilege escalation for the beginner UI.

APT, Docker, system Flatpak and Snap need root; a user who has never seen Linux cannot be
expected to know about ``sudo``, so the UI asks for "the password you use to log in" once
and keeps the session for the rest of the run.

Security properties (deliberate, please keep them):

* The password is **never** passed on a command line and **never** written to disk as
  part of a command: it goes into a ``0600`` file that the temporary askpass helper reads
  through ``$SRCMGR_ASKPASS_FILE``, and both are deleted as soon as the check is over.
* The helper itself is a ``0700`` file in a private temp directory and is removed on
  :meth:`SudoSession.close` (or at process exit via ``atexit``).
* Elevation is **opt-in and per session**: :meth:`ensure` is only called when an action
  actually needs it, and ``sudo -n true`` is retried first so an existing session is
  reused without asking again.
* Nothing here runs ``sudo`` with ``shell=True``; every call is an argument list.
"""

from __future__ import annotations

import atexit
import os
import re
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

#: Message shown when sudo cannot ask for a password at all (no tty, no askpass).
_HELPER = """#!/bin/sh
# Temporary askpass helper written by Source Manager; deleted after use.
cat "$SRCMGR_ASKPASS_FILE"
"""


def sudo_available() -> bool:
    """True when a ``sudo`` binary exists on this machine."""
    return shutil.which("sudo") is not None


def is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


@dataclass
class SudoSession:
    """Keeps an elevated session alive for the duration of the UI."""

    timeout: float = 20.0
    #: set after a successful authentication
    authenticated: bool = False
    #: last failure message (for the UI)
    last_error: str = ""
    #: number of failed attempts (the UI shows a friendlier message after two)
    attempts: int = 0
    _dir: Path | None = field(default=None, repr=False)
    _askpass: Path | None = field(default=None, repr=False)
    _pwfile: Path | None = field(default=None, repr=False)

    # -- environment ---------------------------------------------------------
    def _prepare_helper(self) -> Path:
        if self._askpass is not None and self._askpass.exists():
            return self._askpass
        directory = Path(tempfile.mkdtemp(prefix="srcmgr-askpass-"))
        os.chmod(directory, stat.S_IRWXU)
        helper = directory / "askpass.sh"
        helper.write_text(_HELPER, encoding="utf-8")
        os.chmod(helper, stat.S_IRWXU)  # 0700
        self._dir = directory
        self._askpass = helper
        atexit.register(self.close)
        return helper

    def close(self) -> None:
        """Remove the helper and any password file it may have used."""
        for path in (self._pwfile, self._askpass):
            if path is not None:
                try:
                    path.unlink(missing_ok=True)
                except OSError:  # pragma: no cover - best effort
                    pass
        if self._dir is not None and self._dir.exists():
            shutil.rmtree(self._dir, ignore_errors=True)
        self._dir = None
        self._askpass = None

    # -- queries -------------------------------------------------------------
    def can_run(self, command: list[str] | None = None) -> bool:
        """True when we can already run privileged commands without a password."""
        if is_root():
            return True
        if not sudo_available():
            return False
        try:
            completed = subprocess.run(  # noqa: S603 - fixed argv
                ["sudo", "-n", *(command or ["true"])],
                capture_output=True,
                text=True,
                timeout=self.timeout,
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        if completed.returncode == 0:
            self.authenticated = True
        return completed.returncode == 0

    # -- authentication ------------------------------------------------------
    def ensure(self, password: str) -> bool:
        """Validate ``password`` and keep the session alive.  Returns success."""
        if is_root():
            self.authenticated = True
            return True
        if not sudo_available():
            self.last_error = "这台电脑上没有 sudo，无法获取管理员权限。"
            return False
        if not password:
            self.last_error = "密码是空的。"
            return False

        helper = self._prepare_helper()
        pwfile = Path(tempfile.mkstemp(prefix="srcmgr-pw-", dir=str(self._dir))[1])
        os.chmod(pwfile, stat.S_IRUSR | stat.S_IWUSR)  # 0600
        pwfile.write_text(password, encoding="utf-8")
        self._pwfile = pwfile

        env = os.environ.copy()
        env["SUDO_ASKPASS"] = str(helper)
        env["SRCMGR_ASKPASS_FILE"] = str(pwfile)
        # -A: use askpass; -p '': no prompt on stderr; -k would discard a cached session
        argv = ["sudo", "-A", "-p", "", "-v"]
        try:
            completed = subprocess.run(  # noqa: S603 - fixed argv
                argv,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                stdin=subprocess.DEVNULL,
                env=env,
            )
        except subprocess.TimeoutExpired:
            self.last_error = "验证密码超时。"
            self.attempts += 1
            self.close_password()
            return False
        except OSError as exc:  # pragma: no cover - sudo disappeared
            self.last_error = str(exc)
            self.attempts += 1
            self.close_password()
            return False
        finally:
            self.close_password()

        self.attempts += 1
        if completed.returncode == 0:
            self.authenticated = True
            self.last_error = ""
            return True
        output = _clean_sudo_output(completed.stderr or completed.stdout or "")
        # sudo says "Authentication failed, try again." / "Sorry, try again." for a wrong
        # password; show that as-is but tidied, since it is the most likely reason.
        self.last_error = output or "密码不正确，请再试一次。"
        return False

    def close_password(self) -> None:
        """Delete the transient password file (called immediately after every attempt)."""
        if self._pwfile is not None:
            try:
                self._pwfile.unlink(missing_ok=True)
            except OSError:  # pragma: no cover
                pass
            self._pwfile = None

    def keepalive(self) -> bool:
        """Refresh the sudo timestamp so the session does not expire mid-use."""
        if is_root():
            return True
        if not sudo_available():
            return False
        try:
            completed = subprocess.run(  # noqa: S603
                ["sudo", "-n", "-v"],
                capture_output=True,
                text=True,
                timeout=self.timeout,
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        if completed.returncode != 0:
            self.authenticated = False
        return completed.returncode == 0

    # -- labels --------------------------------------------------------------
    @property
    def status_text(self) -> str:
        """``root`` | ``ready`` | ``missing`` - what the header badge should say."""
        if is_root():
            return "root"
        return "ready" if (self.authenticated or self.can_run()) else "missing"

    @property
    def has_admin(self) -> bool:
        """True when a privileged action would succeed right now (no prompt needed)."""
        return is_root() or self.authenticated or self.can_run()


def _clean_sudo_output(text: str) -> str:
    """Strip ANSI noise and sudo's own prompt lines from an error message."""
    cleaned = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text or "")
    lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    # drop the bare "Password:" style prompts, keep the explanation
    lines = [line for line in lines if not line.lower().rstrip(": ").endswith("password")]
    return " ".join(lines).strip()


__all__ = ["SudoSession", "is_root", "sudo_available"]
