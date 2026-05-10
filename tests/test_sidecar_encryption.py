"""End-to-end tests for the ARQO-encrypted sidecar JSON path (T1).

Arq.app v8 wraps two of its sidecar JSON files in an ``ARQO``
envelope:

- ``<computer_uuid>/backupplan.json``
- ``<computer_uuid>/backupfolders/<folder_uuid>/backupfolder.json``

The envelope is the same one blob payloads use, but **without**
the inner LZ4 block compression — its plaintext is the
pretty-printed JSON bytes verbatim.

These tests pin three things:

1. Our writer emits the two sidecars as ARQO envelopes (not
   plain JSON).
2. The other two top-level sidecars (``backupconfig.json``,
   ``backupfolders.json``) stay plain — the schema diff
   showed Arq.app v8 keeps those plain too.
3. ``arq_validator.sidecar.read_sidecar`` round-trips both
   shapes correctly: with a keyset, ARQO sidecars decrypt; without,
   they return ``None`` rather than raise.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from arq_validator.backend import LocalBackend
from arq_validator.crypto import decrypt_keyset
from arq_validator.sidecar import read_sidecar
from arq_writer import build_backup


_ARQO_MAGIC = b"ARQO"


def _make_simple_tree(root: Path) -> None:
    (root / "subdir").mkdir(parents=True)
    (root / "alpha.txt").write_bytes(b"alpha\n")
    (root / "subdir" / "beta.txt").write_bytes(b"beta\n")


class SidecarEncryptionRoundTripTests(unittest.TestCase):
    """Build a backup, then inspect every sidecar's on-disk magic
    and round-trip it through ``read_sidecar``."""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        tdp = Path(self._td.name)
        self.src = tdp / "src"
        self.src.mkdir()
        _make_simple_tree(self.src)
        self.dest = tdp / "dest"
        self.password = "pw"
        self.r = build_backup(
            self.src, self.dest,
            encryption_password=self.password,
        )
        self.cu_dir = self.dest / self.r.computer_uuid
        self.bf_dir = (
            self.cu_dir / "backupfolders" / self.r.folder_uuid
        )
        self.keyset = decrypt_keyset(
            (self.cu_dir / "encryptedkeyset.dat").read_bytes(),
            self.password,
        )
        self.backend = LocalBackend(self.dest)
        self.cu_root_path = f"/{self.r.computer_uuid}"
        self.bf_path = (
            f"{self.cu_root_path}/backupfolders/"
            f"{self.r.folder_uuid}"
        )

    def test_backupplan_starts_with_ARQO_magic(self) -> None:
        head = (self.cu_dir / "backupplan.json").read_bytes()[:4]
        self.assertEqual(head, _ARQO_MAGIC)

    def test_backupfolder_starts_with_ARQO_magic(self) -> None:
        head = (self.bf_dir / "backupfolder.json").read_bytes()[:4]
        self.assertEqual(head, _ARQO_MAGIC)

    def test_backupconfig_stays_plain_json(self) -> None:
        # backupconfig.json + backupfolders.json are plain in Arq.app
        # v8 — make sure we don't accidentally encrypt them.
        head = (self.cu_dir / "backupconfig.json").read_bytes()[:4]
        self.assertNotEqual(head, _ARQO_MAGIC)
        self.assertEqual(head, b"{\n  ")  # JSON pretty-print head

    def test_backupfolders_index_stays_plain_json(self) -> None:
        head = (self.cu_dir / "backupfolders.json").read_bytes()[:4]
        self.assertNotEqual(head, _ARQO_MAGIC)
        self.assertEqual(head, b"{\n  ")

    def test_read_sidecar_decrypts_backupplan_with_keyset(self) -> None:
        plan = read_sidecar(
            self.backend, f"{self.cu_root_path}/backupplan.json",
            keyset=self.keyset,
        )
        self.assertIsNotNone(plan)
        self.assertEqual(plan["version"], 2)
        self.assertEqual(plan["isEncrypted"], True)
        self.assertEqual(plan["planUUID"], self.r.plan_uuid)
        self.assertIn("backupFolderPlansByUUID", plan)

    def test_read_sidecar_decrypts_backupfolder_with_keyset(self) -> None:
        bf = read_sidecar(
            self.backend, f"{self.bf_path}/backupfolder.json",
            keyset=self.keyset,
        )
        self.assertIsNotNone(bf)
        self.assertEqual(bf["uuid"], self.r.folder_uuid)
        self.assertIn("localPath", bf)

    def test_read_sidecar_returns_none_on_arqo_without_keyset(
        self,
    ) -> None:
        # Without a keyset we can't decrypt — return None instead of
        # raising, mirroring the existing missing-or-unparseable
        # convention used elsewhere in the validator.
        plan = read_sidecar(
            self.backend, f"{self.cu_root_path}/backupplan.json",
            keyset=None,
        )
        self.assertIsNone(plan)

    def test_read_sidecar_parses_plain_backupconfig_without_keyset(
        self,
    ) -> None:
        # Plain JSON sidecars don't need a keyset; passing None must
        # still return the parsed dict.
        cfg = read_sidecar(
            self.backend, f"{self.cu_root_path}/backupconfig.json",
            keyset=None,
        )
        self.assertIsNotNone(cfg)
        self.assertEqual(cfg["chunkerVersion"], 3)
        self.assertEqual(cfg["isEncrypted"], True)

    def test_read_sidecar_returns_none_on_missing_path(self) -> None:
        plan = read_sidecar(
            self.backend, f"{self.cu_root_path}/nope.json",
            keyset=self.keyset,
        )
        self.assertIsNone(plan)

    def test_read_sidecar_returns_none_on_garbled_arqo(self) -> None:
        # Truncate the encrypted backupplan to invalidate its HMAC.
        path = self.cu_dir / "backupplan.json"
        truncated = path.read_bytes()[:64]
        path.write_bytes(truncated)
        plan = read_sidecar(
            self.backend, f"{self.cu_root_path}/backupplan.json",
            keyset=self.keyset,
        )
        self.assertIsNone(plan)


if __name__ == "__main__":
    unittest.main()
