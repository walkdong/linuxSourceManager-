"""Visual design system for the menu UI.

One accent colour, a neutral grey ramp, and exactly four low-saturation status colours.
Everything the UI draws comes from here so the look cannot drift widget by widget.

Design rules (agreed with the product owner, please keep them):

1. **No emoji.** Status and navigation use geometric glyphs (``● ▸ ▎ › ✓``), which
   render in every terminal font and do not look like a landing page.
2. **One accent hue.** Selection, focus and the primary action all use it; nothing else
   is coloured. Two hues competing is what makes an interface look generated.
3. **Four status colours, low saturation.** Green = everything fine, amber = some *extra*
   sources are dead but downloads still work, red = the source actually in use is dead,
   grey = not measured. The ramp is tinted toward the accent so the palette reads as one
   family instead of a traffic light.
4. **Colour never carries meaning alone** - every status also has words.
5. Dark by default (terminals are usually dark), light available; both are defined here.
"""

from __future__ import annotations

from dataclasses import dataclass

from textual.theme import Theme

#: Accent presets.  ``cyan`` is the calm default; ``amber`` is warmer; ``indigo`` is the
#: most "office software" of the three.
ACCENTS: dict[str, tuple[str, str, str]] = {
    # name: (dark accent, light accent, hover/darker variant)
    "cyan": ("#6fb3c9", "#2f6f86", "#8ccbe0"),
    "amber": ("#c9a163", "#9a7b3f", "#dcb87d"),
    "indigo": ("#8ba4d9", "#4a5f9e", "#a8bce8"),
}

#: Neutrals: ``(background, surface, panel, border, text, muted)`` for dark and light.
NEUTRALS: dict[str, tuple[str, ...]] = {
    "dark": ("#15181b", "#1b1f23", "#202529", "#2b3137", "#d7dce0", "#8b949c"),
    "light": ("#f7f8f9", "#ffffff", "#f1f3f5", "#d9dee3", "#25292e", "#6b7480"),
}

#: Status colours per mode: ``(ok, warn, bad, unknown)`` - deliberately desaturated.
STATUS: dict[str, tuple[str, str, str, str]] = {
    "dark": ("#7fb08a", "#d3b06a", "#cc7f7f", "#6b7480"),
    "light": ("#4f8f61", "#8f7133", "#a85f5f", "#8b949c"),
}

#: Text-only glyphs, safe in a monospace font.  The status marks share one shape on
#: purpose: colour is the only difference, and the words next to them carry the meaning.
GLYPHS = {
    "ok": "●",
    "warn": "●",
    "slow": "◐",
    "bad": "●",
    "unknown": "○",
    "locked": "▣",
    "unlocked": "▢",
    "current": "▸",
    "enter": "›",
    "action": "▎",
    "check": "✓",
    "cross": "✗",
    "bullet": "·",
}

#: Rich markup tag for the accent colour (used by the status bar and dialogs).
STYLE_ACCENT = "ansicyan"
STYLE_MUTED = "ansibrightblack"


@dataclass(frozen=True)
class Look:
    """A complete look: mode + accent, resolved into concrete colours."""

    mode: str = "dark"
    accent: str = "cyan"

    @property
    def dark(self) -> bool:
        return self.mode == "dark"

    @property
    def theme_name(self) -> str:
        return f"srcmgr-{self.mode}-{self.accent}"

    def theme(self) -> Theme:
        background, surface, panel, border, text, muted = NEUTRALS[self.mode]
        accent_main, _light, accent_hover = ACCENTS[self.accent]
        ok, warn, bad, unknown = STATUS[self.mode]
        return Theme(
            name=self.theme_name,
            primary=accent_main,
            # NOTE: the framework's own variables are the only ones usable in CSS.  The
            # ``srcmgr-`` prefixed extras below are for Python-side lookups only; a
            # stylesheet that references them fails to parse and is dropped whole.
            secondary=muted,
            accent=accent_main,
            success=ok,
            warning=warn,
            error=bad,
            foreground=text,
            background=background,
            surface=surface,
            panel=panel,
            dark=self.dark,
            # NOTE: only the framework's own variables are resolvable in CSS.  Custom
            # entries here are for Python-side lookups (status marks, previews); the
            # stylesheet uses $text-muted / $foreground / $warning / $success / $error.
            variables={
                "srcmgr-border": border,
                "srcmgr-muted": muted,
                "srcmgr-status-ok": ok,
                "srcmgr-status-warn": warn,
                "srcmgr-status-bad": bad,
                "srcmgr-status-unknown": unknown,
                "srcmgr-accent-hover": accent_hover,
            },
        )

    # -- helpers used by the widgets ----------------------------------------
    def status_colour(self, health: str) -> str:
        ok, warn, bad, unknown = STATUS[self.mode]
        return {
            "ok": ok,
            "slow": warn,
            "warn": warn,
            "bad": bad,
            "unknown": unknown,
        }.get(health, unknown)


#: Stylesheet for the menu UI.
#:
#: **Do not put this in a class attribute called ``CSS``.**  Textual's ``watch_theme``
#: parses a class-level ``CSS`` the moment ``self.theme`` is assigned - which happens
#: before the theme's variables exist - and a single unresolvable variable ($muted, say)
#: makes it throw the entire stylesheet away, leaving every widget at zero size.  Provide
#: it from ``App.get_css_variables``/``App.CSS`` *after* the theme is set, or from
#: ``App.get_default_css`` style hooks.  :func:`stylesheet` returns it for that purpose.
STYLESHEET = """
Screen { background: $background; color: $foreground; }

Header { background: $panel; color: $foreground; }
Header .header--title { text-style: bold; }
Footer { background: $panel; color: $text-muted; }
Footer .footer--key { background: $panel; color: $accent; text-style: bold; }
Footer .footer--description { background: $panel; color: $text-muted; }

/* --- status bar ------------------------------------------------------- */
StatusBar {
    /* dock: top takes the bar out of the 1fr competition with the content area; with
       height:auto alone the content grabbed all the space and the bar collapsed to 0. */
    dock: top;
    height: 2;
    layout: vertical;
    background: $panel;
    padding: 0 1;
}
StatusBar #bar-top { height: 1; width: 1fr; }
StatusBar #bar-title { text-style: bold; width: 1fr; }
StatusBar #bar-language { width: auto; color: $text-muted; padding: 0 1; background: $panel; }
StatusBar #bar-language:hover { color: $accent; text-style: bold; }
StatusBar #bar-language:focus { color: $accent; text-style: bold; }
StatusBar #bar-status { height: 1; color: $text-muted; }

/* --- page frame ------------------------------------------------------- */
#home, #eco, #table-screen { padding: 1 2; }

.page-title { text-style: bold; padding-bottom: 0; }
.page-sub { color: $text-muted; padding-bottom: 1; }
.rule { color: $foreground 20%; height: 1; }

/* --- the list --------------------------------------------------------- */
DataTable {
    background: $surface;
    border: round $foreground 20%;
    padding: 0 1;
    height: 1fr;
}
DataTable > .datatable--header { background: $surface; color: $text-muted; text-style: bold; }
DataTable > .datatable--cursor { background: $accent 25%; color: $foreground; }
DataTable > .datatable--hover { background: $panel; }
DataTable > .datatable--odd-row { background: $surface; }

/* --- actions: plain words, one emphasised ----------------------------- */
#actions, #eco-actions { height: auto; padding-top: 1; }
#actions Button, #eco-actions Button {
    background: transparent;
    border: none;
    color: $text-muted;
    padding: 0 2 0 0;
    min-width: 0;
    height: 1;
}
#actions Button:hover, #eco-actions Button:hover { color: $accent; text-style: bold; }
#actions Button.-primary, #eco-actions Button.-primary { color: $accent; text-style: bold; }
#actions Button:focus, #eco-actions Button:focus { color: $foreground; text-style: bold; }

/* --- dialogs ---------------------------------------------------------- */
.dialog {
    width: 72; height: auto; padding: 1 2;
    border: round $accent; background: $panel; margin: 4 8;
}
.dialog-title { text-style: bold; padding-bottom: 1; }
.dialog-body { color: $foreground; padding-bottom: 1; }
.dialog-error { color: $error; padding-bottom: 1; }
.dialog-hint { color: $text-muted; }
.dialog-buttons { height: auto; align-horizontal: right; padding-top: 1; }
.dialog-buttons Button { margin-left: 2; }
#confirm, #ok { color: $accent; text-style: bold; }
#cancel { color: $text-muted; }

/* --- ecosystem screen ------------------------------------------------- */
#eco-head { height: auto; }
#eco-title { text-style: bold; padding: 0 1 0 0; width: 1fr; }
#eco-lock { color: $text-muted; width: auto; }
#eco-lock.locked { color: $warning; }
#eco-current { color: $foreground; padding: 1 0 0 0; }
#eco-current-label { color: $text-muted; }
#eco-question { color: $text-muted; padding-bottom: 1; }
#eco-testing { color: $accent; }
#mirrors { background: $surface; border: round $foreground 20%; height: 1fr; padding: 0 1; }
#mirrors ListItem { background: transparent; padding: 0 1; }
#mirrors ListItem.--highlight { background: $accent 25%; color: $foreground; }
#mirrors ListItem.current { color: $success; }
#eco-hint, #hint { color: $text-muted; padding-top: 1; }

/* --- tables (history / backups) --------------------------------------- */
#table-head { height: auto; }
#table-title { text-style: bold; padding: 0 1 0 0; width: 1fr; }
#rows { background: $surface; border: round $foreground 20%; height: 1fr; }
#table-empty { color: $text-muted; padding: 1 0; }

/* --- language switch -------------------------------------------------- */
#language { width: auto; color: $text-muted; padding: 0 1; }
#language .on { color: $accent; text-style: bold; }
"""


def register(app, look: Look | None = None) -> Look:  # noqa: ANN001 - Textual App
    """Register every theme on ``app``, activate ``look`` and install the stylesheet.

    Must be called *before* anything else touches ``app.theme``: assigning an
    unregistered theme is silently ignored, and assigning one before the variables exist
    makes Textual discard the stylesheet (see :data:`STYLESHEET`).
    """
    for mode in ("dark", "light"):
        for accent in ACCENTS:
            app.register_theme(Look(mode=mode, accent=accent).theme())
    chosen = look or Look()
    app.theme = chosen.theme_name
    # Install after activation so the variables are resolvable.
    app.stylesheet.add_source(
        STYLESHEET, read_from=("srcmgr/tui/theme.py", "STYLESHEET"), is_default_css=False
    )
    app.stylesheet.parse()
    return chosen


__all__ = [
    "ACCENTS",
    "GLYPHS",
    "Look",
    "NEUTRALS",
    "STATUS",
    "STYLESHEET",
    "register",
    "stylesheet",
]


def stylesheet() -> str:
    """The menu stylesheet (kept as a function for callers that want it lazily)."""
    return STYLESHEET
