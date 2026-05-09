#!/usr/bin/env python3
"""Run the unit-test suite under ``coverage.py`` and produce a
per-module report.

Wraps the standard ``coverage run -m unittest discover tests``
invocation with sensible defaults from ``.coveragerc``:

- branch coverage on
- only ``arq_writer`` / ``arq_reader`` / ``arq_validator`` /
  ``arq_tui`` count toward the score
- tests, scripts, ``__main__`` modules, and the legacy ``arq5_*``
  modules are excluded

Usage::

    python3 scripts/run_coverage.py [--html] [--quiet]

Without ``--html``, prints a per-module text report to stdout
and returns coverage's own exit code (0 = pass, 2 = report
threshold missed when one's configured). With ``--html``,
additionally writes a browseable report under ``htmlcov/``.

Skips tests/integration/ by default since those need SFTP
credentials; pass ``--with-integration`` to include them.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


def _coverage_available() -> bool:
    try:
        import coverage  # noqa: F401
        return True
    except ImportError:
        return False


def _main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--html", action="store_true",
                   help="emit an HTML report under htmlcov/")
    p.add_argument("--with-integration", action="store_true",
                   help="include tests/integration/ (needs SFTP creds)")
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args(argv)

    if not _coverage_available():
        print(
            "coverage.py not installed. Install with one of:\n"
            "  pip install coverage\n"
            "  brew install python-coverage   # macOS w/ Homebrew\n"
            "Then re-run this script.",
            file=sys.stderr,
        )
        return 1

    repo_root = Path(__file__).resolve().parent.parent
    tests_pat = "test_*.py"
    test_dir = "tests"

    cmd = [
        sys.executable, "-m", "coverage", "run",
        "--rcfile", str(repo_root / ".coveragerc"),
        "-m", "unittest", "discover",
        "-t", str(repo_root),
        "-s", str(repo_root / test_dir),
        "-p", tests_pat,
    ]
    if not args.with_integration:
        # Pass through a Python -W to discover that excludes the
        # integration subdir. unittest discover has no exclude
        # arg, so our trick is to scope discover to the top-level
        # tests dir + skip subdirs by NOT recursing — `discover -p`
        # is non-recursive across packages by default, but the
        # tests/integration/ dir does have an __init__.py, so we
        # need an explicit guard. Simplest path: list each top-
        # level test_*.py individually.
        cmd = cmd[:-7]   # drop the discover args
        cmd += ["-m", "unittest"]
        for tf in sorted((repo_root / test_dir).glob(tests_pat)):
            cmd.append(f"tests.{tf.stem}")

    if not args.quiet:
        print(
            "running: " + " ".join(cmd[:8]) + " …",
            file=sys.stderr,
        )
    cp = subprocess.run(cmd, cwd=repo_root)
    if cp.returncode != 0:
        print(
            f"unittest exited rc={cp.returncode}; "
            f"coverage report below covers what ran",
            file=sys.stderr,
        )

    # Always emit the report — it covers whatever ran, even on
    # partial test failures.
    report_cmd = [
        sys.executable, "-m", "coverage", "report",
        "--rcfile", str(repo_root / ".coveragerc"),
    ]
    rc = subprocess.run(report_cmd, cwd=repo_root).returncode
    if args.html:
        subprocess.run([
            sys.executable, "-m", "coverage", "html",
            "--rcfile", str(repo_root / ".coveragerc"),
        ], cwd=repo_root)
        if not args.quiet:
            print(
                f"HTML report at {repo_root / 'htmlcov' / 'index.html'}",
                file=sys.stderr,
            )
    return rc


if __name__ == "__main__":
    sys.exit(_main())
