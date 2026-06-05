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

from .arq_app import ArqAppSource, detect_arq_app
from .screens.home import HomeScreen
from .state import CredentialCache, DestinationStore, PlanRegistry
from .widgets.console import CommandConsole

# Sentinel for the ``arq_app`` constructor argument: distinguishes
# "caller said nothing, auto-detect" from "caller explicitly passed
# None to disable the Arq.app mirror".
_AUTODETECT = object()


# Characters that should drop the operator into the command
# console. ``:`` is the conventional editor binding; `` ` `` is
# the quake-style chord most operators reach for; ``₩`` is what
# the macOS Korean IME emits when the operator hits the same
# physical key with Korean input mode active. All three open the
# same overlay.
_CONSOLE_OPEN_CHARS = (":", "`", "₩")


# Korean 2-beolsik (Dubeolsik) layout: the jamo each physical key emits
# when the macOS Korean IME is active. We reverse-map jamo -> Latin so a
# single-key shortcut works no matter the input-method state — pressing
# the physical "n" key sends "ㅜ" in Korean mode, which we translate back
# to "n" and re-dispatch. Both unshifted jamo and the shifted doubles
# (ㅃㅉㄸㄲㅆ + ㅒㅖ) are covered.
_HANGUL_TO_LATIN = {
    "ㅂ": "q", "ㅈ": "w", "ㄷ": "e", "ㄱ": "r", "ㅅ": "t",
    "ㅛ": "y", "ㅕ": "u", "ㅑ": "i", "ㅐ": "o", "ㅔ": "p",
    "ㅁ": "a", "ㄴ": "s", "ㅇ": "d", "ㄹ": "f", "ㅎ": "g",
    "ㅗ": "h", "ㅓ": "j", "ㅏ": "k", "ㅣ": "l",
    "ㅋ": "z", "ㅌ": "x", "ㅊ": "c", "ㅍ": "v", "ㅠ": "b",
    "ㅜ": "n", "ㅡ": "m",
    # Shift + key (double consonants / shifted vowels).
    "ㅃ": "Q", "ㅉ": "W", "ㄸ": "E", "ㄲ": "R", "ㅆ": "T",
    "ㅒ": "O", "ㅖ": "P",
}


class ArqTuiApp(App):
    """Top-level app. Pushes :class:`HomeScreen` on launch."""

    CSS_PATH = "theming.css"
    TITLE = "arq-backup-tui"
    SUB_TITLE = "Independent Arq 7 backup tool"
    # Suppress Textual's built-in command palette (Ctrl+P) and any
    # other framework-default command menus — this TUI exposes its own
    # slash-command console + sidebar instead, and the operator asked
    # for the generic palette gone.
    ENABLE_COMMAND_PALETTE = False
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
        arq_app=_AUTODETECT,
    ) -> None:
        """``config_dir`` is overridable for tests so they can point
        at a temp directory rather than the user's real
        ``~/.config/arq-backup-tui``.

        ``arq_app`` is the read-only mirror of a locally-installed
        Arq.app (see :mod:`arq_tui.arq_app`). Left at the
        ``_AUTODETECT`` sentinel it is probed for **only** in a real
        session (``config_dir is None``); tests run with an isolated
        ``config_dir`` and therefore get no mirror unless they inject
        an :class:`ArqAppSource` explicitly — so the operator's real
        Arq plans never leak into test assertions. Pass ``None`` to
        force the mirror off, or an :class:`ArqAppSource` to supply a
        fixture.
        """
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
        if arq_app is _AUTODETECT:
            self.arq_app: Optional[ArqAppSource] = (
                detect_arq_app() if config_dir is None else None
            )
        else:
            self.arq_app = arq_app
        # Where the shell's embedded activity panel reads run state
        # files. A real session (``config_dir is None``) uses the
        # default XDG_STATE location so cron / CLI-written runs show
        # up; a test passes ``config_dir`` and gets an isolated runs
        # dir under it so the suite never reads / mark-stales the
        # operator's real run state.
        self.runs_state_dir: Optional[Path] = (
            Path(config_dir) / "runs" if config_dir is not None else None
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
            # A text field is focused — let the raw character through so
            # the operator can type it (incl. composing Korean text).
            return
        if event.character in _CONSOLE_OPEN_CHARS:
            self.console_widget.open()
            event.stop()
            event.prevent_default()
            return
        # IME independence: if the Korean IME turned a shortcut key into
        # a Hangul jamo, translate it back to the Latin key and
        # re-dispatch so every binding fires regardless of input mode.
        latin = _HANGUL_TO_LATIN.get(event.character or "")
        if latin:
            event.stop()
            event.prevent_default()
            self.simulate_key(latin)

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
