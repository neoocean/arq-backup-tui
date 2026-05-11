"""B4 — isEncrypted=false legacy read support.

Some Arq 5 / 6 destinations were configured with
``isEncrypted: false`` in ``backupconfig.json``. The blobs on
disk are then plain content (typically still LZ4-wrapped) with
NO ``ARQO`` magic prefix. The reader's restore path supports
this via the magic-byte check at
``arq_reader/restore.py::_fetch_blob`` ~line 356:

    if raw[:4] == b"ARQO":
        raw = decrypt_encrypted_object(...)

When the bytes don't start with ARQO, the decrypt step is
skipped and the raw bytes are taken as the plaintext.

This module pins the unencrypted-read path:

- Blob without ARQO magic + ``compressionType=0`` → returned
  as-is
- Blob without ARQO magic + ``compressionType=2`` (LZ4) →
  decompressed and returned
- The writer always emits encrypted backups (intentional
  project scope per ``HANDOFF.md``); this is verified
  indirectly by confirming any blob the writer emits starts
  with ARQO magic

The writer's "always encrypts" stance is deliberate scope —
unencrypted backups are a legacy compat concern, not something
new operators should be producing. The reader supports them so
operators with old destinations can still restore.
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


class WriterAlwaysEmitsEncryptedTests(unittest.TestCase):
    """The writer hard-codes ``is_encrypted=True`` in 3 places
    (``backupconfig.json``, ``backupplan.json``, the BackupRecord
    plist). Verify every emitted blob starts with ``ARQO`` magic
    — the "always encrypts" stance documented in HANDOFF.md."""

    @unittest.skipUnless(_has_openssl(), "openssl CLI required")
    def test_every_emitted_blob_starts_with_arqo_magic(self) -> None:
        from arq_writer.backup import build_backup
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "a.txt").write_bytes(b"alpha")
            (src / "b.bin").write_bytes(b"\x00" * 1024)
            dest = tdp / "dest"
            res = build_backup(
                str(src), str(dest), encryption_password="pw",
            )
            so_root = dest / res.computer_uuid / "standardobjects"
            checked = 0
            for shard in so_root.iterdir():
                for blob_path in shard.iterdir():
                    raw = blob_path.read_bytes()
                    self.assertEqual(
                        raw[:4], b"ARQO",
                        f"blob {blob_path.name} doesn't start with "
                        f"ARQO — writer emitted unencrypted bytes?",
                    )
                    checked += 1
            self.assertGreater(checked, 0)


class ReaderUnencryptedBlobPathTests(unittest.TestCase):
    """``_fetch_blob`` skips decrypt when input lacks ARQO magic.
    Verified by feeding synthetic plain bytes via a custom
    backend mock."""

    def _make_loc(
        self, *, rel_path: str = "/plain.bin",
        offset: int = 0, length: int = 0,
        compression_type: int = 0,
    ):
        from arq_writer.types import BlobLoc
        return BlobLoc(
            blobIdentifier="x" * 64,
            isPacked=False,
            relativePath=rel_path,
            offset=offset,
            length=length,
            stretchEncryptionKey=True,
            compressionType=compression_type,
        )

    def _fetch_with_backend(self, loc, backend_data: bytes):
        """Wire a minimal mock backend that returns ``backend_data``
        from read_all(loc.relativePath), then invoke _fetch_blob."""
        from unittest.mock import MagicMock
        from arq_reader.restore import Restore
        from arq_writer.types import BlobLoc

        backend = MagicMock()
        backend.read_all.return_value = backend_data
        backend.read_range.return_value = backend_data

        # The Restore ctor expects a directory; we just need an
        # instance to call _fetch_blob on, but its full init does
        # backend discovery. Bypass with the canonical _fetch_blob
        # signature directly.
        with tempfile.TemporaryDirectory() as td:
            # Build a real but empty destination so Restore
            # initialises cleanly, then mutate its backend.
            from arq_writer.backup import build_backup
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "a").write_bytes(b"x")
            build_backup(
                str(tdp / "src"), str(tdp / "dest"),
                encryption_password="pw",
            )
            rs = Restore(
                str(tdp / "dest"), encryption_password="pw",
            )
            keyset = rs.keyset(rs.layouts()[0].computer_uuid)
            # Swap in mock backend just for this call.
            rs.backend = backend
            return rs._fetch_blob(loc, keyset)

    @unittest.skipUnless(_has_openssl(), "openssl CLI required")
    def test_unencrypted_uncompressed_blob_passes_through(
        self,
    ) -> None:
        plain = b"this is literal file content\n"
        # Synthetic loc: no ARQO, compression_type=0.
        loc = self._make_loc(compression_type=0)
        result = self._fetch_with_backend(loc, plain)
        self.assertEqual(
            result, plain,
            "unencrypted+uncompressed blob should pass through "
            "_fetch_blob unchanged",
        )

    @unittest.skipUnless(_has_openssl(), "openssl CLI required")
    def test_unencrypted_lz4_compressed_blob_decompresses(self) -> None:
        from arq_writer.lz4_block import lz4_wrap
        plain = b"this is literal LZ4-wrapped content\n" * 50
        wrapped = lz4_wrap(plain)
        # Sanity: lz4-wrapped doesn't start with ARQO
        self.assertNotEqual(wrapped[:4], b"ARQO")
        loc = self._make_loc(compression_type=2)
        result = self._fetch_with_backend(loc, wrapped)
        self.assertEqual(result, plain)


if __name__ == "__main__":
    unittest.main()
