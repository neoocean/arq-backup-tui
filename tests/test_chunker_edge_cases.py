"""G3 + G4 + G5 — chunker boundary cases.

The Buzhash chunker has tunable ``min_chunk_size`` /
``max_chunk_size`` clamps and content-driven boundary cuts. The
tests here pin three corners:

- **G3 — min/max_chunk_size boundary behaviour**:
    - Input shorter than ``min_chunk_size`` → exactly one chunk.
    - Input between ``min`` and ``max`` → some content-defined
      number of chunks, each within [some_tail_min..max_chunk_size].
    - Input longer than ``max_chunk_size`` and lacking boundary
      hits → force-close at max_chunk_size.
    - Empty input → zero chunks.

- **G4 — Buzhash on contrived content**:
    - Repeating short pattern: chunk boundaries are deterministic
      and reproducible; same input → same chunk sequence.
    - Monotonic byte sequence (0..255 repeated): chunker doesn't
      loop forever; produces finite chunks.
    - All-same-byte input: chunker yields chunks at max_chunk_size
      (no rolling-hash boundary ever fires).

- **G5 — Cross-chunker dedup invariant**:
    - The FixedChunker (Arq.app v8's useBuzhash=False mode) and
      Buzhash produce different chunk sequences for the same
      input, BUT a fresh chunker invocation with the same config
      on the same input always yields the same sequence —
      pinning determinism is the dedup contract.
"""

from __future__ import annotations

import os
import random
import unittest


class G3_MinMaxBoundaryTests(unittest.TestCase):
    """min/max_chunk_size boundary corner cases."""

    def test_input_shorter_than_min_chunk_size_yields_one(
        self,
    ) -> None:
        from arq_writer.chunker import Buzhash, ChunkerConfig
        cfg = ChunkerConfig(
            window_size=48,
            boundary_bits=10,  # ~1KB average
            min_chunk_size=4 * 1024,
            max_chunk_size=1024 * 1024,
        )
        c = Buzhash(cfg)
        data = b"X" * 100  # << min_chunk_size
        chunks = list(c.chunk(data))
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0], data)

    def test_empty_input_yields_zero_chunks(self) -> None:
        from arq_writer.chunker import Buzhash
        chunks = list(Buzhash().chunk(b""))
        self.assertEqual(chunks, [])

    def test_input_equals_min_chunk_size_yields_one(self) -> None:
        from arq_writer.chunker import Buzhash, ChunkerConfig
        cfg = ChunkerConfig(min_chunk_size=4096)
        c = Buzhash(cfg)
        data = b"Y" * 4096
        chunks = list(c.chunk(data))
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0], data)

    def test_all_same_byte_force_closes_at_max_chunk_size(
        self,
    ) -> None:
        """All-same-byte content has a constant rolling-hash
        value — never hits the boundary mask. Chunker must
        force-close at max_chunk_size."""
        from arq_writer.chunker import Buzhash, ChunkerConfig
        cfg = ChunkerConfig(
            window_size=48,
            boundary_bits=20,  # very unlikely to ever hit
            min_chunk_size=4 * 1024,
            max_chunk_size=8 * 1024,
        )
        c = Buzhash(cfg)
        # 3x max, ensures at least 2 force-closes.
        data = b"A" * (cfg.max_chunk_size * 3)
        chunks = list(c.chunk(data))
        # Expect exactly 3 chunks of max_chunk_size.
        self.assertEqual(len(chunks), 3)
        for i, ch in enumerate(chunks):
            self.assertEqual(
                len(ch), cfg.max_chunk_size,
                f"chunk[{i}] size {len(ch)} != max {cfg.max_chunk_size}",
            )
        self.assertEqual(b"".join(chunks), data)

    def test_min_chunk_below_window_size_raises(self) -> None:
        from arq_writer.chunker import Buzhash, ChunkerConfig
        with self.assertRaises(ValueError):
            Buzhash(ChunkerConfig(
                window_size=48,
                min_chunk_size=10,  # less than window
                max_chunk_size=1024,
            ))

    def test_max_below_min_raises(self) -> None:
        from arq_writer.chunker import Buzhash, ChunkerConfig
        with self.assertRaises(ValueError):
            Buzhash(ChunkerConfig(
                window_size=48,
                min_chunk_size=4096,
                max_chunk_size=512,
            ))


class G4_ContrivedContentTests(unittest.TestCase):
    """Chunker behaviour on pathological inputs."""

    def test_repeating_short_pattern_is_deterministic(self) -> None:
        """Repeating "abc" content; chunker output reproducible
        across runs. Both same-config invocations must yield the
        same chunk sequence."""
        from arq_writer.chunker import Buzhash, ChunkerConfig
        cfg = ChunkerConfig()
        data = (b"abc") * 50_000  # 150KB
        c1 = Buzhash(cfg)
        c2 = Buzhash(cfg)
        chunks1 = list(c1.chunk(data))
        chunks2 = list(c2.chunk(data))
        self.assertEqual(chunks1, chunks2)
        self.assertEqual(b"".join(chunks1), data)

    def test_monotonic_byte_sequence_chunks_finitely(self) -> None:
        """0..255 repeated. Verify the chunker terminates and the
        chunks concatenate back to the original input — basic
        liveness property."""
        from arq_writer.chunker import Buzhash, ChunkerConfig
        cfg = ChunkerConfig(
            min_chunk_size=2048,
            max_chunk_size=8192,
        )
        data = bytes(range(256)) * 500  # 128KB monotonic-like
        chunks = list(Buzhash(cfg).chunk(data))
        self.assertGreater(len(chunks), 0)
        self.assertLessEqual(
            len(chunks),
            (len(data) // cfg.min_chunk_size) + 1,
            "chunker shouldn't produce an absurd number of "
            "chunks for finite input",
        )
        self.assertEqual(b"".join(chunks), data)

    def test_random_bytes_chunks_lossless(self) -> None:
        """Truly random content: chunk boundaries fire on the
        rolling-hash mask hits; chunks concatenate losslessly."""
        from arq_writer.chunker import Buzhash, ChunkerConfig
        data = random.Random(20260511).randbytes(256 * 1024)
        cfg = ChunkerConfig()
        chunks = list(Buzhash(cfg).chunk(data))
        self.assertGreater(len(chunks), 0)
        self.assertEqual(b"".join(chunks), data)
        # Every chunk except the last must be >= min_chunk_size
        # OR == max_chunk_size; the last can be short (tail).
        for i, ch in enumerate(chunks[:-1]):
            self.assertGreaterEqual(
                len(ch), cfg.min_chunk_size,
                f"non-tail chunk[{i}] size {len(ch)} < min "
                f"{cfg.min_chunk_size}",
            )
            self.assertLessEqual(
                len(ch), cfg.max_chunk_size,
                f"chunk[{i}] size {len(ch)} > max "
                f"{cfg.max_chunk_size}",
            )


class G5_CrossChunkerDedupTests(unittest.TestCase):
    """G5 invariant: same config + same input → same chunks
    (dedup-deterministic), regardless of which chunker class.
    Different chunker classes can differ on the same input."""

    def test_buzhash_is_deterministic_across_instances(
        self,
    ) -> None:
        """Same input + same config → same chunk sequence. This
        IS the dedup contract: a re-run computes the same blob_ids
        as the original run."""
        from arq_writer.chunker import Buzhash, ChunkerConfig
        cfg = ChunkerConfig()
        data = random.Random(42).randbytes(200_000)
        runs = [list(Buzhash(cfg).chunk(data)) for _ in range(3)]
        self.assertEqual(runs[0], runs[1])
        self.assertEqual(runs[1], runs[2])

    def test_fixedchunker_is_deterministic_and_positional(
        self,
    ) -> None:
        """The FixedChunker (Arq.app's useBuzhash=False mode)
        cuts at fixed-size boundaries; same input → same chunks,
        and the chunk sizes are exactly the fixed value (except
        the final tail)."""
        from arq_writer.chunker import FixedChunker, FIXED_CHUNK_SIZE_ARQ_V8
        # Use a smaller fixed size for fast testing.
        fixed_size = 1024
        chunker_a = FixedChunker(chunk_size=fixed_size)
        chunker_b = FixedChunker(chunk_size=fixed_size)
        data = random.Random(7).randbytes(5000)
        chunks_a = list(chunker_a.chunk(data))
        chunks_b = list(chunker_b.chunk(data))
        self.assertEqual(chunks_a, chunks_b)
        # 5000 / 1024 → 4 chunks of 1024, 1 tail of 904.
        self.assertEqual(len(chunks_a), 5)
        for ch in chunks_a[:-1]:
            self.assertEqual(len(ch), fixed_size)
        self.assertEqual(len(chunks_a[-1]), 5000 - 4 * fixed_size)
        self.assertEqual(b"".join(chunks_a), data)

    def test_buzhash_and_fixedchunker_differ_on_typical_input(
        self,
    ) -> None:
        """The two chunkers produce different chunk sequences for
        typical input. Pinning this difference (rather than
        bridging it) reflects the design: Arq.app's useBuzhash
        toggle decides which mode the plan runs."""
        from arq_writer.chunker import (
            Buzhash, FixedChunker, ChunkerConfig,
        )
        data = random.Random(99).randbytes(50_000)
        bz_chunks = list(Buzhash(ChunkerConfig()).chunk(data))
        fx_chunks = list(FixedChunker(chunk_size=10_000).chunk(data))
        # Concatenations equal (both lossless), but chunk
        # boundaries differ.
        self.assertEqual(b"".join(bz_chunks), data)
        self.assertEqual(b"".join(fx_chunks), data)
        # Boundary positions of each: cumulative offsets.
        def offsets(chunks):
            out, p = [], 0
            for c in chunks:
                p += len(c)
                out.append(p)
            return out
        # The two boundary sets differ — at least one of the
        # fixed-chunker boundaries doesn't appear in the Buzhash
        # set (or vice versa). Very near-certain probabilistically.
        self.assertNotEqual(offsets(bz_chunks), offsets(fx_chunks))


if __name__ == "__main__":
    unittest.main()
