"""Resumable audit-drip orchestrator.

The L2 ``run_full_audit`` tier verifies every EncryptedObject in one
pass. On large destinations this can take many hours and (over remote
backends) burn a lot of bandwidth, so the operator typically wants to
spread the work across many short fires — for example, 30 minutes
nightly. Audit-drip provides exactly that:

- Walks the deterministic ``(computer, family, shard, file_name)``
  order, persisting the cursor after each processed file.
- Stops at a soft time budget; the next fire resumes after the cursor.
- Marks ``sweep_completed_at`` when the walk reaches the end; the next
  fire starts a fresh sweep with a bumped ``sweep_count``.
- Optional pause via ``paused_until_epoch`` so an operator can suspend
  the sweep without editing scripts.
- Optional rate throttle (files per minute) for remote backends with
  per-IP connection ceilings.

State is a single JSON file per target. ``target`` is a free-form
label (``"local"`` / ``"hetzner"`` etc.) that lets concurrent sweeps
to different destinations keep separate cursors without conflict.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import constants as C
from . import layout as L
from .backend import Backend
from .crypto import Keyset, decrypt_keyset
from .events import EventKind, ProgressCallback, emit
from .tiers import (
    AUDIT_DEFAULT_SKIP_LARGER_THAN,
    ObjectAuditResult,
    _audit_one_file,
)


@dataclass
class AuditDripState:
    """Persistent cursor + per-sweep accumulators for audit-drip.

    ``cursor_*`` together identify the LAST file successfully processed
    (inclusive). On the next fire, the walk resumes immediately AFTER
    this position. ``cursor_kind == None`` plus
    ``sweep_completed_at != None`` marks the boundary "between sweeps":
    the next fire kicks off a fresh sweep with a new ``sweep_count``.
    """

    target: str = ""
    sweep_started_at: float = 0.0
    sweep_completed_at: Optional[float] = None
    sweep_count: int = 0
    cursor_computer: Optional[str] = None
    cursor_kind: Optional[str] = None
    cursor_shard: Optional[str] = None
    cursor_file_name: Optional[str] = None
    files_audited_this_sweep: int = 0
    files_total_this_sweep: int = 0
    bytes_audited_this_sweep: int = 0
    inner_arqos_audited_this_sweep: int = 0
    inner_arqos_ok_this_sweep: int = 0
    inner_arqos_fail_this_sweep: int = 0
    fails_this_sweep: int = 0
    errors_this_sweep: int = 0
    skipped_this_sweep: int = 0
    failed_files_this_sweep: List[Dict[str, str]] = field(default_factory=list)
    last_fire_started_at: float = 0.0
    last_fire_finished_at: float = 0.0
    last_fire_elapsed_sec: float = 0.0
    last_fire_files_processed: int = 0
    last_fire_aborted_reason: Optional[str] = None
    last_fire_keyset_decrypted: Optional[bool] = None
    paused_until_epoch: Optional[float] = None
    error: Optional[str] = None


class Throttle:
    """Minimum inter-event spacing.

    Pass ``files_per_min=None`` (or ``0``) for free-running. Used by
    audit-drip on rate-limited remote backends (Hetzner SFTP caps
    connect-rate per source IP).
    """

    def __init__(self, files_per_min: Optional[float]) -> None:
        self.interval = (
            60.0 / files_per_min
            if files_per_min and files_per_min > 0 else 0.0
        )
        self._next_at: float = 0.0

    def wait(self) -> None:
        if self.interval <= 0:
            return
        now = time.monotonic()
        if self._next_at == 0.0:
            self._next_at = now + self.interval
            return
        delta = self._next_at - now
        if delta > 0:
            time.sleep(delta)
        self._next_at = max(now, self._next_at) + self.interval


def load_state(state_file: Path, target: str) -> AuditDripState:
    """Load existing state or return a fresh ``AuditDripState``.

    Corrupt or schema-drifted state files are silently replaced with
    a fresh state so a single bad fire can't permanently wedge the
    schedule. Operators who care about the old cursor can restore from
    a backup before the next fire.
    """
    if state_file.exists():
        try:
            data = json.loads(state_file.read_text())
            data.setdefault("target", target)
            return AuditDripState(**data)
        except Exception:
            pass
    return AuditDripState(target=target)


def save_state(state: AuditDripState, state_file: Path) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(
        json.dumps(asdict(state), indent=2, ensure_ascii=False)
    )


def build_walk(
    layouts: List[L.Arq7ComputerLayout],
) -> List[Tuple[str, str, str, str]]:
    """Deterministic walk: ``(computer, family, shard, file_name)``.

    Stable across runs so cursor-based resume is meaningful even if
    the underlying directories grow or shrink between fires.
    """
    out: List[Tuple[str, str, str, str]] = []
    for lay in sorted(layouts, key=lambda x: x.computer_uuid):
        for kind in C.OBJECT_FAMILIES:
            for shard, name in sorted(lay.family_items(kind)):
                out.append((lay.computer_uuid, kind, shard, name))
    return out


def _index_after_cursor(
    walk: List[Tuple[str, str, str, str]],
    state: AuditDripState,
) -> int:
    """Find the first walk index strictly AFTER the cursor.

    If the cursor file disappeared between fires, fall back to the
    smallest index whose tuple sorts strictly greater than the cursor.
    Returns ``len(walk)`` when nothing past the cursor remains
    (sweep is effectively complete).
    """
    if state.cursor_kind is None:
        return 0
    cursor = (
        state.cursor_computer or "",
        state.cursor_kind,
        state.cursor_shard or "",
        state.cursor_file_name or "",
    )
    # Linear scan suffices: walks are dominated by I/O, and even at
    # 500K files this is a few ms.
    for i, t in enumerate(walk):
        if t > cursor:
            return i
    return len(walk)


def _decrypt_first_keyset(
    backend: Backend,
    layouts: List[L.Arq7ComputerLayout],
    root: str,
    encryption_password: str,
    *,
    openssl_path: str,
) -> Tuple[Optional[Keyset], Optional[str]]:
    """Decrypt the first available keyset; return ``(keyset, error)``."""
    last_err: Optional[str] = None
    for lay in layouts:
        kp = L.keyset_path(root, lay.computer_uuid)
        try:
            blob = backend.read_all(kp)
            return decrypt_keyset(
                blob, encryption_password, openssl_path=openssl_path,
            ), None
        except Exception as exc:
            last_err = (
                f"keyset decrypt failed for {lay.computer_uuid}: "
                f"{type(exc).__name__}: {exc}"
            )
    return None, last_err or "no computers found"


def run_audit_drip(
    backend: Backend,
    *,
    target: str,
    state_file: Path,
    encryption_password: str,
    root: str = "/",
    max_runtime_sec: int = 0,
    rate_files_per_min: Optional[float] = None,
    skip_larger_than: Optional[int] = AUDIT_DEFAULT_SKIP_LARGER_THAN,
    discover_concurrency: int = 8,
    failed_list_cap: int = 100,
    openssl_path: str = "openssl",
    callback: Optional[ProgressCallback] = None,
) -> AuditDripState:
    """Run one fire of the resumable L2 audit.

    ``max_runtime_sec=0`` removes the time cap (run to completion).
    Returns the persisted :class:`AuditDripState` so callers can
    inspect cursor / counters / errors without reloading the file.
    """
    state = load_state(state_file, target)
    state.target = target

    now = time.time()
    pu = state.paused_until_epoch
    if pu is not None and (pu == -1 or now < pu):
        state.last_fire_started_at = now
        state.last_fire_finished_at = now
        state.last_fire_elapsed_sec = 0.0
        state.last_fire_files_processed = 0
        state.last_fire_aborted_reason = "paused"
        save_state(state, state_file)
        emit(
            callback, EventKind.AUDIT_DRIP_PAUSED,
            f"audit-drip {target}: paused, skipping fire",
            target=target, paused_until_epoch=pu,
        )
        return state

    state.last_fire_started_at = now
    state.last_fire_aborted_reason = None
    state.last_fire_files_processed = 0
    state.error = None
    deadline = (
        time.monotonic() + max_runtime_sec
        if max_runtime_sec and max_runtime_sec > 0 else None
    )
    throttle = Throttle(rate_files_per_min)

    emit(
        callback, EventKind.AUDIT_DRIP_FIRE_STARTED,
        f"audit-drip {target}: fire started",
        target=target,
        max_runtime_sec=max_runtime_sec,
        rate_files_per_min=rate_files_per_min,
    )

    aborted: Optional[str] = None
    try:
        layouts = L.discover_layout(
            backend, root, concurrency=discover_concurrency,
        )
        if not layouts:
            state.error = (
                "no computer UUIDs discovered (check root + destination)"
            )
            _finish(state, state_file, callback, target)
            return state

        keyset, kerr = _decrypt_first_keyset(
            backend, layouts, root, encryption_password,
            openssl_path=openssl_path,
        )
        if keyset is None:
            state.last_fire_keyset_decrypted = False
            state.error = kerr
            emit(
                callback, EventKind.KEYSET_FAILED, kerr or "keyset failed",
                target=target, error=kerr,
            )
            _finish(state, state_file, callback, target)
            return state
        state.last_fire_keyset_decrypted = True
        emit(
            callback, EventKind.KEYSET_DECRYPTED,
            f"keyset decrypted for audit-drip {target}",
            target=target,
        )

        walk = build_walk(layouts)
        if not walk:
            # Nothing to audit — degenerate completed sweep.
            state.cursor_computer = None
            state.cursor_kind = None
            state.cursor_shard = None
            state.cursor_file_name = None
            state.sweep_completed_at = time.time()
            _finish(state, state_file, callback, target)
            return state

        # Sweep boundary: previous sweep completed (or first ever fire)
        # → reset accumulators and bump sweep_count.
        if state.sweep_completed_at is not None or state.sweep_started_at == 0.0:
            state.sweep_count += 1
            state.sweep_started_at = time.time()
            state.sweep_completed_at = None
            state.cursor_computer = None
            state.cursor_kind = None
            state.cursor_shard = None
            state.cursor_file_name = None
            state.files_audited_this_sweep = 0
            state.files_total_this_sweep = len(walk)
            state.bytes_audited_this_sweep = 0
            state.inner_arqos_audited_this_sweep = 0
            state.inner_arqos_ok_this_sweep = 0
            state.inner_arqos_fail_this_sweep = 0
            state.fails_this_sweep = 0
            state.errors_this_sweep = 0
            state.skipped_this_sweep = 0
            state.failed_files_this_sweep = []
            emit(
                callback, EventKind.AUDIT_DRIP_SWEEP_STARTED,
                f"audit-drip {target}: sweep #{state.sweep_count} started",
                target=target,
                sweep_count=state.sweep_count,
                files_total=state.files_total_this_sweep,
            )

        start_idx = _index_after_cursor(walk, state)
        if start_idx >= len(walk):
            # Cursor already past end — degenerate "complete" fire.
            state.sweep_completed_at = time.time()
            emit(
                callback, EventKind.AUDIT_DRIP_SWEEP_COMPLETED,
                f"audit-drip {target}: sweep #{state.sweep_count} complete",
                target=target, sweep_count=state.sweep_count,
            )
            _finish(state, state_file, callback, target)
            return state

        scratch = ObjectAuditResult()
        for idx in range(start_idx, len(walk)):
            cu, kind, shard, file_name = walk[idx]
            if deadline is not None and time.monotonic() >= deadline:
                aborted = "max_runtime"
                break
            throttle.wait()
            abs_path = L.object_path(root, cu, kind, shard, file_name)

            pre_total = scratch.files_total
            pre_ok = scratch.files_ok
            pre_fail = scratch.files_fail
            pre_err = scratch.files_error
            pre_skip = scratch.files_skipped
            pre_bytes = scratch.bytes_read
            pre_iarqo = scratch.inner_arqos_total
            pre_iok = scratch.inner_arqos_ok
            pre_ifail = scratch.inner_arqos_fail
            pre_fails = len(scratch.failures)

            _audit_one_file(
                backend, keyset, cu, kind, shard, file_name, abs_path,
                skip_larger_than, scratch, callback,
            )

            state.files_audited_this_sweep += scratch.files_total - pre_total
            state.bytes_audited_this_sweep += scratch.bytes_read - pre_bytes
            state.inner_arqos_audited_this_sweep += (
                scratch.inner_arqos_total - pre_iarqo
            )
            state.inner_arqos_ok_this_sweep += scratch.inner_arqos_ok - pre_iok
            state.inner_arqos_fail_this_sweep += (
                scratch.inner_arqos_fail - pre_ifail
            )
            if scratch.files_fail > pre_fail:
                state.fails_this_sweep += 1
            if scratch.files_error > pre_err:
                state.errors_this_sweep += 1
            if scratch.files_skipped > pre_skip:
                state.skipped_this_sweep += 1
            if len(scratch.failures) > pre_fails:
                state.failed_files_this_sweep.extend(
                    scratch.failures[pre_fails:]
                )
                if len(state.failed_files_this_sweep) > failed_list_cap:
                    state.failed_files_this_sweep = (
                        state.failed_files_this_sweep[:failed_list_cap]
                    )
            state.last_fire_files_processed += 1

            # Cursor advances on every processed file (success / fail /
            # error / skip) so forward progress is monotonic.
            state.cursor_computer = cu
            state.cursor_kind = kind
            state.cursor_shard = shard
            state.cursor_file_name = file_name

            emit(
                callback, EventKind.AUDIT_DRIP_PROGRESS,
                (f"audit-drip {target}: "
                 f"{state.files_audited_this_sweep}/"
                 f"{state.files_total_this_sweep}"),
                target=target,
                files_audited=state.files_audited_this_sweep,
                files_total=state.files_total_this_sweep,
                fails=state.fails_this_sweep,
                errors=state.errors_this_sweep,
                cursor_computer=cu,
                cursor_kind=kind,
                cursor_shard=shard,
                cursor_file_name=file_name,
            )
        else:
            # Loop ran to completion without break → sweep done.
            state.sweep_completed_at = time.time()
            state.cursor_computer = None
            state.cursor_kind = None
            state.cursor_shard = None
            state.cursor_file_name = None
            emit(
                callback, EventKind.AUDIT_DRIP_SWEEP_COMPLETED,
                f"audit-drip {target}: sweep #{state.sweep_count} complete",
                target=target, sweep_count=state.sweep_count,
                files_audited=state.files_audited_this_sweep,
                fails=state.fails_this_sweep,
                errors=state.errors_this_sweep,
            )

        del keyset
        state.last_fire_aborted_reason = aborted
        if aborted:
            emit(
                callback, EventKind.AUDIT_DRIP_ABORTED,
                f"audit-drip {target}: aborted ({aborted})",
                target=target, reason=aborted,
            )

    except Exception as exc:
        state.error = f"{type(exc).__name__}: {exc}"

    _finish(state, state_file, callback, target)
    return state


def _finish(
    state: AuditDripState,
    state_file: Path,
    callback: Optional[ProgressCallback],
    target: str,
) -> None:
    state.last_fire_finished_at = time.time()
    state.last_fire_elapsed_sec = (
        state.last_fire_finished_at - state.last_fire_started_at
    )
    save_state(state, state_file)
    emit(
        callback, EventKind.AUDIT_DRIP_FIRE_FINISHED,
        f"audit-drip {target}: fire finished in "
        f"{state.last_fire_elapsed_sec:.2f}s",
        target=target,
        elapsed_sec=state.last_fire_elapsed_sec,
        files_processed=state.last_fire_files_processed,
        aborted_reason=state.last_fire_aborted_reason,
        error=state.error,
    )


def pause(state_file: Path, target: str, *, until_epoch: float) -> AuditDripState:
    """Set ``paused_until_epoch`` so subsequent fires silent-skip.

    Pass ``until_epoch=-1`` to pause indefinitely. The next call to
    :func:`run_audit_drip` after the pause window expires will resume
    normally.
    """
    state = load_state(state_file, target)
    state.target = target
    state.paused_until_epoch = until_epoch
    save_state(state, state_file)
    return state


def resume(state_file: Path, target: str) -> AuditDripState:
    """Clear any pause so the next fire runs."""
    state = load_state(state_file, target)
    state.target = target
    state.paused_until_epoch = None
    save_state(state, state_file)
    return state
