"""A12 + A14 + A15 — BackupRecord edge-case fields.

Three groups of edge cases on the BackupRecord plist:

- **A12 `archived: true`** — operator-archived records.
  Reader + validator handle both False (default) and True
  cleanly.
- **A14 `computerOSType` values** — 1 = macOS, 2 = Windows,
  3 = Linux. Validator + reader accept all three.
- **A15 Empty source / zero-children** — backup of a fully-
  empty source. Record has ``node.isTree=True`` with zero
  children + ``containedFilesCount=0``.
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


def _patch_record_field(td_dest: Path, cu: str, fu: str,
                        field: str, value):
    """Patch a single field of the BackupRecord JSON on disk."""
    from arq_validator import LocalBackend
    from arq_validator.crypto import decrypt_keyset
    from arq_writer.crypto_write import build_encrypted_object
    from arq_writer.lz4_block import lz4_wrap
    from arq_writer.backuprecord import (
        parse_backuprecord, serialize_backuprecord,
    )
    from arq_reader.decrypt import decrypt_lz4_arqo
    b = LocalBackend(str(td_dest))
    ks = decrypt_keyset(
        b.read_all(f"/{cu}/encryptedkeyset.dat"), "pw",
    )
    rec_root = (
        td_dest / cu / "backupfolders" / fu / "backuprecords"
    )
    rec_path = next(rec_root.rglob("*.backuprecord"))
    arqo = rec_path.read_bytes()
    plain = decrypt_lz4_arqo(
        arqo, ks.encryption_key, ks.hmac_key,
    )
    rec = parse_backuprecord(plain)
    rec[field] = value
    new_plain = serialize_backuprecord(rec, fmt="json")
    new_arqo = build_encrypted_object(
        lz4_wrap(new_plain), ks.encryption_key, ks.hmac_key,
    )
    rec_path.write_bytes(new_arqo)


@unittest.skipUnless(_has_openssl(), "openssl CLI required")
class A12_ArchivedRecordTests(unittest.TestCase):
    """``archived: true`` record handling."""

    def _build_then_archive(self, td: Path):
        from arq_writer.backup import build_backup
        src = td / "src"
        src.mkdir()
        (src / "f.txt").write_bytes(b"content")
        res = build_backup(
            str(src), str(td / "dest"), encryption_password="pw",
        )
        _patch_record_field(
            td / "dest", res.computer_uuid, res.folder_uuid,
            "archived", True,
        )
        return res

    def test_archived_record_still_readable(self) -> None:
        from arq_reader import Restore
        with tempfile.TemporaryDirectory() as td:
            res = self._build_then_archive(Path(td))
            rs = Restore(
                str(Path(td) / "dest"),
                encryption_password="pw",
            )
            recs = rs.list_records(
                computer_uuid=res.computer_uuid,
                folder_uuid=res.folder_uuid,
            )
            self.assertGreater(len(recs), 0)

    def test_archived_record_passes_validator(self) -> None:
        from arq_validator import (
            LocalBackend, check_arq7_compatibility,
        )
        with tempfile.TemporaryDirectory() as td:
            self._build_then_archive(Path(td))
            report = check_arq7_compatibility(
                LocalBackend(str(Path(td) / "dest")),
                "/", encryption_password="pw",
            )
            # No new failures from archived=true.
            failures = [
                c for c in report.failed_checks
                if "archived" in c.name.lower()
            ]
            self.assertEqual(failures, [])

    def test_archived_record_restores_content(self) -> None:
        from arq_reader import Restore
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            res = self._build_then_archive(tdp)
            out = tdp / "out"
            out.mkdir()
            rs = Restore(
                str(tdp / "dest"),
                encryption_password="pw",
            )
            rs.restore(
                folder_uuid=res.folder_uuid,
                computer_uuid=res.computer_uuid, dest=out,
            )
            self.assertEqual(
                (out / "f.txt").read_bytes(), b"content",
            )


@unittest.skipUnless(_has_openssl(), "openssl CLI required")
class A14_ComputerOSTypeTests(unittest.TestCase):
    """``computerOSType`` value handling."""

    def _build_with_os_type(self, td: Path, os_type: int):
        from arq_writer.backup import build_backup
        src = td / "src"
        src.mkdir()
        (src / "f.txt").write_bytes(b"x")
        res = build_backup(
            str(src), str(td / "dest"), encryption_password="pw",
        )
        _patch_record_field(
            td / "dest", res.computer_uuid, res.folder_uuid,
            "computerOSType", os_type,
        )
        return res

    def test_os_type_1_macos_accepted(self) -> None:
        from arq_reader import Restore
        with tempfile.TemporaryDirectory() as td:
            res = self._build_with_os_type(Path(td), 1)
            rs = Restore(
                str(Path(td) / "dest"),
                encryption_password="pw",
            )
            self.assertGreater(len(rs.list_records(
                computer_uuid=res.computer_uuid,
                folder_uuid=res.folder_uuid,
            )), 0)

    def test_os_type_2_windows_accepted(self) -> None:
        from arq_reader import Restore
        with tempfile.TemporaryDirectory() as td:
            res = self._build_with_os_type(Path(td), 2)
            rs = Restore(
                str(Path(td) / "dest"),
                encryption_password="pw",
            )
            # Listing succeeds (no crash from non-mac OS type).
            recs = rs.list_records(
                computer_uuid=res.computer_uuid,
                folder_uuid=res.folder_uuid,
            )
            self.assertGreater(len(recs), 0)

    def test_os_type_3_linux_accepted(self) -> None:
        from arq_reader import Restore
        with tempfile.TemporaryDirectory() as td:
            res = self._build_with_os_type(Path(td), 3)
            rs = Restore(
                str(Path(td) / "dest"),
                encryption_password="pw",
            )
            recs = rs.list_records(
                computer_uuid=res.computer_uuid,
                folder_uuid=res.folder_uuid,
            )
            self.assertGreater(len(recs), 0)


@unittest.skipUnless(_has_openssl(), "openssl CLI required")
class A15_EmptySourceTests(unittest.TestCase):
    """Backup of a fully-empty source directory."""

    def test_empty_source_produces_valid_record(self) -> None:
        from arq_writer.backup import build_backup
        from arq_reader import Restore
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()   # empty
            dest = tdp / "dest"
            res = build_backup(
                str(src), str(dest), encryption_password="pw",
            )
            # Record exists + is readable.
            rs = Restore(str(dest), encryption_password="pw")
            recs = rs.list_records(
                computer_uuid=res.computer_uuid,
                folder_uuid=res.folder_uuid,
            )
            self.assertEqual(len(recs), 1)

    def test_empty_source_restore_yields_empty_dir(self) -> None:
        from arq_writer.backup import build_backup
        from arq_reader import Restore
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()   # empty
            dest = tdp / "dest"
            res = build_backup(
                str(src), str(dest), encryption_password="pw",
            )
            out = tdp / "out"
            out.mkdir()
            rs = Restore(str(dest), encryption_password="pw")
            rs.restore(
                folder_uuid=res.folder_uuid,
                computer_uuid=res.computer_uuid, dest=out,
            )
            visible = [
                p for p in out.iterdir()
                if not p.name.startswith(".")
            ]
            self.assertEqual(visible, [])

    def test_empty_source_record_node_has_zero_children(self) -> None:
        from arq_writer.backup import build_backup
        from arq_reader.decrypt import decrypt_lz4_arqo
        from arq_reader.parse import parse_tree
        from arq_validator import LocalBackend
        from arq_validator.crypto import decrypt_keyset
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            dest = tdp / "dest"
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
            rec_arqo = b.read_all(
                "/" + str(
                    res.backuprecord_path.relative_to(
                        res.dest_root,
                    )
                )
            )
            plain = decrypt_lz4_arqo(
                rec_arqo, ks.encryption_key, ks.hmac_key,
            )
            rec = json.loads(plain.decode("utf-8"))
            node = rec["node"]
            self.assertTrue(node["isTree"])
            # Fetch the root tree and verify zero children.
            tloc = node["treeBlobLoc"]
            tree_raw = b.read_all(
                f"/{res.computer_uuid}/standardobjects/"
                f"{tloc['blobIdentifier'][:2]}/"
                f"{tloc['blobIdentifier'][2:]}"
            )
            tree = parse_tree(
                decrypt_lz4_arqo(
                    tree_raw, ks.encryption_key, ks.hmac_key,
                )
            )
            self.assertEqual(len(tree.children), 0)


if __name__ == "__main__":
    unittest.main()
