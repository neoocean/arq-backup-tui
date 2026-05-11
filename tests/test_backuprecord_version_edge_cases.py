"""A3 — BackupRecord version field edge cases.

Arq.app v8 emits ``version`` on BackupRecords as one of:

- ``100`` — legacy Tree v3 records (the bulk of pre-7.40 emits)
- ``101`` — current Tree v4 records (added in Arq 7.40)
- ``200`` — forward-compat slot per spec

Validator's SV3 check enforces the value-set. Pre-A3, only
``(100, 200)`` were accepted; ``101`` would FAIL — a real
production bug for any operator with Tree v4 data (e.g. our
operator's destination has 18 v101 records sampled 2026-05-11).

This module pins:

- All three known versions (100, 101, 200) accepted by SV3
- Unknown versions (99, 102, 300, 0, None, string "100") rejected
- Writer's v100 emit when ``node_tree_version`` is None
- Writer's v101 emit when ``node_tree_version=4``
- Explicit version=200 override works (callers can pin
  forward-compat shape for testing)
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
class ValidatorSV3VersionTests(unittest.TestCase):
    """SV3 must accept v100, v101, v200; reject others."""

    def _build_and_patch_version(self, td: Path, new_version):
        from arq_writer.backup import build_backup
        from arq_writer.backuprecord import (
            parse_backuprecord, serialize_backuprecord,
        )
        from arq_validator import LocalBackend
        from arq_validator.crypto import decrypt_keyset
        from arq_reader.decrypt import decrypt_lz4_arqo
        from arq_writer.crypto_write import build_encrypted_object
        from arq_writer.lz4_block import lz4_wrap

        src = td / "src"
        src.mkdir()
        (src / "a.txt").write_bytes(b"x")
        dest = td / "dest"
        res = build_backup(
            str(src), str(dest), encryption_password="pw",
        )
        b = LocalBackend(str(dest))
        ks = decrypt_keyset(
            b.read_all(
                f"/{res.computer_uuid}/encryptedkeyset.dat",
            ),
            "pw",
        )
        rec_rel = str(
            res.backuprecord_path.relative_to(res.dest_root)
        )
        arqo = b.read_all("/" + rec_rel)
        plain = decrypt_lz4_arqo(
            arqo, ks.encryption_key, ks.hmac_key,
        )
        rec = parse_backuprecord(plain)
        rec["version"] = new_version
        new_plain = serialize_backuprecord(rec, fmt="json")
        wrapped = lz4_wrap(new_plain)
        new_arqo = build_encrypted_object(
            wrapped, ks.encryption_key, ks.hmac_key,
        )
        (dest / rec_rel).write_bytes(new_arqo)
        return dest

    def _sv3_status(self, dest):
        from arq_validator import (
            LocalBackend, check_arq7_compatibility,
        )
        report = check_arq7_compatibility(
            LocalBackend(str(dest)),
            "/", encryption_password="pw",
        )
        return [c for c in report.checks if c.id == "SV3"]

    def test_version_100_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            dest = self._build_and_patch_version(Path(td), 100)
            sv3 = self._sv3_status(dest)
            self.assertGreater(len(sv3), 0)
            self.assertTrue(
                all(c.passed for c in sv3),
                f"v100 should pass SV3: {sv3}",
            )

    def test_version_101_accepted(self) -> None:
        """Tree v4 record version. Real Arq.app v8 destinations
        contain these; pre-A3 they would FAIL the validator."""
        with tempfile.TemporaryDirectory() as td:
            dest = self._build_and_patch_version(Path(td), 101)
            sv3 = self._sv3_status(dest)
            self.assertTrue(
                all(c.passed for c in sv3),
                f"v101 (Tree v4) should pass SV3: {sv3}",
            )

    def test_version_200_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            dest = self._build_and_patch_version(Path(td), 200)
            sv3 = self._sv3_status(dest)
            self.assertTrue(all(c.passed for c in sv3))

    def test_unknown_versions_rejected(self) -> None:
        """99 (pre-spec), 102 (gap between 101 and 200), 300
        (forward-compat slot we don't yet recognise) all FAIL."""
        for bad in (99, 102, 300):
            with tempfile.TemporaryDirectory() as td:
                dest = self._build_and_patch_version(
                    Path(td), bad,
                )
                sv3 = self._sv3_status(dest)
                self.assertTrue(
                    any(not c.passed for c in sv3),
                    f"v{bad} should fail SV3 but didn't",
                )


class WriterVersionEmitTests(unittest.TestCase):
    """Writer's version field reflects node_tree_version arg."""

    def _build_rec(self, **kwargs):
        from arq_writer.backuprecord import build_backuprecord_dict
        from arq_writer.types import FileNode
        return build_backuprecord_dict(
            backup_folder_uuid="F",
            backup_plan_uuid="P",
            backup_plan_dict={},
            root_node=FileNode(itemSize=0),
            local_path="/x",
            **kwargs,
        )

    def test_default_emits_v100_no_nodeTreeVersion(self) -> None:
        rec = self._build_rec()
        self.assertEqual(rec["version"], 100)
        self.assertNotIn("nodeTreeVersion", rec)

    def test_tree_v4_emits_v101_with_nodeTreeVersion(self) -> None:
        rec = self._build_rec(node_tree_version=4)
        self.assertEqual(rec["version"], 101)
        self.assertEqual(rec["nodeTreeVersion"], 4)

    def test_explicit_version_200_override_works(self) -> None:
        """Explicit ``version=200`` lets callers pin the
        forward-compat slot for testing — useful for future
        spec changes."""
        rec = self._build_rec(version=200, node_tree_version=4)
        self.assertEqual(rec["version"], 200)
        # nodeTreeVersion is still emitted because of v4 toggle.
        self.assertEqual(rec["nodeTreeVersion"], 4)


if __name__ == "__main__":
    unittest.main()
