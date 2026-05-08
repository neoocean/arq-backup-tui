"""Arq 7 on-disk format constants.

Spec source: https://www.arqbackup.com/documentation/arq7/English.lproj/dataFormat.html

A handful of empirically-corrected fields (noted inline) come from
neoocean/docker-monitor's reverse-engineering against a live Hetzner
SFTP destination on 2026-05-04. Those corrections (25-byte unpadded
keyset magic, 32-byte key fields) supersede the published Arq 7 spec
where they disagree.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Directory layout (per computer UUID)
# ---------------------------------------------------------------------------

BLOBPACKS_DIR = "blobpacks"
TREEPACKS_DIR = "treepacks"
LARGEBLOBPACKS_DIR = "largeblobpacks"
STANDARDOBJECTS_DIR = "standardobjects"
BACKUPFOLDERS_DIR = "backupfolders"
BACKUPRECORDS_DIR = "backuprecords"
KEYSET_FILE = "encryptedkeyset.dat"

OBJECT_FAMILIES = (
    BLOBPACKS_DIR,
    TREEPACKS_DIR,
    LARGEBLOBPACKS_DIR,
    STANDARDOBJECTS_DIR,
)

# All four object trees are sharded by the first 2 hex chars of the
# original SHA/UUID. Shard prefix is 00..ff (256 directories).
SHARD_COUNT = 256

# Pack files in {blob,tree,largeblob}packs/<2hex>/ have UUID-like
# names with the first 2 chars stripped (= shard prefix). Total chars
# of the visible name = 30 hex+dash chars + ".pack" suffix. Example:
#   blobpacks/00/036BE7-B92F-4FCF-A762-EB829DCE7EC3.pack
PACK_NAME_RE = re.compile(
    r"^[0-9A-F]{6}(-[0-9A-F]{4}){3}-[0-9A-F]{12}\.pack$",
    re.IGNORECASE,
)

# standardobjects/<2hex>/<62-char SHA-256 hex (post-shard)>. The
# 64-char total hash splits as: first 2 = shard, remaining 62 = name.
STANDARDOBJECT_NAME_RE = re.compile(r"^[0-9a-f]{62}$")

# Computer UUID at top level (8-4-4-4-12, uppercase canonical).
COMPUTER_UUID_RE = re.compile(
    r"^[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}$",
    re.IGNORECASE,
)
FOLDER_UUID_RE = COMPUTER_UUID_RE

# ---------------------------------------------------------------------------
# encryptedkeyset.dat (per-computer master keys, encrypted at rest)
# ---------------------------------------------------------------------------
#
# Stored layout (CORRECTED — published spec said magic was 32 bytes
# NUL-padded, but the actual file format has NO NUL pad; the salt
# begins immediately after the 25-byte literal magic):
#     0..25    magic "ARQ_ENCRYPTED_MASTER_KEYS" (25 bytes literal)
#     25..33   PBKDF2 salt (8 bytes)
#     33..65   HMAC-SHA256(derived[32:64], IV + ciphertext) (32 bytes)
#     65..81   AES-256-CBC IV (16 bytes)
#     81..end  ciphertext (PKCS7 padded, AES-block aligned)
#
# Plaintext layout (CORRECTED — published spec said key fields were
# 64 bytes; actual format is 32 bytes per field):
#     0..4     encryption version (u32 BE = 0x00000003)
#     4..12    encryption-key length (u64 BE = 0x20)
#     12..44   encryption key (32 bytes)
#     44..52   hmac-key length (u64 BE = 0x20)
#     52..84   hmac key (32 bytes)
#     84..92   blob-id-salt length (u64 BE = 0x20)
#     92..124  blob id salt (32 bytes)

KEYSET_MAGIC = b"ARQ_ENCRYPTED_MASTER_KEYS"
KEYSET_MAGIC_BYTES = len(KEYSET_MAGIC)        # 25 (no NUL pad)
KEYSET_SALT_BYTES = 8
KEYSET_HMAC_BYTES = 32
KEYSET_IV_BYTES = 16
KEYSET_HEADER_BYTES = (
    KEYSET_MAGIC_BYTES
    + KEYSET_SALT_BYTES
    + KEYSET_HMAC_BYTES
    + KEYSET_IV_BYTES
)                                             # 81

KEYSET_PBKDF2_ITERATIONS = 200_000
KEYSET_PBKDF2_DKLEN = 64
KEYSET_PLAIN_VERSION = 0x00000003
KEYSET_PLAIN_FIELD_LEN = 32

# ---------------------------------------------------------------------------
# EncryptedObject (each pack-stored object: commit / tree / blob)
# ---------------------------------------------------------------------------
#
#   0..4     magic "ARQO" (0x4152514F)
#   4..36    HMAC-SHA256(plaintext_keyset.hmac_key, bytes[36:end])
#   36..52   master IV (16 bytes)
#   52..116  encrypted (data_IV + session_key) — 48B plaintext +
#            16B PKCS7 pad after AES-256-CBC with master key
#   116..end ciphertext (AES-256-CBC with session key)
#
# HMAC body covers everything from offset 36 through end-of-object.

ARQO_MAGIC = b"ARQO"
ARQO_HMAC_OFFSET = 4
ARQO_HMAC_BYTES = 32
ARQO_HMAC_BODY_OFFSET = len(ARQO_MAGIC) + ARQO_HMAC_BYTES   # 36
ARQO_MASTER_IV_BYTES = 16
ARQO_ENC_SESSION_BYTES = 64                                # 16 IV + 32 sk + 16 pad
ARQO_HEADER_BYTES = (
    ARQO_HMAC_BODY_OFFSET
    + ARQO_MASTER_IV_BYTES
    + ARQO_ENC_SESSION_BYTES
)                                                          # 116
