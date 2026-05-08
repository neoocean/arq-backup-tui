"""Independent Arq 7 backup validator.

Validates Arq 7 backups stored on a local filesystem or any pluggable
backend (SFTP ships in :mod:`arq_validator.sftp`) without requiring
the official Arq.app. The library exposes a layered set of checks
that match Arq's own self-validation hierarchy:

    L0  layout       directory shape + computer UUIDs
    L1a magic        ARQO magic-byte sweep over object files
    L1b head HMAC    keyset decrypt + latest backuprecord HMAC verify
    L2  full audit   HMAC every EncryptedObject (and inner ARQOs)

For long-running L2 sweeps, :func:`audit_drip.run_audit_drip` provides
a cursor-resumable runner with optional rate-throttle + time budget.

See :func:`runner.validate` for the high-level orchestrator and
:mod:`arq_validator.cli` for the standalone CLI. The library is
designed to be embedded in a TUI via the ``ProgressCallback`` hook.
"""

from .audit_drip import (
    AuditDripState,
    Throttle,
    load_state as load_audit_drip_state,
    pause as pause_audit_drip,
    resume as resume_audit_drip,
    run_audit_drip,
    save_state as save_audit_drip_state,
)
from .backend import Backend, LocalBackend
from .compatibility import (
    CheckResult,
    ComplianceReport,
    check_arq7_compatibility,
)
from .events import Event, EventKind, ProgressCallback
from .fingerprint import (
    compute_shape_fingerprint,
    diff_fingerprints,
)
from .layout import Arq7ComputerLayout, discover_layout
from .runner import ValidationReport, ValidationTier, validate
from .sftp import SftpBackend, SftpConnectionError
from .tiers import (
    BackupRecordResult,
    LayoutResult,
    MagicCheckResult,
    ObjectAuditResult,
)

__all__ = [
    # Backends
    "Backend",
    "LocalBackend",
    "SftpBackend",
    "SftpConnectionError",
    # Arq 7 compatibility checker
    "CheckResult",
    "ComplianceReport",
    "check_arq7_compatibility",
    "compute_shape_fingerprint",
    "diff_fingerprints",
    # Events
    "Event",
    "EventKind",
    "ProgressCallback",
    # Layout
    "Arq7ComputerLayout",
    "discover_layout",
    # Validation
    "ValidationReport",
    "ValidationTier",
    "validate",
    "LayoutResult",
    "MagicCheckResult",
    "BackupRecordResult",
    "ObjectAuditResult",
    # Audit-drip
    "AuditDripState",
    "Throttle",
    "run_audit_drip",
    "pause_audit_drip",
    "resume_audit_drip",
    "load_audit_drip_state",
    "save_audit_drip_state",
]

__version__ = "0.1.0"
