"""Activity / runs monitor.

Shows what backup / restore / validate processes are running across
this host (and which finished recently). Each tile maps to either a
state file under ``$XDG_STATE_HOME/arq-backup-tui/runs/`` written by
some other process (a cron-driven ``arq-backup create --state-file …``
or a TUI-spawned subprocess), or a row from a locally-installed
Arq.app's activity log (badged "◆ Arq", read-only).

The UI lives in :class:`ActivityPanel` (a widget) so it can be hosted
either as the right-hand content of the main shell (sidebar → Activity
Log) or, standalone, inside :class:`RunsMonitorScreen` (a thin wrapper
kept for direct pushes + the slash-command console).

Polling, not pushing: this is a read-only view. The producer side
(``RunWriter``) atomically rewrites each state file, and we sample at
1 Hz. Cancellation (``[c]``) sends SIGTERM to our own writer's PID;
Arq-owned rows can't be cancelled from here.
"""

from __future__ import annotations

import datetime
import time
from pathlib import Path
from typing import Dict, List, Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.reactive import reactive
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Footer,
    Header,
    Label,
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
)


# Polling cadence in seconds. Set on the panel so a test can override.
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
    underlying ``RunRecord`` so the parent can dispatch
    actions (cancel / details) against it."""

    def __init__(self, record: RunRecord) -> None:
        self.record = record
        super().__init__(Static(self._format_label(record), markup=True))

    def update_from(self, record: RunRecord) -> None:
        self.record = record
        try:
            self.query_one(Static).update(self._format_label(record))
        except Exception:
            pass

    @staticmethod
    def _format_label(rec: RunRecord) -> str:
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


# Prefix marking a RunRecord synthesised from an Arq.app activity row.
ARQ_RUN_PREFIX = "arq:"


class ActivityPanel(Vertical):
    """Live activity view — the right-hand content of the Activity Log
    section. Active runs on top, recently-finished below.

    Bindings ride on the panel so they work both standalone (inside
    :class:`RunsMonitorScreen`) and when hosted in the main shell —
    they fire whenever a descendant (a run list) holds focus.
    """

    DEFAULT_CSS = """
    ActivityPanel {
        padding: 1 2;
        height: 1fr;
    }
    ActivityPanel .section-title {
        text-style: bold;
        color: $accent;
        margin-top: 1;
    }
    ActivityPanel .empty-hint {
        color: $text-muted;
        text-style: italic;
        margin-left: 2;
    }
    ActivityPanel ListView {
        height: auto;
        max-height: 16;
    }
    """

    BINDINGS = [
        Binding("r", "refresh", "Refresh", show=True),
        Binding("c", "cancel_focused", "Cancel run", show=True),
        Binding("g", "gc_old", "GC old", show=True),
    ]

    poll_interval: reactive[float] = reactive(DEFAULT_POLL_INTERVAL_SEC)

    def __init__(
        self, *,
        state_dir: Optional[Path] = None,
        id: Optional[str] = None,
    ) -> None:
        super().__init__(id=id)
        self.state_dir = state_dir
        # run_id → RunRow for in-place updates.
        self._rows: Dict[str, RunRow] = {}
        # Arq activity uuid → detailed log file path (for the detail
        # popup). Populated as Arq rows are synthesised.
        self._arq_log_paths: Dict[str, str] = {}
        # Guards against overlapping background refreshes.
        self._refreshing = False

    def compose(self) -> ComposeResult:
        yield Static("In progress", classes="section-title")
        yield ListView(id="active-list")
        yield Static(
            "(no active runs)", id="active-empty", classes="empty-hint",
        )
        yield Static("Recent (last 24h)", classes="section-title")
        yield ListView(id="recent-list")
        yield Static("(none)", id="recent-empty", classes="empty-hint")

    def on_mount(self) -> None:
        self.refresh_lists()
        self.set_interval(float(self.poll_interval), self.refresh_lists)

    # ------------------------------------------------------------------
    # Data refresh
    # ------------------------------------------------------------------

    def refresh_lists(self) -> None:
        """Kick off a refresh of both sections.

        Two responsiveness guarantees:

        - **Skip when hidden.** When another section is showing in the
          shell this panel's ``display`` is False, so we do no work —
          previously the per-second disk/DB scan ran even while the
          operator was on Storage Locations, blocking the event loop
          and making the cursor stutter there.
        - **Off the event loop.** The actual scan (run-state files +
          mark-stale writes + reading the Arq server.db) runs in a
          worker thread; the UI only touches the widget tree once the
          data is ready, so input stays responsive even on the Activity
          panel itself.
        """
        if not self.display:
            return
        if self._refreshing:
            return
        self._refreshing = True
        self.run_worker(
            self._gather, thread=True, exclusive=True,
            group="activity-refresh",
        )

    def _gather(self) -> None:
        """Worker-thread body: all the blocking IO lives here."""
        try:
            now = time.time()
            records = enumerate_runs(state_dir=self.state_dir)
            for rec in records:
                if rec.status == RunStatus.RUNNING.value:
                    mark_stale(rec, state_dir=self.state_dir)
            records = enumerate_runs(state_dir=self.state_dir)

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

            arq_active, arq_recent = self._arq_run_records(now)
            active.extend(arq_active)
            recent.extend(arq_recent)

            active.sort(key=lambda r: r.started_at, reverse=True)
            recent.sort(
                key=lambda r: r.finished_at or r.started_at, reverse=True,
            )
        except Exception:
            active, recent = [], []
        self.app.call_from_thread(self._apply_lists, active, recent)

    def _apply_lists(
        self, active: List[RunRecord], recent: List[RunRecord],
    ) -> None:
        self._refreshing = False
        try:
            self._render_into("#active-list", "#active-empty", active)
            self._render_into("#recent-list", "#recent-empty", recent)
        except Exception:
            # Widget tree not ready yet (first call before mount
            # completed) — retry on the next interval.
            pass

    def _arq_run_records(self, now: float):
        """Project the mirrored Arq.app activity log onto
        ``(active, recent)`` RunRecord lists. Empty when no Arq mirror
        is present; ``recent`` uses the same 24h window."""
        arq_src = getattr(self.app, "arq_app", None)
        if arq_src is None:
            return [], []
        try:
            names = {p.plan_uuid: p.name for p in arq_src.plans()}
            activities = arq_src.activities(limit=200)
        except Exception:
            return [], []
        active: List[RunRecord] = []
        recent: List[RunRecord] = []
        for act in activities:
            rec = self._activity_to_record(act, names)
            if act.is_running:
                active.append(rec)
                continue
            ref = rec.finished_at or rec.started_at
            if ref and (now - ref) <= RECENT_WINDOW_SEC:
                recent.append(rec)
        return active, recent

    def _activity_to_record(self, act, names: Dict[str, str]) -> RunRecord:
        from ..runs import RunProgress
        if act.is_running:
            status = RunStatus.RUNNING.value
        elif act.aborted:
            status = RunStatus.CANCELLED.value
        elif act.error_count:
            status = RunStatus.FAILED.value
        else:
            status = RunStatus.COMPLETED.value
        plan_name = names.get(act.plan_uuid or "", "") or (
            (act.plan_uuid or "")[:8]
        )
        if act.activity_log_path:
            self._arq_log_paths[act.uuid] = act.activity_log_path
        return RunRecord(
            run_id=f"{ARQ_RUN_PREFIX}{act.uuid}",
            kind=act.kind_label,
            status=status,
            started_at=act.created_time,
            finished_at=act.finished_time,
            pid=0,
            plan_id=act.plan_uuid or "",
            plan_name=f"◆ Arq · {plan_name}",
            progress=RunProgress(
                files_total=act.total_files,
                files_done=act.processed_files,
                bytes_total=act.total_bytes,
                bytes_done=act.processed_bytes,
            ),
            error=act.abort_reason if act.aborted else None,
        )

    def _render_into(
        self, list_q: str, empty_q: str, records: List[RunRecord],
    ) -> None:
        list_view = self.query_one(list_q, ListView)
        empty = self.query_one(empty_q, Static)
        empty.display = not records

        rows = [c for c in list_view.children if isinstance(c, RunRow)]
        new_keys = [r.run_id for r in records]
        old_keys = [c.record.run_id for c in rows]
        if new_keys == old_keys:
            # Same runs, same order — just refresh each row's label in
            # place (e.g. a running backup's progress %). Crucially we
            # do NOT clear()/rebuild the ListView, so the operator's
            # keyboard cursor isn't yanked back to the top every poll.
            for row, rec in zip(rows, records):
                row.update_from(rec)
            return

        # Membership changed (a run started / finished): rebuild, but
        # preserve the cursor index (clamped) so it doesn't jump home.
        prev_index = getattr(list_view, "index", None)
        list_view.clear()
        for rec in records:
            row = RunRow(rec)
            list_view.append(row)
            self._rows[rec.run_id] = row
        if records and prev_index is not None:
            list_view.index = max(0, min(prev_index, len(records) - 1))

    # ------------------------------------------------------------------
    # Detail popup (Enter on a row)
    # ------------------------------------------------------------------

    def on_list_view_selected(self, event) -> None:
        row = getattr(event, "item", None)
        if not isinstance(row, RunRow):
            return
        rec = row.record
        log_path = None
        if rec.run_id.startswith(ARQ_RUN_PREFIX):
            uuid = rec.run_id[len(ARQ_RUN_PREFIX):]
            log_path = self._arq_log_paths.get(uuid)
        self.app.push_screen(ActivityDetailModal(record=rec, log_path=log_path))

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
        if rec.run_id.startswith(ARQ_RUN_PREFIX):
            # Arq's agent owns this run — we can read its progress but
            # not signal it. The operator cancels it in Arq.app.
            self.notify(
                "This run is managed by Arq.app — cancel it from the "
                "Arq menu bar / app.",
                severity="warning",
            )
            return
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


class RunsMonitorScreen(Screen):
    """Standalone wrapper around :class:`ActivityPanel` — kept for
    direct pushes (slash-command console, tests). The main shell hosts
    the panel directly instead."""

    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back", show=True),
        Binding("q", "app.quit", "Quit", show=True),
    ]

    def __init__(self, *, state_dir: Optional[Path] = None) -> None:
        super().__init__()
        self._state_dir = state_dir

    def compose(self) -> ComposeResult:
        yield Header()
        yield ActivityPanel(state_dir=self._state_dir, id="activity-panel")
        yield Footer()


def _read_log_tail(path: str, *, max_bytes: int = 65536) -> str:
    """Read the last ``max_bytes`` of a (possibly large) log file as
    text. Returns a friendly message on any error — the Arq logs live
    under a root-owned dir, so reads can legitimately fail."""
    try:
        import os
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            data = f.read()
        text = data.decode("utf-8", errors="replace")
        if size > max_bytes:
            text = "…(truncated to last 64 KB)…\n" + text
        return text
    except Exception as exc:
        return f"(could not read log: {exc})"


class ActivityDetailModal(ModalScreen[None]):
    """Popup showing one run's status + log (Enter on an Activity row).

    For an Arq-mirrored run the detailed per-run log file is read
    (tailed) in a worker thread; for our own runs the recent event tail
    carried in the state file is shown."""

    BINDINGS = [
        Binding("escape", "close", "Close", show=True),
        Binding("q", "close", "Close", show=False),
    ]

    DEFAULT_CSS = """
    ActivityDetailModal {
        align: center middle;
        background: $background 55%;
    }
    ActivityDetailModal > Vertical {
        width: 96;
        max-width: 95%;
        height: auto;
        max-height: 90%;
        background: $surface;
        border: round $primary;
        padding: 1 2;
    }
    ActivityDetailModal .title { text-style: bold; margin-bottom: 1; }
    ActivityDetailModal #status { margin-bottom: 1; }
    ActivityDetailModal .log-label { color: $text-muted; }
    ActivityDetailModal #log-scroll {
        height: auto;
        max-height: 22;
        border: round $surface-lighten-2;
        padding: 0 1;
        margin-top: 1;
    }
    """

    def __init__(self, *, record: RunRecord, log_path: Optional[str] = None):
        super().__init__()
        self.record = record
        self.log_path = log_path

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self._title(), classes="title")
            yield Static(self._status_text(), id="status")
            yield Label("Log", classes="log-label")
            with VerticalScroll(id="log-scroll"):
                yield Static(self._initial_log(), id="log-body")
            yield Static("Esc to close.", classes="log-label")

    def on_mount(self) -> None:
        if self.log_path:
            self.run_worker(self._load_log, thread=True)

    def _load_log(self) -> None:
        text = _read_log_tail(self.log_path)
        self.app.call_from_thread(self._set_log, text)

    def _set_log(self, text: str) -> None:
        try:
            self.query_one("#log-body", Static).update(text or "(log empty)")
        except Exception:
            pass

    def action_close(self) -> None:
        self.dismiss(None)

    # -- text builders -----------------------------------------------

    def _title(self) -> str:
        name = self.record.plan_name or self.record.run_id
        return f"{self.record.kind} — {name}"

    def _status_text(self) -> str:
        rec = self.record
        lines = [f"Status: {rec.status}"]
        if rec.started_at:
            lines.append(f"Started:  {_format_clock(rec.started_at)}")
        if rec.finished_at:
            lines.append(f"Finished: {_format_clock(rec.finished_at)}")
        p = rec.progress
        if p.files_total:
            lines.append(f"Files: {p.files_done}/{p.files_total}")
        elif p.files_done:
            lines.append(f"Files: {p.files_done}")
        if p.bytes_done:
            tot = (
                f" / {_humanize_bytes(p.bytes_total)}"
                if p.bytes_total else ""
            )
            lines.append(
                f"Bytes: {_humanize_bytes(p.bytes_done)}{tot}"
            )
        if rec.error:
            lines.append(f"Error: {rec.error}")
        return "\n".join(lines)

    def _initial_log(self) -> str:
        if self.log_path:
            return "(loading log…)"
        if self.record.events_tail:
            return "\n".join(
                f"{_format_clock(ev.t)} {ev.kind} {ev.payload}"
                for ev in self.record.events_tail
            )
        return "(no detailed log for this run)"
