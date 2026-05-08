"""Pure-Python LZ4 block-format codec.

Arq 7 wraps every blob, ``Tree``, and ``Node`` in
``[UInt32 BE original_length] || lz4_block_data``. The block format is
documented at <https://github.com/lz4/lz4/blob/dev/doc/lz4_Block_format.md>.

The encoder produced here is **literal-only** — every input is emitted
as a single literal sequence with no back-references. That's the
simplest possible valid LZ4 block: it round-trips losslessly through
any conformant decoder, including Apple's, ``arq_restore``'s, and the
reference one. The trade-off is no compression ratio gain — output is
``~len(input) + 1 + ⌈(len(input) - 15) / 255⌉`` bytes. Files in a
backup are already individually small (one blob per file at v0), and
``Tree`` / ``Node`` records are tiny (a few hundred bytes); the
absolute size hit is modest.

A spec-correct decoder is included so tests can round-trip both
arms of the codec without external dependencies. It also lets the
writer's tests verify what the validator's reader will see.
"""

from __future__ import annotations

import struct


def lz4_block_compress(data: bytes) -> bytes:
    """Emit ``data`` as a single literal sequence (no back-references).

    Output is a valid LZ4 block — any conformant decoder (Apple's,
    ``arq_restore``'s, Python's ``lz4`` package, the reference C
    implementation) will reconstruct ``data`` exactly.
    """
    n = len(data)
    if n == 0:
        # Empty literal: token with literal_length=0, match_length=0.
        return b"\x00"
    out = bytearray()
    if n < 15:
        out.append(n << 4)
    else:
        out.append(0xF0)
        rem = n - 15
        while rem >= 255:
            out.append(0xFF)
            rem -= 255
        out.append(rem)
    out += data
    return bytes(out)


def lz4_block_decompress(block: bytes, expected_size: int = -1) -> bytes:
    """Decode an LZ4 block back to plaintext.

    ``expected_size >= 0`` activates a sanity-check against the decoded
    length; pass ``-1`` to skip. Raises ``ValueError`` for malformed
    blocks (truncated tokens, references past start, length mismatches).
    """
    out = bytearray()
    i = 0
    end = len(block)
    while i < end:
        token = block[i]; i += 1
        lit_len = (token >> 4) & 0x0F
        if lit_len == 15:
            while i < end:
                b = block[i]; i += 1
                lit_len += b
                if b != 0xFF:
                    break
        if i + lit_len > end:
            raise ValueError("lz4: literals run past end of block")
        out += block[i : i + lit_len]
        i += lit_len
        if i == end:
            break
        if i + 2 > end:
            raise ValueError("lz4: truncated match offset")
        offset = block[i] | (block[i + 1] << 8)
        i += 2
        if offset == 0:
            raise ValueError("lz4: zero match offset is invalid")
        match_len = token & 0x0F
        if match_len == 15:
            while i < end:
                b = block[i]; i += 1
                match_len += b
                if b != 0xFF:
                    break
        match_len += 4   # minimum match length
        start = len(out) - offset
        if start < 0:
            raise ValueError("lz4: match references before start")
        # LZ4 allows overlapping copies (RLE-style); copy byte-by-byte
        # to handle that correctly.
        for k in range(match_len):
            out.append(out[start + k])
    if expected_size >= 0 and len(out) != expected_size:
        raise ValueError(
            f"lz4: decompressed length {len(out)} != expected {expected_size}"
        )
    return bytes(out)


def lz4_wrap(data: bytes) -> bytes:
    """Apply Arq's outer wrapper: 4-byte BE length then LZ4 block."""
    return struct.pack(">I", len(data)) + lz4_block_compress(data)


def lz4_unwrap(wrapped: bytes) -> bytes:
    """Reverse ``lz4_wrap``."""
    if len(wrapped) < 4:
        raise ValueError("lz4_unwrap: input too short for length prefix")
    expected = struct.unpack(">I", wrapped[:4])[0]
    return lz4_block_decompress(wrapped[4:], expected_size=expected)
