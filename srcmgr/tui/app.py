"""Full-screen menu UI - the beginner frontend.

Who this is for
---------------
Someone who has never used Linux (possibly a child).  They must be able to:

* see, at a glance, whether each software's download source is healthy,
* change it with the arrow keys or the mouse, without typing anything,
* **pin** a source they are happy with so nothing changes it again,
* recover from a mistake (undo, backups), and
* never have to read a stack trace: failures are explained in plain Chinese with the
  button that fixes them.

How it is built
---------------
* Textual widgets, Chinese-first strings from :mod:`srcmgr.tui.i18n`.
* All data/actions live in :mod:`srcmgr.tui.data`; the lock is enforced both there and in
  :mod:`srcmgr.core.locks`, so no widget can bypass it.
* Slow work (reading ecosystems, probing mirrors) runs in Textual workers so the UI stays
  responsive and shows progress.
* The app takes injectable ``runtime``/``sudo`` objects, which is how the tests drive it
  without touching the machine.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Sequence

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    Static,
)

from .. import APP_NAME, __version__
from ..core.config import Config
from ..core.errors import LockedError, SourceManagerError
from ..core.paths import is_root
from ..core.runtime import SourceManagerRuntime, canonical_manager
from . import data as D
from .i18n import friendly_error, tr
from .sudo import SudoSession

ONBOARDING_KEY = "general.seen_onboarding"


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def _onboarding_seen(runtime: SourceManagerRuntime) -> bool:
    try:
        return bool(runtime.config.get(ONBOARDING_KEY, False))
    except Exception:  # noqa: BLE001
        return True


def mark_onboarding_seen(runtime: SourceManagerRuntime) -> None:
    try:
        runtime.config.set(ONBOARDING_KEY, True)
    except Exception:  # noqa: BLE001 - a read-only config must not break start-up
        pass


def _ms(value: float | None) -> str:
    if value is None:
        return "?"
    return f"{value:.0f}ms"


def _is_permission_error(exc: BaseException) -> bool:
    """True for "needs root" failures, wherever they surfaced from."""
    from ..core.errors import PermissionDenied

    if isinstance(exc, PermissionDenied):
        return True
    text = str(exc).lower()
    return any(
        needle in text
        for needle in ("permission", "sudo", "root", "eacces", "not authorized", "需要管理员")
    )


# ---------------------------------------------------------------------------
# dialogs
# ---------------------------------------------------------------------------
class ConfirmDialog(ModalScreen[bool]):
    """Yes/no question.  ``Esc`` cancels (the safe default), ``y``/``n`` also work."""

    BINDINGS = [
        Binding("escape", "dismiss(False)", "取消", show=False),
        Binding("n", "dismiss(False)", "取消", show=False),
        Binding("y", "dismiss(True)", "确定", show=False),
    ]

    def __init__(self, title: str, body: str, *, ok: str = "", cancel: str = "") -> None:
        super().__init__()
        self.title_text = title
        self.body_text = body
        self.ok_text = ok or tr("common.yes")
        self.cancel_text = cancel or tr("common.cancel")

    def compose(self) -> ComposeResult:
        with Container(classes="dialog"):
            yield Label(self.title_text, classes="dialog-title")
            yield Static(self.body_text, classes="dialog-body")
            with Horizontal(classes="dialog-buttons"):
                yield Button(self.ok_text, id="confirm", variant="primary")
                yield Button(self.cancel_text, id="cancel")

    @on(Button.Pressed, "#confirm")
    def _confirm(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(False)


class PasswordDialog(ModalScreen[str | None]):
    """Ask for the login password (never stored, never echoed)."""

    BINDINGS = [Binding("escape", "dismiss(None)", "取消", show=False)]

    def __init__(self, *, error: str = "") -> None:
        super().__init__()
        self.error = error

    def compose(self) -> ComposeResult:
        with Container(classes="dialog"):
            yield Label(tr("admin.needed_title"), classes="dialog-title")
            yield Static(tr("admin.needed_body"), classes="dialog-body")
            if self.error:
                yield Static(f"❌ {self.error}", classes="dialog-error")
            yield Input(password=True, placeholder=tr("admin.password"), id="password")
            with Horizontal(classes="dialog-buttons"):
                yield Button(tr("common.yes"), id="ok", variant="primary")
                yield Button(tr("common.cancel"), id="cancel")

    def on_mount(self) -> None:
        self.query_one("#password", Input).focus()

    @on(Input.Submitted, "#password")
    def _submitted(self) -> None:
        self._accept()

    @on(Button.Pressed, "#ok")
    def _accept(self) -> None:
        self.dismiss(self.query_one("#password", Input).value)

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)


class TextDialog(ModalScreen[str | None]):
    """One-line text prompt (the "自己输入网址" entry)."""

    BINDINGS = [Binding("escape", "dismiss(None)", "取消", show=False)]

    def __init__(self, title: str, *, placeholder: str = "", value: str = "") -> None:
        super().__init__()
        self.title_text = title
        self.placeholder = placeholder
        self.value = value

    def compose(self) -> ComposeResult:
        with Container(classes="dialog"):
            yield Label(self.title_text, classes="dialog-title")
            yield Input(value=self.value, placeholder=self.placeholder, id="text")
            with Horizontal(classes="dialog-buttons"):
                yield Button(tr("common.yes"), id="ok", variant="primary")
                yield Button(tr("common.cancel"), id="cancel")

    def on_mount(self) -> None:
        self.query_one("#text", Input).focus()

    @on(Input.Submitted, "#text")
    def _submitted(self) -> None:
        self.dismiss(self.query_one("#text", Input).value)

    @on(Button.Pressed, "#ok")
    def _accept(self) -> None:
        self.dismiss(self.query_one("#text", Input).value)

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)


class NoticeDialog(ModalScreen[None]):
    """Information with a single button (onboarding, plain errors)."""

    BINDINGS = [Binding("escape", "dismiss(None)", "关闭", show=False)]

    def __init__(self, title: str, body: str, *, ok: str = "", variant: str = "primary") -> None:
        super().__init__()
        self.title_text = title
        self.body_text = body
        self.ok_text = ok or tr("common.ok")
        self.variant = variant

    def compose(self) -> ComposeResult:
        with Container(classes="dialog"):
            yield Label(self.title_text, classes="dialog-title")
            yield Static(self.body_text, classes="dialog-body")
            with Horizontal(classes="dialog-buttons"):
                yield Button(self.ok_text, id="ok", variant=self.variant)

    @on(Button.Pressed, "#ok")
    def _close(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# home screen
# ---------------------------------------------------------------------------
class HomeScreen(Screen[None]):
    """Ecosystem list with always-visible lock buttons and the bulk actions."""

    BINDINGS = [
        Binding("l", "toggle_lock", "锁定/解锁"),
        Binding("r", "refresh", "刷新"),
        Binding("u", "undo", "撤销"),
        Binding("b", "backups", "备份"),
        Binding("h", "history", "记录"),
        Binding("q", "quit", "退出"),
        Binding("enter", "open_row", "进入", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.ecosystems: list[D.Ecosystem] = []
        self.loading = True

    # -- layout --------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical(id="home"):
            yield Static(tr("home.question"), id="question")
            yield Static(tr("home.loading"), id="loading")
            yield DataTable(id="ecosystems", cursor_type="row", zebra_stripes=True)
            with Horizontal(id="actions"):
                yield Button(tr("action.scan"), id="scan", variant="primary")
                yield Button(tr("action.fastest"), id="fastest", variant="success")
                yield Button(tr("action.undo"), id="undo")
                yield Button(tr("action.backups"), id="backups")
                yield Button(tr("action.history"), id="history")
                yield Button(tr("action.admin"), id="admin")
                yield Button(tr("action.quit"), id="quit")
            yield Static(tr("home.hint"), id="hint")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#ecosystems", DataTable)
        table.add_column("", key="mark", width=3)
        table.add_column(tr("home.col_software"), key="software")
        table.add_column(tr("home.col_current"), key="name")
        table.add_column(tr("home.col_state"), key="state", width=22)
        table.add_column(tr("home.col_lock"), key="lock", width=26)
        self.reload()

    # -- data ----------------------------------------------------------------
    def reload(self) -> None:
        """Reload ecosystems (the actual work happens in a worker thread)."""
        loading = self.query_one("#loading", Static)
        loading.display = True
        loading.update(tr("home.loading"))
        self.load_worker()

    @work(thread=True, exclusive=True, group="load")
    def load_worker(self) -> None:
        try:
            ecosystems = D.load_ecosystems(self.app.runtime)
        except Exception as exc:  # noqa: BLE001 - never kill the UI on a plugin problem
            self.app.call_from_thread(self._load_failed, str(exc))
            return
        self.app.call_from_thread(self._show, ecosystems)

    def _show_offline_note(self) -> None:
        """Explain a fully-offline run instead of showing everything as broken."""
        import os as _os

        if _os.environ.get("SRCMGR_OFFLINE", "").strip().lower() not in {"1", "true", "yes", "on"}:
            return
        loading = self.query_one("#loading", Static)
        loading.display = True
        loading.update(tr("home.offline_note"))

    def _load_failed(self, message: str) -> None:
        self.loading = False
        what, todo = friendly_error(message)
        self.query_one("#loading", Static).update(f"❌ {what} {todo}")

    def _show(self, ecosystems: list[D.Ecosystem]) -> None:
        self.ecosystems = ecosystems
        table = self.query_one("#ecosystems", DataTable)
        table.clear()
        for eco in ecosystems:
            table.add_row(
                self._mark(eco),
                eco.name,
                eco.current_text(),
                eco.health_text(),
                eco.lock_text(),
                key=eco.manager,
            )
        self.loading = False
        self.query_one("#loading", Static).display = False
        self._show_offline_note()
        if ecosystems:
            table.move_cursor(row=0)
            table.focus()
        self._update_header()

    def _mark(self, eco: D.Ecosystem) -> str:
        if eco.locked:
            return "🔒"
        if not eco.available:
            return "⚪"
        if eco.health == D.HEALTH_BAD:
            return "🔴"
        if eco.health == D.HEALTH_SLOW:
            return "🟡"
        if eco.health == D.HEALTH_OK:
            return "🟢"
        return "⚫"

    def _update_header(self) -> None:
        locked = [eco for eco in self.ecosystems if eco.locked]
        text = tr("home.locked_summary", count=len(locked)) if locked else ""
        try:
            self.query_one(Header).sub_text = text  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - older Textual without sub_text
            pass

    # -- selection -----------------------------------------------------------
    @property
    def selected(self) -> D.Ecosystem | None:
        row = self.query_one("#ecosystems", DataTable).cursor_row
        if row is None or not (0 <= row < len(self.ecosystems)):
            return None
        return self.ecosystems[row]

    def _typing(self) -> bool:
        """True when a dialog owns the keyboard (so ``l``/``q`` must not fire)."""
        return isinstance(self.app.focused, Input)

    def refresh_lock_cells(self) -> None:
        table = self.query_one("#ecosystems", DataTable)
        for eco in self.ecosystems:
            try:
                table.update_cell(eco.manager, "lock", eco.lock_text())
                table.update_cell(eco.manager, "mark", self._mark(eco))
            except Exception:  # noqa: BLE001 - the table may be mid-reload
                continue
        self._update_header()

    # -- actions -------------------------------------------------------------
    def action_open_row(self) -> None:
        eco = self.selected
        if eco is not None:
            self.app.open_ecosystem(eco.manager)

    @on(DataTable.RowSelected, "#ecosystems")
    def _row_selected(self, event: DataTable.RowSelected) -> None:
        self.app.open_ecosystem(str(event.row_key.value))

    def action_toggle_lock(self) -> None:
        if self._typing():
            return
        eco = self.selected
        if eco is None:
            return
        locked = self.app.toggle_lock(eco.manager)
        eco.locked = locked
        self.refresh_lock_cells()
        self.app.info(
            tr("lock.now_locked", name=eco.name)
            if locked
            else tr("lock.now_unlocked", name=eco.name),
            tr("lock.explain") if locked else "",
        )

    def action_refresh(self) -> None:
        if self._typing():
            return
        self.reload()

    def action_undo(self) -> None:
        if self._typing():
            return
        eco = self.selected
        self.app.request_undo(eco.manager if eco else "")

    def action_backups(self) -> None:
        if self._typing():
            return
        self.app.open_backups()

    def action_history(self) -> None:
        if self._typing():
            return
        self.app.open_history()

    def action_quit(self) -> None:
        if self._typing():
            return
        self.app.exit()

    @on(Button.Pressed, "#scan")
    def _scan(self) -> None:
        self.app.run_scan()

    @on(Button.Pressed, "#fastest")
    def _fastest(self) -> None:
        self.app.run_fastest()

    @on(Button.Pressed, "#undo")
    def _undo(self) -> None:
        self.action_undo()

    @on(Button.Pressed, "#backups")
    def _backups(self) -> None:
        self.app.open_backups()

    @on(Button.Pressed, "#history")
    def _history(self) -> None:
        self.app.open_history()

    @on(Button.Pressed, "#admin")
    def _admin(self) -> None:
        self.app.ask_for_admin()

    @on(Button.Pressed, "#quit")
    def _quit(self) -> None:
        self.app.exit()


# ---------------------------------------------------------------------------
# ecosystem (mirror chooser) screen
# ---------------------------------------------------------------------------
class EcosystemScreen(Screen[None]):
    """Pick a mirror for one ecosystem: the heart of the beginner workflow."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "返回"),
        Binding("l", "toggle_lock", "锁定/解锁"),
        Binding("r", "rescan", "重新测速"),
        Binding("u", "undo", "撤销"),
        Binding("a", "show_all", "显示全部"),
    ]

    def __init__(self, manager: str) -> None:
        super().__init__()
        self.manager = canonical_manager(manager)
        self.eco: D.Ecosystem | None = None
        self.mirrors: list[D.Mirror] = []
        self._show_all = False

    # -- layout --------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical(id="eco"):
            with Horizontal(id="eco-head"):
                yield Button(tr("common.back"), id="back")
                yield Label(D.display_name(self.app.runtime, self.manager), id="eco-title")
                yield Label("", id="eco-lock")
            yield Static("", id="eco-current")
            yield Static(tr("eco.choose"), id="eco-question")
            yield Static(tr("eco.testing"), id="eco-testing")
            yield ListView(id="mirrors")
            with Horizontal(id="eco-actions"):
                yield Button(tr("eco.custom"), id="custom")
                yield Button(tr("eco.rescan"), id="rescan")
                yield Button(tr("eco.undo"), id="undo")
                yield Button(tr("eco.backups"), id="backups")
            yield Static(tr("eco.footer"), id="eco-hint")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#eco-testing", Static).display = False
        self.refresh_state()
        self.measure()

    # -- state ---------------------------------------------------------------
    def refresh_state(self) -> None:
        self.eco = D.load_ecosystem(self.app.runtime, self.manager)
        mirrors = D.load_mirrors(self.app.runtime, self.manager)
        if not self._show_all:
            mirrors = mirrors[: D.MAX_VISIBLE_MIRRORS]
        self.mirrors = mirrors
        self._paint()

    def _paint(self) -> None:
        eco = self.eco
        title = self.query_one("#eco-title", Label)
        lock = self.query_one("#eco-lock", Label)
        if eco is not None:
            title.update(("🔒 " if eco.locked else "") + eco.name)
            lock.update(tr("lock.locked_row") if eco.locked else tr("lock.unlocked_row"))
            lock.set_class(eco.locked, "locked")
        current = self.query_one("#eco-current", Static)
        if eco is None:
            current.update("")
        elif eco.current:
            current.update(f"{tr('eco.current')}：[b]{eco.current}[/b]　{eco.current_url}")
        else:
            current.update(tr("home.no_source"))

        view = self.query_one("#mirrors", ListView)
        view.clear()
        if not self.mirrors:
            view.append(ListItem(Label(tr("eco.none_available")), disabled=True))
            return
        for mirror in self.mirrors:
            marker = "▶ " if mirror.is_current else "　"
            state = f"　[{tr('eco.current')}]" if mirror.is_current else ""
            item = ListItem(Label(f"{marker}{mirror.title}　{mirror.speed_text}{state}"))
            if mirror.is_current:
                item.add_class("current")
            view.append(item)
        view.focus()

    # -- measuring -----------------------------------------------------------
    @work(thread=True, exclusive=True, group="mirrors")
    def measure(self) -> None:
        self.app.call_from_thread(self._set_testing, True)
        try:
            mirrors = D.measure_mirrors(self.app.runtime, self.manager)
        except Exception as exc:  # noqa: BLE001
            self.app.call_from_thread(self._testing_failed, str(exc))
            return
        self.app.call_from_thread(self._measured, mirrors)

    def _set_testing(self, testing: bool) -> None:
        self.query_one("#eco-testing", Static).display = testing

    def _measured(self, mirrors: list[D.Mirror]) -> None:
        if not self._show_all:
            mirrors = mirrors[: D.MAX_VISIBLE_MIRRORS]
        self.mirrors = mirrors
        self._set_testing(False)
        self._paint()

    def _testing_failed(self, message: str) -> None:
        self._set_testing(False)
        what, todo = friendly_error(message)
        self.app.notify(what, todo, severity="warning", timeout=8)

    # -- actions -------------------------------------------------------------
    def action_toggle_lock(self) -> None:
        locked = self.app.toggle_lock(self.manager)
        self.refresh_state()
        name = self.eco.name if self.eco else self.manager
        self.app.info(
            tr("lock.now_locked", name=name)
            if locked
            else tr("lock.now_unlocked", name=name),
            tr("lock.explain") if locked else "",
        )

    def action_rescan(self) -> None:
        self.measure()

    def action_show_all(self) -> None:
        """Show every mirror instead of the fastest dozen."""
        self._show_all = True
        self.mirrors = D.load_mirrors(self.app.runtime, self.manager)
        self._paint()
        self.app.info(tr("eco.showing_all", count=len(self.mirrors)))

    def action_undo(self) -> None:
        self.app.request_undo(self.manager)

    @on(ListView.Selected, "#mirrors")
    def _mirror_chosen(self, event: ListView.Selected) -> None:
        index = event.list_view.index
        if index is None or not (0 <= index < len(self.mirrors)):
            return
        mirror = self.mirrors[index]
        if mirror.is_current:
            self.app.info(tr("eco.already", name=mirror.title))
            return
        self.app.request_switch(self.manager, mirror.source, on_done=self.refresh_state)

    @on(Button.Pressed, "#custom")
    def _custom(self) -> None:
        self.app.request_custom_source(self.manager, on_done=self.refresh_state)

    @on(Button.Pressed, "#back")
    def _back(self) -> None:
        self.app.pop_screen()

    @on(Button.Pressed, "#rescan")
    def _rescan(self) -> None:
        self.measure()

    @on(Button.Pressed, "#undo")
    def _undo(self) -> None:
        self.action_undo()

    @on(Button.Pressed, "#backups")
    def _backups(self) -> None:
        self.app.open_backups()


# ---------------------------------------------------------------------------
# read-only list screens (history / backups)
# ---------------------------------------------------------------------------
class TableScreen(Screen[None]):
    """A scrollable table with a Back button and an optional row action."""

    BINDINGS = [Binding("escape", "app.pop_screen", "返回")]

    def __init__(
        self,
        title: str,
        columns: Sequence[tuple[str, str]],
        rows: Sequence[dict[str, Any]],
        *,
        empty: str = "",
        row_action: str = "",
        key_field: str = "id",
        on_row: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__()
        self.title_text = title
        self.columns = list(columns)
        self.rows = list(rows)
        self.empty = empty
        self.row_action = row_action
        self.key_field = key_field
        self.on_row = on_row

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical(id="table-screen"):
            with Horizontal(id="table-head"):
                yield Button(tr("common.back"), id="back")
                yield Label(self.title_text, id="table-title")
                if self.row_action and self.rows:
                    yield Button(self.row_action, id="row-action", variant="primary")
            if not self.rows:
                yield Static(self.empty or tr("common.none"), id="table-empty")
            yield DataTable(id="rows", cursor_type="row", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#rows", DataTable)
        for column_title, key in self.columns:
            table.add_column(column_title, key=key)
        table.display = bool(self.rows)
        for row in self.rows:
            table.add_row(
                *[str(row.get(key, "")) for _title, key in self.columns],
                key=str(row.get(self.key_field, "")),
            )
        if self.rows:
            table.focus()
        label = self.query_one("#table-title", Label)
        label.update(f"{self.title_text}　（{len(self.rows)}）" if self.rows else self.title_text)

    def _selected_key(self) -> str:
        row = self.query_one("#rows", DataTable).cursor_row
        if row is None or not (0 <= row < len(self.rows)):
            return ""
        return str(self.rows[row].get(self.key_field, ""))

    @on(Button.Pressed, "#row-action")
    def _row_action(self) -> None:
        if self.on_row is not None:
            self.on_row(self._selected_key())

    @on(Button.Pressed, "#back")
    def _back(self) -> None:
        self.app.pop_screen()

    @on(DataTable.RowSelected, "#rows")
    def _row_selected(self, event: DataTable.RowSelected) -> None:
        if self.on_row is not None:
            self.on_row(str(event.row_key.value))


# ---------------------------------------------------------------------------
# the application
# ---------------------------------------------------------------------------
class SourceManagerApp(App[None]):
    """The menu application: home screen, dialogs, workers and admin handling."""

    TITLE = tr("app.title")
    SUB_TITLE = tr("app.subtitle")
    ENABLE_COMMAND_PALETTE = False  # a beginner has no use for Ctrl+P
    #: ``q`` must leave the app from *any* screen (a beginner presses q when lost);
    #: screens that need it for something else can still shadow it.
    BINDINGS = [
        Binding("q", "quit_app", "退出"),
        Binding("ctrl+q", "quit_app", "退出", show=False),
        Binding("f1", "help", "帮助", show=False),
    ]
    CSS = """
    Screen { background: $surface; }
    #home { padding: 1 2; }
    #question { text-style: bold; padding-bottom: 1; }
    #loading { color: $text-warning; padding-bottom: 1; }
    DataTable { height: 1fr; border: round $primary; }
    #actions, #eco-actions { height: auto; padding-top: 1; }
    #actions Button, #eco-actions Button { margin-right: 1; }
    #hint, #eco-hint { color: $text-muted; padding-top: 1; }
    #eco { padding: 1 2; }
    #eco-head { height: auto; }
    #eco-title { text-style: bold; padding: 0 1; width: 1fr; }
    #eco-lock { color: $text-success; width: auto; }
    #eco-lock.locked { color: $text-error; text-style: bold; }
    #eco-current { padding: 1 0; }
    #eco-testing { color: $text-warning; }
    #mirrors { height: 1fr; border: round $primary; }
    #mirrors ListItem { padding: 0 1; }
    #mirrors ListItem.current { color: $text-success; }
    #table-screen { padding: 1 2; }
    #table-head { height: auto; }
    #table-title { text-style: bold; padding: 0 1; width: 1fr; }
    #rows { height: 1fr; border: round $primary; }
    .dialog {
        width: 74; height: auto; padding: 1 2; border: thick $accent;
        background: $panel; margin: 4 8;
    }
    .dialog-title { text-style: bold; padding-bottom: 1; }
    .dialog-body { padding-bottom: 1; }
    .dialog-error { color: $text-error; padding-bottom: 1; }
    .dialog-buttons { height: auto; align-horizontal: right; }
    .dialog-buttons Button { margin-left: 1; }
    """

    def __init__(
        self,
        *,
        runtime: SourceManagerRuntime | None = None,
        config: Config | None = None,
        sudo: SudoSession | None = None,
        show_onboarding: bool | None = None,
    ) -> None:
        super().__init__()
        self.config = config or Config.load()
        self.runtime = runtime or SourceManagerRuntime(config=self.config)
        self.sudo = sudo or SudoSession()
        self._force_onboarding = show_onboarding
        self.busy = False
        #: set once the user has authenticated in this run, so a later action does not
        #: ask for the password again (sudo has a timestamp, but our check cannot always
        #: see it - e.g. a non-interactive `sudo -n` probe)
        self._admin_granted = False
        #: set as soon as the password has been requested once for this run; the retry
        #: path consults it so a refused action explains itself instead of re-prompting
        self._admin_refused = False

    # -- lifecycle -----------------------------------------------------------
    def on_mount(self) -> None:
        self.push_screen(HomeScreen())
        if self.should_onboard():
            self.show_onboarding()

    def should_onboard(self) -> bool:
        if self._force_onboarding is not None:
            return self._force_onboarding
        return not _onboarding_seen(self.runtime)

    def show_onboarding(self) -> None:
        def done(_result: None) -> None:
            mark_onboarding_seen(self.runtime)

        self.push_screen(
            NoticeDialog(tr("onboard.title"), tr("onboard.body"), ok=tr("onboard.ok")), done
        )

    def action_help(self) -> None:
        self.push_screen(NoticeDialog(tr("onboard.title"), tr("onboard.body")))

    def action_quit_app(self) -> None:
        """Visible quit, available everywhere (dialogs keep ``Esc``)."""
        if isinstance(self.screen, ModalScreen):
            return
        if isinstance(self.focused, Input):
            return
        self.exit()

    # -- notifications -------------------------------------------------------
    def info(self, message: str, detail: str = "", *, severity: str = "information") -> None:
        """Tell the user what happened (``detail`` becomes the notification title)."""
        self.notify(message, title=detail or "", severity=severity, timeout=8)

    def fail(self, message: str, detail: str = "") -> None:
        """Explain a failure in plain words, with the action that fixes it."""
        what, todo = friendly_error(message)
        self.notify(what, title=detail or todo, severity="error", timeout=12)

    # -- lock ----------------------------------------------------------------
    def toggle_lock(self, manager: str) -> bool:
        return self.runtime.toggle_lock(manager)

    @property
    def has_admin(self) -> bool:
        """Whether this run has admin rights (root, a live sudo session, or a password
        the user already typed)."""
        return bool(is_root() or self.sudo.has_admin or self._admin_granted)

    # -- navigation ----------------------------------------------------------
    def open_ecosystem(self, manager: str) -> None:
        self.push_screen(EcosystemScreen(manager))

    def open_backups(self) -> None:
        rows = D.backup_rows(self.runtime)

        def restore(backup_id: str) -> None:
            if not backup_id:
                return
            row = next((item for item in rows if item["id"] == backup_id), None)
            when = row["when"] if row else backup_id
            name = row["manager"] if row else ""

            def confirmed(yes: bool | None) -> None:
                if not yes:
                    return
                outcome = D.restore_backup(self.runtime, backup_id)
                if outcome.ok:
                    self.info(outcome.message)
                else:
                    self.fail(outcome.message, outcome.detail)
                self.refresh_home()

            self.push_screen(
                ConfirmDialog(
                    tr("backups.confirm_title"),
                    tr("backups.confirm_body", name=name, when=when),
                    ok=tr("backups.restore"),
                ),
                confirmed,
            )

        self.push_screen(
            TableScreen(
                tr("backups.title"),
                [
                    (tr("backups.col_time"), "when"),
                    (tr("backups.col_manager"), "manager"),
                    (tr("backups.col_files"), "files"),
                ],
                rows,
                empty=tr("backups.empty"),
                row_action=tr("backups.restore"),
                key_field="id",
                on_row=restore,
            )
        )

    def open_history(self) -> None:
        rows = D.history_rows(self.runtime)
        self.push_screen(
            TableScreen(
                tr("history.title"),
                [
                    (tr("history.col_when"), "when"),
                    (tr("history.col_action"), "action"),
                    (tr("history.col_manager"), "manager"),
                    (tr("history.col_detail"), "detail"),
                    ("", "ok"),
                ],
                rows,
                empty=tr("history.empty"),
            )
        )

    def refresh_home(self) -> None:
        for screen in self.screen_stack:
            if isinstance(screen, HomeScreen):
                screen.reload()

    # -- admin ---------------------------------------------------------------
    def ask_for_admin(self, *, then: Callable[[], None] | None = None, error: str = "") -> None:
        """Prompt for the login password and keep the sudo session for this run."""

        def got(password: str | None) -> None:
            if password is None:
                self.info(tr("admin.cancelled"))
                return
            if self.sudo.ensure(password):
                self._admin_granted = True
                self.info(tr("admin.ok"), f"sudo: {self.sudo.status_text}")
                if then is not None:
                    then()
                return
            what, todo = friendly_error(self.sudo.last_error)
            if self.sudo.attempts >= 3:
                self.push_screen(
                    NoticeDialog(tr("error.title"), f"{what}\n\n{todo}", variant="error")
                )
                return
            self.ask_for_admin(then=then, error=tr("admin.bad_password"))

        self.push_screen(PasswordDialog(error=error), got)

    def require_admin(self, managers: Iterable[str], then: Callable[[], None]) -> bool:
        """Run ``then`` once admin rights exist (asking for the password if needed).

        The password is asked at most once per run: after that the action is simply
        attempted again, and if the system still refuses, the user gets an explanation
        instead of a second prompt (a loop here would be maddening).
        """
        if is_root() or self.sudo.has_admin or self._admin_granted or self._admin_refused:
            then()
            return True
        pending = [name for name in managers if D.needs_admin(self.runtime, name)]
        if not pending:
            then()
            return True
        self._admin_refused = True  # ask once; the retry path never asks again
        self.ask_for_admin(then=then)
        return False

    # -- actions that do real work -------------------------------------------
    def request_switch(
        self, manager: str, source: Any, *, on_done: Callable[[], None] | None = None
    ) -> None:
        """Switch one ecosystem - the lock is checked here *and* in the core."""
        if self.runtime.is_locked(manager):
            self._locked_dialog(manager, source, on_done=on_done)
            return

        def do_switch(*, retried: bool = False) -> None:
            try:
                outcome = D.switch(self.runtime, manager, source)
            except LockedError as exc:
                self._locked_dialog(manager, source, on_done=on_done, message=str(exc))
                return
            except SourceManagerError as exc:
                # The core refused because we are not root yet (the app is only told
                # "permission denied" - it has no way to know beforehand on every path).
                # Ask for the password and try once more instead of showing a dead end.
                if _is_permission_error(exc):
                    if retried or self._admin_granted or self.sudo.has_admin:
                        # The password was already given (or sudo cannot help at all):
                        # never prompt again, explain what to do instead.
                        self.fail(tr("admin.cannot_prompt"), tr("admin.skipped", count=1))
                    elif not self.sudo.attempts:
                        self.ask_for_admin(then=lambda: do_switch(retried=True))
                    return  # no prompt loop: at most one password question per action
                self.fail(str(exc))
                return
            if outcome.ok:
                self.info(outcome.message)
            elif outcome.data.get("permission") and not self._admin_refused:
                self._admin_refused = True
                self.ask_for_admin(then=lambda: do_switch(retried=True))
                return
            elif outcome.data.get("permission"):
                self.fail(tr("admin.cannot_prompt"), tr("admin.skipped", count=1))
                return
            else:
                self.fail(outcome.message, outcome.detail)
            if on_done is not None:
                on_done()
            self.refresh_home()

        self.require_admin([manager], lambda: do_switch())

    def _locked_dialog(
        self,
        manager: str,
        source: Any,
        *,
        on_done: Callable[[], None] | None = None,
        message: str = "",
    ) -> None:
        """Explain the pin and offer a one-click way out (never a dead end)."""
        name = D.display_name(self.runtime, manager)

        def answered(yes: bool | None) -> None:
            if not yes:
                if message:
                    self.info(message)
                return
            self.runtime.unlock(manager)
            self.info(tr("lock.now_unlocked", name=name))
            self.request_switch(manager, source, on_done=on_done)
            self.refresh_home()

        self.push_screen(
            ConfirmDialog(
                tr("lock.blocked_title", name=name),
                tr("lock.blocked_body"),
                ok=tr("lock.blocked_unlock_and_switch"),
            ),
            answered,
        )

    def request_custom_source(
        self, manager: str, *, on_done: Callable[[], None] | None = None
    ) -> None:
        def got(url: str | None) -> None:
            if not url:
                return
            if not D.valid_url(url):
                self.push_screen(
                    NoticeDialog(tr("error.title"), tr("eco.custom_invalid"), variant="error")
                )
                return
            self.request_switch(
                manager, D.custom_source(self.runtime, manager, url.strip()), on_done=on_done
            )

        self.push_screen(TextDialog(tr("eco.custom_prompt"), placeholder="https://"), got)

    def request_undo(self, manager: str) -> None:
        if not manager:
            self.info(tr("undo.nothing"))
            return
        name = D.display_name(self.runtime, manager)

        def confirmed(yes: bool | None) -> None:
            if not yes:
                return
            outcome = D.undo_last(self.runtime, manager)
            if outcome.ok:
                self.info(outcome.message)
            else:
                self.fail(outcome.message, outcome.detail)
            self.refresh_home()

        self.push_screen(
            ConfirmDialog(tr("undo.confirm_title"), tr("undo.confirm_body", name=name)), confirmed
        )

    def run_scan(self) -> None:
        if self.busy:
            return
        self.busy = True
        self.info(tr("scan.running"))
        self._scan_worker()

    @work(thread=True, exclusive=True, group="scan")
    def _scan_worker(self) -> None:
        try:
            report = D.scan_all(self.runtime)
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self._scan_failed, str(exc))
            return
        self.call_from_thread(self._scan_done, report)

    def _scan_done(self, report: dict[str, Any]) -> None:
        self.busy = False
        if report["bad"]:
            self.info(tr("scan.done", ok=report["ok"], bad=report["bad"]))
        else:
            self.info(tr("scan.done_all_ok"))
        self.refresh_home()

    def _scan_failed(self, message: str) -> None:
        self.busy = False
        self.fail(message)

    def run_fastest(self) -> None:
        if self.busy:
            return
        self.busy = True
        self.info(tr("fastest.running"))
        self._fastest_plan_worker()

    @work(thread=True, exclusive=True, group="fastest")
    def _fastest_plan_worker(self) -> None:
        try:
            plan = D.plan_fastest(self.runtime)
        except Exception as exc:  # noqa: BLE001
            self.call_from_thread(self._fastest_failed, str(exc))
            return
        self.call_from_thread(self._fastest_planned, plan)

    def _fastest_planned(self, plan: D.SwitchPlan) -> None:
        self.busy = False
        if not plan.changes:
            detail = (
                tr("fastest.skipped_locked", names="、".join(plan.skipped_locked))
                if plan.skipped_locked
                else ""
            )
            self.info(tr("fastest.none"), detail)
            return
        listing = "\n".join(
            f"  • {D.display_name(self.runtime, manager)} → {name}（{_ms(ms)}）"
            for manager, name, _url, ms in plan.changes
        )
        body = tr("fastest.confirm_body", count=plan.count, changes=listing)
        if plan.skipped_locked:
            body += "\n\n" + tr("fastest.skipped_locked", names="、".join(plan.skipped_locked))

        def confirmed(yes: bool | None) -> None:
            if yes:
                self._apply_plan(plan)

        self.push_screen(
            ConfirmDialog(tr("fastest.confirm_title"), body, ok=tr("fastest.confirm_ok")), confirmed
        )

    def _apply_plan(self, plan: D.SwitchPlan) -> None:
        def run() -> None:
            outcome = D.apply_fastest(self.runtime, plan)
            severity = "information" if outcome.ok else "warning"
            self.info(outcome.message, outcome.detail, severity=severity)
            self.refresh_home()

        self.require_admin([manager for manager, *_ in plan.changes], run)

    def _fastest_failed(self, message: str) -> None:
        self.busy = False
        self.fail(message)


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------
def run(argv: Sequence[str] = (), **kwargs: Any) -> int:
    """Start the menu UI (``srcmgr`` / ``srcmgr --tui``)."""
    import sys

    extra = [a for a in argv if a != "--tui"]
    if extra and extra[0] in {"-h", "--help"}:
        print(
            f"{APP_NAME} {__version__} - 图形化的终端界面（不需要输入命令）\n\n"
            "  srcmgr                打开菜单界面\n"
            "  srcmgr --tui          同上（显式）\n"
            "  srcmgr --shell        命令行式交互界面（进阶）\n"
            "  srcmgr --cli <命令>   执行一条命令后退出\n"
            "  srcmgr --gui          图形界面（尚未实现）\n"
        )
        return 0
    if extra:
        from ..cli.output import Output, resolve_color

        Output(color=resolve_color("auto")).error(
            f"菜单界面不接受参数：{' '.join(extra)}",
            hint="直接运行 srcmgr，或在界面里选择；命令行请用 srcmgr --cli …",
        )
        return 2
    if not sys.stdout.isatty() or not sys.stdin.isatty():
        from ..cli.output import Output, resolve_color

        Output(color=resolve_color("auto")).error(
            "菜单界面需要一个真正的终端窗口",
            hint="需要执行命令时请用：srcmgr --cli managers",
        )
        return 2
    app = SourceManagerApp(**kwargs)
    try:
        app.run()
    finally:
        app.sudo.close()
    return 0


__all__ = [
    "ConfirmDialog",
    "EcosystemScreen",
    "HomeScreen",
    "NoticeDialog",
    "PasswordDialog",
    "SourceManagerApp",
    "TableScreen",
    "TextDialog",
    "mark_onboarding_seen",
    "run",
]
