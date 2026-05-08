"""The top-level Textual application.

The app holds two pieces of long-lived state:

- A :class:`PlanRegistry` populated from
  ``$XDG_CONFIG_HOME/arq-backup-tui/plans/`` (M3 onward).
- A session-scoped credential cache (M2 onward) — passwords held
  in memory only, never written to disk.

A single :class:`~arq_tui.widgets.console.CommandConsole` is
mounted on the App's overlay layer and stays loaded for the
session; opening it is just a class toggle, so screen pushes /
pops don't affect history or the visible log.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from textual import events
from textual.app import App
from textual.binding import Binding
from textual.widgets import Input, TextArea

from .screens.home import HomeScreen
from .state import CredentialCache, DestinationStore, PlanRegistry
from .widgets.console import CommandConsole


# Characters that should drop the operator into the command
# console. ``:`` is the conventional editor binding; `` ` `` is
# the quake-style chord most operators reach for; ``₩`` is what
# the macOS Korean IME emits when the operator hits the same
# physical key with Korean input mode active. All three open the
# same overlay.
_CONSOLE_OPEN_CHARS = (":", "`", "₩")


class ArqTuiApp(App):
    """Top-level app. Pushes :class:`HomeScreen` on launch."""

    CSS_PATH = "theming.css"
    TITLE = "arq-backup-tui"
    SUB_TITLE = "Independent Arq 7 backup tool"
    # Reserve the overlay layer for the slide-down console so it
    # paints above every screen / modal without the screen having
    # to know it exists.
    CSS = """
    Screen {
        layers: base overlay;
    }
    """

    BINDINGS = [
        Binding("question_mark", "help", "Help", show=True, key_display="?"),
        Binding("t", "toggle_theme", "Theme", show=True),
        Binding("q", "quit", "Quit", show=True),
        Binding("ctrl+c", "quit", "Quit", show=False),
    ]

    def __init__(
        self, *,
        config_dir: Optional[Path] = None,
        plan_registry: Optional[PlanRegistry] = None,
        destination_store: Optional[DestinationStore] = None,
        credential_cache: Optional[CredentialCache] = None,
    ) -> None:
        """``config_dir`` is overridable for tests so they can point
        at a temp directory rather than the user's real
        ``~/.config/arq-backup-tui``."""
        super().__init__()
        self.plan_registry = (
            plan_registry
            if plan_registry is not None
            else PlanRegistry(config_dir=config_dir)
        )
        self.destination_store = (
            destination_store
            if destination_store is not None
            else DestinationStore(config_dir=config_dir)
        )
        self.credential_cache = (
            credential_cache
            if credential_cache is not None
            else CredentialCache()
        )
        self.console_widget: Optional[CommandConsole] = None

    async def on_mount(self) -> None:
        # Mount the console first so the overlay layer is reserved
        # before HomeScreen lays out its plans-section. The console
        # is hidden via display:none until the operator opens it.
        console = CommandConsole()
        self.console_widget = console
        await self.mount(console)
        from .console_commands import dispatch_command
        console.attach_dispatch(
            lambda line: dispatch_command(self, line),
        )
        self.push_screen(HomeScreen())

    # ------------------------------------------------------------------
    # Console open triggers
    # ------------------------------------------------------------------

    def on_key(self, event: events.Key) -> None:
        """Open / close the command console.

        Open triggers fire on ``:`` / `` ` `` / ``₩`` — the third
        is the Won-sign that macOS Korean IME emits when the
        operator hits the backtick key with Korean input mode
        active. Close fires on ``escape`` while the console is
        open, regardless of which descendant currently holds
        focus (Textual won't refocus the Input until the next
        frame after a display-class flip, so the screen
        underneath may briefly own focus when ``escape`` lands).

        While the console is closed, open triggers are ignored
        when focus is on a normal ``Input`` / ``TextArea`` so the
        operator can type the literal character into a wizard
        field without the overlay barging in.
        """
        if self.console_widget is None:
            return
        if self.console_widget.is_open:
            if event.key == "escape":
                self.console_widget.close()
                event.stop()
                event.prevent_default()
            return
        if isinstance(self.focused, (Input, TextArea)):
            return
        if event.character in _CONSOLE_OPEN_CHARS:
            self.console_widget.open()
            event.stop()
            event.prevent_default()

    def action_help(self) -> None:
        from .screens.help import HelpScreen
        # Avoid opening multiple help screens on top of each other.
        if not isinstance(self.screen, HelpScreen):
            self.push_screen(HelpScreen())

    def action_toggle_theme(self) -> None:
        # Textual exposes themes by name in 8.x; flipping between
        # the two built-ins is the cheapest "dark / light" toggle
        # we can offer without shipping our own palette.
        try:
            current = getattr(self, "theme", "textual-dark")
            self.theme = (
                "textual-light"
                if current.endswith("dark")
                else "textual-dark"
            )
        except Exception:
            pass


def run_app(config_dir: Optional[Path] = None) -> int:
    """Launch the app in the controlling terminal. Returns 0 on a
    clean exit; non-zero codes are reserved for future startup
    failures (e.g. the config dir is unwritable)."""
    app = ArqTuiApp(config_dir=config_dir)
    app.run()
    return 0
