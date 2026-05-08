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
        Binding("e", "edit_focused", "Edit focused plan", show=True),
        Binding("r", "run_focused", "Run focused plan", show=True),
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
        from .plan_wizard import PlanWizardScreen
        self.app.push_screen(PlanWizardScreen(), self._after_wizard)

    def _after_wizard(self, _result) -> None:
        # The wizard saves itself + pops; we just need to refresh
        # the plans list so the new entry shows up.
        self._refresh_plans()

    def action_browse(self) -> None:
        # Local import keeps the M1 home-only test from pulling in
        # the M2 record-browser deps until they're actually used.
        from .backup_sets import BackupSetListScreen
        self.app.push_screen(BackupSetListScreen())

    def action_validate(self) -> None:
        self.notify(
            "Validation runner ships in M5 — coming soon.",
            severity="information",
        )

    def action_run_focused(self) -> None:
        plan = self._focused_plan()
        if plan is None:
            return
        self._run_plan(plan)

    def action_edit_focused(self) -> None:
        plan = self._focused_plan()
        if plan is None:
            return
        from .plan_wizard import PlanWizardScreen
        self.app.push_screen(
            PlanWizardScreen(plan=plan), self._after_wizard,
        )

    def _focused_plan(self):
        """Resolve the currently-focused plan, or surface a hint
        if there are none. Used by both [r] and [e]."""
        plans = self._load_plans()
        if not plans:
            self.notify(
                "No plans yet — press [n] to create one.",
                severity="warning",
            )
            return None
        idx = 0
        try:
            list_view = self.query_one("#plans-list")
            if getattr(list_view, "index", None) is not None:
                idx = int(list_view.index)
        except Exception:
            pass
        return plans[max(0, min(idx, len(plans) - 1))]

    def _refresh_plans(self) -> None:
        # Refresh by recomposing the plans-section from scratch:
        # easier than mutating ListView in place when the empty
        # state may need to flip on/off.
        self.app.pop_screen()
        self.app.push_screen(HomeScreen())

    def _run_plan(self, plan) -> None:
        # Resolve / prompt for the encryption password, then push
        # the BackupRunScreen.
        from ..state import Destination
        from ..widgets.password_modal import PasswordModal
        from .backup_run import BackupRunScreen

        if plan.destination_kind == "local":
            dest = Destination(
                kind="local", label=plan.name,
                path=str(plan.destination.get("path") or ""),
            )
        else:
            d = plan.destination
            dest = Destination(
                kind="sftp", label=plan.name,
                host=str(d.get("host") or ""),
                port=int(d.get("port") or 22),
                user=str(d.get("user") or ""),
                path=str(d.get("path") or ""),
                identity_file=str(d.get("identity_file") or ""),
            )
        cached_pw = self.app.credential_cache.get_encryption_password(dest)
        if cached_pw is not None:
            self.app.push_screen(BackupRunScreen(plan=plan, password=cached_pw))
            return

        def _with_pw(pw):
            if not pw:
                return
            self.app.credential_cache.set_encryption_password(dest, pw)
            self.app.push_screen(BackupRunScreen(plan=plan, password=pw))
        self.app.push_screen(
            PasswordModal(
                prompt=f"Encryption password for {plan.name}",
            ),
            _with_pw,
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
