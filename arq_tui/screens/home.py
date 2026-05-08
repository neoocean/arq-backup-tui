"""Home screen — the app's landing surface.

For M1 this is a working dashboard with three sections:

- **Plans**: lists known plans (empty on a fresh install). M3 will
  add a "New plan" path; for now we render a hint pointing at the
  not-yet-implemented action.
- **Quick actions**: Browse backup sets / Validate / Quit. The
  first two are hooked up to placeholder screens in later
  milestones; for M1 they just show a "Coming soon" notification.
- **Status bar** with key bindings.

Anything more (real plan launch, real browse / validate flows) is
deferred to its respective milestone.
"""

from __future__ import annotations

from typing import List

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import (
    Button,
    Footer,
    Header,
    ListItem,
    ListView,
    Static,
)

from ..state import Plan


class HomeScreen(Screen):
    """The app's landing screen."""

    BINDINGS = [
        Binding("n", "new_plan", "New plan", show=True),
        Binding("b", "browse", "Browse backup sets", show=True),
        Binding("v", "validate", "Validate", show=True),
        Binding("q", "app.quit", "Quit", show=True),
    ]

    DEFAULT_CSS = """
    HomeScreen {
        layout: vertical;
    }
    HomeScreen #plans-section {
        height: auto;
        padding: 1 2;
        border: round $primary;
        margin: 1 1 0 1;
    }
    HomeScreen #actions-section {
        height: auto;
        padding: 1 2;
        border: round $primary;
        margin: 1 1 0 1;
    }
    HomeScreen .section-title {
        text-style: bold;
        margin-bottom: 1;
    }
    HomeScreen .empty-hint {
        color: $text-muted;
        text-style: italic;
    }
    HomeScreen ListView {
        height: auto;
        max-height: 12;
    }
    HomeScreen .action-row {
        height: 3;
        align: left middle;
    }
    HomeScreen Button {
        margin: 0 1 0 0;
    }
    """

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)

        plans = self._load_plans()
        with Vertical(id="plans-section"):
            yield Static("Plans", classes="section-title")
            if plans:
                yield ListView(
                    *[
                        ListItem(Static(self._render_plan_row(p)))
                        for p in plans
                    ],
                    id="plans-list",
                )
            else:
                yield Static(
                    "No plans yet — press [n] to create one.",
                    classes="empty-hint",
                    id="plans-empty",
                )

        with Vertical(id="actions-section"):
            yield Static("Quick actions", classes="section-title")
            with Horizontal(classes="action-row"):
                yield Button("New plan [n]", id="action-new", variant="primary")
                yield Button("Browse [b]", id="action-browse")
                yield Button("Validate [v]", id="action-validate")
                yield Button("Quit [q]", id="action-quit", variant="error")

        yield Footer()

    def _load_plans(self) -> List[Plan]:
        return self.app.plan_registry.list_plans()

    @staticmethod
    def _render_plan_row(p: Plan) -> str:
        suffix = (
            f"   last run: {p.last_run_iso}" if p.last_run_iso
            else "   never run"
        )
        sources_summary = (
            f"{len(p.sources)} source{'s' if len(p.sources) != 1 else ''}"
        )
        return f"{p.name} ({sources_summary}){suffix}"

    # ------------------------------------------------------------------
    # Action handlers
    # ------------------------------------------------------------------

    def action_new_plan(self) -> None:
        self.notify(
            "Plan wizard ships in M3 — coming soon.",
            severity="information",
        )

    def action_browse(self) -> None:
        self.notify(
            "Backup-set browser ships in M2 — coming soon.",
            severity="information",
        )

    def action_validate(self) -> None:
        self.notify(
            "Validation runner ships in M5 — coming soon.",
            severity="information",
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "action-new":
            self.action_new_plan()
        elif event.button.id == "action-browse":
            self.action_browse()
        elif event.button.id == "action-validate":
            self.action_validate()
        elif event.button.id == "action-quit":
            self.app.exit()
