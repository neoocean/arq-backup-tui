"""Constants used by the Arq 7 writer.

Most binary-format constants are re-exported from
``arq_validator.constants`` to keep the read- and write-side in lockstep.
The pieces below are exclusive to the writer (compression-type IDs,
``Tree`` schema versions, etc.).
"""

from __future__ import annotations

# Re-export the read-side format constants for convenience.
from arq_validator.constants import (  # noqa: F401
    ARQO_ENC_SESSION_BYTES,
    ARQO_HEADER_BYTES,
    ARQO_HMAC_BODY_OFFSET,
    ARQO_HMAC_BYTES,
    ARQO_HMAC_OFFSET,
    ARQO_MAGIC,
    ARQO_MASTER_IV_BYTES,
    BACKUPFOLDERS_DIR,
    BACKUPRECORDS_DIR,
    BLOBPACKS_DIR,
    KEYSET_FILE,
    KEYSET_HEADER_BYTES,
    KEYSET_HMAC_BYTES,
    KEYSET_IV_BYTES,
    KEYSET_MAGIC,
    KEYSET_PBKDF2_DKLEN,
    KEYSET_PBKDF2_ITERATIONS,
    KEYSET_PLAIN_FIELD_LEN,
    KEYSET_PLAIN_VERSION,
    KEYSET_SALT_BYTES,
    LARGEBLOBPACKS_DIR,
    STANDARDOBJECTS_DIR,
    TREEPACKS_DIR,
)

# Compression type IDs in BlobLoc.compressionType:
#   0 = none, 1 = Gzip (Arq 5 legacy), 2 = LZ4 (Arq 7 default)
COMPRESSION_NONE = 0
COMPRESSION_GZIP = 1
COMPRESSION_LZ4 = 2

# Tree binary format version. Arq 7 uses version=3 (the version Arq.app
# emits today). Versions >=2 include the win_reparse_tag /
# win_reparse_point_is_directory fields in Node.
TREE_VERSION = 3

# Node fields gated by Tree version.
NODE_REPARSE_FIELDS_MIN_TREE_VERSION = 2

# blobIdentifierType in backupconfig.json: 1 = SHA-1, 2 = SHA-256.
BLOB_ID_SHA1 = 1
BLOB_ID_SHA256 = 2

# Default value emitted by Arq.app today; matches reference backups.
DEFAULT_MAX_PACKED_ITEM_LENGTH = 256000

# Arq 7 backuprecord file format version (top-level "version" key).
BACKUPRECORD_VERSION = 100

# Backup plan version inside backupPlanJSON.
BACKUP_PLAN_VERSION = 2

# Default chunker version reported in backupconfig.json. Even though
# we don't actually run the chunker (we emit one blob per file), this
# value is recorded in the config so existing tooling sees the
# expected schema.
DEFAULT_CHUNKER_VERSION = 3
