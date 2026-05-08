"""Reverse-engineered chunker parameters for Arq.app v7.

This module ships the concrete Buzhash parameters extracted from the
Arq.app v7.41 macOS executable, so that backups produced by this
writer dedup byte-for-byte against partially-modified files in an
existing Arq.app destination.

### Provenance

All values below were obtained by static analysis of
``/Applications/Arq.app/Contents/MacOS/Arq`` (Arq.app v7.41, x86_64
Mach-O) using :mod:`arq_writer.macho_buzhash_finder`. The complete
analyst's recipe is documented in
``docs/RESEARCH-format-extensions.md`` §4.2.

- **T table**: 1024 bytes (256 × uint32, little-endian) starting at
  binary offset ``5,104,816``. Identified as the unique top-scoring
  candidate window (entropy ≈ 7.83 bits/byte, 256 distinct uint32
  values, 16-byte aligned). The same offset reproduces across reruns
  of ``arq-buzhash-find analyze-binary``.
- **window_size = 256 bytes**: the constant ``256`` appears twice
  within 30 bytes of code at offsets ``-2628`` and ``-2608``
  relative to the T-table — adjacent ``MOV r32, 0x100`` instructions
  in what is almost certainly the chunker's window-init prologue.
- **boundary_bits = 16** (mask = ``0xFFFF`` = 65535, average chunk
  ≈ 64 KiB): the constant ``65536`` appears twice near the chunker
  code (offsets ``-2609`` and ``-2535``). Across the full binary,
  ``mask=16bit`` patterns vastly dominate other plausible mask
  widths (725 hits vs. 13 for 13-bit and 72 for 17-bit).
- **max_chunk_size = 131072** (128 KiB): the constant ``131072``
  appears once in the chunker code area (offset ``-2651``).
- **min_chunk_size = 4096** (4 KiB): conservative inference. No
  direct binary evidence, but 4 KiB is the universal lower bound
  for content-defined chunkers (matches borg, restic, casync, and
  Arq's own minimum metadata-block size). Setting this too low
  cannot produce wrong restores; it only affects dedup ratio.

### Confidence

- T table bytes:    **high** (deterministic offset, validated shape)
- window_size:      **high**
- boundary_bits:    **high**
- max_chunk_size:   **medium**
- min_chunk_size:   **low** (conservative guess)

A future verification step would feed a known input through Arq.app,
read back the resulting ``BlobLoc.length`` values, and confirm chunk
boundaries match. That falsification test is not yet automated.

### Use

The constants are *registered* on import side-effect when this module
is imported, but not auto-imported by ``arq_writer/__init__.py`` —
a caller that wants to opt into Arq.app-matching parameters must::

    import arq_writer.arq_chunker_params  # noqa: F401  (registration)

or call :func:`install` explicitly. Generic-default behavior stays
the default; users get exact-match dedup only if they ask for it.
"""

from __future__ import annotations

import struct
from typing import Tuple

from .chunker import ChunkerConfig, register_arq_chunker


# -----------------------------------------------------------------------------
# T table (1024 bytes from Arq.app v7.41 @ binary offset 5,104,816)
# -----------------------------------------------------------------------------
#
# 64 rows × 16 bytes = 1024 bytes total. Each line below is one 16-byte
# row of the original hex dump, presented verbatim so a future analyst
# can diff it against a fresh extraction without any decoding step.
_ARQ_V7_TABLE_HEX = """
be 16 40 a4 a6 f6 81 c5 b4 2a 85 05 84 11 53 10
11 16 6e 5b e7 ab 34 15 7c 69 fd cd 4a 87 a1 d2
8c e7 c9 bc 9c c1 02 ff 21 bf 12 34 77 05 86 98
d3 cb ab a4 ab e0 50 57 2f 13 89 9d dd 02 d2 e9
11 19 78 aa ad 03 a2 f2 92 92 a2 3b 1d 32 f1 24
a9 a7 f8 ef 7a 37 96 22 da 2e 74 eb d1 32 68 5f
90 b8 47 bc 59 f2 2f e8 b3 e9 23 c7 1e cc dd 8f
37 02 c3 d2 04 79 5d ee 3c 31 96 dc ec 0b 79 96
7a 47 96 a0 26 cc 78 e8 c8 e9 33 6c d1 cb da 1c
f8 d2 51 43 ad 72 8e b2 a4 ea e3 81 21 7f db a2
5b 90 b2 a8 a2 df 49 1f 7c 87 b3 1c bd fc ec f1
e2 25 13 db aa 0c 9a a4 c6 8a 7a 21 d1 43 8f 55
1d 7a c2 01 dc 21 4d eb 7a 08 63 34 d3 9c 62 0b
74 47 fc 6c a1 b0 03 7b e2 80 5e e7 43 2c 17 a2
ad bd ea 7b 7c 2c 90 82 34 56 5d 82 d8 ca f3 fd
43 bb 7d 0f 3f ac 1b 6d 7e 1b e0 c3 da d0 12 13
44 65 e4 5a ac cd 85 fd 60 5d 84 09 7e 19 56 e7
15 53 b9 1c 2b 17 fa bf 03 ee 1f 96 45 94 09 e4
d4 29 f4 a5 36 4f 97 c2 34 bc cd 62 5d 17 dd 8e
b7 b0 61 b0 52 6e 60 46 dc fe 66 de b1 7b 2e 91
4d d8 8f 31 2f cd 1a 6a 7a 6c ad f5 12 b0 69 6b
9e 8d 27 d5 80 0e c2 ba b3 8b 32 f5 d4 f7 7f 53
d0 ae 3a 77 a2 c7 74 4b 27 53 78 b8 61 bb 2f d1
50 67 f5 12 85 28 86 83 5c 77 04 49 d5 d5 fb 41
41 eb 33 f4 72 25 70 5c 06 fe 1e e0 af 1e 22 d9
1d 5b 79 a6 25 b1 5b c7 a3 70 0f 64 d2 08 f9 5f
97 59 14 3f c5 82 43 ed 45 87 16 b3 c4 bc 0a 18
06 74 9d 06 d0 aa cd c9 f2 da 98 fe 0f 15 9f 5b
0c d7 17 ac 80 88 a9 89 0e 7c 67 bf d0 27 d2 fd
95 e5 4a 61 f9 0c aa 2a 45 b4 8a 1e 8e e7 97 3f
42 03 a1 a2 a5 c2 2f 9f 50 26 85 24 16 32 5c da
85 c1 2d 35 3b 96 2f d5 8a 7c 62 6e 3f cc 54 d5
90 6a 56 5e 21 ab 2c 92 f4 20 eb 59 53 ac 57 4c
b5 c6 87 73 ba 13 eb 74 d2 f0 f3 c7 f4 b0 df eb
84 2b 04 fe c4 f1 52 af 95 c2 d9 7c e2 a9 76 85
46 48 63 b1 0c 40 66 12 21 23 ec 0b 32 0c f3 1f
c4 c9 ae be ed 03 9d c0 5f bc 66 c5 e9 08 c6 de
d8 e0 1c 82 12 27 fc 04 4e bc 07 45 eb 23 ed a7
3d 09 5e 03 98 ee b6 d0 cf b7 dc 19 56 ad ec c4
65 f7 5a b1 a6 28 0f 84 db 09 27 d6 c6 26 83 a2
ac f3 ea 66 a7 82 db 77 fa 50 ea c3 95 99 95 ad
03 15 d7 51 ac 22 00 60 dd 28 60 9d 5a 89 55 e0
e0 a6 24 e1 3f 57 51 fd 4f 8c 23 13 de ab a5 66
e1 f2 71 8e 3c 8a 20 cf dd 76 3b cc 7b 19 72 13
1f 34 47 44 b1 d5 fa ad 9a 0e c2 c0 1b d0 00 ab
80 91 9b f1 c7 0b 95 80 91 b1 c1 e0 07 ba 0b a7
03 f7 b8 98 74 aa ab 9e 11 38 02 ac 5a dd d8 c7
df f5 3d 73 15 34 17 8c 23 df 16 db 7f d7 8c 85
d0 99 6e a0 14 e2 e5 23 87 db 5c 41 51 1e a2 4f
f9 80 9e ca a0 31 2c 4c df f2 fd 93 01 62 29 df
9f cd 2b c4 eb ca ae 8f 62 17 2a c7 dd f9 72 85
ac f3 d6 35 e7 b0 d8 a8 df eb e1 f9 ec 1b ea 06
18 ee 80 9a 69 c8 63 3d 31 0b f2 3e 19 cb 42 2c
b2 68 fb ab 57 66 12 1a 92 6a 3a 4d a3 26 c7 9d
8a 98 8d f4 5c 11 0f d8 83 5a d9 46 29 83 91 1d
f8 c7 b2 38 ef b9 b7 6f 6f 76 e2 db 8b 58 40 9e
af d3 b6 fa e4 db 90 6b 4d 12 f5 35 08 40 7f f3
80 09 09 1e 3e 60 84 d1 ea aa a4 20 f6 a5 10 19
65 f6 3c d0 b2 73 56 e7 90 92 ca fa 67 e6 31 76
ef 00 2f 0d e9 a6 45 ff be 58 fc 96 4d d8 6c 57
cb 39 27 8a 7a 3f f9 0c ab 79 63 ab 9a 92 c4 2b
a1 09 2f b9 5f 70 f6 9b 1c 3b 5a b6 86 10 e8 e3
1c 48 f4 42 64 5b 09 13 ee 45 e5 ef 30 7f 1b 48
d1 97 f3 08 c8 fc 67 f4 e7 9f 08 c8 48 a8 04 89
"""


def _decode_table(hex_text: str) -> Tuple[int, ...]:
    raw = bytes.fromhex(hex_text)
    if len(raw) != 1024:
        raise RuntimeError(
            f"T-table is {len(raw)} bytes; expected exactly 1024"
        )
    return struct.unpack("<256I", raw)


#: 256-entry uint32 lookup table for the Arq.app v7 Buzhash chunker.
ARQ_V7_BUZHASH_TABLE: Tuple[int, ...] = _decode_table(_ARQ_V7_TABLE_HEX)


#: ``ChunkerConfig`` matching Arq.app v7's chunker parameters.
ARQ_V7_CHUNKER_CONFIG = ChunkerConfig(
    window_size=256,
    boundary_bits=16,
    min_chunk_size=4096,
    max_chunk_size=131072,
    table=ARQ_V7_BUZHASH_TABLE,
)


def install() -> None:
    """Register Arq.app v7 parameters as the canonical (3, True) chunker.

    Idempotent: calling more than once just re-registers the same
    config. After this call, :func:`arq_writer.chunker.chunker_for_arq`
    returns :data:`ARQ_V7_CHUNKER_CONFIG` for the standard Arq 7 variant.
    """
    register_arq_chunker(3, True, ARQ_V7_CHUNKER_CONFIG)


# Auto-register on import. Callers that want generic defaults simply
# don't import this module.
install()
