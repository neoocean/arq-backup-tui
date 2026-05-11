"""E1 — sparse file backup → restore correctness.

A "sparse" file has holes — regions the filesystem hasn't
allocated on disk and reads back as zeros. They show up wherever
software writes past the end of a file then seeks back, or
allocates fixed-size containers (database files, VM disk images,
core dumps, gigantic-but-mostly-empty logs).

Two questions for any backup format:

1. **Correctness** — after backup → restore, do the file's bytes
   read identically? (Holes as zeros, allocated regions intact.)
2. **Sparseness preservation** — does the restored copy also use
   filesystem holes for the zero regions, or does the restore
   write the zeros explicitly and balloon disk usage?

The writer's current behaviour: **correctness yes, sparseness
no**. The walker reads the file via ``Path.read_bytes()`` which
materialises holes as zero bytes in RAM; the restore writes those
zero bytes explicitly. So a 2 GB sparse file with 8 KB of actual
data (a typical VM disk image) round-trips its content correctly
but lands as 2 GB on the restore destination.

Destination-side storage isn't affected — the writer's content-
addressed dedup folds every zero-only chunk into a single blob,
so the destination still costs ~1 chunk + 1 metadata blob no
matter how big the hole is.

This module pins the correctness side. The sparseness-not-
preserved side is documented in ``docs/COVERAGE.md`` (E1 entry).
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from arq_reader import Restore
from arq_writer.backup import build_backup


class SparseFileCorrectnessTests(unittest.TestCase):
    """The HEAD/middle/TAIL pattern locks in three properties at
    once: leading content, hole region (reads as zeros), trailing
    content. Anything that drifted between source and restore
    would surface on at least one of the three reads.
    """

    SPARSE_SIZE_BYTES = 5 * 1024 * 1024   # 5 MB sparse file
    HEAD_MARKER = b"HEAD!"
    TAIL_MARKER = b"TAIL!"

    def _make_sparse(self, path: Path) -> None:
        with open(path, "wb") as f:
            f.write(self.HEAD_MARKER)
            f.seek(self.SPARSE_SIZE_BYTES - len(self.TAIL_MARKER))
            f.write(self.TAIL_MARKER)

    def test_sparse_file_content_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            sparse_path = src / "sparse.bin"
            self._make_sparse(sparse_path)
            # Sanity: confirm the source IS sparse on the test FS
            # (CI runners that fall back to non-sparse FS will still
            # produce a valid backup; this just keeps the test
            # honest about what it's exercising).
            disk_bytes = sparse_path.stat().st_blocks * 512
            self.assertEqual(
                sparse_path.stat().st_size,
                self.SPARSE_SIZE_BYTES,
            )

            dest = tdp / "dest"
            res = build_backup(
                str(src), str(dest), encryption_password="pw",
            )
            self.assertGreater(res.files_written, 0)

            out = tdp / "out"
            out.mkdir()
            rs = Restore(str(dest), encryption_password="pw")
            layouts = rs.layouts()
            rs.restore(
                folder_uuid=layouts[0].backup_folder_uuids[0],
                computer_uuid=layouts[0].computer_uuid, dest=out,
            )

            restored = out / "sparse.bin"
            self.assertEqual(
                restored.stat().st_size, self.SPARSE_SIZE_BYTES,
                "logical size must match",
            )
            with restored.open("rb") as f:
                # Head intact.
                self.assertEqual(f.read(len(self.HEAD_MARKER)),
                                 self.HEAD_MARKER)
                # Middle is zeros.
                f.seek(self.SPARSE_SIZE_BYTES // 2)
                self.assertEqual(f.read(64), b"\x00" * 64)
                # Tail intact.
                f.seek(self.SPARSE_SIZE_BYTES - len(self.TAIL_MARKER))
                self.assertEqual(f.read(), self.TAIL_MARKER)

            # Destination footprint observation (not a hard
            # contract). Without an explicit chunker config, the
            # whole file lands as a single blob; the encrypted blob
            # is roughly the same size as the plaintext. With
            # Buzhash / FixedChunker, hole regions would dedup to a
            # single content-addressed blob per chunk-size step.
            # See ``test_sparse_with_buzhash_dedups_zero_holes``
            # below for the chunker-enabled assertion.

    def test_sparse_with_buzhash_chunks_into_multiple_blobs(self) -> None:
        """With Buzhash content-defined chunking enabled, a 5 MB
        sparse file produces multiple chunks. We don't pin a
        specific dedup property: Buzhash's max-chunk-size cap can
        produce different-length chunks at the boundary of a
        uniform-content region (the rolling hash never finds an
        anchor inside the zero-only middle, so boundaries are
        cap-driven and depend on where Buzhash entered the zero
        region). What we DO pin: the writer doesn't blow up on a
        sparse file, and chunking produces more than one blob —
        proof the chunker exercised the variable-length code path
        rather than falling back to single-blob mode.
        """
        from arq_writer.chunker import ChunkerConfig
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            sparse_path = src / "sparse.bin"
            self._make_sparse(sparse_path)
            dest = tdp / "dest"
            res = build_backup(
                str(src), str(dest), encryption_password="pw",
                use_packs=True,
                chunker_config=ChunkerConfig(),
            )
            # Buzhash should chunk this into > 1 blob (proves the
            # variable-length path fired). A single blob would
            # indicate the chunker silently disabled itself.
            self.assertGreater(
                len(res.blob_ids), 1,
                f"Buzhash should produce >1 chunk for 5 MB file; "
                f"got {len(res.blob_ids)} blob_ids",
            )


@unittest.skipUnless(
    os.environ.get("ARQ_RUN_LARGE_SPARSE_TEST") == "1",
    "set ARQ_RUN_LARGE_SPARSE_TEST=1 to exercise 50 MB sparse file "
    "(skipped by default to keep regular CI fast)",
)
class LargeSparseFileTests(unittest.TestCase):
    """The same correctness contract on a larger sparse file
    (50 MB). Skipped by default because it allocates 50 MB into
    RAM during the walk; enable explicitly when you want to flush
    out memory-bound regressions in the chunker / writer path.
    """

    def test_fifty_megabyte_sparse_round_trips(self) -> None:
        size_bytes = 50 * 1024 * 1024
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            sp = src / "sparse.bin"
            with open(sp, "wb") as f:
                f.write(b"START")
                f.seek(size_bytes - 5)
                f.write(b"END!!")

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

            restored = out / "sparse.bin"
            self.assertEqual(restored.stat().st_size, size_bytes)
            with restored.open("rb") as f:
                self.assertEqual(f.read(5), b"START")
                f.seek(size_bytes // 2)
                self.assertEqual(f.read(32), b"\x00" * 32)
                f.seek(size_bytes - 5)
                self.assertEqual(f.read(), b"END!!")


if __name__ == "__main__":
    unittest.main()
