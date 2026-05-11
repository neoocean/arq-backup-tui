"""K4-2 — residual-classification math regression.

The K4-2 analyzer at ``scripts/k4_2_residual_analysis.py``
classifies trailing_sec values into:

- btime_sec match
- residual:
    - mtime_sec match
    - ctime_sec match
    - none-of-the-above

Against the operator's real destination (`/Volumes/arqbackup1`)
the residual-ctime match rate is 88.2% — the K4-2 finding.
This test pins the classification math itself on synthetic
input so a future refactor can't accidentally drop ctime from
the residual analysis.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


class K4_2ResidualAnalysisTests(unittest.TestCase):
    """Synthetic nodes input → analyzer should classify each one
    into the right correlation bucket."""

    def test_all_btime_match(self) -> None:
        sys.path.insert(
            0, str(Path(__file__).resolve().parent.parent),
        )
        from scripts.k4_2_residual_analysis import _analyze_residual
        nodes = [
            {"trailing_sec": 100, "btime_sec": 100,
             "mtime_sec": 200, "ctime_sec": 300}
            for _ in range(10)
        ]
        stats = _analyze_residual(nodes, record_creation_date=400)
        self.assertEqual(stats["total_nodes"], 10)
        self.assertEqual(stats["btime_match_count"], 10)
        self.assertEqual(stats["residual_count"], 0)

    def test_btime_and_ctime_mixed(self) -> None:
        """Half match btime, the other half (residual) match
        ctime. Verify the classification yields the expected
        per-bucket counts."""
        sys.path.insert(
            0, str(Path(__file__).resolve().parent.parent),
        )
        from scripts.k4_2_residual_analysis import _analyze_residual
        nodes = []
        # 5 btime-matching
        for _ in range(5):
            nodes.append({
                "trailing_sec": 100, "btime_sec": 100,
                "mtime_sec": 200, "ctime_sec": 300,
            })
        # 5 residual-ctime-matching
        for _ in range(5):
            nodes.append({
                "trailing_sec": 300, "btime_sec": 100,
                "mtime_sec": 200, "ctime_sec": 300,
            })
        stats = _analyze_residual(nodes, record_creation_date=1000)
        self.assertEqual(stats["btime_match_count"], 5)
        self.assertEqual(stats["btime_match_pct"], 50.0)
        self.assertEqual(stats["residual_count"], 5)
        self.assertEqual(stats["residual_ctime_match"], 5)
        self.assertEqual(stats["residual_ctime_pct"], 100.0)
        self.assertEqual(stats["residual_mtime_match"], 0)

    def test_residual_offset_mean(self) -> None:
        """Residual nodes' offset-from-creationDate stat math."""
        sys.path.insert(
            0, str(Path(__file__).resolve().parent.parent),
        )
        from scripts.k4_2_residual_analysis import _analyze_residual
        nodes = [
            # btime != trailing_sec; trailing = 500, cd = 1000
            # → offset = 500.
            {"trailing_sec": 500, "btime_sec": 999,
             "mtime_sec": 0, "ctime_sec": 0}
            for _ in range(3)
        ]
        stats = _analyze_residual(nodes, record_creation_date=1000)
        self.assertEqual(
            stats["residual_offset_from_creationdate_mean"], 500,
        )

    def test_no_correlation_residual(self) -> None:
        """Trailing_sec matches NOTHING — all four counters at 0."""
        sys.path.insert(
            0, str(Path(__file__).resolve().parent.parent),
        )
        from scripts.k4_2_residual_analysis import _analyze_residual
        nodes = [
            {"trailing_sec": 999, "btime_sec": 100,
             "mtime_sec": 200, "ctime_sec": 300}
            for _ in range(4)
        ]
        stats = _analyze_residual(nodes, record_creation_date=500)
        self.assertEqual(stats["btime_match_count"], 0)
        self.assertEqual(stats["residual_count"], 4)
        self.assertEqual(stats["residual_mtime_match"], 0)
        self.assertEqual(stats["residual_ctime_match"], 0)


if __name__ == "__main__":
    unittest.main()
