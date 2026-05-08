"""Activity / runs monitor screen.

The screen the operator opens to see what backup / restore /
validate processes are currently running across this host (and
which finished recently). Each tile in the list maps 1-1 to a
state file under ``$XDG_STATE_HOME/arq-backup-tui/runs/`` written
by some other process — usually a cron-driven ``arq-backup
create --state-file …`` invocation, but could also be a TUI-
spawned subprocess (see :class:`BackupRunScreen` once dual-mode
ships).

Polling, not pushing: this screen is a read-only view. The
producer side (``RunWriter``) atomically rewrites each state
file, and we sample at 1 Hz. Cancellation is the one exception —
``[c]`` sends SIGTERM to the writer's PID, which the writer's
exit handler turns into ``status=cancelled`` on disk.
"""

from __future__ import annotations

import datetime
import time
from pathlib import Path
from typing import Dict, List, Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.reactive import reactive
from textual.screen import Screen
from textual.widgets import (
    Button,
    Footer,
    Header,
    ListItem,
    ListView,
    Static,
)

from ..runs import (
    RunRecord,
    RunStatus,
    enumerate_runs,
    gc_finished_runs,
    mark_stale,
    signal_cancel,
    state_file_path,
)


# Polling cadence in seconds. Set on the screen so a test can
# override (e.g. ``screen.poll_interval = 0.05``).
DEFAULT_POLL_INTERVAL_SEC = 1.0
# How recent "recent" means in the bottom section.
RECENT_WINDOW_SEC = 24 * 3600


def _humanize_bytes(n: int) -> str:
    f = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if f < 1024 or unit == "TiB":
            return f"{f:6.1f} {unit}"
        f /= 1024
    return f"{n} B"


def _humanize_eta(sec: Optional[float]) -> str:
    if sec is None or sec < 0:
        return "?"
    if sec >= 99 * 3600:
        return ">99h"
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    return f"{h:d}:{m:02d}:{s:02d}"


def _format_clock(t: float) -> str:
    return datetime.datetime.fromtimestamp(t).strftime("%H:%M:%S")


def _status_emoji(status: str) -> str:
    return {
        RunStatus.STARTING.value: "⏳",
        RunStatus.RUNNING.value: "▶",
        RunStatus.COMPLETED.value: "✓",
        RunStatus.FAILED.value: "✗",
        RunStatus.CANCELLED.value: "⊘",
        RunStatus.STALE.value: "⌽",
    }.get(status, "?")


class RunRow(ListItem):
    """One run-line item in the ``ListView``. Stores the
    underlying ``RunRecord`` so the parent screen can dispatch
    actions (cancel / details) against it."""

    def __init__(self, record: RunRecord) -> None:
        self.record = record
        super().__init__(Static(self._render(record), markup=True))

    def update_from(self, record: RunRecord) -> None:
        self.record = record
        try:
            self.query_one(Static).update(self._render(record))
        except Exception:
            pass

    @staticmethod
    def _render(rec: RunRecord) -> str:
        emoji = _status_emoji(rec.status)
        name = rec.plan_name or rec.run_id[:12]
        kind = rec.kind
        progress = rec.progress
        tail = ""
        if rec.status == RunStatus.RUNNING.value:
            if progress.bytes_total:
                pct = (
                    100.0 * progress.bytes_done
                    / max(progress.bytes_total, 1)
                )
                bar = _bar(pct)
                tail = (
                    f" {bar} {pct:5.1f}%  ETA "
                    f"{_humanize_eta(progress.eta_sec)}"
                )
            else:
                tail = (
                    f"  files={progress.files_done} "
                    f"{_humanize_bytes(progress.bytes_done)}"
                )
        elif rec.status in (
            RunStatus.COMPLETED.value, RunStatus.FAILED.value,
            RunStatus.CANCELLED.value, RunStatus.STALE.value,
        ):
            elapsed = ""
            if rec.finished_at and rec.started_at:
                elapsed_sec = int(rec.finished_at - rec.started_at)
                m = elapsed_sec // 60
                s = elapsed_sec % 60
                elapsed = f"  ({m}m{s:02d}s)"
            tail = (
                f"  {_format_clock(rec.started_at)} → "
                f"{_format_clock(rec.finished_at or rec.started_at)}"
                f"{elapsed}"
            )
        return f"[bold]{emoji} {kind:<8s}[/]  {name}{tail}"


def _bar(pct: float, *, width: int = 10) -> str:
    """A tiny block-progress bar — same shape Arq 7's status menu
    uses, scaled to N cells."""
    filled = int(round(pct / 100.0 * width))
    filled = max(0, min(width, filled))
    return "[" + "█" * filled + "░" * (width - filled) + "]"


class RunsMonitorScreen(Screen):
    """Live view of every backup / restore / validate run on this
    host (active + recently finished)."""

    BINDINGS = [
        Binding("r", "refresh", "Refresh", show=True),
        Binding("c", "cancel_focused", "Cancel", show=True),
        Binding("g", "gc_old", "GC old", show=True),
        Binding("escape", "app.pop_screen", "Back", show=True),
        Binding("q", "app.quit", "Quit", show=True),
    ]

    DEFAULT_CSS = """
    RunsMonitorScreen #container {
        padding: 1 2;
        height: 1fr;
    }
    RunsMonitorScreen .section-title {
        text-style: bold;
        color: $accent;
        margin-top: 1;
    }
    RunsMonitorScreen .empty-hint {
        color: $text-muted;
        text-style: italic;
        margin-left: 2;
    }
    RunsMonitorScreen ListView {
        height: auto;
        max-height: 16;
    }
    """

    poll_interval: reactive[float] = reactive(DEFAULT_POLL_INTERVAL_SEC)

    def __init__(
        self, *,
        state_dir: Optional[Path] = None,
    ) -> None:
        super().__init__()
        self.state_dir = state_dir
        # run_id → RunRow for in-place updates.
        self._rows: Dict[str, RunRow] = {}

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="container"):
            yield Static("Active", classes="section-title")
            yield ListView(id="active-list")
            yield Static(
                "(no active runs)",
                id="active-empty", classes="empty-hint",
            )
            yield Static("Recent (last 24h)", classes="section-title")
            yield ListView(id="recent-list")
            yield Static(
                "(none)",
                id="recent-empty", classes="empty-hint",
            )
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_lists()
        self.set_interval(
            float(self.poll_interval), self.refresh_lists,
        )

    # ------------------------------------------------------------------
    # Data refresh
    # ------------------------------------------------------------------

    def refresh_lists(self) -> None:
        """Re-read state files and update the active + recent
        sections in place."""
        records = enumerate_runs(state_dir=self.state_dir)
        # Detect runs whose PID is gone but status=running and flip
        # them to ``stale`` on disk so subsequent reads stay
        # consistent.
        for rec in records:
            if rec.status == RunStatus.RUNNING.value:
                mark_stale(rec, state_dir=self.state_dir)
        records = enumerate_runs(state_dir=self.state_dir)

        now = time.time()
        active: List[RunRecord] = []
        recent: List[RunRecord] = []
        for rec in records:
            if rec.status in (
                RunStatus.STARTING.value, RunStatus.RUNNING.value,
            ):
                active.append(rec)
                continue
            ref = rec.finished_at or rec.started_at
            if ref and (now - ref) <= RECENT_WINDOW_SEC:
                recent.append(rec)

        # Most recent first in both panes.
        active.sort(key=lambda r: r.started_at, reverse=True)
        recent.sort(
            key=lambda r: r.finished_at or r.started_at,
            reverse=True,
        )

        try:
            self._render_into("#active-list", "#active-empty", active)
            self._render_into("#recent-list", "#recent-empty", recent)
        except Exception:
            # Widget tree not ready yet (first call before mount
            # completed) — just retry on the next interval.
            pass

    def _render_into(
        self, list_q: str, empty_q: str,
        records: List[RunRecord],
    ) -> None:
        list_view = self.query_one(list_q, ListView)
        empty = self.query_one(empty_q, Static)
        # Diff existing rows vs. new records by run_id; clear &
        # rebuild on mismatch (the typical ListView is ≤30 rows so
        # it's cheap, and it avoids edge cases around incremental
        # mutations during polling).
        list_view.clear()
        for rec in records:
            row = RunRow(rec)
            list_view.append(row)
            self._rows[rec.run_id] = row
        empty.display = not records

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_refresh(self) -> None:
        self.refresh_lists()

    def action_cancel_focused(self) -> None:
        list_view = self.query_one("#active-list", ListView)
        idx = getattr(list_view, "index", None)
        if idx is None or idx < 0:
            self.notify("Focus an active run first.", severity="warning")
            return
        try:
            row = list_view.children[idx]
        except IndexError:
            return
        if not isinstance(row, RunRow):
            return
        rec = row.record
        if signal_cancel(rec):
            self.notify(
                f"Cancellation signaled to {rec.run_id[:8]}…",
                severity="information",
            )
        else:
            self.notify(
                f"Could not signal {rec.run_id[:8]}… "
                f"(pid {rec.pid} not alive?)",
                severity="warning",
            )

    def action_gc_old(self) -> None:
        n = gc_finished_runs(state_dir=self.state_dir)
        self.notify(
            f"Removed {n} old terminal record(s).",
            severity="information",
        )
        self.refresh_lists()
