"""Independent Arq 7 backup writer.

Creates Arq-7-compatible backup destinations from a local source
directory. The result is structured exactly like an Arq.app-produced
destination and is restorable by ``arq_restore`` (BSD reference
implementation) and Arq.app itself.

Compatibility scope (see DESIGN.md §9.3 and
``docs/RESEARCH-backup-creation-feasibility.md``):

- **Implemented**: ``encryptedkeyset.dat``, ``EncryptedObject`` envelope
  (HMAC-SHA256 + AES-256-CBC), binary ``Node`` / ``Tree`` / ``BlobLoc``,
  ``backuprecord`` (binary plist), ``backupconfig.json``,
  ``backupplan.json``, ``backupfolders.json``, per-folder
  ``backupfolder.json``.
- **Skipped on purpose** (avoidable per spec):
  ``treepacks/`` / ``blobpacks/`` / ``largeblobpacks/`` — every object
  is written as a standalone ``EncryptedObject`` under
  ``standardobjects/<2-hex-shard>/<62-hex-blobid>``. The spec
  explicitly permits this via ``BlobLoc.isPacked = false``.
- **Skipped on purpose** (no published parameters): the chunker
  (``chunkerVersion: 3``, ``useBuzhash``). Each file becomes a single
  blob — byte-identical files dedup via SHA-256 ID, but
  modified-in-place files don't dedup the way Arq's chunker would.

The orchestrator round-trips through the validator (``arq_validator``)
in tests; live ``arq_restore`` / Arq.app round-trip is left to the
operator's verification environment (the BSD ``arq_restore`` build
target makes this cheap).
"""

from .backup import Backup, BackupResult, build_backup
from .crypto_write import (
    aes_256_cbc_encrypt,
    build_encrypted_keyset,
    build_encrypted_object,
    compute_blob_id,
    rotate_keyset_password,
    rotate_keyset_password_on_disk,
)
from .exclusions import ExclusionRules
from .lz4_block import lz4_block_compress, lz4_block_decompress, lz4_wrap
from .macos_snapshot import (
    NotMacOSError,
    SnapshotError,
    SnapshotInfo,
    is_macos,
    is_macos_apfs,
    with_apfs_snapshot,
)
from .retention import (
    GcResult,
    PruneRecordsResult,
    RetentionPolicy,
    RetentionResult,
    apply_retention,
    gc_orphan_blobs,
    prune_records,
)
from .types import BlobLoc, FileNode, Tree, TreeNode

__all__ = [
    "Backup",
    "BackupResult",
    "build_backup",
    "BlobLoc",
    "ExclusionRules",
    "FileNode",
    "GcResult",
    "NotMacOSError",
    "PruneRecordsResult",
    "RetentionPolicy",
    "RetentionResult",
    "SnapshotError",
    "SnapshotInfo",
    "Tree",
    "TreeNode",
    "apply_retention",
    "gc_orphan_blobs",
    "is_macos",
    "is_macos_apfs",
    "prune_records",
    "with_apfs_snapshot",
    "build_encrypted_keyset",
    "build_encrypted_object",
    "aes_256_cbc_encrypt",
    "compute_blob_id",
    "rotate_keyset_password",
    "rotate_keyset_password_on_disk",
    "lz4_block_compress",
    "lz4_block_decompress",
    "lz4_wrap",
]

__version__ = "0.1.0"
