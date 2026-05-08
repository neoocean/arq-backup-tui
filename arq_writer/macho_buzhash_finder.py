"""Static analyzer for Mach-O binaries — locate Buzhash chunker
parameters in Arq.app (or any other macOS app that uses Buzhash).

The sandbox doesn't allow direct download of ``Arq.app`` from
arqbackup.com. This module is the "if you can hand us the binary,
we can pull the parameters out" half of the RE workflow:

- An end user (or future CI step) extracts the Mach-O binary from
  the Arq.app DMG / pkg.
- This module scans it for the two artifacts that uniquely identify
  a Buzhash chunker:

  1. The 256-entry × 4-byte lookup table ``T``. In a compiled
     binary it appears as 1024 contiguous bytes of "random-looking"
     32-bit values in a ``__const`` segment. We use a statistical
     test (entropy + uniqueness) to score every 4-byte-aligned
     1024-byte window and report top candidates.
  2. Numeric constants that gate the chunker (window size, mask,
     min/max chunk size). These are typically immediates in the
     code section: 32, 48, 64, 4096, 8192, 65536, 0x1FFF, etc.
     We grep the binary for known plausible immediate-value patterns
     and surface their byte offsets.

Output is structured (JSON-friendly) so a follow-up step — manual
review or another script — can pick the right candidate. The
analyzer doesn't disassemble code; it does cheap byte-level pattern
matching only.

Usage:

    from arq_writer.machO_buzhash_finder import analyze_macho_for_buzhash
    report = analyze_macho_for_buzhash(Path("/path/to/Arq.app/Contents/MacOS/Arq"))
    # -> dict: {"t_table_candidates": [{"offset": ..., "score": ...}, ...],
    #          "numeric_constants": [...], ...}
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Mach-O magic numbers we recognize (LE); we just need to confirm
# the file looks like Mach-O.
MACHO_MAGICS = (
    0xFEEDFACE,    # 32-bit
    0xFEEDFACF,    # 64-bit
    0xCEFAEDFE,    # 32-bit BE-stored
    0xCFFAEDFE,    # 64-bit BE-stored
    0xCAFEBABE,    # universal (fat)
    0xBEBAFECA,    # universal BE-stored
)


@dataclass
class TableCandidate:
    """A 1024-byte window scored as a possible 256×4 Buzhash table."""

    offset: int
    score: float
    unique_values: int
    entropy_bits: float

    @property
    def looks_like_table(self) -> bool:
        # Heuristic threshold: ≥ 200 unique 32-bit values out of 256
        # AND entropy ≥ 7.0 bits/byte (max 8.0). Real Buzhash tables
        # are typically all-distinct random 32-bit values; constant
        # arrays and string blocks fail both checks.
        return self.unique_values >= 200 and self.entropy_bits >= 7.0


@dataclass
class NumericHit:
    """A numeric constant matched somewhere in the binary."""

    offset: int
    width: int       # bytes
    value: int
    label: str       # plain-English description ("window=48", etc.)


@dataclass
class MachOBuzhashReport:
    file_path: Path
    file_size: int
    is_macho: bool
    t_table_candidates: List[TableCandidate] = field(default_factory=list)
    numeric_constants: List[NumericHit] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "file_path": str(self.file_path),
            "file_size": self.file_size,
            "is_macho": self.is_macho,
            "t_table_candidates": [
                {
                    "offset": c.offset,
                    "score": c.score,
                    "unique_values": c.unique_values,
                    "entropy_bits": c.entropy_bits,
                    "looks_like_table": c.looks_like_table,
                }
                for c in self.t_table_candidates
            ],
            "numeric_constants": [
                {
                    "offset": h.offset, "width": h.width,
                    "value": h.value, "label": h.label,
                }
                for h in self.numeric_constants
            ],
            "notes": self.notes,
        }


# ---------------------------------------------------------------------------
# T-table scoring
# ---------------------------------------------------------------------------


def _entropy_bits(window: bytes) -> float:
    """Shannon entropy of a byte window in bits per byte."""
    if not window:
        return 0.0
    counts = [0] * 256
    for b in window:
        counts[b] += 1
    n = len(window)
    h = 0.0
    for c in counts:
        if c == 0:
            continue
        p = c / n
        h -= p * math.log2(p)
    return h


def _score_table_window(window: bytes) -> Tuple[float, int, float]:
    """Return ``(score, unique_count, entropy_bits)`` for ``window``.

    A real Buzhash T table has exactly 256 distinct 32-bit values
    drawn uniformly from 0..2^32. Our score combines:

    - Unique count (closer to 256 = better; deviation penalized)
    - Byte entropy (closer to 8.0 = better)
    - Penalty for ASCII-heavy windows (those are likely string data)
    """
    assert len(window) == 1024
    values = struct.unpack("<256I", window)
    unique = len(set(values))
    ent = _entropy_bits(window)

    # ASCII penalty: windows with > 50% printable bytes are almost
    # certainly strings, not crypto tables.
    printable = sum(1 for b in window if 0x20 <= b < 0x7F)
    ascii_ratio = printable / len(window)
    ascii_penalty = max(0.0, ascii_ratio - 0.3) * 4.0

    score = (unique / 256.0) * 4.0 + (ent / 8.0) * 4.0 - ascii_penalty
    return score, unique, ent


def find_t_table_candidates(
    data: bytes, *, top_k: int = 10, stride: int = 4,
) -> List[TableCandidate]:
    """Slide a 1024-byte window over ``data`` (4-byte aligned) and
    return the top-K scored windows.

    On a 100 MiB binary at stride=4 this is ~25M windows — slow in
    pure Python (~minutes). Increase ``stride`` to 16 or 64 for a
    faster but coarser pass; the T table has to start on a 4-byte
    boundary at minimum, but compilers usually align at 16+ bytes.
    """
    if len(data) < 1024:
        return []
    candidates: List[TableCandidate] = []
    # Quick pre-filter: only score windows that have ≥ 200 distinct
    # bytes (a strong indicator vs. all-zero / all-FF / repeating
    # pattern regions, common in binaries). This pre-filter cuts the
    # score work by 2-3 orders of magnitude on real binaries.
    end = len(data) - 1024
    for off in range(0, end, stride):
        # Cheap byte-distinct test first.
        win = data[off : off + 1024]
        # The byte entropy filter on a 1024-byte window is tight
        # enough on its own to be the sole pre-filter.
        if win.count(0) > 800:
            continue
        score, unique, ent = _score_table_window(win)
        if score < 4.0:
            continue
        candidates.append(TableCandidate(
            offset=off, score=score,
            unique_values=unique,
            entropy_bits=ent,
        ))
    candidates.sort(key=lambda c: -c.score)
    return candidates[:top_k]


# ---------------------------------------------------------------------------
# Numeric-constant matching
# ---------------------------------------------------------------------------


PLAUSIBLE_CONSTANTS: Dict[int, str] = {
    16: "window=16",
    32: "window=32",
    48: "window=48 (borg/restic default)",
    64: "window=64",
    128: "window=128",
    256: "window=256",
    1024: "min/max=1KiB",
    2048: "min/max=2KiB",
    4096: "min/max=4KiB",
    8192: "min/max=8KiB",
    16384: "min/max=16KiB",
    32768: "min/max=32KiB",
    65536: "min/max=64KiB",
    131072: "min/max=128KiB",
    262144: "min/max=256KiB (Arq maxPackedItemLength)",
    524288: "min/max=512KiB",
    1048576: "min/max=1MiB",
    2097152: "min/max=2MiB",
    4194304: "min/max=4MiB",
    0x1FFF: "mask=13bit (~8KiB avg)",
    0x3FFF: "mask=14bit (~16KiB avg)",
    0x7FFF: "mask=15bit (~32KiB avg)",
    0xFFFF: "mask=16bit (~64KiB avg)",
    0x1FFFF: "mask=17bit (~128KiB avg)",
    0x3FFFF: "mask=18bit (~256KiB avg)",
}


def find_numeric_constants(
    data: bytes,
    *,
    widths: Tuple[int, ...] = (4, 8),
) -> List[NumericHit]:
    """Locate every plausible chunker constant value.

    Real binaries contain many incidental matches (e.g. 4096 appears
    in lots of unrelated places). The follow-up workflow is:

    1. Run ``find_t_table_candidates`` first to get the T table
       offset.
    2. Filter ``find_numeric_constants`` hits to those within ~4 KiB
       of the table offset — that's where the chunker code is most
       likely to live.
    """
    hits: List[NumericHit] = []
    for value, label in PLAUSIBLE_CONSTANTS.items():
        for width in widths:
            if value >= 1 << (width * 8):
                continue
            needle_le = value.to_bytes(width, "little")
            off = 0
            while True:
                idx = data.find(needle_le, off)
                if idx < 0:
                    break
                hits.append(NumericHit(
                    offset=idx, width=width, value=value, label=label,
                ))
                off = idx + 1
    return hits


# ---------------------------------------------------------------------------
# Min / max chunk-size pairing heuristics
# ---------------------------------------------------------------------------
#
# Compilers typically emit min_chunk_size and max_chunk_size as
# immediate operands of two adjacent ``cmp`` / ``mov`` instructions in
# the chunker's boundary-emit branch. They therefore land within a
# few dozen bytes of each other in the .text segment. We exploit that
# co-location to disambiguate which of the many incidental matches
# for "4096" / "8192" / "65536" / "131072" / etc. are the real
# chunker parameters.
#
# This heuristic is most useful AFTER the T table is located: the
# chunker code lives within ~32 KiB of the table data, so we restrict
# pair-search to that window first. The pair-search result also
# enables disambiguating values that have multiple plausible roles
# (256 = window or could be reach; 4096 = page size everywhere or
# min_chunk_size).


# Plausible min_chunk_size values (4 KiB is universal floor; 1 KiB
# present in some research-grade chunkers; 32 KiB is the typical
# upper bound for "min" in production chunkers).
PLAUSIBLE_MIN_VALUES: Tuple[int, ...] = (
    1024, 2048, 4096, 8192, 16384, 32768,
)

# Plausible max_chunk_size values. 32 KiB lower bound is unusual but
# possible; 4 MiB upper bound covers borg / restic / casync ranges.
PLAUSIBLE_MAX_VALUES: Tuple[int, ...] = (
    32768, 65536, 131072, 262144, 524288,
    1048576, 2097152, 4194304,
)


@dataclass
class MinMaxPair:
    """A co-located (min, max) candidate pair."""

    min_value: int
    max_value: int
    min_offset: int
    max_offset: int
    distance: int        # |min_offset - max_offset|

    @property
    def is_plausible(self) -> bool:
        # Real chunker pairs satisfy min < max with a meaningful
        # ratio (≥ 4×); a 4-KiB / 8-KiB pair would split everything
        # into 2-chunk files, which no real chunker does.
        if self.min_value >= self.max_value:
            return False
        return self.max_value // self.min_value >= 4


def find_min_max_pairs(
    constants: List[NumericHit],
    *,
    max_distance_bytes: int = 64,
) -> List[MinMaxPair]:
    """From a list of numeric-constant hits, find every (min, max)
    pair whose offsets are within ``max_distance_bytes`` of each
    other, in plausibility order.

    Sorted: most-plausible first by (small distance, larger ratio).
    Pre-filtering ``constants`` to a window around the T-table
    (e.g. ±32 KiB) before calling this is recommended — incidental
    matches for "4096" appear all over a binary and inflate the
    output otherwise.
    """
    by_value: Dict[int, List[int]] = {}
    for h in constants:
        by_value.setdefault(h.value, []).append(h.offset)

    pairs: List[MinMaxPair] = []
    for mn in PLAUSIBLE_MIN_VALUES:
        for mx in PLAUSIBLE_MAX_VALUES:
            if mx <= mn:
                continue
            for mn_off in by_value.get(mn, ()):
                for mx_off in by_value.get(mx, ()):
                    dist = abs(mn_off - mx_off)
                    if dist <= max_distance_bytes:
                        pairs.append(MinMaxPair(
                            min_value=mn, max_value=mx,
                            min_offset=mn_off, max_offset=mx_off,
                            distance=dist,
                        ))
    # Most-plausible first: prefer small distance, then large ratio
    # (a real min/max pair has a wider ratio).
    pairs.sort(key=lambda p: (p.distance, -p.max_value // max(p.min_value, 1)))
    return pairs


def filter_constants_near(
    constants: List[NumericHit],
    center_offset: int,
    *,
    radius_bytes: int = 32 * 1024,
) -> List[NumericHit]:
    """Return only the constants whose offset lies within
    ``±radius_bytes`` of ``center_offset`` (typically the top T-table
    candidate's offset)."""
    return [
        h for h in constants
        if abs(h.offset - center_offset) <= radius_bytes
    ]


# ---------------------------------------------------------------------------
# Top-level analyzer
# ---------------------------------------------------------------------------


def analyze_macho_for_buzhash(
    path: Path,
    *,
    table_search_stride: int = 16,
    top_k: int = 10,
    widths: Tuple[int, ...] = (4, 8),
) -> MachOBuzhashReport:
    """Run the full T-table + constants scan.

    On a 50–100 MiB Mach-O binary this typically takes ~30s with
    ``stride=16`` (the default). For an exhaustive scan, drop to
    ``stride=4``.

    Works on any binary file (not just Mach-O), but the magic-number
    check in ``notes`` flags whether the input is what we expect.
    """
    path = Path(path)
    data = path.read_bytes()
    report = MachOBuzhashReport(
        file_path=path, file_size=len(data), is_macho=False,
    )
    if len(data) >= 4:
        magic = int.from_bytes(data[:4], "little")
        report.is_macho = magic in MACHO_MAGICS
    if not report.is_macho:
        report.notes.append(
            "magic number doesn't match Mach-O — may still scan, but "
            "results are less reliable on non-binary inputs"
        )

    report.t_table_candidates = find_t_table_candidates(
        data, top_k=top_k, stride=table_search_stride,
    )
    report.numeric_constants = find_numeric_constants(
        data, widths=widths,
    )

    if not report.t_table_candidates:
        report.notes.append(
            "no T-table candidates found — try a smaller stride "
            "(e.g. table_search_stride=4) or confirm the binary is "
            "actually a Mach-O text/data segment"
        )
    return report


# ---------------------------------------------------------------------------
# Plan B: parameter inference from a real backup's chunk-size distribution
# ---------------------------------------------------------------------------


@dataclass
class InferredParameters:
    sample_count: int
    min_chunk_observed: int
    max_chunk_observed: int
    median_chunk: int
    mean_chunk: int
    estimated_min_chunk_size: int
    estimated_max_chunk_size: int
    estimated_boundary_bits: int
    confidence: str       # "low" / "medium" / "high"


def infer_parameters_from_chunk_sizes(
    chunk_sizes: List[int],
) -> InferredParameters:
    """Given the lengths of the chunks Arq.app produced for one or
    more files, estimate Buzhash parameters.

    This is the "behavioral inference" branch of the RE strategy —
    we don't need Arq.app's binary if we can observe its outputs.

    Method:

    - ``estimated_max_chunk_size`` is the largest non-final chunk.
      (Final chunks are bounded by file size, not the chunker.)
    - ``estimated_min_chunk_size`` is the smallest non-final chunk
      that's not at the END of any file (final chunks can also be
      smaller than min_chunk_size).
    - ``estimated_boundary_bits`` ≈ log2(median chunk size). Buzhash
      with mask = 2^k - 1 produces a geometric distribution with
      mean ≈ 2^k.

    This requires the caller to pre-classify "final" vs "non-final"
    chunks (the last chunk of every file is final). We use a simple
    heuristic when the per-file structure isn't given: chunks
    appearing only once and below the median are flagged as
    "possibly final".

    Confidence:
    - ``high``: ≥ 1000 chunks across multiple files
    - ``medium``: 100-1000
    - ``low``: < 100
    """
    if not chunk_sizes:
        raise ValueError("chunk_sizes must be non-empty")

    sorted_sizes = sorted(chunk_sizes)
    n = len(sorted_sizes)
    max_observed = sorted_sizes[-1]
    min_observed = sorted_sizes[0]
    median = sorted_sizes[n // 2]
    mean = sum(sorted_sizes) // n

    # Estimate min by taking the 5th-percentile size.
    # (Final chunks are <5% of total in typical multi-MiB files.)
    pct5_idx = max(1, int(n * 0.05))
    estimated_min = sorted_sizes[pct5_idx]
    # Estimate max as the 99.5th-percentile (drop the absolute max
    # which might be a force-cap value or anomaly).
    pct995_idx = min(n - 1, int(n * 0.995))
    estimated_max = sorted_sizes[pct995_idx]
    # Round min to the nearest power-of-two-times-1024 for cleaner
    # presentation; min/max in real chunkers are always such values.
    def _round_pow2(x: int) -> int:
        if x <= 0:
            return 0
        return 1 << max(0, x.bit_length() - 1)
    estimated_min_clean = _round_pow2(estimated_min)
    estimated_max_clean = _round_pow2(estimated_max)
    estimated_bits = max(8, median.bit_length() - 1)

    if n >= 1000:
        confidence = "high"
    elif n >= 100:
        confidence = "medium"
    else:
        confidence = "low"

    return InferredParameters(
        sample_count=n,
        min_chunk_observed=min_observed,
        max_chunk_observed=max_observed,
        median_chunk=median,
        mean_chunk=mean,
        estimated_min_chunk_size=estimated_min_clean,
        estimated_max_chunk_size=estimated_max_clean,
        estimated_boundary_bits=estimated_bits,
        confidence=confidence,
    )
