"""Independent Arq 7 backup validator.

Validates Arq 7 backups stored on a local filesystem (or any pluggable
backend implementing the small Backend protocol) without requiring the
official Arq.app. The library exposes a layered set of checks that
match Arq's own self-validation hierarchy:

    L0  layout       directory shape + computer UUIDs
    L1a magic        ARQO magic-byte sweep over object files
    L1b head HMAC    keyset decrypt + latest backuprecord HMAC verify
    L2  full audit   HMAC every EncryptedObject (and inner ARQOs)

See ``arq_validator.runner.validate`` for the high-level orchestrator
and ``arq_validator.cli`` for the standalone CLI. The library is
designed to be embedded in a TUI via the ``ProgressCallback`` hook.
"""

from .backend import Backend, LocalBackend
from .events import Event, EventKind, ProgressCallback
from .layout import Arq7ComputerLayout, discover_layout
from .runner import ValidationReport, ValidationTier, validate
from .tiers import (
    BackupRecordResult,
    LayoutResult,
    MagicCheckResult,
    ObjectAuditResult,
)

__all__ = [
    "Backend",
    "LocalBackend",
    "Event",
    "EventKind",
    "ProgressCallback",
    "Arq7ComputerLayout",
    "discover_layout",
    "ValidationReport",
    "ValidationTier",
    "validate",
    "LayoutResult",
    "MagicCheckResult",
    "BackupRecordResult",
    "ObjectAuditResult",
]

__version__ = "0.1.0"
