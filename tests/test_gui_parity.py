"""Tests for Arq 7 GUI parity features (PR sequence in
``docs/GUI-PARITY.md``).

Phases covered:

  1. mode preservation + symlink write/restore
  2. file-size skip rules + exclusion patterns
  3. largeblobpacks/ write routing
  4a. plan list/show/delete CLI
  4b. per-folder useBuzhash toggle (chunker override per add_folder)
  4c. password change / keyset rotation
"""

from __future__ import annotations

import os
import stat as stat_mod
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from arq_reader import Restore
from arq_writer import (
    Backup,
    ExclusionRules,
    build_backup,
    rotate_keyset_password,
)


# ---------------------------------------------------------------------------
# Phase 1 — metadata + symlink
# ---------------------------------------------------------------------------


class RestoreMetadataTests(unittest.TestCase):
    def test_mode_perm_bits_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            f = src / "exec.sh"
            f.write_bytes(b"#!/bin/sh\necho hi\n")
            os.chmod(f, 0o755)
            dest = tdp / "dest"
            r = build_backup(src, dest, encryption_password="pw")
            out = tdp / "out"
            out.mkdir()
            Restore(dest, encryption_password="pw").restore(
                folder_uuid=r.folder_uuid,
                computer_uuid=r.computer_uuid,
                dest=out,
            )
            restored_mode = (out / "exec.sh").stat().st_mode
            self.assertEqual(
                stat_mod.S_IMODE(restored_mode), 0o755,
            )

    def test_mtime_round_trip_within_1s(self) -> None:
        # Already covered by existing tests but pin explicitly for
        # the metadata-restore phase.
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            f = src / "stamped.txt"
            f.write_bytes(b"old\n")
            os.utime(f, (1_000_000_000, 1_000_000_000))
            dest = tdp / "dest"
            r = build_backup(src, dest, encryption_password="pw")
            out = tdp / "out"
            out.mkdir()
            Restore(dest, encryption_password="pw").restore(
                folder_uuid=r.folder_uuid,
                computer_uuid=r.computer_uuid,
                dest=out,
            )
            self.assertAlmostEqual(
                (out / "stamped.txt").stat().st_mtime,
                1_000_000_000, delta=1,
            )


class SymlinkRoundTripTests(unittest.TestCase):
    def test_relative_symlink_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "real.txt").write_bytes(b"real content\n")
            os.symlink("real.txt", src / "link.txt")
            dest = tdp / "dest"
            r = build_backup(src, dest, encryption_password="pw")
            out = tdp / "out"
            out.mkdir()
            Restore(dest, encryption_password="pw").restore(
                folder_uuid=r.folder_uuid,
                computer_uuid=r.computer_uuid,
                dest=out,
            )
            link_path = out / "link.txt"
            self.assertTrue(link_path.is_symlink())
            self.assertEqual(os.readlink(link_path), "real.txt")
            self.assertEqual(
                (out / "real.txt").read_bytes(), b"real content\n",
            )

    def test_absolute_symlink_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            os.symlink("/tmp/some-target", src / "abs.txt")
            dest = tdp / "dest"
            r = build_backup(src, dest, encryption_password="pw")
            out = tdp / "out"
            out.mkdir()
            Restore(dest, encryption_password="pw").restore(
                folder_uuid=r.folder_uuid,
                computer_uuid=r.computer_uuid,
                dest=out,
            )
            self.assertTrue((out / "abs.txt").is_symlink())
            self.assertEqual(
                os.readlink(out / "abs.txt"),
                "/tmp/some-target",
            )

    def test_symlink_in_subdir(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            (src / "a").mkdir(parents=True)
            (src / "a" / "real.bin").write_bytes(b"deep\n")
            os.symlink("real.bin", src / "a" / "alias.bin")
            dest = tdp / "dest"
            r = build_backup(src, dest, encryption_password="pw")
            out = tdp / "out"
            out.mkdir()
            Restore(dest, encryption_password="pw").restore(
                folder_uuid=r.folder_uuid,
                computer_uuid=r.computer_uuid,
                dest=out,
            )
            self.assertTrue((out / "a" / "alias.bin").is_symlink())


# ---------------------------------------------------------------------------
# Phase 2 — file-size + exclusions
# ---------------------------------------------------------------------------


class FileSizeSkipTests(unittest.TestCase):
    def test_files_over_limit_are_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "small.bin").write_bytes(b"x" * 1024)
            (src / "big.bin").write_bytes(b"x" * (10 * 1024))
            dest = tdp / "dest"
            r = build_backup(
                src, dest, encryption_password="pw",
                max_file_bytes=2 * 1024,
            )
            out = tdp / "out"
            out.mkdir()
            Restore(dest, encryption_password="pw").restore(
                folder_uuid=r.folder_uuid,
                computer_uuid=r.computer_uuid,
                dest=out,
            )
            self.assertTrue((out / "small.bin").exists())
            self.assertFalse((out / "big.bin").exists())


class ExclusionRulesTests(unittest.TestCase):
    def test_wildcard_excludes_basename(self) -> None:
        rules = ExclusionRules.of(wildcard=("*.log",))
        self.assertTrue(rules.excludes("foo.log", is_dir=False))
        self.assertTrue(rules.excludes("a/b/foo.log", is_dir=False))
        self.assertFalse(rules.excludes("foo.txt", is_dir=False))

    def test_regex_excludes_full_path(self) -> None:
        rules = ExclusionRules.of(regex=(r".*node_modules.*",))
        self.assertTrue(
            rules.excludes("a/node_modules/x.js", is_dir=False),
        )
        self.assertFalse(
            rules.excludes("a/src/x.js", is_dir=False),
        )

    def test_gitignore_negation(self) -> None:
        rules = ExclusionRules.of(gitignore_lines=(
            "*.log",
            "!keep.log",
        ))
        self.assertTrue(rules.excludes("a.log", is_dir=False))
        self.assertFalse(rules.excludes("keep.log", is_dir=False))

    def test_gitignore_anchored_pattern(self) -> None:
        rules = ExclusionRules.of(gitignore_lines=("/cache",))
        self.assertTrue(rules.excludes("cache", is_dir=True))
        self.assertFalse(rules.excludes("a/cache", is_dir=True))

    def test_gitignore_dir_only(self) -> None:
        rules = ExclusionRules.of(gitignore_lines=("build/",))
        self.assertTrue(rules.excludes("build", is_dir=True))
        # Same name but is a file, not a dir → not excluded.
        self.assertFalse(rules.excludes("build", is_dir=False))

    def test_exclusions_drop_files_during_walk(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "keep.txt").write_bytes(b"keep\n")
            (src / "drop.log").write_bytes(b"drop\n")
            (src / "node_modules").mkdir()
            (src / "node_modules" / "junk.js").write_bytes(b"junk\n")
            dest = tdp / "dest"
            rules = ExclusionRules.of(
                wildcard=("*.log", "node_modules"),
            )
            r = build_backup(
                src, dest, encryption_password="pw",
                exclusions=rules,
            )
            out = tdp / "out"
            out.mkdir()
            Restore(dest, encryption_password="pw").restore(
                folder_uuid=r.folder_uuid,
                computer_uuid=r.computer_uuid,
                dest=out,
            )
            self.assertTrue((out / "keep.txt").exists())
            self.assertFalse((out / "drop.log").exists())
            self.assertFalse((out / "node_modules").exists())


# ---------------------------------------------------------------------------
# Phase 3 — large blob routing
# ---------------------------------------------------------------------------


class LargeBlobpacksRoutingTests(unittest.TestCase):
    def test_oversized_blob_lands_in_largeblobpacks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            # 80 KiB > our 32 KiB threshold → goes to largeblobpacks
            (src / "huge.bin").write_bytes(b"H" * (80 * 1024))
            (src / "small.bin").write_bytes(b"s\n")
            dest = tdp / "dest"
            r = build_backup(
                src, dest, encryption_password="pw",
                use_packs=True,
                large_blob_threshold=32 * 1024,
            )
            cu_root = dest / r.computer_uuid
            self.assertTrue(
                (cu_root / "largeblobpacks").is_dir(),
                "largeblobpacks dir not created",
            )
            largepacks = list(
                (cu_root / "largeblobpacks").rglob("*.pack")
            )
            self.assertGreaterEqual(len(largepacks), 1)
            # Restore must still succeed end-to-end.
            out = tdp / "out"
            out.mkdir()
            Restore(dest, encryption_password="pw").restore(
                folder_uuid=r.folder_uuid,
                computer_uuid=r.computer_uuid,
                dest=out,
            )
            self.assertEqual(
                (out / "huge.bin").read_bytes(), b"H" * (80 * 1024),
            )


# ---------------------------------------------------------------------------
# Phase 4a — plan CLI
# ---------------------------------------------------------------------------


class PlanCliTests(unittest.TestCase):
    def _run(self, args, *, cfg_dir):
        cmd = [
            sys.executable, "-m", "arq_tui",
            "--config-dir", str(cfg_dir),
            *args,
        ]
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=30,
        )

    def test_list_empty(self) -> None:
        try:
            import textual  # noqa: F401
        except ImportError:
            self.skipTest("textual not installed")
        with tempfile.TemporaryDirectory() as td:
            r = self._run(["plans", "list"], cfg_dir=td)
            self.assertEqual(r.returncode, 0)
            self.assertIn("(no plans)", r.stdout)

    def test_list_show_delete_round_trip(self) -> None:
        try:
            import textual  # noqa: F401
        except ImportError:
            self.skipTest("textual not installed")
        from arq_tui.state import Plan, PlanRegistry

        with tempfile.TemporaryDirectory() as td:
            reg = PlanRegistry(config_dir=Path(td))
            p = Plan(
                plan_id="abc-123",
                name="my-plan",
                sources=["/home/me/Documents"],
                destination_kind="local",
                destination={"path": "/Volumes/dest"},
            )
            reg.save(p)

            # list
            r = self._run(["plans", "list"], cfg_dir=td)
            self.assertEqual(r.returncode, 0)
            self.assertIn("my-plan", r.stdout)

            # show by name
            r = self._run(
                ["plans", "show", "my-plan"], cfg_dir=td,
            )
            self.assertEqual(r.returncode, 0)
            self.assertIn('"plan_id": "abc-123"', r.stdout)

            # delete with --yes
            r = self._run(
                ["plans", "delete", "my-plan", "--yes"], cfg_dir=td,
            )
            self.assertEqual(r.returncode, 0)
            self.assertEqual(reg.list_plans(), [])


# ---------------------------------------------------------------------------
# Phase 4b — per-folder useBuzhash toggle (chunker override)
# ---------------------------------------------------------------------------


class PerFolderChunkerOverrideTests(unittest.TestCase):
    def test_add_folder_can_override_chunker(self) -> None:
        from arq_writer.chunker import ChunkerConfig

        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src_a = tdp / "a"
            src_a.mkdir()
            (src_a / "a.bin").write_bytes(b"A" * (200 * 1024))
            src_b = tdp / "b"
            src_b.mkdir()
            (src_b / "b.bin").write_bytes(b"B" * (200 * 1024))
            dest = tdp / "dest"

            bk = Backup(
                dest_root=dest,
                encryption_password="pw",
                use_packs=True,
                # default: no chunker (one blob per file)
            )
            bk.init_plan()
            # Folder A: one blob per file
            bk.add_folder(src_a, folder_name="A")
            # Folder B: chunker active → one file split into multiple
            #          blobs
            bk.add_folder(
                src_b, folder_name="B",
                chunker_config=ChunkerConfig(
                    window_size=64,
                    boundary_bits=12,
                    min_chunk_size=4096,
                    max_chunk_size=131072,
                ),
            )
            # Quickest assertion: total blob count > num source files
            # only when at least one folder did chunking.
            self.assertGreater(len(bk.blob_ids), 2)

            # And restore must round-trip both folders.
            rs = Restore(dest, encryption_password="pw")
            for folder_plan in bk._folder_plans:
                fu = folder_plan["backupFolderUUID"]
                out = tdp / f"out_{fu}"
                out.mkdir()
                rs.restore(
                    folder_uuid=fu,
                    computer_uuid=bk.computer_uuid,
                    dest=out,
                )


# ---------------------------------------------------------------------------
# Phase 4c — password change
# ---------------------------------------------------------------------------


class PasswordRotationTests(unittest.TestCase):
    def test_rotate_keyset_keeps_existing_records_decryptable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "a.txt").write_bytes(b"hello\n")
            dest = tdp / "dest"
            r = build_backup(src, dest, encryption_password="pw1")

            # Rotate password.
            keyset_path = dest / r.computer_uuid / "encryptedkeyset.dat"
            old_blob = keyset_path.read_bytes()
            new_blob = rotate_keyset_password(
                old_blob, old_password="pw1", new_password="pw2",
            )
            keyset_path.write_bytes(new_blob)

            # Old password no longer decrypts.
            rs = Restore(dest, encryption_password="pw1")
            with self.assertRaises(Exception):
                rs.restore(
                    folder_uuid=r.folder_uuid,
                    computer_uuid=r.computer_uuid,
                    dest=tdp / "out_old",
                )

            # New password decrypts and restores correctly — proving
            # the underlying master keys (and therefore every
            # backuprecord and blob) didn't change.
            out = tdp / "out_new"
            out.mkdir()
            Restore(dest, encryption_password="pw2").restore(
                folder_uuid=r.folder_uuid,
                computer_uuid=r.computer_uuid,
                dest=out,
            )
            self.assertEqual(
                (out / "a.txt").read_bytes(), b"hello\n",
            )

    def test_rotate_with_wrong_old_password_raises(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "a.txt").write_bytes(b"hello\n")
            dest = tdp / "dest"
            r = build_backup(src, dest, encryption_password="pw1")
            keyset_path = (
                dest / r.computer_uuid / "encryptedkeyset.dat"
            )
            with self.assertRaises(Exception):
                rotate_keyset_password(
                    keyset_path.read_bytes(),
                    old_password="WRONG",
                    new_password="pw2",
                )


if __name__ == "__main__":
    unittest.main()
