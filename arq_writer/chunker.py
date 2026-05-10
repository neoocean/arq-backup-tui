"""Content-defined chunker (Buzhash / cyclic polynomial rolling hash).

Splits a stream of bytes into variable-length chunks at boundaries
determined by the rolling hash of a sliding window. Identical content
yields identical chunk boundaries regardless of where it appears in
the stream — the property that makes content-defined chunking
attractive for dedup of files modified in place.

### About Arq's chunker

Arq's ``backupconfig.json`` records ``chunkerVersion: 3`` and a
``useBuzhash`` boolean. Arq's specific parameters (window size, mask,
min / max chunk size, and the precomputed lookup table) are **not
published**: ``arq_restore`` is restore-only and contains zero
chunker code, the spec doesn't list them, and the docker-monitor
reference doesn't address them.

Crucially, **chunker parameters do not have to match Arq.app for the
written backup to be valid**: restore reassembles file content by
concatenating ``dataBlobKey`` plaintexts in the order the writer
recorded them. Any deterministic chunker produces a backup that
Arq.app, our reader, and ``arq_restore`` can all restore correctly.
The downside of a non-matching chunker is purely poor dedup of
partially-modified files against an existing Arq.app backup; new
backups are unaffected.

This module therefore ships a generic, well-tested Buzhash chunker
with sensible defaults (~32 KB average chunk, 4 KB min, 1 MB max,
48-byte window). Future iterations can swap in matching Arq
parameters once they're known without changing the public interface.

### Algorithm

Buzhash (Uzgalis, 1973): the hash of an ``n``-byte window is

    H(x_0..x_{n-1}) = ROL(T[x_0], n-1) ⊕ ROL(T[x_1], n-2) ⊕ … ⊕ T[x_{n-1}]

where ``T`` is a precomputed table from byte to 32-bit value and ROL
is left-rotation. When the window slides by one byte (x_out leaves,
x_in enters):

    H_new = ROL(H_old, 1) ⊕ ROL(T[x_out], n) ⊕ T[x_in]

We declare a chunk boundary whenever ``H & mask == 0``, subject to
``min_chunk_size`` and ``max_chunk_size`` clamps.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Iterable, Iterator, Optional


WORD_BITS = 32
WORD_MASK = 0xFFFFFFFF


def _build_default_table(seed: int = 0xA84_57F2C) -> tuple:
    """Build the 256-entry 32-bit lookup table.

    Deterministic per ``seed`` so a chunked-and-restored byte stream
    is byte-identical across machines + Python versions. The default
    seed is arbitrary but fixed — changing it would change all chunk
    boundaries (and therefore on-disk blob IDs).
    """
    rng = random.Random(seed)
    return tuple(rng.getrandbits(WORD_BITS) for _ in range(256))


_DEFAULT_TABLE = _build_default_table()


def _rol(x: int, n: int) -> int:
    n %= WORD_BITS
    if n == 0:
        return x & WORD_MASK
    return ((x << n) | (x >> (WORD_BITS - n))) & WORD_MASK


@dataclass
class ChunkerConfig:
    """Tuning knobs for :class:`Buzhash`. All defaults are
    arbitrary-but-reasonable; change them only if you have a
    specific dedup target in mind.

    ``window_size``: bytes of context the hash sees at a time.
        48 is a common choice (used by borg, restic with adjustments).
    ``boundary_mask``: chunk boundary occurs at hash & mask == 0.
        15 = 2^15-1 = 32 KiB average chunk; 13 = ~8 KiB; 17 = ~128 KiB.
    ``min_chunk_size``: never close a chunk shorter than this.
    ``max_chunk_size``: force-close after this many bytes regardless
        of the hash.
    """

    window_size: int = 48
    boundary_bits: int = 15
    min_chunk_size: int = 4 * 1024
    max_chunk_size: int = 1 * 1024 * 1024
    table: Optional[tuple] = None        # None = use module default

    def boundary_mask(self) -> int:
        return (1 << self.boundary_bits) - 1


class Buzhash:
    """Stateful Buzhash rolling-hash chunker.

    The expected usage is a single :func:`chunk` call per input
    stream — each call resets internal state. For genuinely
    streaming use cases, instantiate one ``Buzhash`` per stream so
    parallel chunking jobs don't share state.
    """

    def __init__(self, config: Optional[ChunkerConfig] = None) -> None:
        self.config = config or ChunkerConfig()
        if self.config.window_size < 1:
            raise ValueError("window_size must be >= 1")
        if self.config.min_chunk_size < self.config.window_size:
            raise ValueError(
                "min_chunk_size must be >= window_size so the rolling "
                "hash gets a chance to see a full window before any "
                "chunk boundary is even considered"
            )
        if self.config.max_chunk_size < self.config.min_chunk_size:
            raise ValueError("max_chunk_size must be >= min_chunk_size")
        self._table = self.config.table or _DEFAULT_TABLE
        # Precompute ROL(T[b], window_size) for the slide step.
        n = self.config.window_size
        self._table_rol_n = tuple(_rol(t, n) for t in self._table)

    # ------------------------------------------------------------------
    # Hash primitives (exposed for unit testing)
    # ------------------------------------------------------------------

    def initial_hash(self, window: bytes) -> int:
        """Compute the Buzhash of a fresh window from scratch."""
        if len(window) != self.config.window_size:
            raise ValueError(
                f"window must be exactly {self.config.window_size} bytes"
            )
        h = 0
        n = self.config.window_size
        for i, b in enumerate(window):
            h ^= _rol(self._table[b], n - 1 - i)
        return h & WORD_MASK

    def slide(self, current_hash: int, byte_out: int, byte_in: int) -> int:
        """Roll the hash one byte forward."""
        return (
            _rol(current_hash, 1)
            ^ self._table_rol_n[byte_out]
            ^ self._table[byte_in]
        ) & WORD_MASK

    # ------------------------------------------------------------------
    # Chunker
    # ------------------------------------------------------------------

    def chunk(self, data: bytes) -> Iterator[bytes]:
        """Split ``data`` into variable-length chunks.

        Yields each chunk's bytes. The concatenation of all yielded
        chunks equals ``data`` exactly (the chunker is loss-free).
        """
        n = len(data)
        if n == 0:
            return
        cfg = self.config
        if n <= cfg.min_chunk_size:
            yield data
            return

        mask = cfg.boundary_mask()
        win = cfg.window_size
        # Anchor chunk start, and seed the rolling hash on the first
        # window once we cross the min-chunk threshold.
        chunk_start = 0
        while chunk_start < n:
            # Force-close at max chunk size.
            max_end = min(chunk_start + cfg.max_chunk_size, n)
            min_end = min(chunk_start + cfg.min_chunk_size, n)
            if max_end - chunk_start < cfg.min_chunk_size:
                # Final chunk is shorter than min — that's allowed,
                # it's the tail of the stream.
                yield data[chunk_start:max_end]
                chunk_start = max_end
                continue
            # Seed the hash on the window ending exactly at min_end.
            window_start = min_end - win
            h = self.initial_hash(data[window_start:min_end])
            # Already at min chunk size — does the window-end position
            # itself cleave a boundary? (Cleaner than peeking ahead.)
            if (h & mask) == 0:
                yield data[chunk_start:min_end]
                chunk_start = min_end
                continue
            # Slide forward looking for a boundary, up to max_end.
            i = min_end
            found = False
            while i < max_end:
                byte_out = data[i - win]
                byte_in = data[i]
                h = self.slide(h, byte_out, byte_in)
                i += 1
                if (h & mask) == 0:
                    yield data[chunk_start:i]
                    chunk_start = i
                    found = True
                    break
            if not found:
                # Reached max_end without finding a boundary. Force-close.
                yield data[chunk_start:max_end]
                chunk_start = max_end


def chunk_bytes(
    data: bytes, *, config: Optional[ChunkerConfig] = None,
) -> Iterable[bytes]:
    """Convenience wrapper. Equivalent to ``Buzhash(config).chunk(data)``."""
    return Buzhash(config).chunk(data)


# Arq.app v8's apparent fixed-blob cap when the plan has
# ``useBuzhash: False`` — sampled 2026-05-10 from a 91 MB SQLite
# DB inside the operator's destination, which split into
# (40,000,000 + 40,000,000 + 11,815,936) bytes. The constant is
# decimal 40 million, NOT 40 MiB (41,943,040). HANDOFF.md GAP-L.
FIXED_CHUNK_SIZE_ARQ_V8 = 40_000_000


class FixedChunker:
    """Emit ``data`` as back-to-back fixed-size chunks.

    This is the chunker Arq.app v8 uses when the backup plan has
    ``useBuzhash: False``. Default chunk size is
    :data:`FIXED_CHUNK_SIZE_ARQ_V8` (40,000,000 bytes), the value
    Arq.app v8 actually emits in that mode (see HANDOFF.md GAP-L
    for the sampling).

    The split is purely positional — no rolling-hash content
    detection. A 91 MB input therefore deterministically becomes
    three blobs of (40M, 40M, 11.8M) bytes, which matches Arq.app
    byte-for-byte at the chunk-boundary level. Combined with the
    salt-shared ``compute_blob_id`` from PR #57 (Strategy E),
    that's enough to write a destination whose blob_ids align
    with whatever Arq.app would produce on the same source under
    the same plan.

    Use it from the writer via ``--chunker fixed-40m`` (the
    value :data:`FIXED_CHUNK_SIZE_ARQ_V8` is exposed only because
    advanced operators on different Arq.app versions may need to
    override it).
    """

    def __init__(
        self,
        chunk_size: int = FIXED_CHUNK_SIZE_ARQ_V8,
    ) -> None:
        if chunk_size < 1:
            raise ValueError(
                f"chunk_size must be >= 1, got {chunk_size}"
            )
        self.chunk_size = int(chunk_size)

    def chunk(self, data: bytes) -> Iterator[bytes]:
        """Yield ``data`` in successive ``chunk_size``-byte slices.

        The trailing slice is whatever's left over (size in
        ``[1, chunk_size]``); empty input produces no chunks at
        all (matching :class:`Buzhash`'s same-shape behaviour, so
        the writer's existing zero-bytes short-circuit keeps
        working).
        """
        if not data:
            return
        n = len(data)
        cs = self.chunk_size
        offset = 0
        while offset < n:
            end = offset + cs
            if end > n:
                end = n
            yield data[offset:end]
            offset = end


# ---------------------------------------------------------------------------
# Multi-version registry for Arq compatibility
# ---------------------------------------------------------------------------
#
# Arq's ``backupconfig.json`` carries a ``chunkerVersion`` (1, 2, 3,
# ...) and a per-folder ``useBuzhash`` boolean. Different combinations
# (e.g. ``chunkerVersion=3`` + ``useBuzhash=true``) use different
# parameters; the spec doesn't publish them.
#
# This registry maps known ``(chunkerVersion, useBuzhash)`` tuples
# to the best ``ChunkerConfig`` we have for that variant. Empty
# entries fall back to ``DEFAULT`` (our generic Buzhash params),
# documented in the research doc as "non-matching but correct".
#
# When the parameters are reverse-engineered (via Mach-O scan or
# behavioral inference — see ``macho_buzhash_finder.py``) they're
# inserted here. The writer's high-level ``chunker_for_arq`` helper
# always returns a ChunkerConfig — never raises — so callers can
# write backups even when we don't yet know the exact match.


# Generic fallback (= our default ChunkerConfig).
GENERIC_DEFAULT = ChunkerConfig()

# Known variants live in this dict. Empty for now: see
# docs/RESEARCH-format-extensions.md §4 for the RE recipe.
_ARQ_PARAM_REGISTRY: Dict[Tuple[int, bool], ChunkerConfig] = {}


def register_arq_chunker(
    chunker_version: int, use_buzhash: bool, config: ChunkerConfig,
) -> None:
    """Plug in a (chunkerVersion, useBuzhash) → ChunkerConfig binding.

    Use this from a setup script after RE has produced concrete
    parameters, e.g.::

        from arq_writer.chunker import (
            ChunkerConfig, register_arq_chunker,
        )
        register_arq_chunker(3, True, ChunkerConfig(
            window_size=64,
            boundary_bits=15,
            min_chunk_size=4096,
            max_chunk_size=1048576,
            table=... ,    # 256-tuple from the Mach-O T table
        ))
    """
    _ARQ_PARAM_REGISTRY[(chunker_version, use_buzhash)] = config


def chunker_for_arq(
    chunker_version: int = 3, use_buzhash: bool = True,
) -> ChunkerConfig:
    """Return the registered ChunkerConfig for an Arq variant, or
    the generic default if unregistered.

    The fallback is documented in the research doc: any deterministic
    chunker produces a valid Arq.app-restorable backup; matching
    Arq's exact boundaries only matters for cross-backup dedup.
    """
    return _ARQ_PARAM_REGISTRY.get(
        (chunker_version, use_buzhash), GENERIC_DEFAULT,
    )


def known_arq_variants() -> List[Tuple[int, bool]]:
    """List currently-registered (chunker_version, use_buzhash) pairs."""
    return sorted(_ARQ_PARAM_REGISTRY.keys())


# Type-only import so the type hints above don't need it earlier.
from typing import Dict, List, Tuple  # noqa: E402

