"""Help screen — single-page summary of every key binding.

Available everywhere via ``?``. Closes with ``Esc`` or ``q``.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Footer, Static


HELP_TEXT = """\
Global
  ?         Show this help
  q         Quit the app
  Esc       Back / close current screen
  t         Toggle dark / light theme

Home
  n         New plan (wizard)
  r         Run focused plan
  b         Browse backup sets
  v         Validate (open a destination first)

Plan wizard
  Tab/Shift+Tab    Move between fields
  (per step)       Back / Next / Save buttons

Backup sets
  a         Add destination
  v         Validate the open destination
  Enter     Open the selected destination / record

Record browser
  Space     Mark / unmark the focused entry for restore
  R         Restore the full record
  r         Restore the marked paths

Restore / Backup / Validate runs
  Esc       Cancel (during run) / Back (when finished)

Notes
  • Plan editing is deferred to a later release; create a new
    plan and remove the old plan file from
    $XDG_CONFIG_HOME/arq-backup-tui/plans/.
  • SFTP destinations require a host that the local OpenSSH
    client can already reach; the TUI uses ssh-key or password
    auth via the standard OpenSSH master pattern.
"""


class HelpScreen(ModalScreen):
    """Static help overlay."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Close", show=True),
        Binding("q", "app.pop_screen", "Close", show=True),
        Binding("?", "app.pop_screen", "Close", show=False),
    ]

    DEFAULT_CSS = """
    HelpScreen {
        align: center middle;
    }
    HelpScreen > Vertical {
        width: 80;
        max-width: 95%;
        max-height: 90%;
        background: $surface;
        border: round $primary;
        padding: 1 2;
    }
    HelpScreen #title {
        text-style: bold;
        margin-bottom: 1;
    }
    HelpScreen #body {
        height: auto;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("Keyboard reference", id="title")
            yield Static(HELP_TEXT, id="body")
        yield Footer()
