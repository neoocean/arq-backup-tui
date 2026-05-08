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

    LOG_TAIL = 50

    def __init__(self) -> None:
        super().__init__()
        self._log: Deque[str] = collections.deque(maxlen=self.LOG_TAIL)
        self._t_start = time.monotonic()

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("Files written:    0", id="line-files-written", classes="stat-line")
            yield Static("Files reused:     0", id="line-files-reused", classes="stat-line")
            yield Static("Trees written:    0", id="line-trees", classes="stat-line")
            yield Static("Bytes plaintext:  0 B", id="line-bytes-plain", classes="stat-line")
            yield Static("Bytes on disk:    0 B", id="line-bytes-disk", classes="stat-line")
            yield Static("Throughput:       0 B/s", id="line-throughput", classes="stat-line")
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
        elapsed = max(time.monotonic() - self._t_start, 0.001)
        rate = self.bytes_plaintext / elapsed
        self.query_one("#line-throughput", Static).update(
            f"Throughput:      {_human_bytes(int(rate))}/s"
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
