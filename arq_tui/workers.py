"""Worker-thread bridge for ProgressCb-emitting library calls.

The library's writer / reader / validator all use the same
``ProgressCb(kind: str, payload: dict)`` shape. The TUI runs them
on a sibling Python thread (so the Textual event loop stays
responsive) and forwards every callback into the main loop as a
Textual ``Message`` via ``app.call_from_thread``.

Three workers ship here:

- :class:`BackupWorker` — wraps :func:`arq_writer.build_backup`.
- :class:`RestoreWorker` — wraps :meth:`arq_reader.Restore.restore`.
- :class:`ValidateWorker` — wraps :func:`arq_validator.validate`.

Each posts a ``WorkerEvent`` per callback plus a final
``WorkerFinished`` (or ``WorkerFailed``) message. Cancellation is
cooperative: ``BackupWorker.cancel`` flips a flag the writer
already knows about; restore/validate cancellation is best-effort
(KeyboardInterrupt-equivalent).
"""

from __future__ import annotations

import threading
from typing import Any, Dict, Optional

from textual.message import Message


class WorkerEvent(Message):
    """One ProgressCb tick translated into a Textual message."""

    def __init__(self, kind: str, payload: Dict[str, Any]) -> None:
        super().__init__()
        self.kind = kind
        self.payload = payload


class WorkerFinished(Message):
    """The worker's underlying call returned without raising."""

    def __init__(self, result: Any = None) -> None:
        super().__init__()
        self.result = result


class WorkerFailed(Message):
    """The worker's underlying call raised."""

    def __init__(self, error: str, traceback: str = "") -> None:
        super().__init__()
        self.error = error
        self.traceback = traceback


class _BaseWorker:
    """Common scaffolding for the three worker types.

    Subclasses implement :meth:`_run` with the actual
    library-blocking work and use ``self._emit`` for ProgressCb
    forwarding. Messages post to a target widget (typically the
    screen that owns the worker), where on_worker_* handlers
    receive them — Textual's App.post_message does NOT bubble
    down to children automatically, so posting to the App alone
    leaves the messages unobserved.
    """

    def __init__(self, target) -> None:
        self.target = target           # widget that receives messages
        self.app = target.app          # for call_from_thread
        self._thread: Optional[threading.Thread] = None
        self._cancelled = False

    # -- subclass hooks ------------------------------------------------

    def _run(self) -> Any:
        raise NotImplementedError

    # -- public API ----------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("worker already started")
        self._thread = threading.Thread(
            target=self._thread_main, daemon=True,
        )
        self._thread.start()

    def cancel(self) -> None:
        """Default cancel: just flip the flag. Subclasses with a
        library-specific cancel hook (Backup) override."""
        self._cancelled = True

    def join(self, timeout: Optional[float] = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    # -- internals -----------------------------------------------------

    def _thread_main(self) -> None:
        try:
            result = self._run()
        except Exception as exc:
            import traceback as _tb
            self._post(WorkerFailed(
                f"{type(exc).__name__}: {exc}",
                _tb.format_exc(),
            ))
            return
        self._post(WorkerFinished(result))

    def _emit(self, kind: str, payload: dict) -> None:
        # Marshal the payload into a fresh dict so the consumer
        # can't see later mutations from the writer.
        self._post(WorkerEvent(kind, dict(payload)))

    def _post(self, msg: Message) -> None:
        # call_from_thread is Textual's officially-supported
        # cross-thread bridge; it queues into the main loop. We
        # target the worker's owning screen / widget so its
        # on_worker_event handlers receive the message — App
        # message dispatch does not bubble down to children.
        try:
            self.app.call_from_thread(self.target.post_message, msg)
        except Exception:
            # Posting after the app has shut down is fine; just
            # drop the event silently.
            pass


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------


class BackupWorker(_BaseWorker):
    """Drive ``arq_writer.build_backup`` from a worker thread.

    Constructor builds and stores all the call kwargs so the
    thread itself doesn't have to touch the DOM.
    """

    def __init__(
        self, target, *,
        sources,
        dest_root,
        encryption_password: str,
        backend=None,
        computer_uuid: Optional[str] = None,
        plan_uuid: Optional[str] = None,
        folder_uuid: Optional[str] = None,
        use_packs: bool = True,
        chunker_config=None,
        dedup_against_existing: bool = True,
        backup_name: str = "TUI backup",
        exclusions=None,
        max_file_bytes: Optional[int] = None,
        use_apfs_snapshot: bool = False,
    ) -> None:
        super().__init__(target)
        self.sources = list(sources)
        self.dest_root = dest_root
        self.encryption_password = encryption_password
        self.backend = backend
        self.computer_uuid = computer_uuid
        self.plan_uuid = plan_uuid
        self.folder_uuid = folder_uuid
        self.use_packs = use_packs
        self.chunker_config = chunker_config
        self.dedup_against_existing = dedup_against_existing
        self.backup_name = backup_name
        self.exclusions = exclusions
        self.max_file_bytes = max_file_bytes
        self.use_apfs_snapshot = use_apfs_snapshot
        self._backup = None  # set by _run for cancel routing

    def cancel(self) -> None:
        super().cancel()
        bk = self._backup
        if bk is not None:
            bk.cancel()

    def _run(self):
        # Use the lower-level Backup class rather than build_backup
        # so we can route cancel through and so multi-source plans
        # can call add_folder once per source.
        from arq_writer import Backup

        bk = Backup(
            dest_root=self.dest_root,
            encryption_password=self.encryption_password,
            backup_name=self.backup_name,
            backend=self.backend,
            computer_uuid=self.computer_uuid,
            plan_uuid=self.plan_uuid,
            use_packs=self.use_packs,
            chunker_config=self.chunker_config,
            dedup_against_existing=self.dedup_against_existing,
            exclusions=self.exclusions,
            max_file_bytes=self.max_file_bytes,
            callback=self._emit,
        )
        self._backup = bk
        bk.init_plan()
        results = []
        for src in self.sources:
            from pathlib import Path as _P
            results.append(self._add_folder(
                bk, _P(src),
                folder_uuid=(
                    self.folder_uuid if len(self.sources) == 1 else None
                ),
                folder_name=_P(src).name or "root",
            ))
        return {
            "computer_uuid": bk.computer_uuid,
            "plan_uuid": bk.plan_uuid,
            "files_written": bk.files_written,
            "files_reused": bk.files_reused,
            "trees_written": bk.trees_written,
            "bytes_plaintext": bk.bytes_plaintext,
            "bytes_on_disk": bk.bytes_on_disk,
            "blob_count": len(bk.blob_ids),
            "backuprecords": [str(p) for p in results],
        }

    def _add_folder(self, bk, src, *, folder_uuid, folder_name):
        """Run ``bk.add_folder`` on ``src`` honouring
        ``use_apfs_snapshot``.

        On macOS APFS the writer is fed a frozen snapshot of the
        source so file content can't shift mid-walk; on every other
        platform :func:`arq_writer.with_apfs_snapshot` raises
        :class:`NotMacOSError` and we fall back to the live walk
        with an ``apfs_snapshot_skipped`` event for the panel.
        """
        if not self.use_apfs_snapshot:
            return bk.add_folder(
                src,
                folder_uuid=folder_uuid,
                folder_name=folder_name,
            )
        from arq_writer import NotMacOSError, with_apfs_snapshot
        try:
            with with_apfs_snapshot(src) as snap_path:
                return bk.add_folder(
                    snap_path,
                    folder_uuid=folder_uuid,
                    folder_name=folder_name,
                )
        except NotMacOSError:
            self._emit(
                "apfs_snapshot_skipped",
                {"reason": "not_macos", "source": str(src)},
            )
            return bk.add_folder(
                src,
                folder_uuid=folder_uuid,
                folder_name=folder_name,
            )


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------


class RestoreWorker(_BaseWorker):
    """Drive ``arq_reader.Restore.restore`` from a worker thread."""

    def __init__(
        self, target, *,
        backend,
        encryption_password: str,
        folder_uuid: str,
        computer_uuid: str,
        dest,
        backuprecord_path: Optional[str] = None,
        paths=None,
    ) -> None:
        super().__init__(target)
        self.backend = backend
        self.encryption_password = encryption_password
        self.folder_uuid = folder_uuid
        self.computer_uuid = computer_uuid
        self.dest = dest
        self.backuprecord_path = backuprecord_path
        self.paths = list(paths) if paths is not None else None

    def _run(self):
        from arq_reader import Restore
        rs = Restore(
            "/",
            encryption_password=self.encryption_password,
            backend=self.backend,
        )
        return rs.restore(
            folder_uuid=self.folder_uuid,
            computer_uuid=self.computer_uuid,
            dest=self.dest,
            backuprecord_path=self.backuprecord_path,
            paths=self.paths,
            callback=self._emit,
            plan_totals=True,
        )


# ---------------------------------------------------------------------------
# Validate
# ---------------------------------------------------------------------------


class ValidateWorker(_BaseWorker):
    """Drive ``arq_validator.validate`` from a worker thread."""

    def __init__(
        self, target, *,
        backend,
        root: str = "/",
        tier: str = "quick",
        password: Optional[str] = None,
        audit_skip_larger_than: Optional[int] = None,
        audit_max_runtime_sec: Optional[float] = None,
        audit_max_bytes: Optional[int] = None,
    ) -> None:
        super().__init__(target)
        self.backend = backend
        self.root = root
        self.tier = tier
        self.password = password
        self.audit_skip_larger_than = audit_skip_larger_than
        self.audit_max_runtime_sec = audit_max_runtime_sec
        self.audit_max_bytes = audit_max_bytes

    def _run(self):
        from arq_validator import ValidationTier, validate
        # Validator's ProgressCallback shape is Callable[[Event],
        # None] (single Event arg with .kind / .message / .payload),
        # not (kind, payload) like writer / reader. Adapt here so
        # the rest of the bridge keeps a uniform message surface.
        def _adapt(event):
            payload = dict(event.payload)
            if event.message:
                payload.setdefault("message", event.message)
            self._emit(event.kind.value, payload)
        return validate(
            backend=self.backend,
            root=self.root,
            tier=ValidationTier(self.tier),
            encryption_password=self.password,
            audit_skip_larger_than=self.audit_skip_larger_than,
            audit_max_runtime_sec=self.audit_max_runtime_sec,
            audit_max_bytes=self.audit_max_bytes,
            callback=_adapt,
        )
