"""Synthetic Arq 7 fixtures for tests.

Real Arq backups can't be redistributed (they contain operator data
and operator-specific encrypted master keys), so the test suite
synthesizes fixtures from scratch:

- An ``encryptedkeyset.dat`` whose layout matches Arq's exactly,
  built from a known password + random keys, so we can round-trip
  decrypt it through the same code paths the validator uses against
  real backups.
- ARQO-format pseudo-objects keyed with the synthetic ``hmac_key``,
  matching the byte layout (magic, HMAC, master IV, encrypted
  session, ciphertext) Arq's pack files use.

The fixtures are deliberately minimal: just enough to drive every
validation tier through its happy path and a couple of failure paths.
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
import os
import secrets
import struct
import subprocess
from pathlib import Path
from typing import Tuple

from arq_validator import constants as C


def aes_256_cbc_encrypt(key: bytes, iv: bytes, plaintext: bytes) -> bytes:
    """Encrypt with the openssl CLI (mirrors the validator's decrypt path)."""
    r = subprocess.run(
        ["openssl", "enc", "-aes-256-cbc",
         "-K", key.hex(), "-iv", iv.hex()],
        input=plaintext, capture_output=True, timeout=10, check=True,
    )
    return r.stdout


def make_keyset_plaintext(
    encryption_key: bytes, hmac_key: bytes, blob_id_salt: bytes,
) -> bytes:
    """Build the 124-byte keyset plaintext layout."""
    assert len(encryption_key) == 32
    assert len(hmac_key) == 32
    assert len(blob_id_salt) == 32
    return (
        struct.pack(">I", C.KEYSET_PLAIN_VERSION)
        + struct.pack(">Q", 32) + encryption_key
        + struct.pack(">Q", 32) + hmac_key
        + struct.pack(">Q", 32) + blob_id_salt
    )


def make_encrypted_keyset(
    password: str,
    encryption_key: bytes,
    hmac_key: bytes,
    blob_id_salt: bytes,
) -> bytes:
    """Build a complete ``encryptedkeyset.dat`` blob."""
    salt = secrets.token_bytes(C.KEYSET_SALT_BYTES)
    iv = secrets.token_bytes(C.KEYSET_IV_BYTES)
    derived = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt,
        C.KEYSET_PBKDF2_ITERATIONS, dklen=C.KEYSET_PBKDF2_DKLEN,
    )
    aes_key, mac_key = derived[:32], derived[32:]
    plaintext = make_keyset_plaintext(encryption_key, hmac_key, blob_id_salt)
    ciphertext = aes_256_cbc_encrypt(aes_key, iv, plaintext)
    mac = _hmac.new(mac_key, iv + ciphertext, hashlib.sha256).digest()
    return C.KEYSET_MAGIC + salt + mac + iv + ciphertext


def make_arqo(
    hmac_key: bytes, body_size: int = 256, *, body: bytes | None = None,
) -> bytes:
    """Build a minimal valid ARQO whose HMAC verifies under ``hmac_key``."""
    master_iv = secrets.token_bytes(C.ARQO_MASTER_IV_BYTES)
    enc_session = secrets.token_bytes(C.ARQO_ENC_SESSION_BYTES)
    ciphertext = body if body is not None else secrets.token_bytes(body_size)
    body_bytes = master_iv + enc_session + ciphertext
    mac = _hmac.new(hmac_key, body_bytes, hashlib.sha256).digest()
    return C.ARQO_MAGIC + mac + body_bytes


def make_multi_arqo(
    hmac_key: bytes, n_inner: int = 3, body_size: int = 64,
) -> bytes:
    """Concatenate ``n_inner`` valid ARQOs into one multi-object blob."""
    return b"".join(
        make_arqo(hmac_key, body_size=body_size) for _ in range(n_inner)
    )


def write_synthetic_backup(
    root: Path,
    password: str,
    *,
    computer_uuid: str = "12345678-ABCD-1234-ABCD-1234567890AB",
    folder_uuid: str = "87654321-DCBA-4321-DCBA-BA0987654321",
    n_blobpacks: int = 2,
    n_treepacks: int = 1,
    n_largeblobpacks: int = 0,
    n_standardobjects: int = 4,
    backuprecord_num: int = 42,
    corrupt_first_blobpack: bool = False,
) -> Tuple[bytes, bytes, str]:
    """Materialize a tiny but realistically-shaped Arq 7 destination.

    Returns the synthesized ``(encryption_key, hmac_key, computer_uuid)``
    so tests can sanity-check round-trips against the same key material.
    The optional ``corrupt_first_blobpack`` flag flips a byte inside the
    first blobpack's HMAC so audit-mode failure paths can be exercised.
    """
    root.mkdir(parents=True, exist_ok=True)
    enc_key = secrets.token_bytes(32)
    hmac_key = secrets.token_bytes(32)
    blob_id_salt = secrets.token_bytes(32)

    cu_root = root / computer_uuid
    (cu_root / C.BLOBPACKS_DIR).mkdir(parents=True, exist_ok=True)
    (cu_root / C.TREEPACKS_DIR).mkdir(parents=True, exist_ok=True)
    (cu_root / C.LARGEBLOBPACKS_DIR).mkdir(parents=True, exist_ok=True)
    (cu_root / C.STANDARDOBJECTS_DIR).mkdir(parents=True, exist_ok=True)

    keyset_blob = make_encrypted_keyset(
        password, enc_key, hmac_key, blob_id_salt,
    )
    (cu_root / C.KEYSET_FILE).write_bytes(keyset_blob)

    def _write_pack(family: str, count: int) -> None:
        for i in range(count):
            shard = f"{i:02x}"
            (cu_root / family / shard).mkdir(parents=True, exist_ok=True)
            name = (f"{i:06X}-{i:04X}-{i:04X}-{i:04X}-"
                    f"{i:012X}.pack").upper()
            data = make_arqo(hmac_key, body_size=128)
            (cu_root / family / shard / name).write_bytes(data)

    _write_pack(C.BLOBPACKS_DIR, n_blobpacks)
    _write_pack(C.TREEPACKS_DIR, n_treepacks)
    _write_pack(C.LARGEBLOBPACKS_DIR, n_largeblobpacks)

    for i in range(n_standardobjects):
        shard = f"{i:02x}"
        (cu_root / C.STANDARDOBJECTS_DIR / shard).mkdir(
            parents=True, exist_ok=True,
        )
        name = ("a" * 62)[:62 - len(str(i))] + str(i)
        (cu_root / C.STANDARDOBJECTS_DIR / shard / name).write_bytes(
            make_arqo(hmac_key, body_size=64),
        )

    bf = cu_root / C.BACKUPFOLDERS_DIR / folder_uuid / C.BACKUPRECORDS_DIR
    outer = bf / "00001"
    outer.mkdir(parents=True, exist_ok=True)
    (outer / f"{backuprecord_num}.backuprecord").write_bytes(
        make_arqo(hmac_key, body_size=512),
    )

    if corrupt_first_blobpack and n_blobpacks > 0:
        first_dir = cu_root / C.BLOBPACKS_DIR / "00"
        for entry in first_dir.iterdir():
            data = bytearray(entry.read_bytes())
            data[10] ^= 0xFF      # flip a byte inside the HMAC field
            entry.write_bytes(bytes(data))
            break

    return enc_key, hmac_key, computer_uuid
