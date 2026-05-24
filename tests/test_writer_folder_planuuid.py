"""Writer default: the top-level destination folder is named by the
planUUID (folder_name == planUUID).

Real Arq 7 always names a backup plan's top-level destination folder
by its planUUID. Verified 2026-05-24 against /Volumes/arqbackup1
(folder ``2DAC24D1-…`` == decrypted ``planUUID``) and confirmed by
Arq.app v8's GUI, which only recognises a destination when this holds.

Our top-level folder is named by ``computer_uuid``, so the writer must
default ``computer_uuid`` and ``plan_uuid`` to a single shared value.
These tests pin that coupling — both the in-memory default and the
on-disk + decrypted invariant.
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


class WriterUUIDCouplingTests(unittest.TestCase):
    """In-memory default coupling — no crypto / disk required."""

    def test_default_couples_computer_and_plan_uuid(self) -> None:
        from arq_writer.backup import Backup
        b = Backup(dest_root="/tmp/unused", encryption_password="pw")
        self.assertEqual(
            b.computer_uuid, b.plan_uuid,
            "default computer_uuid must equal plan_uuid (Arq layout)",
        )

    def test_only_plan_uuid_given_computer_follows(self) -> None:
        from arq_writer.backup import Backup
        b = Backup(
            dest_root="/tmp/unused", encryption_password="pw",
            plan_uuid="AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA",
        )
        self.assertEqual(b.computer_uuid, b.plan_uuid)

    def test_only_computer_uuid_given_plan_follows(self) -> None:
        from arq_writer.backup import Backup
        b = Backup(
            dest_root="/tmp/unused", encryption_password="pw",
            computer_uuid="BBBBBBBB-BBBB-BBBB-BBBB-BBBBBBBBBBBB",
        )
        self.assertEqual(b.plan_uuid, b.computer_uuid)

    def test_both_given_distinct_values_honoured(self) -> None:
        """Explicit distinct values are an override and must be kept."""
        from arq_writer.backup import Backup
        b = Backup(
            dest_root="/tmp/unused", encryption_password="pw",
            computer_uuid="CCCCCCCC-CCCC-CCCC-CCCC-CCCCCCCCCCCC",
            plan_uuid="DDDDDDDD-DDDD-DDDD-DDDD-DDDDDDDDDDDD",
        )
        self.assertNotEqual(b.computer_uuid, b.plan_uuid)


@unittest.skipUnless(_has_openssl(), "openssl CLI required")
class WriterFolderEqualsPlanUUIDTests(unittest.TestCase):
    """End-to-end: on-disk top-level folder name == decrypted planUUID."""

    def test_ondisk_folder_name_equals_decrypted_plan_uuid(self) -> None:
        from arq_writer.backup import build_backup
        from arq_validator.backend import LocalBackend
        from arq_validator.crypto import decrypt_keyset
        from arq_validator.sidecar import read_sidecar
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "f.txt").write_bytes(b"x")
            dest = tdp / "dest"
            r = build_backup(
                str(src), str(dest), encryption_password="pw",
            )
            folder = dest / r.computer_uuid
            keyset = decrypt_keyset(
                (folder / "encryptedkeyset.dat").read_bytes(), "pw",
            )
            plan = read_sidecar(
                LocalBackend(str(folder)), "backupplan.json", keyset,
            )
            self.assertIsNotNone(plan, "backupplan.json failed to decrypt")
            self.assertEqual(
                folder.name, plan["planUUID"],
                "top-level destination folder must be named by planUUID",
            )


if __name__ == "__main__":
    unittest.main()
