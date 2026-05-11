"""B2 — chunkerVersion 1/2/3 handling.

Arq's ``backupconfig.json`` records ``chunkerVersion`` as one of
{1, 2, 3} per the spec:

- ``1`` — original Arq 5 chunker (fastCDC-like, internal)
- ``2`` — intermediate Arq 7 chunker
- ``3`` — current Arq 7+ chunker (Buzhash + ``useBuzhash`` toggle)

The reader must:

1. **Accept** all three known versions (validator marks them OK)
2. **Reject** unknown / malformed values cleanly (validator flags)
3. **Restore** destinations from any of the three versions correctly
   — since chunker_version determines *how blobs were split* on
   write, not how they're concatenated on restore, all three
   restore through the same code path

This module pins both the validator-side acceptance and the
restore-side cross-version compatibility.
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
class ValidatorAcceptsKnownChunkerVersionsTests(unittest.TestCase):
    """SV1 invariant: backupconfig.json's chunkerVersion must be
    in {1, 2, 3}. Verify the validator passes each known value
    and rejects unknowns."""

    def _build_then_patch_chunker_version(
        self, td: Path, new_version,
    ):
        """Build a normal backup, then patch backupconfig.json's
        chunkerVersion to ``new_version``. Return the dest root."""
        import json
        from arq_writer.backup import build_backup
        src = td / "src"
        src.mkdir()
        (src / "a.txt").write_bytes(b"alpha")
        dest = td / "dest"
        res = build_backup(
            str(src), str(dest), encryption_password="pw",
        )
        cfg_path = dest / res.computer_uuid / "backupconfig.json"
        cfg = json.loads(cfg_path.read_text())
        cfg["chunkerVersion"] = new_version
        cfg_path.write_text(json.dumps(cfg))
        return dest

    def _validator_status_for(self, dest, password="pw"):
        from arq_validator import (
            LocalBackend, check_arq7_compatibility,
        )
        return check_arq7_compatibility(
            LocalBackend(str(dest)),
            "/", encryption_password=password,
        )

    def test_chunker_version_1_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            dest = self._build_then_patch_chunker_version(
                Path(td), 1,
            )
            report = self._validator_status_for(dest)
            sv1 = [c for c in report.checks if c.id == "SV1"]
            self.assertEqual(len(sv1), 1)
            self.assertTrue(
                sv1[0].passed,
                f"chunkerVersion=1 should pass SV1: {sv1[0].message}",
            )

    def test_chunker_version_2_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            dest = self._build_then_patch_chunker_version(
                Path(td), 2,
            )
            report = self._validator_status_for(dest)
            sv1 = [c for c in report.checks if c.id == "SV1"]
            self.assertTrue(sv1[0].passed)

    def test_chunker_version_3_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            dest = self._build_then_patch_chunker_version(
                Path(td), 3,
            )
            report = self._validator_status_for(dest)
            sv1 = [c for c in report.checks if c.id == "SV1"]
            self.assertTrue(sv1[0].passed)

    def test_chunker_version_4_rejected(self) -> None:
        """Unknown / future version. SV1 should flag it because
        the reader doesn't know the chunker shape."""
        with tempfile.TemporaryDirectory() as td:
            dest = self._build_then_patch_chunker_version(
                Path(td), 4,
            )
            report = self._validator_status_for(dest)
            sv1 = [c for c in report.checks if c.id == "SV1"]
            self.assertFalse(
                sv1[0].passed,
                "chunkerVersion=4 (unknown) must fail SV1",
            )

    def test_chunker_version_string_rejected(self) -> None:
        """Type mismatch (str instead of int). Whatever the
        validator's specific failure mode, it should NOT pass."""
        with tempfile.TemporaryDirectory() as td:
            dest = self._build_then_patch_chunker_version(
                Path(td), "3",
            )
            report = self._validator_status_for(dest)
            sv1 = [c for c in report.checks if c.id == "SV1"]
            # Either the type check fails, OR SV1 still resolves
            # to "value in {1,2,3}" via string comparison. The
            # canonical behaviour is the value-set check rejecting
            # "3" (string ≠ int 3).
            self.assertFalse(
                sv1[0].passed,
                "chunkerVersion='3' (string, not int) must fail SV1",
            )


@unittest.skipUnless(_has_openssl(), "openssl CLI required")
class CrossChunkerVersionRestoreTests(unittest.TestCase):
    """Each chunkerVersion produces a different chunk split on
    write, but restore concatenates blobs back regardless. Verify
    a backup patched to advertise chunkerVersion=1/2/3 still
    restores byte-identical content.

    (The on-disk blobs were emitted with our writer's actual
    chunker — patching the config field after the fact doesn't
    change the bytes, just the metadata claim. Restore reads the
    BlobLocs in tree-order; it doesn't re-chunk.)
    """

    def _round_trip_with_version(self, version: int) -> bool:
        import json
        from arq_writer.backup import build_backup
        from arq_reader import Restore
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            content = b"version " + str(version).encode() + b" content\n" * 100
            (src / "f.bin").write_bytes(content)
            dest = tdp / "dest"
            res = build_backup(
                str(src), str(dest), encryption_password="pw",
            )
            # Patch chunkerVersion.
            cfg_path = (
                dest / res.computer_uuid / "backupconfig.json"
            )
            cfg = json.loads(cfg_path.read_text())
            cfg["chunkerVersion"] = version
            cfg_path.write_text(json.dumps(cfg))
            # Restore.
            out = tdp / "out"
            out.mkdir()
            rs = Restore(str(dest), encryption_password="pw")
            layouts = rs.layouts()
            rs.restore(
                folder_uuid=layouts[0].backup_folder_uuids[0],
                computer_uuid=layouts[0].computer_uuid, dest=out,
            )
            return (out / "f.bin").read_bytes() == content

    def test_restore_from_chunker_version_1_destination(
        self,
    ) -> None:
        self.assertTrue(self._round_trip_with_version(1))

    def test_restore_from_chunker_version_2_destination(
        self,
    ) -> None:
        self.assertTrue(self._round_trip_with_version(2))

    def test_restore_from_chunker_version_3_destination(
        self,
    ) -> None:
        self.assertTrue(self._round_trip_with_version(3))


if __name__ == "__main__":
    unittest.main()
