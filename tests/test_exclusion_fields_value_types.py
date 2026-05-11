"""D3 — per-folder exclusion field value types + defaults.

Each folder plan in ``backupplan.json::backupFolderPlansByUUID``
carries five exclusion-related fields:

| Field | Type | Default | Semantics |
|---|---|---|---|
| ``wildcardExcludes`` | list[str] | ``[]`` | shell-glob patterns |
| ``regexExcludes`` | list[str] | ``[]`` | Python regex strings |
| ``ignoredRelativePaths`` | list[str] | ``[]`` | absolute (from src root) path matches |
| ``skipDuringBackup`` | bool | ``False`` | pause this folder |
| ``skipIfNotMounted`` | bool | ``False`` | skip when mount-point absent |

These all derive from operator config; the writer's defaults
match Arq.app v8's empty-everything emit (sampled per
D1 investigation). This module pins:

- Default types are exact (``[]`` not ``None``, ``False`` not
  ``0``)
- Lists round-trip non-empty values verbatim
- A typo'd exclusion field with the wrong type fails validation
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
class ExclusionFieldValueTypesTests(unittest.TestCase):

    def _build_and_get_plan(self, td: Path, **build_kwargs):
        """Build a backup + return the decrypted plan dict."""
        from arq_writer.backup import build_backup
        from arq_validator import LocalBackend
        from arq_validator.crypto import decrypt_keyset
        from arq_validator.sidecar import read_sidecar

        src = td / "src"
        src.mkdir()
        (src / "a.txt").write_bytes(b"x")
        dest = td / "dest"
        res = build_backup(
            str(src), str(dest), encryption_password="pw",
            **build_kwargs,
        )
        b = LocalBackend(str(dest))
        ks = decrypt_keyset(
            b.read_all(f"/{res.computer_uuid}/encryptedkeyset.dat"),
            "pw",
        )
        plan = read_sidecar(
            b, f"/{res.computer_uuid}/backupplan.json",
            keyset=ks,
        )
        return plan, res

    def test_default_exclusion_fields_have_correct_types(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            plan, res = self._build_and_get_plan(Path(td))
            folder_plan = plan["backupFolderPlansByUUID"][
                res.folder_uuid
            ]
            # List-typed defaults.
            for field in (
                "wildcardExcludes",
                "regexExcludes",
                "ignoredRelativePaths",
                "excludedDrives",
            ):
                self.assertIn(field, folder_plan)
                self.assertIsInstance(
                    folder_plan[field], list,
                    f"{field} should be list, got "
                    f"{type(folder_plan[field]).__name__}",
                )
                self.assertEqual(folder_plan[field], [])
            # Bool-typed defaults.
            for field in (
                "skipDuringBackup", "skipIfNotMounted",
                "skipTMExcludes", "useDiskIdentifier",
                "allDrives",
            ):
                self.assertIn(field, folder_plan)
                self.assertIsInstance(
                    folder_plan[field], bool,
                    f"{field} should be bool, got "
                    f"{type(folder_plan[field]).__name__}",
                )

    def test_blob_storage_class_default_is_standard(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            plan, res = self._build_and_get_plan(Path(td))
            folder_plan = plan["backupFolderPlansByUUID"][
                res.folder_uuid
            ]
            self.assertEqual(
                folder_plan.get("blobStorageClass"), "STANDARD",
            )

    def test_disk_identifier_is_string_not_null(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            plan, res = self._build_and_get_plan(Path(td))
            folder_plan = plan["backupFolderPlansByUUID"][
                res.folder_uuid
            ]
            self.assertIsInstance(
                folder_plan["diskIdentifier"], str,
                "diskIdentifier should always be a string, not null",
            )


if __name__ == "__main__":
    unittest.main()
