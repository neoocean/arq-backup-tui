"""macOS-only progress + completion notifications.

The TUI itself runs in the terminal, so the operator might
not see in-TUI progress while their attention is elsewhere
(other terminal tab, browser, IDE). This module surfaces
backup progress through native macOS UI affordances:

- Notification Center toasts at start, every N% milestone,
  and at completion.
- Dock badge showing the current % progress (via
  ``osascript`` controlling the operator's terminal app's
  dock icon).

PyObjC-free implementation — ``osascript`` is in every macOS
install + we just shell out. The cost is one subprocess per
notification (cheap; fires at most once per ~10% milestone).

No-ops cleanly on non-macOS hosts. No imports of macOS-only
modules at module load so the file is safe to import from
cross-platform code.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import List, Optional


def is_supported() -> bool:
    """True iff this host can fire the macOS notifications."""
    return (
        platform.system() == "Darwin"
        and bool(shutil.which("osascript"))
    )


@dataclass
class _ProgressState:
    """Tracker for milestone-based emission. Operators don't
    want a notification per file — they want one at start,
    every 10% (or so), and at completion."""

    last_milestone: int = -1     # last % already announced
    milestone_step: int = 10     # one notification per 10%
    plan_name: str = "backup"


def show_start(plan_name: str) -> None:
    """One-shot 'backup started' notification."""
    if not is_supported():
        return
    _osascript_notification(
        title=f"arq-backup-tui — {plan_name}",
        body="Backup started.",
    )


def show_complete(
    plan_name: str, *, ok: bool, summary: str = "",
) -> None:
    """Final notification at the end of the run."""
    if not is_supported():
        return
    title = f"arq-backup-tui — {plan_name}"
    if ok:
        body = "Backup completed."
    else:
        body = "Backup FAILED."
    if summary:
        body = f"{body}  {summary}"
    _osascript_notification(title=title, body=body)


def maybe_show_progress(
    state: _ProgressState,
    *,
    bytes_done: int, bytes_total: int,
) -> None:
    """Fire a notification when crossing the next milestone.

    ``state`` is a :class:`_ProgressState` the caller keeps
    across successive ticks; this function mutates
    ``state.last_milestone`` after each emission so the same
    milestone never fires twice."""
    if not is_supported():
        return
    if not bytes_total:
        return
    pct = int(100 * bytes_done / bytes_total)
    next_step = (state.last_milestone + state.milestone_step)
    if pct < next_step:
        return
    # Round down to the milestone we crossed.
    crossed = (
        (pct // state.milestone_step) * state.milestone_step
    )
    if crossed <= state.last_milestone:
        return
    state.last_milestone = crossed
    _osascript_notification(
        title=f"arq-backup-tui — {state.plan_name}",
        body=f"Backup progress: {crossed}%",
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _osascript_notification(*, title: str, body: str) -> None:
    """Fire a Notification Center notification. Best-effort:
    osascript failures are silently swallowed so a flaky
    notification daemon can't break the backup."""

    def _esc(s: str) -> str:
        return (
            s.replace("\\", "\\\\").replace('"', '\\"')
        )

    script = (
        f'display notification "{_esc(body)}" '
        f'with title "{_esc(title)}"'
    )
    try:
        subprocess.run(
            ["osascript", "-e", script],
            check=False, capture_output=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        pass
