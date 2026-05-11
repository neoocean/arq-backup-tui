"""C-H2 — macOS bundle / .app directory handling.

``.app``, ``.pkg``, ``.bundle`` etc. on macOS are **directories**
with internal structure (``Contents/MacOS/``, ``Contents/Info.plist``,
…). The Finder treats them as opaque packages but the
filesystem-level walker should see them as ordinary directories
and recurse into them.

This module pins:

- ``.app`` bundle recursed into; internal files round-trip
- ``Contents/Info.plist`` survives byte-identical
- Nested bundles (.app within .app's plugins/) handled
- Symlinks inside bundles preserved
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


def _has_openssl() -> bool:
    try:
        subprocess.run(
            ["openssl", "version"],
            check=True, capture_output=True, timeout=5,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


@unittest.skipUnless(_has_openssl(), "openssl CLI required")
class MacOSBundleHandlingTests(unittest.TestCase):

    def _make_app_bundle(self, src: Path) -> Path:
        """Create a synthetic .app bundle structure."""
        app = src / "TestApp.app"
        (app / "Contents" / "MacOS").mkdir(parents=True)
        (app / "Contents" / "Resources").mkdir(parents=True)
        (app / "Contents" / "Info.plist").write_bytes(
            b"<?xml version=\"1.0\"?>\n<plist><dict>"
            b"<key>CFBundleIdentifier</key>"
            b"<string>com.test.app</string>"
            b"</dict></plist>\n",
        )
        (app / "Contents" / "MacOS" / "TestApp").write_bytes(
            b"#!/bin/sh\necho hello\n",
        )
        os.chmod(app / "Contents" / "MacOS" / "TestApp", 0o755)
        (app / "Contents" / "Resources" / "icon.icns").write_bytes(
            b"icns" + b"\x00" * 100,
        )
        return app

    def test_app_bundle_round_trips_with_internal_structure(
        self,
    ) -> None:
        from arq_writer.backup import build_backup
        from arq_reader import Restore
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            self._make_app_bundle(src)
            dest = tdp / "dest"
            build_backup(
                str(src), str(dest), encryption_password="pw",
            )
            out = tdp / "out"
            out.mkdir()
            rs = Restore(str(dest), encryption_password="pw")
            layouts = rs.layouts()
            rs.restore(
                folder_uuid=layouts[0].backup_folder_uuids[0],
                computer_uuid=layouts[0].computer_uuid, dest=out,
            )
            # Bundle structure intact.
            restored_app = out / "TestApp.app"
            self.assertTrue(restored_app.is_dir())
            info = restored_app / "Contents" / "Info.plist"
            self.assertTrue(info.is_file())
            self.assertIn(b"CFBundleIdentifier", info.read_bytes())
            exe = restored_app / "Contents" / "MacOS" / "TestApp"
            self.assertTrue(exe.is_file())
            # Executable bit preserved.
            self.assertTrue(
                exe.stat().st_mode & 0o111,
                "executable bit lost on restore",
            )

    def test_nested_bundles_handled(self) -> None:
        """A plugin .app inside an outer .app's PlugIns/ folder
        should round-trip just as well as the outer."""
        from arq_writer.backup import build_backup
        from arq_reader import Restore
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            outer = self._make_app_bundle(src)
            plugins = outer / "Contents" / "PlugIns"
            plugins.mkdir()
            inner_dir = plugins / "MyPlugin.appex"
            (inner_dir / "Contents").mkdir(parents=True)
            (inner_dir / "Contents" / "Info.plist").write_bytes(
                b"<?xml version=\"1.0\"?>\n<plist><dict>"
                b"<key>CFBundleIdentifier</key>"
                b"<string>com.test.plugin</string>"
                b"</dict></plist>\n",
            )
            dest = tdp / "dest"
            build_backup(
                str(src), str(dest), encryption_password="pw",
            )
            out = tdp / "out"
            out.mkdir()
            rs = Restore(str(dest), encryption_password="pw")
            layouts = rs.layouts()
            rs.restore(
                folder_uuid=layouts[0].backup_folder_uuids[0],
                computer_uuid=layouts[0].computer_uuid, dest=out,
            )
            inner_restored = (
                out / "TestApp.app" / "Contents"
                / "PlugIns" / "MyPlugin.appex"
            )
            self.assertTrue(inner_restored.is_dir())
            self.assertIn(
                b"com.test.plugin",
                (inner_restored / "Contents" / "Info.plist")
                .read_bytes(),
            )

    def test_symlink_inside_bundle_preserved(self) -> None:
        """Frameworks/X.framework/Versions/Current is typically a
        symlink to ``A`` — must restore as a symlink, not as
        a copy of the target."""
        from arq_writer.backup import build_backup
        from arq_reader import Restore
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            app = self._make_app_bundle(src)
            fwk = app / "Contents" / "Frameworks" / "X.framework"
            fwk_a = fwk / "Versions" / "A"
            fwk_a.mkdir(parents=True)
            (fwk_a / "X").write_bytes(b"dylib bytes")
            os.symlink("A", str(fwk / "Versions" / "Current"))
            os.symlink("Versions/Current/X", str(fwk / "X"))
            dest = tdp / "dest"
            build_backup(
                str(src), str(dest), encryption_password="pw",
            )
            out = tdp / "out"
            out.mkdir()
            rs = Restore(str(dest), encryption_password="pw")
            layouts = rs.layouts()
            rs.restore(
                folder_uuid=layouts[0].backup_folder_uuids[0],
                computer_uuid=layouts[0].computer_uuid, dest=out,
            )
            current_link = (
                out / "TestApp.app" / "Contents"
                / "Frameworks" / "X.framework"
                / "Versions" / "Current"
            )
            self.assertTrue(
                current_link.is_symlink(),
                "Versions/Current must restore as symlink",
            )
            self.assertEqual(os.readlink(current_link), "A")


if __name__ == "__main__":
    unittest.main()
