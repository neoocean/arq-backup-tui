"""Tests for the plan-edit flow.

The wizard supports both create AND edit (via ``PlanWizardScreen(plan=…)``);
the home screen wires the [e] key to launch it pre-populated. These
tests pin the round-trip:

- Open the wizard with an existing plan → fields are pre-loaded.
- Save without changing anything → plan_id + last_run_iso are
  preserved, no new plan file is created.
- Edit one field → only that field changes; the rest match the
  original.

These complement the create-flow test in ``test_tui_m3_backup``;
together they cover both halves of the wizard contract.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

try:
    from textual.widgets import Input  # noqa: F401
    HAS_TEXTUAL = True
except ImportError:
    HAS_TEXTUAL = False


@unittest.skipUnless(HAS_TEXTUAL, "textual not installed")
class PlanEditFlowTests(unittest.IsolatedAsyncioTestCase):

    async def _make_plan(self, app):
        """Save a fixed test plan into the app's registry and
        return it (we need a real Plan object to feed back into
        the wizard's edit mode)."""
        from arq_tui.state import Plan
        plan = Plan(
            plan_id="P-orig",
            name="original-plan",
            sources=["/data/A", "/data/B"],
            destination_kind="local",
            destination={"path": "/Volumes/old"},
            chunker="default",
            use_packs=True,
            dedup_against_existing=True,
            exclude_globs=["*.tmp"],
            last_run_iso="2025-12-01T12:00:00Z",
        )
        app.plan_registry.save(plan)
        return plan

    async def test_edit_preserves_plan_id_and_last_run_iso(self) -> None:
        """The wizard must round-trip plan_id + last_run_iso when
        editing — a fresh UUID + reset run-time would orphan the
        existing destination + activity history."""
        from arq_tui import ArqTuiApp
        from arq_tui.screens.plan_wizard import PlanWizardScreen
        from arq_tui.widgets.source_picker import SourcePicker

        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "cfg"
            app = ArqTuiApp(config_dir=cfg)
            async with app.run_test() as pilot:
                await pilot.pause()
                original = await self._make_plan(app)

                # Open wizard in edit mode.
                app.push_screen(PlanWizardScreen(plan=original))
                await pilot.pause()
                wizard = app.screen

                # Sources should already be the originals.
                picker = wizard.query_one(SourcePicker)
                self.assertEqual(
                    picker.paths, ["/data/A", "/data/B"],
                )

                # Walk straight to step 6 without changing anything.
                # Each step's "next" handler validates → advances;
                # blank password on step 3 means "preserve existing"
                # in edit mode (per wizard's handler).
                for _ in range(5):
                    wizard._handle_next()
                    await pilot.pause()
                # Step 6 has the plan-name field already pre-filled.
                wizard._handle_next()
                await pilot.pause()

            plans = app.plan_registry.list_plans()
            self.assertEqual(len(plans), 1)
            saved = plans[0]
            # Same ID — no orphaned plan + no fresh-plan rename.
            self.assertEqual(saved.plan_id, "P-orig")
            # last_run_iso preserved (a brand-new save would clear it).
            self.assertEqual(
                saved.last_run_iso, "2025-12-01T12:00:00Z",
            )
            # Original fields intact.
            self.assertEqual(saved.name, "original-plan")
            self.assertEqual(
                saved.sources, ["/data/A", "/data/B"],
            )
            self.assertEqual(
                saved.destination["path"], "/Volumes/old",
            )
            self.assertEqual(saved.exclude_globs, ["*.tmp"])

    async def test_edit_modifies_only_one_field(self) -> None:
        """Change just the plan name on step 6; every other field
        must come back unchanged from the registry."""
        from arq_tui import ArqTuiApp
        from arq_tui.screens.plan_wizard import PlanWizardScreen

        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "cfg"
            app = ArqTuiApp(config_dir=cfg)
            async with app.run_test() as pilot:
                await pilot.pause()
                original = await self._make_plan(app)

                app.push_screen(PlanWizardScreen(plan=original))
                await pilot.pause()
                wizard = app.screen
                # Walk to step 6.
                for _ in range(5):
                    wizard._handle_next()
                    await pilot.pause()
                # Rename.
                wizard.query_one(
                    "#plan-name", Input,
                ).value = "renamed-plan"
                wizard._handle_next()
                await pilot.pause()

            plans = app.plan_registry.list_plans()
            self.assertEqual(len(plans), 1)
            saved = plans[0]
            self.assertEqual(saved.name, "renamed-plan")
            self.assertEqual(saved.plan_id, "P-orig")
            self.assertEqual(saved.sources, ["/data/A", "/data/B"])
            self.assertEqual(
                saved.destination["path"], "/Volumes/old",
            )
            self.assertEqual(saved.exclude_globs, ["*.tmp"])
            self.assertTrue(saved.use_packs)
            self.assertTrue(saved.dedup_against_existing)


if __name__ == "__main__":
    unittest.main()
