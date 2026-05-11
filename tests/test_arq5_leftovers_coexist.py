"""B5 — Arq 5 reflog / commit-tree leftover coexistence.

Some operator destinations contain BOTH Arq 5 era artefacts AND
Arq 7 layouts. The Arq 5 paths look like:

- ``<cu>/bucketdata/<folder_uuid>/refs/heads/master`` — git-
  style ref pointing at the most-recent commit SHA-1
- ``<cu>/bucketdata/<folder_uuid>/packsets/<folder>-trees/...``
- ``<cu>/packsets/<folder>-blobs/...``

The Arq 7 layout adds new sibling dirs under ``<cu>/``:

- ``<cu>/backupfolders/<fu>/backuprecords/...``
- ``<cu>/standardobjects/...``
- ``<cu>/treepacks/``, ``<cu>/blobpacks/``

The Arq 7 reader walks ONLY the Arq 7 paths; Arq 5 leftovers
should be ignored cleanly. Pinned properties:

- ``Restore.layouts()`` doesn't include Arq 5 ref-tree
  paths as backup folders
- Validator (``check_arq7_compatibility``) doesn't flag Arq 5
  artefacts as L6/L7 violations (it only checks the Arq 7
  ``backupfolders/`` subtree)
- Restoring an Arq 7 folder from a hybrid destination works —
  Arq 5 leftovers don't interfere

The tests synthesise the hybrid layout by adding Arq 5-shaped
dummy directories to a real Arq 7 destination, then verifying
the reader/validator stay clean.
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
class Arq5LeftoversCoexistTests(unittest.TestCase):

    def _make_arq5_leftovers(self, dest: Path, cu: str) -> None:
        """Add Arq 5-shaped dummy artefacts under <cu>/."""
        bucket_root = dest / cu / "bucketdata"
        bucket_root.mkdir(exist_ok=True)
        fake_folder = bucket_root / "LEGACY-ARQ5-FOLDER-UUID"
        (fake_folder / "refs" / "heads").mkdir(
            parents=True, exist_ok=True,
        )
        (fake_folder / "refs" / "heads" / "master").write_bytes(
            b"deadbeef" * 5,
        )
        # Arq 5 packsets root.
        (dest / cu / "packsets").mkdir(exist_ok=True)
        (dest / cu / "packsets" / "fake-trees").mkdir(exist_ok=True)

    def test_arq7_validator_ignores_arq5_leftovers(self) -> None:
        from arq_writer.backup import build_backup
        from arq_validator import (
            LocalBackend, check_arq7_compatibility,
        )
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "a.txt").write_bytes(b"x")
            dest = tdp / "dest"
            res = build_backup(
                str(src), str(dest), encryption_password="pw",
            )
            self._make_arq5_leftovers(dest, res.computer_uuid)
            report = check_arq7_compatibility(
                LocalBackend(str(dest)),
                "/", encryption_password="pw",
            )
            # No L6/L7 failures (which scan backupfolders/).
            failures = [
                c for c in report.failed_checks
                if c.id in ("L6", "L7")
            ]
            self.assertEqual(
                failures, [],
                f"Arq 5 leftovers triggered L6/L7 failures: "
                f"{failures}",
            )

    def test_arq7_reader_lists_only_arq7_folders(self) -> None:
        from arq_writer.backup import build_backup
        from arq_reader import Restore
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "a.txt").write_bytes(b"x")
            dest = tdp / "dest"
            res = build_backup(
                str(src), str(dest), encryption_password="pw",
            )
            self._make_arq5_leftovers(dest, res.computer_uuid)
            rs = Restore(str(dest), encryption_password="pw")
            layouts = rs.layouts()
            # Each layout's folder UUIDs come from
            # backupfolders/<UUID>/ — NOT from bucketdata/<UUID>/.
            for layout in layouts:
                for folder in layout.backup_folder_uuids:
                    self.assertNotEqual(
                        folder, "LEGACY-ARQ5-FOLDER-UUID",
                        "Arq 5 bucketdata UUID leaked into "
                        "Arq 7 layout's folder list",
                    )

    def test_restore_works_on_hybrid_destination(self) -> None:
        """Arq 7 folder restore proceeds normally despite Arq 5
        artefacts in the same destination."""
        from arq_writer.backup import build_backup
        from arq_reader import Restore
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "real.txt").write_bytes(b"arq7 content")
            dest = tdp / "dest"
            res = build_backup(
                str(src), str(dest), encryption_password="pw",
            )
            self._make_arq5_leftovers(dest, res.computer_uuid)
            out = tdp / "out"
            out.mkdir()
            rs = Restore(str(dest), encryption_password="pw")
            layouts = rs.layouts()
            rs.restore(
                folder_uuid=layouts[0].backup_folder_uuids[0],
                computer_uuid=layouts[0].computer_uuid, dest=out,
            )
            self.assertEqual(
                (out / "real.txt").read_bytes(), b"arq7 content",
            )


if __name__ == "__main__":
    unittest.main()
