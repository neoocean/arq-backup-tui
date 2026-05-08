"""Falsification harness for reverse-engineered chunker parameters.

The values shipped in :mod:`arq_writer.arq_chunker_params` were
extracted by static analysis of the Arq.app v7.41 macOS binary.
Three of the four scalar parameters have direct binary evidence;
``min_chunk_size`` is a 4 KiB conservative inference. The harness
in this module verifies the full parameter set against a real
backup that Arq.app produced — independent of our static analysis
— so a wrong parameter shows up as a definite divergence rather
than a silent dedup-quality regression.

### Workflow

1. On a macOS box with Arq.app installed, pick a known-content file
   (any non-empty file ≥ 1 MiB; ``head -c 16M /dev/urandom > test.bin``
   is a fine choice). Back it up with Arq.app. Note the source path
   and the password.
2. Use this project's :mod:`arq_reader` to decrypt + walk the
   resulting backup; for the FileNode of the known file, collect
   the plaintext length of each ``dataBlobLoc`` (decrypt → LZ4
   decompress → ``len(plaintext)``). Persist them as a JSON list,
   in order, to ``observed-lengths.json``.
3. Run::

       arq-chunker-verify ./test.bin ./observed-lengths.json

   The harness re-runs our :class:`Buzhash` chunker on ``test.bin``
   with :data:`ARQ_V7_CHUNKER_CONFIG`, prints the observed vs.
   expected length sequences, and reports the byte offset of the
   first divergence (or "match" if every chunk lines up exactly).

### What a divergence means

- **Different chunk count, identical bytes**: a structural parameter
  is wrong (window / mask / max). Look at the first divergence
  offset — if Arq's first chunk is shorter than ours, our mask is
  too big (avg chunk too large); longer than ours, our mask is too
  small or our max-cap fires too early.
- **Same first N chunks, then divergence**: the T table is correct
  for the first N boundaries but a later boundary depends on a byte
  pattern we score differently. This usually means a typo in the
  hex-literal table.
- **All chunk lengths identical**: parameters confirmed to within
  the resolution of this single test file. Arq.app's chunker is a
  deterministic function of (bytes, parameters), so a single
  matching ≥ 1 MiB file is strong evidence; a 16 MiB file at
  64 KiB average → ~256 chunk boundaries, each one an independent
  check.

A confirming run is enough to upgrade ``min_chunk_size`` from "low
confidence" to "high" in the research doc.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from .chunker import Buzhash, ChunkerConfig


@dataclass
class ChunkOracleReport:
    """Outcome of comparing our chunker against an external length list.

    ``match`` is the headline. Everything else is diagnostic data
    intended to help an analyst figure out which parameter is wrong
    when ``match`` is False.
    """

    match: bool
    input_sha256: str
    input_length: int
    expected_chunk_count: int
    observed_chunk_count: int
    expected_lengths: List[int] = field(default_factory=list)
    observed_lengths: List[int] = field(default_factory=list)
    matching_prefix: int = 0           # chunks that line up from the start
    first_divergence_offset: Optional[int] = None  # byte offset, or None
    notes: List[str] = field(default_factory=list)


def _equalize_for_comparison(seq: Sequence[int]) -> List[int]:
    return [int(x) for x in seq]


def compare_chunking(
    input_bytes: bytes,
    observed_lengths: Sequence[int],
    *,
    config: Optional[ChunkerConfig] = None,
) -> ChunkOracleReport:
    """Run our chunker on ``input_bytes`` and compare against
    ``observed_lengths`` (the chunk-length sequence Arq.app produced
    for the same input).

    Both sequences must sum to ``len(input_bytes)``; if not, one of
    them isn't a partition of the same input and the comparison
    isn't meaningful — the harness flags this in ``notes``.

    The chunker is run with :data:`ARQ_V7_CHUNKER_CONFIG` by default,
    but ``config`` lets a caller probe alternative parameter sets
    (useful for "what if we change ``min_chunk_size`` to 8 KiB?"
    style hypothesis testing).
    """
    if config is None:
        # Local import to keep this module's import graph
        # ChunkerConfig-only — callers depending on the harness
        # alone shouldn't pull in the table data.
        from .arq_chunker_params import ARQ_V7_CHUNKER_CONFIG
        config = ARQ_V7_CHUNKER_CONFIG

    expected_lengths = _equalize_for_comparison(observed_lengths)
    notes: List[str] = []

    input_sha = hashlib.sha256(input_bytes).hexdigest()
    input_len = len(input_bytes)

    if sum(expected_lengths) != input_len:
        notes.append(
            f"sum(observed_lengths) = {sum(expected_lengths)} != "
            f"len(input_bytes) = {input_len}; observed sequence is "
            "not a partition of the input"
        )

    bz = Buzhash(config)
    our_lengths = [len(c) for c in bz.chunk(input_bytes)]
    if sum(our_lengths) != input_len:
        # Should be impossible — chunker is loss-free.
        notes.append(
            f"INTERNAL: sum(our_lengths) = {sum(our_lengths)} != "
            f"len(input_bytes) = {input_len}"
        )

    # Walk both sequences in lockstep to find the first divergence.
    matching_prefix = 0
    first_div_offset: Optional[int] = None
    cumulative = 0
    for ours, theirs in zip(our_lengths, expected_lengths):
        if ours != theirs:
            first_div_offset = cumulative
            break
        matching_prefix += 1
        cumulative += ours
    else:
        # zip exhausted at least one side without divergence.
        if len(our_lengths) != len(expected_lengths):
            first_div_offset = cumulative
            notes.append(
                f"sequences agree on first {matching_prefix} chunks "
                "but differ in chunk count "
                f"({len(our_lengths)} vs {len(expected_lengths)})"
            )

    is_match = (
        first_div_offset is None
        and len(our_lengths) == len(expected_lengths)
        and not notes
    )

    return ChunkOracleReport(
        match=is_match,
        input_sha256=input_sha,
        input_length=input_len,
        expected_chunk_count=len(expected_lengths),
        observed_chunk_count=len(our_lengths),
        expected_lengths=expected_lengths,
        observed_lengths=our_lengths,
        matching_prefix=matching_prefix,
        first_divergence_offset=first_div_offset,
        notes=notes,
    )
