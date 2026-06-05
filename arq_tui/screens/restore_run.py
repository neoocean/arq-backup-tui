"""Restore execution screen.

Spawns a :class:`RestoreWorker` against an already-opened backend
and renders progress through the shared :class:`ProgressPanel`.

Two entry shapes:

- **Whole-record restore**: caller passes ``paths=None``.
- **Selective restore**: caller passes a list of source-relative
  POSIX paths (typically marked in :class:`RecordBrowserScreen`).
  Path matching is byte-for-byte against the Tree's UTF-8 child
  names — non-ASCII paths round-trip transparently.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, List, Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Footer, Static

from ._overlay import OverlayScreen

from ..widgets.progress_panel import ProgressPanel
from ..workers import (
    RestoreWorker,
    WorkerEvent,
    WorkerFailed,
    WorkerFinished,
)


class RestoreRunScreen(OverlayScreen):
    """Drive ``arq_reader.Restore.restore`` for a chosen record."""

    BINDINGS = [
        Binding("escape", "back_or_close", "Back", show=True),
    ]

    DEFAULT_CSS = """
    RestoreRunScreen #title {
        text-style: bold;
        padding: 1 2;
    }
    RestoreRunScreen #panel {
        border: round $primary;
        padding: 0 1;
        margin: 0 1;
    }
    """

    def __init__(
        self,
        *,
        backend: Any,
        encryption_password: str,
        computer_uuid: str,
        folder_uuid: str,
        backuprecord_path: Optional[str],
        target: Path,
        paths: Optional[List[str]] = None,
        record_label: str = "",
    ) -> None:
        super().__init__()
        self.backend = backend
        self.password = encryption_password
        self.computer_uuid = computer_uuid
        self.folder_uuid = folder_uuid
        self.backuprecord_path = backuprecord_path
        self.target = Path(target)
        self.paths = list(paths) if paths is not None else None
        self.record_label = record_label
        self.worker: Optional[RestoreWorker] = None

    def compose(self) -> ComposeResult:
        with Vertical(classes="overlay-box"):
            yield Static(self._title(), id="title")
            with Vertical(id="panel"):
                yield ProgressPanel()
            yield Footer()

    def _title(self) -> str:
        scope = "selected paths" if self.paths else "full folder"
        if self.record_label:
            return f"Restoring {scope}: {self.record_label} → {self.target}"
        return f"Restoring {scope} → {self.target}"

    def on_mount(self) -> None:
        # Materialize the destination root so the restorer's first
        # mkdir on a child path can succeed.
        try:
            self.target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.notify(
                f"Could not create target: {exc}", severity="error",
            )
            self.app.pop_screen()
            return
        self.worker = RestoreWorker(
            self,
            backend=self.backend,
            encryption_password=self.password,
            folder_uuid=self.folder_uuid,
            computer_uuid=self.computer_uuid,
            dest=self.target,
            backuprecord_path=self.backuprecord_path,
            paths=self.paths,
        )
        self.worker.start()

    def on_worker_event(self, event: WorkerEvent) -> None:
        self.query_one(ProgressPanel).consume_event(
            event.kind, event.payload,
        )

    def on_worker_finished(self, event: WorkerFinished) -> None:
        panel = self.query_one(ProgressPanel)
        panel.finished = True
        result = event.result
        if result is not None:
            panel.append_log(
                f"Restore finished: files={result.files_restored} "
                f"dirs={result.dirs_restored} "
                f"failures={len(result.failures)}"
            )

    def on_worker_failed(self, event: WorkerFailed) -> None:
        panel = self.query_one(ProgressPanel)
        panel.failed = True
        panel.error_message = event.error
        panel.append_log(f"FAILED: {event.error}")

    def action_back_or_close(self) -> None:
        self.app.pop_screen()
