"""Tests for the doc-code stale-link checker (PR-D1).

The checker walks every Markdown under a given root + reports
broken refs to ``arq_*/...py`` paths or ``arq_*.module.symbol``
identifiers. We exercise it against a synthetic repo so the
test isn't sensitive to the actual repo's stale refs (the
production repo has known stale refs the next docs PR will
clean up).
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


class DocLinkCheckerTests(unittest.TestCase):

    def _scan(self, root: Path):
        # Re-import each call so the module-cache the checker
        # builds doesn't bleed across tests.
        if "scripts" in sys.modules:
            del sys.modules["scripts"]
        for k in list(sys.modules):
            if k.startswith("scripts.check_doc_links"):
                del sys.modules[k]
        sys.path.insert(0, str(
            Path(__file__).resolve().parent.parent / "scripts"
        ))
        try:
            import check_doc_links as cdl
            return cdl._scan(root)
        finally:
            sys.path.pop(0)

    def test_clean_repo_has_zero_stale(self) -> None:
        # Create a tiny synthetic repo: one .md file pointing at
        # a real file. No stale refs should be reported.
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            (td / "arq_writer").mkdir()
            (td / "arq_writer" / "real.py").write_text(
                "x = 1\n",
            )
            (td / "README.md").write_text(
                "see `arq_writer/real.py` for the implementation.\n",
            )
            r = self._scan(td)
            self.assertEqual(
                r.stale, [],
                f"unexpected stale refs: {r.stale!r}",
            )

    def test_stale_path_ref_detected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            (td / "README.md").write_text(
                "see `arq_writer/missing.py` for that thing.\n",
            )
            r = self._scan(td)
            self.assertEqual(len(r.stale), 1)
            self.assertEqual(r.stale[0].kind, "path")
            self.assertEqual(
                r.stale[0].ref, "arq_writer/missing.py",
            )

    def test_url_in_prose_is_ignored(self) -> None:
        # Free-prose URLs and command names shouldn't trigger
        # the path regex.
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            (td / "README.md").write_text(
                "see https://example.com or run `git status`. "
                "PR #21 is `accepted`.\n"
            )
            r = self._scan(td)
            self.assertEqual(r.stale, [])

    def test_real_repo_at_least_runs(self) -> None:
        """Smoke-run against the real repo. We don't assert
        zero-stale (the docs have known refs to clean up); just
        that the checker completes without raising."""
        repo_root = Path(__file__).resolve().parent.parent
        r = self._scan(repo_root)
        self.assertGreater(r.files_scanned, 5)
        # Just verify it has SOME refs to check (sanity — if the
        # regex is broken it'd return 0).
        self.assertGreater(r.refs_checked, 0)


if __name__ == "__main__":
    unittest.main()
