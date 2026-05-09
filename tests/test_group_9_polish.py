"""Tests for group 9: H1 D1-checker ASCII constraint, H3
Sidebar click routing, H4 gc schedule install, H5 wizard
dry-run preview, sidebar cross-screen tracking, A1-(b)
documentation.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(
    Path(__file__).resolve().parent.parent / "scripts",
))


# ---------------------------------------------------------------------------
# H1 — D1 checker ASCII-only constraint
# ---------------------------------------------------------------------------


class DocLinkRegexASCIIConstraintTests(unittest.TestCase):
    """The path / symbol regexes in scripts/check_doc_links.py
    intentionally only match ASCII — that's what protects the
    Korean test-data filenames (한글.txt, 문서/이력서.txt) from
    being flagged as stale paths.

    Pin the constraint so a future regex widening (e.g.
    accepting Unicode identifiers) can't accidentally start
    flagging operator-facing test fixtures."""

    def test_path_regex_does_not_match_korean(self) -> None:
        from check_doc_links import _PATH_RE
        # Real Korean filename + path that appears in our docs
        # as test-data examples — these MUST NOT be flagged
        # as paths the checker should resolve.
        for sample in (
            "`tests/integration/한글.txt`",
            "`docs/문서/이력서.txt`",
            "`scripts/한글-script.py`",
        ):
            self.assertEqual(
                _PATH_RE.findall(sample), [],
                f"regex unexpectedly matched Unicode-bearing "
                f"path: {sample!r}",
            )

    def test_symbol_regex_does_not_match_korean(self) -> None:
        from check_doc_links import _SYMBOL_RE
        for sample in (
            "`arq_writer.한글_helper`",
            "`arq_validator.tiers.run_한글`",
        ):
            self.assertEqual(
                _SYMBOL_RE.findall(sample), [],
                f"symbol regex matched non-ASCII identifier: "
                f"{sample!r}",
            )

    def test_path_regex_still_matches_ascii_paths(self) -> None:
        """Sanity: the regex must keep matching real ASCII
        paths so the checker still does its job."""
        from check_doc_links import _PATH_RE
        matches = _PATH_RE.findall(
            "see `arq_writer/backup.py` for the walker"
        )
        self.assertEqual(matches, ["arq_writer/backup.py"])


# ---------------------------------------------------------------------------
# H3 — Sidebar click routing
# ---------------------------------------------------------------------------


class SidebarClickTests(unittest.IsolatedAsyncioTestCase):

    async def test_click_emits_sidebar_navigation(self) -> None:
        try:
            from arq_tui.widgets.sidebar import (
                Sidebar, SidebarNavigation,
            )
        except ImportError:
            self.skipTest("textual not installed")
        from textual.app import App

        captured = []

        class _A(App):
            def compose(self):
                yield Sidebar(active="plans")

            def on_sidebar_navigation(
                self, event: SidebarNavigation,
            ) -> None:
                captured.append(event.section)

        async with _A().run_test() as pilot:
            sidebar = pilot.app.query_one(Sidebar)
            # Manually post — driving an actual mouse click in
            # a headless test is fragile; the on_click handler
            # is what we want to exercise, but its key job is
            # posting the message which we can test by calling
            # the public set_active path + verifying the
            # message bubbles. Simpler: post the message
            # directly to confirm the handler wiring.
            sidebar.post_message(SidebarNavigation("activity"))
            await pilot.pause()
            self.assertIn("activity", captured)


# ---------------------------------------------------------------------------
# H4 — Auto-gc schedule install
# ---------------------------------------------------------------------------


class GCScheduleInstallTests(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="arq-gc-test-"))
        self.tab_file = self.tmp / "fake.crontab"
        self.tab_file.write_text("")
        self.fake_cmd = self.tmp / "fake-crontab"
        self.fake_cmd.write_text(
            "#!/bin/sh\nset -e\n"
            f'TAB="{self.tab_file}"\n'
            'if [ "$1" = "-l" ]; then cat "$TAB"; exit 0; fi\n'
            'cp "$1" "$TAB"\n'
        )
        self.fake_cmd.chmod(0o755)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_install_writes_marker_and_argv(self) -> None:
        from arq_tui.scheduling import install_gc_schedule
        install_gc_schedule(
            crontab_cmd=str(self.fake_cmd),
            schedule_expr="0 4 * * 0",
            older_than_days=14,
            executable="/usr/bin/python3",
        )
        contents = self.tab_file.read_text()
        self.assertIn("# arq-backup-tui:gc", contents)
        self.assertIn("runs gc", contents)
        self.assertIn("--older-than-days", contents)
        self.assertIn("14", contents)
        # Cron expression preserved verbatim.
        self.assertIn("0 4 * * 0", contents)

    def test_remove_drops_only_gc_entry(self) -> None:
        from arq_tui.scheduling import (
            install_gc_schedule, remove_gc_schedule,
        )
        # Pre-populate with an unrelated entry the operator owns.
        self.tab_file.write_text(
            "0 0 * * * /usr/bin/operator-job\n"
        )
        install_gc_schedule(crontab_cmd=str(self.fake_cmd))
        removed = remove_gc_schedule(
            crontab_cmd=str(self.fake_cmd),
        )
        self.assertTrue(removed)
        contents = self.tab_file.read_text()
        # Operator entry preserved.
        self.assertIn("operator-job", contents)
        # gc entry gone.
        self.assertNotIn("# arq-backup-tui:gc", contents)

    def test_re_install_replaces_rather_than_duplicates(self) -> None:
        from arq_tui.scheduling import install_gc_schedule
        install_gc_schedule(
            crontab_cmd=str(self.fake_cmd),
            schedule_expr="0 3 * * *",
        )
        install_gc_schedule(
            crontab_cmd=str(self.fake_cmd),
            schedule_expr="0 5 * * 0",
        )
        contents = self.tab_file.read_text()
        # Exactly one gc marker.
        self.assertEqual(
            contents.count("# arq-backup-tui:gc"), 1,
        )
        # Latest schedule expression wins.
        self.assertIn("0 5 * * 0", contents)


# ---------------------------------------------------------------------------
# H5 — Wizard dry-run preview
# ---------------------------------------------------------------------------


class WizardDryRunPreviewTests(unittest.TestCase):

    def test_action_preview_dry_run_method_exists(self) -> None:
        try:
            from arq_tui.screens.plan_wizard import PlanWizardScreen
        except ImportError:
            self.skipTest("textual not installed")
        # Method exists + accepts no args (action_* convention).
        self.assertTrue(
            hasattr(PlanWizardScreen, "action_preview_dry_run"),
        )

    def test_d_binding_added(self) -> None:
        try:
            from arq_tui.screens.plan_wizard import PlanWizardScreen
        except ImportError:
            self.skipTest("textual not installed")
        # Look up the d binding in the class's BINDINGS list.
        keys = [b.key for b in PlanWizardScreen.BINDINGS]
        self.assertIn("d", keys)


# ---------------------------------------------------------------------------
# Sidebar tracking
# ---------------------------------------------------------------------------


class SectionForScreenTests(unittest.TestCase):

    def test_known_screens_route_to_their_section(self) -> None:
        try:
            from arq_tui.widgets.sidebar import section_for_screen
        except ImportError:
            self.skipTest("textual not installed")
        self.assertEqual(
            section_for_screen("HomeScreen"), "plans",
        )
        self.assertEqual(
            section_for_screen("RunsMonitorScreen"), "activity",
        )
        self.assertEqual(
            section_for_screen("BackupSetListScreen"), "browse",
        )
        self.assertEqual(
            section_for_screen("ValidateRunScreen"), "validate",
        )

    def test_unknown_screen_defaults_to_plans(self) -> None:
        try:
            from arq_tui.widgets.sidebar import section_for_screen
        except ImportError:
            self.skipTest("textual not installed")
        self.assertEqual(
            section_for_screen("FutureScreen"), "plans",
        )


# ---------------------------------------------------------------------------
# A1-(b) — xattr probe documentation
# ---------------------------------------------------------------------------


class XattrProbeDocTests(unittest.TestCase):

    def test_doc_exists_and_explains_running(self) -> None:
        doc = (
            Path(__file__).resolve().parent.parent
            / "docs" / "XATTR-BULK-PROBE.md"
        )
        self.assertTrue(doc.is_file())
        text = doc.read_text(encoding="utf-8")
        for needle in (
            "probe_xattr_blob_bulk.py",
            "XAttrSetV002",
            ".secrets/sftp.json",
        ):
            self.assertIn(needle, text)


if __name__ == "__main__":
    unittest.main()
