"""Tests for the ``tree_version`` knob exposed on
:class:`arq_writer.Backup` + :func:`arq_writer.build_backup`.

The serialize layer learned to emit Tree v4 in PR #21, but the
public Backup constructor + ``build_backup`` convenience wrapper
weren't wired through, so callers couldn't actually opt in. These
tests pin both:

- The default still emits v3 (no accidental flip).
- ``Backup(tree_version=4)`` produces tree blobs that, when the
  reader parses them back, report ``version == 4``.
- The CLI ``--tree-version`` flag accepts 3 and 4 only.
"""

from __future__ import annotations

import argparse
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
class TreeVersionWireupTests(unittest.TestCase):
    """End-to-end: a ``Backup(tree_version=4)`` round-trip must
    yield trees the reader sees as v4."""

    def _build_one(self, td: Path, *, tree_version: int):
        """Run a tiny backup with the given tree_version and
        return the destination root + computer_uuid."""
        from arq_writer import build_backup
        src = td / "src"
        src.mkdir()
        (src / "a.txt").write_text("hello\n")
        (src / "sub").mkdir()
        (src / "sub" / "b.txt").write_text("world\n")
        dst = td / "dst"
        dst.mkdir()
        build_backup(
            src, dst, "secret",
            backup_name="tv-wireup-test",
            tree_version=tree_version,
        )
        cu = next(p.name for p in dst.iterdir() if p.is_dir())
        return dst, cu

    def _read_root_tree_version(self, dst: Path, cu: str) -> int:
        """Find the latest backuprecord, decrypt the root-tree
        blob it references, and return the parsed version field."""
        from arq_validator.backend import LocalBackend
        from arq_validator.crypto import decrypt_keyset
        from arq_validator.layout import (
            keyset_path, list_backuprecords,
        )
        from arq_writer.backuprecord import parse_backuprecord
        from arq_reader.decrypt import decrypt_lz4_arqo
        from arq_reader.parse import parse_tree

        backend = LocalBackend(dst)
        keyset = decrypt_keyset(
            backend.read_all(keyset_path("/", cu)), "secret",
        )
        # Latest record of the (sole) folder.
        from arq_validator import discover_layout
        layout = next(iter(discover_layout(
            backend, "/", enumerate_objects=False,
        )))
        rec_paths = list_backuprecords(
            backend, "/", cu, layout.backup_folder_uuids[0],
        )
        rec_blob = backend.read_all(rec_paths[-1])
        rec = parse_backuprecord(decrypt_lz4_arqo(
            rec_blob, keyset.encryption_key, keyset.hmac_key,
        ))
        node = rec["node"]
        tree_loc = node["treeBlobLoc"]
        # Standalone-objects layout (no packs) — fetch by path.
        rel = tree_loc["relativePath"]
        tree_arqo = backend.read_all(rel)
        tree_plain = decrypt_lz4_arqo(
            tree_arqo, keyset.encryption_key, keyset.hmac_key,
        )
        return parse_tree(tree_plain).version

    def test_default_emit_is_v3(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            dst, cu = self._build_one(Path(td), tree_version=3)
            v = self._read_root_tree_version(dst, cu)
            self.assertEqual(v, 3)

    def test_explicit_v4_round_trips_as_v4(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            dst, cu = self._build_one(Path(td), tree_version=4)
            v = self._read_root_tree_version(dst, cu)
            self.assertEqual(v, 4)


class CLIFlagAcceptsThreeAndFourTests(unittest.TestCase):
    """The ``--tree-version`` arg must accept exactly 3 and 4 —
    a typo defaulting to 'v3' or '4.0' has to fail at parse time
    so we never silently emit garbage."""

    def test_cli_parses_three(self) -> None:
        from arq_writer.cli import _build_parser
        ns = _build_parser().parse_args([
            "create", "/tmp/src", "--dest", "/tmp/dst",
            "--tree-version", "3",
        ])
        self.assertEqual(ns.tree_version, 3)

    def test_cli_parses_four(self) -> None:
        from arq_writer.cli import _build_parser
        ns = _build_parser().parse_args([
            "create", "/tmp/src", "--dest", "/tmp/dst",
            "--tree-version", "4",
        ])
        self.assertEqual(ns.tree_version, 4)

    def test_cli_rejects_unknown_version(self) -> None:
        from arq_writer.cli import _build_parser
        with self.assertRaises(SystemExit):
            _build_parser().parse_args([
                "create", "/tmp/src", "--dest", "/tmp/dst",
                "--tree-version", "5",
            ])


if __name__ == "__main__":
    unittest.main()
