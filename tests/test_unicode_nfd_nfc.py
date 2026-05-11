"""E5 — Unicode NFD vs NFC normalisation policy.

Three filesystems treat the same logical filename in different
ways:

- **APFS** (macOS modern, default on Big Sur ≥): byte-preserving.
  Whatever encoding the caller passed is what ``listdir`` returns.
- **HFS+** (macOS legacy): NFD-normalises every filename on store.
  ``"한글.txt"`` (NFC) and ``"한글.txt"`` (jamo NFD) collapse to
  the same on-disk name.
- **ext4 / btrfs / xfs / NTFS** (Linux + Windows): byte-preserving.

The writer's contract is **byte preservation** — neither the
walker, the Tree serialiser, nor the restore re-normalises a
filename. This module pins three properties:

1. ``capture_xattrs``-equivalent for paths: a NFD-encoded name
   read off the filesystem is stored byte-identical in the Tree
   blob, and reads back byte-identical on restore.
2. **NFC and NFD coexist**: a source directory with both
   ``unicodedata.normalize("NFC", x)`` and
   ``unicodedata.normalize("NFD", x)`` versions of the same
   logical name survives → restore preserves the distinction
   on a byte-preserving FS. (HFS+ skips this test — its NFD-
   normalisation collapses them at the source FS layer.)
3. **Tree blob bytes are unchanged**: the Tree binary for an NFD
   name contains the NFD bytes verbatim (the on-disk format
   carries the raw filename, not a re-normalised version).

The existing :class:`tests.test_unicode_path_stress.NormalizationStressTests`
covers (1) for the four pre-built names ``español.txt``,
``español_nfd.txt``, ``한글.txt``, ``한글_jamo.txt`` — but those
are *different filenames*, not the same logical name in two
encodings. This module covers the harder case: same logical name,
distinct encodings, both present.
"""

from __future__ import annotations

import os
import platform
import sys
import tempfile
import unicodedata
import unittest
from pathlib import Path

from arq_reader import Restore
from arq_writer.backup import build_backup


def _filesystem_is_byte_preserving(td: Path) -> bool:
    """Probe: write a file with an NFD-encoded name, then read
    ``os.listdir`` back. If the OS gave us back NFD bytes, the FS
    is byte-preserving. If it gave back NFC bytes (HFS+), the FS
    is NFD-normalising.

    Returns True iff the FS preserves whatever bytes we wrote.
    """
    test_name_nfc = "ñ-test"
    test_name_nfd = unicodedata.normalize("NFD", test_name_nfc)
    assert test_name_nfc != test_name_nfd, (
        "test setup bug — NFC and NFD encodings must differ"
    )
    target = td / test_name_nfd
    try:
        target.write_bytes(b"")
    except OSError:
        return False
    listed = os.listdir(td)
    # The FS preserved our NFD bytes iff what we got back
    # equals what we wrote.
    return test_name_nfd in listed


class NFDPathBytePreservationTests(unittest.TestCase):
    """A NFD-encoded source path round-trips byte-identical
    through backup → restore on byte-preserving filesystems."""

    def test_nfd_korean_name_round_trips_byte_for_byte(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            if not _filesystem_is_byte_preserving(tdp):
                self.skipTest(
                    "this filesystem normalises filenames "
                    "(probably HFS+); NFD byte preservation "
                    "isn't testable here"
                )
            src = tdp / "src"
            src.mkdir()
            nfc_name = "한글.txt"
            nfd_name = unicodedata.normalize("NFD", nfc_name)
            self.assertNotEqual(nfc_name, nfd_name)
            self.assertNotEqual(
                nfc_name.encode("utf-8"), nfd_name.encode("utf-8"),
            )
            (src / nfd_name).write_bytes(b"nfd content\n")

            dest = tdp / "dest"
            build_backup(
                str(src), str(dest), encryption_password="pw",
            )
            out = tdp / "out"
            out.mkdir()
            rs = Restore(str(dest), encryption_password="pw")
            layouts = rs.layouts()
            rs.restore(
                folder_uuid=layouts[0].backup_folder_uuids[0],
                computer_uuid=layouts[0].computer_uuid, dest=out,
            )

            listed = os.listdir(out)
            self.assertIn(
                nfd_name, listed,
                f"restored dir must contain the NFD-encoded name "
                f"verbatim; got {listed!r}",
            )
            # And specifically NOT the NFC equivalent (proves no
            # silent normalisation).
            self.assertNotIn(
                nfc_name, listed,
                "restored dir must NOT contain the NFC name — that "
                "would mean somebody normalised on the round-trip",
            )
            self.assertEqual(
                (out / nfd_name).read_bytes(), b"nfd content\n",
            )


class NFCAndNFDCoexistTests(unittest.TestCase):
    """Source directory with both NFC and NFD versions of the
    same logical name. Both must survive as distinct files
    through backup → restore."""

    def test_both_encodings_survive_as_distinct_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            if not _filesystem_is_byte_preserving(tdp):
                self.skipTest(
                    "this filesystem normalises filenames; "
                    "NFC + NFD coexistence isn't testable here"
                )
            src = tdp / "src"
            src.mkdir()
            base = "한글"
            nfc = unicodedata.normalize("NFC", base) + ".txt"
            nfd = unicodedata.normalize("NFD", base) + ".txt"
            self.assertNotEqual(nfc, nfd)
            (src / nfc).write_bytes(b"nfc-version\n")
            (src / nfd).write_bytes(b"nfd-version\n")

            # Source check. On macOS APFS, even though stored
            # filename bytes are preserved, the directory's name-
            # resolution layer folds NFC + NFD of the same logical
            # name to a single entry — the second write replaces
            # the first. This test exercises a property only
            # available on truly form-sensitive filesystems (most
            # Linux distros' ext4 / btrfs / xfs).
            listed_src = set(os.listdir(src))
            if listed_src != {nfc, nfd}:
                self.skipTest(
                    f"this filesystem folds NFC + NFD at the "
                    f"name-resolution layer (likely macOS APFS); "
                    f"listed {listed_src!r}, expected {{nfc, nfd}}"
                )

            dest = tdp / "dest"
            build_backup(
                str(src), str(dest), encryption_password="pw",
            )
            out = tdp / "out"
            out.mkdir()
            rs = Restore(str(dest), encryption_password="pw")
            layouts = rs.layouts()
            rs.restore(
                folder_uuid=layouts[0].backup_folder_uuids[0],
                computer_uuid=layouts[0].computer_uuid, dest=out,
            )

            listed_out = set(os.listdir(out))
            self.assertEqual(
                listed_out, {nfc, nfd},
                "both NFC and NFD versions must survive as "
                "distinct entries; got "
                f"{listed_out!r}",
            )
            # And contents match — proves the backup didn't
            # cross-contaminate them.
            self.assertEqual(
                (out / nfc).read_bytes(), b"nfc-version\n",
            )
            self.assertEqual(
                (out / nfd).read_bytes(), b"nfd-version\n",
            )


class TreeBlobPreservesNFDBytesTests(unittest.TestCase):
    """The Tree binary blob carries the raw filename bytes. A
    parse → write round-trip of a tree containing an NFD entry
    must preserve those bytes verbatim — no silent NFC-normalise
    inside the serialiser."""

    def test_tree_serialise_preserves_nfd_filename_bytes(self) -> None:
        from arq_reader.parse import parse_tree
        from arq_writer.serialize import write_tree
        from arq_writer.types import FileNode, Tree, TreeChild

        nfd_name = unicodedata.normalize("NFD", "한글.txt")
        nfd_bytes = nfd_name.encode("utf-8")
        # The decomposed form is longer in bytes than the
        # precomposed form (precomposed Korean is 3 bytes per
        # syllable; jamo is 3 bytes per jamo × 3 jamos per syllable
        # ≈ 9 bytes per syllable). Sanity-check the test setup.
        self.assertGreater(
            len(nfd_bytes),
            len("한글.txt".encode("utf-8")),
            "NFD bytes should be longer than NFC bytes for Korean",
        )

        tree = Tree(children=[
            TreeChild(
                name=nfd_name,
                node=FileNode(itemSize=0, mac_st_mode=0o100644),
            ),
        ])
        blob = write_tree(tree)
        # The NFD UTF-8 bytes appear verbatim in the serialized
        # tree.
        self.assertIn(nfd_bytes, blob)
        # Parse round-trip: re-emit equals first emit.
        re_parsed = parse_tree(blob)
        self.assertEqual(re_parsed.children[0].name, nfd_name)
        re_emit = write_tree(re_parsed)
        self.assertEqual(re_emit, blob)


if __name__ == "__main__":
    unittest.main()
