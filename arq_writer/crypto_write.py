"""Write-side crypto: ARQO encoder + ``encryptedkeyset.dat`` writer.

The validator's ``arq_validator.crypto`` module is the inverse of what
this module produces — every function here has a counterpart there.
HMAC-SHA256 and PBKDF2-SHA256 use Python's stdlib ``hashlib`` /
``hmac``; AES-256-CBC + PKCS7 runs through the host's ``openssl``
binary (same dependency the read side uses, no Python crypto packages
introduced).
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
import secrets
import struct
import subprocess
from typing import Optional

from arq_validator.crypto import CryptoError

from .constants import (
    ARQO_MAGIC,
    KEYSET_MAGIC,
    KEYSET_PBKDF2_DKLEN,
    KEYSET_PBKDF2_ITERATIONS,
    KEYSET_PLAIN_FIELD_LEN,
    KEYSET_PLAIN_VERSION,
    KEYSET_SALT_BYTES,
    KEYSET_IV_BYTES,
)


def aes_256_cbc_encrypt(
    key: bytes, iv: bytes, plaintext: bytes,
    *, openssl_path: str = "openssl",
) -> bytes:
    """AES-256-CBC + PKCS7-pad encrypt via the openssl CLI."""
    if len(key) != 32:
        raise CryptoError(f"AES-256 needs 32-byte key, got {len(key)}")
    if len(iv) != 16:
        raise CryptoError(f"AES-CBC IV must be 16 bytes, got {len(iv)}")
    cmd = [
        openssl_path, "enc", "-aes-256-cbc",
        "-K", key.hex(), "-iv", iv.hex(),
    ]
    try:
        r = subprocess.run(
            cmd, input=plaintext, capture_output=True, timeout=30,
        )
    except FileNotFoundError as exc:
        raise CryptoError(
            f"openssl CLI not found at '{openssl_path}'"
        ) from exc
    if r.returncode != 0:
        err = r.stderr.decode(errors="replace").strip()[:200]
        raise CryptoError(f"openssl encrypt failed: rc={r.returncode} {err}")
    return r.stdout


def build_encrypted_keyset(
    password: str,
    encryption_key: bytes,
    hmac_key: bytes,
    blob_id_salt: bytes,
    *,
    salt: Optional[bytes] = None,
    iv: Optional[bytes] = None,
    openssl_path: str = "openssl",
) -> bytes:
    """Build an ``encryptedkeyset.dat`` payload.

    Plaintext layout matches the format the validator's
    ``parse_keyset_plaintext`` reads:

        [UInt32 BE version=3]
        [UInt64 BE 32] [encryption_key (32)]
        [UInt64 BE 32] [hmac_key (32)]
        [UInt64 BE 32] [blob_id_salt (32)]

    Storage layout:

        magic (25)  ||  salt (8)  ||  HMAC-SHA256 (32)  ||  IV (16)  ||
        AES-256-CBC(PBKDF2-derived AES key, IV, plaintext)
    """
    for name, b in (
        ("encryption_key", encryption_key),
        ("hmac_key", hmac_key),
        ("blob_id_salt", blob_id_salt),
    ):
        if len(b) != KEYSET_PLAIN_FIELD_LEN:
            raise CryptoError(
                f"{name} must be {KEYSET_PLAIN_FIELD_LEN} bytes, got {len(b)}"
            )
    if salt is None:
        salt = secrets.token_bytes(KEYSET_SALT_BYTES)
    elif len(salt) != KEYSET_SALT_BYTES:
        raise CryptoError(
            f"salt must be {KEYSET_SALT_BYTES} bytes, got {len(salt)}"
        )
    if iv is None:
        iv = secrets.token_bytes(KEYSET_IV_BYTES)
    elif len(iv) != KEYSET_IV_BYTES:
        raise CryptoError(
            f"iv must be {KEYSET_IV_BYTES} bytes, got {len(iv)}"
        )

    plaintext = (
        struct.pack(">I", KEYSET_PLAIN_VERSION)
        + struct.pack(">Q", KEYSET_PLAIN_FIELD_LEN) + encryption_key
        + struct.pack(">Q", KEYSET_PLAIN_FIELD_LEN) + hmac_key
        + struct.pack(">Q", KEYSET_PLAIN_FIELD_LEN) + blob_id_salt
    )
    derived = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt,
        KEYSET_PBKDF2_ITERATIONS, dklen=KEYSET_PBKDF2_DKLEN,
    )
    aes_key, mac_key = derived[:32], derived[32:]
    ciphertext = aes_256_cbc_encrypt(
        aes_key, iv, plaintext, openssl_path=openssl_path,
    )
    mac = _hmac.new(mac_key, iv + ciphertext, hashlib.sha256).digest()
    return KEYSET_MAGIC + salt + mac + iv + ciphertext


def build_encrypted_object(
    plaintext: bytes,
    encryption_key: bytes,
    hmac_key: bytes,
    *,
    session_key: Optional[bytes] = None,
    data_iv: Optional[bytes] = None,
    master_iv: Optional[bytes] = None,
    openssl_path: str = "openssl",
) -> bytes:
    """Build a single-object ``EncryptedObject`` (ARQO).

    Steps mirror the spec:

        1. random session_key (32 B) and data_iv (16 B)
        2. random master_iv (16 B)
        3. ciphertext = AES-256-CBC(session_key, data_iv, plaintext)
        4. encrypted_session = AES-256-CBC(master_key, master_iv,
                                           data_iv ‖ session_key)
        5. body = master_iv ‖ encrypted_session ‖ ciphertext
        6. mac = HMAC-SHA256(hmac_key, body)
        7. return  b"ARQO" ‖ mac ‖ body

    Deterministic IV / session-key inputs are accepted for tests; in
    production, leave them ``None`` so the function generates fresh
    random material per object.
    """
    if len(encryption_key) != 32:
        raise CryptoError(
            f"encryption_key must be 32 bytes, got {len(encryption_key)}"
        )
    if len(hmac_key) != 32:
        raise CryptoError(
            f"hmac_key must be 32 bytes, got {len(hmac_key)}"
        )

    if session_key is None:
        session_key = secrets.token_bytes(32)
    elif len(session_key) != 32:
        raise CryptoError(
            f"session_key must be 32 bytes, got {len(session_key)}"
        )
    if data_iv is None:
        data_iv = secrets.token_bytes(16)
    elif len(data_iv) != 16:
        raise CryptoError(
            f"data_iv must be 16 bytes, got {len(data_iv)}"
        )
    if master_iv is None:
        master_iv = secrets.token_bytes(16)
    elif len(master_iv) != 16:
        raise CryptoError(
            f"master_iv must be 16 bytes, got {len(master_iv)}"
        )

    encrypted_session = aes_256_cbc_encrypt(
        encryption_key, master_iv, data_iv + session_key,
        openssl_path=openssl_path,
    )
    if len(encrypted_session) != 64:
        # 48 bytes plaintext (16 + 32) + 16 bytes PKCS7 pad = 64. Any
        # other size means openssl version/feature mismatch.
        raise CryptoError(
            f"encrypted_session unexpected size: {len(encrypted_session)}"
        )

    ciphertext = aes_256_cbc_encrypt(
        session_key, data_iv, plaintext, openssl_path=openssl_path,
    )

    body = master_iv + encrypted_session + ciphertext
    mac = _hmac.new(hmac_key, body, hashlib.sha256).digest()
    return ARQO_MAGIC + mac + body


def rotate_keyset_password(
    keyset_blob: bytes,
    *,
    old_password: str,
    new_password: str,
    openssl_path: str = "openssl",
) -> bytes:
    """Re-encrypt an ``encryptedkeyset.dat`` payload under a new
    password without changing the master keys.

    The (encryption_key, hmac_key, blob_id_salt) triple stays the
    same so every existing backuprecord / blob remains decryptable
    afterward. Only the keyset's salt + IV + ciphertext + outer
    HMAC change. Re-derives the storage AES + HMAC keys via
    PBKDF2-SHA256 with fresh 8-byte salt.

    Returns the new ``encryptedkeyset.dat`` bytes — caller is
    responsible for writing them back via ``backend.write_all`` (or
    `Path.write_bytes`). The old keyset is **not** modified
    in-place by this function — atomicity is the caller's
    responsibility (write to a temp path + rename).
    """
    # Local import to avoid a top-level cycle: the validator
    # decrypt_keyset is the inverse of build_encrypted_keyset.
    from arq_validator.crypto import decrypt_keyset

    keyset = decrypt_keyset(
        keyset_blob, old_password, openssl_path=openssl_path,
    )
    return build_encrypted_keyset(
        new_password,
        keyset.encryption_key, keyset.hmac_key, keyset.blob_id_salt,
        openssl_path=openssl_path,
    )


def compute_blob_id(blob_id_salt: bytes, plaintext: bytes) -> str:
    """SHA-256 hex blob identifier — Arq 7 content addressing.

    Matches the Arq 5 convention (``SHA-256(salt ‖ plaintext)``)
    extended to SHA-256 in Arq 7. Identical-content files share the
    same blob ID and dedup naturally.
    """
    h = hashlib.sha256()
    h.update(blob_id_salt)
    h.update(plaintext)
    return h.hexdigest()
