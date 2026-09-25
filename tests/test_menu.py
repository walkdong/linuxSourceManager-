"""Tests for the M5 menu UI (``srcmgr`` with no arguments).

Two layers are covered:

* the **data layer** (``srcmgr.tui.data``) that every widget calls - including the hard
  rule that a locked ecosystem cannot be switched, not even by the one-click action;
* the **Textual application** driven through ``App.run_test()``, which is how a user
  presses keys and clicks buttons.

Nothing here touches the real machine: the ``isolated_env`` fixture redirects every root,
and ``SRCMGR_OFFLINE`` keeps the probes offline (they fail fast and deterministically).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from srcmgr.core.api import Source
from srcmgr.core.config import Config
from srcmgr.core.errors import LockedError
from srcmgr.core.runtime import SourceManagerRuntime
from srcmgr.tui import data as D
from srcmgr.tui.app import (
    ConfirmDialog,
    EcosystemScreen,
    HomeScreen,
    NoticeDialog,
    SourceManagerApp,
    mark_onboarding_seen,
)
from srcmgr.tui.i18n import friendly_error, tr
from srcmgr.tui.sudo import SudoSession

UBUNTU_SOURCES = """\
Types: deb
URIs: http://archive.ubuntu.com/ubuntu/
Suites: resolute
Components: main restricted universe multiverse
"""


class FakeSudo(SudoSession):
    """Sudo session that "succeeds" without asking the system."""

    def __init__(self, *, accept: bool = True) -> None:
        super().__init__()
        self.accept = accept
        self.asked: list[str] = []

    def can_run(self, command=None) -> bool:  # noqa: ANN001
        return False

    def ensure(self, password: str) -> bool:
        self.asked.append(password)
        self.attempts += 1
        if self.accept and password:
            self.authenticated = True
            return True
        self.last_error = "sudo: Authentication failed, try again."
        return False

    @property
    def has_admin(self) -> bool:
        return self.authenticated

    def close(self) -> None:
        pass


@pytest.fixture
def apt_tree(isolated_env) -> Path:
    root = Path(isolated_env["SRCMGR_APT_ROOT"])
    dropins = root / "sources.list.d"
    dropins.mkdir(parents=True, exist_ok=True)
    (dropins / "ubuntu.sources").write_text(UBUNTU_SOURCES, encoding="utf-8")
    (root / "sources.list").write_text("# empty\n", encoding="utf-8")
    return root


def make_runtime() -> SourceManagerRuntime:
    return SourceManagerRuntime(config=Config.defaults())


def make_app(**kwargs) -> SourceManagerApp:
    runtime = kwargs.pop("runtime", None) or make_runtime()
    return SourceManagerApp(
        runtime=runtime,
        config=runtime.config,
        sudo=kwargs.pop("sudo", FakeSudo()),
        show_onboarding=kwargs.pop("show_onboarding", False),
        **kwargs,
    )


def run(coro):
    """Run a coroutine in a private event loop (Textual's tests are async)."""
    return asyncio.run(coro)


async def wait_for(predicate, pilot, *, timeout: float = 15.0, what: str = "condition"):
    """Poll until ``predicate()`` is truthy.

    The home screen fills its table from a worker thread, so a fixed ``pause()`` is a
    race; this waits for the state the test actually cares about.
    """
    import time as _time

    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        if predicate():
            return True
        await pilot.pause(0.1)
    raise AssertionError(f"timed out waiting for {what}")


# ---------------------------------------------------------------------------
# data layer: the lock is absolute
# ---------------------------------------------------------------------------
def test_load_ecosystems_describes_every_manager(isolated_env, apt_tree):
    runtime = make_runtime()
    rows = D.load_ecosystems(runtime)
    assert rows, "at least apt should be listed"
    by_name = {row.manager: row for row in rows}
    assert "apt" in by_name
    apt = by_name["apt"]
    assert apt.available is True
    assert apt.current, "the current source should be shown"
    assert apt.needs_admin is True, "APT writes to /etc and needs admin rights"
    assert by_name.get("npm") is not None
    assert by_name["npm"].needs_admin is False
    # the lock cell is rendered from the theme glyphs (see tests/test_design.py)
    from srcmgr.tui.app import _lock_cell

    assert "锁定" in _lock_cell(apt)
    apt.locked = True
    assert _lock_cell(apt).startswith("▣")


def test_switch_refuses_a_locked_ecosystem(isolated_env, apt_tree):
    runtime = make_runtime()
    runtime.lock("apt")
    source = Source(manager="apt", name="X", url="https://x.example/ubuntu/", suite="resolute")
    with pytest.raises(LockedError):
        D.switch(runtime, "apt", source)


def test_switch_works_when_unlocked(isolated_env, apt_tree):
    runtime = make_runtime()
    source = D.custom_source(runtime, "npm", "https://registry.npmmirror.com")
    outcome = D.switch(runtime, "npm", source)
    assert outcome.ok, outcome.message
    assert outcome.data.get("backup"), "a switch must be undoable"


def test_plan_fastest_never_touches_a_locked_ecosystem(isolated_env, apt_tree, monkeypatch):
    """The one-click action must skip locks *before* probing anything."""
    runtime = make_runtime()
    runtime.lock("apt")

    probed: list[str] = []
    real_measure = D.measure_mirrors

    def spy(runtime_, manager, **kwargs):  # noqa: ANN001
        probed.append(manager)
        return real_measure(runtime_, manager, **kwargs)

    monkeypatch.setattr(D, "measure_mirrors", spy)
    plan = D.plan_fastest(runtime, ["apt", "npm"])
    assert "apt" not in probed, "a locked ecosystem must not even be measured"
    assert any("APT" in name for name in plan.skipped_locked)
    assert all(manager != "apt" for manager, *_ in plan.changes)


def test_apply_fastest_refuses_to_change_a_locked_ecosystem(isolated_env, apt_tree):
    runtime = make_runtime()
    runtime.lock("apt")
    plan = D.SwitchPlan(changes=[("apt", "Somewhere", "https://x.example/ubuntu/", 10.0)])
    outcome = D.apply_fastest(runtime, plan)
    assert outcome.data["switched"] == [], "nothing may actually be switched"
    assert any("APT" in name for name in outcome.data["skipped"])
    assert runtime.current("apt").url != "https://x.example/ubuntu/"
    assert runtime.is_locked("apt"), "the bulk action must not unlock anything"


def test_scan_reports_per_ecosystem(isolated_env, apt_tree):
    runtime = make_runtime()
    report = D.scan_all(runtime)
    assert report["checked"] >= 0
    assert isinstance(report["managers"], dict)


def test_mirror_shortlist_is_ranked_before_probing(isolated_env, apt_tree):
    """Only a useful dozen mirrors are measured (probing 65 would take a minute)."""
    runtime = make_runtime()
    measured = D.measure_mirrors(runtime, "apt")
    assert len(measured) <= D.MAX_VISIBLE_MIRRORS
    everything = D.load_mirrors(runtime, "apt")
    assert len(everything) >= len(measured)
    # mirrors rank above the official archives, which rank above vendor entries
    kinds = [D._kind_value(m.source) for m in measured]
    assert "vendor" not in kinds[:3], kinds


def test_current_mirror_is_marked_even_with_a_country_alias(isolated_env, apt_tree):
    runtime = make_runtime()
    mirrors = D.load_mirrors(runtime, "apt")
    assert any(m.is_current for m in mirrors), "the mirror in use must be visible"


def test_mirrors_of_one_ecosystem_can_be_measured_offline(isolated_env, apt_tree):
    runtime = make_runtime()
    mirrors = D.measure_mirrors(runtime, "npm")
    assert mirrors, "npm has registry mirrors"
    assert all(m.ok is False for m in mirrors), "SRCMGR_OFFLINE marks probes as failures"
    assert all(m.speed_text for m in mirrors)


def test_friendly_error_translates_technical_failures():
    what, todo = friendly_error("sudo: a password is required")
    assert "管理员" in what and "密码" in todo
    what, todo = friendly_error("Connection refused")
    assert "打不开" in what and "镜像" in todo
    what, todo = friendly_error("npm 已锁定，不能修改")
    assert "锁定" in what
    what, todo = friendly_error("something nobody has seen before")
    assert what and todo


def test_strings_are_chinese_by_default():
    """Plain Chinese words, no icon prefixes (the design refresh removed them)."""
    assert tr("action.scan") == "测速"
    assert tr("lock.lock") == "锁定"
    assert tr("action.fastest") == "换到最快"


# ---------------------------------------------------------------------------
# the application
# ---------------------------------------------------------------------------
def test_home_screen_shows_rows_lock_column_and_actions(isolated_env, apt_tree):
    async def scenario() -> None:
        app = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            home = app.screen
            assert isinstance(home, HomeScreen)
            table = home.query_one("#ecosystems")
            await wait_for(lambda: table.row_count >= 10, pilot, what="the ecosystem rows")
            # every action button exists and is labelled in Chinese
            for button_id in ("scan", "fastest", "undo", "backups", "history", "admin", "quit"):
                assert home.query_one(f"#{button_id}")
            assert "锁定" in tr("home.col_lock")

    run(scenario())


def test_locked_row_is_visually_marked_and_toggles(isolated_env, apt_tree):
    async def scenario() -> None:
        app = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            home = app.screen
            assert isinstance(home, HomeScreen)
            table = home.query_one("#ecosystems")
            await wait_for(lambda: table.row_count >= 1, pilot, what="the ecosystem rows")
            table.move_cursor(row=0)
            eco = home.selected
            assert eco is not None
            assert eco.locked is False
            await pilot.press("l")
            await pilot.pause(0.3)
            assert app.runtime.is_locked(eco.manager), "pressing l must lock"
            assert eco.locked is True
            await pilot.press("l")
            await pilot.pause(0.3)
            assert not app.runtime.is_locked(eco.manager)

    run(scenario())


def test_onboarding_is_shown_once(isolated_env):
    async def scenario() -> None:
        runtime = make_runtime()
        app = SourceManagerApp(runtime=runtime, config=runtime.config, sudo=FakeSudo())
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.4)
            assert isinstance(app.screen, NoticeDialog), "first run explains itself"
            await pilot.press("escape")
            await pilot.pause(0.2)
            assert not isinstance(app.screen, NoticeDialog)
        # the flag is persisted, so a second run goes straight to the list
        assert runtime.config.get("general.seen_onboarding") is True
        # a second run reads the same config file and must not show the welcome again
        reloaded = Config.load()
        app2 = SourceManagerApp(
            runtime=SourceManagerRuntime(config=reloaded), config=reloaded, sudo=FakeSudo()
        )
        assert app2.should_onboard() is False, "the welcome must not reappear"

    run(scenario())


def test_onboarding_can_be_forced_for_demos(isolated_env):
    mark_onboarding_seen(make_runtime())  # pretend it was seen before
    app = make_app(show_onboarding=True)
    assert app.should_onboard() is True


def test_clicking_a_row_opens_the_mirror_screen(isolated_env, apt_tree):
    async def scenario() -> None:
        app = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.4)
            app.open_ecosystem("apt")
            await pilot.pause(0.8)
            screen = app.screen
            assert isinstance(screen, EcosystemScreen)
            # the current mirror is described and the chooser is populated
            await wait_for(lambda: bool(screen.mirrors), pilot, what="the mirror list")
            assert screen.query_one("#eco-current")

    run(scenario())


def test_switch_from_the_ui_asks_for_admin_then_switches(isolated_env, apt_tree, monkeypatch):
    async def scenario() -> None:
        sudo = FakeSudo()
        app = make_app(sudo=sudo)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.5)
            source = D.custom_source(app.runtime, "apt", "https://mirror.example.org/ubuntu/")
            app.request_switch("apt", source)
            await pilot.pause(0.4)
            # APT needs admin rights: the password dialog must appear
            from srcmgr.tui.app import PasswordDialog

            assert isinstance(app.screen, PasswordDialog), "APT must ask for the password"
            app.screen.query_one("#password").value = "hunter2"
            await pilot.press("enter")
            await pilot.pause(0.6)
            assert sudo.asked == ["hunter2"]
            # The password is accepted but this process still is not root, so the core
            # refuses again - the UI must NOT loop asking, it must explain.  (Pretending
            # to be root is the only way to exercise the success path here.)
            assert not isinstance(app.screen, PasswordDialog), "no endless password prompt"
            assert app._admin_granted, "a password the user typed must count as admin"
            assert "mirror.example.org" not in str(D.load_ecosystem(app.runtime, "apt").current_url)
            # asking again for the same action is exactly what must NOT happen
            assert app._admin_refused is True

            # Now the real success path, with the privileges the password unlocked.
            monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)
            app._admin_granted = True
            app._admin_refused = False
            app.request_switch("apt", source)
            await wait_for(
                lambda: "mirror.example.org" in D.load_ecosystem(app.runtime, "apt").current_url,
                pilot,
                what="the switch to take effect",
            )
            assert not app.runtime.is_locked("apt")

    run(scenario())


def test_locked_ecosystem_cannot_be_switched_from_the_ui(isolated_env, apt_tree):
    """The heart of the hard lock: the UI must not change a pinned ecosystem."""

    async def scenario() -> None:
        app = make_app(sudo=FakeSudo())
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.5)
            before = app.runtime.current("apt").url
            app.runtime.lock("apt")
            source = D.custom_source(app.runtime, "apt", "https://evil.example.org/ubuntu/")
            app.request_switch("apt", source)
            await pilot.pause(0.4)
            assert isinstance(app.screen, ConfirmDialog), "the UI explains the lock"
            # cancelling leaves everything untouched
            await pilot.press("escape")
            await pilot.pause(0.3)
            assert app.runtime.current("apt").url == before
            assert app.runtime.is_locked("apt"), "cancelling must not unlock"

    run(scenario())


def test_lock_dialog_offers_a_one_click_way_out(isolated_env, apt_tree):
    async def scenario() -> None:
        app = make_app(sudo=FakeSudo())
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.5)
            app.runtime.lock("npm")
            source = D.custom_source(app.runtime, "npm", "https://registry.npmmirror.com")
            app.request_switch("npm", source)
            await pilot.pause(0.3)
            assert isinstance(app.screen, ConfirmDialog)
            assert "解锁" in str(app.screen.ok_text), "the dialog must offer unlocking"
            await pilot.press("y")  # confirm: unlock and switch
            await pilot.pause(0.6)
            assert not app.runtime.is_locked("npm")
            assert "npmmirror" in app.runtime.current("npm").url

    run(scenario())


def test_fastest_action_skips_locked_and_reports(isolated_env, apt_tree, monkeypatch):
    """With every ecosystem locked, the bulk action must report and change nothing."""
    runtime = make_runtime()
    for manager in runtime.manager_names():
        runtime.lock(manager)
    plan = D.plan_fastest(runtime)
    assert plan.changes == [], "nothing may be planned while everything is locked"
    assert plan.skipped_locked, "the skipped ecosystems must be reported"
    outcome = D.apply_fastest(runtime, plan)
    assert outcome.ok
    for manager in runtime.manager_names():
        assert runtime.is_locked(manager)


def test_wrong_password_is_reported_and_retried(isolated_env, apt_tree):
    async def scenario() -> None:
        sudo = FakeSudo(accept=False)
        app = make_app(sudo=sudo)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.4)
            app.ask_for_admin()
            await pilot.pause(0.3)
            from srcmgr.tui.app import PasswordDialog

            assert isinstance(app.screen, PasswordDialog)
            app.screen.query_one("#password").value = "wrong"
            await pilot.press("enter")
            await pilot.pause(0.5)
            # it asks again, with an error note, instead of failing silently
            assert isinstance(app.screen, PasswordDialog)
            assert app.screen.error, "the retry must explain what happened"
            await pilot.press("escape")

    run(scenario())


def test_quit_key_leaves_the_app_from_any_screen(isolated_env, apt_tree):
    async def scenario() -> None:
        app = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.5)
            app.open_ecosystem("apt")
            await pilot.pause(0.6)
            assert isinstance(app.screen, EcosystemScreen)
            await pilot.press("q")
            await pilot.pause(0.4)
            assert app.return_value is None  # exited without an exception

    run(scenario())


def test_backups_and_history_screens_are_reachable(isolated_env, apt_tree):
    async def scenario() -> None:
        app = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.5)
            app.open_history()
            await pilot.pause(0.3)
            assert app.screen.__class__.__name__ == "TableScreen"
            await pilot.press("escape")
            await pilot.pause(0.2)
            app.open_backups()
            await pilot.pause(0.3)
            assert app.screen.__class__.__name__ == "TableScreen"
            await pilot.press("escape")

    run(scenario())


def test_notify_is_used_for_results_not_a_crash(isolated_env, apt_tree):
    """A failing action must surface as a notification with guidance."""

    async def scenario() -> None:
        app = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.4)
            app.fail("Connection refused")
            await pilot.pause(0.2)
            assert app._notifications, "the user must be told"

    run(scenario())
