"""Unit tests for layout discovery against synthetic Arq trees."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from arq_validator import constants as C
from arq_validator.backend import LocalBackend
from arq_validator.layout import (
    discover_layout,
    find_latest_backuprecord,
    keyset_path,
    object_path,
)

from tests.fixtures import write_synthetic_backup


class LayoutDiscoveryTests(unittest.TestCase):
    def test_discovers_synthetic_backup(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _, _, cu = write_synthetic_backup(
                root, "pw",
                n_blobpacks=3, n_treepacks=2,
                n_standardobjects=4,
            )
            backend = LocalBackend(root)
            layouts = discover_layout(backend, "/", concurrency=2)
        self.assertEqual(len(layouts), 1)
        lay = layouts[0]
        self.assertEqual(lay.computer_uuid, cu)
        self.assertTrue(lay.has_keyset)
        self.assertEqual(len(lay.blobpacks), 3)
        self.assertEqual(len(lay.treepacks), 2)
        self.assertEqual(len(lay.standardobjects), 4)
        self.assertEqual(len(lay.backup_folder_uuids), 1)

    def test_empty_root_returns_no_layouts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            backend = LocalBackend(Path(td))
            layouts = discover_layout(backend, "/")
        self.assertEqual(layouts, [])

    def test_path_helpers(self) -> None:
        cu = "12345678-ABCD-1234-ABCD-1234567890AB"
        self.assertEqual(
            object_path("/", cu, "blobpacks", "00", "x.pack"),
            f"/{cu}/blobpacks/00/x.pack",
        )
        self.assertEqual(keyset_path("/", cu), f"/{cu}/{C.KEYSET_FILE}")

    def test_find_latest_backuprecord(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _, _, cu = write_synthetic_backup(
                root, "pw", backuprecord_num=99,
            )
            backend = LocalBackend(root)
            layouts = discover_layout(backend, "/")
            self.assertEqual(len(layouts), 1)
            folder = layouts[0].backup_folder_uuids[0]
            path = find_latest_backuprecord(backend, "/", cu, folder)
        self.assertIsNotNone(path)
        self.assertTrue(path.endswith("99.backuprecord"))

    def test_find_latest_backuprecord_missing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            backend = LocalBackend(Path(td))
            r = find_latest_backuprecord(
                backend, "/", "nope", "nope",
            )
        self.assertIsNone(r)


class BackendSafetyTests(unittest.TestCase):
    def test_path_traversal_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            backend = LocalBackend(Path(td))
            with self.assertRaises(PermissionError):
                backend.read_all("/../../etc/passwd")


if __name__ == "__main__":
    unittest.main()
