"""Run a single :class:`Plan` against every destination it lists.

Plans normally back up to one destination (the legacy
``Plan.destination`` + ``Plan.destination_kind``). For
operators who want belt-and-suspenders durability — e.g.
"NAS first, then off-site SFTP mirror" — :class:`Plan` also
carries an ``additional_destinations`` list. This module is the
sequential runner that drives one Backup per destination so a
single failure doesn't drop the others.

Sequential, not parallel, because:

1. The writer's per-destination state (cache files, prior-tree
   index, packed buffers) lives on disk + isn't safe to share
   across processes touching the same dest_root.
2. Network-wise, parallel SFTP uploads to two destinations
   would each fight for the operator's outgoing bandwidth.
3. Per-destination retries (the SFTP backoff) work better when
   one destination's slowness doesn't backpressure another.

The runner returns a :class:`MultiBackupResult` reporting per-
destination status so a TUI / cron-wrapper can surface partial
success ("destination A succeeded, B failed: <reason>").
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, List, Optional


@dataclass
class PerDestinationOutcome:
    """One destination's outcome from :func:`run_plan_multi`."""

    index: int                  # 0-based position in iter_destinations()
    kind: str                   # "local" | "sftp"
    label: str                  # operator-facing identifier
    ok: bool
    elapsed_sec: float = 0.0
    files_written: int = 0
    files_reused: int = 0
    bytes_plaintext: int = 0
    error: Optional[str] = None


@dataclass
class MultiBackupResult:
    """Top-level result of running one plan against N destinations."""

    plan_id: str = ""
    plan_name: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    destinations: List[PerDestinationOutcome] = field(default_factory=list)

    @property
    def all_ok(self) -> bool:
        return bool(self.destinations) and all(
            d.ok for d in self.destinations
        )

    @property
    def any_failed(self) -> bool:
        return any(not d.ok for d in self.destinations)


def run_plan_multi(
    plan,
    *,
    encryption_password: str,
    callback: Optional[Callable[[str, dict], None]] = None,
    chunker_config=None,
    exclusions=None,
    openssl_path: str = "openssl",
    backend_factory: Optional[
        Callable[[dict], Any]
    ] = None,
) -> MultiBackupResult:
    """Run ``plan`` once per destination (in iteration order).

    ``backend_factory`` is injected for testability — production
    callers leave it as None and let the helper open a fresh
    LocalBackend / SftpBackend per destination dict; tests pass
    a stub that returns a temp-dir-rooted LocalBackend.

    Per-destination failures are caught + recorded on the result
    rather than re-raised so a partial success on N>1 destinations
    is still observable. The ``callback`` (if supplied) gets
    ``destination_started`` / ``destination_finished`` events at
    the boundaries so a TUI can render per-destination progress.
    """
    from arq_writer import Backup
    result = MultiBackupResult(
        plan_id=plan.plan_id, plan_name=plan.name,
        started_at=time.time(),
    )
    sources = [Path(s) for s in plan.sources]
    for i, dest in enumerate(plan.iter_destinations()):
        kind = dest.get("kind", "local")
        label = (
            f"sftp://{dest.get('user', '')}@{dest.get('host', '')}"
            f"{dest.get('path', '')}"
            if kind == "sftp" else str(dest.get("path", ""))
        )
        if callback is not None:
            try:
                callback("destination_started",
                         {"index": i, "kind": kind, "label": label})
            except Exception:
                pass
        outcome = PerDestinationOutcome(
            index=i, kind=kind, label=label, ok=False,
        )
        started = time.time()
        try:
            backend = (
                backend_factory(dest)
                if backend_factory is not None
                else _default_backend(dest)
            )
            try:
                bk = Backup(
                    dest_root=Path("/") if kind == "sftp"
                    else Path(dest.get("path") or ""),
                    encryption_password=encryption_password,
                    backup_name=plan.name or "multi-dest",
                    callback=callback,
                    openssl_path=openssl_path,
                    use_packs=plan.use_packs,
                    chunker_config=chunker_config,
                    dedup_against_existing=plan.dedup_against_existing,
                    max_file_bytes=plan.max_file_bytes,
                    exclusions=exclusions,
                    backend=backend if kind == "sftp" else None,
                )
                bk.init_plan()
                for src in sources:
                    bk.add_folder(src)
                outcome.files_written = bk.files_written
                outcome.files_reused = bk.files_reused
                outcome.bytes_plaintext = bk.bytes_plaintext
                outcome.ok = True
            finally:
                if backend is not None and hasattr(
                    backend, "__exit__",
                ):
                    try:
                        backend.__exit__(None, None, None)
                    except Exception:
                        pass
        except Exception as exc:
            outcome.error = f"{type(exc).__name__}: {exc}"
        outcome.elapsed_sec = time.time() - started
        result.destinations.append(outcome)
        if callback is not None:
            try:
                callback("destination_finished",
                         {"index": i, "ok": outcome.ok,
                          "error": outcome.error})
            except Exception:
                pass
    result.finished_at = time.time()
    return result


def _default_backend(dest: dict):
    """Open the right backend type for ``dest``. Returns None for
    local (Backup creates LocalBackend internally); returns an
    open SftpBackend for sftp."""
    if dest.get("kind") == "sftp":
        from arq_validator.sftp import SftpBackend
        backend = SftpBackend(
            dest.get("host", ""),
            port=int(dest.get("port") or 22),
            user=dest.get("user", ""),
            password=dest.get("password"),
            identity_file=dest.get("identity_file"),
            root=dest.get("path", ""),
        )
        backend.__enter__()
        return backend
    return None
