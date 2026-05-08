"""Tests for the Mach-O Buzhash finder + chunker registry."""

from __future__ import annotations

import os
import random
import secrets
import struct
import tempfile
import unittest
from pathlib import Path


def _seeded_bytes(seed: int, n: int) -> bytes:
    """High-entropy bytes that are reproducible across test runs.

    Uses ``random.Random(seed)`` rather than ``secrets`` so the
    statistical scoring tests below — which check that a planted
    "real-looking" table beats neighboring windows — never flake.
    """
    return random.Random(seed).randbytes(n)

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
    NumericHit,
    analyze_macho_for_buzhash,
    filter_constants_near,
    find_min_max_pairs,
    find_numeric_constants,
    find_t_table_candidates,
    infer_parameters_from_chunk_sizes,
)


class TableScoringTests(unittest.TestCase):
    def test_real_random_table_scores_high(self) -> None:
        # Generate a 256×4 table of random bytes — the shape a real
        # Buzhash table has — and confirm it scores in our top
        # candidates list.
        rng_bytes = _seeded_bytes(0xB2_C3_AB_71, 1024)
        # Pad with mostly-zero data on either side so the search has
        # plenty of low-scoring noise to compare against.
        data = (b"\x00" * 4096) + rng_bytes + (b"\x00" * 4096)
        cands = find_t_table_candidates(data, top_k=5, stride=4)
        self.assertGreaterEqual(len(cands), 1)
        # Adjacent 4-byte-aligned windows that mostly cover the
        # table score within rounding of the exact-aligned window;
        # which one wins is dominated by 4 bytes of noise. We assert
        # the looser invariant: the top candidate's window mostly
        # overlaps the planted table (≥ 75 %).
        top = cands[0]
        overlap = max(
            0, min(top.offset + 1024, 4096 + 1024) - max(top.offset, 4096),
        )
        self.assertGreaterEqual(overlap, 768)
        self.assertTrue(top.looks_like_table)
        self.assertGreaterEqual(top.unique_values, 200)
        self.assertGreater(top.entropy_bits, 7.0)

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
        # Real-ish Buzhash table — seeded so adjacent-window scoring
        # ties don't make the test flake.
        table = _seeded_bytes(0xC4_D9_57_2A, 1024)
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
            # The top candidate's window must mostly overlap the
            # planted table (≥ 75 %); whether the exact-aligned
            # window or a ±N-stride neighbor wins depends on a few
            # bytes of noise, so we don't pin the offset tightly.
            overlap = max(
                0,
                min(top.offset + 1024, 4096 + 1024) - max(top.offset, 4096),
            )
            self.assertGreaterEqual(overlap, 768)
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


class MinMaxPairTests(unittest.TestCase):
    def test_co_located_pair_found(self) -> None:
        # min=4096 at offset 100, max=131072 at offset 110: distance
        # 10, well under the default 64-byte threshold.
        hits = [
            NumericHit(offset=100, width=4, value=4096, label="m"),
            NumericHit(offset=110, width=4, value=131072, label="M"),
            # Distractor: same min value far away.
            NumericHit(offset=10000, width=4, value=4096, label="m"),
        ]
        pairs = find_min_max_pairs(hits)
        self.assertGreaterEqual(len(pairs), 1)
        top = pairs[0]
        self.assertEqual(top.min_value, 4096)
        self.assertEqual(top.max_value, 131072)
        self.assertEqual(top.distance, 10)
        self.assertTrue(top.is_plausible)

    def test_distant_pairs_excluded(self) -> None:
        hits = [
            NumericHit(offset=100, width=4, value=4096, label="m"),
            NumericHit(offset=10000, width=4, value=131072, label="M"),
        ]
        pairs = find_min_max_pairs(hits, max_distance_bytes=64)
        self.assertEqual(pairs, [])

    def test_implausible_min_max_ratio(self) -> None:
        # 16 KiB / 32 KiB pair: ratio 2 < 4 → flagged not plausible
        # (32768 is in PLAUSIBLE_MAX_VALUES; 16384 in PLAUSIBLE_MIN_VALUES).
        hits = [
            NumericHit(offset=100, width=4, value=16384, label="m"),
            NumericHit(offset=104, width=4, value=32768, label="m"),
        ]
        pairs = find_min_max_pairs(hits)
        # The pair is found (within distance) but not_plausible.
        flagged_implausible = [p for p in pairs if not p.is_plausible]
        self.assertGreaterEqual(len(flagged_implausible), 1)

    def test_pairs_sorted_closest_first(self) -> None:
        hits = [
            NumericHit(offset=100, width=4, value=4096, label="m"),
            NumericHit(offset=140, width=4, value=131072, label="M"),
            NumericHit(offset=200, width=4, value=4096, label="m"),
            NumericHit(offset=205, width=4, value=131072, label="M"),
        ]
        pairs = find_min_max_pairs(hits)
        self.assertGreaterEqual(len(pairs), 2)
        # Closest pair (5 bytes apart) must come first.
        self.assertEqual(pairs[0].distance, 5)


class FilterConstantsNearTests(unittest.TestCase):
    def test_radius_filter(self) -> None:
        hits = [
            NumericHit(offset=1000, width=4, value=4096, label="m"),
            NumericHit(offset=2000, width=4, value=4096, label="m"),
            NumericHit(offset=50000, width=4, value=4096, label="m"),
        ]
        nearby = filter_constants_near(hits, 1500, radius_bytes=600)
        offsets = sorted(h.offset for h in nearby)
        self.assertEqual(offsets, [1000, 2000])


class PairSearchCLITests(unittest.TestCase):
    def test_pair_search_on_synthesized_report(self) -> None:
        # Build a JSON report file mimicking analyze-binary output
        # with a planted (4096, 131072) pair near a planted T-table
        # offset, plus distractor pairs far away.
        import json as _json
        import tempfile as _tempfile
        from arq_writer.buzhash_re_cli import main as buzhash_main

        report = {
            "file_path": "synthetic",
            "file_size": 1_000_000,
            "is_macho": True,
            "t_table_candidates": [{"offset": 50000, "score": 7.9}],
            "numeric_constants": [
                # Real chunker pair near the T-table.
                {"offset": 49000, "width": 4, "value": 4096, "label": "min"},
                {"offset": 49016, "width": 4, "value": 131072, "label": "max"},
                # Distractor pair near each other but far from table.
                {"offset": 800000, "width": 4, "value": 4096, "label": "min"},
                {"offset": 800020, "width": 4, "value": 131072, "label": "max"},
            ],
            "notes": [],
        }
        with _tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False,
        ) as f:
            _json.dump(report, f)
            jpath = f.name
        try:
            # Capture stdout via io redirection.
            import io as _io, contextlib as _contextlib
            buf = _io.StringIO()
            with _contextlib.redirect_stdout(buf):
                rc = buzhash_main([
                    "pair-search", jpath, "--radius", str(8 * 1024),
                ])
            self.assertEqual(rc, 0)
            out = _json.loads(buf.getvalue())
            self.assertEqual(out["anchor_offset"], 50000)
            # Distractor pair at offset 800000 is well outside the
            # 8 KiB radius from anchor 50000 → only the real pair
            # remains.
            self.assertEqual(len(out["pair_candidates"]), 1)
            top = out["pair_candidates"][0]
            self.assertEqual(top["min_value"], 4096)
            self.assertEqual(top["max_value"], 131072)
            self.assertTrue(top["is_plausible"])
        finally:
            os.unlink(jpath)


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
