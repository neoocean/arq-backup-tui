"""Tests for the M2 backup-set + record-browser screens.

Drives the screens through Textual's pilot. Real backup
destinations are produced by ``arq_writer.build_backup`` against a
temp dir, then opened through the TUI's ``LocalBackend`` flow. SFTP
is not exercised here (covered by tests/test_sftp_backend_wiring.py
at the library layer); these tests exercise the *UI* mechanics.
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

from arq_writer import build_backup


def _make_tree(root: Path) -> None:
    (root / "subdir").mkdir(parents=True)
    (root / "alpha.txt").write_bytes(b"alpha\n")
    (root / "subdir" / "gamma.txt").write_bytes(b"gamma\n")
    # Unicode-named files exercise the path round-trip property.
    (root / "한글.txt").write_bytes("내용".encode("utf-8"))


@unittest.skipUnless(HAS_TEXTUAL, "textual not installed")
class DestinationStoreTests(unittest.TestCase):
    def test_add_or_touch_persists_and_orders(self) -> None:
        from arq_tui.state import Destination, DestinationStore
        with tempfile.TemporaryDirectory() as td:
            store = DestinationStore(config_dir=Path(td))
            d1 = Destination(kind="local", path="/a", label="A")
            d2 = Destination(kind="local", path="/b", label="B")
            store.add_or_touch(d1)
            store.add_or_touch(d2)
            store.add_or_touch(d1)   # touch → moves to front
            order = [d.path for d in store.list()]
            self.assertEqual(order, ["/a", "/b"])

    def test_persistence_round_trip(self) -> None:
        from arq_tui.state import Destination, DestinationStore
        with tempfile.TemporaryDirectory() as td:
            store1 = DestinationStore(config_dir=Path(td))
            store1.add_or_touch(Destination(
                kind="sftp", host="example.com", user="me",
                port=2222, path="/srv/arq",
            ))
            store2 = DestinationStore(config_dir=Path(td))
            items = store2.list()
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].host, "example.com")
            self.assertEqual(items[0].port, 2222)
            self.assertEqual(items[0].path, "/srv/arq")


@unittest.skipUnless(HAS_TEXTUAL, "textual not installed")
class CredentialCacheTests(unittest.TestCase):
    def test_password_round_trip_in_memory(self) -> None:
        from arq_tui.state import CredentialCache, Destination
        cache = CredentialCache()
        d = Destination(kind="local", path="/x")
        self.assertIsNone(cache.get_encryption_password(d))
        cache.set_encryption_password(d, "secret123")
        self.assertEqual(cache.get_encryption_password(d), "secret123")
        cache.forget(d)
        self.assertIsNone(cache.get_encryption_password(d))


@unittest.skipUnless(HAS_TEXTUAL, "textual not installed")
class BackupSetListScreenTests(unittest.IsolatedAsyncioTestCase):
    async def test_browse_reaches_screen_and_tree_renders(self) -> None:
        from arq_tui import ArqTuiApp
        from arq_tui.state import Destination

        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            _make_tree(src)
            dest = tdp / "dest"
            r1 = build_backup(src, dest, encryption_password="pw")

            app = ArqTuiApp(config_dir=tdp / "cfg")
            # Pre-seed: cached encryption password + recent
            # destination so the screen doesn't prompt during the
            # test.
            target = Destination(kind="local", path=str(dest))
            app.destination_store.add_or_touch(target)
            app.credential_cache.set_encryption_password(target, "pw")

            async with app.run_test() as pilot:
                await pilot.pause()
                # Open the Storage Locations section — the shell swaps
                # the right-hand panel in place (no full-screen push).
                await pilot.press("b")
                await pilot.pause()
                from textual.widgets import ContentSwitcher
                self.assertEqual(
                    app.screen.query_one(
                        "#home-content", ContentSwitcher,
                    ).current,
                    "panel-browse",
                )
                # Activate the destination row (first entry).
                from textual.widgets import ListView
                lv = app.screen.query_one(
                    "#destinations-list", ListView,
                )
                lv.focus()
                lv.index = 0
                await pilot.pause()
                # Trigger selection. Opening + reading records runs in
                # a worker thread now, so wait for the tree to populate.
                await pilot.press("enter")
                from textual.widgets import Tree
                tree = app.screen.query_one("#layout-tree", Tree)
                for _ in range(200):
                    await pilot.pause()
                    await asyncio.sleep(0.02)
                    if tree.display and len(tree.root.children) >= 1:
                        break
                self.assertTrue(tree.display)
                root = tree.root
                # Root has the destination label, with at least one
                # computer-uuid child.
                self.assertGreaterEqual(len(root.children), 1)
                comp_node = root.children[0]
                self.assertIn(
                    r1.computer_uuid, str(comp_node.label)
                )
                # Computer node has the folder; folder has at least
                # one record leaf.
                self.assertGreaterEqual(len(comp_node.children), 1)
                folder_node = comp_node.children[0]
                self.assertIn(
                    r1.folder_uuid, str(folder_node.label),
                )
                self.assertGreaterEqual(
                    len(folder_node.children), 1,
                )

    async def test_record_browser_opens_with_unicode_path(self) -> None:
        # Drive all the way down to the record browser, then verify
        # the tree's children include the Unicode filename we wrote.
        from arq_tui import ArqTuiApp
        from arq_tui.state import Destination

        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            _make_tree(src)
            dest = tdp / "dest"
            r1 = build_backup(src, dest, encryption_password="pw")
            app = ArqTuiApp(config_dir=tdp / "cfg")
            target = Destination(kind="local", path=str(dest))
            app.destination_store.add_or_touch(target)
            app.credential_cache.set_encryption_password(target, "pw")

            async with app.run_test() as pilot:
                await pilot.pause()
                await pilot.press("b")
                await pilot.pause()
                # Activate the destination (browse panel is now in the
                # shell's content switcher, not a separate screen).
                from textual.widgets import ListView, Tree
                lv = app.screen.query_one(
                    "#destinations-list", ListView,
                )
                lv.focus()
                lv.index = 0
                await pilot.press("enter")
                # Opening + reading records is async (worker thread) —
                # wait for the tree to populate down to a record leaf.
                tree = app.screen.query_one("#layout-tree", Tree)
                for _ in range(200):
                    await pilot.pause()
                    await asyncio.sleep(0.02)
                    if (tree.display and tree.root.children
                            and tree.root.children[0].children
                            and tree.root.children[0].children[0].children):
                        break
                rec_node = (
                    tree.root.children[0].children[0].children[0]
                )
                tree.select_node(rec_node)
                await pilot.pause()
                # Push happens via on_tree_node_selected. Trigger
                # that explicitly via select_node + the screen's
                # handler (Textual re-emits NodeSelected on Enter).
                # The record browser sits on top now.
                from arq_tui.screens.record_browser import (
                    RecordBrowserScreen,
                )
                from arq_tui.screens.backup_sets import StoragePanel
                # If on_tree_node_selected didn't fire from the
                # programmatic select, fallback: invoke the panel's
                # handler manually (it moved off the screen onto the
                # StoragePanel in the shell refactor).
                if not isinstance(app.screen, RecordBrowserScreen):
                    panel = app.screen.query_one(StoragePanel)
                    panel.on_tree_node_selected(Tree.NodeSelected(rec_node))
                    await pilot.pause()
                self.assertIsInstance(app.screen, RecordBrowserScreen)
                rec_tree = app.screen.query_one(
                    "#record-tree", Tree,
                )
                child_labels = [
                    str(c.label) for c in rec_tree.root.children
                ]
                self.assertIn("alpha.txt", child_labels)
                self.assertIn("subdir", child_labels)
                # Critical: non-ASCII filename round-trips through
                # the Tree blob → UTF-8 child name → label exactly.
                self.assertIn("한글.txt", child_labels)


@unittest.skipUnless(HAS_TEXTUAL, "textual not installed")
class StorageDeleteTests(unittest.IsolatedAsyncioTestCase):
    def test_destination_store_remove(self) -> None:
        from arq_tui.state import Destination, DestinationStore
        with tempfile.TemporaryDirectory() as td:
            store = DestinationStore(config_dir=Path(td))
            d = Destination(kind="local", path="/a")
            store.add_or_touch(d)
            self.assertEqual(len(store.list()), 1)
            self.assertTrue(store.remove(d))
            self.assertEqual(store.list(), [])
            self.assertFalse(store.remove(d))   # already gone

    async def _panel_with_own_dest(self, app, pilot):
        from arq_tui.screens.backup_sets import (
            BackupSetListScreen, StoragePanel,
        )
        app.push_screen(BackupSetListScreen())
        await pilot.pause()
        return app.screen.query_one(StoragePanel)

    async def test_delete_own_destination_after_confirm(self) -> None:
        from arq_tui import ArqTuiApp
        from arq_tui.state import Destination
        from arq_tui.widgets.confirm_modal import ConfirmModal
        from textual.widgets import ListView
        with tempfile.TemporaryDirectory() as cfg:
            app = ArqTuiApp(config_dir=Path(cfg), arq_app=None)
            d = Destination(kind="local", path="/some/dest", label="mine")
            app.destination_store.add_or_touch(d)
            async with app.run_test() as pilot:
                await pilot.pause()
                panel = await self._panel_with_own_dest(app, pilot)
                panel.query_one("#destinations-list", ListView).index = 0
                panel.action_delete_destination()
                await pilot.pause()
                self.assertIsInstance(app.screen, ConfirmModal)
                app.screen.dismiss(True)   # confirm
                await pilot.pause()
                self.assertEqual(app.destination_store.list(), [])

    async def test_delete_cancelled_keeps_destination(self) -> None:
        from arq_tui import ArqTuiApp
        from arq_tui.state import Destination
        from textual.widgets import ListView
        with tempfile.TemporaryDirectory() as cfg:
            app = ArqTuiApp(config_dir=Path(cfg), arq_app=None)
            d = Destination(kind="local", path="/some/dest", label="mine")
            app.destination_store.add_or_touch(d)
            async with app.run_test() as pilot:
                await pilot.pause()
                panel = await self._panel_with_own_dest(app, pilot)
                panel.query_one("#destinations-list", ListView).index = 0
                panel.action_delete_destination()
                await pilot.pause()
                app.screen.dismiss(False)  # cancel
                await pilot.pause()
                self.assertEqual(len(app.destination_store.list()), 1)


if __name__ == "__main__":
    unittest.main()
