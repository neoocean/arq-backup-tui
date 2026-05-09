"""Dry-run a backup plan: walk the source + apply the plan's
exclusion + size rules + report what WOULD be backed up, without
encrypting or writing any blob.

Operators want this before kicking off a real backup against an
unfamiliar source — to confirm the exclusion rules actually
exclude what they expect, to estimate disk usage at the
destination, and to spot huge files that would otherwise blow
through the size cap.

Pure read-only on the source. No keyset is decrypted, no
destination is touched, no openssl is invoked. The walk uses
the same :class:`arq_writer.exclusions.ExclusionRules` the real
backup would.

API surface:

- :class:`DryRunSummary` — totals + the largest-N files +
  per-extension breakdown.
- :func:`dry_run_plan(plan)` / :func:`dry_run_source(src, …)` —
  return one summary covering the supplied plan / source(s).

The CLI exposes this as ``arq-backup create … --dry-run`` (added
in :mod:`arq_writer.cli`).
"""

from __future__ import annotations

import os
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple


@dataclass
class DryRunFileEntry:
    """One file the dry run encountered. Captured for the
    largest-N + skipped lists; omitted from the count of in-scope
    files (which is just an integer)."""

    path: str
    size: int
    skipped_reason: str = ""    # "" = in-scope


@dataclass
class DryRunSummary:
    """What a real backup would do, without doing it."""

    sources: List[str] = field(default_factory=list)
    files_in_scope: int = 0
    bytes_in_scope: int = 0
    dirs_walked: int = 0
    files_skipped_size: int = 0
    files_skipped_excluded: int = 0
    files_unreadable: int = 0
    largest_in_scope: List[DryRunFileEntry] = field(
        default_factory=list,
    )
    extensions: Counter = field(default_factory=Counter)
    elapsed_sec: float = 0.0

    def add_in_scope(self, entry: DryRunFileEntry) -> None:
        self.files_in_scope += 1
        self.bytes_in_scope += entry.size
        # Maintain a sorted-by-size top-N list; cap so we don't
        # accumulate millions of entries on huge sources.
        self.largest_in_scope.append(entry)
        self.largest_in_scope.sort(
            key=lambda e: e.size, reverse=True,
        )
        del self.largest_in_scope[20:]
        ext = Path(entry.path).suffix.lower() or "(no ext)"
        self.extensions[ext] += 1


# ---------------------------------------------------------------------------
# Walkers
# ---------------------------------------------------------------------------


def dry_run_source(
    source: Path,
    *,
    exclusions=None,
    max_file_bytes: Optional[int] = None,
) -> DryRunSummary:
    """Walk ``source`` + return what would be backed up.

    ``exclusions`` is the same :class:`~arq_writer.exclusions.ExclusionRules`
    a real Backup uses; ``max_file_bytes`` mirrors
    :attr:`Backup.max_file_bytes`. Both default to no filtering
    so this can also serve as a "raw inventory" tool.
    """
    summary = DryRunSummary(sources=[str(source)])
    started = time.time()
    from .exclusions import ExclusionRules
    rules = exclusions if exclusions is not None else ExclusionRules.empty()
    source = Path(source)
    if not source.is_dir():
        raise ValueError(f"source is not a directory: {source}")

    for root, dirs, files in os.walk(source, followlinks=False):
        rel_root = os.path.relpath(root, source)
        if rel_root == ".":
            rel_root = ""
        summary.dirs_walked += 1
        # Drop excluded dirs in-place so os.walk skips them.
        if not rules.is_empty:
            kept_dirs = []
            for d in dirs:
                rel_d = (
                    f"{rel_root}/{d}" if rel_root else d
                )
                if rules.excludes(rel_d, is_dir=True):
                    summary.files_skipped_excluded += 1
                else:
                    kept_dirs.append(d)
            dirs[:] = kept_dirs
        for fname in files:
            rel_path = (
                f"{rel_root}/{fname}" if rel_root else fname
            )
            full = os.path.join(root, fname)
            if not rules.is_empty and rules.excludes(
                rel_path, is_dir=False,
            ):
                summary.files_skipped_excluded += 1
                continue
            try:
                size = os.path.getsize(full)
            except OSError:
                summary.files_unreadable += 1
                continue
            if (
                max_file_bytes is not None
                and size > max_file_bytes
            ):
                summary.files_skipped_size += 1
                continue
            summary.add_in_scope(DryRunFileEntry(
                path=rel_path, size=size,
            ))
    summary.elapsed_sec = time.time() - started
    return summary


def dry_run_plan(plan, *, exclusions=None) -> DryRunSummary:
    """Convenience: dry-run every source in a Plan as a single
    aggregated summary.

    Pulls ``max_file_bytes`` and (when ``exclusions`` is None)
    the plan's own exclusion rules so the dry-run output matches
    what the real Backup would produce."""
    from .exclusions import ExclusionRules
    if exclusions is None:
        if (
            plan.exclude_globs
            or plan.exclude_regexes
            or plan.exclude_gitignore_lines
        ):
            exclusions = ExclusionRules.of(
                wildcard=tuple(plan.exclude_globs),
                regex=tuple(plan.exclude_regexes),
                gitignore_lines=tuple(plan.exclude_gitignore_lines),
            )
        else:
            exclusions = ExclusionRules.empty()
    aggregate = DryRunSummary(sources=list(plan.sources))
    for src in plan.sources:
        s = dry_run_source(
            Path(src),
            exclusions=exclusions,
            max_file_bytes=plan.max_file_bytes,
        )
        aggregate.files_in_scope += s.files_in_scope
        aggregate.bytes_in_scope += s.bytes_in_scope
        aggregate.dirs_walked += s.dirs_walked
        aggregate.files_skipped_size += s.files_skipped_size
        aggregate.files_skipped_excluded += s.files_skipped_excluded
        aggregate.files_unreadable += s.files_unreadable
        for e in s.largest_in_scope:
            aggregate.largest_in_scope.append(e)
        aggregate.largest_in_scope.sort(
            key=lambda e: e.size, reverse=True,
        )
        del aggregate.largest_in_scope[20:]
        aggregate.extensions.update(s.extensions)
        aggregate.elapsed_sec += s.elapsed_sec
    return aggregate
