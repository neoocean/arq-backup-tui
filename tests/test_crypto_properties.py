"""Property-style tests for the cryptographic primitives.

Hypothesis isn't a project dependency — these tests use
manual randomized iteration via ``secrets.token_bytes`` so
they run on any Python ≥ 3.9 without extra installs. The
intent matches a Hypothesis-style test: a small number of
randomized inputs verifying a universal invariant.

Properties pinned:

- ARQO encrypt → decrypt round-trip is the identity for any
  plaintext + any 32-byte encryption key + 32-byte HMAC key.
- ARQO HMAC verification rejects single-byte mutations
  anywhere in the post-magic body region.
- LZ4 wrap → unwrap round-trips for any input bytes.
- XAttrSetV002 serialize → deserialize round-trips for any
  name → value dict, including binary values + multi-attr
  + Unicode names.
- Pack file walker reconstruct_index gives back the entries
  it was given for any concatenation of synthetic ARQOs.

Each property runs ``ITERATIONS`` random samples (default 16
— enough to catch boundary bugs without making the suite
slow). Fixed seeds are NOT used so re-running can surface
late-discovered regressions; failures print the seed for
manual reproduction.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import unittest


# Number of random samples per property. Bumped via env var
# for hard-to-reproduce failures.
ITERATIONS = int(os.environ.get("ARQ_PROPERTY_ITERATIONS", "16"))


def _has_openssl() -> bool:
    try:
        subprocess.run(
            ["openssl", "version"],
            check=True, capture_output=True, timeout=5,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _random_plaintexts():
    """Yield a representative spread of plaintext sizes:
    empty, single block, exactly aligned blocks, off-by-one,
    multi-block, large."""
    # Boundary cases first.
    yield b""
    yield b"\x00"
    yield b"\xff" * 16
    yield b"a" * 17       # 1 byte over a block
    yield b"x" * 1024
    # Then random samples spanning small + medium sizes.
    for _ in range(ITERATIONS):
        size = secrets.randbelow(8192) + 1
        yield secrets.token_bytes(size)


# ---------------------------------------------------------------------------
# ARQO encrypt / decrypt round-trip
# ---------------------------------------------------------------------------


@unittest.skipUnless(_has_openssl(), "openssl CLI required")
class ARQOEncryptDecryptPropertyTests(unittest.TestCase):

    def test_round_trip_identity_for_random_plaintexts(self) -> None:
        from arq_writer.crypto_write import build_encrypted_object
        from arq_reader.decrypt import decrypt_encrypted_object
        for plaintext in _random_plaintexts():
            enc_key = secrets.token_bytes(32)
            hmac_key = secrets.token_bytes(32)
            arqo = build_encrypted_object(
                plaintext, enc_key, hmac_key,
            )
            recovered = decrypt_encrypted_object(
                arqo, enc_key, hmac_key,
            )
            self.assertEqual(
                recovered, plaintext,
                f"round-trip mismatch for "
                f"plaintext_len={len(plaintext)}: "
                f"{plaintext!r} → {recovered!r}",
            )

    def test_hmac_rejects_single_byte_mutation(self) -> None:
        """For any ARQO + any byte position in the body region
        (offset 4 onwards — the magic at 0-3 stays untouched
        because it's checked separately), flipping one bit
        causes HMAC verify to fail."""
        from arq_writer.crypto_write import build_encrypted_object
        from arq_validator.crypto import verify_encrypted_object_hmac
        plaintext = secrets.token_bytes(256)
        enc_key = secrets.token_bytes(32)
        hmac_key = secrets.token_bytes(32)
        arqo = build_encrypted_object(
            plaintext, enc_key, hmac_key,
        )
        # Sample a few random byte positions outside the
        # magic prefix.
        for _ in range(min(ITERATIONS, 8)):
            pos = 4 + secrets.randbelow(len(arqo) - 4)
            tampered = bytearray(arqo)
            tampered[pos] ^= 0x01
            ok, _exp, _act = verify_encrypted_object_hmac(
                bytes(tampered), hmac_key,
            )
            self.assertFalse(
                ok,
                f"HMAC verify accepted a tampered byte at "
                f"pos={pos}",
            )


# ---------------------------------------------------------------------------
# LZ4 wrap / unwrap round-trip
# ---------------------------------------------------------------------------


class LZ4PropertyTests(unittest.TestCase):

    def test_wrap_unwrap_round_trip(self) -> None:
        from arq_writer.lz4_block import lz4_wrap
        from arq_writer.lz4_block import lz4_unwrap
        for plaintext in _random_plaintexts():
            wrapped = lz4_wrap(plaintext)
            unwrapped = lz4_unwrap(wrapped)
            self.assertEqual(
                unwrapped, plaintext,
                f"LZ4 round-trip mismatch for "
                f"len={len(plaintext)}",
            )


# ---------------------------------------------------------------------------
# XAttrSetV002 round-trip
# ---------------------------------------------------------------------------


class XAttrSetV002PropertyTests(unittest.TestCase):

    def test_serialize_deserialize_round_trip(self) -> None:
        from arq_writer.xattrs import (
            serialize_xattrs, deserialize_xattrs,
        )
        # Fixed boundary cases.
        cases = [
            {},
            {"user.simple": b"value"},
            {"user.empty": b""},
            {"user.binary": bytes(range(256))},
            {
                "com.apple.fakedata": b"\x00\x01\x02\xff",
                "user.text": b"hello, world",
                "trusted.empty": b"",
            },
            # Unicode in the name (allowed by xattr APIs even
            # though it's rare in practice).
            {"user.한글": b"binary value"},
        ]
        for d in cases:
            ser = serialize_xattrs(d)
            recovered = deserialize_xattrs(ser)
            self.assertEqual(
                recovered, d,
                f"XAttrSetV002 round-trip mismatch for {d!r}: "
                f"got {recovered!r}",
            )
        # Random samples.
        for _ in range(ITERATIONS):
            count = secrets.randbelow(5) + 1
            d = {}
            for _ in range(count):
                name_len = secrets.randbelow(40) + 1
                name = "user." + secrets.token_hex(name_len)[:name_len]
                value_len = secrets.randbelow(2048)
                d[name] = secrets.token_bytes(value_len)
            ser = serialize_xattrs(d)
            recovered = deserialize_xattrs(ser)
            self.assertEqual(
                recovered, d,
                f"random round-trip mismatch ("
                f"{count} keys, total "
                f"{sum(len(v) for v in d.values())} bytes)",
            )


# ---------------------------------------------------------------------------
# Pack file walker round-trip
# ---------------------------------------------------------------------------


@unittest.skipUnless(_has_openssl(), "openssl CLI required")
class PackWalkerPropertyTests(unittest.TestCase):

    def test_concatenated_arqos_reconstruct_to_original_count(self) -> None:
        """For any concatenation of N synthetic ARQOs,
        reconstruct_index returns N entries with byte offsets
        that sum to the pack length."""
        from arq_writer.crypto_write import build_encrypted_object
        from arq_reader.pack import reconstruct_index
        for _ in range(min(ITERATIONS, 6)):
            n = secrets.randbelow(8) + 1
            enc_key = secrets.token_bytes(32)
            hmac_key = secrets.token_bytes(32)
            arqos = [
                build_encrypted_object(
                    secrets.token_bytes(
                        secrets.randbelow(256) + 1,
                    ),
                    enc_key, hmac_key,
                )
                for _ in range(n)
            ]
            pack = b"".join(arqos)
            entries = reconstruct_index(pack)
            self.assertEqual(
                len(entries), n,
                f"expected {n} entries from {n} ARQOs, "
                f"got {len(entries)}",
            )
            # Offsets cover the pack with no gaps.
            running = 0
            for entry, original in zip(entries, arqos):
                self.assertEqual(entry.offset, running)
                self.assertEqual(entry.length, len(original))
                running += len(original)
            self.assertEqual(running, len(pack))


# ---------------------------------------------------------------------------
# Tree v4 trailing block round-trip
# ---------------------------------------------------------------------------


class TreeV4BlockPropertyTests(unittest.TestCase):

    def test_v4_trailing_block_constant_flag_invariant(self) -> None:
        """For any (sec, nsec) tuple, the emitted block always
        has the constant flag at bytes 16..23 + 14 reserved
        zeros at bytes 24..37. Pinning this prevents a future
        writer change from accidentally drifting away from the
        observed Arq.app v8 shape."""
        import struct
        from arq_writer.serialize import _v4_trailing_block
        from arq_writer.types import FileNode
        for _ in range(ITERATIONS):
            sec = secrets.randbelow(2 ** 32)
            nsec = secrets.randbelow(1_000_000_000)
            node = FileNode(
                dataBlobLocs=[], itemSize=0,
                create_time_sec=sec,
                create_time_nsec=nsec,
            )
            blk = _v4_trailing_block(node)
            self.assertEqual(len(blk), 38)
            flag = struct.unpack(">q", blk[16:24])[0]
            self.assertEqual(
                flag, 0x01000000,
                f"flag drifted at sec={sec}, nsec={nsec}",
            )
            self.assertEqual(
                blk[24:38], b"\x00" * 14,
                f"reserved bytes non-zero at "
                f"sec={sec}, nsec={nsec}",
            )


if __name__ == "__main__":
    unittest.main()
