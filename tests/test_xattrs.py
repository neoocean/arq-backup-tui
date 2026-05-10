"""Tests for the macOS / Linux xattr capture + restore path.

The writer's old behaviour was to emit empty ``xattrsBlobLocs``
on every Node; the restore path didn't even read the field. As
of this change xattrs are captured per-entry into one consolidated
binary-plist blob and re-applied at restore. These tests pin:

- The serialize/deserialize round-trip for both empty + populated
  xattr dicts.
- The actual capture → blob → apply pipeline against a real
  filesystem (skipped on hosts without xattr support).
- Backup → restore round-trip via the public ``Backup`` /
  ``Restore`` classes, asserting the restored file's xattrs
  match the source byte-for-byte.

The xattr APIs available on macOS go through ``ctypes`` against
libc (Python's stdlib doesn't expose them on Darwin); on Linux
they go through ``os.{list,get,set}xattr``. Both code paths land
in the same ``capture_xattrs`` / ``apply_xattrs`` entry points,
so the same tests cover both runtimes.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from arq_writer.xattrs import (
    apply_xattrs,
    capture_xattrs,
    deserialize_xattrs,
    has_xattr_support,
    serialize_xattrs,
)


class SerializeRoundTripTests(unittest.TestCase):
    """Pure encode/decode — works on every platform regardless of
    whether the host actually exposes xattr APIs."""

    def test_empty_dict_serializes_to_empty_bytes(self) -> None:
        # Empty dict short-circuits to b"" so the writer can skip
        # the BlobLoc allocation entirely.
        self.assertEqual(serialize_xattrs({}), b"")

    def test_round_trip_preserves_binary_values(self) -> None:
        # Real xattr values are arbitrary binary (Finder labels,
        # quarantine plists, ResourceFork). We must NOT base64 or
        # str-encode them.
        xattrs = {
            "user.text": b"hello",
            "com.apple.fakedata": b"\x00\x01\x02\xff\xfe",
            "trusted.empty": b"",
        }
        decoded = deserialize_xattrs(serialize_xattrs(xattrs))
        self.assertEqual(decoded, xattrs)

    def test_deserialize_of_empty_returns_empty_dict(self) -> None:
        # Round-trip the b"" sentinel.
        self.assertEqual(deserialize_xattrs(b""), {})

    def test_serialize_emits_arq_xattrset_v002_magic(self) -> None:
        # Format conformance: every non-empty serialize must start
        # with "XAttrSetV002" so an Arq.app reader can recognise
        # our output.
        out = serialize_xattrs({"user.k": b"v"})
        self.assertTrue(out.startswith(b"XAttrSetV002"))

    def test_deserialize_real_operator_blob(self) -> None:
        """The 68-byte xattr blob captured from the operator's
        actual Arq.app destination via probe_xattr_blob.py.
        Decoded via reverse-engineering of the XAttrSetV002 format
        — pin its decode result so a future format-handler change
        can't silently corrupt operator-readable bytes."""
        from binascii import unhexlify
        real = unhexlify(
            "5841747472536574563030320000000000000001"
            "010000000000000014"
            "636f6d2e6170706c652e70726f76656e616e6365"
            "000000000000000b"
            "0102008129abf0b51596fe"
        )
        parsed = deserialize_xattrs(real)
        self.assertEqual(set(parsed.keys()), {"com.apple.provenance"})
        self.assertEqual(
            parsed["com.apple.provenance"],
            bytes.fromhex("0102008129abf0b51596fe"),
        )

    def test_deserialize_legacy_binary_plist_still_works(self) -> None:
        """Backward-compat: destinations written with the original
        binary-plist xattr format must keep restoring through the
        new code."""
        import plistlib
        legacy = plistlib.dumps(
            {"user.legacy": b"hello"},
            fmt=plistlib.FMT_BINARY,
        )
        self.assertTrue(legacy.startswith(b"bplist00"))
        parsed = deserialize_xattrs(legacy)
        self.assertEqual(parsed, {"user.legacy": b"hello"})

    def test_deserialize_unknown_format_raises(self) -> None:
        with self.assertRaises(ValueError):
            deserialize_xattrs(b"\x00random\x00garbage")


@unittest.skipUnless(
    has_xattr_support(), "this host has no xattr APIs",
)
class FilesystemRoundTripTests(unittest.TestCase):
    """End-to-end via the actual ctypes / stdlib APIs.

    Uses the system ``xattr`` CLI (macOS) / ``setfattr`` (Linux)
    to seed values when present; falls back to ``apply_xattrs``
    itself when neither is on PATH (Linux CI without attr-utils).
    """

    def _seed_xattrs(self, path: str, items: dict) -> None:
        """Write ``items`` to ``path`` via the OS's CLI when
        available, else via our own apply_xattrs (which is the
        same code path the restore exercises)."""
        if sys.platform == "darwin" and self._has_cmd("xattr"):
            for name, value in items.items():
                subprocess.run(
                    ["xattr", "-wx", name, value.hex(), path],
                    check=True, capture_output=True,
                )
            return
        if sys.platform.startswith("linux") and self._has_cmd("setfattr"):
            for name, value in items.items():
                # setfattr's -v takes the value with a 0x prefix
                # for hex, which lets us preserve binary.
                subprocess.run(
                    ["setfattr", "-n", name,
                     "-v", "0x" + value.hex(), path],
                    check=True, capture_output=True,
                )
            return
        # No CLI available: bootstrap via apply_xattrs itself.
        apply_xattrs(path, items)

    @staticmethod
    def _has_cmd(name: str) -> bool:
        from shutil import which
        return which(name) is not None

    def test_capture_returns_empty_for_file_with_no_xattrs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "blank.txt"
            p.write_bytes(b"x")
            captured = capture_xattrs(p)
            # Recent macOS releases (≥ Sequoia) auto-attach
            # ``com.apple.provenance`` to every file the kernel sees
            # written, regardless of whether userland touches xattrs.
            # That's a system-managed attribute, not a "the user set
            # an xattr" event — the contract this test pins is "no
            # *user-applied* xattrs ⇒ no user-visible xattrs in the
            # capture". Filter the system-managed namespace before
            # asserting empty, matching the same robustness pattern
            # ``test_capture_then_apply_round_trips_byte_perfect``
            # already uses for its captured set.
            user_visible = {
                k: v for k, v in captured.items()
                if not k.startswith("com.apple.provenance")
            }
            self.assertEqual(user_visible, {})

    def test_capture_then_apply_round_trips_byte_perfect(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "src.bin"
            src.write_bytes(b"original")
            seed = {
                "user.simple": b"value-bytes",
                "user.binary": b"\x00\x01\x02\x03\xff",
            }
            try:
                self._seed_xattrs(str(src), seed)
            except subprocess.CalledProcessError as exc:
                self.skipTest(
                    "this filesystem rejects xattrs: "
                    f"{exc.stderr!r}"
                )
            captured = capture_xattrs(src)
            # The kernel may surface system-managed xattrs we didn't
            # seed (e.g. com.apple.* on macOS); our seeded ones must
            # be a subset of what's captured and match byte-for-byte.
            for name, value in seed.items():
                self.assertIn(name, captured)
                self.assertEqual(captured[name], value)
            # Apply onto a fresh file → re-capture matches.
            dst = Path(td) / "dst.bin"
            dst.write_bytes(b"different content")
            applied = apply_xattrs(dst, seed)
            self.assertEqual(applied, len(seed))
            recaptured = capture_xattrs(dst)
            for name, value in seed.items():
                self.assertEqual(recaptured.get(name), value)


@unittest.skipUnless(
    has_xattr_support(), "this host has no xattr APIs",
)
class BackupRestoreRoundTripTests(unittest.TestCase):
    """The full pipeline: walk a source dir whose files carry
    xattrs through ``Backup``, then restore via ``Restore``, and
    confirm the restored copies have matching xattrs.

    Skips on hosts without OpenSSL (the writer needs it for
    AES-256-CBC). The arq-backup-tui CI matrix supplies it; ad-hoc
    stripped builds fall through cleanly.
    """

    def test_backup_then_restore_preserves_user_xattr(self) -> None:
        try:
            subprocess.run(
                ["openssl", "version"],
                check=True, capture_output=True, timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            self.skipTest("openssl CLI required")

        from arq_writer import build_backup
        from arq_reader import Restore

        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "src"
            src.mkdir()
            (src / "subdir").mkdir()
            f1 = src / "subdir" / "with-xattrs.txt"
            f1.write_bytes(b"file content")

            seed = {"user.testkey": b"hello-xattr-world"}
            try:
                if sys.platform == "darwin":
                    subprocess.run(
                        ["xattr", "-w",
                         "user.testkey", "hello-xattr-world",
                         str(f1)],
                        check=True, capture_output=True,
                    )
                else:
                    apply_xattrs(f1, seed)
            except subprocess.CalledProcessError as exc:
                self.skipTest(
                    "filesystem rejected xattr seed: "
                    f"{exc.stderr!r}"
                )
            # Confirm seed actually stuck before we go further —
            # tmpfs on some Linux CI runners silently rejects
            # user.* xattrs.
            if not capture_xattrs(f1):
                self.skipTest(
                    "filesystem accepted xattr writes but won't "
                    "surface them on read; can't test round-trip"
                )

            dst = Path(td) / "dst"
            dst.mkdir()
            build_backup(
                src, dst, "hunter2", backup_name="xattr-test",
            )

            # Restore into a fresh dir + verify xattrs persist.
            out = Path(td) / "out"
            rs = Restore(str(dst), encryption_password="hunter2")
            cuuid = next(
                p.name for p in dst.iterdir() if p.is_dir()
            )
            from arq_reader.restore import find_latest_backuprecord
            from arq_validator.backend import LocalBackend
            backend = LocalBackend(dst)
            folder_uuids = list(
                rs._iter_folder_uuids(cuuid)
            ) if hasattr(rs, "_iter_folder_uuids") else []
            # Discover folder UUID via filesystem rather than
            # relying on a Restore-private helper.
            recs_root = dst / cuuid / "backupfolders"
            folder_uuid = next(
                p.name for p in recs_root.iterdir()
                if p.is_dir()
            )
            rs.restore(
                folder_uuid=folder_uuid,
                computer_uuid=cuuid,
                dest=out,
            )

            # The restored file should carry our seeded xattr.
            restored = out / "subdir" / "with-xattrs.txt"
            self.assertTrue(restored.is_file())
            recaptured = capture_xattrs(restored)
            self.assertEqual(
                recaptured.get("user.testkey"),
                b"hello-xattr-world",
                f"xattr did not survive backup→restore; "
                f"got {recaptured!r}",
            )


if __name__ == "__main__":
    unittest.main()
