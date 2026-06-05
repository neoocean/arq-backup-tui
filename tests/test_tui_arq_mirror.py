"""TUI integration: the Home screen mirrors a locally-installed
Arq.app's plans (read-only) alongside the operator's own plans.

Uses an injected :class:`ArqAppSource` built from the synthetic fixture
DB in :mod:`tests.test_tui_arq_app`, so no real Arq install is touched.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.test_tui_arq_app import _build_fixture_db

try:
    import textual  # noqa: F401
    HAS_TEXTUAL = True
except ImportError:  # pragma: no cover
    HAS_TEXTUAL = False


def _fixture_source(td: str):
    from arq_tui.arq_app import ArqAppSource
    db = Path(td) / "server.db"
    _build_fixture_db(db)
    return ArqAppSource(db)


@unittest.skipUnless(HAS_TEXTUAL, "textual not installed; install with .[tui]")
class HomeArqMirrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_home_shows_arq_plans_when_mirror_present(self) -> None:
        from arq_tui import ArqTuiApp
        with tempfile.TemporaryDirectory() as cfg, \
                tempfile.TemporaryDirectory() as dbdir:
            app = ArqTuiApp(
                config_dir=Path(cfg), arq_app=_fixture_source(dbdir),
            )
            async with app.run_test() as pilot:
                await pilot.pause()
                plans = app.screen._load_plans()
                names = {p.name for p in plans}
                # Only ACTIVE Arq plans are mirrored — "Archive plan"
                # (PLAN-RETAINALL, active=0 in the fixture) is excluded,
                # matching the Arq GUI which hides deactivated plans.
                self.assertEqual(names, {"Buzhash plan", "Fixed plan"})
                self.assertNotIn("Archive plan", names)
                self.assertTrue(all(p.origin == "arq" for p in plans))
                # Populated list, not the empty-state hint.
                app.screen.query_one("#plans-list")
                await pilot.press("q")

    async def test_arq_rows_are_badged(self) -> None:
        from arq_tui import ArqTuiApp
        from arq_tui.state import Plan
        with tempfile.TemporaryDirectory() as cfg, \
                tempfile.TemporaryDirectory() as dbdir:
            app = ArqTuiApp(
                config_dir=Path(cfg), arq_app=_fixture_source(dbdir),
            )
            async with app.run_test() as pilot:
                await pilot.pause()
                screen = app.screen
                arq_plan = next(
                    p for p in screen._load_plans() if p.origin == "arq"
                )
                self.assertIn("◆ Arq", screen._render_plan_row(arq_plan))
                own = Plan(plan_id="x", name="My own", origin="tui")
                self.assertNotIn("◆ Arq", screen._render_plan_row(own))
                await pilot.press("q")

    async def test_editable_rows_bright_readonly_rows_dim(self) -> None:
        # Own (editable) plans render with .plan-own; read-only Arq
        # plans with .plan-arq, so editability reads at a glance.
        from arq_tui import ArqTuiApp
        from arq_tui.state import Plan, PlanRegistry
        from textual.widgets import Static
        with tempfile.TemporaryDirectory() as cfg, \
                tempfile.TemporaryDirectory() as dbdir:
            reg = PlanRegistry(config_dir=Path(cfg))
            reg.save(Plan(
                plan_id="own1", name="My own plan", origin="tui",
                sources=["/x"], destination={"path": "/d"},
            ))
            app = ArqTuiApp(
                config_dir=Path(cfg), plan_registry=reg,
                arq_app=_fixture_source(dbdir),
            )
            async with app.run_test() as pilot:
                await pilot.pause()
                statics = app.screen.query("#plans-list Static")
                classes = [set(s.classes) for s in statics]
                self.assertTrue(
                    any("plan-arq" in c for c in classes),
                    "expected a dimmed read-only Arq row",
                )
                self.assertTrue(
                    any("plan-own" in c for c in classes),
                    "expected a bright editable own row",
                )
                await pilot.press("q")

    async def test_plan_detail_tree_populates(self) -> None:
        # The right column shows the focused plan's info as a tree.
        from arq_tui import ArqTuiApp
        from textual.widgets import Tree
        with tempfile.TemporaryDirectory() as cfg, \
                tempfile.TemporaryDirectory() as dbdir:
            app = ArqTuiApp(
                config_dir=Path(cfg), arq_app=_fixture_source(dbdir),
            )
            async with app.run_test() as pilot:
                await pilot.pause()
                tree = app.screen.query_one("#plan-detail-tree", Tree)
                self.assertTrue(tree.display)

                def _labels(node):
                    out = [str(node.label)]
                    for c in node.children:
                        out.extend(_labels(c))
                    return out
                labels = " | ".join(_labels(tree.root))
                self.assertIn("Destination:", labels)
                self.assertIn("Chunker:", labels)
                self.assertIn("Last backup:", labels)
                self.assertIn("Sources", labels)
                await pilot.press("q")

    async def test_own_plan_overrides_arq_plan_with_same_id(self) -> None:
        # An own plan whose id == an Arq planUUID should win (no dupes).
        from arq_tui import ArqTuiApp
        from arq_tui.state import Plan, PlanRegistry
        with tempfile.TemporaryDirectory() as cfg, \
                tempfile.TemporaryDirectory() as dbdir:
            reg = PlanRegistry(config_dir=Path(cfg))
            reg.save(Plan(
                plan_id="PLAN-BUZHASH", name="Adopted", origin="tui",
                sources=["/x"], destination={"path": "/d"},
            ))
            app = ArqTuiApp(
                config_dir=Path(cfg), plan_registry=reg,
                arq_app=_fixture_source(dbdir),
            )
            async with app.run_test() as pilot:
                await pilot.pause()
                plans = app.screen._load_plans()
                buzhash_like = [
                    p for p in plans if p.plan_id == "PLAN-BUZHASH"
                ]
                self.assertEqual(len(buzhash_like), 1)
                self.assertEqual(buzhash_like[0].origin, "tui")
                self.assertEqual(buzhash_like[0].name, "Adopted")
                await pilot.press("q")

    async def test_editing_arq_plan_is_blocked(self) -> None:
        from arq_tui import ArqTuiApp
        with tempfile.TemporaryDirectory() as cfg, \
                tempfile.TemporaryDirectory() as dbdir:
            app = ArqTuiApp(
                config_dir=Path(cfg), arq_app=_fixture_source(dbdir),
            )
            async with app.run_test() as pilot:
                await pilot.pause()
                notes = []
                app.screen.notify = lambda *a, **k: notes.append((a, k))
                # Focus the plans list, then attempt edit on first row.
                app.screen.action_edit_focused()
                await pilot.pause()
                # A warning was raised and no wizard pushed.
                self.assertTrue(notes, "expected a read-only warning")
                self.assertNotEqual(
                    app.screen.__class__.__name__, "PlanWizardScreen",
                )
                await pilot.press("q")

    async def test_running_cloud_plan_is_guarded(self) -> None:
        # An Arq plan with no openable destination (cloud) must warn,
        # not launch a backup at an empty path.
        from arq_tui import ArqTuiApp
        from arq_tui.state import Plan
        with tempfile.TemporaryDirectory() as cfg:
            app = ArqTuiApp(config_dir=Path(cfg), arq_app=None)
            async with app.run_test() as pilot:
                await pilot.pause()
                notes = []
                app.screen.notify = lambda *a, **k: notes.append((a, k))
                cloud = Plan(
                    plan_id="c", name="Cloud plan", origin="arq",
                    destination_kind="local", destination={"path": ""},
                )
                app.screen._run_plan(cloud)
                await pilot.pause()
                self.assertTrue(notes, "expected a cloud-destination warning")
                self.assertEqual(
                    app.screen.__class__.__name__, "HomeScreen",
                )
                await pilot.press("q")

    async def test_no_mirror_when_arq_app_none(self) -> None:
        # Explicit arq_app=None disables the mirror entirely.
        from arq_tui import ArqTuiApp
        with tempfile.TemporaryDirectory() as cfg:
            app = ArqTuiApp(config_dir=Path(cfg), arq_app=None)
            async with app.run_test() as pilot:
                await pilot.pause()
                self.assertEqual(app.screen._load_plans(), [])
                app.screen.query_one("#plans-empty")
                await pilot.press("q")


@unittest.skipUnless(HAS_TEXTUAL, "textual not installed; install with .[tui]")
class BackupSetsArqMirrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_destinations_include_arq_openable_locations(self) -> None:
        from arq_tui import ArqTuiApp
        from arq_tui.screens.backup_sets import (
            BackupSetListScreen, StoragePanel,
        )
        with tempfile.TemporaryDirectory() as cfg, \
                tempfile.TemporaryDirectory() as dbdir:
            app = ArqTuiApp(
                config_dir=Path(cfg), arq_app=_fixture_source(dbdir),
            )
            async with app.run_test() as pilot:
                await pilot.pause()
                app.push_screen(BackupSetListScreen())
                await pilot.pause()
                dests = app.screen.query_one(StoragePanel)._merged_destinations()
                # folder + sftp are openable; arqpremium cloud is not.
                self.assertEqual(
                    {d.path for d in dests},
                    {"/Volumes/arqbackup1", "/home"},
                )
                self.assertTrue(all(d.origin == "arq" for d in dests))
                await pilot.press("escape")
                await pilot.press("q")

    async def test_own_destination_not_duplicated_by_arq(self) -> None:
        from arq_tui import ArqTuiApp
        from arq_tui.screens.backup_sets import (
            BackupSetListScreen, StoragePanel,
        )
        from arq_tui.state import Destination, DestinationStore
        with tempfile.TemporaryDirectory() as cfg, \
                tempfile.TemporaryDirectory() as dbdir:
            store = DestinationStore(config_dir=Path(cfg))
            # Same coordinates as the Arq folder location.
            store.add_or_touch(Destination(
                kind="local", label="mine", path="/Volumes/arqbackup1",
                origin="tui",
            ))
            app = ArqTuiApp(
                config_dir=Path(cfg), destination_store=store,
                arq_app=_fixture_source(dbdir),
            )
            async with app.run_test() as pilot:
                await pilot.pause()
                app.push_screen(BackupSetListScreen())
                await pilot.pause()
                dests = app.screen.query_one(StoragePanel)._merged_destinations()
                arqbackup = [
                    d for d in dests if d.path == "/Volumes/arqbackup1"
                ]
                self.assertEqual(len(arqbackup), 1)
                self.assertEqual(arqbackup[0].origin, "tui")  # own wins
                await pilot.press("escape")
                await pilot.press("q")


@unittest.skipUnless(HAS_TEXTUAL, "textual not installed; install with .[tui]")
class RunsMonitorArqMirrorTests(unittest.IsolatedAsyncioTestCase):
    async def _booted_monitor(self, app, pilot):
        from arq_tui.screens.runs_monitor import (
            ActivityPanel, RunsMonitorScreen,
        )
        # Empty own-runs state dir so only Arq rows appear.
        self._runs_dir = tempfile.TemporaryDirectory()
        screen = RunsMonitorScreen(state_dir=Path(self._runs_dir.name))
        app.push_screen(screen)
        await pilot.pause()
        # Activity logic lives on the panel now; return it for the
        # direct ``_arq_run_records`` assertions.
        return screen.query_one(ActivityPanel)

    async def test_activities_split_active_recent(self) -> None:
        from arq_tui import ArqTuiApp
        with tempfile.TemporaryDirectory() as cfg, \
                tempfile.TemporaryDirectory() as dbdir:
            app = ArqTuiApp(
                config_dir=Path(cfg), arq_app=_fixture_source(dbdir),
            )
            async with app.run_test() as pilot:
                await pilot.pause()
                screen = await self._booted_monitor(app, pilot)
                # Fix `now` near the fixture timestamps so finished rows
                # fall inside the 24h recent window deterministically.
                active, recent = screen._arq_run_records(now=3500.0)
                self.assertEqual([r.run_id for r in active], ["arq:A-RUN"])
                self.assertEqual(
                    {r.run_id for r in recent},
                    {"arq:A-DONE", "arq:A-VALIDATE", "arq:A-ABORT"},
                )
                self._runs_dir.cleanup()
                await pilot.press("escape")
                await pilot.press("q")

    async def test_status_and_badge_mapping(self) -> None:
        from arq_tui import ArqTuiApp
        from arq_tui.runs import RunStatus
        with tempfile.TemporaryDirectory() as cfg, \
                tempfile.TemporaryDirectory() as dbdir:
            app = ArqTuiApp(
                config_dir=Path(cfg), arq_app=_fixture_source(dbdir),
            )
            async with app.run_test() as pilot:
                await pilot.pause()
                screen = await self._booted_monitor(app, pilot)
                active, recent = screen._arq_run_records(now=3500.0)
                by_id = {r.run_id: r for r in active + recent}
                self.assertEqual(
                    by_id["arq:A-RUN"].status, RunStatus.RUNNING.value)
                self.assertEqual(
                    by_id["arq:A-ABORT"].status, RunStatus.CANCELLED.value)
                self.assertEqual(
                    by_id["arq:A-VALIDATE"].status, RunStatus.COMPLETED.value)
                self.assertEqual(by_id["arq:A-VALIDATE"].kind, "validate")
                # Every Arq row is badged + names resolved.
                self.assertTrue(
                    all(r.plan_name.startswith("◆ Arq")
                        for r in by_id.values()))
                self.assertIn(
                    "Buzhash plan", by_id["arq:A-RUN"].plan_name)
                self._runs_dir.cleanup()
                await pilot.press("escape")
                await pilot.press("q")


@unittest.skipUnless(HAS_TEXTUAL, "textual not installed; install with .[tui]")
class SummaryPanelTests(unittest.IsolatedAsyncioTestCase):
    async def test_summary_renders_overview(self) -> None:
        from arq_tui import ArqTuiApp
        from arq_tui.screens.summary import SummaryPanel
        with tempfile.TemporaryDirectory() as cfg, \
                tempfile.TemporaryDirectory() as dbdir:
            app = ArqTuiApp(
                config_dir=Path(cfg), arq_app=_fixture_source(dbdir),
            )
            async with app.run_test() as pilot:
                await pilot.pause()
                panel = app.screen.query_one(SummaryPanel)
                text = panel._report_text
                self.assertIn("Backup plans:", text)
                self.assertIn("Storage locations:", text)
                self.assertIn("In progress:", text)
                self.assertIn("Recent (24h):", text)
                await pilot.press("q")

    async def test_save_report_writes_file(self) -> None:
        from arq_tui import ArqTuiApp
        from arq_tui.screens.summary import SummaryPanel
        with tempfile.TemporaryDirectory() as cfg, \
                tempfile.TemporaryDirectory() as dbdir:
            app = ArqTuiApp(
                config_dir=Path(cfg), arq_app=_fixture_source(dbdir),
            )
            async with app.run_test() as pilot:
                await pilot.pause()
                panel = app.screen.query_one(SummaryPanel)
                panel.action_save_report()
                await pilot.pause()
                files = list((Path(cfg) / "summaries").glob("summary-*.txt"))
                self.assertEqual(len(files), 1)
                self.assertIn("Backup plans:", files[0].read_text())
                await pilot.press("q")


@unittest.skipUnless(HAS_TEXTUAL, "textual not installed; install with .[tui]")
class ActivityDetailModalTests(unittest.IsolatedAsyncioTestCase):
    async def test_modal_shows_status_and_event_log(self) -> None:
        from arq_tui import ArqTuiApp
        from arq_tui.runs import RunEvent, RunProgress, RunRecord
        from arq_tui.screens.runs_monitor import ActivityDetailModal
        from textual.widgets import Static
        with tempfile.TemporaryDirectory() as cfg:
            app = ArqTuiApp(config_dir=Path(cfg), arq_app=None)
            async with app.run_test() as pilot:
                await pilot.pause()
                rec = RunRecord(
                    run_id="own1", kind="backup", status="completed",
                    started_at=100.0, finished_at=200.0, plan_name="My Plan",
                    progress=RunProgress(
                        files_done=5, files_total=5, bytes_done=2048),
                    events_tail=[RunEvent(
                        t=150.0, kind="file_written",
                        payload={"path": "/a.txt"})],
                )
                app.push_screen(ActivityDetailModal(record=rec))
                await pilot.pause()
                modal = app.screen
                self.assertIsInstance(modal, ActivityDetailModal)
                # Assert on the content builders (deterministic; avoids
                # widget-internal text accessors).
                self.assertIn("My Plan", modal._title())
                self.assertIn("completed", modal._status_text())
                self.assertIn("Files: 5/5", modal._status_text())
                self.assertIn("file_written", modal._initial_log())
                # The widgets exist + mounted.
                modal.query_one("#status", Static)
                modal.query_one("#log-body", Static)
                await pilot.press("escape")

    async def test_enter_on_row_opens_detail(self) -> None:
        from arq_tui import ArqTuiApp
        from arq_tui.runs import RunRecord
        from arq_tui.screens.runs_monitor import (
            ActivityDetailModal, ActivityPanel, RunRow, RunsMonitorScreen,
        )
        with tempfile.TemporaryDirectory() as cfg, \
                tempfile.TemporaryDirectory() as rd:
            app = ArqTuiApp(config_dir=Path(cfg), arq_app=None)
            async with app.run_test() as pilot:
                await pilot.pause()
                app.push_screen(RunsMonitorScreen(state_dir=Path(rd)))
                await pilot.pause()
                panel = app.screen.query_one(ActivityPanel)
                rec = RunRecord(
                    run_id="own2", kind="restore", status="completed",
                    started_at=1.0, finished_at=2.0, plan_name="R",
                )

                class _Evt:
                    item = RunRow(rec)
                panel.on_list_view_selected(_Evt())
                await pilot.pause()
                self.assertIsInstance(app.screen, ActivityDetailModal)
                await pilot.press("escape")


@unittest.skipUnless(HAS_TEXTUAL, "textual not installed; install with .[tui]")
class ActivityCursorStabilityTests(unittest.IsolatedAsyncioTestCase):
    """The 1 Hz refresh must not yank the keyboard cursor back to the
    top of a list when the run set is unchanged (the reported bug:
    arrowing down in Recent jumps home every poll)."""

    def _recs(self, *ids):
        from arq_tui.runs import RunRecord
        return [
            RunRecord(
                run_id=i, kind="backup", status="completed",
                started_at=100.0, finished_at=200.0,
            )
            for i in ids
        ]

    async def _panel(self, app, pilot):
        from arq_tui.screens.runs_monitor import (
            ActivityPanel, RunsMonitorScreen,
        )
        self._runs_dir = tempfile.TemporaryDirectory()
        app.push_screen(RunsMonitorScreen(state_dir=Path(self._runs_dir.name)))
        await pilot.pause()
        return app.screen.query_one(ActivityPanel)

    async def test_cursor_preserved_when_set_unchanged(self) -> None:
        from arq_tui import ArqTuiApp
        from textual.widgets import ListView
        with tempfile.TemporaryDirectory() as cfg:
            app = ArqTuiApp(config_dir=Path(cfg), arq_app=None)
            async with app.run_test() as pilot:
                await pilot.pause()
                panel = await self._panel(app, pilot)
                recs = self._recs("r0", "r1", "r2", "r3")
                panel._render_into("#recent-list", "#recent-empty", recs)
                await pilot.pause()
                lv = panel.query_one("#recent-list", ListView)
                lv.index = 2
                await pilot.pause()
                # Re-render with the identical run set (what every poll
                # does) — cursor must stay put, not snap to 0.
                panel._render_into("#recent-list", "#recent-empty", recs)
                self.assertEqual(lv.index, 2)
                self._runs_dir.cleanup()
                await pilot.press("q")

    async def test_cursor_clamped_when_set_shrinks(self) -> None:
        from arq_tui import ArqTuiApp
        from textual.widgets import ListView
        with tempfile.TemporaryDirectory() as cfg:
            app = ArqTuiApp(config_dir=Path(cfg), arq_app=None)
            async with app.run_test() as pilot:
                await pilot.pause()
                panel = await self._panel(app, pilot)
                recs = self._recs("r0", "r1", "r2", "r3")
                panel._render_into("#recent-list", "#recent-empty", recs)
                await pilot.pause()
                lv = panel.query_one("#recent-list", ListView)
                lv.index = 3
                await pilot.pause()
                # A run drops out → rebuild, but clamp instead of home.
                panel._render_into(
                    "#recent-list", "#recent-empty", self._recs("r0", "r1"),
                )
                await pilot.pause()
                self.assertEqual(lv.index, 1)
                self._runs_dir.cleanup()
                await pilot.press("q")


if __name__ == "__main__":
    unittest.main()
