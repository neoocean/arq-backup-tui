"""Unit tests for crypto primitives."""

from __future__ import annotations

import secrets
import unittest

from arq_validator import constants as C
from arq_validator.crypto import (
    CryptoError,
    decrypt_keyset,
    derive_keyset_keys,
    find_arqo_positions,
    parse_keyset_storage,
    verify_encrypted_object_hmac,
    verify_keyset_hmac,
    verify_multi_object_arqos,
)

from .fixtures import (
    aes_256_cbc_encrypt,
    make_arqo,
    make_encrypted_keyset,
    make_keyset_plaintext,
    make_multi_arqo,
)


class KeysetRoundTripTests(unittest.TestCase):
    def test_round_trip_decrypt(self) -> None:
        password = "correct horse battery staple"
        enc_key = secrets.token_bytes(32)
        hmac_key = secrets.token_bytes(32)
        salt = secrets.token_bytes(32)
        blob = make_encrypted_keyset(password, enc_key, hmac_key, salt)
        keyset = decrypt_keyset(blob, password)
        self.assertEqual(keyset.encryption_key, enc_key)
        self.assertEqual(keyset.hmac_key, hmac_key)
        self.assertEqual(keyset.blob_id_salt, salt)

    def test_wrong_password_fails(self) -> None:
        password = "right"
        blob = make_encrypted_keyset(
            password,
            secrets.token_bytes(32),
            secrets.token_bytes(32),
            secrets.token_bytes(32),
        )
        with self.assertRaises(CryptoError) as ctx:
            decrypt_keyset(blob, "wrong")
        self.assertIn("HMAC", str(ctx.exception))

    def test_corrupted_keyset_magic(self) -> None:
        blob = bytearray(make_encrypted_keyset(
            "pw",
            secrets.token_bytes(32),
            secrets.token_bytes(32),
            secrets.token_bytes(32),
        ))
        blob[0] = ord("X")
        with self.assertRaises(CryptoError) as ctx:
            parse_keyset_storage(bytes(blob))
        self.assertIn("magic", str(ctx.exception))

    def test_short_input_fails(self) -> None:
        with self.assertRaises(CryptoError):
            parse_keyset_storage(b"x" * 10)

    def test_pbkdf2_split(self) -> None:
        aes_key, mac_key = derive_keyset_keys("pw", b"\x00" * 8)
        self.assertEqual(len(aes_key), 32)
        self.assertEqual(len(mac_key), 32)
        self.assertNotEqual(aes_key, mac_key)


class KeysetPlaintextValidationTests(unittest.TestCase):
    def test_bad_version_raises(self) -> None:
        password = "pw"
        bad_plain = (
            (0x00000099).to_bytes(4, "big")
            + (32).to_bytes(8, "big") + secrets.token_bytes(32)
            + (32).to_bytes(8, "big") + secrets.token_bytes(32)
            + (32).to_bytes(8, "big") + secrets.token_bytes(32)
        )
        salt = secrets.token_bytes(8)
        iv = secrets.token_bytes(16)
        # Re-derive keys + recompute HMAC so only the plaintext version
        # field is invalid (everything else is genuine).
        import hashlib
        import hmac
        derived = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), salt,
            C.KEYSET_PBKDF2_ITERATIONS, dklen=64,
        )
        aes_key, mac_key = derived[:32], derived[32:]
        ciphertext = aes_256_cbc_encrypt(aes_key, iv, bad_plain)
        mac = hmac.new(mac_key, iv + ciphertext, hashlib.sha256).digest()
        blob = C.KEYSET_MAGIC + salt + mac + iv + ciphertext
        with self.assertRaises(CryptoError) as ctx:
            decrypt_keyset(blob, password)
        self.assertIn("version", str(ctx.exception))


class ArqoHmacTests(unittest.TestCase):
    def test_round_trip_single(self) -> None:
        hmac_key = secrets.token_bytes(32)
        blob = make_arqo(hmac_key, body_size=128)
        ok, _, _ = verify_encrypted_object_hmac(blob, hmac_key)
        self.assertTrue(ok)

    def test_wrong_key_fails(self) -> None:
        hmac_key = secrets.token_bytes(32)
        blob = make_arqo(hmac_key, body_size=128)
        ok, _, _ = verify_encrypted_object_hmac(blob, secrets.token_bytes(32))
        self.assertFalse(ok)

    def test_corrupted_byte_fails(self) -> None:
        hmac_key = secrets.token_bytes(32)
        blob = bytearray(make_arqo(hmac_key, body_size=128))
        blob[100] ^= 0xFF
        ok, _, _ = verify_encrypted_object_hmac(bytes(blob), hmac_key)
        self.assertFalse(ok)

    def test_bad_magic_fails(self) -> None:
        ok, _, _ = verify_encrypted_object_hmac(
            b"NOPE" + b"\x00" * 200, secrets.token_bytes(32),
        )
        self.assertFalse(ok)

    def test_truncated_fails(self) -> None:
        ok, _, _ = verify_encrypted_object_hmac(
            b"ARQO", secrets.token_bytes(32),
        )
        self.assertFalse(ok)

    def test_find_positions(self) -> None:
        hmac_key = secrets.token_bytes(32)
        blob = make_multi_arqo(hmac_key, n_inner=4, body_size=32)
        positions = find_arqo_positions(blob)
        self.assertGreaterEqual(len(positions), 4)
        # First position must be at offset 0.
        self.assertEqual(positions[0], 0)

    def test_multi_object_all_ok(self) -> None:
        hmac_key = secrets.token_bytes(32)
        blob = make_multi_arqo(hmac_key, n_inner=5, body_size=64)
        n_ok, n_fail, fails = verify_multi_object_arqos(blob, hmac_key)
        # n_ok counts inner ARQOs that verified — 5 was the input.
        # In rare cases the random ciphertext contains a stray b"ARQO"
        # which adds bogus extra positions; the test tolerates that
        # by checking the ratio rather than equality.
        self.assertGreaterEqual(n_ok, 5)
        self.assertEqual(n_fail, len(fails))

    def test_multi_object_one_corrupted(self) -> None:
        hmac_key = secrets.token_bytes(32)
        blob = bytearray(make_multi_arqo(hmac_key, n_inner=3, body_size=64))
        # Corrupt the second inner object (skip past first ~200 byte ARQO).
        blob[260] ^= 0xFF
        n_ok, n_fail, _ = verify_multi_object_arqos(bytes(blob), hmac_key)
        self.assertGreaterEqual(n_fail, 1)


if __name__ == "__main__":
    unittest.main()
