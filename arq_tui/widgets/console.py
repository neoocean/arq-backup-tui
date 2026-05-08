"""Quake-style command console overlay.

Slides down from the top covering the upper half of the viewport
when the operator presses ``:``, `` ` ``, or ``₩`` (the latter is
the Won-sign that macOS Korean-IME emits when the operator hits
the backtick key with Korean input mode active).

Inside the console:

- An ``Input`` row at the bottom takes one slash-command per line.
  Leading ``:`` / ``/`` are tolerated so ``:help``, ``/help`` and
  ``help`` all resolve identically.
- A ``RichLog`` above the input streams command output. Both the
  echo line (``› <command>``) and the dispatched action's text
  result land here so the operator sees their last few invocations
  without leaving the console.
- ``Up`` / ``Down`` arrows walk the in-memory command history (most
  recent at the bottom). ``Esc`` closes the console and returns
  focus to whatever screen is below.

The console is mounted **once** at the App level by
:meth:`arq_tui.app.ArqTuiApp.on_mount`, lives on a CSS overlay
layer, and is hidden via ``display: none`` until opened. That
keeps it cheap when not in use and lets a single instance carry
history across screen pushes/pops.
"""

from __future__ import annotations

from typing import Awaitable, Callable, List, Optional

from textual import events
from textual.containers import Container
from textual.reactive import reactive
from textual.widgets import Input, RichLog


# Public type alias for the dispatch coroutine the App injects into
# the console. The console hands every submitted line to the
# dispatcher and prints whatever string it returns into the log.
CommandDispatch = Callable[[str], Awaitable[str]]


class CommandConsole(Container):
    """Slide-down overlay that takes one slash-command at a time."""

    DEFAULT_CSS = """
    CommandConsole {
        layer: overlay;
        dock: bottom;
        height: 50%;
        width: 100%;
        background: $panel;
        border-top: heavy $primary;
        display: none;
        layout: vertical;
    }
    CommandConsole.-open {
        display: block;
    }
    CommandConsole #console-log {
        height: 1fr;
        background: $surface;
        padding: 0 1;
        scrollbar-size: 1 1;
    }
    CommandConsole #console-input {
        dock: bottom;
        height: 3;
        background: $surface;
        border-top: solid $primary;
    }
    """

    is_open: reactive[bool] = reactive(False)

    def __init__(self) -> None:
        super().__init__()
        # Most-recent at the end. Kept process-lifetime; never
        # persisted to disk so the operator's last-typed command
        # doesn't leak between sessions.
        self.history: List[str] = []
        # Cursor while navigating with Up/Down. ``len(history)`` is
        # the synthetic "after-end" slot that holds whatever the
        # operator was mid-typing before they pressed Up.
        self._history_idx: int = 0
        self._draft: str = ""
        # The dispatcher is set by App.on_mount via attach_dispatch.
        # Until it lands, submitted lines just print "no dispatcher".
        self._dispatch: Optional[CommandDispatch] = None

    def compose(self):  # type: ignore[override]
        yield RichLog(
            id="console-log",
            highlight=False, wrap=True, max_lines=2000,
        )
        yield Input(
            id="console-input",
            placeholder="type a command (try :help) — Esc to close",
        )

    def on_mount(self) -> None:
        log = self.query_one("#console-log", RichLog)
        log.write(
            "[dim]arq-tui console — type [b]:help[/] to list commands, "
            "Esc to close, ↑/↓ for history.[/]"
        )

    # ------------------------------------------------------------------
    # Wiring
    # ------------------------------------------------------------------

    def attach_dispatch(self, dispatch: CommandDispatch) -> None:
        """Called by the App once on mount. The dispatch coroutine
        receives the **stripped** command line (no leading ``:``)
        and returns a string to write back to the console log."""
        self._dispatch = dispatch

    def write_log(self, line: str) -> None:
        """Public hook the dispatcher uses to interleave extra
        output between the user's commands (e.g. for multi-line
        listings)."""
        self.query_one("#console-log", RichLog).write(line)

    # ------------------------------------------------------------------
    # Open / close
    # ------------------------------------------------------------------

    def watch_is_open(self, value: bool) -> None:
        self.set_class(value, "-open")
        if value:
            # Defer the focus + clear by one refresh tick: Textual
            # won't focus a widget that's still ``display: none``,
            # so we have to wait for the class flip to land before
            # asking the Input to take focus.
            self.call_after_refresh(self._after_open)

    def _after_open(self) -> None:
        inp = self.query_one("#console-input", Input)
        inp.value = ""
        inp.focus()

    def open(self) -> None:
        self.is_open = True

    def close(self) -> None:
        self.is_open = False

    # ------------------------------------------------------------------
    # Input handling
    # ------------------------------------------------------------------

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        line = event.value.strip()
        if not line:
            return
        if not self.history or self.history[-1] != line:
            self.history.append(line)
        self._history_idx = len(self.history)
        self._draft = ""
        self.write_log(f"[bold cyan]›[/] {line}")
        # Strip leading prefix so the dispatcher always sees a bare
        # command. ``:`` / ``/`` / ``\\`` (Won's ASCII fallback) are
        # all tolerated.
        cmd_line = line.lstrip(":/\\")
        try:
            if self._dispatch is None:
                self.write_log("[red]no dispatcher attached[/]")
            else:
                result = await self._dispatch(cmd_line)
                if result:
                    self.write_log(result)
        except Exception as exc:  # noqa: BLE001 - operator-facing
            self.write_log(
                f"[bold red]error:[/] {type(exc).__name__}: {exc}"
            )
        event.input.value = ""

    def on_key(self, event: events.Key) -> None:
        # Up / Down history walk only fires while the console's own
        # Input has focus; lets nested screens use the same keys.
        if not self.is_open:
            return
        try:
            inp = self.query_one("#console-input", Input)
        except Exception:
            return
        if not inp.has_focus:
            return
        if event.key == "up":
            self._history_up(inp)
            event.stop()
        elif event.key == "down":
            self._history_down(inp)
            event.stop()
        elif event.key == "escape":
            self.close()
            event.stop()

    def _history_up(self, inp: Input) -> None:
        if not self.history:
            return
        # First Up press from the live edit slot stashes the draft
        # so Down can restore whatever the user was mid-typing.
        if self._history_idx == len(self.history):
            self._draft = inp.value
        if self._history_idx > 0:
            self._history_idx -= 1
            inp.value = self.history[self._history_idx]
            inp.cursor_position = len(inp.value)

    def _history_down(self, inp: Input) -> None:
        if self._history_idx < len(self.history):
            self._history_idx += 1
            if self._history_idx == len(self.history):
                inp.value = self._draft
            else:
                inp.value = self.history[self._history_idx]
            inp.cursor_position = len(inp.value)
