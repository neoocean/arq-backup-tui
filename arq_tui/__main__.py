"""``python -m arq_tui`` entry point.

With no arguments, launches the Textual GUI. With a recognized
subcommand (``plans list`` / ``plans show`` / ``plans delete``),
runs the headless plan-management CLI in :mod:`arq_tui.cli`.
"""

from __future__ import annotations

from .cli import main


if __name__ == "__main__":
    raise SystemExit(main())
