"""Round-trip tests for the pure-Python LZ4 codec."""

from __future__ import annotations

import secrets
import struct
import unittest

from arq_writer.lz4_block import (
    lz4_block_compress,
    lz4_block_decompress,
    lz4_unwrap,
    lz4_wrap,
)


class Lz4BlockTests(unittest.TestCase):
    def test_empty(self) -> None:
        self.assertEqual(lz4_block_decompress(lz4_block_compress(b"")), b"")

    def test_short(self) -> None:
        for n in (1, 5, 14, 15, 16, 17, 50, 100, 270):
            data = bytes((i * 13 + 7) & 0xFF for i in range(n))
            self.assertEqual(
                lz4_block_decompress(lz4_block_compress(data), expected_size=n),
                data,
                f"failed at n={n}",
            )

    def test_long(self) -> None:
        for n in (1024, 4096, 65535, 65536, 100_000):
            data = secrets.token_bytes(n)
            self.assertEqual(
                lz4_block_decompress(lz4_block_compress(data), expected_size=n),
                data,
            )

    def test_wrap_roundtrip(self) -> None:
        for n in (0, 1, 100, 10_000):
            data = secrets.token_bytes(n)
            wrapped = lz4_wrap(data)
            self.assertEqual(struct.unpack(">I", wrapped[:4])[0], n)
            self.assertEqual(lz4_unwrap(wrapped), data)

    def test_decompress_size_check(self) -> None:
        with self.assertRaises(ValueError):
            lz4_block_decompress(lz4_block_compress(b"abc"), expected_size=99)

    def test_truncated_block_rejected(self) -> None:
        # token says 20 literals but we only give 5 — must error.
        bad = bytes([0xF0, 0x05]) + b"abcde"   # high4=15, ext=5 → 20 literals
        with self.assertRaises(ValueError):
            lz4_block_decompress(bad)


if __name__ == "__main__":
    unittest.main()
