"""A보완-6 — D4 placeholder path non-existence pin.

D4 (PR #98) emitted placeholder PATHS for all 6 storage-class
ObjectDirs in ``backupfolders.json``. But those paths are
**advertisements**, not actual directories — Arq.app v8 doesn't
create the corresponding dirs on disk when no objects use those
storage classes.

This module pins that contract:

- The writer emits the path strings in JSON
- The writer does NOT create the actual directories on the
  filesystem (a fresh backup destination has only
  ``standardobjects/`` materialized; the other 5 storage-class
  dirs are advertised paths only)

If a future change inadvertently mkdir's the placeholder paths
during init, this test flags it — that'd be a regression (Arq.app
would still work but the destination would have phantom dirs +
the schema diff against real Arq.app would re-emerge).
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
class D4PlaceholderPathNonExistenceTests(unittest.TestCase):

    def _build_backup(self, td: Path):
        from arq_writer.backup import build_backup
        src = td / "src"
        src.mkdir()
        (src / "a.txt").write_bytes(b"x")
        dest = td / "dest"
        res = build_backup(
            str(src), str(dest), encryption_password="pw",
        )
        return dest, res

    def test_only_standardobjects_dir_actually_exists(self) -> None:
        """Fresh backup has the standardobjects/ dir materialized.
        The other 5 storage-class placeholder paths exist only as
        JSON advertisements; the directories themselves are NOT
        created."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            dest, res = self._build_backup(tdp)
            cu_root = dest / res.computer_uuid
            # standardobjects/ DOES exist (the only one we actually use).
            self.assertTrue(
                (cu_root / "standardobjects").is_dir(),
                "standardobjects/ should be created on the FS",
            )
            # The other 5 advertised placeholder dirs do NOT exist.
            for placeholder in (
                "standardiaobjects",
                "onezoneiaobjects",
                "s3glacierobjects",
                "s3glacierirobjects",
                "s3deeparchiveobjects",
            ):
                placeholder_path = cu_root / placeholder
                self.assertFalse(
                    placeholder_path.is_dir(),
                    f"{placeholder}/ should NOT be materialized — "
                    f"it's a placeholder path advertised in JSON, "
                    f"not an actual directory. (D4 contract)",
                )

    def test_packs_and_blobs_dirs_materialized_only_when_used(
        self,
    ) -> None:
        """treepacks/ + blobpacks/ should only exist when
        use_packs=True. The fresh standalone-objects backup above
        leaves them un-created."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            dest, res = self._build_backup(tdp)
            cu_root = dest / res.computer_uuid
            for pack_dir in ("treepacks", "blobpacks", "largeblobpacks"):
                self.assertFalse(
                    (cu_root / pack_dir).is_dir(),
                    f"{pack_dir}/ should NOT exist when use_packs=False",
                )

    def test_placeholder_paths_in_json_match_naming_convention(
        self,
    ) -> None:
        """Cross-check: the path STRINGS emitted in
        backupfolders.json point at the same naming convention
        we expect — even though those dirs don't materialize."""
        from arq_validator import LocalBackend
        from arq_validator.crypto import decrypt_keyset
        from arq_validator.sidecar import read_sidecar
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            dest, res = self._build_backup(tdp)
            b = LocalBackend(str(dest))
            ks = decrypt_keyset(
                b.read_all(
                    f"/{res.computer_uuid}/encryptedkeyset.dat",
                ),
                "pw",
            )
            data = read_sidecar(
                b, f"/{res.computer_uuid}/backupfolders.json",
                keyset=ks,
            )
            cu = res.computer_uuid
            naming = {
                "standardObjectDirs": f"/{cu}/standardobjects",
                "standardIAObjectDirs": f"/{cu}/standardiaobjects",
                "onezoneIAObjectDirs": f"/{cu}/onezoneiaobjects",
                "s3GlacierObjectDirs": f"/{cu}/s3glacierobjects",
                "s3GlacierIRObjectDirs": (
                    f"/{cu}/s3glacierirobjects"
                ),
                "s3DeepArchiveObjectDirs": (
                    f"/{cu}/s3deeparchiveobjects"
                ),
            }
            for field, expected_path in naming.items():
                self.assertEqual(data[field], [expected_path])


if __name__ == "__main__":
    unittest.main()
