"""Tests for the quake-style command console.

Three concerns:

- :class:`~arq_tui.widgets.console.CommandConsole` opens on each of
  the three trigger characters (``:`` / `` ` `` / ``₩``), Esc
  closes it, and arrow-key history works.
- :func:`~arq_tui.console_commands.dispatch_command` resolves
  every registered command name + alias and surfaces sensible
  errors for unknown / argument-less calls.
- The trigger handler must NOT fire while focus is on a regular
  ``Input`` (e.g. mid-wizard) — typing ``:`` into a wizard field
  has to pass through as literal text.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

try:
    import textual  # noqa: F401
    HAS_TEXTUAL = True
except ImportError:  # pragma: no cover
    HAS_TEXTUAL = False


@unittest.skipUnless(HAS_TEXTUAL, "textual not installed")
class ConsoleOpenCloseTests(unittest.IsolatedAsyncioTestCase):
    async def test_colon_opens_console(self) -> None:
        from arq_tui import ArqTuiApp
        with tempfile.TemporaryDirectory() as td:
            app = ArqTuiApp(config_dir=Path(td))
            async with app.run_test() as pilot:
                await pilot.pause()
                self.assertFalse(app.console_widget.is_open)
                await pilot.press(":")
                await pilot.pause()
                self.assertTrue(app.console_widget.is_open)

    async def test_backtick_opens_console(self) -> None:
        # Pilot.press doesn't take arbitrary printable chars by
        # name, so drive the App's on_key handler directly with a
        # synthesized event — matches what the runtime would
        # deliver.
        from arq_tui import ArqTuiApp
        from textual import events
        with tempfile.TemporaryDirectory() as td:
            app = ArqTuiApp(config_dir=Path(td))
            async with app.run_test() as pilot:
                await pilot.pause()
                app.on_key(
                    events.Key(key="grave_accent", character="`"),
                )
                await pilot.pause()
                self.assertTrue(app.console_widget.is_open)

    async def test_won_sign_opens_console(self) -> None:
        # macOS Korean IME emits ₩ (U+20A9) when the operator hits
        # the backtick key with Korean input mode active.
        from arq_tui import ArqTuiApp
        from textual import events
        with tempfile.TemporaryDirectory() as td:
            app = ArqTuiApp(config_dir=Path(td))
            async with app.run_test() as pilot:
                await pilot.pause()
                app.on_key(events.Key(key="₩", character="₩"))
                await pilot.pause()
                self.assertTrue(app.console_widget.is_open)

    async def test_escape_closes_console(self) -> None:
        from arq_tui import ArqTuiApp
        with tempfile.TemporaryDirectory() as td:
            app = ArqTuiApp(config_dir=Path(td))
            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.press(":")
                await pilot.pause()
                self.assertTrue(app.console_widget.is_open)
                await pilot.press("escape")
                await pilot.pause()
                self.assertFalse(app.console_widget.is_open)

    async def test_focused_input_swallows_open_char(self) -> None:
        # Wizard step: focus is on a regular Input — typing `:` must
        # NOT open the console; it should land as a character in the
        # focused field.
        from arq_tui import ArqTuiApp
        from arq_tui.screens.plan_wizard import PlanWizardScreen
        from textual.widgets import Input
        with tempfile.TemporaryDirectory() as td:
            app = ArqTuiApp(config_dir=Path(td))
            async with app.run_test() as pilot:
                await pilot.pause()
                app.push_screen(PlanWizardScreen())
                await pilot.pause()
                # Focus an Input on the destination step.
                # Cheap path: walk to step 2 and focus the local-path.
                wizard = app.screen
                # The plan-name field on the review pane is a fine
                # focusable input even before we reach that step,
                # since query_one returns the widget regardless of
                # display state.
                inp = wizard.query_one("#plan-name", Input)
                inp.focus()
                await pilot.pause()
                await pilot.press(":")
                await pilot.pause()
                self.assertFalse(app.console_widget.is_open)


@unittest.skipUnless(HAS_TEXTUAL, "textual not installed")
class ConsoleHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_up_down_arrow_walks_history(self) -> None:
        from arq_tui import ArqTuiApp
        from arq_tui.widgets.console import CommandConsole
        from textual.widgets import Input
        with tempfile.TemporaryDirectory() as td:
            app = ArqTuiApp(config_dir=Path(td))
            async with app.run_test() as pilot:
                await pilot.pause()
                console: CommandConsole = app.console_widget
                # Pre-load history without going through dispatch.
                console.history = ["help", "plans", "browse"]
                console._history_idx = len(console.history)
                console.open()
                await pilot.pause()
                inp = console.query_one(
                    "#console-input", Input,
                )
                inp.value = "draft-text"
                # Up steps back through the history; the draft is
                # stashed when the first Up press fires.
                console._history_up(inp)
                self.assertEqual(inp.value, "browse")
                console._history_up(inp)
                self.assertEqual(inp.value, "plans")
                console._history_up(inp)
                self.assertEqual(inp.value, "help")
                # Stop at the top — extra Up doesn't underflow.
                console._history_up(inp)
                self.assertEqual(inp.value, "help")
                # Down walks back; final Down restores the draft.
                console._history_down(inp)
                self.assertEqual(inp.value, "plans")
                console._history_down(inp)
                self.assertEqual(inp.value, "browse")
                console._history_down(inp)
                self.assertEqual(inp.value, "draft-text")


@unittest.skipUnless(HAS_TEXTUAL, "textual not installed")
class ConsoleDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_help_lists_every_registered_command(self) -> None:
        from arq_tui import ArqTuiApp
        from arq_tui.console_commands import _COMMANDS, dispatch_command
        with tempfile.TemporaryDirectory() as td:
            app = ArqTuiApp(config_dir=Path(td))
            async with app.run_test() as pilot:
                await pilot.pause()
                out = await dispatch_command(app, "help")
                for cmd in _COMMANDS:
                    self.assertIn(cmd.name, out)

    async def test_unknown_command_returns_friendly_error(self) -> None:
        from arq_tui import ArqTuiApp
        from arq_tui.console_commands import dispatch_command
        with tempfile.TemporaryDirectory() as td:
            app = ArqTuiApp(config_dir=Path(td))
            async with app.run_test() as pilot:
                await pilot.pause()
                out = await dispatch_command(
                    app, "this-is-not-a-thing",
                )
                self.assertIn("unknown command", out)

    async def test_plans_lists_saved_plans(self) -> None:
        from arq_tui import ArqTuiApp
        from arq_tui.console_commands import dispatch_command
        from arq_tui.state import Plan
        with tempfile.TemporaryDirectory() as td:
            app = ArqTuiApp(config_dir=Path(td))
            app.plan_registry.save(Plan(
                plan_id="DISPATCH-UUID",
                name="dispatch-target",
                sources=["/x"],
                destination_kind="local",
                destination={"path": "/Volumes/x"},
            ))
            async with app.run_test() as pilot:
                await pilot.pause()
                out = await dispatch_command(app, "plans")
                self.assertIn("dispatch-target", out)

    async def test_edit_resolves_plan_by_name(self) -> None:
        from arq_tui import ArqTuiApp
        from arq_tui.console_commands import dispatch_command
        from arq_tui.screens.plan_wizard import PlanWizardScreen
        from arq_tui.state import Plan
        with tempfile.TemporaryDirectory() as td:
            app = ArqTuiApp(config_dir=Path(td))
            app.plan_registry.save(Plan(
                plan_id="EDIT-UUID-99",
                name="dispatch-edit",
                sources=["/x"],
                destination_kind="local",
                destination={"path": "/Volumes/x"},
            ))
            async with app.run_test() as pilot:
                await pilot.pause()
                await dispatch_command(app, "edit dispatch-edit")
                await pilot.pause()
                self.assertIsInstance(app.screen, PlanWizardScreen)
                self.assertEqual(
                    app.screen._editing_plan.plan_id,
                    "EDIT-UUID-99",
                )

    async def test_delete_removes_plan_file(self) -> None:
        from arq_tui import ArqTuiApp
        from arq_tui.console_commands import dispatch_command
        from arq_tui.state import Plan
        with tempfile.TemporaryDirectory() as td:
            app = ArqTuiApp(config_dir=Path(td))
            app.plan_registry.save(Plan(
                plan_id="DELETE-ME-ID",
                name="goes-away",
                sources=["/x"],
                destination_kind="local",
                destination={"path": "/Volumes/x"},
            ))
            async with app.run_test() as pilot:
                await pilot.pause()
                out = await dispatch_command(app, "delete goes-away")
                self.assertIn("deleted", out)
                self.assertEqual(
                    len(app.plan_registry.list_plans()), 0,
                )

    async def test_close_command_hides_console(self) -> None:
        from arq_tui import ArqTuiApp
        from arq_tui.console_commands import dispatch_command
        with tempfile.TemporaryDirectory() as td:
            app = ArqTuiApp(config_dir=Path(td))
            async with app.run_test() as pilot:
                await pilot.pause()
                app.console_widget.open()
                await pilot.pause()
                self.assertTrue(app.console_widget.is_open)
                await dispatch_command(app, "close")
                self.assertFalse(app.console_widget.is_open)


@unittest.skipUnless(HAS_TEXTUAL, "textual not installed")
class ConsoleSubmitFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_submit_strips_leading_sigils(self) -> None:
        # Three different sigils all reach the same dispatcher
        # because the console strips them before forwarding.
        from arq_tui import ArqTuiApp
        from textual.widgets import Input
        with tempfile.TemporaryDirectory() as td:
            app = ArqTuiApp(config_dir=Path(td))
            async with app.run_test() as pilot:
                await pilot.pause()
                console = app.console_widget
                console.open()
                await pilot.pause()
                inp = console.query_one("#console-input", Input)
                seen: list = []

                async def fake(line: str) -> str:
                    seen.append(line)
                    return ""

                console.attach_dispatch(fake)
                for raw in (":help", "/help", "\\help", "help"):
                    inp.value = raw
                    await inp.action_submit()
                    await pilot.pause()
                self.assertEqual(seen, ["help"] * 4)


if __name__ == "__main__":
    unittest.main()
