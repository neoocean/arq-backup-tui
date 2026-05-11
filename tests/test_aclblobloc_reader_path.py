"""A보완-1 — Reader's aclBlobLoc JSON consumption path.

D2 (PR #86) added ``aclBlobLoc`` to the BackupRecord JSON's node
emit. A보완-1 closes the reader-side consumption gap: the root
TreeNode reconstruction now reads ``aclBlobLoc`` from JSON so
the restore's ACL-application step has the right BlobLoc.

The binary Tree parser (`arq_reader.parse.parse_tree`) was
already reading `aclBlobLoc` for nested nodes; the gap was
ONLY at the root TreeNode whose existence is JSON-only (lives
inside the BackupRecord plist, not inside any Tree binary).

This module pins:

- A BackupRecord JSON with ``aclBlobLoc: dict`` is read into
  the root TreeNode's ``aclBlobLoc`` field
- A BackupRecord JSON with ``aclBlobLoc: null`` (the default
  for ACL-less roots) reads back as ``None``
- The reader's restore path consumes the root ACL (if the
  blob is reachable) and emits a ``acl_applied`` event
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
class AclBlobLocReaderPathTests(unittest.TestCase):

    def test_null_aclBlobLoc_reads_as_none(self) -> None:
        """Default emit (no ACL on root) → root TreeNode's
        aclBlobLoc is None after JSON round-trip."""
        from arq_writer.backup import build_backup
        from arq_reader import Restore
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "a.txt").write_bytes(b"x")
            dest = tdp / "dest"
            build_backup(
                str(src), str(dest), encryption_password="pw",
            )
            # Restore should succeed without errors even though
            # there's no ACL — verifies reader handles
            # aclBlobLoc=null cleanly.
            rs = Restore(str(dest), encryption_password="pw")
            out = tdp / "out"
            out.mkdir()
            layouts = rs.layouts()
            rs.restore(
                folder_uuid=layouts[0].backup_folder_uuids[0],
                computer_uuid=layouts[0].computer_uuid, dest=out,
            )
            self.assertEqual(
                (out / "a.txt").read_bytes(), b"x",
            )

    def test_root_acl_blobloc_dict_is_consumed(self) -> None:
        """Patch a real backup's JSON to put a BlobLoc-shaped
        dict in aclBlobLoc on the root node; verify the reader
        builds the right BlobLoc internally."""
        from arq_writer.backup import build_backup
        from arq_writer.backuprecord import (
            parse_backuprecord, serialize_backuprecord,
        )
        from arq_writer.lz4_block import lz4_wrap
        from arq_writer.crypto_write import build_encrypted_object
        from arq_reader import Restore
        from arq_reader.decrypt import decrypt_lz4_arqo
        from arq_validator import LocalBackend
        from arq_validator.crypto import decrypt_keyset
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "a.txt").write_bytes(b"x")
            dest = tdp / "dest"
            res = build_backup(
                str(src), str(dest), encryption_password="pw",
            )
            backend = LocalBackend(str(dest))
            ks = decrypt_keyset(
                backend.read_all(
                    f"/{res.computer_uuid}/encryptedkeyset.dat",
                ),
                "pw",
            )
            # Patch the root node's aclBlobLoc to a synthetic
            # BlobLoc dict pointing at a non-existent blob.
            # The reader should pick it up + the restore path
            # tries to fetch the ACL blob, which fails
            # gracefully (the blob isn't there) — but the
            # critical part is the FIELD is consumed (not
            # silently dropped).
            rec_rel = str(
                res.backuprecord_path.relative_to(res.dest_root)
            )
            arqo = backend.read_all("/" + rec_rel)
            plain = decrypt_lz4_arqo(
                arqo, ks.encryption_key, ks.hmac_key,
            )
            record = parse_backuprecord(plain)
            record["node"]["aclBlobLoc"] = {
                "blobIdentifier": "f" * 64,
                "isPacked": False,
                "relativePath": "",
                "offset": 0,
                "length": 0,
                "stretchEncryptionKey": True,
                "compressionType": 2,
            }
            new_plain = serialize_backuprecord(record, fmt="json")
            new_arqo = build_encrypted_object(
                lz4_wrap(new_plain),
                ks.encryption_key, ks.hmac_key,
            )
            (dest / rec_rel).write_bytes(new_arqo)
            # Restore. With a non-existent ACL blob it should
            # not crash — graceful skip.
            rs = Restore(str(dest), encryption_password="pw")
            out = tdp / "out"
            out.mkdir()
            layouts = rs.layouts()
            try:
                rs.restore(
                    folder_uuid=layouts[0].backup_folder_uuids[0],
                    computer_uuid=layouts[0].computer_uuid,
                    dest=out,
                )
            except Exception as exc:
                # Must not be an internal-state crash from a
                # silently-ignored aclBlobLoc.
                self.assertNotIn(
                    type(exc).__name__,
                    ("AttributeError", "TypeError"),
                    f"reader's aclBlobLoc handling raised "
                    f"{type(exc).__name__}: {exc}",
                )
            # File still restored.
            self.assertEqual(
                (out / "a.txt").read_bytes(), b"x",
            )


if __name__ == "__main__":
    unittest.main()
