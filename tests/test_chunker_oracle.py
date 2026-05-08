"""Tests for the chunker falsification harness."""

from __future__ import annotations

import json
import random
import tempfile
import unittest
from pathlib import Path

from arq_writer.buzhash_re_cli import main as buzhash_main
from arq_writer.chunker import Buzhash
from arq_writer.chunker_oracle import compare_chunking
from arq_writer import arq_chunker_params  # noqa: F401  (registers config)
from arq_writer.arq_chunker_params import ARQ_V7_CHUNKER_CONFIG


def _seeded_input(seed: int, n: int) -> bytes:
    return random.Random(seed).randbytes(n)


class CompareChunkingTests(unittest.TestCase):
    def test_self_consistency_is_a_match(self) -> None:
        # Feed our own chunker's output back in as the "expected"
        # sequence. Trivially matches — guards against bugs in the
        # comparison loop.
        data = _seeded_input(0xA1, 256 * 1024)
        ours = [len(c) for c in Buzhash(ARQ_V7_CHUNKER_CONFIG).chunk(data)]
        report = compare_chunking(data, ours)
        self.assertTrue(report.match)
        self.assertEqual(report.matching_prefix, len(ours))
        self.assertIsNone(report.first_divergence_offset)
        self.assertEqual(report.notes, [])

    def test_divergence_reports_first_offset(self) -> None:
        data = _seeded_input(0xA2, 256 * 1024)
        ours = [len(c) for c in Buzhash(ARQ_V7_CHUNKER_CONFIG).chunk(data)]
        # Construct a "fake observed" sequence that agrees on the
        # first 2 chunks then differs: take ours, but split the 3rd
        # chunk into halves. Sum is still len(data).
        if len(ours) < 3:
            self.skipTest("not enough chunks for divergence test")
        third = ours[2]
        half = third // 2
        fake = ours[:2] + [half, third - half] + ours[3:]
        report = compare_chunking(data, fake)
        self.assertFalse(report.match)
        self.assertEqual(report.matching_prefix, 2)
        # First two chunks have lengths ours[0] and ours[1].
        expected_offset = ours[0] + ours[1]
        self.assertEqual(report.first_divergence_offset, expected_offset)

    def test_sum_mismatch_recorded_in_notes(self) -> None:
        data = _seeded_input(0xA3, 64 * 1024)
        # An observed sequence whose sum differs from len(data).
        report = compare_chunking(data, [10, 20, 30])
        self.assertFalse(report.match)
        self.assertTrue(any("partition" in n for n in report.notes))

    def test_chunk_count_mismatch_when_lengths_align_short(self) -> None:
        # Truncated observed sequence — shares prefix with ours but
        # short of total length; sum won't match.
        data = _seeded_input(0xA4, 256 * 1024)
        ours = [len(c) for c in Buzhash(ARQ_V7_CHUNKER_CONFIG).chunk(data)]
        truncated = ours[:-1]
        report = compare_chunking(data, truncated)
        self.assertFalse(report.match)
        self.assertEqual(report.observed_chunk_count, len(ours))
        self.assertEqual(report.expected_chunk_count, len(truncated))


class CompareChunkingCLITests(unittest.TestCase):
    def test_cli_reports_match_with_zero_exit(self) -> None:
        data = _seeded_input(0xB1, 256 * 1024)
        ours = [len(c) for c in Buzhash(ARQ_V7_CHUNKER_CONFIG).chunk(data)]
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            in_path = tdp / "input.bin"
            in_path.write_bytes(data)
            obs_path = tdp / "observed.json"
            obs_path.write_text(json.dumps(ours))
            rc = buzhash_main([
                "verify-chunking", str(in_path), str(obs_path),
            ])
            self.assertEqual(rc, 0)

    def test_cli_reports_nonzero_on_divergence(self) -> None:
        # 2 MiB > max_chunk_size, so the chunker is guaranteed to
        # produce multiple chunks; a "single chunk covering the
        # whole file" hypothesis must therefore diverge.
        data = _seeded_input(0xB2, 2 * 1024 * 1024)
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            in_path = tdp / "input.bin"
            in_path.write_bytes(data)
            obs_path = tdp / "observed.json"
            obs_path.write_text(json.dumps([len(data)]))
            rc = buzhash_main([
                "verify-chunking", str(in_path), str(obs_path),
            ])
            self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
