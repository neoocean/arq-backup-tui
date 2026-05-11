"""N5 — ResourceFork + FinderInfo xattr end-to-end round-trip
on macOS.

F4-F7 (Round 6) tested xattr serialize/deserialize via
synthetic blobs. N5 closes the macOS-OS-level half: write the
two Apple-canonical xattrs to a real file via `xattr -wx`, back
up via our writer, restore via our reader, and verify the
restored file has the SAME xattr bytes.

ResourceFork in particular has historically had length quirks
(can be very large; on HFS+ stored as a separate fork). N5
tests both 32-byte FinderInfo and a few KB of ResourceFork.

Auto-skips on non-Darwin or when openssl is unavailable.
"""

from __future__ import annotations

import os
import platform
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


def _xattr_set(path: Path, name: str, value: bytes) -> bool:
    """Set xattr ``name`` on ``path`` to ``value``. Returns
    True on success, False on platform mismatch."""
    try:
        subprocess.run(
            ["xattr", "-wx", name, value.hex(), str(path)],
            check=True, capture_output=True, timeout=5,
        )
        return True
    except (subprocess.SubprocessError, OSError):
        return False


def _xattr_get(path: Path, name: str) -> bytes:
    proc = subprocess.run(
        ["xattr", "-px", name, str(path)],
        check=True, capture_output=True, text=True, timeout=5,
    )
    # xattr -px prints hex with spaces / newlines.
    hex_clean = "".join(
        c for c in proc.stdout if c in "0123456789abcdefABCDEF"
    )
    return bytes.fromhex(hex_clean)


@unittest.skipUnless(
    platform.system() == "Darwin",
    "ResourceFork + FinderInfo xattrs are macOS-specific",
)
@unittest.skipUnless(_has_openssl(), "openssl CLI required")
class N5_ResourceForkFinderInfoRoundTripTests(unittest.TestCase):

    def test_finderinfo_32bytes_full_round_trip(self) -> None:
        from arq_writer.backup import build_backup
        from arq_reader.restore import Restore
        finderinfo = bytes.fromhex(
            "fefefefefefefefe"   # 0-7
            "00caca0011caca00"   # 8-15
            "deadbeefdeadbeef"   # 16-23
            "10203040cafebabe"   # 24-31
        )
        self.assertEqual(len(finderinfo), 32)
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            f = src / "with_finderinfo.txt"
            f.write_bytes(b"content with FinderInfo xattr\n")
            if not _xattr_set(f, "com.apple.FinderInfo", finderinfo):
                self.skipTest(
                    "xattr -wx not available / failed; FS may "
                    "not support FinderInfo",
                )
            self.assertEqual(
                _xattr_get(f, "com.apple.FinderInfo"), finderinfo,
            )
            dest = tdp / "dest"
            res = build_backup(
                str(src), str(dest), encryption_password="pw",
            )
            out = tdp / "out"
            out.mkdir()
            r = Restore(str(dest), encryption_password="pw")
            r.restore(folder_uuid=res.folder_uuid, dest=str(out))
            restored = next(
                out.rglob("with_finderinfo.txt"), None,
            )
            self.assertIsNotNone(restored)
            self.assertEqual(
                _xattr_get(restored, "com.apple.FinderInfo"),
                finderinfo,
                f"FinderInfo bytes not preserved across "
                f"backup → restore",
            )

    def test_resourcefork_large_payload_round_trip(self) -> None:
        """ResourceFork can carry KB-scale binary data
        historically (icon resources, etc.). Verify our writer
        + reader handle a 4 KB ResourceFork payload."""
        from arq_writer.backup import build_backup
        from arq_reader.restore import Restore
        # Synthetic 4096-byte resource fork content.
        rfork = bytes(range(256)) * 16  # 4096 bytes
        self.assertEqual(len(rfork), 4096)
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            f = src / "has_rfork.bin"
            f.write_bytes(b"data fork content\n")
            if not _xattr_set(f, "com.apple.ResourceFork", rfork):
                self.skipTest(
                    "xattr -wx ResourceFork failed on this "
                    "filesystem (e.g. APFS may have a size cap)",
                )
            stored = _xattr_get(f, "com.apple.ResourceFork")
            if stored != rfork:
                self.skipTest(
                    "filesystem stored ResourceFork "
                    "differently than set; can't test "
                    "round-trip"
                )
            dest = tdp / "dest"
            res = build_backup(
                str(src), str(dest), encryption_password="pw",
            )
            out = tdp / "out"
            out.mkdir()
            r = Restore(str(dest), encryption_password="pw")
            r.restore(folder_uuid=res.folder_uuid, dest=str(out))
            restored = next(out.rglob("has_rfork.bin"), None)
            self.assertIsNotNone(restored)
            # ResourceFork bytes preserved (may be missing if
            # the reader skips the restore for some reason; if
            # so, that's a real gap).
            try:
                restored_rf = _xattr_get(
                    restored, "com.apple.ResourceFork",
                )
            except subprocess.CalledProcessError:
                self.fail(
                    "ResourceFork xattr missing on restored "
                    "file — writer/reader dropped it",
                )
            self.assertEqual(
                restored_rf, rfork,
                f"ResourceFork bytes drifted: "
                f"in={len(rfork)} out={len(restored_rf)}",
            )

    def test_both_xattrs_coexist_on_same_file(self) -> None:
        """A file with BOTH FinderInfo and ResourceFork should
        round-trip both."""
        from arq_writer.backup import build_backup
        from arq_reader.restore import Restore
        finfo = bytes.fromhex(
            "1122334455667788"
            "1122334455667788"
            "1122334455667788"
            "1122334455667788"
        )
        rfork = b"\xfa" * 512
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            f = src / "both.dat"
            f.write_bytes(b"data\n")
            if not _xattr_set(f, "com.apple.FinderInfo", finfo):
                self.skipTest("FinderInfo set failed")
            if not _xattr_set(
                f, "com.apple.ResourceFork", rfork,
            ):
                self.skipTest("ResourceFork set failed")
            dest = tdp / "dest"
            res = build_backup(
                str(src), str(dest), encryption_password="pw",
            )
            out = tdp / "out"
            out.mkdir()
            r = Restore(str(dest), encryption_password="pw")
            r.restore(folder_uuid=res.folder_uuid, dest=str(out))
            restored = next(out.rglob("both.dat"), None)
            self.assertIsNotNone(restored)
            self.assertEqual(
                _xattr_get(restored, "com.apple.FinderInfo"),
                finfo,
            )
            try:
                rf_out = _xattr_get(
                    restored, "com.apple.ResourceFork",
                )
                self.assertEqual(rf_out, rfork)
            except subprocess.CalledProcessError:
                self.fail(
                    "ResourceFork xattr dropped during restore",
                )


if __name__ == "__main__":
    unittest.main()
