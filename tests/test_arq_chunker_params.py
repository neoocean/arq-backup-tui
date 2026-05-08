"""Sanity tests for the reverse-engineered Arq.app v7 chunker params.

The values in :mod:`arq_writer.arq_chunker_params` were extracted by
static analysis of the Arq.app v7.41 macOS binary. These tests don't
re-do the RE — they just lock in the decoded shape (so a typo in the
hex dump can never silently slip through) and confirm registration
plumbing wires everything together correctly.
"""

from __future__ import annotations

import secrets
import unittest

from arq_writer import arq_chunker_params as params
from arq_writer.chunker import (
    Buzhash,
    ChunkerConfig,
    GENERIC_DEFAULT,
    chunker_for_arq,
    register_arq_chunker,
    _ARQ_PARAM_REGISTRY,
)


class TableShapeTests(unittest.TestCase):
    def test_table_has_256_uint32_entries(self) -> None:
        tbl = params.ARQ_V7_BUZHASH_TABLE
        self.assertEqual(len(tbl), 256)
        for v in tbl:
            self.assertIsInstance(v, int)
            self.assertGreaterEqual(v, 0)
            self.assertLess(v, 1 << 32)

    def test_table_entries_are_unique(self) -> None:
        # A real Buzhash T table is essentially a permutation of 256
        # random uint32 values. Collisions are statistically possible
        # but Arq's table — like every published Buzhash table —
        # happens to have all-distinct entries.
        tbl = params.ARQ_V7_BUZHASH_TABLE
        self.assertEqual(len(set(tbl)), 256)

    def test_first_and_last_entries_match_extraction(self) -> None:
        # Anchor values: if these change, someone re-pasted the hex
        # dump and the rest of the table needs re-verification too.
        # First 4 bytes "be 16 40 a4" decoded as <I → 0xa44016be.
        self.assertEqual(params.ARQ_V7_BUZHASH_TABLE[0], 0xa44016be)
        # Last 4 bytes "48 a8 04 89" decoded as <I → 0x8904a848.
        self.assertEqual(params.ARQ_V7_BUZHASH_TABLE[-1], 0x8904a848)


class ConfigShapeTests(unittest.TestCase):
    def test_config_carries_extracted_params(self) -> None:
        cfg = params.ARQ_V7_CHUNKER_CONFIG
        self.assertEqual(cfg.window_size, 256)
        self.assertEqual(cfg.boundary_bits, 16)
        self.assertEqual(cfg.min_chunk_size, 4096)
        self.assertEqual(cfg.max_chunk_size, 131072)
        self.assertIs(cfg.table, params.ARQ_V7_BUZHASH_TABLE)

    def test_config_validates(self) -> None:
        # Buzhash __init__ enforces the parameter invariants — this
        # round-trips ARQ_V7_CHUNKER_CONFIG through that check.
        Buzhash(params.ARQ_V7_CHUNKER_CONFIG)


class RegistrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = dict(_ARQ_PARAM_REGISTRY)

    def tearDown(self) -> None:
        _ARQ_PARAM_REGISTRY.clear()
        _ARQ_PARAM_REGISTRY.update(self._saved)

    def test_install_registers_under_3_true(self) -> None:
        _ARQ_PARAM_REGISTRY.clear()
        params.install()
        self.assertIs(
            chunker_for_arq(3, True), params.ARQ_V7_CHUNKER_CONFIG,
        )

    def test_install_does_not_clobber_other_variants(self) -> None:
        _ARQ_PARAM_REGISTRY.clear()
        sentinel = ChunkerConfig(window_size=64, boundary_bits=14)
        register_arq_chunker(2, False, sentinel)
        params.install()
        self.assertIs(chunker_for_arq(2, False), sentinel)
        self.assertIs(
            chunker_for_arq(3, True), params.ARQ_V7_CHUNKER_CONFIG,
        )
        # And other unregistered variants still fall through.
        self.assertIs(chunker_for_arq(1, True), GENERIC_DEFAULT)

    def test_install_is_idempotent(self) -> None:
        params.install()
        params.install()
        params.install()
        self.assertIs(
            chunker_for_arq(3, True), params.ARQ_V7_CHUNKER_CONFIG,
        )


class ChunkerRoundTripTests(unittest.TestCase):
    """End-to-end: chunk a buffer with Arq's params, confirm sanity."""

    def test_round_trip_lossless(self) -> None:
        bz = Buzhash(params.ARQ_V7_CHUNKER_CONFIG)
        data = secrets.token_bytes(512 * 1024)
        chunks = list(bz.chunk(data))
        self.assertEqual(b"".join(chunks), data)

    def test_chunks_respect_size_bounds(self) -> None:
        # 4 MiB of random data → chunks no larger than max_chunk_size
        # (128 KiB) and no smaller than min_chunk_size (4 KiB) except
        # possibly the final tail chunk.
        bz = Buzhash(params.ARQ_V7_CHUNKER_CONFIG)
        data = secrets.token_bytes(4 * 1024 * 1024)
        chunks = list(bz.chunk(data))
        self.assertGreater(len(chunks), 1)
        self.assertLessEqual(max(len(c) for c in chunks), 131072)
        # All but the last chunk must hit min_chunk_size.
        for c in chunks[:-1]:
            self.assertGreaterEqual(len(c), 4096)

    def test_chunking_is_deterministic(self) -> None:
        data = secrets.token_bytes(256 * 1024)
        a = list(Buzhash(params.ARQ_V7_CHUNKER_CONFIG).chunk(data))
        b = list(Buzhash(params.ARQ_V7_CHUNKER_CONFIG).chunk(data))
        self.assertEqual(a, b)

    def test_average_chunk_size_near_target(self) -> None:
        # boundary_bits=16 + max_chunk_size=128 KiB → average lands
        # somewhere in [40 KiB, 90 KiB] depending on input. We assert
        # only this loose envelope; tighter bounds would be flaky.
        bz = Buzhash(params.ARQ_V7_CHUNKER_CONFIG)
        data = secrets.token_bytes(8 * 1024 * 1024)
        chunks = list(bz.chunk(data))
        avg = sum(len(c) for c in chunks) // len(chunks)
        self.assertGreater(avg, 40 * 1024)
        self.assertLess(avg, 90 * 1024)


if __name__ == "__main__":
    unittest.main()
