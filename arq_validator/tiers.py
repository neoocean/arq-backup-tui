"""Validation tiers: L0 layout, L1a magic, L1b head HMAC, L2 full audit.

Each tier is a stateless function that takes a backend, the discovered
layout, and tier-specific options, and returns a result dataclass.
Progress is reported through the optional ``ProgressCallback``.
"""

from __future__ import annotations

import random
import threading
import time
from concurrent.futures import (
    FIRST_COMPLETED, Future, ThreadPoolExecutor, as_completed, wait,
)
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import constants as C
from . import layout as L
from .backend import Backend
from .crypto import (
    Keyset,
    decrypt_keyset,
    verify_encrypted_object_hmac,
    verify_multi_object_arqos,
)
from .events import EventKind, ProgressCallback, emit


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class LayoutResult:
    """L0 outcome: did we find a valid Arq 7 layout?"""

    layout_ok: bool = False
    computer_uuids: List[str] = field(default_factory=list)
    blobpack_count: int = 0
    treepack_count: int = 0
    largeblobpack_count: int = 0
    standardobject_count: int = 0
    backup_folder_count: int = 0
    missing_keyset_for: List[str] = field(default_factory=list)


@dataclass
class MagicCheckResult:
    """L1a outcome: ARQO magic-byte sample sweep."""

    total: int = 0
    ok: int = 0
    fail: int = 0
    failures: List[Dict[str, str]] = field(default_factory=list)
    sample_fraction: float = 1.0


@dataclass
class BackupRecordResult:
    """L1b outcome: keyset decrypt + per-folder latest-backuprecord HMAC."""

    keyset_decrypted: bool = False
    keyset_error: Optional[str] = None
    total: int = 0
    ok: int = 0
    fail: int = 0
    failures: List[Dict[str, str]] = field(default_factory=list)


@dataclass
class GraphCheckResult:
    """L3 outcome: per-backuprecord blob-graph consistency.

    L3 walks every backuprecord (not just the latest, like L1b)
    and follows its tree → file-node → blob-loc chain end-to-end.
    Catches defects that the cheaper tiers miss:

      - Tree node references a blob that no longer exists on
        disk (orphaned reference / partial GC).  L2 doesn't
        notice because L2 audits files that ARE present; it
        never asks "was anything expected to be here?"
      - Bit-rot on a blob that L1b's latest-record sweep
        skipped because it lives in an older record.

    The check delegates per-record work to
    :func:`record_validator.validate_record` and aggregates the
    results into one structured report.  Failures are dedupped
    by ``blob_id`` so a blob shared across N records doesn't
    appear N times in the operator-visible failure list.
    """

    records_checked: int = 0
    records_ok: int = 0
    records_fail: int = 0
    # Unique blob count (after dedup across records); raw
    # blob-walk count is in `blob_walks_total` so the operator
    # can see "we walked 50000 blob refs but only 12000 unique
    # blobs" — high dedup is the normal case for snapshot-y
    # destinations.
    blobs_unique: int = 0
    blob_walks_total: int = 0
    blobs_missing: int = 0
    blobs_hmac_fail: int = 0
    blobs_decode_fail: int = 0
    bytes_fetched: int = 0
    # Unique-by-blob_id failure entries (kind = "missing" /
    # "hmac" / "decode" / "fetch").  See
    # :class:`RecordValidationFailure` for the schema.
    failures: List[Dict[str, str]] = field(default_factory=list)
    aborted_reason: Optional[str] = None
    # Wall time spent in L3 (same shape as ObjectAuditResult).
    started_at: float = 0.0
    planned_records: int = 0


@dataclass
class ObjectAuditResult:
    """L2 outcome: full HMAC sweep over every EncryptedObject."""

    files_total: int = 0
    files_ok: int = 0
    files_fail: int = 0
    files_error: int = 0
    files_skipped: int = 0
    # Counted separately from ``files_skipped`` (size cap) so an
    # operator's incremental sweep summary distinguishes "the
    # blob was huge, we skipped it" from "the ledger says we
    # already audited this blob recently". See
    # arq_validator.incremental_audit.AuditLedger.
    files_skipped_by_ledger: int = 0
    inner_arqos_total: int = 0
    inner_arqos_ok: int = 0
    inner_arqos_fail: int = 0
    bytes_read: int = 0
    failures: List[Dict[str, str]] = field(default_factory=list)
    aborted_reason: Optional[str] = None
    # Backup sets (computer-UUIDs) skipped — not failed — because
    # their keyset is absent (unencrypted set) or doesn't open with
    # the supplied password (a different-password set). Each entry
    # "<cu>: <reason>". Same multi-set guard as audit-drip's
    # _decrypt_keysets_per_cu (a destination can host several backup
    # sets with different / no encryption).
    skipped_backup_sets: List[str] = field(default_factory=list)
    # Operator-visible progress fields (§ AUDIT_PROGRESS ETA, CL
    # TBD 2026-05-12).  ``started_at`` is set when
    # ``run_full_audit`` begins; ``planned_files`` is computed
    # from layout sizes at the same time.  The driver uses both
    # to derive elapsed / throughput / ETA in AUDIT_PROGRESS
    # events without forcing every UI to track its own
    # wallclock.
    started_at: float = 0.0
    planned_files: int = 0


# ---------------------------------------------------------------------------
# L0 — layout sanity
# ---------------------------------------------------------------------------


def run_layout_check(
    layouts: List[L.Arq7ComputerLayout],
    *,
    callback: Optional[ProgressCallback] = None,
) -> LayoutResult:
    """L0: assert at least one computer UUID with at least one object."""
    emit(callback, EventKind.TIER_STARTED, "L0 layout check", tier="L0")
    result = LayoutResult()
    for lay in layouts:
        result.computer_uuids.append(lay.computer_uuid)
        result.blobpack_count += len(lay.blobpacks)
        result.treepack_count += len(lay.treepacks)
        result.largeblobpack_count += len(lay.largeblobpacks)
        result.standardobject_count += len(lay.standardobjects)
        result.backup_folder_count += len(lay.backup_folder_uuids)
        if not lay.has_keyset:
            result.missing_keyset_for.append(lay.computer_uuid)
    total_objects = (
        result.blobpack_count
        + result.treepack_count
        + result.largeblobpack_count
        + result.standardobject_count
    )
    result.layout_ok = bool(result.computer_uuids) and total_objects > 0
    emit(
        callback, EventKind.TIER_FINISHED, "L0 layout check finished",
        tier="L0", layout_ok=result.layout_ok,
        computer_count=len(result.computer_uuids),
        total_objects=total_objects,
    )
    return result


# ---------------------------------------------------------------------------
# L1a — ARQO magic-byte sweep
# ---------------------------------------------------------------------------


def _select_magic_targets(
    layouts: List[L.Arq7ComputerLayout],
    root: str,
    sample_fraction: float,
    rng: Optional[random.Random] = None,
) -> List[Tuple[str, str, str, str, str]]:
    """Return ``(computer, kind, shard, file, abs_path)`` tuples to check."""
    pool: List[Tuple[str, str, str, str, str]] = []
    for lay in layouts:
        for kind in C.OBJECT_FAMILIES:
            for shard, name in lay.family_items(kind):
                pool.append((
                    lay.computer_uuid,
                    kind,
                    shard,
                    name,
                    L.object_path(root, lay.computer_uuid, kind, shard, name),
                ))
    if sample_fraction <= 0:
        return []
    if sample_fraction >= 1.0 or len(pool) == 0:
        return pool
    n = max(1, int(len(pool) * sample_fraction))
    if n >= len(pool):
        return pool
    return (rng or random).sample(pool, n)


def run_magic_check(
    backend: Backend,
    layouts: List[L.Arq7ComputerLayout],
    *,
    root: str = "/",
    sample_fraction: float = 0.05,
    concurrency: int = 4,
    progress_every: int = 500,
    callback: Optional[ProgressCallback] = None,
    rng: Optional[random.Random] = None,
) -> MagicCheckResult:
    """L1a: fetch the first 4 bytes of a sample of object files and
    assert they equal ``b"ARQO"``.

    Sampling defaults to 5% of object files — sufficient to detect
    population-scale corruption without paying the wall-clock cost of
    a full sweep on multi-hundred-thousand-object backups. Pass
    ``sample_fraction=1.0`` for an exhaustive sweep.
    """
    emit(callback, EventKind.TIER_STARTED, "L1a magic check", tier="L1a")
    targets = _select_magic_targets(layouts, root, sample_fraction, rng)
    result = MagicCheckResult(sample_fraction=sample_fraction)

    def _check_one(t):
        cu, kind, shard, name, path = t
        try:
            head = backend.read_range(path, 0, 4)
            return cu, kind, shard, name, head, None
        except Exception as exc:
            return cu, kind, shard, name, None, f"{type(exc).__name__}: {exc}"

    if not targets:
        emit(
            callback, EventKind.TIER_FINISHED,
            "L1a magic check finished (empty sample)",
            tier="L1a", total=0, fail=0,
        )
        return result

    if concurrency <= 1:
        iterator = (_check_one(t) for t in targets)
    else:
        ex = ThreadPoolExecutor(max_workers=concurrency)
        futures = [ex.submit(_check_one, t) for t in targets]
        iterator = (fut.result() for fut in as_completed(futures))

    try:
        for cu, kind, shard, name, head, err in iterator:
            result.total += 1
            if err is not None:
                result.fail += 1
                result.failures.append({
                    "computer": cu, "kind": kind, "shard": shard,
                    "file_name": name, "error": err[:200],
                })
                emit(
                    callback, EventKind.MAGIC_CHECK_FAILED,
                    f"magic check failed: {kind}/{shard}/{name}",
                    computer=cu, family=kind, shard=shard,
                    file_name=name, error=err[:200],
                )
            elif head[:4] != C.ARQO_MAGIC:
                result.fail += 1
                err_text = f"bad magic: {head[:4]!r} hex={head[:4].hex()}"
                result.failures.append({
                    "computer": cu, "kind": kind, "shard": shard,
                    "file_name": name, "error": err_text,
                })
                emit(
                    callback, EventKind.MAGIC_CHECK_FAILED,
                    f"magic check bad bytes: {kind}/{shard}/{name}",
                    computer=cu, family=kind, shard=shard,
                    file_name=name, error=err_text,
                )
            else:
                result.ok += 1
            if result.total % progress_every == 0:
                emit(
                    callback, EventKind.MAGIC_CHECK_PROGRESS,
                    f"{result.total}/{len(targets)} files checked",
                    total=result.total,
                    target_total=len(targets),
                    fail=result.fail,
                )
    finally:
        if concurrency > 1:
            ex.shutdown(wait=True)

    emit(
        callback, EventKind.TIER_FINISHED,
        f"L1a magic check finished: {result.ok}/{result.total} OK",
        tier="L1a", total=result.total, ok=result.ok, fail=result.fail,
    )
    return result


# ---------------------------------------------------------------------------
# L1b — keyset decrypt + latest backuprecord HMAC
# ---------------------------------------------------------------------------


# Cap on backuprecord size we'll fetch. Real records are <100 KB; the
# 50 MB safety net catches pathological growth without blowing up.
BACKUPRECORD_MAX_BYTES = 50 * 1024 * 1024


def _verify_latest_backuprecord(
    backend: Backend, root: str, computer_uuid: str,
    folder_uuid: str, keyset: Keyset,
) -> Tuple[bool, Optional[str], int]:
    """Returns ``(hmac_ok, error, size_read)``."""
    path = L.find_latest_backuprecord(
        backend, root, computer_uuid, folder_uuid,
    )
    if path is None:
        return False, "no backuprecord found", 0
    try:
        size = backend.stat_size(path)
    except Exception as exc:
        return False, f"stat: {type(exc).__name__}: {exc}", 0
    if size > BACKUPRECORD_MAX_BYTES:
        return False, (
            f"backuprecord unexpectedly large: {size:,} bytes "
            f"> cap {BACKUPRECORD_MAX_BYTES:,}"
        ), size
    try:
        body = backend.read_all(path)
    except Exception as exc:
        return False, f"fetch: {type(exc).__name__}: {exc}", 0
    if len(body) != size:
        return False, f"short read: expected {size}, got {len(body)}", len(body)
    if body[: len(C.ARQO_MAGIC)] != C.ARQO_MAGIC:
        return False, f"missing ARQO magic: {body[:4]!r}", len(body)
    ok, _, _ = verify_encrypted_object_hmac(body, keyset.hmac_key)
    if not ok:
        return False, "HMAC mismatch", len(body)
    return True, None, len(body)


def run_backuprecord_check(
    backend: Backend,
    layouts: List[L.Arq7ComputerLayout],
    encryption_password: str,
    *,
    root: str = "/",
    openssl_path: str = "openssl",
    callback: Optional[ProgressCallback] = None,
) -> BackupRecordResult:
    """L1b: decrypt each computer's keyset, then HMAC-verify the most
    recent backuprecord of every backup folder.

    A failure of any keyset decrypt aborts the tier early — without
    the keyset we can't verify any HMAC.
    """
    emit(callback, EventKind.TIER_STARTED, "L1b backuprecord check", tier="L1b")
    result = BackupRecordResult()
    for lay in layouts:
        kp = L.keyset_path(root, lay.computer_uuid)
        try:
            keyset_bytes = backend.read_all(kp)
        except Exception as exc:
            result.keyset_decrypted = False
            result.keyset_error = (
                f"keyset read failed for {lay.computer_uuid}: "
                f"{type(exc).__name__}: {exc}"
            )
            emit(
                callback, EventKind.KEYSET_FAILED, result.keyset_error,
                computer=lay.computer_uuid, error=str(exc),
            )
            return result
        try:
            keyset = decrypt_keyset(
                keyset_bytes, encryption_password, openssl_path=openssl_path,
            )
        except Exception as exc:
            result.keyset_decrypted = False
            result.keyset_error = (
                f"keyset decrypt failed for {lay.computer_uuid}: "
                f"{type(exc).__name__}: {exc}"
            )
            emit(
                callback, EventKind.KEYSET_FAILED, result.keyset_error,
                computer=lay.computer_uuid, error=str(exc),
            )
            return result

        result.keyset_decrypted = True
        emit(
            callback, EventKind.KEYSET_DECRYPTED,
            f"keyset decrypted for {lay.computer_uuid}",
            computer=lay.computer_uuid,
        )

        for folder_uuid in lay.backup_folder_uuids:
            result.total += 1
            ok, err, _size = _verify_latest_backuprecord(
                backend, root, lay.computer_uuid, folder_uuid, keyset,
            )
            if ok:
                result.ok += 1
                emit(
                    callback, EventKind.BACKUPRECORD_VERIFIED,
                    f"backuprecord verified: {folder_uuid}",
                    computer=lay.computer_uuid, folder_uuid=folder_uuid,
                )
            else:
                result.fail += 1
                result.failures.append({
                    "computer": lay.computer_uuid,
                    "folder_uuid": folder_uuid,
                    "error": err or "unknown",
                })
                emit(
                    callback, EventKind.BACKUPRECORD_FAILED,
                    f"backuprecord failed: {folder_uuid}: {err}",
                    computer=lay.computer_uuid, folder_uuid=folder_uuid,
                    error=err,
                )

        del keyset

    emit(
        callback, EventKind.TIER_FINISHED,
        f"L1b backuprecord check finished: {result.ok}/{result.total} OK",
        tier="L1b", total=result.total, ok=result.ok, fail=result.fail,
    )
    return result


# ---------------------------------------------------------------------------
# L2 — full HMAC audit
# ---------------------------------------------------------------------------


# Default skip threshold: matches Arq's ``maxPackedItemLength=256000``.
# Files larger than this are typically multi-object containers; we
# still verify them via ``verify_multi_object_arqos`` but the cap
# protects against pathological single-file sizes blowing memory.
AUDIT_DEFAULT_SKIP_LARGER_THAN = 256 * 1024


def _format_duration(seconds: float) -> str:
    """Render a duration as ``H:MM:SS`` (or ``M:SS`` for short
    spans).  Used by AUDIT_PROGRESS ETA strings; chosen over
    ``timedelta.__str__`` because that produces ``"0:01:23.456789"``
    which is hard to read in a chat message.
    """
    if seconds < 0:
        seconds = 0.0
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


# ---------------------------------------------------------------------------
# Per-file audit work — pure compute + lock-free delta merge
# ---------------------------------------------------------------------------


@dataclass
class _AuditDelta:
    """Per-file outcome ready to merge into an ``ObjectAuditResult``.

    ``_audit_one_file_compute`` builds + returns one of these and emits
    the file-level callback events itself; the caller (whether
    sequential or parallel) is responsible only for merging the delta
    into the shared result.  Splitting the compute from the merge is
    what enables parallel L2 audit (PR § parallel-L2): worker threads
    produce deltas, the driver thread merges them under a lock.

    Sequential mode uses the same compute → merge dance, just with no
    actual contention; this keeps both code paths converged.
    """

    files_total: int = 0
    files_ok: int = 0
    files_fail: int = 0
    files_error: int = 0
    files_skipped: int = 0
    bytes_read: int = 0
    inner_arqos_total: int = 0
    inner_arqos_ok: int = 0
    inner_arqos_fail: int = 0
    failures: List[Dict[str, str]] = field(default_factory=list)
    # Set to ``file_name`` when the audit succeeded; consumed by the
    # driver to update ``ledger`` (if any).  ``None`` for failures,
    # errors, and skips so the ledger never marks an unverified file
    # as audited.
    ledger_record_file_name: Optional[str] = None


def _audit_one_file_compute(
    backend: Backend,
    keyset: Keyset,
    computer: str,
    kind: str,
    shard: str,
    file_name: str,
    abs_path: str,
    skip_larger_than: Optional[int],
    callback: Optional[ProgressCallback],
) -> _AuditDelta:
    """Pure per-file audit worker — no mutation of shared state.

    Mirrors :func:`_audit_one_file` (which keeps writing to a shared
    ``result`` for backwards compat).  This variant exists so the
    parallel L2 driver can run many computes concurrently and merge
    deltas serially under a lock.  Callback events are emitted from
    within the worker — :func:`emit` swallows callback exceptions so
    multi-thread callback hooks don't crash the driver.

    See ``_merge_audit_delta`` for the inverse + ``run_full_audit``
    for the orchestrator that ties both halves together.
    """
    d = _AuditDelta()
    d.files_total = 1
    try:
        size = backend.stat_size(abs_path)
    except Exception as exc:
        err = f"stat: {type(exc).__name__}: {exc}"
        d.files_error = 1
        d.failures.append({
            "computer": computer, "kind": kind, "shard": shard,
            "file_name": file_name, "error": err[:200],
        })
        emit(
            callback, EventKind.AUDIT_FILE_FAILED, err,
            computer=computer, family=kind, shard=shard,
            file_name=file_name, error=err[:200],
        )
        return d
    if skip_larger_than is not None and size > skip_larger_than:
        d.files_skipped = 1
        emit(
            callback, EventKind.AUDIT_FILE_SKIPPED,
            f"skipped (size {size:,} > cap {skip_larger_than:,})",
            computer=computer, family=kind, shard=shard,
            file_name=file_name, size=size,
        )
        return d
    try:
        body = backend.read_all(abs_path)
    except Exception as exc:
        err = f"fetch: {type(exc).__name__}: {exc}"
        d.files_error = 1
        d.failures.append({
            "computer": computer, "kind": kind, "shard": shard,
            "file_name": file_name, "error": err[:200],
        })
        emit(
            callback, EventKind.AUDIT_FILE_FAILED, err,
            computer=computer, family=kind, shard=shard,
            file_name=file_name, error=err[:200],
        )
        return d
    d.bytes_read = len(body)
    if len(body) != size:
        err = f"short read: expected {size}, got {len(body)}"
        d.files_error = 1
        d.failures.append({
            "computer": computer, "kind": kind, "shard": shard,
            "file_name": file_name, "error": err,
        })
        emit(
            callback, EventKind.AUDIT_FILE_FAILED, err,
            computer=computer, family=kind, shard=shard,
            file_name=file_name, error=err,
        )
        return d
    if body[: len(C.ARQO_MAGIC)] != C.ARQO_MAGIC:
        err = f"missing ARQO magic: {body[:4]!r}"
        d.files_fail = 1
        d.failures.append({
            "computer": computer, "kind": kind, "shard": shard,
            "file_name": file_name, "error": err,
        })
        emit(
            callback, EventKind.AUDIT_FILE_FAILED, err,
            computer=computer, family=kind, shard=shard,
            file_name=file_name, error=err,
        )
        return d
    n_ok, n_fail, fail_offsets = verify_multi_object_arqos(
        body, keyset.hmac_key,
    )
    d.inner_arqos_total = n_ok + n_fail
    d.inner_arqos_ok = n_ok
    d.inner_arqos_fail = n_fail
    if n_fail == 0:
        d.files_ok = 1
        d.ledger_record_file_name = file_name
        emit(
            callback, EventKind.AUDIT_FILE_VERIFIED,
            f"audit OK: {kind}/{shard}/{file_name}",
            computer=computer, family=kind, shard=shard,
            file_name=file_name, inner_arqos=n_ok,
        )
    else:
        d.files_fail = 1
        err = (
            f"{n_fail}/{n_ok + n_fail} inner ARQO HMAC mismatch "
            f"(first failed offset: {fail_offsets[0]})"
        )
        d.failures.append({
            "computer": computer, "kind": kind, "shard": shard,
            "file_name": file_name, "error": err,
        })
        emit(
            callback, EventKind.AUDIT_FILE_FAILED, err,
            computer=computer, family=kind, shard=shard,
            file_name=file_name, error=err,
        )
    return d


def _merge_audit_delta(
    result: ObjectAuditResult, delta: _AuditDelta,
) -> None:
    """Apply ``delta`` to ``result`` in place.  Caller holds a lock
    when invoked from the parallel driver."""
    result.files_total += delta.files_total
    result.files_ok += delta.files_ok
    result.files_fail += delta.files_fail
    result.files_error += delta.files_error
    result.files_skipped += delta.files_skipped
    result.bytes_read += delta.bytes_read
    result.inner_arqos_total += delta.inner_arqos_total
    result.inner_arqos_ok += delta.inner_arqos_ok
    result.inner_arqos_fail += delta.inner_arqos_fail
    result.failures.extend(delta.failures)


def _audit_one_file(
    backend: Backend,
    keyset: Keyset,
    computer: str,
    kind: str,
    shard: str,
    file_name: str,
    abs_path: str,
    skip_larger_than: Optional[int],
    result: ObjectAuditResult,
    callback: Optional[ProgressCallback],
) -> None:
    result.files_total += 1
    try:
        size = backend.stat_size(abs_path)
    except Exception as exc:
        result.files_error += 1
        err = f"stat: {type(exc).__name__}: {exc}"
        result.failures.append({
            "computer": computer, "kind": kind, "shard": shard,
            "file_name": file_name, "error": err[:200],
        })
        emit(
            callback, EventKind.AUDIT_FILE_FAILED, err,
            computer=computer, family=kind, shard=shard,
            file_name=file_name, error=err[:200],
        )
        return
    if skip_larger_than is not None and size > skip_larger_than:
        result.files_skipped += 1
        emit(
            callback, EventKind.AUDIT_FILE_SKIPPED,
            f"skipped (size {size:,} > cap {skip_larger_than:,})",
            computer=computer, family=kind, shard=shard,
            file_name=file_name, size=size,
        )
        return
    try:
        body = backend.read_all(abs_path)
    except Exception as exc:
        result.files_error += 1
        err = f"fetch: {type(exc).__name__}: {exc}"
        result.failures.append({
            "computer": computer, "kind": kind, "shard": shard,
            "file_name": file_name, "error": err[:200],
        })
        emit(
            callback, EventKind.AUDIT_FILE_FAILED, err,
            computer=computer, family=kind, shard=shard,
            file_name=file_name, error=err[:200],
        )
        return
    result.bytes_read += len(body)
    if len(body) != size:
        result.files_error += 1
        err = f"short read: expected {size}, got {len(body)}"
        result.failures.append({
            "computer": computer, "kind": kind, "shard": shard,
            "file_name": file_name, "error": err,
        })
        emit(
            callback, EventKind.AUDIT_FILE_FAILED, err,
            computer=computer, family=kind, shard=shard,
            file_name=file_name, error=err,
        )
        return
    if body[: len(C.ARQO_MAGIC)] != C.ARQO_MAGIC:
        result.files_fail += 1
        err = f"missing ARQO magic: {body[:4]!r}"
        result.failures.append({
            "computer": computer, "kind": kind, "shard": shard,
            "file_name": file_name, "error": err,
        })
        emit(
            callback, EventKind.AUDIT_FILE_FAILED, err,
            computer=computer, family=kind, shard=shard,
            file_name=file_name, error=err,
        )
        return
    n_ok, n_fail, fail_offsets = verify_multi_object_arqos(
        body, keyset.hmac_key,
    )
    result.inner_arqos_total += n_ok + n_fail
    result.inner_arqos_ok += n_ok
    result.inner_arqos_fail += n_fail
    if n_fail == 0:
        result.files_ok += 1
        emit(
            callback, EventKind.AUDIT_FILE_VERIFIED,
            f"audit OK: {kind}/{shard}/{file_name}",
            computer=computer, family=kind, shard=shard,
            file_name=file_name, inner_arqos=n_ok,
        )
    else:
        result.files_fail += 1
        err = (
            f"{n_fail}/{n_ok + n_fail} inner ARQO HMAC mismatch "
            f"(first failed offset: {fail_offsets[0]})"
        )
        result.failures.append({
            "computer": computer, "kind": kind, "shard": shard,
            "file_name": file_name, "error": err,
        })
        emit(
            callback, EventKind.AUDIT_FILE_FAILED, err,
            computer=computer, family=kind, shard=shard,
            file_name=file_name, error=err,
        )


def run_full_audit(
    backend: Backend,
    layouts: List[L.Arq7ComputerLayout],
    encryption_password: str,
    *,
    root: str = "/",
    skip_larger_than: Optional[int] = AUDIT_DEFAULT_SKIP_LARGER_THAN,
    max_runtime_sec: Optional[float] = None,
    max_bytes: Optional[int] = None,
    progress_every: int = 100,
    openssl_path: str = "openssl",
    callback: Optional[ProgressCallback] = None,
    ledger=None,
    audit_concurrency: int = 1,
) -> ObjectAuditResult:
    """L2: HMAC-verify every EncryptedObject across all object families.

    Soft caps (``max_runtime_sec``, ``max_bytes``) abort the run with
    ``aborted_reason`` populated and partial counters preserved — the
    operator can resume from a fresh invocation later.

    Pass ``ledger`` (an
    :class:`arq_validator.incremental_audit.AuditLedger`) to enable
    incremental mode — files whose ``file_name`` (Arq's content-
    addressed filename) is already in the ledger get skipped + counted
    in ``files_skipped_by_ledger``, and successful audits register
    themselves in the ledger so the next sweep can skip them too.
    The caller is responsible for ``save_ledger(ledger, path)`` after
    the run; we don't persist on every sweep so a failed sweep can
    be re-run without losing prior state.

    Parallel L2 audit (operator-tunable via ``audit_concurrency``):

      ``audit_concurrency = 1`` (default) — purely sequential; the
        existing behavior, completely unchanged for backwards compat
        and for the SFTP path where a single channel cannot
        multiplex.

      ``audit_concurrency > 1`` — runs the per-file compute on a
        ``ThreadPoolExecutor`` with ``audit_concurrency`` workers
        and merges deltas under a lock.  The driver bounds in-flight
        work to ``audit_concurrency * 2`` so memory + budget-check
        responsiveness stay constant.  Useful for ``LocalBackend``
        where reads open fresh file descriptors and HMAC is the
        CPU-bound bottleneck.

      Safety clamp: if ``backend.supports_concurrent_reads`` is
      False (the Backend protocol default, e.g. ``SftpBackend``
      with a single channel), ``audit_concurrency`` is silently
      clamped to 1 and a LOG event is emitted so the operator
      sees the downgrade.

    Event ordering: in parallel mode, AUDIT_FILE_VERIFIED /
    AUDIT_FILE_FAILED events arrive out of order; UIs that depend
    on file-order sequence should fall back to sequential mode.
    AUDIT_PROGRESS events remain serialized by the driver and
    reflect a consistent merge-point snapshot.
    """
    emit(callback, EventKind.TIER_STARTED, "L2 full audit", tier="L2")
    result = ObjectAuditResult()
    result.started_at = time.time()
    started_monotonic = time.monotonic()
    deadline = (
        started_monotonic + max_runtime_sec if max_runtime_sec else None
    )
    # Compute planned-file count up-front so AUDIT_PROGRESS can
    # carry meaningful ETA estimates.  Source: the discovered
    # layout's per-family counts (same numbers that drive
    # COMPUTER_FOUND).  Includes ALL files — the runtime may
    # later skip some via the size cap or ledger; the ETA
    # framework treats those as completed-already, so the
    # estimate stays a useful upper bound.
    result.planned_files = sum(
        len(getattr(lay, "blobpacks", ()))
        + len(getattr(lay, "treepacks", ()))
        + len(getattr(lay, "largeblobpacks", ()))
        + len(getattr(lay, "standardobjects", ()))
        for lay in layouts
    )

    # Resolve effective concurrency.  Defensive bounds (1 ≤ N ≤ 64)
    # — over 64 workers buys nothing on any current target and
    # exposes us to fd-table exhaustion on dense backups.
    requested_concurrency = max(1, min(64, int(audit_concurrency)))
    backend_concurrent = bool(getattr(
        backend, "supports_concurrent_reads", False))
    if requested_concurrency > 1 and not backend_concurrent:
        emit(
            callback, EventKind.LOG,
            (f"audit_concurrency={requested_concurrency} requested "
             f"but backend.supports_concurrent_reads=False — "
             f"clamping to 1 (sequential)"),
            requested=requested_concurrency, clamped_to=1,
            backend=type(backend).__name__,
        )
        eff_concurrency = 1
    else:
        eff_concurrency = requested_concurrency

    # Shared-state lock used in parallel mode.  Sequential mode never
    # acquires it (we branch on ``eff_concurrency`` before).
    merge_lock = threading.Lock()

    def _budget_check() -> Optional[str]:
        """Return ``"max_runtime"`` / ``"max_bytes"`` if a budget
        has been hit, else None.  Called under ``merge_lock`` in
        parallel mode so ``result.bytes_read`` is a consistent
        snapshot."""
        if deadline is not None and time.monotonic() >= deadline:
            return "max_runtime"
        if max_bytes is not None and result.bytes_read >= max_bytes:
            return "max_bytes"
        return None

    def _emit_progress_if_due() -> None:
        """Emit AUDIT_PROGRESS when ``files_total`` crosses the
        ``progress_every`` boundary.  Caller holds ``merge_lock``
        in parallel mode so the read of ``result.files_total`` is
        consistent with the most recent merge.

        Payload includes elapsed / throughput / ETA so UIs can
        render a complete progress line without tracking their
        own wallclock.  ETA is a linear extrapolation
        (``elapsed * remaining / processed``) and gets more
        accurate as the run proceeds; the first few emissions
        may be wildly optimistic or pessimistic depending on
        whether the early files are large or small.
        """
        if not (result.files_total > 0
                and result.files_total % progress_every == 0):
            return
        # Throughput math under the same lock as the counter
        # snapshot — readers get a consistent picture.
        elapsed = max(0.001, time.monotonic() - started_monotonic)
        files_per_sec = result.files_total / elapsed
        bytes_per_sec = result.bytes_read / elapsed
        # ETA: only meaningful when we know the total file
        # count from layout.  remaining = planned - total
        # (clamped to 0; planned is an upper bound that may
        # over-estimate slightly if the layout shifted mid-
        # run).
        eta_sec: Optional[float] = None
        remaining = 0
        if result.planned_files > 0:
            remaining = max(
                0, result.planned_files - result.files_total)
            if files_per_sec > 0 and remaining > 0:
                eta_sec = remaining / files_per_sec
        # Human message: keep the historical prefix + append
        # the new throughput + ETA suffix so existing log
        # filters keep matching.
        pct = (
            f"{100.0 * result.files_total / result.planned_files:.1f}%"
            if result.planned_files > 0 else "?%")
        msg_extra = (
            f" — {pct} done, {files_per_sec:.1f} files/s"
            f", {bytes_per_sec / 1e6:.1f} MB/s"
        )
        if eta_sec is not None:
            msg_extra += f", ETA {_format_duration(eta_sec)}"
        else:
            msg_extra += ", ETA unknown"
        emit(
            callback, EventKind.AUDIT_PROGRESS,
            (f"audit: {result.files_total} files; "
             f"{result.files_ok} OK, "
             f"{result.files_fail + result.files_error} fail/err, "
             f"{result.files_skipped} skipped"
             + msg_extra),
            files_total=result.files_total,
            files_ok=result.files_ok,
            files_fail=result.files_fail,
            files_error=result.files_error,
            files_skipped=result.files_skipped,
            bytes_read=result.bytes_read,
            # ETA + throughput fields (CL TBD, 2026-05-12).
            # New consumers can read these directly; older
            # callbacks ignore unknown keys per the events
            # forward-compat contract.
            elapsed_sec=elapsed,
            files_per_sec=files_per_sec,
            bytes_per_sec=bytes_per_sec,
            planned_files=result.planned_files,
            remaining_files=remaining,
            eta_sec=eta_sec,
            progress_fraction=(
                result.files_total / result.planned_files
                if result.planned_files > 0 else None),
        )

    def _merge_and_finalize(delta: _AuditDelta) -> None:
        """Apply a worker's delta into ``result`` + update ``ledger``
        + emit periodic progress.  Caller holds ``merge_lock`` in
        parallel mode."""
        _merge_audit_delta(result, delta)
        if (delta.ledger_record_file_name is not None
                and ledger is not None):
            ledger.record(delta.ledger_record_file_name)
        _emit_progress_if_due()

    # Sequential path — preserves the historical behavior bit-for-
    # bit.  Used when ``eff_concurrency == 1``.
    def _run_sequential(lay, kind, keyset) -> Optional[str]:
        for shard, file_name in lay.family_items(kind):
            abort = _budget_check()
            if abort is not None:
                return abort
            abs_path = L.object_path(
                root, lay.computer_uuid, kind, shard, file_name,
            )
            if ledger is not None and ledger.contains(file_name):
                result.files_total += 1
                result.files_skipped_by_ledger += 1
                emit(
                    callback, EventKind.AUDIT_FILE_SKIPPED,
                    "ledger-skipped (audited previously)",
                    computer=lay.computer_uuid, family=kind,
                    shard=shard, file_name=file_name,
                    reason="ledger",
                )
                continue
            delta = _audit_one_file_compute(
                backend, keyset, lay.computer_uuid,
                kind, shard, file_name, abs_path,
                skip_larger_than, callback,
            )
            _merge_and_finalize(delta)
        return None

    # Parallel path — submit per-file computes to a thread pool,
    # bounded in-flight, drain as completed.  Caller holds the
    # merge_lock during result-mutation.  Budget checks under lock
    # so a partial-merge state isn't mistakenly read.
    def _run_parallel(
            lay, kind, keyset, pool: ThreadPoolExecutor,
            ) -> Optional[str]:
        max_in_flight = eff_concurrency * 2
        in_flight: set[Future] = set()
        abort: Optional[str] = None

        for shard, file_name in lay.family_items(kind):
            # Budget check (cheap, under lock for consistency).
            with merge_lock:
                abort = _budget_check()
            if abort is not None:
                break
            abs_path = L.object_path(
                root, lay.computer_uuid, kind, shard, file_name,
            )
            # Ledger skip is cheap + thread-safe (set.contains is
            # atomic under GIL).  Doing it pre-submit avoids
            # spending a worker slot on a known skip.
            if ledger is not None and ledger.contains(file_name):
                with merge_lock:
                    result.files_total += 1
                    result.files_skipped_by_ledger += 1
                    _emit_progress_if_due()
                emit(
                    callback, EventKind.AUDIT_FILE_SKIPPED,
                    "ledger-skipped (audited previously)",
                    computer=lay.computer_uuid, family=kind,
                    shard=shard, file_name=file_name,
                    reason="ledger",
                )
                continue
            # Drain one if we've hit the in-flight cap.
            if len(in_flight) >= max_in_flight:
                done, in_flight = wait(
                    in_flight, return_when=FIRST_COMPLETED)
                for f in done:
                    with merge_lock:
                        _merge_and_finalize(f.result())
                # Re-check budget after merging a batch of deltas.
                with merge_lock:
                    abort = _budget_check()
                if abort is not None:
                    break
            in_flight.add(pool.submit(
                _audit_one_file_compute,
                backend, keyset, lay.computer_uuid,
                kind, shard, file_name, abs_path,
                skip_larger_than, callback,
            ))

        # Always drain the remaining futures — even on abort.  Any
        # work already in-flight has already opened file
        # descriptors / consumed a worker; we must collect the
        # delta so result counters stay consistent (a leaked
        # future would skew totals on the next call site).
        for f in as_completed(list(in_flight)):
            try:
                d = f.result()
            except Exception as exc:
                # Worker raised — shouldn't happen because
                # _audit_one_file_compute catches its own
                # exceptions, but defense in depth.
                emit(
                    callback, EventKind.LOG,
                    f"audit worker raised: "
                    f"{type(exc).__name__}: {exc}",
                    error=str(exc),
                )
                continue
            with merge_lock:
                _merge_and_finalize(d)
        return abort

    audited_any = False
    for lay in layouts:
        kp = L.keyset_path(root, lay.computer_uuid)
        # Per-cu keyset (2026-05-27): a destination can host several
        # backup sets with different / no encryption. SKIP (don't
        # abort the whole audit for) a set whose keyset is missing
        # (unencrypted) or doesn't open with the supplied password
        # (a different-password set) — the auditor simply doesn't
        # hold the key, which is not corruption. Mirrors audit-drip
        # _decrypt_keysets_per_cu.
        try:
            keyset_bytes = backend.read_all(kp)
        except Exception:
            reason = "unencrypted backup set (no encryptedkeyset.dat)"
            result.skipped_backup_sets.append(
                f"{lay.computer_uuid}: {reason}")
            emit(callback, EventKind.AUDIT_FILE_SKIPPED,
                 f"skipping backup set {lay.computer_uuid}: {reason}",
                 computer=lay.computer_uuid)
            continue
        try:
            keyset = decrypt_keyset(
                keyset_bytes, encryption_password, openssl_path=openssl_path,
            )
        except Exception as exc:
            reason = (
                f"keyset not decryptable with the configured password "
                f"({type(exc).__name__}) — likely a different backup set")
            result.skipped_backup_sets.append(
                f"{lay.computer_uuid}: {reason}")
            emit(callback, EventKind.AUDIT_FILE_SKIPPED,
                 f"skipping backup set {lay.computer_uuid}: {reason}",
                 computer=lay.computer_uuid, error=str(exc))
            continue
        audited_any = True

        emit(
            callback, EventKind.KEYSET_DECRYPTED,
            f"keyset decrypted for {lay.computer_uuid}",
            computer=lay.computer_uuid,
        )

        if eff_concurrency == 1:
            for kind in C.OBJECT_FAMILIES:
                abort = _run_sequential(lay, kind, keyset)
                if abort is not None:
                    result.aborted_reason = abort
                    del keyset
                    return result
        else:
            with ThreadPoolExecutor(
                    max_workers=eff_concurrency,
                    thread_name_prefix="arq-l2-audit",
                    ) as pool:
                for kind in C.OBJECT_FAMILIES:
                    abort = _run_parallel(lay, kind, keyset, pool)
                    if abort is not None:
                        result.aborted_reason = abort
                        del keyset
                        return result
        del keyset

    # No backup set yielded a usable keyset (all skipped — wrong
    # password and/or all unencrypted) → genuine keyset failure.
    if layouts and not audited_any:
        result.aborted_reason = "keyset_failed"
        emit(callback, EventKind.KEYSET_FAILED,
             "no decryptable keyset for any backup set: "
             + "; ".join(result.skipped_backup_sets),
             error="no_auditable_keyset")

    # Bump sweep_count + last_sweep_finished_at on the ledger so the
    # operator can ``cat`` the file later + see how many sweeps ran.
    if ledger is not None:
        ledger.sweep_count += 1
        ledger.last_sweep_finished_at = time.time()
    # Final throughput snapshot mirrors AUDIT_PROGRESS payload so
    # UIs can render a "run complete" summary without re-deriving.
    elapsed_total = max(
        0.001, time.monotonic() - started_monotonic)
    final_files_per_sec = result.files_total / elapsed_total
    final_bytes_per_sec = result.bytes_read / elapsed_total
    emit(
        callback, EventKind.TIER_FINISHED,
        (f"L2 audit finished in {_format_duration(elapsed_total)}: "
         f"{result.files_ok}/{result.files_total} OK, "
         f"{result.files_fail + result.files_error} fail/err, "
         f"{result.files_skipped_by_ledger} ledger-skipped, "
         f"{final_files_per_sec:.1f} files/s, "
         f"{final_bytes_per_sec / 1e6:.1f} MB/s"),
        tier="L2",
        files_total=result.files_total,
        files_ok=result.files_ok,
        files_fail=result.files_fail,
        files_error=result.files_error,
        files_skipped=result.files_skipped,
        files_skipped_by_ledger=result.files_skipped_by_ledger,
        elapsed_sec=elapsed_total,
        files_per_sec=final_files_per_sec,
        bytes_per_sec=final_bytes_per_sec,
        planned_files=result.planned_files,
    )
    return result


# ---------------------------------------------------------------------------
# L3: per-backuprecord blob-graph consistency
# ---------------------------------------------------------------------------


def run_graph_check(
    backend: Backend,
    layouts: List[L.Arq7ComputerLayout],
    encryption_password: str,
    *,
    root: str = "/",
    max_records: Optional[int] = None,
    max_blobs_per_record: int = 0,
    max_runtime_sec: Optional[float] = None,
    openssl_path: str = "openssl",
    callback: Optional[ProgressCallback] = None,
) -> GraphCheckResult:
    """L3: walk every backuprecord's blob graph end-to-end.

    For each layout's backup folder, iterate every backuprecord
    chronologically, delegate the per-record walk to
    :func:`record_validator.validate_record`, and aggregate the
    results.  Failures are dedupped by ``blob_id`` across records.

    Cost: ``records × O(blobs per record)``.  Dedup-friendly
    backups (most snapshots share most blobs) come out
    significantly cheaper because ``validate_record``'s per-call
    walk hits the same on-disk bytes; if performance becomes a
    concern, pass ``ledger`` (future) to short-circuit repeat
    fetches across records.

    Soft caps:
      ``max_records`` — abort after this many records walked
        (0 / None = unlimited).
      ``max_blobs_per_record`` — passed through to
        ``validate_record``; 0 = unlimited per record.
      ``max_runtime_sec`` — wall-time budget for the whole tier.

    Use cases:
      - Detect bit-rot in tree nodes that reference blobs whose
        on-disk file is intact (L2 wouldn't notice; the broken
        REFERENCE is the corruption).
      - Detect partial-GC scenarios where a blob got cleaned up
        but a record still references it.
      - Confirm older records (not just latest) are still
        restorable.

    L3 sits between L1b (latest backuprecord only) and L2 (every
    EncryptedObject regardless of references) in scope: it
    follows the ``records → trees → blobs`` graph for ALL
    records.  L2 catches orphan-blob bit-rot; L3 catches
    missing-reference + orphan-reference bit-rot.
    """
    from .record_validator import validate_record  # lazy
    emit(callback, EventKind.TIER_STARTED,
         "L3 graph consistency", tier="L3")
    result = GraphCheckResult()
    result.started_at = time.time()
    started_monotonic = time.monotonic()
    deadline = (
        started_monotonic + max_runtime_sec if max_runtime_sec
        else None)

    # Enumerate all record paths up front so the operator
    # gets a planned-records count in the TIER_STARTED event +
    # downstream progress events.
    record_paths: List[Tuple[str, str]] = []  # (cu, rec_path)
    for lay in layouts:
        for folder_uuid in getattr(
                lay, "backup_folder_uuids", ()):
            try:
                paths = L.list_backuprecords(
                    backend, root, lay.computer_uuid, folder_uuid)
            except Exception as exc:
                err = (f"list_backuprecords failed for "
                       f"{lay.computer_uuid}/{folder_uuid}: "
                       f"{type(exc).__name__}: {exc}")
                result.failures.append({
                    "blob_id": "", "kind": "fetch",
                    "rel_path": (f"{lay.computer_uuid}/"
                                 f"backupfolders/{folder_uuid}"),
                    "error": err,
                })
                emit(callback, EventKind.LOG, err,
                     computer=lay.computer_uuid,
                     folder=folder_uuid)
                continue
            for p in paths:
                record_paths.append((lay.computer_uuid, p))
    result.planned_records = len(record_paths)

    if max_records and result.planned_records > max_records:
        record_paths = record_paths[:max_records]

    # Track blob_ids seen across records so the final failure
    # list is unique-by-blob_id (a 5-record run that fails
    # the same blob in all 5 surfaces once).
    seen_blob_failures: set = set()

    for cu, record_path in record_paths:
        if deadline is not None and time.monotonic() >= deadline:
            result.aborted_reason = "max_runtime"
            break
        rep = validate_record(
            backend, record_path, encryption_password,
            computer_uuid=cu, openssl_path=openssl_path,
            max_blobs=max_blobs_per_record,
            callback=None,    # per-record callback would
                              # spam — L3 emits its own
                              # AUDIT_PROGRESS instead.
        )
        result.records_checked += 1
        result.blob_walks_total += rep.blobs_walked
        result.bytes_fetched += rep.bytes_fetched
        if rep.ok and not rep.failures:
            result.records_ok += 1
        else:
            result.records_fail += 1
            for f in rep.failures:
                blob_id = getattr(f, "blob_id", "") or ""
                kind = getattr(f, "kind", "") or ""
                # Bucket: dedup by (blob_id, kind) so the
                # same blob failing the same way in multiple
                # records collapses to one row.
                dedup_key = (blob_id, kind)
                if dedup_key in seen_blob_failures:
                    continue
                seen_blob_failures.add(dedup_key)
                if kind == "missing":
                    result.blobs_missing += 1
                elif kind == "hmac":
                    result.blobs_hmac_fail += 1
                elif kind == "decode":
                    result.blobs_decode_fail += 1
                result.failures.append({
                    "blob_id": blob_id,
                    "rel_path": getattr(f, "rel_path", ""),
                    "offset": str(getattr(f, "offset", 0)),
                    "length": str(getattr(f, "length", 0)),
                    "kind": kind,
                    "error": getattr(f, "error", "")[:200],
                    "node_path": getattr(f, "node_path", ""),
                    "record_path": record_path,
                    "computer": cu,
                })
        # Emit progress every record (cheap; one record can
        # take minutes already, so per-record cadence is the
        # right granularity for L3).
        elapsed = max(0.001,
                       time.monotonic() - started_monotonic)
        records_per_sec = result.records_checked / elapsed
        bytes_per_sec = result.bytes_fetched / elapsed
        remaining = max(
            0, result.planned_records - result.records_checked)
        eta_sec: Optional[float] = None
        if records_per_sec > 0 and remaining > 0:
            eta_sec = remaining / records_per_sec
        emit(
            callback, EventKind.AUDIT_PROGRESS,
            (f"graph: {result.records_checked}/"
             f"{result.planned_records} records, "
             f"{result.records_ok} OK, "
             f"{result.records_fail} fail, "
             f"{result.blobs_missing} missing-blob, "
             f"{result.blobs_hmac_fail} hmac-fail"
             + (f" — ETA {_format_duration(eta_sec)}"
                if eta_sec is not None else "")),
            records_checked=result.records_checked,
            records_ok=result.records_ok,
            records_fail=result.records_fail,
            blob_walks_total=result.blob_walks_total,
            blobs_missing=result.blobs_missing,
            blobs_hmac_fail=result.blobs_hmac_fail,
            blobs_decode_fail=result.blobs_decode_fail,
            bytes_fetched=result.bytes_fetched,
            planned_records=result.planned_records,
            remaining_records=remaining,
            elapsed_sec=elapsed,
            records_per_sec=records_per_sec,
            bytes_per_sec=bytes_per_sec,
            eta_sec=eta_sec,
            tier="L3",
        )

    result.blobs_unique = len(seen_blob_failures) + (
        result.blob_walks_total - len(seen_blob_failures))
    # Note: blobs_unique above is an upper bound — the
    # per-record reports' `blobs_walked` counter doesn't
    # currently expose unique IDs.  We expose the raw walk
    # total via blob_walks_total + the dedupped failure count
    # via blobs_missing/hmac/decode for actionable numbers.

    elapsed_total = max(
        0.001, time.monotonic() - started_monotonic)
    final_records_per_sec = (
        result.records_checked / elapsed_total)
    final_bytes_per_sec = (
        result.bytes_fetched / elapsed_total)
    emit(
        callback, EventKind.TIER_FINISHED,
        (f"L3 graph check finished in "
         f"{_format_duration(elapsed_total)}: "
         f"{result.records_ok}/{result.records_checked} "
         f"records OK, {result.blobs_missing} missing-blob, "
         f"{result.blobs_hmac_fail} hmac-fail, "
         f"{final_records_per_sec:.2f} rec/s, "
         f"{final_bytes_per_sec / 1e6:.1f} MB/s"),
        tier="L3",
        records_checked=result.records_checked,
        records_ok=result.records_ok,
        records_fail=result.records_fail,
        blobs_missing=result.blobs_missing,
        blobs_hmac_fail=result.blobs_hmac_fail,
        blobs_decode_fail=result.blobs_decode_fail,
        bytes_fetched=result.bytes_fetched,
        elapsed_sec=elapsed_total,
        records_per_sec=final_records_per_sec,
        bytes_per_sec=final_bytes_per_sec,
        planned_records=result.planned_records,
    )
    return result
