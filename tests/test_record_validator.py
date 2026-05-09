"""Tests for the record-level validator.

The L0-L2 tiers in :mod:`arq_validator.tiers` cover layout shape,
magic bytes, latest-record HMAC, and a sampled object audit. None
of them follow a specific record's actual blob graph end-to-end.
:func:`arq_validator.record_validator.validate_record` plugs that
gap; these tests pin its happy path + corruption-detection
behaviour against a freshly-created backup.

Skips on hosts without OpenSSL since the writer + validator both
need it for AES-256-CBC.
"""

from __future__ import annotations

import os
import shutil
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
class ValidateRecordHappyPathTests(unittest.TestCase):
    """A clean backup must produce an all-OK report covering
    every blob the record references."""

    def _make_backup(self, td: Path) -> tuple:
        """Return (backend, record_path, dest_root)."""
        from arq_writer import build_backup
        from arq_validator.backend import LocalBackend

        src = td / "src"
        src.mkdir()
        (src / "a.txt").write_text("alpha\n")
        (src / "b.bin").write_bytes(b"\x01\x02\x03" * 100)
        (src / "sub").mkdir()
        (src / "sub" / "c.txt").write_text("gamma\n")

        dst = td / "dst"
        dst.mkdir()
        result = build_backup(
            src, dst, "secret", backup_name="rec-test",
        )
        backend = LocalBackend(dst)
        # build_backup returns the absolute backuprecord path; we
        # need the server-side ("/") relative form for the
        # validator. macOS rewrites /var → /private/var on a
        # tmpdir resolution, so resolve both before subtracting.
        rec_abs = Path(str(result.backuprecord_path)).resolve()
        dst_abs = dst.resolve()
        rec_rel = "/" + rec_abs.relative_to(dst_abs).as_posix()
        return backend, rec_rel, dst

    def test_clean_backup_passes_record_walk(self) -> None:
        from arq_validator.record_validator import validate_record
        with tempfile.TemporaryDirectory() as td:
            backend, rec_path, _ = self._make_backup(Path(td))
            report = validate_record(
                backend, rec_path, "secret",
            )
            self.assertTrue(
                report.ok,
                f"failures={report.failures!r}",
            )
            # Multiple blobs (a.txt + b.bin + sub/c.txt + at least
            # one tree blob); exact count varies with chunker but
            # 3 files + 2 trees = 5 minimum.
            self.assertGreaterEqual(report.blobs_walked, 3)
            self.assertGreater(report.bytes_fetched, 0)
            self.assertGreater(report.trees_walked, 0)

    def test_max_blobs_truncates_walk_and_flags_it(self) -> None:
        from arq_validator.record_validator import validate_record
        with tempfile.TemporaryDirectory() as td:
            backend, rec_path, _ = self._make_backup(Path(td))
            report = validate_record(
                backend, rec_path, "secret", max_blobs=2,
            )
            # max_blobs <= total → walk stops with truncated_after
            # set to the cap (or just under).
            self.assertGreater(report.truncated_after, 0)
            self.assertLessEqual(report.blobs_walked, 3)

    def test_corrupted_blob_is_caught(self) -> None:
        from arq_validator.record_validator import validate_record
        with tempfile.TemporaryDirectory() as td:
            backend, rec_path, dst = self._make_backup(Path(td))
            # Find a standardobject blob and flip a byte in its
            # HMAC region. Our writer uses the standalone-object
            # layout when use_packs is False (the default), so the
            # blobs live under <cu>/standardobjects/<shard>/<rest>.
            cu = next(
                p for p in dst.iterdir() if p.is_dir()
            )
            so_dir = cu / "standardobjects"
            self.assertTrue(so_dir.is_dir())
            target = None
            for shard in so_dir.iterdir():
                for blob in shard.iterdir():
                    target = blob
                    break
                if target is not None:
                    break
            self.assertIsNotNone(target, "no blob to corrupt")
            data = bytearray(target.read_bytes())
            # Byte 8 lives in the HMAC region (offset 4..36).
            data[8] ^= 0x01
            target.write_bytes(bytes(data))

            report = validate_record(
                backend, rec_path, "secret",
            )
            self.assertFalse(report.ok)
            kinds = {f.kind for f in report.failures}
            self.assertIn(
                "hmac", kinds,
                f"expected hmac failure, got {kinds!r} "
                f"with errors {[f.error for f in report.failures]}",
            )


if __name__ == "__main__":
    unittest.main()
