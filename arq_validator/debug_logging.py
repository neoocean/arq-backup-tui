"""Centralised debug-logging configuration.

Operators run into "why is the backup slow / stuck / failing"
questions where the real signal is in per-blob / per-SFTP-call
timing. Adding ad-hoc print() statements per module wouldn't
let them filter / redirect / suppress what they don't want.
This module wires Python's standard ``logging`` package to:

- one root logger named ``"arq_backup_tui"`` (so external apps
  importing the libraries can configure it independently);
- separate child loggers per subsystem (``"arq_backup_tui.sftp"``,
  ``"arq_backup_tui.blob"``, ``"arq_backup_tui.tree"``,
  ``"arq_backup_tui.cli"``) so operators can crank up just the
  noisy bit they need;
- a single :func:`enable_debug_logging` entry point each CLI
  calls when ``--debug`` (or ``--debug=sftp``) is passed.

Why a separate module: the writer / reader / validator each
have their own callback contract (the ProgressCb pattern) but
those events are deliberately minimal — they're for UI panels,
not for diagnostic deep dives. Operators investigating an issue
need the granularity that would drown a UI. Logging is the
right channel for that.

The module is also import-cheap (only imports ``logging`` +
``sys``) so it can be imported from any subsystem without
pulling in the wider library graph.
"""

from __future__ import annotations

import logging
import sys
from typing import List, Optional


_ROOT_NAME = "arq_backup_tui"

# Subsystem loggers operators are most likely to want to filter
# on. Each is a child of _ROOT_NAME so configuring the root
# affects all of them; configuring one specifically overrides.
_SUBSYSTEMS = (
    "sftp",          # SFTP commands + responses + reconnects
    "blob",          # per-blob fetch / write / cache hit
    "tree",          # tree walk events (planning + restore)
    "cli",           # CLI argv + outcome
    "backend",       # backend protocol op-level dispatch
    "crypto",        # keyset decrypt + encrypt-object operations
)


def get_logger(subsystem: str = "") -> logging.Logger:
    """Return the named logger for ``subsystem``.

    Empty string returns the root logger. Use this from
    subsystem code instead of ``logging.getLogger(__name__)``
    so operators can configure all of arq_backup_tui's logs
    via one well-known root."""
    if not subsystem:
        return logging.getLogger(_ROOT_NAME)
    if subsystem not in _SUBSYSTEMS:
        # Tolerate unknown names rather than raising — better
        # to log under the wrong child than to silently lose
        # the message. Operators see the name in the output.
        pass
    return logging.getLogger(f"{_ROOT_NAME}.{subsystem}")


def enable_debug_logging(
    *,
    subsystems: Optional[List[str]] = None,
    stream=None,
    level: int = logging.DEBUG,
) -> None:
    """Configure the arq_backup_tui logger family for verbose
    output.

    ``subsystems`` (default = all) selects which child loggers
    get the verbose level; the rest stay at WARNING. Pass e.g.
    ``["sftp", "blob"]`` when only the network + per-blob
    timing matters and the rest is just noise.

    ``stream`` defaults to ``sys.stderr`` so debug output stays
    out of the JSON-events stdout the CLIs use for machine
    consumption. Pass ``open("/tmp/run.log", "w")`` to redirect.

    Idempotent: re-calling does not double up handlers — each
    call clears existing handlers under the root + re-installs
    one per subsystem.
    """
    root = logging.getLogger(_ROOT_NAME)
    # Clear any previous handlers we installed so a second call
    # doesn't double-log.
    for h in list(root.handlers):
        root.removeHandler(h)
    # Install one stderr handler at the root; child loggers
    # inherit propagation by default.
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s.%(msecs)03d  %(name)-30s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    ))
    root.addHandler(handler)
    root.setLevel(logging.WARNING)   # default for the family

    selected = set(subsystems) if subsystems else set(_SUBSYSTEMS)
    for sub in _SUBSYSTEMS:
        child = logging.getLogger(f"{_ROOT_NAME}.{sub}")
        if sub in selected:
            child.setLevel(level)
        else:
            child.setLevel(logging.WARNING)
        child.propagate = True


def is_debug_enabled(subsystem: str = "") -> bool:
    """Operators / library code can short-circuit expensive
    diagnostic-payload formatting by checking this first."""
    log = get_logger(subsystem)
    return log.isEnabledFor(logging.DEBUG)


def parse_debug_flag(value: str) -> List[str]:
    """Convert a ``--debug`` value to the subsystem list.

    Accepts:
    - empty / None → all subsystems
    - "all" → all subsystems
    - comma-separated names → that subset

    Unknown names are silently passed through to
    :func:`enable_debug_logging`'s tolerant lookup; an operator
    typo means "no logger at that name" rather than "crash on
    bad CLI value".
    """
    if not value or value == "all":
        return list(_SUBSYSTEMS)
    return [s.strip() for s in value.split(",") if s.strip()]
