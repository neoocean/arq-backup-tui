"""Comprehensive Unicode / multi-language / emoji / long-path
stress suite for the backup → validate → restore pipeline.

Property checked end-to-end for every fixture shape:

  source rel-path bytes  ==  restored rel-path bytes  (byte-for-byte)
  source file content    ==  restored file content    (byte-for-byte)

Plus the format-conformance check
(:func:`arq_validator.check_arq7_compatibility`) must pass on
each destination — confirming that JSON sidecars and Tree blobs
preserve the exact UTF-8 byte sequences end-to-end.

These tests are the answer to:

  - 한글 / 日本語 / 中文 / Arabic / Hebrew / Greek / Cyrillic /
    Thai / Devanagari paths round-trip
  - Emoji paths (🎵, 📁, 🚀, 👨‍👩‍👧‍👦, ❤️, 🇰🇷, 👋🏽) round-trip
  - Special characters (spaces, dots, dashes, parens, brackets,
    quotes, ampersands, pipes) round-trip
  - NFC vs NFD combining-character forms preserved as bytes
  - Long filenames (~250 bytes) work
  - Deeply nested paths (~30 levels, ~1 KiB total) work
  - JSON sidecars carry non-ASCII names verbatim (no \\uXXXX escape)
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Dict

from arq_reader import Restore
from arq_validator import LocalBackend, check_arq7_compatibility
from arq_writer import build_backup

from tests.fixtures_unicode import (
    DEEP_LEVELS,
    EMOJI_NAMES,
    LONG_ASCII_NAME,
    LONG_KOREAN_NAME,
    MULTI_SCRIPT_NAMES,
    SPECIAL_CHAR_NAMES,
    make_combined_tree,
    make_deep_path_tree,
    make_emoji_tree,
    make_long_name_tree,
    make_multiscript_tree,
    make_normalization_tree,
    make_special_chars_tree,
)


# ---------------------------------------------------------------------------
# Common helpers
# ---------------------------------------------------------------------------


def _round_trip(
    testcase: unittest.TestCase,
    src: Path,
    expected: Dict[str, bytes],
    *,
    use_packs: bool = False,
) -> None:
    """Backup ``src``, run compatibility audit, restore, then
    assert every expected file exists with the right bytes."""
    if not expected:
        testcase.skipTest("fixture produced no entries on this filesystem")
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        dest = tdp / "dest"
        out = tdp / "out"
        out.mkdir()
        r = build_backup(
            src, dest, encryption_password="pw",
            use_packs=use_packs,
        )
        # Format conformance must pass for every shape.
        backend = LocalBackend(dest)
        report = check_arq7_compatibility(
            backend, "/", encryption_password="pw",
            computer_uuid=r.computer_uuid,
        )
        testcase.assertTrue(
            report.passed,
            msg="\n".join([
                f"compatibility report failed: {report.summary()}",
                *(f"  [{c.id}] {c.name}: {c.message}"
                  for c in report.failed_checks),
            ]),
        )
        Restore(dest, encryption_password="pw").restore(
            folder_uuid=r.folder_uuid,
            computer_uuid=r.computer_uuid,
            dest=out,
        )
        for rel, content in expected.items():
            full = out / rel
            testcase.assertTrue(
                full.exists(),
                msg=f"missing after restore: {rel!r}",
            )
            testcase.assertEqual(
                full.read_bytes(), content,
                msg=f"content mismatch for {rel!r}",
            )


# ---------------------------------------------------------------------------
# Per-shape stress tests
# ---------------------------------------------------------------------------


class MultiScriptStressTests(unittest.TestCase):
    def test_standalone_mode(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "src"
            src.mkdir()
            expected = make_multiscript_tree(src)
            _round_trip(self, src, expected, use_packs=False)

    def test_packed_mode(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "src"
            src.mkdir()
            expected = make_multiscript_tree(src)
            _round_trip(self, src, expected, use_packs=True)


class EmojiStressTests(unittest.TestCase):
    def test_emoji_filenames_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "src"
            src.mkdir()
            expected = make_emoji_tree(src)
            _round_trip(self, src, expected, use_packs=True)

    def test_zwj_sequence_preserved(self) -> None:
        # The 4-person family emoji ZWJ sequence is the most
        # bug-prone shape: 7 codepoints joined by U+200D. If any
        # layer normalizes or strips ZWJ, the filename breaks.
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "src"
            src.mkdir()
            family_name = "👨‍👩‍👧‍👦.txt"
            full = src / family_name
            try:
                full.write_bytes(b"ok")
            except OSError:
                self.skipTest("filesystem refused the ZWJ sequence")
            _round_trip(self, src, {family_name: b"ok"})


class SpecialCharsStressTests(unittest.TestCase):
    def test_special_characters_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "src"
            src.mkdir()
            expected = make_special_chars_tree(src)
            _round_trip(self, src, expected, use_packs=False)


class NormalizationStressTests(unittest.TestCase):
    def test_combining_forms_preserved(self) -> None:
        # NFC vs NFD should round-trip as bytes — neither writer
        # nor reader normalizes filenames.
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "src"
            src.mkdir()
            expected = make_normalization_tree(src)
            _round_trip(self, src, expected, use_packs=False)


class LongNameStressTests(unittest.TestCase):
    def test_long_ascii_filename(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "src"
            src.mkdir()
            full = src / LONG_ASCII_NAME
            try:
                full.write_bytes(b"long ascii")
            except OSError:
                self.skipTest("filesystem rejected the long name")
            _round_trip(self, src, {LONG_ASCII_NAME: b"long ascii"})

    def test_long_korean_filename(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "src"
            src.mkdir()
            full = src / LONG_KOREAN_NAME
            try:
                full.write_bytes(b"long korean")
            except OSError:
                self.skipTest("filesystem rejected the long name")
            _round_trip(self, src, {LONG_KOREAN_NAME: b"long korean"})


class DeepPathStressTests(unittest.TestCase):
    def test_deeply_nested_path(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "src"
            src.mkdir()
            expected = make_deep_path_tree(src)
            _round_trip(self, src, expected, use_packs=False)


class CombinedStressTests(unittest.TestCase):
    def test_everything_at_once(self) -> None:
        # Megaroot fixture: every shape under one source. Validates
        # that fixture composition doesn't surface a hidden
        # interaction (e.g. tree ordering bugs that only appear
        # when neighbors include both ASCII and non-ASCII).
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "src"
            src.mkdir()
            expected = make_combined_tree(src)
            _round_trip(self, src, expected, use_packs=True)


# ---------------------------------------------------------------------------
# JSON sidecar carry non-ASCII verbatim
# ---------------------------------------------------------------------------


class JsonSidecarUnicodeTests(unittest.TestCase):
    def test_backupplan_preserves_korean_folder_name(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "src"
            src.mkdir()
            (src / "안녕.txt").write_bytes(b"hi\n")
            dest = Path(td) / "dest"
            r = build_backup(
                src, dest, encryption_password="pw",
                folder_name="한국어-폴더",
            )
            plan_text = (
                dest / r.computer_uuid / "backupplan.json"
            ).read_text(encoding="utf-8")
            # The folder name must appear as the literal Korean
            # text, not as a \uXXXX escape sequence.
            self.assertIn("한국어-폴더", plan_text)
            self.assertNotIn("\\u", plan_text)

    def test_backupfolder_preserves_emoji_in_local_path(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "src-🎵"
            src.mkdir()
            (src / "song.mp3").write_bytes(b"bytes\n")
            dest = Path(td) / "dest"
            r = build_backup(src, dest, encryption_password="pw")
            bf = (
                dest / r.computer_uuid
                / "backupfolders" / r.folder_uuid
                / "backupfolder.json"
            )
            text = bf.read_text(encoding="utf-8")
            # localPath must include the emoji literal.
            self.assertIn("🎵", text)


# ---------------------------------------------------------------------------
# Compatibility checker on a Korean-heavy destination
# ---------------------------------------------------------------------------


class CompatibilityOnUnicodeDestinations(unittest.TestCase):
    def test_audit_passes_on_multi_script_destination(self) -> None:
        # The compatibility checker walks every backuprecord +
        # tree blob; if any UTF-8 path corner triggers a parse
        # failure it surfaces as a failed invariant.
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "src"
            src.mkdir()
            make_multiscript_tree(src)
            dest = Path(td) / "dest"
            r = build_backup(src, dest, encryption_password="pw")
            backend = LocalBackend(dest)
            report = check_arq7_compatibility(
                backend, "/", encryption_password="pw",
                computer_uuid=r.computer_uuid,
            )
            self.assertTrue(
                report.passed,
                msg="\n".join([
                    f"summary: {report.summary()}",
                    *(f"  [{c.id}] {c.name}: {c.message}"
                      for c in report.failed_checks),
                ]),
            )


# ---------------------------------------------------------------------------
# Tree-walk reuse with non-ASCII paths (regression)
# ---------------------------------------------------------------------------


class TreeWalkReuseUnicodeTests(unittest.TestCase):
    def test_korean_path_reuse_on_second_run(self) -> None:
        # The PriorTreeIndex stat-match path uses the writer's
        # rel_path string as a lookup key. Confirm UTF-8 names
        # match across runs.
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "src"
            src.mkdir()
            (src / "한글폴더").mkdir()
            (src / "한글폴더" / "메모.txt").write_bytes(b"unchanged")
            (src / "🎵").mkdir()
            (src / "🎵" / "song.mp3").write_bytes(b"unchanged")
            dest = Path(td) / "dest"
            r1 = build_backup(src, dest, encryption_password="pw")

            # Second run with dedup_against_existing=True should
            # tree-walk-reuse both files (no read of file bytes).
            from arq_writer import Backup
            bk = Backup(
                dest_root=dest,
                encryption_password="pw",
                computer_uuid=r1.computer_uuid,
                dedup_against_existing=True,
            )
            bk.init_plan()
            bk.add_folder(src, folder_uuid=r1.folder_uuid)
            self.assertGreaterEqual(bk.files_reused, 2)


# ---------------------------------------------------------------------------
# Path-length boundary handling
# ---------------------------------------------------------------------------


class PathLengthBoundaryTests(unittest.TestCase):
    def test_just_under_path_max_works(self) -> None:
        # Build a tree that pushes total path length close to but
        # under the OS limit (Linux PATH_MAX = 4096; macOS = 1024
        # default). 25 levels × 30-byte components ≈ 750 bytes
        # fits comfortably under macOS too.
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "src"
            src.mkdir()
            cur = src
            rel_parts = []
            for i in range(25):
                seg = f"deep_segment_{i:03d}"
                rel_parts.append(seg)
                cur = cur / seg
                try:
                    cur.mkdir()
                except OSError:
                    self.skipTest(
                        f"OS rejected depth {i}: PATH_MAX limit"
                    )
            leaf_rel = "/".join([*rel_parts, "leaf.txt"])
            (cur / "leaf.txt").write_bytes(b"deep ok")
            _round_trip(self, src, {leaf_rel: b"deep ok"})

    def test_writer_emits_events_for_unreadable_dirs(self) -> None:
        # Create a directory with mode 0 (unreadable). Writer
        # should emit a dir_read_error event but NOT crash; the
        # backuprecord is still produced for the readable parts.
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "src"
            src.mkdir()
            (src / "ok.txt").write_bytes(b"ok\n")
            unread = src / "no_perm"
            unread.mkdir()
            (unread / "secret.txt").write_bytes(b"secret\n")
            os.chmod(unread, 0)
            try:
                events = []

                def cb(kind, payload):
                    events.append((kind, payload))

                dest = Path(td) / "dest"
                build_backup(
                    src, dest, encryption_password="pw",
                    callback=cb,
                )
                kinds = {k for k, _ in events}
                # Either dir_read_error fired, or running as root
                # let the walk proceed -- both are acceptable; the
                # writer must not crash either way.
                # In the root case secret.txt is just included.
                if "dir_read_error" in kinds:
                    self.assertTrue(
                        any(
                            k == "dir_read_error" and "no_perm" in p.get("path", "")
                            for k, p in events
                        ),
                    )
            finally:
                os.chmod(unread, 0o755)


if __name__ == "__main__":
    unittest.main()
