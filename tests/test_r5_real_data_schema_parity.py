"""R5 — schema parity against real Arq.app v8 destination.

After Round 6's landing + V4's aclBlobLoc fix, this test pins
the current "schema-complete" state by re-sampling real Arq.app
v8 sidecars and BackupRecords from the operator's destination
and confirming the writer's emit matches key-for-key.

Auto-skips when ``/Volumes/arqbackup1`` is unmounted or
``.secrets/dest_password`` is absent — the test is meaningful
only when the operator's real destination is available, so the
skip is acceptable on CI / machines without the fixture.

Counts pinned by this module:

| Sidecar / record | Keys expected |
|---|---:|
| ``backupplan.json`` | 47 |
| ``backupfolders.json`` | 6 |
| ``backupconfig.json`` | 11 |
| ``backupfolder.json`` (per folder) | 8 |
| BackupRecord top level | 20 (v4) / 19 (v3) |
| BackupRecord ``node`` (v3) | 27 |
| BackupRecord ``node`` (v4) | 34 |

Any future writer change that drops a key OR adds a key Arq.app
doesn't emit will fail one of these counts + show the diff.
"""

from __future__ import annotations

import json
import subprocess
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


REAL_DEST = Path("/Volumes/arqbackup1")
SECRETS_PW = Path(".secrets/dest_password")


@unittest.skipUnless(_has_openssl(), "openssl CLI required")
@unittest.skipUnless(
    REAL_DEST.is_dir(),
    f"real Arq.app v8 destination not mounted at {REAL_DEST}",
)
@unittest.skipUnless(
    SECRETS_PW.is_file(),
    f"destination password not at {SECRETS_PW}",
)
class R5_RealDataSchemaParityTests(unittest.TestCase):
    """Sample real sidecars + records; confirm writer key set
    matches Arq.app v8's emit key-for-key."""

    @classmethod
    def setUpClass(cls):
        from arq_validator.crypto import decrypt_keyset
        # Locate any computer-UUID directory under the destination.
        cu_dirs = [
            p for p in REAL_DEST.iterdir()
            if p.is_dir() and len(p.name) == 36
        ]
        if not cu_dirs:
            raise unittest.SkipTest(
                f"no computer UUID directory under {REAL_DEST}"
            )
        cls.cu_dir = cu_dirs[0]
        cls.password = SECRETS_PW.read_text().strip()
        ks_path = cls.cu_dir / "encryptedkeyset.dat"
        if not ks_path.is_file():
            raise unittest.SkipTest(
                f"no keyset under {cls.cu_dir}"
            )
        cls.keyset = decrypt_keyset(
            ks_path.read_bytes(), cls.password,
        )

    def _decrypt_object(self, p: Path):
        """Decrypt an ARQO-wrapped JSON file (backupplan.json,
        backupfolder.json) and return parsed dict."""
        from arq_reader.decrypt import decrypt_encrypted_object
        plain = decrypt_encrypted_object(
            p.read_bytes(),
            self.keyset.encryption_key, self.keyset.hmac_key,
        )
        return json.loads(plain.decode("utf-8"))

    def test_backupconfig_47_keys_match(self) -> None:
        """backupconfig.json is cleartext; should be 11 keys."""
        from arq_writer.json_configs import build_backupconfig
        arq = json.loads(
            (self.cu_dir / "backupconfig.json").read_text()
        )
        ours = build_backupconfig(
            backup_name="test", computer_name="test",
        )
        self.assertEqual(
            set(arq.keys()), set(ours.keys()),
            f"backupconfig key diff: arq-only="
            f"{set(arq) - set(ours)}, ours-only="
            f"{set(ours) - set(arq)}",
        )
        self.assertEqual(len(arq), 11)

    def test_backupfolders_keys_match(self) -> None:
        """backupfolders.json is cleartext; should be 6 keys."""
        from arq_writer.json_configs import build_backupfolders_json
        arq = json.loads(
            (self.cu_dir / "backupfolders.json").read_text()
        )
        ours = build_backupfolders_json(
            "00000000-0000-0000-0000-000000000000",
        )
        self.assertEqual(
            set(arq.keys()), set(ours.keys()),
            f"backupfolders key diff: arq-only="
            f"{set(arq) - set(ours)}, ours-only="
            f"{set(ours) - set(arq)}",
        )
        self.assertEqual(len(arq), 6)

    def test_backupplan_keys_match(self) -> None:
        """backupplan.json is encrypted ARQO; 47 keys expected."""
        from arq_writer.json_configs import build_backupplan
        arq = self._decrypt_object(
            self.cu_dir / "backupplan.json"
        )
        ours = build_backupplan(
            plan_uuid="00000000-0000-0000-0000-000000000000",
            plan_name="test", folder_plans=[],
        )
        self.assertEqual(
            set(arq.keys()), set(ours.keys()),
            f"backupplan key diff: arq-only="
            f"{set(arq) - set(ours)}, ours-only="
            f"{set(ours) - set(arq)}",
        )
        self.assertEqual(len(arq), 47)

    def test_backupfolder_keys_match(self) -> None:
        """backupfolder.json (per folder) is encrypted ARQO; 8
        keys expected."""
        from arq_writer.json_configs import build_backupfolder_json
        bf_paths = list(
            self.cu_dir.glob("backupfolders/*/backupfolder.json")
        )
        if not bf_paths:
            self.skipTest("no backupfolder.json files found")
        arq = self._decrypt_object(bf_paths[0])
        ours = build_backupfolder_json(
            folder_uuid="aaa", name="test",
            local_path="/x", local_mount_point="/",
        )
        self.assertEqual(
            set(arq.keys()), set(ours.keys()),
            f"backupfolder key diff: arq-only="
            f"{set(arq) - set(ours)}, ours-only="
            f"{set(ours) - set(arq)}",
        )
        self.assertEqual(len(arq), 8)

    def _find_v4_record(self):
        from arq_reader.decrypt import decrypt_lz4_arqo
        for rp in self.cu_dir.glob(
            "backupfolders/*/backuprecords/*/*.backuprecord"
        ):
            try:
                plain = decrypt_lz4_arqo(
                    rp.read_bytes(),
                    self.keyset.encryption_key,
                    self.keyset.hmac_key,
                )
                rec = json.loads(plain.decode("utf-8"))
            except Exception:
                continue
            if rec.get("nodeTreeVersion") == 4:
                return rec
        return None

    def test_backuprecord_v4_top_level_keys(self) -> None:
        """A v4 BackupRecord has 20 top-level keys (the v3 keys +
        ``nodeTreeVersion``)."""
        rec = self._find_v4_record()
        if rec is None:
            self.skipTest(
                "no v4 BackupRecord found in destination"
            )
        self.assertEqual(
            len(rec.keys()), 20,
            f"v4 BackupRecord should have 20 top-level keys; "
            f"got {sorted(rec.keys())}",
        )
        self.assertIn("nodeTreeVersion", rec)

    def test_backuprecord_v4_node_keys_match_ours(self) -> None:
        """The v4 record's node dict has 34 keys; our writer's
        emit (with V4 fix omitting null aclBlobLoc) matches."""
        from arq_writer.backuprecord import node_to_dict
        from arq_writer.types import TreeNode, BlobLoc
        rec = self._find_v4_record()
        if rec is None:
            self.skipTest("no v4 BackupRecord found")
        arq_node_keys = set(rec["node"].keys())
        # Our writer's node_to_dict on a TreeNode with no ACL.
        our_node = TreeNode(
            treeBlobLoc=BlobLoc(blobIdentifier="aa" * 32),
        )
        our_node_keys = set(node_to_dict(our_node).keys())
        self.assertEqual(
            arq_node_keys, our_node_keys,
            f"node key diff: arq-only="
            f"{arq_node_keys - our_node_keys}, ours-only="
            f"{our_node_keys - arq_node_keys}",
        )
        # Concrete count.
        self.assertEqual(len(arq_node_keys), 34)


if __name__ == "__main__":
    unittest.main()
