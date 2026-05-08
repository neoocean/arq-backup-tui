# Coverage matrix vs. Arq Backup 5 / 6 / 7

This document records, per Arq.app version, which features the
``arq_validator`` / ``arq_reader`` / ``arq_writer`` triple covers,
which it doesn't, and why. It is the single-page answer to "can
this project handle a backup made by Arq <N>?"

The matrix is not a roadmap — items marked **out of scope** are
deliberate trade-offs (separate product, redundant with arq_restore,
or the format isn't published). Items marked **partial** are honest
about which sub-features are missing.

## TL;DR by version

| Version           | Read                                    | Validate                              | Write                                          | Notes |
|-------------------|-----------------------------------------|---------------------------------------|------------------------------------------------|-------|
| **Arq 5**         | ✅ End-to-end restorer                   | ⚠️ Format-incompatible with validator | ❌ Not implemented                             | Oldest line still in active community use; read path complete |
| **Arq 6**         | ✅ End-to-end restorer (shared with 5)   | ⚠️ Same                                | ❌ Not implemented                             | Binary format identical to Arq 5 in published parts |
| **Arq 7**         | ✅ End-to-end restorer                   | ✅ All four tiers (L0/L1a/L1b/L2)      | ✅ Standalone + packed; chunker matches Arq.app v7.41 | Primary target; most complete |
| **Arq Cloud**     | 🔴 Out of scope                          | 🔴 Out of scope                        | 🔴 Out of scope                                | Separate proprietary cloud-only product |

## Format families

Arq doesn't share its on-disk format between major version lines.
Internally there are essentially two distinct families:

- **Arq 5/6 family** — binary ``Tree`` v10–v22 (skipping v13),
  binary ``Commit`` v3–v12, ``BlobKey`` (SHA-1 blob IDs),
  ``encryptionvN.dat`` keyset using PBKDF2-**SHA1**, optional
  Glacier metadata, ``bucketdata/<folder>/`` layout, framed
  ``.pack`` containers with optional Glacier extension block.
- **Arq 7 family** — binary plist ``backuprecord``, binary
  ``Tree`` / ``Node`` (different layout from 5/6), JSON
  ``backupconfig`` / ``backupplan`` / ``backupfolder``,
  ``encryptedkeyset.dat`` using PBKDF2-**SHA256**, plain
  ARQO-concatenation packs in ``treepacks/`` / ``blobpacks/`` /
  ``largeblobpacks/``, standalone object family in
  ``standardobjects/``, per-folder Buzhash chunker
  (``chunkerVersion: 3 + useBuzhash``).

Both share the **EncryptedObject** ("ARQO") wire format —
``ARQO`` magic + IV + HMAC-SHA256 + AES-256-CBC body — and both
support LZ4 block compression of the post-decryption plaintext.
Arq 5/6 additionally supports legacy Gzip, which our restorer
handles via stdlib ``gzip``.

## Detailed feature matrix

Legend: ✅ implemented + tested · ⚠️ partial · ❌ not implemented ·
🔴 out of scope · ➖ not applicable.

### Crypto + container format

| Capability                                              | Arq 5 | Arq 6 | Arq 7 | Module |
|---------------------------------------------------------|:-----:|:-----:|:-----:|--------|
| EncryptedObject (ARQO) decrypt + HMAC verify            |  ✅   |  ✅   |  ✅   | ``arq_validator.crypto``, ``arq_reader.decrypt`` |
| EncryptedObject (ARQO) encrypt (write path)             |  ❌   |  ❌   |  ✅   | ``arq_writer.crypto_write`` |
| Keyset (``encryptionvN.dat`` v2 + v3) decrypt           |  ✅   |  ✅   |  ➖   | ``arq_reader.arq5_keyset.decrypt_arq5_keyset`` |
| Keyset (``encryptedkeyset.dat``) decrypt                |  ➖   |  ➖   |  ✅   | ``arq_validator.crypto`` |
| Keyset write path                                       |  ⚠️   |  ⚠️   |  ✅   | Arq 5: ``build_arq5_keyset_blob`` (testing only); Arq 7: ``arq_writer.crypto_write.build_encrypted_keyset`` |
| LZ4 block compression (compress + decompress)           |  ✅   |  ✅   |  ✅   | ``arq_writer.lz4_block`` |
| Gzip legacy compression (decompress only)               |  ✅   |  ✅   |  ➖   | stdlib ``gzip`` via ``arq_reader.restore`` |
| AES-256-CBC + PKCS7 (via ``openssl``)                   |  ✅   |  ✅   |  ✅   | ``arq_writer.crypto_write.aes_256_cbc_encrypt`` |

### Object storage layout

| Capability                                              | Arq 5 | Arq 6 | Arq 7 | Module |
|---------------------------------------------------------|:-----:|:-----:|:-----:|--------|
| Standalone-object layout discovery                      |  ➖   |  ➖   |  ✅   | ``arq_validator.layout`` |
| Sharded blob path resolution (``standardobjects/``)     |  ➖   |  ➖   |  ✅   | ``arq_writer.backup`` |
| Sharded blob path resolution (Arq 5 historical layouts) |  ✅   |  ✅   |  ➖   | ``arq_reader.arq5_keyset.arq5_object_paths`` |
| Pack file read (``isPacked: true`` Arq 7 packs)         |  ➖   |  ➖   |  ✅   | ``arq_reader.restore`` |
| Pack file read (Arq 5/6 ``.pack`` + ``.index``)         |  ✅   |  ✅   |  ➖   | ``arq_reader.arq5_pack`` |
| Pack file emission (Arq 7)                              |  ❌   |  ❌   |  ✅   | ``arq_writer.pack_builder.PackBuilder`` |
| Pack file emission (Arq 5/6)                            |  ⚠️   |  ⚠️   |  ➖   | Building helpers exist (``build_pack_body`` / ``build_index_body``) but no end-to-end backup writer wires them |
| Glacier extension block in ``.pack`` (skip-on-read)     |  ✅   |  ✅   |  ➖   | ``arq_reader.arq5_pack`` |

### Tree / commit / node binary parsing

| Capability                                              | Arq 5 | Arq 6 | Arq 7 | Module |
|---------------------------------------------------------|:-----:|:-----:|:-----:|--------|
| ``Tree`` parse (Arq 5/6 — versions 10–22, ex. v13)      |  ✅   |  ✅   |  ➖   | ``arq_reader.arq5_binary.parse_tree`` |
| ``Commit`` parse (Arq 5/6 — versions 3–12)              |  ✅   |  ✅   |  ➖   | ``arq_reader.arq5_binary.parse_commit`` |
| ``Node`` parse (Arq 5/6, version-gated fields)          |  ✅   |  ✅   |  ➖   | ``arq_reader.arq5_binary.parse_node`` |
| ``BlobKey`` parse (Arq 5/6, SHA-1)                      |  ✅   |  ✅   |  ➖   | ``arq_reader.arq5_binary.parse_blob_key`` |
| ``Tree`` / ``Node`` / ``BlobLoc`` parse (Arq 7)         |  ➖   |  ➖   |  ✅   | ``arq_reader.parse`` |
| ``Tree`` / ``Node`` write (Arq 7)                       |  ➖   |  ➖   |  ✅   | ``arq_writer.serialize`` |
| ``Tree`` / ``Node`` write (Arq 5/6)                     |  ❌   |  ❌   |  ➖   | Symmetric writer not implemented |

### Restore (read path end-to-end)

| Capability                                              | Arq 5 | Arq 6 | Arq 7 | Module |
|---------------------------------------------------------|:-----:|:-----:|:-----:|--------|
| Walk computer / folder list                             |  ✅   |  ✅   |  ✅   | ``Arq5Restore`` / ``Restore`` |
| ``master`` ref → commit → tree traversal                |  ✅   |  ✅   |  ✅   | both restorers |
| File restore (regular files)                            |  ✅   |  ✅   |  ✅   | both restorers |
| Symlink restore                                         |  ⚠️   |  ⚠️   |  ⚠️   | Mode-flagged in metadata; physical symlink emission left to caller |
| Hardlink dedup                                          |  ❌   |  ❌   |  ❌   | Treated as separate files (Arq.app behavior) |
| xattr / ACL restore                                     |  ❌   |  ❌   |  ❌   | Parsed but not applied (cross-platform compatibility) |
| Resource forks / Mac metadata                           |  ❌   |  ❌   |  ❌   | Out of scope |
| Multi-folder restore from one master keyset             |  ✅   |  ✅   |  ✅   | both restorers |

### Backup (write path end-to-end)

| Capability                                              | Arq 5 | Arq 6 | Arq 7 | Module |
|---------------------------------------------------------|:-----:|:-----:|:-----:|--------|
| Source-tree → backup destination                        |  ❌   |  ❌   |  ✅   | ``arq_writer.backup.build_backup`` |
| ``backupconfig.json`` / ``backupplan.json`` / ``backupfolder.json`` | ➖ | ➖ | ✅ | ``arq_writer.json_configs`` |
| ``backuprecord`` (binary plist)                         |  ➖   |  ➖   |  ✅   | ``arq_writer.backuprecord`` |
| Optional packed-object emission                         |  ➖   |  ➖   |  ✅   | ``Backup(use_packs=True)`` |
| Buzhash content-defined chunking (generic)              |  ➖   |  ➖   |  ✅   | ``arq_writer.chunker`` |
| Buzhash with Arq.app v7.41 parameters                   |  ➖   |  ➖   |  ✅   | ``arq_writer.arq_chunker_params`` (opt-in) |
| Incremental backup (commit chain on existing dest)      |  ❌   |  ❌   |  ❌   | Each ``build_backup`` writes a new full backuprecord; no parent-commit linking yet |
| Retention / pruning of old commits                      |  ❌   |  ❌   |  ❌   | Out of scope (Arq.app side concern) |
| Schedule-driven runs                                    |  ❌   |  ❌   |  ❌   | Out of scope |

### Validation

The validator is Arq-7-only by design — it's the integrity-audit
companion to ``arq_writer``, and the closest analogue for Arq 5/6
(``arq_restore -v``) already exists upstream.

| Capability                                              | Arq 5 | Arq 6 | Arq 7 | Module |
|---------------------------------------------------------|:-----:|:-----:|:-----:|--------|
| L0 — directory-layout shape check                       |  ❌   |  ❌   |  ✅   | ``arq_validator.tiers.run_l0`` |
| L1a — ARQO magic-byte sample sweep                      |  ❌   |  ❌   |  ✅   | ``arq_validator.tiers.run_l1a`` |
| L1b — keyset decrypt + latest backuprecord HMAC         |  ❌   |  ❌   |  ✅   | ``arq_validator.tiers.run_l1b`` |
| L2 — full HMAC sweep over every EncryptedObject         |  ❌   |  ❌   |  ✅   | ``arq_validator.tiers.run_l2`` |
| Resumable audit-drip (cursor + throttle + state file)   |  ❌   |  ❌   |  ✅   | ``arq_validator.audit_drip`` |
| Pluggable storage backend (Local + SFTP)                |  ❌   |  ❌   |  ✅   | ``arq_validator.backend`` / ``arq_validator.sftp`` |

### Storage backends (where the destination lives)

| Backend                                                 | Arq 5 | Arq 6 | Arq 7 | Notes |
|---------------------------------------------------------|:-----:|:-----:|:-----:|-------|
| Local filesystem / NAS                                  |  ✅   |  ✅   |  ✅   | Default |
| SFTP                                                    |  ⚠️   |  ⚠️   |  ✅   | Arq 5/6: backend layer is read-side via local-mount; Arq 7: built-in SFTP backend in validator |
| S3 / Wasabi / B2 / GCS / Azure / OneDrive / Dropbox     |  ❌   |  ❌   |  ❌   | None implemented; user can mount via rclone or use Arq.app's own restore for cloud-only destinations |

### Reverse-engineering tooling

| Capability                                              | Arq 5 | Arq 6 | Arq 7 | Module |
|---------------------------------------------------------|:-----:|:-----:|:-----:|--------|
| Mach-O T-table + numeric-constant scanner               |  ➖   |  ➖   |  ✅   | ``arq_writer.macho_buzhash_finder`` |
| Behavioral chunker-parameter inference                  |  ➖   |  ➖   |  ✅   | ``infer_parameters_from_chunk_sizes`` |
| (min, max) co-located pair-search heuristic             |  ➖   |  ➖   |  ✅   | ``find_min_max_pairs`` + ``arq-buzhash-find pair-search`` |
| Falsification harness (compare our chunks vs. Arq.app)  |  ➖   |  ➖   |  ✅   | ``arq_writer.chunker_oracle`` + ``arq-buzhash-find verify-chunking`` |

## Boundary explanations (why "not implemented" instead of "scoped in")

- **Arq 5/6 writer**: building Arq 5/6 backups would mean re-deriving
  PBKDF2-SHA1 keyset emission + version-gated Tree/Commit/Node
  serialization for ~12 historical versions. The cost is high and
  the audience small (Arq 5 has been EOL since Arq 7's release in
  2021). The reverse direction — restoring an existing 5/6 backup —
  has real users and is implemented.

- **Arq 5/6 validator**: Arq 5/6 destinations are already covered by
  upstream ``arq_restore -v`` (BSD source); replicating a tier-stack
  for them is duplicate work. If a user wants integrity validation
  for an Arq 5/6 destination, ``arq_restore -v`` is the supported
  path; if they want integrity validation for Arq 7, our four-tier
  validator is the most complete option.

- **xattr / ACL / hardlink / resource-fork restoration**: out of
  scope by deliberate cross-platform stance. The metadata is parsed
  and exposed in the FileNode; physically applying it is OS-specific
  (different syscalls on Linux vs. macOS) and the round-trip use
  cases for our writer don't depend on it.

- **Cloud storage backends (S3 / B2 / GCS / Azure / OneDrive /
  Dropbox)**: each backend is a separate auth + transport stack.
  Users can mount any of them via ``rclone`` and point our local
  backend at the mount; pure-Python implementations are deferred
  until a concrete user need surfaces.

- **Arq Cloud Backup**: separate proprietary product with cloud-only
  format and server-side keyset, restored exclusively via Arq.app's
  Cloud Backup pane. No way for an independent client to round-trip
  it without Arq's server cooperation.

- **Incremental backup / commit chain on the writer side**: each
  ``build_backup`` run currently writes a fresh full backuprecord;
  the writer doesn't yet inspect existing commits to chain. The
  backup is still valid (Arq.app accepts standalone ``backuprecord``s
  in a folder) but doesn't dedup against history without manual
  parent-commit wiring. Could be added without spec extension.

## What "covers" actually means here

This project's correctness target is:

1. **Read**: byte-identical reconstruction of any file in any
   commit, given the keyset password + a destination produced by
   the relevant Arq version.
2. **Validate**: detect corruption (bit-flip, truncation, partial
   upload) before restore, without exfiltrating plaintext.
3. **Write**: produce a destination that Arq.app and ``arq_restore``
   both accept and round-trip identically to its input source tree.

A ✅ in the matrix means the corresponding test suite covers the
happy path and the major branches. ⚠️ means the metadata is
parsed/preserved but the physical effect (e.g. emitting a symlink)
is left to the caller. ❌ means it isn't there and a test would
fail (or doesn't exist).

The aggregate test count is **190 unit tests** at the time this
table was last updated; the suite runs in ~35s on a stdlib-only
toolchain (``python -m unittest discover``).
