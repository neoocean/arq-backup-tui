"""The Arq compat suite must not leak machine-specific absolute paths into
its committed reports, and its scripts must import cleanly on every CI
Python (a nested-quote f-string once broke 3.9/3.11). This pins both.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_SUITE = Path(__file__).resolve().parent.parent / "scripts" / "arq_compat"
sys.path.insert(0, str(_SUITE))


class CompatSuiteImportTests(unittest.TestCase):
    def test_suite_modules_import(self) -> None:
        # Importing also compiles them under the running CI Python, so a
        # version-specific SyntaxError (e.g. a PEP 701 nested-quote f-string
        # under 3.9/3.11) fails here rather than only in the scheduled run.
        import run  # noqa: F401
        import scenarios  # noqa: F401


class ScrubPathsTests(unittest.TestCase):
    def test_repo_prefix_becomes_relative(self) -> None:
        import run
        s = f'{{"path": "{run.REPO}/arq_compat_run/from_arq"}}'
        out = run._scrub_paths(s)
        self.assertNotIn(str(run.REPO), out)
        self.assertIn("arq_compat_run/from_arq", out)

    def test_home_path_becomes_tilde(self) -> None:
        import run
        home = str(Path.home())
        out = run._scrub_paths(f"left {home}/elsewhere/x right")
        self.assertNotIn(home, out)
        self.assertIn("~/elsewhere/x", out)

    def test_non_path_text_unchanged(self) -> None:
        import run
        self.assertEqual(run._scrub_paths("no paths here"), "no paths here")


if __name__ == "__main__":
    unittest.main()
