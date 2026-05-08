"""Textual-based TUI frontend for arq-backup-tui.

This package depends on `textual` (declared in pyproject.toml's
``tui`` optional-dependencies extra). The library packages
(``arq_validator`` / ``arq_reader`` / ``arq_writer``) stay
stdlib-only; nothing inside them imports anything from here.

Entry points:

- ``python -m arq_tui`` (or ``arq-tui`` once the console script is
  installed) launches the full app.
- :func:`run_app` exposes the same as a function, useful when
  embedding inside another Python program (e.g. tests).
- :class:`ArqTuiApp` is the Textual ``App`` subclass; instantiate
  it directly for headless ``pilot`` testing.
"""

from __future__ import annotations

from .app import ArqTuiApp, run_app

__all__ = ["ArqTuiApp", "run_app"]

__version__ = "0.1.0"
