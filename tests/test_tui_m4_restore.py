"""Tests for the M4 restore-run screen.

Drives a real RestoreWorker through the screen and verifies the
restored output. The key Unicode-safe-paths invariant: a marked
non-ASCII filename round-trips through the path filter and lands
in the restore target verbatim.
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
    (root / "beta.txt").write_bytes(b"beta\n")
    (root / "subdir" / "gamma.txt").write_bytes(b"gamma\n")
    (root / "한글.txt").write_bytes("내용".encode("utf-8"))


@unittest.skipUnless(HAS_TEXTUAL, "textual not installed")
class RestoreRunFullTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_restore_round_trips_bytes(self) -> None:
        from arq_tui import ArqTuiApp
        from arq_tui.backend_open import close_backend, open_backend
        from arq_tui.screens.restore_run import RestoreRunScreen
        from arq_tui.state import Destination
        from arq_tui.widgets.progress_panel import ProgressPanel

        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            _make_tree(src)
            dest = tdp / "dest"
            r1 = build_backup(src, dest, encryption_password="pw")
            target = tdp / "out"
            cfg = tdp / "cfg"

            app = ArqTuiApp(config_dir=cfg)
            d = Destination(kind="local", path=str(dest))
            app.credential_cache.set_encryption_password(d, "pw")
            backend = open_backend(d)
            try:
                async with app.run_test() as pilot:
                    await pilot.pause()
                    app.push_screen(RestoreRunScreen(
                        backend=backend,
                        encryption_password="pw",
                        computer_uuid=r1.computer_uuid,
                        folder_uuid=r1.folder_uuid,
                        backuprecord_path=None,
                        target=target,
                        paths=None,
                    ))
                    panel = None
                    for _ in range(300):
                        await pilot.pause()
                        await asyncio.sleep(0.05)
                        if panel is None:
                            try:
                                panel = app.screen.query_one(ProgressPanel)
                            except Exception:
                                continue
                        if panel.finished or panel.failed:
                            break
                    self.assertIsNotNone(panel)
                    self.assertTrue(
                        panel.finished,
                        msg=f"failed={panel.failed} err={panel.error_message}",
                    )
            finally:
                close_backend(backend)

            # All files must exist with the right content.
            self.assertEqual(
                (target / "alpha.txt").read_bytes(), b"alpha\n",
            )
            self.assertEqual(
                (target / "beta.txt").read_bytes(), b"beta\n",
            )
            self.assertEqual(
                (target / "subdir" / "gamma.txt").read_bytes(),
                b"gamma\n",
            )
            self.assertEqual(
                (target / "한글.txt").read_bytes(),
                "내용".encode("utf-8"),
            )

    async def test_selected_restore_filters_paths(self) -> None:
        from arq_tui import ArqTuiApp
        from arq_tui.backend_open import close_backend, open_backend
        from arq_tui.screens.restore_run import RestoreRunScreen
        from arq_tui.state import Destination
        from arq_tui.widgets.progress_panel import ProgressPanel

        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            _make_tree(src)
            dest = tdp / "dest"
            r1 = build_backup(src, dest, encryption_password="pw")
            target = tdp / "out"
            cfg = tdp / "cfg"

            app = ArqTuiApp(config_dir=cfg)
            d = Destination(kind="local", path=str(dest))
            backend = open_backend(d)
            try:
                async with app.run_test() as pilot:
                    await pilot.pause()
                    # Selectively restore only the Korean file +
                    # subdir contents.
                    app.push_screen(RestoreRunScreen(
                        backend=backend,
                        encryption_password="pw",
                        computer_uuid=r1.computer_uuid,
                        folder_uuid=r1.folder_uuid,
                        backuprecord_path=None,
                        target=target,
                        paths=["한글.txt", "subdir"],
                    ))
                    panel = None
                    for _ in range(300):
                        await pilot.pause()
                        await asyncio.sleep(0.05)
                        if panel is None:
                            try:
                                panel = app.screen.query_one(ProgressPanel)
                            except Exception:
                                continue
                        if panel.finished or panel.failed:
                            break
                    self.assertTrue(panel.finished)
            finally:
                close_backend(backend)

            self.assertEqual(
                (target / "한글.txt").read_bytes(),
                "내용".encode("utf-8"),
            )
            self.assertEqual(
                (target / "subdir" / "gamma.txt").read_bytes(),
                b"gamma\n",
            )
            # Excluded files must not appear.
            self.assertFalse((target / "alpha.txt").exists())
            self.assertFalse((target / "beta.txt").exists())


if __name__ == "__main__":
    unittest.main()
