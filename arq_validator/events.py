"""Progress events emitted by validation tiers.

The validator pushes :class:`Event` instances to a user-supplied
``ProgressCallback`` so a TUI (or any UI) can render live progress
without polling. Events are designed to be self-contained and
serializable: the TUI only needs to dispatch on ``kind`` and read
the typed fields.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Optional


class EventKind(Enum):
    """All progress event types emitted by validation tiers."""

    # Lifecycle
    RUN_STARTED = "run_started"
    RUN_FINISHED = "run_finished"
    TIER_STARTED = "tier_started"
    TIER_FINISHED = "tier_finished"

    # Layout discovery
    LAYOUT_DISCOVERED = "layout_discovered"
    COMPUTER_FOUND = "computer_found"

    # L1a magic-byte sweep
    MAGIC_CHECK_PROGRESS = "magic_check_progress"
    MAGIC_CHECK_FAILED = "magic_check_failed"

    # L1b keyset / backuprecord
    KEYSET_DECRYPTED = "keyset_decrypted"
    KEYSET_FAILED = "keyset_failed"
    BACKUPRECORD_VERIFIED = "backuprecord_verified"
    BACKUPRECORD_FAILED = "backuprecord_failed"

    # L2 audit
    AUDIT_FILE_VERIFIED = "audit_file_verified"
    AUDIT_FILE_FAILED = "audit_file_failed"
    AUDIT_FILE_SKIPPED = "audit_file_skipped"
    AUDIT_PROGRESS = "audit_progress"

    # Audit-drip lifecycle
    AUDIT_DRIP_FIRE_STARTED = "audit_drip_fire_started"
    AUDIT_DRIP_FIRE_FINISHED = "audit_drip_fire_finished"
    AUDIT_DRIP_SWEEP_STARTED = "audit_drip_sweep_started"
    AUDIT_DRIP_SWEEP_COMPLETED = "audit_drip_sweep_completed"
    AUDIT_DRIP_PROGRESS = "audit_drip_progress"
    AUDIT_DRIP_ABORTED = "audit_drip_aborted"
    AUDIT_DRIP_PAUSED = "audit_drip_paused"

    # Diagnostics / informational
    LOG = "log"


@dataclass
class Event:
    """Single progress event.

    ``payload`` is a free-form dict. Each ``EventKind`` has a defined
    set of payload keys (documented at the call site that emits the
    event); UIs should treat unknown keys as forward-compatible.
    """

    kind: EventKind
    message: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)


ProgressCallback = Callable[[Event], None]


def noop_callback(event: Event) -> None:
    """No-op callback for callers that don't want progress events."""
    return None


def emit(
    callback: Optional[ProgressCallback],
    kind: EventKind,
    message: str = "",
    **payload: Any,
) -> None:
    """Build and dispatch an ``Event``, swallowing callback exceptions.

    Validation must not fail because a UI handler raised — the loop
    keeps running and the operator's terminal stays responsive.
    """
    if callback is None:
        return
    try:
        callback(Event(kind=kind, message=message, payload=payload))
    except Exception:
        pass
