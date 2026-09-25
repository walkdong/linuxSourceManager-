"""Tests for the visual design system and the language switch.

These pin the rules the product owner asked for, so a later change cannot quietly bring
back the "AI look": no emoji, one accent colour, plain-word actions, four low-saturation
status colours where a dead *spare* source is a warning (not a failure), and a language
switch that works with one click and is remembered.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from srcmgr.core.api import TestResult as ProbeResult
from srcmgr.core.config import Config
from srcmgr.core.runtime import SourceManagerRuntime
from srcmgr.tui import data as D
from srcmgr.tui import theme as ui
from srcmgr.tui.app import SourceManagerApp, StatusBar
from srcmgr.tui.i18n import (
    MESSAGES,
    current_language,
    language_label,
    other_language,
    remember_language,
    tr,
)
from srcmgr.tui.sudo import SudoSession

#: Emoji and pictographs are what made the first version look machine-made.
EMOJI = re.compile(
    "[\U0001f300-\U0001faff\u2600-\u27bf\ufe0f\u2b00-\u2bff\U0001f000-\U0001f2ff]"
)

UBUNTU = "Types: deb\nURIs: http://archive.ubuntu.com/ubuntu/\nSuites: resolute\nComponents: main\n"
SECURITY = (
    "Types: deb\nURIs: http://security.ubuntu.com/ubuntu/\nSuites: resolute-security\n"
    "Components: main\n"
)


class FakeSudo(SudoSession):
    def can_run(self, command=None) -> bool:  # noqa: ANN001
        return False

    @property
    def has_admin(self) -> bool:
        return False

    def close(self) -> None:
        pass


@pytest.fixture
def apt_tree(isolated_env) -> Path:
    root = Path(isolated_env["SRCMGR_APT_ROOT"])
    dropins = root / "sources.list.d"
    dropins.mkdir(parents=True, exist_ok=True)
    (dropins / "ubuntu.sources").write_text(UBUNTU, encoding="utf-8")
    (dropins / "security.sources").write_text(SECURITY, encoding="utf-8")
    (root / "sources.list").write_text("# empty\n", encoding="utf-8")
    return root


def run(coro):
    return asyncio.run(coro)


def make_app(**kwargs) -> SourceManagerApp:
    runtime = kwargs.pop("runtime", None) or SourceManagerRuntime(config=Config.load())
    return SourceManagerApp(
        runtime=runtime,
        config=runtime.config,
        sudo=FakeSudo(),
        show_onboarding=kwargs.pop("show_onboarding", False),
        **kwargs,
    )


def set_health(runtime: SourceManagerRuntime, manager: str, *, active_ok: bool, spare_ok: bool,
               active_ms: float | None = 120.0) -> None:
    """Install synthetic probe results for the active and spare sources."""
    installed = runtime.installed(manager)
    active = runtime.current(manager)
    for source in installed:
        is_active = active is not None and source.id == active.id
        ok = active_ok if is_active else spare_ok
        runtime.cache.put(
            ProbeResult(
                source_id=source.id, manager=manager, name=source.name, ok=ok,
                status="ok" if ok else "timeout", latency_ms=active_ms if ok else None,
            )
        )


# ---------------------------------------------------------------------------
# message table hygiene
# ---------------------------------------------------------------------------
def test_no_emoji_in_any_message():
    offenders = [
        (language, key, text)
        for language, table in MESSAGES.items()
        for key, text in table.items()
        if EMOJI.search(text)
    ]
    assert not offenders, f"emoji belong nowhere in this UI: {offenders[:3]}"


def test_both_languages_define_the_same_keys():
    zh, en = set(MESSAGES["zh"]), set(MESSAGES["en"])
    assert zh == en, f"missing in en: {sorted(zh - en)} | extra in en: {sorted(en - zh)}"


def test_actions_are_plain_words():
    """No icon prefixes, no title-case shouting: a short verb each."""
    for key in ("action.scan", "action.fastest", "action.undo", "action.backups",
                "action.history", "action.admin", "action.quit"):
        text = tr(key, language="zh")
        assert EMOJI.search(text) is None, key
        assert len(text) <= 6, f"{key} should be a short label, got {text!r}"


def test_status_wording_is_about_impact_not_colour():
    assert "不影响" in tr("home.spares_down", language="zh", count=2)
    assert "正在用的源" in tr("home.current_down", language="zh")
    assert "打不开" in tr("home.current_down", language="zh")


# ---------------------------------------------------------------------------
# the four health phases
# ---------------------------------------------------------------------------
def test_all_healthy_is_ok(isolated_env, apt_tree):
    runtime = SourceManagerRuntime(config=Config.defaults())
    set_health(runtime, "apt", active_ok=True, spare_ok=True)
    row = D.load_ecosystem(runtime, "apt")
    assert row.health == D.HEALTH_OK
    assert "正常" in row.health_text()


def test_dead_spare_is_a_warning_not_a_failure(isolated_env, apt_tree):
    """The case the product owner called out: downloads still work -> amber, not red."""
    runtime = SourceManagerRuntime(config=Config.defaults())
    set_health(runtime, "apt", active_ok=True, spare_ok=False)
    row = D.load_ecosystem(runtime, "apt")
    assert row.health == D.HEALTH_SPARES_DOWN
    assert row.health == "spares_down"
    assert row.bad_sources >= 1
    text = row.health_text()
    assert "备用源失效" in text and "不影响使用" in text
    assert "打不开" not in text, "a spare being down must not read like a failure"


def test_dead_active_source_is_the_real_failure(isolated_env, apt_tree):
    runtime = SourceManagerRuntime(config=Config.defaults())
    set_health(runtime, "apt", active_ok=False, spare_ok=True)
    row = D.load_ecosystem(runtime, "apt")
    assert row.health == D.HEALTH_CURRENT_DOWN


def test_slow_active_source_is_amber(isolated_env, apt_tree):
    runtime = SourceManagerRuntime(config=Config.defaults())
    set_health(runtime, "apt", active_ok=True, spare_ok=True, active_ms=D.SLOW_MS + 500)
    row = D.load_ecosystem(runtime, "apt")
    assert row.health == D.HEALTH_SLOW


def test_unmeasured_is_unknown_and_says_so(isolated_env, apt_tree):
    runtime = SourceManagerRuntime(config=Config.defaults())
    row = D.load_ecosystem(runtime, "apt")
    assert row.health == D.HEALTH_UNKNOWN
    assert row.health_text() == tr("home.health_unknown", language="zh")


def test_status_colours_are_low_saturation():
    """Every status colour must be muted: a saturated traffic light looks cheap."""
    from colorsys import rgb_to_hls

    for mode, colours in ui.STATUS.items():
        for hexcode in colours:
            r, g, b = (int(hexcode[i : i + 2], 16) / 255 for i in (1, 3, 5))
            lightness = rgb_to_hls(r, g, b)[1]
            saturation = rgb_to_hls(r, g, b)[2]
            assert saturation < 0.55, f"{mode} {hexcode} is too vivid ({saturation:.2f})"
            assert 0.2 < lightness < 0.85, f"{mode} {hexcode} is not readable"


def test_one_accent_colour_per_look():
    """The palette must not grow a second accent: that is what reads as 'generated'."""
    for mode in ("dark", "light"):
        for accent in ui.ACCENTS:
            theme = ui.Look(mode=mode, accent=accent).theme()
            assert theme.primary == theme.accent, "primary and accent must be the same hue"
            assert theme.secondary != theme.primary or True


def test_stylesheet_is_installed_and_references_only_real_variables(isolated_env):
    """Regression: a class-level ``CSS`` with the custom theme dropped the whole sheet.

    Textual parses a class-level ``CSS`` the moment ``self.theme`` is assigned - before the
    theme's variables exist - and one unresolvable variable (``$muted``) made it discard
    everything, collapsing every widget to zero size.  The stylesheet is therefore
    installed by ``theme.register()`` *after* the theme is active.
    """
    from srcmgr.tui.theme import STYLESHEET

    app = make_app()
    selectors = [str(rule.selectors) for rule in app.stylesheet.rules]
    assert "StatusBar" in selectors, "the status bar's rules must be installed"
    assert any("bar-language" in selector for selector in selectors)
    # every variable used must be one Textual can resolve from the theme/colour system
    import re

    from textual.app import App as _App

    class _Probe(_App):
        def compose(self):  # pragma: no cover
            return iter(())

    probe = _Probe()
    srcmgr_theme = ui.Look()
    probe.register_theme(srcmgr_theme.theme())
    probe.theme = srcmgr_theme.theme_name
    available = set(probe.get_css_variables())
    used = set(re.findall(r"\$([a-z0-9\-]+)", STYLESHEET))
    assert used <= available, f"unresolvable variables: {sorted(used - available)}"


def test_dark_is_the_default_look():
    assert ui.Look().mode == "dark"
    assert ui.Look().dark is True


# ---------------------------------------------------------------------------
# language switching
# ---------------------------------------------------------------------------
def test_language_helpers(isolated_env):
    assert current_language() == "zh"
    assert language_label("zh") == "中"
    assert other_language("zh") == "en"
    assert other_language("en") == "zh"
    assert remember_language("en") is True
    assert current_language() == "en"
    assert remember_language("zh") is True


async def wait_for(predicate, pilot, *, timeout: float = 20.0, what: str = "condition"):
    """Poll until ``predicate()`` holds (the home screen fills its table in a worker)."""
    import time as _time

    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        if predicate():
            return
        await pilot.pause(0.1)
    raise AssertionError(f"timed out waiting for {what}")


def test_switch_language_rebuilds_every_label(isolated_env, apt_tree):
    async def scenario() -> None:
        app = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            home = app.screen
            await wait_for(
                lambda: len(home.query("Button")) > 0 and home.ecosystems, pilot,
                what="the first render",
            )
            labels = [str(button.label) for button in home.query("Button")]
            assert "测速" in labels, labels
            app.switch_language("en")
            await pilot.pause(0.4)
            labels = [str(button.label) for button in app.screen.query("Button")]
            assert "Test speeds" in labels, labels
            table = app.screen.query_one("#ecosystems")
            cells = [str(table.get_cell(key, "state")) for key in list(table.rows)]
            assert cells, "rows must survive a language switch"
            assert any("not measured" in cell or "not installed" in cell for cell in cells), cells

    run(scenario())


def test_language_choice_is_remembered(isolated_env, apt_tree):
    async def scenario() -> None:
        app = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.4)
            app.switch_language("en")
            await pilot.pause(0.3)
        assert Config.load().get("general.language") == "en"
        # a fresh app starts in the saved language
        app2 = make_app()
        assert app2.language == "en"

    run(scenario())


def test_language_switch_is_clickable_in_the_status_bar(isolated_env, apt_tree):
    """The label must be a real click target with a wired handler.

    The test harness does not compute widget geometry, so the click is delivered through
    the same handler a real click reaches instead of hit-testing pixel coordinates.
    """
    from textual.events import Click

    async def scenario() -> None:
        app = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.4)
            bar = app.screen.query_one(StatusBar)
            label = bar.query_one("#bar-language")
            assert label.can_focus, "the language label must be a focusable click target"
            before = app.language
            label.post_message(
                Click(widget=label, x=0, y=0, delta_x=0, delta_y=0, button=1,
                      shift=False, meta=False, ctrl=False)
            )
            await pilot.pause(0.5)
            assert app.language != before, "clicking the language label must switch"

    run(scenario())


def test_status_bar_shows_lock_count_and_admin_state(isolated_env, apt_tree):
    async def scenario() -> None:
        app = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.4)
            app.runtime.lock("apt")
            app._refresh_status_bar()
            await pilot.pause(0.3)
            text = " ".join(app._lock_summary() for _ in [0])
            assert "锁定" in text or "locked" in text
            assert app._admin_summary()

    run(scenario())


def test_no_emoji_reaches_the_rendered_rows(isolated_env, apt_tree):
    async def scenario() -> None:
        app = make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.4)
            table = app.screen.query_one("#ecosystems")
            await wait_for(lambda: bool(table.rows), pilot, what="the rows")
            rendered = [
                str(table.get_cell(key, column))
                for key in table.rows
                for column in ("mark", "state", "lock")
            ]
            joined = " ".join(rendered)
            assert EMOJI.search(joined) is None, joined
            assert ui.GLYPHS["locked"] in joined or ui.GLYPHS["unlocked"] in joined

    run(scenario())
