"""Backup execution screen.

Spawns a :class:`BackupWorker`, displays a live
:class:`ProgressPanel`, and handles the worker's
``WorkerEvent`` / ``WorkerFinished`` / ``WorkerFailed`` messages.

The ``Esc`` key requests cooperative cancellation; the worker
flips ``Backup.cancel()`` and the writer aborts at the next
directory boundary, raising ``BackupCancelled`` (which we display
as a "Cancelled" status).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Static

from ..backend_open import close_backend, open_backend
from ..state import Destination, Plan
from ..widgets.progress_panel import ProgressPanel
from ..workers import BackupWorker, WorkerEvent, WorkerFailed, WorkerFinished


class BackupRunScreen(Screen):
    """Drive ``arq_writer.Backup`` for a saved Plan.

    Caller passes a :class:`Plan` (already saved on disk) and the
    encryption password (already cached in
    :class:`~arq_tui.state.CredentialCache`).
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    DEFAULT_CSS = """
    BackupRunScreen #title {
        text-style: bold;
        padding: 1 2;
    }
    BackupRunScreen #panel {
        border: round $primary;
        padding: 0 1;
        margin: 0 1;
    }
    BackupRunScreen #footer-row {
        margin-top: 1;
        height: 3;
        padding: 0 2;
        align: right middle;
    }
    """

    def __init__(self, *, plan: Plan, password: str) -> None:
        super().__init__()
        self.plan = plan
        self.password = password
        self.worker: Optional[BackupWorker] = None
        self._backend: Any = None
        self._dest: Optional[Destination] = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(f"Backing up: {self.plan.name}", id="title")
        with Vertical(id="panel"):
            yield ProgressPanel()
        yield Static("", id="footer-row")
        yield Footer()

    def on_mount(self) -> None:
        # Resolve the destination + open the backend on the main
        # thread so any setup error surfaces immediately.
        self._dest = self._plan_destination()
        sftp_password = None
        if self._dest.kind == "sftp":
            cached = self.app.credential_cache.get_sftp_auth(
                self._dest,
            )
            if cached:
                sftp_password = cached.get("password")
        try:
            self._backend = open_backend(
                self._dest, sftp_password=sftp_password,
            )
        except Exception as exc:
            self.notify(
                f"Could not open destination: {exc}",
                severity="error",
            )
            self.app.pop_screen()
            return

        chunker_config = self._resolve_chunker(self.plan.chunker)
        self.worker = BackupWorker(
            self,
            sources=self.plan.sources,
            dest_root="/" if self._backend is not None else "/",
            encryption_password=self.password,
            backend=self._backend,
            use_packs=self.plan.use_packs,
            chunker_config=chunker_config,
            dedup_against_existing=self.plan.dedup_against_existing,
            backup_name=self.plan.name,
        )
        self.worker.start()

    def on_unmount(self) -> None:
        if self._backend is not None:
            close_backend(self._backend)
            self._backend = None

    # ------------------------------------------------------------------
    # Worker message handlers
    # ------------------------------------------------------------------

    def on_worker_event(self, event: WorkerEvent) -> None:
        panel = self.query_one(ProgressPanel)
        panel.consume_event(event.kind, event.payload)

    def on_worker_finished(self, event: WorkerFinished) -> None:
        panel = self.query_one(ProgressPanel)
        panel.finished = True
        if isinstance(event.result, dict):
            panel.append_log(
                f"Backup finished: "
                f"files_written={event.result.get('files_written')} "
                f"files_reused={event.result.get('files_reused')} "
                f"trees={event.result.get('trees_written')} "
                f"bytes_on_disk={event.result.get('bytes_on_disk')}"
            )

    def on_worker_failed(self, event: WorkerFailed) -> None:
        panel = self.query_one(ProgressPanel)
        if "BackupCancelled" in event.error:
            panel.append_log("Cancelled.")
            # Treat cancel as a non-error completion.
            panel.finished = True
        else:
            panel.failed = True
            panel.error_message = event.error
            panel.append_log(f"FAILED: {event.error}")

    def action_cancel(self) -> None:
        if self.worker is not None and not (
            self.query_one(ProgressPanel).finished
            or self.query_one(ProgressPanel).failed
        ):
            self.worker.cancel()
            self.notify("Cancellation requested.", severity="warning")
        else:
            self.app.pop_screen()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _plan_destination(self) -> Destination:
        d = self.plan.destination
        if self.plan.destination_kind == "local":
            return Destination(
                kind="local",
                label=self.plan.name,
                path=str(d.get("path") or ""),
            )
        return Destination(
            kind="sftp",
            label=self.plan.name,
            host=str(d.get("host") or ""),
            port=int(d.get("port") or 22),
            user=str(d.get("user") or ""),
            path=str(d.get("path") or ""),
            identity_file=str(d.get("identity_file") or ""),
        )

    @staticmethod
    def _resolve_chunker(name: str):
        if name == "arq_v7_41":
            from arq_writer.arq_chunker_params import ARQ_V7_CHUNKER_CONFIG
            return ARQ_V7_CHUNKER_CONFIG
        if name == "default":
            from arq_writer.chunker import ChunkerConfig
            return ChunkerConfig()
        # "none" or unrecognized -> no chunker (single blob per file).
        return None
