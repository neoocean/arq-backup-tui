"""Arq 5 / Arq 6 ``encryptionvN.dat`` master-key file decryption.

Format reverse-engineered from ``arq_restore/repo/EncryptionDatFile.m``
``loadPrivateKeyFromData:`` (the canonical reader).

On-disk layout (constants verified against the source):

    [12 bytes]   ASCII header: "ENCRYPTIONV2" or "ENCRYPTIONV3"
    [ 8 bytes]   PBKDF2 salt
    [32 bytes]   HMAC-SHA256 of (IV + ciphertext) under derived MAC key
    [16 bytes]   AES-256-CBC IV
    [N  bytes]   ciphertext (PKCS7-padded master keys)

KDF differs from Arq 7: PBKDF2 uses **SHA-1** (not SHA-256) at
200,000 iterations, output 64 bytes. First 32 bytes = AES key, last
32 bytes = HMAC-SHA256 key — same split convention as Arq 7.

Master keys plaintext:

- v2 (64 bytes): aes_key (32) + hmac_key (32). The blob_id_salt is
  **not stored in the file** — instead, the per-computer UUID's
  UTF-8 bytes are used as the salt for blob ID hashing.
- v3 (96 bytes): aes_key (32) + hmac_key (32) + blob_id_salt (32).

Both produce the same downstream interface (the Arq 5 EncryptedObject
format is bit-for-bit identical to Arq 7's ARQO, so
``arq_reader.decrypt.decrypt_encrypted_object`` works unchanged on
Arq 5 blobs once the master keys are in hand).
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
from dataclasses import dataclass
from typing import Optional, Tuple

from arq_validator.crypto import CryptoError, aes_256_cbc_decrypt

KEYSET_HEADER_V2 = b"ENCRYPTIONV2"
KEYSET_HEADER_V3 = b"ENCRYPTIONV3"
KEYSET_HEADER_LEN = 12
KEYSET_SALT_LEN = 8
KEYSET_HMAC_LEN = 32
KEYSET_IV_LEN = 16
KEYSET_PBKDF2_ROUNDS = 200_000
KEYSET_PBKDF2_DKLEN = 64

V2_MASTER_KEYS_LEN = 64    # 32 enc + 32 hmac
V3_MASTER_KEYS_LEN = 96    # 32 enc + 32 hmac + 32 blob_id_salt

KEYSET_MIN_LEN = (
    KEYSET_HEADER_LEN + KEYSET_SALT_LEN + KEYSET_HMAC_LEN + KEYSET_IV_LEN
)


@dataclass(frozen=True)
class Arq5Keyset:
    """Decrypted Arq 5/6 master keys."""

    encryption_version: int     # 2 or 3
    encryption_key: bytes       # 32 bytes
    hmac_key: bytes             # 32 bytes
    blob_id_salt: bytes         # 32 bytes (v3) or computer-UUID bytes (v2)


def _detect_version(data: bytes) -> int:
    if data[:KEYSET_HEADER_LEN] == KEYSET_HEADER_V3:
        return 3
    if data[:KEYSET_HEADER_LEN] == KEYSET_HEADER_V2:
        return 2
    raise CryptoError(
        f"unsupported encryption file header: "
        f"{data[:KEYSET_HEADER_LEN]!r}"
    )


def decrypt_arq5_keyset(
    data: bytes, password: str, computer_uuid: str,
    *, openssl_path: str = "openssl",
) -> Arq5Keyset:
    """Decrypt an Arq 5/6 ``encryptionv2.dat`` or ``encryptionv3.dat``
    blob and return the master keys.

    ``computer_uuid`` is the ASCII UUID for the computer subtree; it
    only matters for v2 (where the blob ID salt is the UUID bytes).

    Raises :class:`arq_validator.crypto.CryptoError` on header,
    HMAC, or AES failures — same exception class as Arq 7's keyset
    pipeline so callers can handle both uniformly.
    """
    if len(data) < KEYSET_MIN_LEN + 1:
        raise CryptoError(
            f"encryption file too short: {len(data)} < {KEYSET_MIN_LEN + 1}"
        )
    version = _detect_version(data)

    o = KEYSET_HEADER_LEN
    salt = data[o : o + KEYSET_SALT_LEN]; o += KEYSET_SALT_LEN
    stored_hmac = data[o : o + KEYSET_HMAC_LEN]; o += KEYSET_HMAC_LEN
    iv = data[o : o + KEYSET_IV_LEN]; o += KEYSET_IV_LEN
    ciphertext = data[o:]
    if not ciphertext or len(ciphertext) % 16 != 0:
        raise CryptoError(
            f"ciphertext length not AES-block-aligned: {len(ciphertext)}"
        )

    # PBKDF2-HMAC-SHA1 (NOT SHA-256 — different from Arq 7).
    derived = hashlib.pbkdf2_hmac(
        "sha1", password.encode("utf-8"), salt,
        KEYSET_PBKDF2_ROUNDS, dklen=KEYSET_PBKDF2_DKLEN,
    )
    aes_key = derived[:32]
    mac_key = derived[32:]

    actual_hmac = _hmac.new(
        mac_key, iv + ciphertext, hashlib.sha256,
    ).digest()
    if not _hmac.compare_digest(actual_hmac, stored_hmac):
        raise CryptoError(
            "Arq 5 keyset HMAC mismatch — wrong password OR file corruption"
        )

    plaintext = aes_256_cbc_decrypt(
        aes_key, iv, ciphertext, openssl_path=openssl_path,
    )

    if version == 3:
        if len(plaintext) != V3_MASTER_KEYS_LEN:
            raise CryptoError(
                f"v3 master keys plaintext length {len(plaintext)}, "
                f"expected {V3_MASTER_KEYS_LEN}"
            )
        enc_key = plaintext[0:32]
        hk = plaintext[32:64]
        blob_id_salt = plaintext[64:96]
    else:
        if len(plaintext) != V2_MASTER_KEYS_LEN:
            raise CryptoError(
                f"v2 master keys plaintext length {len(plaintext)}, "
                f"expected {V2_MASTER_KEYS_LEN}"
            )
        enc_key = plaintext[0:32]
        hk = plaintext[32:64]
        # v2 salt is the computer UUID UTF-8 bytes, padded/truncated
        # to 32 bytes for parity with the v3 / Arq 7 interface.
        salt_bytes = computer_uuid.encode("utf-8")
        blob_id_salt = (salt_bytes + b"\x00" * 32)[:32]

    return Arq5Keyset(
        encryption_version=version,
        encryption_key=enc_key,
        hmac_key=hk,
        blob_id_salt=blob_id_salt,
    )


def build_arq5_keyset_blob(
    password: str,
    encryption_key: bytes, hmac_key: bytes,
    blob_id_salt_for_v3: Optional[bytes] = None,
    *,
    salt: Optional[bytes] = None,
    iv: Optional[bytes] = None,
    openssl_path: str = "openssl",
) -> bytes:
    """Build a fresh ``encryptionv2.dat`` (no salt arg) or
    ``encryptionv3.dat`` (salt arg present) blob.

    Mainly useful for tests — we need a way to produce spec-conformant
    Arq 5/6 keyset files without an actual Arq install.
    """
    import os

    salt = salt if salt is not None else os.urandom(KEYSET_SALT_LEN)
    iv = iv if iv is not None else os.urandom(KEYSET_IV_LEN)
    if len(salt) != KEYSET_SALT_LEN:
        raise ValueError(f"salt must be {KEYSET_SALT_LEN} bytes")
    if len(iv) != KEYSET_IV_LEN:
        raise ValueError(f"iv must be {KEYSET_IV_LEN} bytes")
    if len(encryption_key) != 32 or len(hmac_key) != 32:
        raise ValueError("encryption / hmac keys must be 32 bytes each")

    if blob_id_salt_for_v3 is None:
        version = 2
        plaintext = encryption_key + hmac_key
        header = KEYSET_HEADER_V2
    else:
        version = 3
        if len(blob_id_salt_for_v3) != 32:
            raise ValueError("blob_id_salt must be 32 bytes for v3")
        plaintext = encryption_key + hmac_key + blob_id_salt_for_v3
        header = KEYSET_HEADER_V3

    # PKCS7-pad to 16-byte boundary.
    pad_len = 16 - (len(plaintext) % 16) if len(plaintext) % 16 else 16
    padded = plaintext + bytes([pad_len] * pad_len)

    derived = hashlib.pbkdf2_hmac(
        "sha1", password.encode("utf-8"), salt,
        KEYSET_PBKDF2_ROUNDS, dklen=KEYSET_PBKDF2_DKLEN,
    )
    aes_key = derived[:32]
    mac_key = derived[32:]

    # AES-256-CBC encrypt via openssl CLI (no padding — we PKCS7-padded
    # ourselves above; openssl with -nopad accepts that). The
    # validator's helper only does decrypt, so we shell out to openssl
    # directly here.
    import subprocess
    cp = subprocess.run(
        [openssl_path, "enc", "-aes-256-cbc", "-nopad",
         "-K", aes_key.hex(), "-iv", iv.hex()],
        input=padded, capture_output=True, timeout=10,
    )
    if cp.returncode != 0:
        raise CryptoError(
            f"openssl encrypt failed: {cp.stderr.decode(errors='replace')[:200]}"
        )
    ciphertext = cp.stdout

    mac = _hmac.new(mac_key, iv + ciphertext, hashlib.sha256).digest()
    return header + salt + mac + iv + ciphertext


def arq5_compute_blob_sha1(plaintext: bytes, blob_id_salt: bytes) -> str:
    """Arq 5/6 blob ID = SHA-1(salt + plaintext), hex.

    Note this is the **plaintext** SHA-1 (computed BEFORE LZ4 wrapping
    or ARQO encryption) — the same convention Arq 7's blob_id uses,
    just with SHA-1 instead of SHA-256.
    """
    h = hashlib.sha1()
    h.update(blob_id_salt)
    h.update(plaintext)
    return h.hexdigest()


def arq5_object_paths(computer_uuid: str, sha1: str) -> Tuple[str, ...]:
    """Return the candidate paths to try, in order, for a given SHA-1.

    Arq 5/6 sharded blob storage uses several historical layouts
    depending on the destination type. This list mirrors
    ``Fark.m::pathsToTryForSHA1:`` so we can find blobs written by
    any past Arq version.
    """
    return (
        f"/{computer_uuid}/objects/{sha1[:2]}/{sha1[2:]}",
        f"/{computer_uuid}/objects2/{sha1[:2]}/{sha1[2:]}",
        f"/{computer_uuid}/objects/{sha1}",
        f"/{computer_uuid}/objects/{sha1[:2]}/{sha1[2:4]}/{sha1[4:]}",
        f"/{computer_uuid}/objects2/{sha1[:2]}/{sha1[2:4]}/{sha1[4:]}",
    )
