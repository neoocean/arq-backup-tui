#!/usr/bin/env python3
"""Repo-root launcher for the arq-backup-tui Textual app.

A convenience entry point so the TUI is reachable as
``./arq-tui.py`` (or ``python arq-tui.py``) without first
``pip install``-ing the package or remembering the
``python -m arq_tui`` invocation. The hyphenated filename matches
the published binary name; we cannot ``import`` this file as a
module (Python identifiers don't allow hyphens), but executing it
directly works fine.

Behaviour matches ``python -m arq_tui`` exactly:

- no arguments → launch the Textual GUI on stdin/stdout;
- a ``plans`` subcommand → run the headless plan-management CLI.

The script auto-prepends its own directory to ``sys.path`` so it
keeps working from a freshly-cloned checkout where the
``arq_tui`` package isn't on the import path yet.
"""

from __future__ import annotations

import os
import sys

# Make sure the in-tree ``arq_tui`` / ``arq_writer`` / etc. packages
# are importable when the script is run from a fresh checkout
# without ``pip install -e .``.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from arq_tui.cli import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
