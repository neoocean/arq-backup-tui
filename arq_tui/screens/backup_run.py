"""Backup execution screen.

Default path: spawn ``arq-backup create`` as a subprocess and
watch its state file via :class:`SubprocessBackupWorker`. The
state file is the same JSON the cron / systemd path produces, so
the TUI's progress display works identically whether the operator
launched the backup from the menu or it was kicked off
out-of-band by a scheduler.

Fallback path: if the plan can't be expressed in CLI args (SFTP
destination, multi-source plan) or the operator forces the
escape hatch (``ARQ_TUI_IN_PROCESS=1``), drop back to the
in-process :class:`BackupWorker`. Both code paths post the same
``WorkerEvent`` / ``WorkerFinished`` / ``WorkerFailed`` messages
so the screen handlers don't need to know which one is active.

The ``Esc`` key requests cooperative cancellation. For
in-process mode that flips ``Backup.cancel()``; for subprocess
mode it sends SIGTERM to the child PID, which the writer's
``RunWriter`` context handles as ``status=cancelled``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional, Union

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Static

from ..backend_open import close_backend, open_backend
from ..state import Destination, Plan
from ..subprocess_workers import (
    SubprocessBackupWorker,
    subprocess_eligible,
)
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
        # Either an in-process BackupWorker or a SubprocessBackupWorker;
        # both expose ``start`` / ``cancel``. The subprocess variant
        # also writes its progress to a state file under the user's
        # XDG_STATE_HOME so a separate ``arq-tui runs`` invocation
        # could observe the same run.
        self.worker: Optional[Union[
            BackupWorker, SubprocessBackupWorker,
        ]] = None
        self._backend: Any = None
        self._dest: Optional[Destination] = None
        self._mode: str = "subprocess"   # set in on_mount

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(f"Backing up: {self.plan.name}", id="title")
        with Vertical(id="panel"):
            yield ProgressPanel()
        yield Static("", id="footer-row")
        yield Footer()

    def on_mount(self) -> None:
        # Decide which worker mode applies first: subprocess (the
        # default — same code path as cron / systemd) or in-process
        # (legacy + fallback when CLI args can't express the plan).
        force_in_process = bool(os.environ.get("ARQ_TUI_IN_PROCESS"))
        eligible = subprocess_eligible(
            self.plan, self.plan.destination_kind,
        )
        self._mode = (
            "in_process"
            if force_in_process or not eligible
            else "subprocess"
        )

        if self._mode == "subprocess":
            self._start_subprocess_worker()
        else:
            self._start_in_process_worker()

    def _start_subprocess_worker(self) -> None:
        """Spawn ``arq-backup create --state-file …`` as a child
        process and start polling its state file.

        We deliberately don't open the backend on this side: the
        child process opens its own (local-only for now), so we
        avoid double-opening and the race of the parent holding a
        backend the child also writes to.
        """
        panel = self.query_one(ProgressPanel)
        panel.append_log(
            f"Spawning arq-backup CLI subprocess "
            f"(plan={self.plan.name!r})…"
        )
        self.worker = SubprocessBackupWorker(
            self,
            plan=self.plan,
            password=self.password,
        )
        try:
            self.worker.start()
        except Exception as exc:
            self.notify(
                f"Could not spawn backup CLI: {exc}",
                severity="error",
            )
            self.app.pop_screen()
            return
        panel.append_log(
            f"Subprocess running, state file: "
            f"{self.worker.state_file}"
        )

    def _start_in_process_worker(self) -> None:
        """Legacy path: open the backend in the parent and run the
        backup on a sibling Python thread. Required for SFTP
        destinations + multi-source plans the CLI can't express."""
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
        exclusions = self._resolve_exclusions(self.plan)
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
            exclusions=exclusions,
            max_file_bytes=self.plan.max_file_bytes,
            use_apfs_snapshot=self.plan.use_apfs_snapshot,
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

    @staticmethod
    def _resolve_exclusions(plan: Plan):
        """Build an :class:`arq_writer.ExclusionRules` from the
        plan's three pattern lists. Empty lists → ``None`` so the
        writer keeps its default-empty behaviour."""
        if not (
            plan.exclude_globs
            or plan.exclude_regexes
            or plan.exclude_gitignore_lines
        ):
            return None
        from arq_writer import ExclusionRules
        return ExclusionRules.of(
            wildcard=tuple(plan.exclude_globs),
            regex=tuple(plan.exclude_regexes),
            gitignore_lines=tuple(plan.exclude_gitignore_lines),
        )
