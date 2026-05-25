"""Unencrypted backups (`isEncrypted: false`, no keyset, no password).

Matches Arq.app's "Continue Without Encryption" on-disk format
(docs/UNENCRYPTED-FORMAT-RE.md), RE'd 2026-05-25 from a real unencrypted
destination:

- `backupconfig.json` `isEncrypted: false`; no `encryptedkeyset.dat`.
- `backupplan.json` / per-folder `backupfolder.json` = plaintext JSON.
- blobs + backuprecord = `lz4_wrap(plaintext)` with NO ARQO envelope.
- blob_id = SHA-256(plaintext) (no salt, since there is no keyset).
"""

from __future__ import annotations

import hashlib
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path


def _has_openssl() -> bool:
    try:
        subprocess.run(["openssl", "version"], check=True,
                       capture_output=True, timeout=5)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


@unittest.skipUnless(_has_openssl(), "openssl CLI required")
class UnencryptedWriterTests(unittest.TestCase):

    def _build(self, td: Path, **kw):
        from arq_writer.backup import build_backup
        src = td / "src"
        (src / "sub").mkdir(parents=True)
        (src / "a.txt").write_bytes(b"plaintext payload\n")
        (src / "sub" / "b.bin").write_bytes(bytes(range(256)) * 64)
        dest = td / "dest"
        r = build_backup(str(src), str(dest), encryption_password="",
                         encrypt=False, **kw)
        return src, dest, r

    def test_no_keyset_and_isencrypted_false(self) -> None:
        import json
        with tempfile.TemporaryDirectory() as td:
            _src, dest, r = self._build(Path(td))
            cu = dest / r.computer_uuid
            self.assertFalse((cu / "encryptedkeyset.dat").exists())
            cfg = json.loads((cu / "backupconfig.json").read_text())
            self.assertIs(cfg["isEncrypted"], False)
            # sidecar is plaintext JSON (not ARQO)
            self.assertTrue(
                (cu / "backupplan.json").read_bytes().lstrip().startswith(b"{")
            )

    def test_blob_id_is_sha256_of_plaintext_no_salt(self) -> None:
        import lz4.block as lb
        with tempfile.TemporaryDirectory() as td:
            _src, dest, r = self._build(Path(td), use_packs=False)
            cu = dest / r.computer_uuid
            so = next(p for p in (cu / "standardobjects").rglob("*")
                      if p.is_file())
            blob_id = so.parent.name + so.name
            raw = so.read_bytes()
            self.assertNotEqual(raw[:4], b"ARQO")  # no encryption envelope
            n = struct.unpack(">I", raw[:4])[0]    # lz4_wrap length prefix
            plain = lb.decompress(raw[4:], uncompressed_size=n)
            self.assertEqual(hashlib.sha256(plain).hexdigest(), blob_id)

    def test_round_trip_no_password(self) -> None:
        from arq_reader import Restore
        for use_packs in (False, True):
            with tempfile.TemporaryDirectory() as td:
                src, dest, r = self._build(Path(td), use_packs=use_packs,
                                           tree_version=4)
                out = Path(td) / "out"
                out.mkdir()
                # No password — unencrypted destinations need none.
                rs = Restore(str(dest), encryption_password="")
                res = rs.restore(folder_uuid=r.folder_uuid, dest=str(out),
                                 computer_uuid=r.computer_uuid)
                self.assertEqual(res.failures, [])
                self.assertEqual((out / "a.txt").read_bytes(),
                                 (src / "a.txt").read_bytes())
                self.assertEqual((out / "sub" / "b.bin").read_bytes(),
                                 (src / "sub" / "b.bin").read_bytes())


if __name__ == "__main__":
    unittest.main()
