"""Pre-backup disk-usage estimator.

Operators sometimes start a backup against a destination
that's too small for the source — only to discover hours
later when the writer hits ENOSPC mid-pack-flush. This module
estimates the destination's required size BEFORE the backup
starts so the operator can cancel + free space first.

The estimate has three components:

- **Source plaintext size**: walked via the same logic
  ``arq_writer.dry_run`` uses; honours exclusion + size rules.
- **Compression factor**: a per-extension hint (LZ4 typically
  yields 1.0–1.4× for binary, 1.5–4× for text). Operator-
  supplied; default 1.2× as a conservative overall ratio.
- **Encryption overhead**: ARQO header + AES padding adds
  ~120 bytes per blob on average; for typical 1MB chunks
  this is &lt;0.02% — usually rounded out by other rounding.

The output is a :class:`DiskEstimate` carrying both the raw
estimate AND the destination's current free-space value, so
the caller can emit a single ``destination_undersized``
warning when ``free_space &lt; estimate × safety_factor``.

This is operator-facing diagnostics, NOT a hard gate. The
backup still runs even when the estimate says it won't fit —
operators sometimes WANT to start + see how far they get
(many sources have huge dedup ratios that the estimator
can't model from the source side alone).
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class DiskEstimate:
    """Output of :func:`estimate_destination_size`."""

    source_bytes: int = 0
    estimated_dest_bytes: int = 0
    free_space_bytes: int = 0
    will_fit: bool = True
    safety_margin_factor: float = 1.0
    notes: str = ""

    @property
    def shortfall_bytes(self) -> int:
        """How many bytes short the destination is (positive
        only when ``will_fit`` is False; 0 otherwise)."""
        deficit = (
            self.estimated_dest_bytes - self.free_space_bytes
        )
        return max(0, deficit)


def estimate_destination_size(
    source_bytes: int,
    dest_path: Path,
    *,
    compression_ratio: float = 1.2,
    safety_factor: float = 1.1,
) -> DiskEstimate:
    """Estimate whether ``dest_path`` has enough free space for
    a fresh backup of ``source_bytes``.

    ``compression_ratio`` is source/dest ratio; >1 = compresses
    well, <1 = inflates (rare; happens for already-encrypted /
    already-compressed source data).

    ``safety_factor`` pads the comparison so a marginal fit
    flips to "won't fit". Default 1.1 = require 10% headroom
    over the raw estimate.

    Returns a :class:`DiskEstimate` with both the estimate +
    the destination's actual free space + a will_fit boolean.
    Errors querying free space (path doesn't exist, perms) →
    ``will_fit=True`` + a note (we don't want to gate operator
    action on a transient stat failure).
    """
    if compression_ratio <= 0:
        raise ValueError(
            f"compression_ratio must be > 0, got {compression_ratio}"
        )
    estimated = int(source_bytes / compression_ratio)
    free = 0
    notes_parts = []
    try:
        # On a fresh destination dir that doesn't exist yet,
        # query the parent. Most operators point at a sub-dir
        # of an existing volume.
        target = dest_path
        if not target.exists():
            target = target.parent
        if target.exists():
            usage = shutil.disk_usage(target)
            free = usage.free
        else:
            notes_parts.append(
                f"could not stat free space for {dest_path} "
                f"(neither it nor its parent exists)"
            )
    except OSError as exc:
        notes_parts.append(
            f"disk_usage failed: {exc}"
        )
    will_fit = (
        free == 0
        or free >= int(estimated * safety_factor)
    )
    if free == 0 and notes_parts:
        # Stat error — be optimistic + flag the issue.
        notes_parts.append(
            "treating as will_fit=True (no free-space data)"
        )
    return DiskEstimate(
        source_bytes=source_bytes,
        estimated_dest_bytes=estimated,
        free_space_bytes=free,
        will_fit=will_fit,
        safety_margin_factor=safety_factor,
        notes="; ".join(notes_parts),
    )


def estimate_for_plan(
    plan,
    *,
    compression_ratio: float = 1.2,
    safety_factor: float = 1.1,
) -> DiskEstimate:
    """Convenience: walk every source in a Plan + sum sizes +
    estimate against the plan's primary destination.

    Use :func:`estimate_destination_size` directly when you've
    already walked the source (e.g. via
    :func:`arq_writer.dry_run.dry_run_source`) + want to skip
    the second walk.
    """
    from .dry_run import dry_run_source
    from .exclusions import ExclusionRules
    if (
        plan.exclude_globs
        or plan.exclude_regexes
        or plan.exclude_gitignore_lines
    ):
        rules = ExclusionRules.of(
            wildcard=tuple(plan.exclude_globs),
            regex=tuple(plan.exclude_regexes),
            gitignore_lines=tuple(plan.exclude_gitignore_lines),
        )
    else:
        rules = ExclusionRules.empty()
    total_bytes = 0
    for src in plan.sources:
        s = dry_run_source(
            Path(src), exclusions=rules,
            max_file_bytes=plan.max_file_bytes,
        )
        total_bytes += s.bytes_in_scope
    primary = plan.destination or {}
    dest_path = Path(primary.get("path") or "/tmp")
    return estimate_destination_size(
        total_bytes, dest_path,
        compression_ratio=compression_ratio,
        safety_factor=safety_factor,
    )
