"""B1 — blobIdentifierType (SHA-1 = 1, SHA-256 = 2) handling.

``backupconfig.json::blobIdentifierType`` records which hash
algorithm produced the blob filenames:

- ``1`` — SHA-1 (Arq 5 era, 40 hex char filenames)
- ``2`` — SHA-256 (Arq 7 default, 64 hex char filenames)

Validator's SV2 check enforces the value-set; downstream code
that walks blob filenames must know to use the right hex
length. This module pins:

- SV2 accepts both 1 and 2
- SV2 rejects unknown / future values (3, 0, string)
- ``compute_blob_id`` always emits SHA-256 (writer-side
  alignment with current default)
- Standalone-object filename hex length matches the
  blobIdentifierType the writer announces (62 hex chars after
  shard, since shard is 2 chars + 62 = 64 total for SHA-256)
"""

from __future__ import annotations

import hashlib
import json
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
class BlobIdTypeValidatorTests(unittest.TestCase):
    """SV2 invariant: blobIdentifierType ∈ {1, 2}."""

    def _build_then_patch(self, td: Path, new_value):
        from arq_writer.backup import build_backup
        src = td / "src"
        src.mkdir()
        (src / "f").write_bytes(b"x")
        dest = td / "dest"
        res = build_backup(
            str(src), str(dest), encryption_password="pw",
        )
        cfg_path = (
            dest / res.computer_uuid / "backupconfig.json"
        )
        cfg = json.loads(cfg_path.read_text())
        cfg["blobIdentifierType"] = new_value
        cfg_path.write_text(json.dumps(cfg))
        return dest

    def _sv2_status(self, dest):
        from arq_validator import (
            LocalBackend, check_arq7_compatibility,
        )
        report = check_arq7_compatibility(
            LocalBackend(str(dest)),
            "/", encryption_password="pw",
        )
        return [c for c in report.checks if c.id == "SV2"][0]

    def test_sha1_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            dest = self._build_then_patch(Path(td), 1)
            self.assertTrue(self._sv2_status(dest).passed)

    def test_sha256_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            dest = self._build_then_patch(Path(td), 2)
            self.assertTrue(self._sv2_status(dest).passed)

    def test_unknown_value_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            dest = self._build_then_patch(Path(td), 3)
            self.assertFalse(self._sv2_status(dest).passed)

    def test_zero_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            dest = self._build_then_patch(Path(td), 0)
            self.assertFalse(self._sv2_status(dest).passed)

    def test_string_value_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            dest = self._build_then_patch(Path(td), "2")
            self.assertFalse(self._sv2_status(dest).passed)


@unittest.skipUnless(_has_openssl(), "openssl CLI required")
class WriterSHA256AlignmentTests(unittest.TestCase):
    """The writer emits SHA-256 blob_ids by default. Verify the
    blob filenames have 64-hex-char SHA-256 length (shard 2 +
    rest 62)."""

    def test_blob_filename_lengths_match_sha256(self) -> None:
        from arq_writer.backup import build_backup
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "a.txt").write_bytes(b"alpha")
            (src / "b.txt").write_bytes(b"bravo")
            dest = tdp / "dest"
            res = build_backup(
                str(src), str(dest), encryption_password="pw",
            )
            so_root = dest / res.computer_uuid / "standardobjects"
            for shard in so_root.iterdir():
                self.assertEqual(
                    len(shard.name), 2,
                    f"shard name '{shard.name}' isn't 2 chars",
                )
                for blob_path in shard.iterdir():
                    self.assertEqual(
                        len(blob_path.name), 62,
                        f"blob filename '{blob_path.name}' isn't "
                        f"62 chars (shard 2 + 62 = 64 = SHA-256)",
                    )
                    # All lowercase hex.
                    self.assertTrue(
                        all(
                            c in "0123456789abcdef"
                            for c in blob_path.name
                        ),
                        f"blob filename has non-hex char: "
                        f"{blob_path.name!r}",
                    )

    def test_compute_blob_id_is_sha256(self) -> None:
        """compute_blob_id(salt, plaintext) = SHA-256(salt ‖ plaintext).
        Pin the algorithm choice — switching to SHA-1 would break
        every existing destination."""
        from arq_writer.crypto_write import compute_blob_id
        salt = b"S" * 32
        plain = b"test-content"
        computed = compute_blob_id(salt, plain)
        expected = hashlib.sha256(salt + plain).hexdigest()
        self.assertEqual(computed, expected)
        self.assertEqual(len(computed), 64)


if __name__ == "__main__":
    unittest.main()
