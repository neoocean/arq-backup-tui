"""Tests for the Arq 7 pack file walker.

The Arq 7 pack format is deliberately frame-less: pack files are
the concatenation of ``EncryptedObject`` (ARQO) blobs with no
header, no per-entry length prefix, and no companion ``.index``.
:mod:`arq_reader.pack` reconstructs the implicit index by
scanning forward for ARQO magic bytes.

These tests exercise:

- :func:`reconstruct_index` on a clean pack: entry offsets +
  lengths must sum to the full pack size.
- The same on a torn pack (last entry truncated mid-ciphertext):
  the trailing entry is flagged ``truncated=True``.
- :func:`decode_pack` round-trips each blob through HMAC verify +
  AES-CBC decrypt and yields the original plaintexts in order.
- Bad HMAC raises :class:`PackError` rather than silently
  skipping — silent skip would mask real corruption.
- Empty input returns an empty index, not an error.
- :func:`pack_summary` reports entry count + payload bytes
  matching the construction input.
"""

from __future__ import annotations

import os
import secrets
import unittest
from pathlib import Path
from typing import List

# Skip the whole module on environments missing OpenSSL — the
# whole point is to exercise the real ARQO encryption path.
try:
    import subprocess
    subprocess.run(
        ["openssl", "version"],
        check=True, capture_output=True, timeout=5,
    )
    HAS_OPENSSL = True
except Exception:
    HAS_OPENSSL = False


def _build_arqo(
    plaintext: bytes,
    enc_key: bytes,
    hmac_key: bytes,
) -> bytes:
    """Wrap ``plaintext`` in an ARQO using the writer's helper.

    The writer's ``build_encrypted_object`` is the canonical
    source for the on-disk format; using it directly ensures the
    test fixture matches what the reader is expected to handle
    in production.
    """
    from arq_writer.crypto_write import build_encrypted_object
    return build_encrypted_object(plaintext, enc_key, hmac_key)


@unittest.skipUnless(HAS_OPENSSL, "openssl CLI required")
class ReconstructIndexTests(unittest.TestCase):
    """Pure-byte structural scan, no keys involved."""

    def setUp(self) -> None:
        # Two independent ARQOs so the offsets aren't trivially 0.
        self.enc_key = secrets.token_bytes(32)
        self.hmac_key = secrets.token_bytes(32)
        self.plaintexts: List[bytes] = [
            b"hello, world\n",
            b"second blob - slightly different length",
            secrets.token_bytes(48),
        ]
        self.arqos = [
            _build_arqo(p, self.enc_key, self.hmac_key)
            for p in self.plaintexts
        ]

    def test_clean_pack_yields_entry_per_arqo(self) -> None:
        from arq_reader.pack import reconstruct_index
        pack = b"".join(self.arqos)
        entries = reconstruct_index(pack)
        self.assertEqual(len(entries), len(self.arqos))
        # Offsets cover the pack with no gaps.
        running = 0
        for entry, arqo in zip(entries, self.arqos):
            self.assertEqual(entry.offset, running)
            self.assertEqual(entry.length, len(arqo))
            self.assertFalse(entry.truncated)
            running += len(arqo)
        self.assertEqual(running, len(pack))

    def test_empty_pack_returns_empty(self) -> None:
        from arq_reader.pack import reconstruct_index
        self.assertEqual(reconstruct_index(b""), [])

    def test_pack_without_arqo_magic_raises(self) -> None:
        from arq_reader.pack import PackError, reconstruct_index
        with self.assertRaises(PackError):
            reconstruct_index(b"\x00" * 256)

    def test_truncated_tail_is_flagged(self) -> None:
        from arq_reader.pack import reconstruct_index
        pack = b"".join(self.arqos)
        # Lop off ~20 bytes of the last entry's ciphertext.
        torn = pack[: len(pack) - 20]
        entries = reconstruct_index(torn)
        # We should still get all three entries, the last marked
        # truncated. (The walker can't know whether 20 bytes are
        # missing or 200, but it can flag the tail.)
        self.assertEqual(len(entries), len(self.arqos))
        self.assertTrue(entries[-1].truncated)
        self.assertFalse(entries[0].truncated)


@unittest.skipUnless(HAS_OPENSSL, "openssl CLI required")
class DecodePackTests(unittest.TestCase):
    """End-to-end: scan + HMAC verify + AES decrypt."""

    def setUp(self) -> None:
        self.enc_key = secrets.token_bytes(32)
        self.hmac_key = secrets.token_bytes(32)
        self.plaintexts = [
            b"alpha", b"beta", b"gamma" * 100,
            secrets.token_bytes(72),
        ]
        self.arqos = [
            _build_arqo(p, self.enc_key, self.hmac_key)
            for p in self.plaintexts
        ]

    def test_decode_round_trips_every_blob(self) -> None:
        from arq_reader.pack import decode_pack
        pack = b"".join(self.arqos)
        recovered = list(decode_pack(
            pack, self.enc_key, self.hmac_key,
        ))
        self.assertEqual(len(recovered), len(self.plaintexts))
        for (_entry, plaintext), expected in zip(
            recovered, self.plaintexts,
        ):
            self.assertEqual(plaintext, expected)

    def test_decode_skip_truncated_drops_torn_tail(self) -> None:
        from arq_reader.pack import decode_pack
        pack = b"".join(self.arqos)[: -8]
        recovered = list(decode_pack(
            pack, self.enc_key, self.hmac_key, skip_truncated=True,
        ))
        # Drops one entry (the torn one) but reads the rest cleanly.
        self.assertEqual(len(recovered), len(self.plaintexts) - 1)

    def test_decode_raises_on_torn_tail_when_strict(self) -> None:
        from arq_reader.pack import PackTruncated, decode_pack
        pack = b"".join(self.arqos)[: -8]
        gen = decode_pack(
            pack, self.enc_key, self.hmac_key, skip_truncated=False,
        )
        with self.assertRaises(PackTruncated):
            list(gen)

    def test_decode_raises_on_hmac_corruption(self) -> None:
        from arq_reader.pack import PackError, decode_pack
        # Flip a single byte of the second entry's HMAC area
        # (offset 4..36 within the second ARQO).
        pack = bytearray(b"".join(self.arqos))
        target = len(self.arqos[0]) + 8   # mid-HMAC of arqo[1]
        pack[target] ^= 0x01
        with self.assertRaises(PackError):
            list(decode_pack(
                bytes(pack), self.enc_key, self.hmac_key,
            ))


@unittest.skipUnless(HAS_OPENSSL, "openssl CLI required")
class PackSummaryTests(unittest.TestCase):
    def test_summary_counts_and_total_match_pack(self) -> None:
        from arq_reader.pack import pack_summary
        enc = secrets.token_bytes(32)
        hk = secrets.token_bytes(32)
        arqos = [
            _build_arqo(b"x" * (i + 1) * 7, enc, hk)
            for i in range(5)
        ]
        pack = b"".join(arqos)
        s = pack_summary(pack)
        self.assertEqual(s.total_size, len(pack))
        self.assertEqual(s.entry_count, 5)
        self.assertEqual(s.payload_bytes, len(pack))
        self.assertFalse(s.truncated_tail)


if __name__ == "__main__":
    unittest.main()
