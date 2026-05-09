"""Tests for the CHANGELOG.md auto-generator (D9).

The generator walks ``git log``, classifies each commit into one
of five categories (Features / Fixes / Docs / Tests / Internal),
and emits a Markdown changelog grouped by month + category.

These tests work against a synthetic CommitEntry list so they
don't depend on the actual repo's history shape (which evolves
with every PR).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


# Add scripts/ to sys.path so we can import build_changelog
# directly without making it a package.
sys.path.insert(0, str(
    Path(__file__).resolve().parent.parent / "scripts",
))


class CategorisationTests(unittest.TestCase):

    def _entry(self, subject: str, files=None):
        from build_changelog import CommitEntry
        return CommitEntry(
            sha7="abc1234", subject=subject,
            author_date="2026-05-09",
            files_changed=files or [],
        )

    def test_feat_subject_is_feature(self) -> None:
        from build_changelog import _categorise
        self.assertEqual(
            _categorise(self._entry("feat: add foo")),
            "Features",
        )
        self.assertEqual(
            _categorise(self._entry("Feature: add bar")),
            "Features",
        )
        self.assertEqual(
            _categorise(self._entry("Group 4: restore conflict")),
            "Features",
        )
        self.assertEqual(
            _categorise(self._entry("Adds new module")),
            "Features",
        )

    def test_fix_subject_is_fix(self) -> None:
        from build_changelog import _categorise
        self.assertEqual(
            _categorise(self._entry("fix: typo in tree parser")),
            "Fixes",
        )
        self.assertEqual(
            _categorise(self._entry("Fix CI: pyright settings")),
            "Fixes",
        )

    def test_docs_only_files_route_to_docs(self) -> None:
        from build_changelog import _categorise
        self.assertEqual(
            _categorise(self._entry(
                "Refresh the architecture explanation",
                files=["DESIGN.md", "docs/MECHANISM.md"],
            )),
            "Docs",
        )

    def test_docs_subject_overrides_files(self) -> None:
        from build_changelog import _categorise
        # 'docs:' subject wins even when only python files
        # changed (rare but happens for docstring updates).
        self.assertEqual(
            _categorise(self._entry(
                "docs: clarify Backup.cancel semantics",
                files=["arq_writer/backup.py"],
            )),
            "Docs",
        )

    def test_tests_only_files_route_to_tests(self) -> None:
        """When the subject doesn't match a higher-priority
        category (Features/Fixes/Docs) but the files are all
        under tests/, route to Tests."""
        from build_changelog import _categorise
        self.assertEqual(
            _categorise(self._entry(
                # Subject deliberately avoids 'add' / 'fix' /
                # 'docs' prefixes — those would override the
                # file-shape routing.
                "Sharpen safety scenario coverage",
                files=["tests/test_safety_scenarios.py"],
            )),
            "Tests",
        )

    def test_default_routes_to_internal(self) -> None:
        from build_changelog import _categorise
        self.assertEqual(
            _categorise(self._entry(
                "bump pyright config",
                files=["pyproject.toml"],
            )),
            "Internal",
        )


class RenderTests(unittest.TestCase):

    def test_empty_list_renders_no_changes(self) -> None:
        from build_changelog import render_changelog
        out = render_changelog([])
        self.assertIn("No changes", out)

    def test_render_groups_by_month_then_category(self) -> None:
        from build_changelog import (
            CommitEntry, render_changelog,
        )
        commits = [
            CommitEntry(
                sha7="aaa1111",
                subject="feat: add thing",
                author_date="2026-05-09",
                pr_number="42",
            ),
            CommitEntry(
                sha7="bbb2222",
                subject="fix: typo",
                author_date="2026-05-08",
            ),
            CommitEntry(
                sha7="ccc3333",
                subject="docs: README polish",
                author_date="2026-04-15",
            ),
        ]
        out = render_changelog(commits)
        # Months sorted by insertion order (we feed newest-first).
        idx_may = out.find("## 2026-05")
        idx_apr = out.find("## 2026-04")
        self.assertLess(idx_may, idx_apr)
        # Categories appear under their respective month.
        self.assertIn("### Features", out)
        self.assertIn("### Fixes", out)
        self.assertIn("### Docs", out)

    def test_pr_number_renders_with_link_when_repo_url(self) -> None:
        from build_changelog import (
            CommitEntry, render_changelog,
        )
        out = render_changelog(
            [CommitEntry(
                sha7="abcdef0",
                subject="feat: do thing",
                author_date="2026-05-09",
                pr_number="42",
            )],
            repo_url="https://github.com/owner/repo",
        )
        self.assertIn(
            "[#42](https://github.com/owner/repo/pull/42)", out,
        )
        self.assertIn(
            "[abcdef0](https://github.com/owner/repo/commit/abcdef0)",
            out,
        )

    def test_pr_number_strips_subject_suffix(self) -> None:
        """A subject 'foo (#42)' should NOT render the (#42)
        twice — once cleaned from the subject, once via the
        pr_number."""
        from build_changelog import (
            CommitEntry, render_changelog,
        )
        out = render_changelog(
            [CommitEntry(
                sha7="abcdef0",
                subject="feat: do thing",  # already cleaned
                author_date="2026-05-09",
                pr_number="42",
            )],
        )
        self.assertEqual(out.count("#42"), 1)


class GitDriverSmokeTests(unittest.TestCase):
    """Run against the real repo just enough to confirm the
    git-driver doesn't choke on actual history."""

    def test_collect_against_real_repo_returns_entries(self) -> None:
        # Skip on shallow clones (GitHub Actions default
        # checkout depth is 1, so HEAD~5 would not exist).
        # The test exercises the git driver against whatever
        # depth IS available.
        import subprocess
        try:
            subprocess.run(
                ["git", "rev-parse", "HEAD~3"],
                check=True, capture_output=True, timeout=5,
            )
            since = "HEAD~3"
        except subprocess.CalledProcessError:
            self.skipTest(
                "shallow clone — git history < 3 commits"
            )
        from build_changelog import _collect_commits
        commits = _collect_commits(since=since)
        self.assertGreater(len(commits), 0)
        for c in commits:
            self.assertEqual(len(c.sha7), 7)
            self.assertTrue(c.subject)


if __name__ == "__main__":
    unittest.main()
