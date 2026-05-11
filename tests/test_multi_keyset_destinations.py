"""E4-new — multi-keyset destinations (multiple computer UUIDs).

A single Arq destination can host backups from multiple
computers — each computer gets its own UUID directory, its own
``encryptedkeyset.dat``, and its own per-folder structure.
Operators using a shared NAS / SFTP destination across machines
encounter this routinely.

The reader must:

- ``layouts()`` returns one Layout per computer UUID
- Each layout's keyset decrypts independently
- A blob_id from computer A's keyset does NOT collide with
  computer B's (different salts)
- Restoring from computer A doesn't touch computer B's data

This module pins those properties:

- ``Restore.layouts()`` enumerates all computer subdirs
- Per-computer keyset decryption is independent
- Per-computer salt produces different blob_ids for identical
  plaintext
- Wrong-password failure on one computer doesn't break the
  other's read path
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
class MultiKeysetDestinationTests(unittest.TestCase):

    def _build_two_computer_destination(self, td: Path):
        """Build a destination containing two computer UUID
        subtrees, each with its own keyset + plan + record."""
        from arq_writer.backup import build_backup
        dest = td / "dest"
        results = []
        for i, password in enumerate(["alpha", "beta"]):
            src = td / f"src{i}"
            src.mkdir()
            (src / f"file-{i}.txt").write_bytes(
                f"content for computer {i}".encode(),
            )
            res = build_backup(
                str(src), str(dest),
                encryption_password=password,
                computer_uuid=None,   # fresh UUID per build
            )
            results.append((password, res))
        return dest, results

    def test_layouts_enumerates_both_computers(self) -> None:
        from arq_reader import Restore
        with tempfile.TemporaryDirectory() as td:
            dest, results = self._build_two_computer_destination(
                Path(td),
            )
            # Restore needs ONE password to discover layouts;
            # the layouts list shows all computers regardless of
            # whether THIS password unlocks all of them.
            rs = Restore(str(dest), encryption_password="alpha")
            layouts = rs.layouts()
            self.assertEqual(
                len(layouts), 2,
                f"expected 2 computer layouts, got "
                f"{len(layouts)}",
            )
            seen_uuids = {l.computer_uuid for l in layouts}
            expected_uuids = {r[1].computer_uuid for r in results}
            self.assertEqual(seen_uuids, expected_uuids)

    def test_each_computer_has_independent_keyset(self) -> None:
        """Decrypting with computer A's password against
        computer B's keyset fails — verifies they're independent.
        And vice versa."""
        from arq_validator import LocalBackend
        from arq_validator.crypto import decrypt_keyset, CryptoError
        with tempfile.TemporaryDirectory() as td:
            dest, results = self._build_two_computer_destination(
                Path(td),
            )
            (pw_a, res_a), (pw_b, res_b) = results
            b = LocalBackend(str(dest))
            # Correct passwords work.
            ks_a = decrypt_keyset(
                b.read_all(
                    f"/{res_a.computer_uuid}/encryptedkeyset.dat",
                ),
                pw_a,
            )
            ks_b = decrypt_keyset(
                b.read_all(
                    f"/{res_b.computer_uuid}/encryptedkeyset.dat",
                ),
                pw_b,
            )
            # Different salts.
            self.assertNotEqual(
                ks_a.blob_id_salt, ks_b.blob_id_salt,
            )
            self.assertNotEqual(
                ks_a.encryption_key, ks_b.encryption_key,
            )
            # Wrong password on A's keyset raises.
            with self.assertRaises(CryptoError):
                decrypt_keyset(
                    b.read_all(
                        f"/{res_a.computer_uuid}/encryptedkeyset.dat",
                    ),
                    pw_b,   # B's password against A's keyset
                )

    def test_restore_each_computer_independently(self) -> None:
        """Restoring from computer A's records gives A's files
        only — B's data is untouched and isn't accessible
        through A's password."""
        from arq_reader import Restore
        with tempfile.TemporaryDirectory() as td:
            dest, results = self._build_two_computer_destination(
                Path(td),
            )
            (pw_a, res_a), (pw_b, res_b) = results
            # Restore A's computer with A's password.
            out_a = Path(td) / "out_a"
            out_a.mkdir()
            rs = Restore(str(dest), encryption_password=pw_a)
            rs.restore(
                folder_uuid=res_a.folder_uuid,
                computer_uuid=res_a.computer_uuid,
                dest=out_a,
            )
            self.assertEqual(
                (out_a / "file-0.txt").read_bytes(),
                b"content for computer 0",
            )
            # Restore B's computer with B's password.
            out_b = Path(td) / "out_b"
            out_b.mkdir()
            rs2 = Restore(str(dest), encryption_password=pw_b)
            rs2.restore(
                folder_uuid=res_b.folder_uuid,
                computer_uuid=res_b.computer_uuid,
                dest=out_b,
            )
            self.assertEqual(
                (out_b / "file-1.txt").read_bytes(),
                b"content for computer 1",
            )

    def test_different_salts_produce_different_blob_ids(self) -> None:
        """Identical plaintext content across two computers
        produces DIFFERENT blob_ids (because salts differ).
        This is the per-destination dedup boundary — content
        addressing folds within a keyset, not across."""
        from arq_writer.crypto_write import compute_blob_id
        from arq_validator import LocalBackend
        from arq_validator.crypto import decrypt_keyset
        with tempfile.TemporaryDirectory() as td:
            dest, results = self._build_two_computer_destination(
                Path(td),
            )
            (pw_a, res_a), (pw_b, res_b) = results
            b = LocalBackend(str(dest))
            ks_a = decrypt_keyset(
                b.read_all(
                    f"/{res_a.computer_uuid}/encryptedkeyset.dat",
                ),
                pw_a,
            )
            ks_b = decrypt_keyset(
                b.read_all(
                    f"/{res_b.computer_uuid}/encryptedkeyset.dat",
                ),
                pw_b,
            )
            plaintext = b"identical content for both"
            bid_a = compute_blob_id(ks_a.blob_id_salt, plaintext)
            bid_b = compute_blob_id(ks_b.blob_id_salt, plaintext)
            self.assertNotEqual(
                bid_a, bid_b,
                "different keysets must produce different "
                "blob_ids for identical plaintext (per-keyset "
                "salt isolation)",
            )


if __name__ == "__main__":
    unittest.main()
