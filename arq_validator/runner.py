"""High-level orchestrator that wires tiers together for one run.

The TUI (or the CLI) creates a :class:`ValidationTier` enum value and
hands it to :func:`validate` along with a backend, an optional
``ProgressCallback``, and any tier-specific options. The result is a
:class:`ValidationReport` that can be serialized to JSON or rendered
directly.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field, fields
from enum import Enum
from typing import Any, Dict, List, Optional, Type, TypeVar

from . import layout as L
from .backend import Backend
from .events import EventKind, ProgressCallback, emit
from .tiers import (
    AUDIT_DEFAULT_SKIP_LARGER_THAN,
    BackupRecordResult,
    LayoutResult,
    MagicCheckResult,
    ObjectAuditResult,
    run_backuprecord_check,
    run_full_audit,
    run_layout_check,
    run_magic_check,
)


class ValidationTier(Enum):
    """Coarse tier selector matching Arq's own validation hierarchy.

    Each tier subsumes the cheaper tiers below it: ``DEEP`` runs L0 +
    L1a + L1b; ``AUDIT`` runs everything.
    """

    DRY_RUN = "dry-run"   # L0 only
    QUICK = "quick"       # L0 + L1a magic-byte sample
    DEEP = "deep"         # L0 + L1a + L1b backuprecord HMAC
    AUDIT = "audit"       # L0 + L1a + L1b + L2 full HMAC sweep


@dataclass
class ValidationReport:
    """Aggregate results from a validation run."""

    tier: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    root: str = ""
    backend_kind: str = ""
    error: Optional[str] = None
    layout: Optional[LayoutResult] = None
    magic_check: Optional[MagicCheckResult] = None
    backuprecord: Optional[BackupRecordResult] = None
    audit: Optional[ObjectAuditResult] = None

    @property
    def elapsed_sec(self) -> float:
        return max(0.0, self.finished_at - self.started_at)

    def has_failures(self) -> bool:
        if self.error:
            return True
        if self.magic_check and self.magic_check.fail:
            return True
        if self.backuprecord and (
            self.backuprecord.fail or not self.backuprecord.keyset_decrypted
            and self.tier in (ValidationTier.DEEP.value, ValidationTier.AUDIT.value)
        ):
            return True
        if self.audit and (
            self.audit.files_fail or self.audit.files_error
            or self.audit.aborted_reason
        ):
            return True
        if self.layout and not self.layout.layout_ok:
            return True
        return False

    def to_dict(self) -> Dict[str, Any]:
        out = asdict(self)
        # Drop None tier blocks for cleaner JSON output.
        return {k: v for k, v in out.items() if v is not None}

    def to_json(self, *, indent: Optional[int] = 2) -> str:
        """Serialize the report to a JSON string.

        ``indent=None`` produces the compact form (useful for
        log lines / wire transport); the default 2-space
        indent is human-friendly for `cat report.json`.

        Schema-version + format hints are not embedded: the
        per-tier dataclass field names are the schema.  Future
        non-additive changes would need ``from_dict`` to be
        version-aware.
        """
        return json.dumps(
            self.to_dict(), indent=indent, ensure_ascii=False,
            sort_keys=False, default=str)

    @classmethod
    def from_dict(
            cls, data: Dict[str, Any]) -> "ValidationReport":
        """Reconstruct a ValidationReport from a ``to_dict``
        output.

        Unknown top-level keys are ignored (forward-compat) —
        a newer ``to_dict`` that adds fields can still be read
        by an older ``from_dict``.  Nested tier blocks are
        reconstructed via ``_dataclass_from_dict`` which honors
        each block's known field set; the same forward-compat
        rule applies there.

        Missing tier blocks (operator ran ``--tier quick`` so
        ``audit`` is None) come back as None — round-trip
        preserves the "this tier wasn't run" signal.
        """
        kwargs: Dict[str, Any] = {}
        known = {f.name for f in fields(cls)}
        for k, v in data.items():
            if k not in known:
                continue        # forward-compat: ignore extras
            kwargs[k] = v
        # Hoist tier blocks into their dataclass forms.
        if isinstance(kwargs.get("layout"), dict):
            kwargs["layout"] = _dataclass_from_dict(
                LayoutResult, kwargs["layout"])
        if isinstance(kwargs.get("magic_check"), dict):
            kwargs["magic_check"] = _dataclass_from_dict(
                MagicCheckResult, kwargs["magic_check"])
        if isinstance(kwargs.get("backuprecord"), dict):
            kwargs["backuprecord"] = _dataclass_from_dict(
                BackupRecordResult, kwargs["backuprecord"])
        if isinstance(kwargs.get("audit"), dict):
            kwargs["audit"] = _dataclass_from_dict(
                ObjectAuditResult, kwargs["audit"])
        return cls(**kwargs)

    @classmethod
    def from_json(cls, text: str) -> "ValidationReport":
        """Parse a JSON string produced by ``to_json``.
        Convenience wrapper around ``from_dict``."""
        return cls.from_dict(json.loads(text))


# JSON round-trip helper for the tier-result dataclasses.  We
# could plug in a third-party serializer (``cattrs``, etc.) for
# polymorphic + union support, but every tier-result field is a
# JSON primitive (str / int / float / bool / list / dict / None),
# so a tiny dataclass-aware re-hydrator is sufficient + keeps the
# pure-stdlib dependency posture.
_T = TypeVar("_T")


def _dataclass_from_dict(cls: Type[_T], data: Dict[str, Any]) -> _T:
    """Re-hydrate a dataclass from its ``asdict`` output.

    Forward-compat: unknown keys in ``data`` are silently
    dropped, so adding new fields to the dataclass between
    write + read still works in the read direction.

    Backward-compat: missing fields fall through to the
    dataclass's own defaults (no special handling needed —
    the dataclass constructor supplies them).
    """
    known = {f.name for f in fields(cls)}
    kwargs = {k: v for k, v in data.items() if k in known}
    return cls(**kwargs)


def _tier_runs(tier: ValidationTier) -> List[str]:
    """Return the list of tier short-names this validation tier runs."""
    return {
        ValidationTier.DRY_RUN: ["L0"],
        ValidationTier.QUICK: ["L0", "L1a"],
        ValidationTier.DEEP: ["L0", "L1a", "L1b"],
        ValidationTier.AUDIT: ["L0", "L1a", "L1b", "L2"],
    }[tier]


def validate(
    backend: Backend,
    *,
    tier: ValidationTier = ValidationTier.QUICK,
    root: str = "/",
    encryption_password: Optional[str] = None,
    sample_fraction: float = 0.05,
    audit_skip_larger_than: Optional[int] = AUDIT_DEFAULT_SKIP_LARGER_THAN,
    audit_max_runtime_sec: Optional[float] = None,
    audit_max_bytes: Optional[int] = None,
    discover_concurrency: int = 8,
    magic_concurrency: int = 4,
    audit_concurrency: int = 1,
    openssl_path: str = "openssl",
    callback: Optional[ProgressCallback] = None,
    audit_ledger=None,
) -> ValidationReport:
    """Run a full validation at ``tier`` against ``backend``.

    ``encryption_password`` is required for ``DEEP`` and ``AUDIT`` —
    L0/L1a do not touch keyset material. The function never raises for
    expected validation failures: those land in the report's per-tier
    blocks. Unexpected exceptions (programmer error, backend bug) are
    captured into ``report.error`` so the run still finishes cleanly.

    ``audit_concurrency`` (default 1) controls L2 worker parallelism.
    Values >1 only take effect when the backend declares
    ``supports_concurrent_reads = True`` (LocalBackend does;
    SftpBackend does not — single channel) — otherwise the L2
    driver silently clamps to 1 and emits a LOG event.  See
    :func:`arq_validator.tiers.run_full_audit` for the safety
    contract.
    """
    report = ValidationReport(
        tier=tier.value,
        started_at=time.time(),
        root=root,
        backend_kind=type(backend).__name__,
    )
    emit(
        callback, EventKind.RUN_STARTED,
        f"validation started: tier={tier.value}",
        tier=tier.value, root=root,
        runs=_tier_runs(tier),
    )

    try:
        layouts = L.discover_layout(
            backend, root, concurrency=discover_concurrency,
        )
        emit(
            callback, EventKind.LAYOUT_DISCOVERED,
            f"discovered {len(layouts)} computer subtree(s)",
            computer_count=len(layouts),
            computers=[lay.computer_uuid for lay in layouts],
        )
        for lay in layouts:
            emit(
                callback, EventKind.COMPUTER_FOUND,
                f"computer: {lay.computer_uuid}",
                computer=lay.computer_uuid,
                blobpacks=len(lay.blobpacks),
                treepacks=len(lay.treepacks),
                largeblobpacks=len(lay.largeblobpacks),
                standardobjects=len(lay.standardobjects),
                folders=len(lay.backup_folder_uuids),
                has_keyset=lay.has_keyset,
            )

        report.layout = run_layout_check(layouts, callback=callback)
        if not report.layout.layout_ok:
            return report

        if tier in (
            ValidationTier.QUICK,
            ValidationTier.DEEP,
            ValidationTier.AUDIT,
        ):
            report.magic_check = run_magic_check(
                backend, layouts, root=root,
                sample_fraction=sample_fraction,
                concurrency=magic_concurrency,
                callback=callback,
            )

        if tier in (ValidationTier.DEEP, ValidationTier.AUDIT):
            if not encryption_password:
                report.error = (
                    "encryption_password is required for "
                    f"tier={tier.value}"
                )
                return report
            report.backuprecord = run_backuprecord_check(
                backend, layouts, encryption_password,
                root=root, openssl_path=openssl_path,
                callback=callback,
            )
            if not report.backuprecord.keyset_decrypted:
                # Keyset failure means we can't proceed to L2 either.
                return report

        if tier is ValidationTier.AUDIT:
            assert encryption_password is not None
            report.audit = run_full_audit(
                backend, layouts, encryption_password,
                root=root,
                skip_larger_than=audit_skip_larger_than,
                max_runtime_sec=audit_max_runtime_sec,
                max_bytes=audit_max_bytes,
                openssl_path=openssl_path,
                callback=callback,
                ledger=audit_ledger,
                audit_concurrency=audit_concurrency,
            )

    except Exception as exc:
        report.error = f"{type(exc).__name__}: {exc}"

    finally:
        report.finished_at = time.time()
        emit(
            callback, EventKind.RUN_FINISHED,
            f"validation finished in {report.elapsed_sec:.2f}s",
            tier=tier.value,
            elapsed_sec=report.elapsed_sec,
            error=report.error,
            has_failures=report.has_failures(),
        )

    return report
