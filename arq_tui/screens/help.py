"""Help screen — single-page summary of every key binding.

Available everywhere via ``?``. Closes with ``Esc`` or ``q``.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Footer, Static


HELP_TEXT = """\
Global
  ?         Show this help
  q         Quit the app
  Esc       Back / close current screen
  t         Toggle dark / light theme

Backup Plans (home)
  n         Add Backup Plan (wizard)
  e         Edit Backup Plan (focused)
  r         Back Up Now (focused plan)
  b         Storage Locations (browse + restore)
  v         Validate
  a         Activity Log
  s         Scheduling

Plan wizard
  Tab/Shift+Tab    Move between fields
  (per step)       Back / Next / Save buttons

Storage Locations
  a         Add Storage Location
  v         Validate the open location
  m         Maintenance (password rotation / retention)
  Enter     Open the selected location / backup record

Backup record browser
  Space     Mark / unmark the focused entry for restore
  R         Restore the full record
  r         Restore the marked paths

Restore / Backup / Validate runs
  Esc       Cancel (during run) / Back (when finished)

Notes
  • When Arq.app is installed, its backup plans, storage
    locations, and activity log are mirrored read-only and
    badged "◆ Arq". Edit those in Arq.app; a mirrored local /
    SFTP plan can still be backed up from here.
  • SFTP storage locations require a host the local OpenSSH
    client can already reach; the TUI uses ssh-key or password
    auth via the standard OpenSSH master pattern.
"""


class HelpPanel(VerticalScroll):
    """Help as a right-hand content panel (sidebar → Help). Same text
    as the ``?`` overlay, scrollable in place."""

    DEFAULT_CSS = """
    HelpPanel {
        padding: 1 2;
        height: 1fr;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static(HELP_TEXT)


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
        /* Translucent scrim so the screen behind stays visible. */
        background: $background 55%;
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
