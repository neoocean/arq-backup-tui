"""Reusable live-progress panel.

Shared shape for backup / restore / validate runs. Counters /
current-file / throughput / event-tail are all reactive properties
so a worker just emits ``WorkerEvent`` messages and the panel
re-renders automatically.
"""

from __future__ import annotations

import collections
import time
from typing import Deque

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static


def _human_bytes(n: int) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    f = float(n)
    for u in units:
        if f < 1024.0 or u == units[-1]:
            return f"{f:7.2f} {u}"
        f /= 1024.0
    return f"{n} B"


class ProgressPanel(Widget):
    """Counters + current-file + throughput + last-events tail."""

    DEFAULT_CSS = """
    ProgressPanel {
        height: auto;
        padding: 0 1;
    }
    ProgressPanel .stat-line {
        height: 1;
    }
    ProgressPanel #current {
        margin-top: 1;
    }
    ProgressPanel #log-tail {
        margin-top: 1;
        max-height: 12;
    }
    ProgressPanel .label {
        color: $text-muted;
    }
    """

    files_written: reactive[int] = reactive(0)
    files_reused: reactive[int] = reactive(0)
    trees_written: reactive[int] = reactive(0)
    bytes_plaintext: reactive[int] = reactive(0)
    bytes_on_disk: reactive[int] = reactive(0)
    current_path: reactive[str] = reactive("")
    finished: reactive[bool] = reactive(False)
    failed: reactive[bool] = reactive(False)
    error_message: reactive[str] = reactive("")
    # Optional total budget — set when the worker can plan ahead
    # (e.g. Restore.restore(plan_totals=True) emits restore_planned).
    # Zero means "no plan", in which case the ETA line stays blank.
    total_bytes: reactive[int] = reactive(0)
    total_files: reactive[int] = reactive(0)

    LOG_TAIL = 50
    # Sliding window for throughput / ETA. Each sample is
    # (monotonic_t, bytes_seen). 30 s of history smooths out
    # bursty single-file drops without lagging too far behind a
    # real slowdown.
    _RATE_WINDOW_SEC = 30.0

    def __init__(self) -> None:
        super().__init__()
        self._log: Deque[str] = collections.deque(maxlen=self.LOG_TAIL)
        self._t_start = time.monotonic()
        self._rate_samples: Deque[tuple] = collections.deque()

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("Files written:    0", id="line-files-written", classes="stat-line")
            yield Static("Files reused:     0", id="line-files-reused", classes="stat-line")
            yield Static("Trees written:    0", id="line-trees", classes="stat-line")
            yield Static("Bytes plaintext:  0 B", id="line-bytes-plain", classes="stat-line")
            yield Static("Bytes on disk:    0 B", id="line-bytes-disk", classes="stat-line")
            yield Static("Throughput:       0 B/s", id="line-throughput", classes="stat-line")
            yield Static("", id="line-eta", classes="stat-line")
            yield Static("", id="current")
            yield Static("", id="log-tail")
            yield Static("", id="status")

    # ------------------------------------------------------------------
    # Reactive watchers
    # ------------------------------------------------------------------

    def watch_files_written(self, value: int) -> None:
        self.query_one("#line-files-written", Static).update(
            f"Files written:   {value:>8}"
        )

    def watch_files_reused(self, value: int) -> None:
        self.query_one("#line-files-reused", Static).update(
            f"Files reused:    {value:>8}"
        )

    def watch_trees_written(self, value: int) -> None:
        self.query_one("#line-trees", Static).update(
            f"Trees written:   {value:>8}"
        )

    def watch_bytes_plaintext(self, value: int) -> None:
        self.query_one("#line-bytes-plain", Static).update(
            f"Bytes plaintext: {_human_bytes(value)}"
        )
        self._update_throughput()

    def watch_bytes_on_disk(self, value: int) -> None:
        self.query_one("#line-bytes-disk", Static).update(
            f"Bytes on disk:   {_human_bytes(value)}"
        )

    def watch_current_path(self, value: str) -> None:
        if value:
            self.query_one("#current", Static).update(
                f"Current: {value}"
            )

    def watch_finished(self, value: bool) -> None:
        if value:
            self.query_one("#status", Static).update(
                "[reverse green]✓ Finished[/]"
            )

    def watch_failed(self, value: bool) -> None:
        if value:
            self.query_one("#status", Static).update(
                f"[reverse red]✗ Failed[/] {self.error_message}"
            )

    def _update_throughput(self) -> None:
        now = time.monotonic()
        # Drop samples older than the window and append the current
        # observation so the rate reflects the recent past, not the
        # whole run. A bursty start no longer makes the ETA wildly
        # optimistic five minutes in.
        self._rate_samples.append((now, self.bytes_plaintext))
        cutoff = now - self._RATE_WINDOW_SEC
        while (
            self._rate_samples and self._rate_samples[0][0] < cutoff
        ):
            self._rate_samples.popleft()
        if len(self._rate_samples) >= 2:
            t0, b0 = self._rate_samples[0]
            t1, b1 = self._rate_samples[-1]
            rate = max((b1 - b0), 0) / max((t1 - t0), 0.001)
        else:
            elapsed = max(now - self._t_start, 0.001)
            rate = self.bytes_plaintext / elapsed
        self.query_one("#line-throughput", Static).update(
            f"Throughput:      {_human_bytes(int(rate))}/s"
        )
        self._update_eta(rate)

    def _update_eta(self, rate: float) -> None:
        """Render the ETA line.

        Hidden when no plan has been emitted (``total_bytes == 0``)
        or when the rate is too small to give a sensible estimate.
        Otherwise show ``H:MM:SS`` of remaining wall time.
        """
        line = self.query_one("#line-eta", Static)
        if self.total_bytes <= 0 or rate <= 0:
            line.update("")
            return
        remaining = max(self.total_bytes - self.bytes_plaintext, 0)
        seconds = remaining / rate
        # Cap absurdly large ETAs (huge file, tiny initial rate)
        # so the output stays readable.
        if seconds > 99 * 3600:
            line.update(
                f"ETA:             >99h "
                f"({_human_bytes(remaining)} left)"
            )
            return
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = int(seconds % 60)
        pct = 100.0 * self.bytes_plaintext / self.total_bytes
        line.update(
            f"ETA:             {h:d}:{m:02d}:{s:02d} "
            f"(progress {pct:5.1f}%, "
            f"{_human_bytes(remaining)} left)"
        )

    def watch_total_bytes(self, value: int) -> None:
        # Re-render the ETA line as soon as a plan lands so the user
        # gets feedback before bytes start ticking in.
        if value > 0:
            line = self.query_one("#line-eta", Static)
            line.update(
                f"ETA:             — "
                f"(0.0%, {_human_bytes(value)} planned)"
            )

    # ------------------------------------------------------------------
    # External event sink
    # ------------------------------------------------------------------

    def append_log(self, line: str) -> None:
        self._log.append(line)
        self.query_one("#log-tail", Static).update(
            "\n".join(self._log)
        )

    def consume_event(self, kind: str, payload: dict) -> None:
        """Apply one ProgressCb-style event to this panel.

        Recognized kinds (others are forwarded to the log only):

        - ``file_written`` / ``file_reused`` (writer)
        - ``tree_written`` (writer)
        - ``backuprecord_written`` / ``keyset_written`` (writer)
        - ``file_restored`` / ``tree_restored`` (reader)
        - ``audit_progress`` etc. (validator) — log-only by default
        """
        if kind == "restore_planning":
            # Drip from the pre-walk so the user sees "Planning:
            # 1234 files, 567 MB so far…" instead of an idle bar
            # for the duration of a slow tree fetch. We don't set
            # total_bytes yet — that lands on restore_planned.
            files = int(payload.get("files") or 0)
            byts = int(payload.get("bytes") or 0)
            self.append_log(
                f"Planning: {files} files, "
                f"{_human_bytes(byts)} so far…"
            )
            return
        if kind == "restore_planned":
            # Pre-walk emitted by Restore.restore(plan_totals=True).
            # Sets the budget the ETA uses; further ticks update the
            # remaining estimate.
            self.total_files = int(payload.get("total_files") or 0)
            self.total_bytes = int(payload.get("total_bytes") or 0)
            return
        if kind == "file_written":
            self.files_written += 1
            self.bytes_plaintext += int(payload.get("size") or 0)
            self.current_path = str(payload.get("path") or "")
        elif kind == "file_reused":
            self.files_reused += 1
            self.files_written += 1   # also counts as "covered"
            self.current_path = str(payload.get("path") or "")
        elif kind == "tree_written":
            self.trees_written += 1
        elif kind in ("file_restored", "audit_file_verified"):
            self.files_written += 1
            self.bytes_plaintext += int(payload.get("size") or 0)
            self.current_path = str(payload.get("path") or "")
        elif kind == "tree_restored":
            self.trees_written += 1
        # Everything goes to the log tail.
        summary = self._summarize_event(kind, payload)
        if summary:
            self.append_log(summary)

    @staticmethod
    def _summarize_event(kind: str, payload: dict) -> str:
        path = payload.get("path") or payload.get("rel_path") or ""
        size = payload.get("size")
        if size is not None:
            return f"{kind:24s} {path}  size={size}"
        if path:
            return f"{kind:24s} {path}"
        return f"{kind:24s} {payload}"
