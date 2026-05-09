"""Persistent set of already-audited blob_ids so subsequent
validation runs can skip what they've already proven good.

The existing ``audit_drip`` tracks a cursor through one sweep
of the file list; it doesn't remember which blob_ids it has
audited across sweeps. For an operator running a daily
``arq-validate audit`` against an O(100k-blob) destination
that's painful — each sweep re-HMACs every object even though
99% of them haven't changed since yesterday.

This module is the orthogonal "skip already-good blobs"
register:

- :class:`AuditLedger` — append-only set of blob_ids that
  passed audit + the last-checked timestamp.
- :func:`load_ledger(path)` / :func:`save_ledger(ledger, path)`
  — atomic JSON read / write.
- :func:`prune_older_than(ledger, age_sec)` — drop entries
  that haven't been re-confirmed in N seconds (so a corrupt
  blob can't permanently mask itself by being in the ledger).

Callers (the new ``--incremental`` flag on
``arq_validator.tiers.run_full_audit`` later) pass the
ledger's ``contains(blob_id)`` to skip + ``record(blob_id)``
to register a fresh pass. The ledger is destination-scoped:
one file per destination (default
``~/.local/state/arq-backup-tui/audit-ledgers/<computer-uuid>.json``).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, Optional, Set


@dataclass
class AuditLedger:
    """Persistent set of audited blob_ids + per-id last-seen
    epoch.

    Schema is intentionally flat (one dict, blob_id → last_ok)
    so a future ``--age`` filter can decide which entries are
    too stale to trust without re-walking. JSON-friendly so
    operators can ``cat`` the file + verify.
    """

    target: str = ""               # destination identifier (cu / label)
    schema_version: int = 1
    blob_last_ok: Dict[str, float] = field(default_factory=dict)
    sweep_count: int = 0
    last_sweep_finished_at: float = 0.0

    def contains(self, blob_id: str) -> bool:
        return blob_id in self.blob_last_ok

    def record(self, blob_id: str, *, when: Optional[float] = None) -> None:
        """Register ``blob_id`` as having passed audit at the
        supplied time (defaults to now). Idempotent —
        re-recording just refreshes the timestamp."""
        self.blob_last_ok[blob_id] = (
            when if when is not None else time.time()
        )

    def forget(self, blob_id: str) -> None:
        """Remove ``blob_id`` from the ledger. Useful when an
        audit later shows the bytes have changed (so the next
        sweep will re-validate them)."""
        self.blob_last_ok.pop(blob_id, None)

    @property
    def size(self) -> int:
        return len(self.blob_last_ok)


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------


def default_ledger_dir() -> Path:
    """Operator-friendly default location, honoring XDG."""
    base = (
        Path(os.environ.get("XDG_STATE_HOME"))
        if os.environ.get("XDG_STATE_HOME")
        else Path.home() / ".local" / "state"
    )
    return base / "arq-backup-tui" / "audit-ledgers"


def ledger_path_for(target: str) -> Path:
    """Per-destination ledger file under the default dir."""
    safe = target.replace("/", "_") or "default"
    return default_ledger_dir() / f"{safe}.json"


def load_ledger(
    path: Path, *, target: str = "",
) -> AuditLedger:
    """Load an existing ledger or return a fresh empty one.

    Schema mismatches and decode errors silently yield a fresh
    ledger so a corrupt state file can't break the validator
    pass — the worst case is the operator re-audits everything
    once."""
    if not path.is_file():
        return AuditLedger(target=target)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return AuditLedger(target=target)
    if data.get("schema_version") != 1:
        return AuditLedger(target=target)
    return AuditLedger(
        target=str(data.get("target") or target),
        schema_version=1,
        blob_last_ok={
            str(k): float(v)
            for k, v in (data.get("blob_last_ok") or {}).items()
        },
        sweep_count=int(data.get("sweep_count") or 0),
        last_sweep_finished_at=float(
            data.get("last_sweep_finished_at") or 0.0
        ),
    )


def save_ledger(
    ledger: AuditLedger, path: Path,
) -> None:
    """Atomic write — same write-tmp-then-rename pattern the
    state-file IPC uses, so a crash mid-save can't leave a
    half-written ledger."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(asdict(ledger), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Maintenance
# ---------------------------------------------------------------------------


def prune_older_than(
    ledger: AuditLedger,
    age_sec: float,
    *, now: Optional[float] = None,
) -> int:
    """Drop ledger entries last confirmed more than
    ``age_sec`` seconds ago. Returns the count removed.

    Operators should call this periodically (e.g. monthly) so
    a blob that quietly went bad after its last ledger entry
    eventually gets re-audited the hard way."""
    cutoff = (now if now is not None else time.time()) - age_sec
    to_drop = [
        b for b, t in ledger.blob_last_ok.items()
        if t < cutoff
    ]
    for b in to_drop:
        del ledger.blob_last_ok[b]
    return len(to_drop)


def merge_ledgers(
    a: AuditLedger, b: AuditLedger,
) -> AuditLedger:
    """Combine two ledgers, keeping the later of the two
    timestamps for each shared blob_id. Useful when running
    audit on multiple machines against the same destination
    + then merging the ledgers manually."""
    out = AuditLedger(
        target=a.target or b.target,
        schema_version=1,
        blob_last_ok=dict(a.blob_last_ok),
        sweep_count=max(a.sweep_count, b.sweep_count),
        last_sweep_finished_at=max(
            a.last_sweep_finished_at,
            b.last_sweep_finished_at,
        ),
    )
    for blob_id, t in b.blob_last_ok.items():
        if blob_id in out.blob_last_ok:
            out.blob_last_ok[blob_id] = max(
                out.blob_last_ok[blob_id], t,
            )
        else:
            out.blob_last_ok[blob_id] = t
    return out
