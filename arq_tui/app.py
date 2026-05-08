"""The top-level Textual application.

The app holds two pieces of long-lived state:

- A :class:`PlanRegistry` populated from
  ``$XDG_CONFIG_HOME/arq-backup-tui/plans/`` (M3 onward).
- A session-scoped credential cache (M2 onward) — passwords held
  in memory only, never written to disk.

For M1 both are stub instances that initialize empty so the Home
screen can render a "No plans yet" state without any real I/O.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from textual.app import App
from textual.binding import Binding

from .screens.home import HomeScreen
from .state import PlanRegistry


class ArqTuiApp(App):
    """Top-level app. Pushes :class:`HomeScreen` on launch."""

    CSS_PATH = "theming.css"
    TITLE = "arq-backup-tui"
    SUB_TITLE = "Independent Arq 7 backup tool"

    BINDINGS = [
        Binding("q", "quit", "Quit", show=True),
        Binding("ctrl+c", "quit", "Quit", show=False),
    ]

    def __init__(
        self, *,
        config_dir: Optional[Path] = None,
        plan_registry: Optional[PlanRegistry] = None,
    ) -> None:
        """``config_dir`` is overridable for tests so they can point
        at a temp directory rather than the user's real
        ``~/.config/arq-backup-tui``."""
        super().__init__()
        if plan_registry is not None:
            self.plan_registry = plan_registry
        else:
            self.plan_registry = PlanRegistry(config_dir=config_dir)

    def on_mount(self) -> None:
        self.push_screen(HomeScreen())


def run_app(config_dir: Optional[Path] = None) -> int:
    """Launch the app in the controlling terminal. Returns 0 on a
    clean exit; non-zero codes are reserved for future startup
    failures (e.g. the config dir is unwritable)."""
    app = ArqTuiApp(config_dir=config_dir)
    app.run()
    return 0
