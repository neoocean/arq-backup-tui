"""TUI screen for managing scheduled backup plans.

Operators want to see "which of my plans are wired into cron /
launchd, and how do I add or remove one?" without leaving the
TUI. This screen lists every arq-backup-tui-managed schedule
visible on the host (queries crontab + ~/Library/LaunchAgents
via :func:`arq_tui.scheduling.list_schedules`) plus offers
keyboard actions to install or remove a schedule for a
focused plan.

Bindings:
- ``[i]`` install a cron schedule for the focused plan
- ``[l]`` install a launchd schedule for the focused plan (macOS)
- ``[r]`` remove the focused schedule
- ``[escape]`` back to the previous screen

Doesn't actually invoke the backup itself — that stays cron's
or launchd's job. The screen is a configuration / monitoring
surface only.
"""

from __future__ import annotations

import platform
from typing import Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import (
    Footer, Header, ListItem, ListView, Static,
)

from ..scheduling import (
    install_schedule, list_schedules, remove_schedule,
)


class _ScheduleRow(ListItem):
    """One row in the schedule list. Stores the (plan_id, kind,
    path) tuple so the screen-level handler can look up + act on
    it from the focused row without re-querying."""

    def __init__(self, plan_id: str, kind: str, path) -> None:
        super().__init__()
        self.plan_id = plan_id
        self.kind = kind
        self.path = path
        label = f"{kind:8s}  plan={plan_id}  →  {path}"
        self._label = label

    def compose(self) -> ComposeResult:
        yield Static(self._label, classes="schedule-row")


class SchedulingScreen(Screen):
    """List + manage all arq-backup-tui-managed schedules."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back", show=True),
        Binding("i", "install_cron",
                "Install (cron)", show=True),
        Binding("l", "install_launchd",
                "Install (launchd)", show=True),
        Binding("r", "remove_focused",
                "Remove focused", show=True),
        Binding("g", "refresh", "Refresh", show=True),
    ]

    DEFAULT_CSS = """
    SchedulingScreen #title {
        text-style: bold;
        padding: 1 2;
    }
    SchedulingScreen #empty-hint {
        padding: 0 2;
        color: $text-muted;
    }
    """

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Scheduled backups", id="title")
        yield ListView(id="schedule-list")
        yield Static("", id="empty-hint")
        yield Footer()

    def on_mount(self) -> None:
        self._refresh()

    def action_refresh(self) -> None:
        self._refresh()

    def _refresh(self) -> None:
        lv = self.query_one("#schedule-list", ListView)
        lv.clear()
        rows = list_schedules()
        if not rows:
            self.query_one("#empty-hint", Static).update(
                "No schedules installed. "
                "Press [i] (cron) or [l] (launchd) on a plan "
                "in HomeScreen to add one — schedule text comes "
                "from the plan's `schedule` field."
            )
            return
        self.query_one("#empty-hint", Static).update(
            f"{len(rows)} schedule(s) installed."
        )
        for plan_id, kind, path in rows:
            lv.append(_ScheduleRow(plan_id, kind, path))

    def _focused_plan(self) -> "Optional[str]":
        """Return the plan_id of the currently-focused row, or
        None when the list is empty / nothing's selected. Used
        by the install / remove actions so the operator only
        needs to focus a row + press a key."""
        lv = self.query_one("#schedule-list", ListView)
        idx = lv.index
        if idx is None or idx >= len(lv.children):
            return None
        row = lv.children[idx]
        return getattr(row, "plan_id", None)

    def action_install_cron(self) -> None:
        plan = self._plan_to_install()
        if plan is None:
            return
        try:
            install_schedule(plan, kind="cron")
            self.notify(
                f"Installed cron schedule for {plan.name!r}",
                severity="information",
            )
        except Exception as exc:
            self.notify(
                f"Cron install failed: {exc}",
                severity="error",
            )
        self._refresh()

    def action_install_launchd(self) -> None:
        if platform.system() != "Darwin":
            self.notify(
                "launchd is macOS-only; use [i] for cron.",
                severity="warning",
            )
            return
        plan = self._plan_to_install()
        if plan is None:
            return
        try:
            install_schedule(plan, kind="launchd")
            self.notify(
                f"Installed launchd schedule for {plan.name!r}",
                severity="information",
            )
        except Exception as exc:
            self.notify(
                f"launchd install failed: {exc}",
                severity="error",
            )
        self._refresh()

    def action_remove_focused(self) -> None:
        plan_id = self._focused_plan()
        if plan_id is None:
            self.notify(
                "Focus a schedule row first.",
                severity="warning",
            )
            return
        try:
            n = remove_schedule(plan_id)
            self.notify(
                f"Removed {n} schedule(s) for plan {plan_id}",
                severity="information",
            )
        except Exception as exc:
            self.notify(
                f"Remove failed: {exc}", severity="error",
            )
        self._refresh()

    def _plan_to_install(self):
        """Resolve the plan to install a schedule for.

        Right now this expects the operator to have focused a
        row from a different screen + come here via a "schedule
        this plan" jump (deferred — for the first iteration the
        screen only manages EXISTING schedules; install actions
        require an in-screen focus by plan_id, which falls back
        to a "no plan focused" notification). The wiring for
        "select plan on HomeScreen → press [s] → land on
        SchedulingScreen with that plan pre-focused" is a
        follow-up.
        """
        # First pass: when a row is focused, look up the plan by
        # ID. When the list is empty + no plan is staged, we
        # surface a hint instead of crashing.
        plan_id = self._focused_plan()
        if plan_id is None:
            self.notify(
                "Open this screen with a plan focused on "
                "HomeScreen to install a schedule "
                "(install-from-here is a follow-up).",
                severity="warning",
            )
            return None
        # Look up the plan from the registry by ID.
        registry = getattr(self.app, "plan_registry", None)
        if registry is None:
            return None
        for plan in registry.list_plans():
            if plan.plan_id == plan_id:
                return plan
        return None
