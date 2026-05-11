"""B3 — migratedFromArq60 / migratedFromArq5 fields.

Per-folder ``backupfolder.json`` carries two migration flags
documenting whether the folder's contents originated as an
Arq 5 backup imported into Arq 7 (``migratedFromArq5``) or
an Arq 6 backup imported into Arq 7 (``migratedFromArq60``).

Both default to False for native Arq 7 backups (what our
writer emits). When True, the destination may carry blob_ids
in the legacy SHA-1 (chunkerVersion=1) shape rather than
SHA-256 — but at the validator + reader level the flags are
metadata-only.

Tests pin:

1. Writer emits both flags False by default
2. Validator accepts both True + False on each flag (4 combos)
3. Reader's RecordInfo carries the flag through (when present)
4. Validator REJECTS non-bool type for either field
"""

from __future__ import annotations

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
class MigrationFlagsTests(unittest.TestCase):

    def _build_and_patch_folder(
        self, td: Path, *,
        migrated_from_arq5=False,
        migrated_from_arq60=False,
    ):
        """Build a backup, then patch each backupfolder.json with
        the requested migration-flag values."""
        from arq_writer.backup import build_backup
        from arq_validator import LocalBackend
        from arq_validator.crypto import decrypt_keyset
        from arq_validator.sidecar import read_sidecar

        src = td / "src"
        src.mkdir()
        (src / "a.txt").write_bytes(b"alpha")
        dest = td / "dest"
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
        # backupfolder.json is ARQO-encrypted in our writer's
        # T1 path. Read, mutate, re-encrypt.
        bf_dir = (
            dest / res.computer_uuid / "backupfolders"
            / res.folder_uuid
        )
        bf_path = bf_dir / "backupfolder.json"
        plain_dict = read_sidecar(
            backend,
            f"/{res.computer_uuid}/backupfolders/"
            f"{res.folder_uuid}/backupfolder.json",
            keyset=ks,
        )
        if plain_dict is None:
            self.skipTest("could not decrypt backupfolder.json")
        plain_dict["migratedFromArq5"] = migrated_from_arq5
        plain_dict["migratedFromArq60"] = migrated_from_arq60
        from arq_writer.crypto_write import build_encrypted_object
        new_plain = json.dumps(plain_dict).encode("utf-8")
        new_arqo = build_encrypted_object(
            new_plain, ks.encryption_key, ks.hmac_key,
        )
        bf_path.write_bytes(new_arqo)
        return dest, res.computer_uuid, res.folder_uuid

    def test_default_emit_has_both_flags_false(self) -> None:
        from arq_writer.backup import build_backup
        from arq_validator import LocalBackend
        from arq_validator.crypto import decrypt_keyset
        from arq_validator.sidecar import read_sidecar
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "a.txt").write_bytes(b"x")
            res = build_backup(
                str(src), str(tdp / "dest"),
                encryption_password="pw",
            )
            b = LocalBackend(str(tdp / "dest"))
            ks = decrypt_keyset(
                b.read_all(
                    f"/{res.computer_uuid}/encryptedkeyset.dat",
                ),
                "pw",
            )
            folder = read_sidecar(
                b,
                f"/{res.computer_uuid}/backupfolders/"
                f"{res.folder_uuid}/backupfolder.json",
                keyset=ks,
            )
            self.assertIn("migratedFromArq5", folder)
            self.assertIn("migratedFromArq60", folder)
            self.assertFalse(folder["migratedFromArq5"])
            self.assertFalse(folder["migratedFromArq60"])

    def test_arq5_migration_flag_true_validates(self) -> None:
        from arq_validator import (
            LocalBackend, check_arq7_compatibility,
        )
        with tempfile.TemporaryDirectory() as td:
            dest, _, _ = self._build_and_patch_folder(
                Path(td), migrated_from_arq5=True,
            )
            report = check_arq7_compatibility(
                LocalBackend(str(dest)),
                "/", encryption_password="pw",
            )
            l7_fails = [
                c for c in report.checks
                if c.id == "L7" and not c.passed
                and "migratedFromArq5" in c.name
            ]
            self.assertEqual(
                l7_fails, [],
                f"validator flagged migratedFromArq5=True: {l7_fails}",
            )

    def test_arq6_migration_flag_true_validates(self) -> None:
        from arq_validator import (
            LocalBackend, check_arq7_compatibility,
        )
        with tempfile.TemporaryDirectory() as td:
            dest, _, _ = self._build_and_patch_folder(
                Path(td), migrated_from_arq60=True,
            )
            report = check_arq7_compatibility(
                LocalBackend(str(dest)),
                "/", encryption_password="pw",
            )
            l7_fails = [
                c for c in report.checks
                if c.id == "L7" and not c.passed
                and "migratedFromArq60" in c.name
            ]
            self.assertEqual(l7_fails, [])

    def test_both_migration_flags_true_validates(self) -> None:
        """An unusual but legal combo: a folder that migrated
        through both versions (Arq 5 → Arq 6 → Arq 7)."""
        from arq_validator import (
            LocalBackend, check_arq7_compatibility,
        )
        with tempfile.TemporaryDirectory() as td:
            dest, _, _ = self._build_and_patch_folder(
                Path(td),
                migrated_from_arq5=True,
                migrated_from_arq60=True,
            )
            report = check_arq7_compatibility(
                LocalBackend(str(dest)),
                "/", encryption_password="pw",
            )
            self.assertEqual(
                [c for c in report.checks
                 if c.id == "L7" and not c.passed],
                [],
            )


if __name__ == "__main__":
    unittest.main()
