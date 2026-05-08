"""Crypto primitives for Arq 7 keyset decryption and HMAC verification.

Hard rule: no third-party Python crypto packages. HMAC-SHA256 and
PBKDF2-SHA256 come from stdlib (``hashlib`` + ``hmac``); AES-256-CBC
runs through the ``openssl`` CLI via subprocess. The only AES use is
to decrypt ``encryptedkeyset.dat`` once per validation run (a few
hundred bytes), so the subprocess cost is negligible.

Shape of the keyset / EncryptedObject layouts is documented in
``arq_validator.constants`` along with empirical corrections from the
docker-monitor reverse-engineering pass.
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
import struct
import subprocess
from dataclasses import dataclass
from typing import Tuple

from . import constants as C


class CryptoError(RuntimeError):
    """Wrong password, corrupted keyset, missing openssl, etc."""


@dataclass
class Keyset:
    """Decrypted master keys held in process memory.

    ``encryption_key`` is the AES-256 key for nested object decrypts;
    ``hmac_key`` is the HMAC-SHA256 key used to authenticate every
    EncryptedObject. ``blob_id_salt`` is unused by this validator
    (Arq uses it for content-addressing) but parsed for completeness.
    """

    encryption_key: bytes
    hmac_key: bytes
    blob_id_salt: bytes


# ---------------------------------------------------------------------------
# Keyset parsing pipeline
# ---------------------------------------------------------------------------


def parse_keyset_storage(
    data: bytes,
) -> Tuple[bytes, bytes, bytes, bytes]:
    """Split encryptedkeyset.dat into (salt, hmac_stored, iv, ciphertext)."""
    if len(data) < C.KEYSET_HEADER_BYTES:
        raise CryptoError(
            f"keyset too short: {len(data)} < {C.KEYSET_HEADER_BYTES}"
        )
    magic = data[: C.KEYSET_MAGIC_BYTES]
    if magic != C.KEYSET_MAGIC:
        raise CryptoError(f"bad keyset magic: {magic!r}")
    o = C.KEYSET_MAGIC_BYTES
    salt = data[o : o + C.KEYSET_SALT_BYTES]
    o += C.KEYSET_SALT_BYTES
    hmac_stored = data[o : o + C.KEYSET_HMAC_BYTES]
    o += C.KEYSET_HMAC_BYTES
    iv = data[o : o + C.KEYSET_IV_BYTES]
    o += C.KEYSET_IV_BYTES
    ciphertext = data[o:]
    if not ciphertext or len(ciphertext) % 16 != 0:
        raise CryptoError(
            f"keyset ciphertext not AES-block-aligned: {len(ciphertext)}"
        )
    return salt, hmac_stored, iv, ciphertext


def derive_keyset_keys(
    password: str, salt: bytes,
) -> Tuple[bytes, bytes]:
    """PBKDF2-SHA256(password, salt) -> (aes_key[32], hmac_key[32]).

    Spec quote (Arq 5/7 share this convention):
      "Encrypt the master keys with AES256-CBC using the FIRST 32 bytes
       of the derived key … HMAC-SHA256 of (IV + encrypted master keys)
       using the SECOND 32 bytes of the derived key."
    """
    derived = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        C.KEYSET_PBKDF2_ITERATIONS,
        dklen=C.KEYSET_PBKDF2_DKLEN,
    )
    return derived[:32], derived[32:]


def verify_keyset_hmac(
    hmac_key: bytes, iv: bytes, ciphertext: bytes, expected: bytes,
) -> bool:
    """HMAC-SHA256(hmac_key, IV + ciphertext) == expected?

    A mismatch is indistinguishable between "wrong password" and
    "keyset file corruption" without an oracle — caller surfaces both
    as the same failure mode.
    """
    actual = _hmac.new(hmac_key, iv + ciphertext, hashlib.sha256).digest()
    return _hmac.compare_digest(actual, expected)


def parse_keyset_plaintext(data: bytes) -> Keyset:
    """Parse the AES-decrypted keyset plaintext into a ``Keyset``.

    Trailing PKCS7 padding bytes are tolerated — fields are read at
    fixed offsets. Version + length sanity-checks raise ``CryptoError``
    so callers can tell "wrong password" (HMAC fail above) from
    "format drift" (bad version / length).
    """
    needed = (
        4 + 8 + C.KEYSET_PLAIN_FIELD_LEN
        + 8 + C.KEYSET_PLAIN_FIELD_LEN
        + 8 + C.KEYSET_PLAIN_FIELD_LEN
    )                                          # 124
    if len(data) < needed:
        raise CryptoError(
            f"keyset plaintext too short: {len(data)} < {needed}"
        )
    version = struct.unpack(">I", data[0:4])[0]
    if version != C.KEYSET_PLAIN_VERSION:
        raise CryptoError(
            f"unexpected keyset plaintext version: 0x{version:08x}"
        )

    o = 4
    enc_len = struct.unpack(">Q", data[o : o + 8])[0]
    if enc_len != C.KEYSET_PLAIN_FIELD_LEN:
        raise CryptoError(f"unexpected enc-key length: {enc_len}")
    o += 8
    encryption_key = data[o : o + C.KEYSET_PLAIN_FIELD_LEN]
    o += C.KEYSET_PLAIN_FIELD_LEN

    hmac_len = struct.unpack(">Q", data[o : o + 8])[0]
    if hmac_len != C.KEYSET_PLAIN_FIELD_LEN:
        raise CryptoError(f"unexpected hmac-key length: {hmac_len}")
    o += 8
    hmac_key = data[o : o + C.KEYSET_PLAIN_FIELD_LEN]
    o += C.KEYSET_PLAIN_FIELD_LEN

    salt_len = struct.unpack(">Q", data[o : o + 8])[0]
    if salt_len != C.KEYSET_PLAIN_FIELD_LEN:
        raise CryptoError(f"unexpected blob-id-salt length: {salt_len}")
    o += 8
    blob_id_salt = data[o : o + C.KEYSET_PLAIN_FIELD_LEN]

    return Keyset(
        encryption_key=encryption_key,
        hmac_key=hmac_key,
        blob_id_salt=blob_id_salt,
    )


def aes_256_cbc_decrypt(
    key: bytes, iv: bytes, ciphertext: bytes,
    *, openssl_path: str = "openssl",
) -> bytes:
    """Decrypt ``ciphertext`` via the openssl CLI.

    PKCS7 padding (Arq's choice) is the openssl default. Raises
    ``CryptoError`` if openssl is missing or rejects the ciphertext.
    """
    if len(key) != 32:
        raise CryptoError(f"AES-256 needs 32-byte key, got {len(key)}")
    if len(iv) != 16:
        raise CryptoError(f"AES-CBC IV must be 16 bytes, got {len(iv)}")
    cmd = [
        openssl_path, "enc", "-aes-256-cbc", "-d",
        "-K", key.hex(),
        "-iv", iv.hex(),
    ]
    try:
        r = subprocess.run(
            cmd, input=ciphertext, capture_output=True, timeout=10,
        )
    except FileNotFoundError as exc:
        raise CryptoError(
            f"openssl CLI not found at '{openssl_path}'"
        ) from exc
    if r.returncode != 0:
        err = r.stderr.decode(errors="replace").strip()[:200]
        raise CryptoError(f"openssl decrypt failed: rc={r.returncode} {err}")
    return r.stdout


def decrypt_keyset(
    storage_data: bytes, password: str,
    *, openssl_path: str = "openssl",
) -> Keyset:
    """Full ``encryptedkeyset.dat`` -> ``Keyset`` pipeline.

    Steps:
      1. Split storage layout (salt + HMAC + IV + ciphertext)
      2. Derive (aes_key, hmac_key) via PBKDF2-SHA256
      3. Verify HMAC-SHA256(hmac_key, IV + ciphertext)
      4. Decrypt ciphertext with aes_key + IV (openssl CLI)
      5. Parse plaintext into ``Keyset``

    Any step's failure raises ``CryptoError`` with a diagnostic prefix.
    """
    salt, hmac_stored, iv, ciphertext = parse_keyset_storage(storage_data)
    aes_key, hmac_key = derive_keyset_keys(password, salt)
    if not verify_keyset_hmac(hmac_key, iv, ciphertext, hmac_stored):
        raise CryptoError(
            "keyset HMAC mismatch — wrong password OR keyset corruption"
        )
    plaintext = aes_256_cbc_decrypt(
        aes_key, iv, ciphertext, openssl_path=openssl_path,
    )
    return parse_keyset_plaintext(plaintext)


# ---------------------------------------------------------------------------
# EncryptedObject HMAC verification
# ---------------------------------------------------------------------------


def verify_encrypted_object_hmac(
    data: bytes, hmac_key: bytes,
) -> Tuple[bool, str, str]:
    """Verify a single EncryptedObject's HMAC field.

    Per Arq 5/7 spec:
        HMAC-SHA256(hmac_key, master_IV + encrypted_session + ciphertext)
            == bytes[4..36]

    Returns ``(ok, expected_hex, actual_hex)``. Truncated input or
    bad magic returns ``(False, "", "")``.
    """
    if len(data) < C.ARQO_HEADER_BYTES:
        return False, "", ""
    if data[: len(C.ARQO_MAGIC)] != C.ARQO_MAGIC:
        return False, "", ""
    expected = data[
        C.ARQO_HMAC_OFFSET : C.ARQO_HMAC_OFFSET + C.ARQO_HMAC_BYTES
    ]
    body = data[C.ARQO_HMAC_BODY_OFFSET :]
    actual = _hmac.new(hmac_key, body, hashlib.sha256).digest()
    return (
        _hmac.compare_digest(actual, expected),
        expected.hex(),
        actual.hex(),
    )


def find_arqo_positions(body: bytes) -> list:
    """Return offsets where ``b"ARQO"`` appears in ``body``.

    False-positive matches in random ciphertext are statistically rare
    (~1 / 2^32 per 4-byte window) and surface as HMAC mismatches.
    """
    positions: list = []
    o = 0
    while True:
        idx = body.find(C.ARQO_MAGIC, o)
        if idx < 0:
            break
        positions.append(idx)
        o = idx + 1
    return positions


def verify_multi_object_arqos(
    body: bytes, hmac_key: bytes,
) -> Tuple[int, int, list]:
    """HMAC-verify every inner ARQO in a multi-object pack body.

    Each inner object ``i`` covers ``[positions[i], positions[i+1])``,
    or the end of the buffer for the last one. HMAC is bytes
    ``[Pi+4 : Pi+36]``, computed over ``body[Pi+36 : end_i]``.

    Returns ``(n_ok, n_fail, fail_offsets)``. Single-object files
    reduce to ``verify_encrypted_object_hmac``.
    """
    positions = find_arqo_positions(body)
    if not positions:
        return 0, 0, []
    n_ok = 0
    n_fail = 0
    fail_offsets: list = []
    for i, pos in enumerate(positions):
        end = positions[i + 1] if i + 1 < len(positions) else len(body)
        if pos + C.ARQO_HMAC_BODY_OFFSET > end:
            fail_offsets.append(pos)
            n_fail += 1
            continue
        stored = body[
            pos + C.ARQO_HMAC_OFFSET : pos + C.ARQO_HMAC_OFFSET + C.ARQO_HMAC_BYTES
        ]
        inner_body = body[pos + C.ARQO_HMAC_BODY_OFFSET : end]
        calc = _hmac.new(hmac_key, inner_body, hashlib.sha256).digest()
        if _hmac.compare_digest(calc, stored):
            n_ok += 1
        else:
            n_fail += 1
            fail_offsets.append(pos)
    return n_ok, n_fail, fail_offsets
