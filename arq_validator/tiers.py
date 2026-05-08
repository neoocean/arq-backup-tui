"""Validation tiers: L0 layout, L1a magic, L1b head HMAC, L2 full audit.

Each tier is a stateless function that takes a backend, the discovered
layout, and tier-specific options, and returns a result dataclass.
Progress is reported through the optional ``ProgressCallback``.
"""

from __future__ import annotations

import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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
class ObjectAuditResult:
    """L2 outcome: full HMAC sweep over every EncryptedObject."""

    files_total: int = 0
    files_ok: int = 0
    files_fail: int = 0
    files_error: int = 0
    files_skipped: int = 0
    inner_arqos_total: int = 0
    inner_arqos_ok: int = 0
    inner_arqos_fail: int = 0
    bytes_read: int = 0
    failures: List[Dict[str, str]] = field(default_factory=list)
    aborted_reason: Optional[str] = None


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
) -> ObjectAuditResult:
    """L2: HMAC-verify every EncryptedObject across all object families.

    Soft caps (``max_runtime_sec``, ``max_bytes``) abort the run with
    ``aborted_reason`` populated and partial counters preserved — the
    operator can resume from a fresh invocation later.
    """
    emit(callback, EventKind.TIER_STARTED, "L2 full audit", tier="L2")
    result = ObjectAuditResult()
    deadline = (
        time.monotonic() + max_runtime_sec if max_runtime_sec else None
    )

    for lay in layouts:
        kp = L.keyset_path(root, lay.computer_uuid)
        try:
            keyset_bytes = backend.read_all(kp)
            keyset = decrypt_keyset(
                keyset_bytes, encryption_password, openssl_path=openssl_path,
            )
        except Exception as exc:
            err = (
                f"keyset for {lay.computer_uuid}: "
                f"{type(exc).__name__}: {exc}"
            )
            result.failures.append({
                "computer": lay.computer_uuid,
                "kind": "keyset", "shard": "", "file_name": C.KEYSET_FILE,
                "error": err,
            })
            result.aborted_reason = "keyset_failed"
            emit(callback, EventKind.KEYSET_FAILED, err,
                 computer=lay.computer_uuid, error=str(exc))
            return result

        emit(
            callback, EventKind.KEYSET_DECRYPTED,
            f"keyset decrypted for {lay.computer_uuid}",
            computer=lay.computer_uuid,
        )

        for kind in C.OBJECT_FAMILIES:
            for shard, file_name in lay.family_items(kind):
                if deadline is not None and time.monotonic() >= deadline:
                    result.aborted_reason = "max_runtime"
                    del keyset
                    return result
                if max_bytes is not None and result.bytes_read >= max_bytes:
                    result.aborted_reason = "max_bytes"
                    del keyset
                    return result
                abs_path = L.object_path(
                    root, lay.computer_uuid, kind, shard, file_name,
                )
                _audit_one_file(
                    backend, keyset, lay.computer_uuid,
                    kind, shard, file_name, abs_path,
                    skip_larger_than, result, callback,
                )
                if result.files_total % progress_every == 0:
                    emit(
                        callback, EventKind.AUDIT_PROGRESS,
                        (f"audit: {result.files_total} files; "
                         f"{result.files_ok} OK, "
                         f"{result.files_fail + result.files_error} fail/err, "
                         f"{result.files_skipped} skipped"),
                        files_total=result.files_total,
                        files_ok=result.files_ok,
                        files_fail=result.files_fail,
                        files_error=result.files_error,
                        files_skipped=result.files_skipped,
                        bytes_read=result.bytes_read,
                    )
        del keyset

    emit(
        callback, EventKind.TIER_FINISHED,
        (f"L2 audit finished: {result.files_ok}/{result.files_total} OK, "
         f"{result.files_fail + result.files_error} fail/err"),
        tier="L2",
        files_total=result.files_total,
        files_ok=result.files_ok,
        files_fail=result.files_fail,
        files_error=result.files_error,
        files_skipped=result.files_skipped,
    )
    return result
