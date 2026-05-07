"""Tests for the Mach-O Buzhash finder + chunker registry."""

from __future__ import annotations

import os
import secrets
import struct
import tempfile
import unittest
from pathlib import Path

from arq_writer.chunker import (
    ChunkerConfig,
    GENERIC_DEFAULT,
    chunker_for_arq,
    known_arq_variants,
    register_arq_chunker,
    _ARQ_PARAM_REGISTRY,
)
from arq_writer.macho_buzhash_finder import (
    MACHO_MAGICS,
    PLAUSIBLE_CONSTANTS,
    analyze_macho_for_buzhash,
    find_numeric_constants,
    find_t_table_candidates,
    infer_parameters_from_chunk_sizes,
)


class TableScoringTests(unittest.TestCase):
    def test_real_random_table_scores_high(self) -> None:
        # Generate a 256×4 table of random bytes — the shape a real
        # Buzhash table has — and confirm it scores in our top
        # candidates list.
        rng_bytes = secrets.token_bytes(1024)
        # Pad with mostly-zero data on either side so the search has
        # plenty of low-scoring noise to compare against.
        data = (b"\x00" * 4096) + rng_bytes + (b"\x00" * 4096)
        cands = find_t_table_candidates(data, top_k=5, stride=4)
        self.assertGreaterEqual(len(cands), 1)
        # The top candidate must lie within stride distance of the
        # planted offset (4096). Adjacent 4-byte-aligned windows that
        # mostly cover the table can score equally well; the analyst
        # picks the right one from the shortlist by inspecting the
        # entries.
        self.assertLessEqual(abs(cands[0].offset - 4096), 16)
        self.assertTrue(cands[0].looks_like_table)
        self.assertGreaterEqual(cands[0].unique_values, 200)
        self.assertGreater(cands[0].entropy_bits, 7.0)

    def test_zero_region_does_not_score(self) -> None:
        data = b"\x00" * 8192
        cands = find_t_table_candidates(data, top_k=5)
        self.assertEqual(cands, [])

    def test_repeating_pattern_does_not_score(self) -> None:
        data = b"\x12\x34\x56\x78" * 4096    # 16 KiB of repeats
        cands = find_t_table_candidates(data, top_k=5)
        self.assertEqual(cands, [])

    def test_ascii_text_block_does_not_score(self) -> None:
        # Pure ASCII at very high entropy still gets ascii-penalized.
        data = (
            b"This is a long block of plausibly looking english text "
            b"meant to imitate a strings table dumped into a binary. "
            b"Buzhash analyzers must not flag this as a candidate. "
        ) * 30
        cands = find_t_table_candidates(data[:8192], top_k=5)
        for c in cands:
            self.assertFalse(c.looks_like_table)


class NumericConstantTests(unittest.TestCase):
    def test_known_constants_found(self) -> None:
        # Build a tiny binary with several plausible chunker constants
        # back-to-back and confirm the scanner surfaces them.
        body = (
            struct.pack("<I", 48)                # window=48
            + struct.pack("<I", 4096)            # min=4 KiB
            + struct.pack("<I", 1048576)         # max=1 MiB
            + struct.pack("<I", 0x7FFF)          # mask=15bit
            + b"\x00" * 1000                     # noise
        )
        hits = find_numeric_constants(body, widths=(4,))
        # Must hit each of the four constants at least once.
        labels = {h.label for h in hits}
        self.assertIn("window=48 (borg/restic default)", labels)
        self.assertIn("min/max=4KiB", labels)
        self.assertIn("min/max=1MiB", labels)
        self.assertIn("mask=15bit (~32KiB avg)", labels)


class FullAnalyzerTests(unittest.TestCase):
    def test_synthetic_macho_with_table_and_constants(self) -> None:
        # Synthesize a "binary" with the right Mach-O magic + a
        # plausible table at a known offset + plausible constants.
        magic = struct.pack("<I", 0xFEEDFACF)        # 64-bit Mach-O magic
        header_padding = b"\x00" * (4096 - 4)
        # Real-ish Buzhash table.
        table = secrets.token_bytes(1024)
        constants = (
            struct.pack("<I", 64) + struct.pack("<I", 4096)
            + struct.pack("<I", 1048576) + struct.pack("<I", 0x7FFF)
        )
        body = magic + header_padding + table + constants + (b"\x00" * 4096)
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(body)
            fpath = Path(f.name)
        try:
            report = analyze_macho_for_buzhash(
                fpath, table_search_stride=4, top_k=5,
            )
            self.assertTrue(report.is_macho)
            self.assertGreaterEqual(len(report.t_table_candidates), 1)
            top = report.t_table_candidates[0]
            self.assertLessEqual(abs(top.offset - 4096), 16)
            self.assertTrue(top.looks_like_table)
            # Constants should include the values we planted.
            labels = {h.label for h in report.numeric_constants}
            self.assertIn("window=64", labels)
            self.assertIn("min/max=4KiB", labels)
            self.assertIn("min/max=1MiB", labels)
        finally:
            os.unlink(fpath)

    def test_non_macho_input_flagged(self) -> None:
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"\x00" * 2048)
            fpath = Path(f.name)
        try:
            report = analyze_macho_for_buzhash(fpath)
            self.assertFalse(report.is_macho)
            self.assertTrue(any("Mach-O" in n for n in report.notes))
        finally:
            os.unlink(fpath)


class ParameterInferenceTests(unittest.TestCase):
    def test_inference_on_known_distribution(self) -> None:
        # Simulate Buzhash output: ~1000 chunks, geometric distribution
        # with mean ≈ 32 KiB (mask=15 bits).
        rng = secrets.SystemRandom()
        sizes = []
        for _ in range(1000):
            # Geometric with p ≈ 1/32768; clamp to [4096, 1048576].
            n = int(rng.expovariate(1 / 32768))
            n = max(4096, min(n, 1048576))
            sizes.append(n)
        ip = infer_parameters_from_chunk_sizes(sizes)
        self.assertEqual(ip.confidence, "high")
        # Estimated bits should land near 15 ± 2 (geometric noise).
        self.assertGreaterEqual(ip.estimated_boundary_bits, 13)
        self.assertLessEqual(ip.estimated_boundary_bits, 17)

    def test_inference_low_confidence_small_sample(self) -> None:
        ip = infer_parameters_from_chunk_sizes([4000, 8000, 16000])
        self.assertEqual(ip.confidence, "low")

    def test_inference_empty_rejected(self) -> None:
        with self.assertRaises(ValueError):
            infer_parameters_from_chunk_sizes([])


class ChunkerRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        # Save + restore the registry so tests don't pollute each
        # other.
        self._saved = dict(_ARQ_PARAM_REGISTRY)
        _ARQ_PARAM_REGISTRY.clear()

    def tearDown(self) -> None:
        _ARQ_PARAM_REGISTRY.clear()
        _ARQ_PARAM_REGISTRY.update(self._saved)

    def test_unregistered_falls_back_to_default(self) -> None:
        cfg = chunker_for_arq(3, True)
        self.assertIs(cfg, GENERIC_DEFAULT)

    def test_register_and_lookup(self) -> None:
        custom = ChunkerConfig(
            window_size=64, boundary_bits=14,
            min_chunk_size=2048, max_chunk_size=524288,
        )
        register_arq_chunker(3, True, custom)
        cfg = chunker_for_arq(3, True)
        self.assertIs(cfg, custom)

    def test_known_variants_listed(self) -> None:
        register_arq_chunker(3, True, ChunkerConfig())
        register_arq_chunker(3, False, ChunkerConfig())
        self.assertEqual(known_arq_variants(), [(3, False), (3, True)])


if __name__ == "__main__":
    unittest.main()
