"""Tests for the expanded ``arq-backup`` CLI flags.

The CLI is invoked as a subprocess so argv parsing + module
loading is exercised end-to-end. Each scenario builds a small
real source tree, runs the CLI, then runs Restore against the
result to confirm correctness.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from arq_reader import Restore


def _run_cli(argv, *, cwd=None):
    """Run `python -m arq_writer.cli <argv>` and return CompletedProcess."""
    return subprocess.run(
        [sys.executable, "-m", "arq_writer.cli", *argv],
        capture_output=True, text=True, timeout=120, cwd=cwd,
    )


def _make_tree(root: Path) -> None:
    (root / "include").mkdir(parents=True)
    (root / "include" / "keep.txt").write_bytes(b"keep\n")
    (root / "logs").mkdir()
    (root / "logs" / "trace.log").write_bytes(b"log\n")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "junk.js").write_bytes(b"x")
    (root / "tiny.txt").write_bytes(b"t")
    (root / "huge.bin").write_bytes(b"H" * (1024 * 64))


class CliBackupNewFlagsTests(unittest.TestCase):

    def test_use_packs_flag(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            _make_tree(src)
            dest = tdp / "dest"
            r = _run_cli([
                "create", str(src),
                "--dest", str(dest),
                "--password", "pw",
                "--use-packs",
            ])
            self.assertEqual(
                r.returncode, 0,
                msg=f"stdout={r.stdout}\nstderr={r.stderr}",
            )
            out = json.loads(r.stdout)
            cu = out["computer_uuid"]
            # Pack mode → blobpacks/ exists, standardobjects/ has no
            # data blobs (just the dir itself, possibly empty).
            self.assertTrue((dest / cu / "blobpacks").is_dir())

    def test_chunker_arq_v7_41_flag(self) -> None:
        # 64 KiB file with the Arq.app v7.41 chunker → may be split
        # into multiple chunks. We just verify the flag accepted +
        # backup completes + restore matches.
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "f.bin").write_bytes(b"X" * (256 * 1024))
            dest = tdp / "dest"
            r = _run_cli([
                "create", str(src),
                "--dest", str(dest),
                "--password", "pw",
                "--use-packs",
                "--chunker", "arq_v7_41",
            ])
            self.assertEqual(r.returncode, 0,
                             msg=f"stderr={r.stderr}")
            out = json.loads(r.stdout)
            # Restore + content check
            target = tdp / "out"
            target.mkdir()
            Restore(dest, encryption_password="pw").restore(
                folder_uuid=out["folder_uuid"],
                computer_uuid=out["computer_uuid"],
                dest=target,
            )
            self.assertEqual(
                (target / "f.bin").read_bytes(), b"X" * (256 * 1024),
            )

    def test_max_file_bytes_skips_large(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            _make_tree(src)
            dest = tdp / "dest"
            r = _run_cli([
                "create", str(src),
                "--dest", str(dest),
                "--password", "pw",
                "--max-file-bytes", "16",     # tiny.txt fits, huge.bin doesn't
            ])
            self.assertEqual(r.returncode, 0,
                             msg=f"stderr={r.stderr}")
            out = json.loads(r.stdout)
            target = tdp / "out"
            target.mkdir()
            Restore(dest, encryption_password="pw").restore(
                folder_uuid=out["folder_uuid"],
                computer_uuid=out["computer_uuid"],
                dest=target,
            )
            self.assertTrue((target / "tiny.txt").exists())
            self.assertFalse((target / "huge.bin").exists())

    def test_exclude_glob(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            _make_tree(src)
            dest = tdp / "dest"
            r = _run_cli([
                "create", str(src),
                "--dest", str(dest),
                "--password", "pw",
                "--exclude-glob", "*.log",
                "--exclude-glob", "node_modules",
            ])
            self.assertEqual(r.returncode, 0,
                             msg=f"stderr={r.stderr}")
            out = json.loads(r.stdout)
            target = tdp / "out"
            target.mkdir()
            Restore(dest, encryption_password="pw").restore(
                folder_uuid=out["folder_uuid"],
                computer_uuid=out["computer_uuid"],
                dest=target,
            )
            self.assertTrue((target / "include" / "keep.txt").exists())
            self.assertFalse((target / "logs" / "trace.log").exists())
            self.assertFalse((target / "node_modules").exists())

    def test_exclude_regex(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            _make_tree(src)
            dest = tdp / "dest"
            r = _run_cli([
                "create", str(src),
                "--dest", str(dest),
                "--password", "pw",
                "--exclude-regex", r".*\.log",
            ])
            self.assertEqual(r.returncode, 0,
                             msg=f"stderr={r.stderr}")
            out = json.loads(r.stdout)
            target = tdp / "out"
            target.mkdir()
            Restore(dest, encryption_password="pw").restore(
                folder_uuid=out["folder_uuid"],
                computer_uuid=out["computer_uuid"],
                dest=target,
            )
            self.assertFalse((target / "logs" / "trace.log").exists())

    def test_exclude_from_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            _make_tree(src)
            ignore = tdp / "ignore.txt"
            ignore.write_text(
                "# comment\n"
                "*.log\n"
                "node_modules/\n"
            )
            dest = tdp / "dest"
            r = _run_cli([
                "create", str(src),
                "--dest", str(dest),
                "--password", "pw",
                "--exclude-from", str(ignore),
            ])
            self.assertEqual(r.returncode, 0,
                             msg=f"stderr={r.stderr}")
            out = json.loads(r.stdout)
            target = tdp / "out"
            target.mkdir()
            Restore(dest, encryption_password="pw").restore(
                folder_uuid=out["folder_uuid"],
                computer_uuid=out["computer_uuid"],
                dest=target,
            )
            self.assertFalse((target / "logs" / "trace.log").exists())
            self.assertFalse((target / "node_modules").exists())

    def test_dedup_against_existing(self) -> None:
        # Run twice with the same UUIDs; second run should reuse
        # the first run's keyset (proving --dedup-against-existing
        # plumbed through).
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            _make_tree(src)
            dest = tdp / "dest"

            # Run 1: collect uuids
            r1 = _run_cli([
                "create", str(src),
                "--dest", str(dest),
                "--password", "pw",
            ])
            self.assertEqual(r1.returncode, 0)
            out1 = json.loads(r1.stdout)
            keyset_path = (
                dest / out1["computer_uuid"] / "encryptedkeyset.dat"
            )
            blob1 = keyset_path.read_bytes()

            # Run 2: same uuids + --dedup-against-existing should
            # NOT rewrite the keyset (proving the path was reused).
            r2 = _run_cli([
                "create", str(src),
                "--dest", str(dest),
                "--password", "pw",
                "--computer-uuid", out1["computer_uuid"],
                "--dedup-against-existing",
            ])
            self.assertEqual(r2.returncode, 0)
            blob2 = keyset_path.read_bytes()
            self.assertEqual(
                blob1, blob2,
                "keyset bytes changed despite --dedup-against-existing",
            )

    def test_use_apfs_snapshot_falls_back_on_linux(self) -> None:
        # On Linux the flag must be a no-op (skip event), and the
        # backup still completes successfully.
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "a.txt").write_bytes(b"hello\n")
            dest = tdp / "dest"
            r = _run_cli([
                "create", str(src),
                "--dest", str(dest),
                "--password", "pw",
                "--use-apfs-snapshot",
                "--json-events",
            ])
            self.assertEqual(r.returncode, 0,
                             msg=f"stderr={r.stderr}")
            # The skip event should appear in JSON-events stderr.
            # (May be platform-skipped on real macOS — that's fine.)
            if sys.platform != "darwin":
                self.assertIn(
                    "apfs_snapshot_skipped", r.stderr,
                )
            out = json.loads(r.stdout)
            target = tdp / "out"
            target.mkdir()
            Restore(dest, encryption_password="pw").restore(
                folder_uuid=out["folder_uuid"],
                computer_uuid=out["computer_uuid"],
                dest=target,
            )
            self.assertEqual(
                (target / "a.txt").read_bytes(), b"hello\n",
            )


if __name__ == "__main__":
    unittest.main()
