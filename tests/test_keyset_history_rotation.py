"""``rotate_keyset_password_on_disk`` — Arq-faithful keyset rotation.

Arq.app v8 archives the superseded keyset to
``<cu>/keyset_history/encryptedkeyset_<unix-epoch>.dat`` on a password
change (verified 2026-05-24 against a real destination + an Arq.app GUI
password change). These tests pin that the on-disk rotation helper:

- writes the new password to the live ``encryptedkeyset.dat``,
- archives the OLD keyset to ``keyset_history/`` under the epoch name,
- leaves the archived copy unlockable with the OLD password,
- keeps the master keys intact (data still restores under the new pw),
- and skips archival when ``archive_old=False``.
"""

from __future__ import annotations

import subprocess
import tempfile
import time
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
class KeysetHistoryRotationTests(unittest.TestCase):

    def _make_dest(self, td: Path):
        from arq_writer.backup import build_backup
        src = td / "src"
        src.mkdir()
        (src / "f.txt").write_bytes(b"keyset-history rotation payload")
        dest = td / "dest"
        r = build_backup(str(src), str(dest), encryption_password="old")
        return src, dest, r

    def test_rotation_archives_old_keyset_to_history(self) -> None:
        from arq_writer import rotate_keyset_password_on_disk
        from arq_validator.backend import LocalBackend
        from arq_validator.crypto import decrypt_keyset, CryptoError
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            _src, dest, r = self._make_dest(tdp)
            cu = r.computer_uuid
            before = int(time.time())
            rotate_keyset_password_on_disk(
                LocalBackend(str(dest)), cu,
                old_password="old", new_password="new",
            )
            after = int(time.time()) + 1

            # 1. history dir + a single epoch-named archive exist
            hist = dest / cu / "keyset_history"
            archives = sorted(hist.glob("encryptedkeyset_*.dat"))
            self.assertEqual(len(archives), 1, "expected one archived keyset")
            name = archives[0].name
            self.assertRegex(name, r"^encryptedkeyset_\d+\.dat$")
            epoch = int(name[len("encryptedkeyset_"):-len(".dat")])
            self.assertGreaterEqual(epoch, before)
            self.assertLessEqual(epoch, after)

            # 2. live keyset: new pw unlocks, old rejected
            live = (dest / cu / "encryptedkeyset.dat").read_bytes()
            self.assertIsNotNone(decrypt_keyset(live, "new"))
            with self.assertRaises(CryptoError):
                decrypt_keyset(live, "old")

            # 3. archived keyset: still unlockable with the OLD pw
            archived = archives[0].read_bytes()
            self.assertIsNotNone(decrypt_keyset(archived, "old"))

    def test_master_keys_preserved_data_restores_under_new_pw(self) -> None:
        from arq_writer import rotate_keyset_password_on_disk
        from arq_validator.backend import LocalBackend
        from arq_reader import Restore
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src, dest, r = self._make_dest(tdp)
            rotate_keyset_password_on_disk(
                LocalBackend(str(dest)), r.computer_uuid,
                old_password="old", new_password="new",
            )
            out = tdp / "restored"
            out.mkdir()
            rs = Restore(str(dest), encryption_password="new")
            res = rs.restore(folder_uuid=r.folder_uuid, dest=str(out))
            self.assertEqual(res.failures, [])
            restored = (out / "f.txt").read_bytes()
            self.assertEqual(restored, b"keyset-history rotation payload")

    def test_archive_old_false_skips_history(self) -> None:
        from arq_writer import rotate_keyset_password_on_disk
        from arq_validator.backend import LocalBackend
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            _src, dest, r = self._make_dest(tdp)
            rotate_keyset_password_on_disk(
                LocalBackend(str(dest)), r.computer_uuid,
                old_password="old", new_password="new",
                archive_old=False,
            )
            self.assertFalse(
                (dest / r.computer_uuid / "keyset_history").exists(),
                "keyset_history must not be created when archive_old=False",
            )


if __name__ == "__main__":
    unittest.main()
