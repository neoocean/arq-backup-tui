"""End-to-end tests that drive Restore + Backup through an injected
``Backend`` instance, exercising the same code path SFTP would use.

We use ``LocalBackend`` rooted at a temp directory as the test
backend — the *I/O contract* the writer/reader exercise is exactly
what ``SftpBackend`` would receive in production. Live SFTP tests
exist in ``test_sftp.py``; those need an actual ssh server so we
keep them disabled in the default suite.

Three goals to lock in:

1. ``Backup(backend=...)`` produces a destination indistinguishable
   from the default LocalBackend path — round-trip through Restore
   reads back the source byte-for-byte.
2. ``Restore(backend=...)`` reads a destination written by either
   the default-backend or backend-injected ``Backup``.
3. ``dedup_against_existing=True`` works against a backend-driven
   destination — the keyset, standardobjects, and prior tree are
   all reachable through ``backend.read_all`` / ``list_dir`` /
   ``read_range``.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from arq_reader import Restore
from arq_validator.backend import LocalBackend
from arq_writer import Backup, build_backup


def _make_tree(root: Path) -> None:
    (root / "subdir").mkdir(parents=True)
    (root / "alpha.txt").write_bytes(b"alpha content " * 50)
    (root / "beta.txt").write_bytes(b"beta\n")
    (root / "subdir" / "gamma.txt").write_bytes(b"gamma " * 30)


class BackupViaInjectedBackendTests(unittest.TestCase):
    def test_writer_routes_all_writes_through_backend(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            _make_tree(src)
            dest = tdp / "dest"
            dest.mkdir()
            # Inject a LocalBackend; for production SFTP this is
            # SftpBackend, but the writer code path is identical.
            backend = LocalBackend(dest)
            bk = Backup(
                dest_root="/", encryption_password="pw",
                backend=backend,
            )
            bk.init_plan()
            bk.add_folder(src, folder_name="root")
            # Sanity: keyset must exist on disk.
            self.assertTrue(
                (dest / bk.computer_uuid / "encryptedkeyset.dat").exists()
            )
            # standardobjects/ must be populated.
            so = dest / bk.computer_uuid / "standardobjects"
            shards = list(so.iterdir())
            self.assertGreater(len(shards), 0)
            # And restore round-trips (using a default-backend Restore
            # so we exercise both shapes in a single test).
            out = tdp / "out"
            out.mkdir()
            rs = Restore(dest, encryption_password="pw")
            (computer, folder), = [
                (lay.computer_uuid, fu)
                for lay in rs.layouts()
                for fu in lay.backup_folder_uuids
            ]
            rs.restore(folder_uuid=folder, dest=out, computer_uuid=computer)
            self.assertEqual(
                (out / "alpha.txt").read_bytes(),
                b"alpha content " * 50,
            )
            self.assertEqual(
                (out / "subdir" / "gamma.txt").read_bytes(),
                b"gamma " * 30,
            )

    def test_writer_packed_mode_via_injected_backend(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            _make_tree(src)
            dest = tdp / "dest"
            dest.mkdir()
            backend = LocalBackend(dest)
            bk = Backup(
                dest_root="/", encryption_password="pw",
                use_packs=True, backend=backend,
            )
            bk.init_plan()
            bk.add_folder(src, folder_name="root")
            # Pack files must be on disk.
            blobpacks = dest / bk.computer_uuid / "blobpacks"
            self.assertTrue(blobpacks.is_dir())
            self.assertGreater(
                sum(1 for _ in blobpacks.rglob("*.pack")), 0
            )
            # Restore round-trips.
            out = tdp / "out"
            out.mkdir()
            rs = Restore(dest, encryption_password="pw")
            (computer, folder), = [
                (lay.computer_uuid, fu)
                for lay in rs.layouts()
                for fu in lay.backup_folder_uuids
            ]
            rs.restore(folder_uuid=folder, dest=out, computer_uuid=computer)
            self.assertEqual(
                (out / "beta.txt").read_bytes(), b"beta\n",
            )


class RestoreViaInjectedBackendTests(unittest.TestCase):
    def test_reader_uses_injected_backend(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            _make_tree(src)
            dest = tdp / "dest"
            r1 = build_backup(src, dest, encryption_password="pw")
            backend = LocalBackend(dest)
            # The first positional arg of Restore is unused when a
            # backend is injected (the backend has its own root);
            # pass "/" for clarity.
            rs = Restore("/", encryption_password="pw", backend=backend)
            out = tdp / "out"
            out.mkdir()
            rs.restore(
                folder_uuid=r1.folder_uuid,
                dest=out,
                computer_uuid=r1.computer_uuid,
            )
            self.assertEqual(
                (out / "alpha.txt").read_bytes(),
                b"alpha content " * 50,
            )


class DedupAcrossBackendTests(unittest.TestCase):
    def test_dedup_against_existing_works_via_backend(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            _make_tree(src)
            dest = tdp / "dest"
            dest.mkdir()
            backend = LocalBackend(dest)
            # First run: write everything through the backend.
            bk1 = Backup(
                dest_root="/", encryption_password="pw",
                backend=backend,
            )
            bk1.init_plan()
            bk1.add_folder(src, folder_name="root")
            # Capture first-run blob_ids before reusing the backend.
            r1_blob_ids = set(bk1.blob_ids)
            r1_cu = bk1.computer_uuid
            r1_folder = next(iter(bk1._folder_plans))["backupFolderUUID"]
            # Second run: dedup_against_existing + same UUIDs through
            # a fresh backend (mimicking a re-connection in SFTP land).
            backend2 = LocalBackend(dest)
            bk2 = Backup(
                dest_root="/", encryption_password="pw",
                computer_uuid=r1_cu,
                backend=backend2,
                dedup_against_existing=True,
            )
            bk2.init_plan()
            bk2.add_folder(src, folder_uuid=r1_folder, folder_name="root")
            # Tree-walk reuse must have fired — reused count covers
            # every leaf.
            self.assertGreaterEqual(bk2.files_reused, 3)
            # The keyset wasn't rewritten (proves _try_load_existing_keyset
            # fed bk2 the same keys as bk1).
            self.assertTrue(
                all(bid in r1_blob_ids for bid in bk2.blob_ids),
                f"new blob_ids in run 2: "
                f"{set(bk2.blob_ids) - r1_blob_ids}",
            )


if __name__ == "__main__":
    unittest.main()
