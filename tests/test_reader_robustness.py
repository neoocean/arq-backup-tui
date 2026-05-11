"""A1 + A5 — reader robustness against malformed / tampered input.

The bytes our reader consumes come from a destination the writer
emitted, but in practice operators also encounter destinations
emitted by Arq.app (which has its own bugs over the years),
destinations corrupted by cosmic rays / SFTP truncation / disk
ECC failure, and (rarely) destinations deliberately tampered
with by an attacker. The reader must handle every shape
gracefully:

- **Graceful failure on malformed input** (truncation, mid-record
  byte flip, header corruption) — no crashes, clear error
  signals, no silent decode.
- **HMAC tamper detection at every byte position** — flipping
  any single bit anywhere in the ARQO body must produce an HMAC
  mismatch (and never a "looks-valid but content-different"
  result).

A1 pins the malformed-input behaviour; A5 pins the HMAC tamper
detection. Both share infrastructure so they live in one module.

The tests use **our writer's own emit as the corpus** — we
produce a known-good blob, then systematically corrupt one byte
at a time and verify the reader's response. This catches a class
of silent-decode regressions that schema checks can't.
"""

from __future__ import annotations

import os
import random
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import List, Tuple


def _has_openssl() -> bool:
    try:
        subprocess.run(
            ["openssl", "version"],
            check=True, capture_output=True, timeout=5,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _build_tiny_backup(td: Path):
    """Build a small backup; return a (dest, keyset, tree_blob_path,
    arqo_bytes) tuple where ``tree_blob_path`` is the on-disk path
    of one tree blob + ``arqo_bytes`` is its raw ARQO content."""
    from arq_writer.backup import build_backup
    from arq_validator import LocalBackend
    from arq_validator.crypto import decrypt_keyset

    src = td / "src"
    src.mkdir()
    (src / "a.txt").write_bytes(b"alpha")
    (src / "subdir").mkdir()
    (src / "subdir" / "b.txt").write_bytes(b"bravo")
    dest = td / "dest"
    res = build_backup(
        str(src), str(dest), encryption_password="pw",
    )
    backend = LocalBackend(str(dest))
    ks = decrypt_keyset(
        backend.read_all(
            f"/{res.computer_uuid}/encryptedkeyset.dat",
        ),
        "pw",
    )
    # Pick a tree blob — the root tree blob has the highest entropy
    # (any tree blob will do for tamper testing). Walk
    # standardobjects/ until we find one we can parse as a tree.
    so_root = dest / res.computer_uuid / "standardobjects"
    from arq_reader.decrypt import decrypt_lz4_arqo
    from arq_reader.parse import parse_tree
    for shard in so_root.iterdir():
        for blob_path in shard.iterdir():
            arqo = blob_path.read_bytes()
            try:
                plain = decrypt_lz4_arqo(
                    arqo, ks.encryption_key, ks.hmac_key,
                )
                parse_tree(plain)
            except Exception:
                continue
            return dest, ks, blob_path, arqo
    raise RuntimeError("could not find a tree blob in the backup")


@unittest.skipUnless(_has_openssl(), "openssl CLI required")
class A1_MalformedInputTests(unittest.TestCase):
    """A1 — malformed input behaviours. Truncation, bad magic,
    impossible header fields. Reader must fail clean (raise or
    return None per its API contract); no silent successes, no
    AttributeError-style crashes."""

    def test_arqo_truncated_to_header_only_fails_clean(self) -> None:
        from arq_reader.decrypt import decrypt_lz4_arqo
        with tempfile.TemporaryDirectory() as td:
            _, ks, _, arqo = _build_tiny_backup(Path(td))
            # Keep just the 56-byte ARQO header + 16-byte IV.
            truncated = arqo[:72]
            with self.assertRaises(Exception) as ctx:
                decrypt_lz4_arqo(
                    truncated, ks.encryption_key, ks.hmac_key,
                )
            # The error should be a specific recognisable type
            # (ValueError, CryptoError, etc.), NOT a generic
            # AttributeError / IndexError that hints at internal
            # state mishandling.
            err_name = type(ctx.exception).__name__
            self.assertNotIn(
                err_name,
                ("AttributeError", "TypeError"),
                f"truncated ARQO produced {err_name} — should be "
                f"a recognised crypto/format error",
            )

    def test_arqo_with_bad_magic_fails_clean(self) -> None:
        from arq_reader.decrypt import decrypt_lz4_arqo
        with tempfile.TemporaryDirectory() as td:
            _, ks, _, arqo = _build_tiny_backup(Path(td))
            # Replace ARQO magic with something else.
            mangled = b"XXXX" + arqo[4:]
            with self.assertRaises(Exception):
                decrypt_lz4_arqo(
                    mangled, ks.encryption_key, ks.hmac_key,
                )

    def test_tree_blob_with_random_byte_flip_fails_clean(self) -> None:
        """For each of 16 randomly-chosen ciphertext byte
        positions, flip the highest bit and confirm the reader
        produces a clean error (HMAC mismatch) rather than a
        crash or silent-decode."""
        from arq_reader.decrypt import decrypt_lz4_arqo
        with tempfile.TemporaryDirectory() as td:
            _, ks, _, arqo = _build_tiny_backup(Path(td))
            # The body starts at byte 36 (4 magic + 32 HMAC).
            # Restrict flips to the body region — header tamper
            # is covered by the magic test above.
            rng = random.Random(20260511)
            body_start = 36
            tested = 0
            for _ in range(16):
                idx = rng.randrange(body_start, len(arqo))
                mangled = bytearray(arqo)
                mangled[idx] ^= 0x80   # flip the high bit
                try:
                    decrypt_lz4_arqo(
                        bytes(mangled), ks.encryption_key, ks.hmac_key,
                    )
                except Exception as exc:
                    # Any exception is fine; specifically check it
                    # isn't an "internal-state" leak.
                    err = type(exc).__name__
                    self.assertNotIn(
                        err, ("AttributeError", "TypeError"),
                        f"byte-flip at {idx} produced internal "
                        f"error {err}: {exc!r}",
                    )
                    tested += 1
                    continue
                self.fail(
                    f"byte-flip at offset {idx} silently succeeded "
                    f"— HMAC should have caught it",
                )
            self.assertGreater(
                tested, 0,
                "expected at least one byte-flip to produce a "
                "graceful error",
            )

    def test_parse_tree_on_random_bytes_fails_clean(self) -> None:
        """Feed parse_tree() randomly-shaped non-tree bytes and
        ensure it raises a recognised error type, not crashes."""
        from arq_reader.parse import parse_tree
        rng = random.Random(2026)
        for _ in range(8):
            n = rng.randrange(0, 1024)
            blob = bytes(rng.getrandbits(8) for _ in range(n))
            with self.assertRaises(Exception) as ctx:
                parse_tree(blob)
            self.assertNotIn(
                type(ctx.exception).__name__,
                ("AttributeError",),
                f"parse_tree on random {n}-byte input crashed via "
                f"internal-state error: {ctx.exception}",
            )


@unittest.skipUnless(_has_openssl(), "openssl CLI required")
class A5_HMACTamperDetectionTests(unittest.TestCase):
    """A5 — HMAC must detect a tamper at every byte position in
    the ARQO body. The spec promises HMAC-SHA256 over
    ``IV + ciphertext`` (bytes 36..end); flipping any single bit
    in that span must produce a mismatch on verify."""

    def test_hmac_detects_flip_at_every_sampled_body_position(
        self,
    ) -> None:
        from arq_validator.crypto import verify_encrypted_object_hmac
        with tempfile.TemporaryDirectory() as td:
            _, ks, _, arqo = _build_tiny_backup(Path(td))
            # Sample positions across the body (the 36..end span);
            # exhaustive coverage would be O(N*K) for an N-byte
            # blob with K random bits per byte — sample-based
            # coverage is sufficient for regression purposes.
            rng = random.Random(20260511)
            body_start = 36
            body_end = len(arqo)
            sample_positions = sorted(
                rng.sample(
                    range(body_start, body_end),
                    k=min(32, body_end - body_start),
                )
            )
            for idx in sample_positions:
                for bit_mask in (0x01, 0x80, 0xFF):
                    mangled = bytearray(arqo)
                    mangled[idx] ^= bit_mask
                    ok, _expected_hex, _actual_hex = (
                        verify_encrypted_object_hmac(
                            bytes(mangled), ks.hmac_key,
                        )
                    )
                    self.assertFalse(
                        ok,
                        f"HMAC did NOT detect flip at body byte "
                        f"{idx} with mask {bit_mask:#x} — silent "
                        f"tamper acceptance",
                    )

    def test_hmac_passes_on_unmodified_arqo(self) -> None:
        """Baseline: the unmodified ARQO must verify cleanly.
        Catches a regression where the verifier itself produces
        false positives."""
        from arq_validator.crypto import verify_encrypted_object_hmac
        with tempfile.TemporaryDirectory() as td:
            _, ks, _, arqo = _build_tiny_backup(Path(td))
            ok, expected, actual = verify_encrypted_object_hmac(
                arqo, ks.hmac_key,
            )
            self.assertTrue(
                ok,
                f"unmodified ARQO failed HMAC verify "
                f"(expected={expected[:8]}..., "
                f"actual={actual[:8]}...)",
            )

    def test_hmac_detects_truncated_body(self) -> None:
        """Truncate the ARQO and confirm HMAC verify fails (the
        HMAC's body input has the wrong length so it won't
        match)."""
        from arq_validator.crypto import verify_encrypted_object_hmac
        with tempfile.TemporaryDirectory() as td:
            _, ks, _, arqo = _build_tiny_backup(Path(td))
            for cut in (1, 16, 64, 256):
                truncated = arqo[: len(arqo) - cut]
                if len(truncated) < 36:
                    continue
                ok, _, _ = verify_encrypted_object_hmac(
                    truncated, ks.hmac_key,
                )
                self.assertFalse(
                    ok,
                    f"truncated ARQO ({cut} bytes off) passed HMAC",
                )

    def test_hmac_detects_appended_garbage(self) -> None:
        """Append garbage bytes to the ARQO. The HMAC field is at
        bytes 4..36, computed over a fixed-extent body — extra
        bytes at the tail should fail verify."""
        from arq_validator.crypto import verify_encrypted_object_hmac
        with tempfile.TemporaryDirectory() as td:
            _, ks, _, arqo = _build_tiny_backup(Path(td))
            for extra in (1, 16, 64, 4096):
                appended = arqo + b"X" * extra
                ok, _, _ = verify_encrypted_object_hmac(
                    appended, ks.hmac_key,
                )
                self.assertFalse(
                    ok,
                    f"ARQO + {extra} garbage bytes passed HMAC",
                )

    def test_hmac_field_tamper_directly_detected(self) -> None:
        """Flip a bit in the HMAC field itself (bytes 4..36). The
        verifier should detect 'wrong HMAC' even though body
        bytes are intact — pins the canonical 'someone replaced
        the auth tag' case."""
        from arq_validator.crypto import verify_encrypted_object_hmac
        with tempfile.TemporaryDirectory() as td:
            _, ks, _, arqo = _build_tiny_backup(Path(td))
            for hmac_idx in (4, 12, 20, 28, 35):
                mangled = bytearray(arqo)
                mangled[hmac_idx] ^= 0xAA
                ok, _, _ = verify_encrypted_object_hmac(
                    bytes(mangled), ks.hmac_key,
                )
                self.assertFalse(
                    ok,
                    f"HMAC field tamper at byte {hmac_idx} "
                    f"undetected",
                )


if __name__ == "__main__":
    unittest.main()
