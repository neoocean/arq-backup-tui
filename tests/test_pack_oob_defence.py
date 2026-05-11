"""A4 — pack file out-of-bounds defence.

The reader trusts ``BlobLoc.offset`` + ``BlobLoc.length`` to
slice a blob out of its pack file. If a record's BlobLoc was
corrupted (deliberately or by disk error) to claim a range that
extends past EOF, the reader must NOT:

- Read out-of-bounds bytes (no buffer over-read)
- Silently return partial / wrong content (no false-success)
- Crash on internal state (no AttributeError / IndexError leaks)

The defence is layered:

1. **Backend bounds**: ``LocalBackend.read_range`` rejects
   negative offset/length and returns short on EOF (no crash).
2. **HMAC catches truncation**: a truncated read produces an
   ARQO that fails HMAC verify → graceful error from
   ``decrypt_encrypted_object``.
3. **No silent decode**: even if HMAC were somehow bypassed,
   AES-CBC + LZ4 decompression each reject malformed input
   independently.

This module pins the chain end-to-end. We synthesize a corrupted
BlobLoc and verify the reader produces a clean error rather than
a partial result or crash.
"""

from __future__ import annotations

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
class BackendBoundsTests(unittest.TestCase):
    """Direct ``LocalBackend.read_range`` bounds behaviour."""

    def test_negative_offset_raises(self) -> None:
        from arq_validator import LocalBackend
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            (tdp / "f").write_bytes(b"hello")
            with self.assertRaises(ValueError):
                LocalBackend(str(tdp)).read_range("/f", -1, 5)

    def test_negative_length_raises(self) -> None:
        from arq_validator import LocalBackend
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            (tdp / "f").write_bytes(b"hello")
            with self.assertRaises(ValueError):
                LocalBackend(str(tdp)).read_range("/f", 0, -1)

    def test_zero_length_returns_empty(self) -> None:
        from arq_validator import LocalBackend
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            (tdp / "f").write_bytes(b"hello")
            self.assertEqual(
                LocalBackend(str(tdp)).read_range("/f", 0, 0),
                b"",
            )

    def test_offset_past_eof_returns_empty(self) -> None:
        """No crash on out-of-bounds offset — backend returns the
        empty short-read. Downstream HMAC catches the truncation."""
        from arq_validator import LocalBackend
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            (tdp / "f").write_bytes(b"hello")
            result = LocalBackend(str(tdp)).read_range(
                "/f", 1000, 100,
            )
            # File.seek + read past EOF returns empty bytes, no crash.
            self.assertEqual(result, b"")

    def test_overlapping_eof_returns_short_read(self) -> None:
        """Range partially past EOF returns whatever's available
        — short read, no crash."""
        from arq_validator import LocalBackend
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            (tdp / "f").write_bytes(b"hello")
            result = LocalBackend(str(tdp)).read_range(
                "/f", 3, 100,
            )
            self.assertEqual(result, b"lo")


@unittest.skipUnless(_has_openssl(), "openssl CLI required")
class CorruptedBlobLocReaderDefenceTests(unittest.TestCase):
    """End-to-end: simulate a corrupted BlobLoc and verify the
    reader fails clean.

    We synthesize the corruption by editing a backup's pack file
    in-place (truncating it) and then attempting restore — the
    reader's read_range returns short, decrypt fails HMAC,
    restore reports the failure."""

    def _build_packed_backup(self, td: Path):
        from arq_writer.backup import build_backup
        src = td / "src"
        src.mkdir()
        (src / "a.txt").write_bytes(b"alpha content " * 50)
        (src / "b.txt").write_bytes(b"bravo content " * 50)
        res = build_backup(
            str(src), str(td / "dest"),
            encryption_password="pw",
            use_packs=True,
        )
        return res

    def test_truncated_pack_file_produces_clean_restore_failure(
        self,
    ) -> None:
        """Truncate a pack file by 1000 bytes mid-blob and confirm
        the reader's restore fails with a recognised error
        (not a crash, not silent partial content)."""
        from arq_reader import Restore
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            res = self._build_packed_backup(tdp)
            dest = tdp / "dest"
            # Find a blob pack file. Tree packs would have the
            # same issue but truncating a data pack lets us
            # observe the failure on file content (more direct).
            bp_dir = dest / res.computer_uuid / "blobpacks"
            self.assertTrue(bp_dir.is_dir())
            packs = list(bp_dir.rglob("*.pack"))
            self.assertGreater(len(packs), 0)
            # Truncate one pack file by 1 KB. The blob it held
            # gets a malformed read on restore.
            pack = packs[0]
            data = pack.read_bytes()
            truncated = data[:max(0, len(data) - 1024)]
            pack.write_bytes(truncated)
            # Now attempt restore. The reader should detect the
            # corruption + fail gracefully.
            out = tdp / "out"
            out.mkdir()
            rs = Restore(str(dest), encryption_password="pw")
            layouts = rs.layouts()
            # Either restore raises a recognised error, OR it
            # produces a result with .failures populated. Both
            # are acceptable graceful failures.
            error_seen = False
            try:
                result = rs.restore(
                    folder_uuid=layouts[0].backup_folder_uuids[0],
                    computer_uuid=layouts[0].computer_uuid,
                    dest=out,
                )
                failures = getattr(result, "failures", None) or []
                if failures:
                    error_seen = True
            except Exception as exc:
                # Must NOT be an internal-state crash.
                self.assertNotIn(
                    type(exc).__name__,
                    ("AttributeError", "TypeError"),
                    f"restore on truncated pack crashed via "
                    f"internal error: {exc!r}",
                )
                error_seen = True
            self.assertTrue(
                error_seen,
                "restore on truncated pack should have flagged "
                "an error or returned failures; got silent "
                "success — possible silent-corruption regression",
            )


if __name__ == "__main__":
    unittest.main()
