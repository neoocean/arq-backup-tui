"""Full ``EncryptedObject`` decryption.

The validator's :func:`arq_validator.crypto.verify_encrypted_object_hmac`
already covers HMAC verification; the reader needs to go one step
further and recover the plaintext. The pipeline (inverse of
``arq_writer.crypto_write.build_encrypted_object``):

    1. Sanity-check magic + size.
    2. Verify HMAC-SHA256(hmac_key, body[36:]).
    3. AES-256-CBC decrypt ``body[36:36+16]`` (master_iv) +
       ``body[52:52+64]`` (encrypted_session) using the keyset's
       ``encryption_key`` -> 48 bytes plaintext = data_iv (16) +
       session_key (32).
    4. AES-256-CBC decrypt the trailing ciphertext using session_key
       and data_iv -> plaintext blob bytes (still LZ4-wrapped if the
       blob is a Tree / Node / file content).
    5. Optionally LZ4-unwrap.
"""

from __future__ import annotations

from arq_validator import constants as C
from arq_validator.crypto import (
    aes_256_cbc_decrypt,
    verify_encrypted_object_hmac,
)
from arq_writer.lz4_block import lz4_unwrap


class DecryptError(RuntimeError):
    """Raised on HMAC mismatch, malformed ARQO, or AES failure."""


def decrypt_encrypted_object(
    arqo: bytes,
    encryption_key: bytes,
    hmac_key: bytes,
    *,
    openssl_path: str = "openssl",
    skip_hmac: bool = False,
) -> bytes:
    """Decrypt a single ``EncryptedObject`` blob to its raw plaintext.

    Pass ``skip_hmac=True`` only for synthetic test fixtures or when
    you've already authenticated the bytes via another path; production
    callers should leave it ``False`` so corruption is caught early.
    """
    if len(arqo) < C.ARQO_HEADER_BYTES:
        raise DecryptError(
            f"ARQO too short: {len(arqo)} < {C.ARQO_HEADER_BYTES}"
        )
    if arqo[: len(C.ARQO_MAGIC)] != C.ARQO_MAGIC:
        raise DecryptError(f"bad ARQO magic: {arqo[:4]!r}")

    if not skip_hmac:
        ok, expected_hex, actual_hex = verify_encrypted_object_hmac(
            arqo, hmac_key,
        )
        if not ok:
            raise DecryptError(
                f"HMAC mismatch (expected {expected_hex[:16]}…, "
                f"got {actual_hex[:16]}…)"
            )

    o = C.ARQO_HMAC_BODY_OFFSET                # 36
    master_iv = arqo[o : o + C.ARQO_MASTER_IV_BYTES]    # 16
    o += C.ARQO_MASTER_IV_BYTES
    encrypted_session = arqo[o : o + C.ARQO_ENC_SESSION_BYTES]  # 64
    o += C.ARQO_ENC_SESSION_BYTES
    ciphertext = arqo[o:]

    try:
        session_pt = aes_256_cbc_decrypt(
            encryption_key, master_iv, encrypted_session,
            openssl_path=openssl_path,
        )
    except Exception as exc:
        raise DecryptError(f"session-key decrypt failed: {exc}") from exc
    if len(session_pt) != 48:
        raise DecryptError(
            f"unexpected session-key plaintext length: {len(session_pt)}"
        )
    data_iv, session_key = session_pt[:16], session_pt[16:]

    if not ciphertext:
        return b""
    try:
        plaintext = aes_256_cbc_decrypt(
            session_key, data_iv, ciphertext,
            openssl_path=openssl_path,
        )
    except Exception as exc:
        raise DecryptError(f"payload decrypt failed: {exc}") from exc
    return plaintext


def decrypt_lz4_arqo(
    arqo: bytes,
    encryption_key: bytes,
    hmac_key: bytes,
    *,
    openssl_path: str = "openssl",
    skip_hmac: bool = False,
) -> bytes:
    """Decrypt then LZ4-unwrap.

    Most things stored under ``standardobjects/`` are an
    LZ4-wrapped payload inside an ``EncryptedObject`` (matching the
    writer's ``lz4_wrap`` -> ``build_encrypted_object`` pipeline).
    """
    plaintext = decrypt_encrypted_object(
        arqo, encryption_key, hmac_key,
        openssl_path=openssl_path, skip_hmac=skip_hmac,
    )
    return lz4_unwrap(plaintext)
