"""D4 — Glacier / storage-class sidecar field defaults.

``backupfolders.json`` carries six storage-class
ObjectDir fields. Each must be a single-element list with the
placeholder path under ``/<cu>/<class>objects``, even when no
actual objects use that storage class.

Sampled 2026-05-11 against ``/Volumes/arqbackup1`` (D4
investigation): all 6 fields carry placeholder paths even
though only the ``standardobjects`` directory actually exists
on disk.

| Field | Path suffix |
|---|---|
| ``standardObjectDirs`` | ``standardobjects`` |
| ``standardIAObjectDirs`` | ``standardiaobjects`` |
| ``onezoneIAObjectDirs`` | ``onezoneiaobjects`` |
| ``s3GlacierObjectDirs`` | ``s3glacierobjects`` |
| ``s3GlacierIRObjectDirs`` | ``s3glacierirobjects`` |
| ``s3DeepArchiveObjectDirs`` | ``s3deeparchiveobjects`` |

Pre-D4 our writer emitted 5 of the 6 as ``[]``; only
``standardObjectDirs`` had the placeholder. Schema-fingerprint
diff against real Arq.app showed 5 missing entries; D4 closes
that gap.
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


class BuildBackupFoldersJsonTests(unittest.TestCase):
    """Pure builder check — no I/O."""

    def test_all_six_object_dirs_have_placeholder_paths(self) -> None:
        from arq_writer.json_configs import build_backupfolders_json
        cu = "8EB255DD-09D3-43F8-8FE5-6106EBCE1A5D"
        data = build_backupfolders_json(cu)

        expected = {
            "standardObjectDirs": f"/{cu}/standardobjects",
            "standardIAObjectDirs": f"/{cu}/standardiaobjects",
            "onezoneIAObjectDirs": f"/{cu}/onezoneiaobjects",
            "s3GlacierObjectDirs": f"/{cu}/s3glacierobjects",
            "s3GlacierIRObjectDirs": f"/{cu}/s3glacierirobjects",
            "s3DeepArchiveObjectDirs": (
                f"/{cu}/s3deeparchiveobjects"
            ),
        }
        for field, expected_path in expected.items():
            self.assertIn(field, data)
            self.assertIsInstance(data[field], list)
            self.assertEqual(
                len(data[field]), 1,
                f"{field} should be single-element list, "
                f"got len={len(data[field])}",
            )
            self.assertEqual(
                data[field][0], expected_path,
                f"{field}: expected {expected_path!r}, got "
                f"{data[field][0]!r}",
            )


@unittest.skipUnless(_has_openssl(), "openssl CLI required")
class GlacierFieldsEndToEndTests(unittest.TestCase):
    """End-to-end: build a backup, decrypt backupfolders.json,
    verify all 6 fields carry placeholder paths."""

    def test_all_six_object_dirs_emitted_in_real_backup(
        self,
    ) -> None:
        import json
        from arq_writer.backup import build_backup
        from arq_validator import LocalBackend
        from arq_validator.crypto import decrypt_keyset
        from arq_validator.sidecar import read_sidecar
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "a.txt").write_bytes(b"x")
            dest = tdp / "dest"
            res = build_backup(
                str(src), str(dest), encryption_password="pw",
            )
            cu = res.computer_uuid
            b = LocalBackend(str(dest))
            data = read_sidecar(
                b, f"/{cu}/backupfolders.json",
                keyset=decrypt_keyset(
                    b.read_all(f"/{cu}/encryptedkeyset.dat"),
                    "pw",
                ),
            )
            for field in (
                "standardObjectDirs",
                "standardIAObjectDirs",
                "onezoneIAObjectDirs",
                "s3GlacierObjectDirs",
                "s3GlacierIRObjectDirs",
                "s3DeepArchiveObjectDirs",
            ):
                self.assertEqual(
                    len(data[field]), 1,
                    f"{field} should have 1 entry, got "
                    f"{len(data[field])}",
                )
                self.assertTrue(
                    data[field][0].startswith(f"/{cu}/"),
                    f"{field}[0] doesn't start with /<cu>/",
                )


class BackupConfigGlacierFlagsTests(unittest.TestCase):
    """``backupconfig.json`` carries two Glacier-related bool
    flags. Both default False (no Glacier archives, not WORM)."""

    def test_isWORM_and_containsGlacierArchives_are_bool_false(
        self,
    ) -> None:
        from arq_writer.json_configs import build_backupconfig
        data = build_backupconfig(
            backup_name="x", computer_name="y",
        )
        self.assertIsInstance(data["isWORM"], bool)
        self.assertIsInstance(
            data["containsGlacierArchives"], bool,
        )
        self.assertFalse(data["isWORM"])
        self.assertFalse(data["containsGlacierArchives"])


if __name__ == "__main__":
    unittest.main()
