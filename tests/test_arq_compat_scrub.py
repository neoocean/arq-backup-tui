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


class RunProblemsTests(unittest.TestCase):
    """`_run_problems` (drives `--notify`) flags real regressions but not the
    benign plan-config fingerprint polymorphism."""

    def test_clean_result_has_no_problems(self) -> None:
        import run
        clean = {
            "direction_a": {"configs": {
                "v4": {"reader_scenarios": {"a": {"status": "PASS",
                                                  "detail": ""}}}}},
            "direction_b": {"scenarios": {"a": {"status": "PASS_NORM",
                                                "detail": ""}},
                            "arq_error_count": 0},
            "server_db_drift": {"server_db_schema": "none — unchanged"},
        }
        self.assertEqual(run._run_problems(clean), [])

    def test_flags_fail_arq_errors_and_schema_drift(self) -> None:
        import run
        bad = {
            "direction_a": {"configs": {
                "v4-fixed": {"reader_scenarios": {"big": {"status": "FAIL",
                                                          "detail": "x"}}}}},
            "direction_b": {"scenarios": {}, "arq_error_count": 3},
            "server_db_drift": {"server_db_schema": "DRIFT — see detail"},
        }
        probs = run._run_problems(bad)
        joined = " ".join(probs)
        self.assertIn("Dir-A v4-fixed FAIL", joined)
        self.assertIn("errorCount=3", joined)
        self.assertIn("server.db schema DRIFT", joined)


if __name__ == "__main__":
    unittest.main()
