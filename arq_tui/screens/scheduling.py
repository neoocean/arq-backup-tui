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
from textual.widgets import (
    Footer, ListItem, ListView, Static,
)

from ._overlay import OverlayScreen

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


class SchedulingScreen(OverlayScreen):
    """List + manage all arq-backup-tui-managed schedules.

    When opened from HomeScreen with a focused plan
    (``staged_plan`` set), the install actions ([i] / [l]) target
    that plan instead of requiring a row to be focused. This is
    the install-from-Home flow: HomeScreen pushes
    ``SchedulingScreen(staged_plan=plan)`` → operator presses
    [i] → cron entry installed for that plan without leaving
    the screen.
    """

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

    def __init__(self, *, staged_plan=None) -> None:
        super().__init__()
        self._staged_plan = staged_plan

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
        with Vertical(classes="overlay-box"):
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

        Order of preference:
        1. ``self._staged_plan`` — set by HomeScreen when the
           operator presses [s] with a plan focused. This is the
           normal install-from-Home flow.
        2. Focused row in the schedule list — re-uses the same
           plan_id for the new install (effectively reschedules).
        3. None — emit a hint pointing at the install-from-Home
           flow.
        """
        if self._staged_plan is not None:
            return self._staged_plan
        plan_id = self._focused_plan()
        if plan_id is None:
            self.notify(
                "No plan staged. Press [s] on HomeScreen with a "
                "plan focused to install a schedule for it.",
                severity="warning",
            )
            return None
        registry = getattr(self.app, "plan_registry", None)
        if registry is None:
            return None
        for plan in registry.list_plans():
            if plan.plan_id == plan_id:
                return plan
        return None
