"""C5-alt — pack file flush boundary (``max_pack_bytes``).

``PackBuilder.add`` flushes the current pack when appending
would push the buffer past ``max_pack_bytes``, AND the buffer
is non-empty. Pinned behaviour:

- Single oversized blob (≥ ``max_pack_bytes``) is placed in
  its own pack (no infinite buffer growth, no flush of an
  empty pack just to make room).
- Sequence of blobs each smaller than the threshold flushes at
  exactly the boundary (cumulative size > threshold triggers).
- ``close()`` flushes any final non-empty buffer.
- Each pack file has a unique UUID-derived path; flushes don't
  overwrite earlier packs.
"""

from __future__ import annotations

import secrets
import tempfile
import unittest
from pathlib import Path


class PackFlushBoundaryTests(unittest.TestCase):

    def _new_builder(self, td: Path, max_bytes: int):
        from arq_writer.pack_builder import PackBuilder
        return PackBuilder(
            computer_uuid="8EB255DD-09D3-43F8-8FE5-6106EBCE1A5D",
            family="blobpacks",
            dest_root=td,
            max_pack_bytes=max_bytes,
        )

    def test_below_threshold_stays_in_one_pack(self) -> None:
        """A few small blobs whose total stays under
        max_pack_bytes → one pack file."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            builder = self._new_builder(tdp, max_bytes=10_000)
            for i in range(5):
                builder.add(
                    f"{i:064d}",
                    b"ARQO" + secrets.token_bytes(500),
                )
            infos = builder.close()
            self.assertEqual(
                len(infos), 1,
                f"5 × 504 B = 2520 B < 10000 → 1 pack, "
                f"got {len(infos)}",
            )

    def test_crossing_threshold_flushes_and_starts_new(self) -> None:
        """Adding a blob that crosses the threshold flushes the
        current pack (the existing buffer's content stays in the
        previous pack) and starts a fresh pack for the new blob."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            builder = self._new_builder(tdp, max_bytes=2_000)
            # Add 3 blobs of 800 bytes each:
            # - after #1 (800): 800 ≤ 2000 → no flush
            # - after #2 (1600): 1600 ≤ 2000 → no flush
            # - adding #3 (would total 2400 > 2000) → FLUSH
            #   current pack (with blobs 1+2), start new pack
            #   with blob 3
            for i in range(3):
                builder.add(
                    f"{i:064d}",
                    b"ARQO" + secrets.token_bytes(800 - 4),
                )
            infos = builder.close()
            self.assertEqual(
                len(infos), 2,
                f"expected 2 packs after threshold crossing, "
                f"got {len(infos)}",
            )
            # First pack has 2 blobs, second has 1.
            self.assertEqual(infos[0].blob_count, 2)
            self.assertEqual(infos[1].blob_count, 1)

    def test_oversized_single_blob_gets_own_pack(self) -> None:
        """A blob whose size exceeds ``max_pack_bytes`` by itself
        gets placed in its own pack. No infinite loop, no
        empty pack flushed first."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            builder = self._new_builder(tdp, max_bytes=1_000)
            oversized = b"ARQO" + secrets.token_bytes(5_000)
            builder.add("a" * 64, oversized)
            infos = builder.close()
            self.assertEqual(len(infos), 1)
            self.assertEqual(infos[0].size, len(oversized))

    def test_close_flushes_final_buffer(self) -> None:
        """``close()`` writes out any pending non-empty buffer."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            builder = self._new_builder(tdp, max_bytes=1_000_000)
            builder.add("a" * 64, b"ARQO" + b"x" * 96)
            infos = builder.close()
            self.assertEqual(len(infos), 1)
            self.assertEqual(infos[0].blob_count, 1)

    def test_close_with_no_adds_returns_empty(self) -> None:
        """``close()`` on a never-add()'d builder doesn't emit
        any pack files."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            builder = self._new_builder(tdp, max_bytes=1_000)
            infos = builder.close()
            self.assertEqual(infos, [])

    def test_each_pack_has_unique_path(self) -> None:
        """Multiple flushes produce distinct pack file paths
        (UUID-derived). Pin that no two packs collide."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            builder = self._new_builder(tdp, max_bytes=500)
            # 4 blobs of 400 bytes each = 4 flushes (one per blob
            # since each pushes total over 500).
            for i in range(4):
                builder.add(
                    f"{i:064d}",
                    b"ARQO" + secrets.token_bytes(396),
                )
            infos = builder.close()
            paths = {info.relative_path for info in infos}
            self.assertEqual(
                len(paths), len(infos),
                f"some packs share a path: "
                f"{[info.relative_path for info in infos]}",
            )


if __name__ == "__main__":
    unittest.main()
