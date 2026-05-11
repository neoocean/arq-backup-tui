"""E4 — ACL / Finder info / resource fork policy.

This module pins the writer/reader behaviour for three macOS-
specific metadata channels that aren't covered by mtime / mode /
xattr round-trip tests:

- **ACLs** — captured via the platform CLI (``ls -le`` on macOS,
  ``getfacl`` on Linux), restored via ``chmod +a`` / ``setfacl``.
  Stored as a single ``aclBlobLoc`` per Node.
- **com.apple.FinderInfo** — 32-byte structured xattr carrying
  Finder's per-file metadata (label, custom icon flag, creator
  code). Treated as a regular xattr: captured + restored via
  the XAttrSetV002 blob alongside other xattrs.
- **com.apple.ResourceFork** — legacy resource-fork xattr that
  classic Mac apps used for icons / dialog resources. Modern
  apps mostly don't use it, but third-party tools (BBEdit,
  some older Adobe artefacts) still do. Also treated as a
  regular xattr.

The principal claim being pinned: **ACLs, FinderInfo, and
ResourceFork all survive a backup → restore round-trip on
their native platform**. Cross-platform restore (macOS ACL on
Linux, etc.) deliberately doesn't translate — the captured
bytes are platform-specific. That stance is documented in
``arq_writer/acl.py`` and exercised by
``test_cross_platform_acl_apply_is_skipped``.
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from arq_reader import Restore
from arq_writer.acl import apply_acl, capture_acl, has_acl_support
from arq_writer.backup import build_backup


def _has_cmd(name: str) -> bool:
    from shutil import which
    return which(name) is not None


# ---------------------------------------------------------------------------
# macOS ACL round-trip
# ---------------------------------------------------------------------------


@unittest.skipUnless(
    platform.system() == "Darwin" and _has_cmd("chmod") and _has_cmd("ls"),
    "macOS NFSv4 ACL round-trip needs Darwin + chmod + ls",
)
class MacOSACLRoundTripTests(unittest.TestCase):
    def test_user_allow_read_acl_round_trips(self) -> None:
        # macOS NFSv4 ACL targets must resolve via the local
        # directory service. ``everyone`` works on some systems
        # but fails on minimal CI hosts; the current user (always
        # exists, always resolvable) is the safe choice.
        import getpass
        username = getpass.getuser()
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            target = src / "with_acl.txt"
            target.write_bytes(b"content under ACL\n")
            cp = subprocess.run(
                ["chmod", "+a", f"user:{username} allow read",
                 str(target)],
                capture_output=True, text=True,
            )
            if cp.returncode != 0:
                self.skipTest(
                    f"chmod +a failed on this host (returncode "
                    f"{cp.returncode}): {cp.stderr!r}"
                )
            captured = capture_acl(target)
            self.assertTrue(
                captured.startswith(b"ACL_MACOS_NFSV4\n"),
                f"expected ACL_MACOS_NFSV4 header, got {captured[:32]!r}",
            )
            self.assertIn(username.encode(), captured)

            # End-to-end: backup → restore preserves the ACL.
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
            restored = out / "with_acl.txt"
            self.assertTrue(restored.is_file())
            self.assertEqual(
                restored.read_bytes(), b"content under ACL\n",
            )
            recaptured = capture_acl(restored)
            self.assertTrue(
                recaptured.startswith(b"ACL_MACOS_NFSV4\n"),
                "restored ACL header missing — restore didn't "
                "reapply the ACL",
            )
            self.assertIn(username.encode(), recaptured)


# ---------------------------------------------------------------------------
# Cross-platform refusal (the documented stance)
# ---------------------------------------------------------------------------


class CrossPlatformACLApplyTests(unittest.TestCase):
    """When the wrong-platform ACL blob lands on this host, apply
    should refuse gracefully and emit ``acl_apply_skipped`` with
    a clear reason. The captured bytes are platform-specific."""

    def test_cross_platform_acl_apply_is_skipped(self) -> None:
        events = []

        def cb(kind, payload):
            events.append((kind, payload))

        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "f.txt"
            target.write_bytes(b"")
            # Synthesize a "wrong platform" ACL blob: pick the OPPOSITE
            # platform's header from what we're running on.
            if platform.system() == "Darwin":
                wrong = b"ACL_LINUX_POSIX\nuser:foo:rwx\n"
            else:
                wrong = b"ACL_MACOS_NFSV4\n0: user:foo allow read\n"
            applied = apply_acl(target, wrong, callback=cb)
            self.assertFalse(
                applied,
                "wrong-platform ACL must not be applied",
            )
            skipped = [
                p for (k, p) in events if k == "acl_apply_skipped"
            ]
            self.assertEqual(len(skipped), 1)
            self.assertEqual(skipped[0]["reason"], "wrong-platform")

    def test_unknown_magic_acl_apply_errors_cleanly(self) -> None:
        events = []
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "f.txt"
            target.write_bytes(b"")
            unknown = b"ACL_MARS_QUANTUM_v9\nnonsense"
            applied = apply_acl(
                target, unknown,
                callback=lambda k, p: events.append((k, p)),
            )
            self.assertFalse(applied)
            errors = [
                p for (k, p) in events if k == "acl_apply_error"
            ]
            self.assertEqual(len(errors), 1)
            self.assertIn("unknown acl blob magic", errors[0]["error"])


# ---------------------------------------------------------------------------
# FinderInfo + ResourceFork xattr handling
# ---------------------------------------------------------------------------


class FinderInfoAndResourceForkTests(unittest.TestCase):
    """``com.apple.FinderInfo`` and ``com.apple.ResourceFork`` are
    regular xattrs from the format's perspective — they ride in
    the same XAttrSetV002 blob as everything else. Pin that the
    format-handler doesn't do anything special with their
    well-known names (e.g. silently strip them, reformat their
    32-byte FinderInfo structure, etc.).
    """

    def test_finder_info_xattr_round_trips_through_serialize(self) -> None:
        from arq_writer.xattrs import (
            deserialize_xattrs, serialize_xattrs,
        )
        # 32 bytes — the canonical FinderInfo size. Use a
        # recognisable pattern so a silent reformat would surface
        # as a content diff.
        finder_info = bytes([
            0x46, 0x49, 0x4E, 0x46,    # "FINF" creator-code-ish
            0x54, 0x45, 0x58, 0x54,    # "TEXT" type-code-ish
            0, 0, 0, 0, 0, 0, 0, 0,
            0, 0, 0, 0, 0, 0, 0, 0,
            0, 0, 0, 0, 0, 0, 0, 0,
        ])
        self.assertEqual(len(finder_info), 32)
        xattrs = {
            "com.apple.FinderInfo": finder_info,
            "user.other": b"value",
        }
        blob = serialize_xattrs(xattrs)
        decoded = deserialize_xattrs(blob)
        self.assertEqual(
            decoded["com.apple.FinderInfo"], finder_info,
            "FinderInfo bytes must round-trip unchanged",
        )
        self.assertEqual(decoded["user.other"], b"value")
        # Order preserved.
        self.assertEqual(
            list(decoded.keys()),
            ["com.apple.FinderInfo", "user.other"],
        )

    def test_resource_fork_xattr_round_trips_through_serialize(self) -> None:
        from arq_writer.xattrs import (
            deserialize_xattrs, serialize_xattrs,
        )
        # Resource forks can be large (icons, classic dialog
        # resources). 64 KiB exercises the multi-kilobyte path.
        rfork = (b"RSRC" + bytes(range(256))) * 256
        self.assertGreaterEqual(len(rfork), 64 * 1024)
        xattrs = {
            "com.apple.ResourceFork": rfork,
            "com.apple.FinderInfo": b"\x00" * 32,
        }
        blob = serialize_xattrs(xattrs)
        decoded = deserialize_xattrs(blob)
        self.assertEqual(
            decoded["com.apple.ResourceFork"], rfork,
            "ResourceFork bytes (including binary content + "
            "multi-KiB length) must round-trip unchanged",
        )
        self.assertEqual(serialize_xattrs(decoded), blob)


if __name__ == "__main__":
    unittest.main()
