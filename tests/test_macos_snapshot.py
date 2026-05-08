"""Tests for the APFS snapshot helper.

Live macOS execution requires `tmutil` + `mount_apfs` + sudo, so
the tests here run on Linux/CI by **mocking subprocess.run**.
The same code path is exercised; we just intercept the
``tmutil`` / ``mount_apfs`` invocations and assert the right
arguments + return values flow through.

For real macOS verification an operator runs
``arq-backup ... --use-apfs-snapshot`` against a known fixture
and pastes the resulting backuprecord stat snapshot back; see
``docs/APFS-SNAPSHOTS.md`` §5.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from arq_writer.macos_snapshot import (
    NotMacOSError,
    SnapshotError,
    SnapshotInfo,
    _strip_volume_anchor,
    create_snapshot,
    delete_snapshot,
    is_macos,
    is_macos_apfs,
    list_snapshots,
    mount_snapshot,
    unmount_snapshot,
)


# ---------------------------------------------------------------------------
# Platform-detection guards
# ---------------------------------------------------------------------------


class PlatformGuardTests(unittest.TestCase):
    def test_non_macos_create_raises(self) -> None:
        if is_macos():
            self.skipTest("real macOS — guard test is for Linux")
        with self.assertRaises(NotMacOSError):
            create_snapshot()

    def test_non_macos_list_raises(self) -> None:
        if is_macos():
            self.skipTest("real macOS")
        with self.assertRaises(NotMacOSError):
            list_snapshots()

    def test_non_macos_mount_raises(self) -> None:
        if is_macos():
            self.skipTest("real macOS")
        with self.assertRaises(NotMacOSError):
            mount_snapshot(
                SnapshotInfo(name="x"), mount_point=Path("/tmp/x"),
            )

    def test_non_macos_is_macos_apfs_returns_false(self) -> None:
        if is_macos():
            self.skipTest("real macOS")
        # Any path on non-macOS → False without raising.
        self.assertFalse(is_macos_apfs(Path("/tmp")))


# ---------------------------------------------------------------------------
# Snapshot listing parser
# ---------------------------------------------------------------------------


class ListSnapshotsParserTests(unittest.TestCase):
    """The output parser is logic-only; we feed it canned tmutil
    output via mock and check parsing."""

    def test_parses_typical_tmutil_output(self) -> None:
        sample = (
            "Snapshots for volume group containing disk2s1:\n"
            "com.apple.TimeMachine.2026-05-08-130000.local\n"
            "com.apple.TimeMachine.2026-05-07-130000.local\n"
        )
        with patch("arq_writer.macos_snapshot.is_macos", return_value=True), \
             patch("subprocess.run") as run:
            run.return_value = MagicMock(
                returncode=0, stdout=sample, stderr="",
            )
            snaps = list_snapshots()
        self.assertEqual(len(snaps), 2)
        names = [s.name for s in snaps]
        self.assertIn(
            "com.apple.TimeMachine.2026-05-08-130000.local",
            names,
        )
        # Date stamp is parsed.
        self.assertEqual(snaps[0].creation_iso, "2026-05-08-130000")

    def test_skips_garbage_lines(self) -> None:
        sample = (
            "Snapshots for volume group containing disk2s1:\n"
            "garbage line that doesn't match\n"
            "com.apple.TimeMachine.2026-05-08-130000.local\n"
        )
        with patch("arq_writer.macos_snapshot.is_macos", return_value=True), \
             patch("subprocess.run") as run:
            run.return_value = MagicMock(
                returncode=0, stdout=sample, stderr="",
            )
            snaps = list_snapshots()
        self.assertEqual(len(snaps), 1)

    def test_failure_raises_snapshot_error(self) -> None:
        with patch("arq_writer.macos_snapshot.is_macos", return_value=True), \
             patch("subprocess.run") as run:
            run.return_value = MagicMock(
                returncode=2, stdout="",
                stderr="tmutil: not authorized",
            )
            with self.assertRaises(SnapshotError):
                list_snapshots()


# ---------------------------------------------------------------------------
# Snapshot create + mount happy path (mocked)
# ---------------------------------------------------------------------------


class SnapshotLifecycleMockedTests(unittest.TestCase):
    """End-to-end create → mount → unmount → delete with all
    subprocess.run invocations mocked."""

    def test_create_returns_newest_snapshot(self) -> None:
        sample = (
            "Snapshots for /:\n"
            "com.apple.TimeMachine.2026-05-08-130000.local\n"
            "com.apple.TimeMachine.2026-05-08-140000.local\n"
        )
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            if cmd[:2] == ["sudo", "tmutil"]:
                return MagicMock(returncode=0, stderr=b"", stdout=b"")
            if cmd[:2] == ["tmutil", "listlocalsnapshots"]:
                return MagicMock(
                    returncode=0, stdout=sample, stderr="",
                )
            return MagicMock(returncode=0, stderr=b"", stdout=b"")

        with patch("arq_writer.macos_snapshot.is_macos", return_value=True), \
             patch("subprocess.run", side_effect=fake_run):
            snap = create_snapshot()
        self.assertEqual(
            snap.name,
            "com.apple.TimeMachine.2026-05-08-140000.local",
        )
        # Verified: sudo tmutil localsnapshot was called.
        self.assertTrue(any(
            c[:3] == ["sudo", "tmutil", "localsnapshot"] for c in calls
        ))

    def test_mount_sends_correct_args(self) -> None:
        snap = SnapshotInfo(
            name="com.apple.TimeMachine.2026-05-08-140000.local",
            creation_iso="2026-05-08-140000",
        )
        captured = {}

        def fake_run(cmd, **kw):
            captured["cmd"] = list(cmd)
            return MagicMock(returncode=0, stderr=b"", stdout=b"")

        with patch("arq_writer.macos_snapshot.is_macos", return_value=True), \
             patch("subprocess.run", side_effect=fake_run):
            with patch.object(Path, "mkdir"):
                mount_snapshot(snap, mount_point=Path("/tmp/test-mount"))
        cmd = captured["cmd"]
        self.assertEqual(cmd[:2], ["sudo", "mount_apfs"])
        self.assertIn("-s", cmd)
        s_idx = cmd.index("-s")
        self.assertEqual(cmd[s_idx + 1], snap.name)
        # Read-only + nobrowse flags
        o_idx = cmd.index("-o")
        self.assertEqual(cmd[o_idx + 1], "ro,nobrowse")

    def test_delete_uses_date_stamp_not_full_name(self) -> None:
        snap = SnapshotInfo(
            name="com.apple.TimeMachine.2026-05-08-140000.local",
            creation_iso="2026-05-08-140000",
        )
        captured = {}

        def fake_run(cmd, **kw):
            captured["cmd"] = list(cmd)
            return MagicMock(returncode=0, stderr=b"", stdout=b"")

        with patch("arq_writer.macos_snapshot.is_macos", return_value=True), \
             patch("subprocess.run", side_effect=fake_run):
            delete_snapshot(snap)
        cmd = captured["cmd"]
        self.assertEqual(
            cmd, [
                "sudo", "tmutil", "deletelocalsnapshots",
                "2026-05-08-140000",
            ],
        )

    def test_delete_without_creation_iso_raises(self) -> None:
        snap = SnapshotInfo(name="bad-name", creation_iso="")
        with patch("arq_writer.macos_snapshot.is_macos", return_value=True):
            with self.assertRaises(SnapshotError):
                delete_snapshot(snap)


# ---------------------------------------------------------------------------
# Path-anchor translation helper
# ---------------------------------------------------------------------------


class StripVolumeAnchorTests(unittest.TestCase):
    def test_absolute_path_loses_leading_slash(self) -> None:
        out = _strip_volume_anchor(Path("/Users/me/foo"))
        self.assertEqual(out, Path("Users/me/foo"))

    def test_relative_path_unchanged(self) -> None:
        out = _strip_volume_anchor(Path("Users/me/foo"))
        self.assertEqual(out, Path("Users/me/foo"))

    def test_root_only_becomes_empty(self) -> None:
        out = _strip_volume_anchor(Path("/"))
        self.assertEqual(out, Path())


# ---------------------------------------------------------------------------
# build_backup with use_apfs_snapshot=True falls through on Linux
# ---------------------------------------------------------------------------


class BuildBackupSnapshotFallthroughTests(unittest.TestCase):
    def test_use_apfs_snapshot_falls_back_on_linux(self) -> None:
        # On non-macOS, the option must not crash; the writer
        # falls through to a live walk and emits an
        # `apfs_snapshot_skipped` event.
        if is_macos():
            self.skipTest("This test verifies non-macOS fallback")
        import tempfile

        from arq_reader import Restore
        from arq_writer import build_backup

        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "a.txt").write_bytes(b"alpha\n")
            dest = tdp / "dest"
            events = []

            def cb(kind, payload):
                events.append((kind, payload))

            r = build_backup(
                src, dest, encryption_password="pw",
                use_apfs_snapshot=True,
                callback=cb,
            )
            # The skip event must have been emitted.
            kinds = [k for k, _ in events]
            self.assertIn("apfs_snapshot_skipped", kinds)
            # Backup still completed and is restorable.
            out = tdp / "out"
            out.mkdir()
            Restore(dest, encryption_password="pw").restore(
                folder_uuid=r.folder_uuid,
                computer_uuid=r.computer_uuid,
                dest=out,
            )
            self.assertEqual(
                (out / "a.txt").read_bytes(), b"alpha\n",
            )


if __name__ == "__main__":
    unittest.main()
