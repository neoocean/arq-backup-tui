# Coverage matrix vs. Arq Backup 7

This document records, feature by feature, how this project's
``arq_validator`` / ``arq_reader`` / ``arq_writer`` triple covers
Arq Backup 7. It is the single-page answer to "what can this
project do with an Arq 7 destination?"

The matrix is not a roadmap — items marked **out of scope** are
deliberate trade-offs (Arq.app side concern, redundant with
``arq_restore``, or the format isn't published). Items marked
**partial** are honest about which sub-features are missing.

Earlier versions (Arq 5, Arq 6) and the separate Arq Cloud Backup
product are not in scope for this comparison.

## Headline status

| Area              | Status                                                  |
|-------------------|---------------------------------------------------------|
| Read              | ✅ End-to-end restorer (standalone + packed objects)     |
| Validate          | ✅ All four tiers (L0 / L1a / L1b / L2) + resumable audit-drip |
| Write             | ✅ Standalone-objects mode + optional pack mode; chunker matches Arq.app v7.41 |

The aggregate test count is **198 unit tests** at the time this
table was last updated; the suite runs in ~45 s on a stdlib-only
toolchain (``python -m unittest discover``).

## Detailed feature matrix

Legend: ✅ implemented + tested · ⚠️ partial · ❌ not implemented ·
🔴 out of scope.

### Crypto + container format

| Arq 7 capability                                              | Status | Module |
|---------------------------------------------------------------|:------:|--------|
| ``EncryptedObject`` (ARQO) decrypt + HMAC verify              |  ✅    | ``arq_validator.crypto``, ``arq_reader.decrypt`` |
| ``EncryptedObject`` (ARQO) encrypt (write path)               |  ✅    | ``arq_writer.crypto_write`` |
| ``encryptedkeyset.dat`` (PBKDF2-SHA256) decrypt               |  ✅    | ``arq_validator.crypto`` |
| ``encryptedkeyset.dat`` build (write path)                    |  ✅    | ``arq_writer.crypto_write.build_encrypted_keyset`` |
| AES-256-CBC + PKCS7 (via host ``openssl``)                    |  ✅    | ``arq_writer.crypto_write.aes_256_cbc_encrypt`` |
| LZ4 block compression (compress + decompress)                 |  ✅    | ``arq_writer.lz4_block`` |

### Object storage layout

| Arq 7 capability                                              | Status | Module |
|---------------------------------------------------------------|:------:|--------|
| Computer-tree layout discovery                                |  ✅    | ``arq_validator.layout`` |
| ``standardobjects/<2-hex-shard>/<62-hex>`` standalone blobs   |  ✅    | ``arq_writer.backup``, ``arq_reader.restore`` |
| ``treepacks/`` / ``blobpacks/`` / ``largeblobpacks/`` read    |  ✅    | ``arq_reader.restore`` (via ``BlobLoc.isPacked``) |
| Pack-file emission                                            |  ✅    | ``arq_writer.pack_builder.PackBuilder`` (opt-in via ``Backup(use_packs=True)``) |
| ``backupfolders/<folder>/backuprecords/`` shape (2-level)     |  ✅    | ``arq_validator.layout``, ``arq_writer.backup`` |

### Tree / Node / Commit binary format

| Arq 7 capability                                              | Status | Module |
|---------------------------------------------------------------|:------:|--------|
| ``Tree`` / ``Node`` / ``BlobLoc`` parse                       |  ✅    | ``arq_reader.parse`` |
| ``Tree`` / ``Node`` / ``BlobLoc`` write                       |  ✅    | ``arq_writer.serialize`` |
| ``backuprecord`` (binary plist) parse                         |  ✅    | ``arq_reader.parse``, ``arq_validator.tiers`` |
| ``backuprecord`` write                                        |  ✅    | ``arq_writer.backuprecord`` |
| JSON sidecars (``backupconfig`` / ``backupplan`` / ``backupfolder`` / ``backupfolders``) |  ✅    | ``arq_writer.json_configs`` |

### Chunker (``chunkerVersion: 3 + useBuzhash``)

| Arq 7 capability                                              | Status | Module |
|---------------------------------------------------------------|:------:|--------|
| Generic Buzhash content-defined chunker                       |  ✅    | ``arq_writer.chunker.Buzhash`` |
| Arq.app v7.41 exact chunker parameters                        |  ✅    | ``arq_writer.arq_chunker_params`` (T table + window=256 + boundary_bits=16 + max=128 KiB; min=4 KiB at low confidence) |
| Mach-O T-table + numeric-constant scanner                     |  ✅    | ``arq_writer.macho_buzhash_finder`` |
| Behavioral chunker-parameter inference from chunk-size dist   |  ✅    | ``infer_parameters_from_chunk_sizes`` |
| (min, max) co-located pair-search heuristic                   |  ✅    | ``find_min_max_pairs`` + ``arq-buzhash-find pair-search`` |
| Falsification harness (compare our chunks vs. Arq.app)        |  ✅    | ``arq_writer.chunker_oracle`` + ``arq-buzhash-find verify-chunking`` |

### Restore (read path end-to-end)

| Arq 7 capability                                              | Status | Module |
|---------------------------------------------------------------|:------:|--------|
| Walk computer / folder list                                   |  ✅    | ``arq_reader.restore.Restore`` |
| ``master`` ref → backuprecord → tree traversal                |  ✅    | ``arq_reader.restore`` |
| File restore (regular files)                                  |  ✅    | ``arq_reader.restore`` |
| Pack-stored blob retrieval (``isPacked: true``)               |  ✅    | ``arq_reader.restore`` |
| Multi-folder restore from one keyset                          |  ✅    | ``arq_reader.restore`` |
| Symlink restore                                               |  ⚠️    | Mode-flagged in ``Node`` and exposed; physical symlink emission left to the caller (cross-platform) |
| Hardlink dedup                                                |  ❌    | Treated as separate files (matches Arq.app behavior) |
| xattr / ACL application                                       |  ❌    | Parsed and exposed in ``Node``; physical apply not implemented (cross-platform) |
| Resource forks / Mac-specific metadata application            |  ❌    | Out of scope (cross-platform stance) |

### Backup (write path end-to-end)

| Arq 7 capability                                              | Status | Module |
|---------------------------------------------------------------|:------:|--------|
| Source-tree → backup destination                              |  ✅    | ``arq_writer.backup.build_backup`` |
| ``backupconfig.json`` / ``backupplan.json`` / ``backupfolder.json`` |  ✅    | ``arq_writer.json_configs`` |
| Standalone-objects emission (``standardobjects/``)            |  ✅    | ``arq_writer.backup`` (default) |
| Packed emission (``treepacks/`` + ``blobpacks/`` + ``largeblobpacks/``) |  ✅    | ``Backup(use_packs=True)`` |
| Buzhash chunking with generic params                          |  ✅    | ``build_backup(..., chunker_config=ChunkerConfig(...))`` |
| Buzhash chunking with Arq.app v7.41 params                    |  ✅    | opt-in via ``import arq_writer.arq_chunker_params`` |
| Within-run dedup (identical SHA-256 blobs share one BlobLoc)  |  ✅    | ``Backup._written_blobs`` cache; standalone + packed modes |
| Cross-run dedup against an existing destination               |  ✅    | ``build_backup(..., dedup_against_existing=True)`` reuses the destination's keyset and seeds the cache from ``standardobjects/`` + the most recent backuprecord (covers packed mode); see ``arq_writer.dedup`` |
| Incremental backup (commit chain on existing destination)     |  ⚠️    | Cross-run dedup works (no rewrites of unchanged blobs). Explicit parent-commit linking via a dedicated field isn't required — Arq 7 backuprecords are ordered chronologically by path (``backuprecords/<bucket>/<num>``), so chronologically newer records are implicitly children of older ones |
| Retention / pruning of old commits                            |  🔴   | Arq.app side concern (not part of the on-disk format spec) |
| Schedule-driven runs                                          |  🔴   | Arq.app side concern |

### Validation

| Arq 7 capability                                              | Status | Module |
|---------------------------------------------------------------|:------:|--------|
| L0 — directory-layout shape check                             |  ✅    | ``arq_validator.tiers.run_l0`` |
| L1a — ARQO magic-byte sample sweep                            |  ✅    | ``arq_validator.tiers.run_l1a`` |
| L1b — keyset decrypt + latest backuprecord HMAC               |  ✅    | ``arq_validator.tiers.run_l1b`` |
| L2 — full HMAC sweep over every EncryptedObject               |  ✅    | ``arq_validator.tiers.run_l2`` |
| Resumable audit-drip (cursor + throttle + state file)         |  ✅    | ``arq_validator.audit_drip`` |
| Pluggable storage backend (read-side)                         |  ✅    | ``arq_validator.backend`` |
| SFTP backend                                                  |  ✅    | ``arq_validator.sftp`` |

### Storage backends (where the destination lives)

| Backend                                                       | Status | Notes |
|---------------------------------------------------------------|:------:|-------|
| Local filesystem / NAS                                        |  ✅    | Default; ``arq_validator.backend.LocalBackend`` |
| SFTP                                                          |  ✅    | Built-in ``arq_validator.sftp.SftpBackend`` for the validator |
| S3 / Wasabi / B2 / GCS / Azure / OneDrive / Dropbox           |  ❌    | None implemented; mount via ``rclone`` and point the local backend at the mount, or use Arq.app for cloud-only destinations |

## Boundary explanations (why some "❌"s are intentional)

- **xattr / ACL / hardlink / resource-fork restoration**: out of
  scope by deliberate cross-platform stance. The metadata is parsed
  and exposed in the ``Node``; physically applying it is OS-specific
  (different syscalls on Linux vs. macOS) and the round-trip use
  cases for our writer don't depend on it.

- **Cloud storage backends (S3 / B2 / GCS / Azure / OneDrive /
  Dropbox)**: each backend is a separate auth + transport stack.
  Users can mount any of them via ``rclone`` and point our local
  backend at the mount; pure-Python implementations are deferred
  until a concrete user need surfaces.

- **Incremental backup / commit chain on the writer side**: each
  ``build_backup`` run writes a fresh backuprecord regardless of
  prior runs, so every run is a complete snapshot — no explicit
  parent-commit field is needed (Arq 7 doesn't have one). When
  ``dedup_against_existing=True``, the writer reuses the
  destination's existing keyset (so SHA-256 blob_ids line up across
  runs) and seeds the within-run dedup cache from existing
  ``standardobjects/`` files and the most-recent backuprecord's
  walk. Result: identical content from a prior run isn't
  re-encrypted or re-written. The remaining ❌ box would be
  *tree-walk reuse* — recognizing that an unchanged subtree's
  Tree blob is byte-identical to a prior run's and skipping the
  recursive walk entirely. Today every run still walks the full
  source tree even when dedup is on; the cost is ``O(source bytes)``
  read + hash, not write.

- **Retention / scheduling**: those concerns belong to Arq.app's
  policy layer rather than to the on-disk format. A standalone CLI
  scheduler could wrap ``build_backup`` if needed; the format
  itself is unaffected.

## What "covers" actually means here

This project's correctness target for Arq 7 is:

1. **Read**: byte-identical reconstruction of any file in any
   commit, given the keyset password + an Arq 7 destination.
2. **Validate**: detect corruption (bit-flip, truncation, partial
   upload) before restore, without exfiltrating plaintext.
3. **Write**: produce a destination that Arq.app and ``arq_restore``
   both accept and round-trip identically to its input source tree.

A ✅ in the matrix means the corresponding test suite covers the
happy path and the major branches. ⚠️ means the metadata is
parsed/preserved but the physical effect (e.g. emitting a symlink)
is left to the caller. ❌ means it isn't there and a test would
fail (or doesn't exist). 🔴 means it lives outside the on-disk
format spec entirely.
