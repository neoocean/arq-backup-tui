# Coverage matrix vs. Arq Backup 7

This document records, feature by feature, how this project's
``arq_validator`` / ``arq_reader`` / ``arq_writer`` triple covers
Arq Backup 7. It is the single-page answer to "what can this
project do with an Arq 7 destination?"

The matrix is not a roadmap — items marked **out of scope** are
deliberate trade-offs (Arq.app side concern, redundant with
``arq_restore``, or the format isn't published). Items marked
**partial** are honest about which sub-features are missing.

## Headline status

| Area              | Status                                                  |
|-------------------|---------------------------------------------------------|
| Read              | ✅ End-to-end restorer (standalone + packed objects, multi-folder, tree walk) |
| Validate          | ✅ All four tiers (L0 / L1a / L1b / L2) + resumable audit-drip |
| Write             | ✅ Standalone-objects mode + optional pack mode; chunker matches Arq.app v7.41; cross-run + cross-folder dedup with tree-walk reuse |
| Operate           | ⚠️ Library only — schedule, throttling, notifications, GUI/TUI all absent |

The aggregate test count is **265 unit tests** at the time this
table was last updated; the suite runs in ~92 s on a stdlib-only
toolchain (``python -m unittest discover``). 24 of those are TUI
tests that require the optional ``textual`` dep — without it they
auto-skip and the rest of the suite (library + RE +
compatibility) runs cleanly.

For a structured **format-conformance audit** of any destination
this project produces (or any Arq 7 destination, regardless of
origin), see ``docs/COMPATIBILITY.md`` and
``arq_validator.check_arq7_compatibility``. The audit cross-checks
every invariant in the published Arq 7 spec — layout shape, JSON
sidecar fields, keyset format, ARQO envelope, blob_id derivation,
pack-file naming, backuprecord plist keys, etc. — and returns a
structured pass/fail report. 15 dedicated tests in
``tests/test_arq7_compatibility.py`` exercise the audit across
every backup scenario (standalone vs packed, single file, empty
tree, multi-folder, Unicode filenames, multi-MiB chunked file)
plus 5 negative tests that intentionally damage a correct
destination and assert the right invariant fires.

## Detailed feature matrix

Legend: ✅ implemented + tested · ⚠️ partial · ❌ not implemented ·
🔴 out of scope.

### 1. Crypto + container format

| Arq 7 capability                                              | Status | Notes |
|---------------------------------------------------------------|:------:|-------|
| EncryptedObject (ARQO) decrypt + HMAC-SHA256 verify           |  ✅    | ``arq_validator.crypto``, ``arq_reader.decrypt`` |
| EncryptedObject (ARQO) encrypt (write path)                   |  ✅    | ``arq_writer.crypto_write`` |
| ``encryptedkeyset.dat`` (PBKDF2-SHA256, AES-256-CBC) decrypt  |  ✅    | ``arq_validator.crypto.decrypt_keyset`` |
| ``encryptedkeyset.dat`` build (write path)                    |  ✅    | ``arq_writer.crypto_write.build_encrypted_keyset`` |
| AES-256-CBC + PKCS7 (via host ``openssl`` subprocess)         |  ✅    | ``arq_writer.crypto_write.aes_256_cbc_encrypt`` |
| LZ4 block compression (compress + decompress)                 |  ✅    | ``arq_writer.lz4_block`` |
| ``stretchEncryptionKey`` per-blob flag                        |  ✅    | Honored on both read and write paths |
| Unencrypted backups (``isEncrypted: false``)                  |  ❌    | Writer always emits encrypted backups; reader hard-codes ARQO magic check before decrypt — would need a small change to read genuinely unencrypted destinations |
| Password change / keyset rotation                             |  ❌    | No tooling to re-encrypt the keyset under a new password |

### 2. Object storage layout

| Arq 7 capability                                              | Status | Notes |
|---------------------------------------------------------------|:------:|-------|
| Computer-tree layout discovery                                |  ✅    | ``arq_validator.layout.discover_layout`` |
| ``standardobjects/<2-hex>/<62-hex>`` standalone blobs         |  ✅    | ``arq_writer.backup``, ``arq_reader.restore`` |
| ``treepacks/`` read + write                                   |  ✅    | ``arq_writer.pack_builder`` (write), ``arq_reader.restore`` (read) |
| ``blobpacks/`` read + write                                   |  ✅    | Same modules |
| ``largeblobpacks/`` read                                      |  ✅    | Reader treats it transparently via ``BlobLoc.relativePath`` |
| ``largeblobpacks/`` write (large-file routing)                |  ❌    | Writer puts every non-tree blob into ``blobpacks/`` regardless of size; ``maxPackedItemLength`` (~256 KiB) routing not implemented |
| ``backupfolders/<folder>/backuprecords/<bucket>/<num>``       |  ✅    | Both directions |
| Multi-folder per computer                                     |  ✅    | ``Backup.add_folder`` can be called multiple times in one run |
| Multi-computer per destination                                |  ⚠️    | Reader auto-discovers multiple computers; writer always writes to a fresh or single computer UUID per ``Backup`` instance |

### 3. Tree / Node / Commit binary format

| Arq 7 capability                                              | Status | Notes |
|---------------------------------------------------------------|:------:|-------|
| ``Tree`` / ``Node`` / ``BlobLoc`` parse                       |  ✅    | ``arq_reader.parse`` |
| ``Tree`` / ``Node`` / ``BlobLoc`` write                       |  ✅    | ``arq_writer.serialize`` |
| ``backuprecord`` (binary plist) parse                         |  ✅    | ``arq_reader.parse``, ``arq_validator.tiers`` |
| ``backuprecord`` write                                        |  ✅    | ``arq_writer.backuprecord`` |
| JSON sidecars (``backupconfig`` / ``backupplan`` / ``backupfolder`` / ``backupfolders``) |  ✅    | ``arq_writer.json_configs`` |
| Glacier metadata fields (``s3GlacierObjectDirs``, ``containsGlacierArchives``, ``isWORM``) | ⚠️ | Fields are emitted as the spec-required defaults (empty / False); not honored for actual S3 Glacier tiering |

### 4. Chunker (``chunkerVersion: 3 + useBuzhash``)

| Arq 7 capability                                              | Status | Notes |
|---------------------------------------------------------------|:------:|-------|
| Generic Buzhash content-defined chunker                       |  ✅    | ``arq_writer.chunker.Buzhash`` |
| Arq.app v7.41 exact chunker parameters                        |  ✅    | ``arq_writer.arq_chunker_params`` (T table + window=256 + boundary_bits=16 + max=128 KiB; min=4 KiB at low confidence — see §4.2 of RESEARCH-format-extensions.md) |
| Mach-O T-table + numeric-constant scanner (RE)                |  ✅    | ``arq_writer.macho_buzhash_finder`` |
| Behavioral chunker-parameter inference from chunk-size dist   |  ✅    | ``infer_parameters_from_chunk_sizes`` |
| (min, max) co-located pair-search heuristic                   |  ✅    | ``find_min_max_pairs`` + ``arq-buzhash-find pair-search`` |
| Falsification harness (compare our chunks vs. Arq.app)        |  ✅    | ``arq_writer.chunker_oracle`` + ``arq-buzhash-find verify-chunking`` |
| Per-folder ``useBuzhash`` toggle                              |  ⚠️    | Spec exposes the flag per folder; our writer applies one ``chunker_config`` to the whole ``Backup`` |

### 5. Backup (write path)

#### 5.1 Core write

| Arq 7 capability                                              | Status | Notes |
|---------------------------------------------------------------|:------:|-------|
| Source-tree → backup destination                              |  ✅    | ``arq_writer.backup.build_backup`` |
| Standalone-objects emission                                   |  ✅    | Default mode |
| Packed emission (treepacks + blobpacks)                       |  ✅    | ``Backup(use_packs=True)`` |
| Buzhash chunking (generic params)                             |  ✅    | ``build_backup(..., chunker_config=ChunkerConfig(...))`` |
| Buzhash chunking (Arq.app v7.41 params)                       |  ✅    | opt-in via ``import arq_writer.arq_chunker_params`` |

#### 5.2 Incremental / dedup

| Arq 7 capability                                              | Status | Notes |
|---------------------------------------------------------------|:------:|-------|
| Within-run dedup (identical SHA-256 blobs share one BlobLoc)  |  ✅    | ``Backup._written_blobs`` cache; standalone + packed |
| Cross-run dedup against an existing destination               |  ✅    | ``build_backup(..., dedup_against_existing=True)`` reuses the keyset and seeds the cache from ``standardobjects/`` + every folder's most recent backuprecord (recursive tree walk for packed coverage); see ``arq_writer.dedup`` |
| Multi-folder dedup (computer-scoped blob storage)             |  ✅    | Both within-run and cross-run honor the shared ``standardobjects/`` / ``treepacks/`` / ``blobpacks/`` tree — adding a new folder reuses blobs already written by any sibling folder. Matches Arq 7's actual storage model |
| Tree-walk reuse (skip read+chunk on unchanged files)          |  ✅    | ``arq_writer.prior_tree.PriorTreeIndex`` lazily walks the prior tree and reuses any FileNode whose ``stat`` triple (mtime, size, mode) still matches; tracked via ``Backup.files_reused`` and the ``file_reused`` callback event |
| Implicit chronological ordering of records                    |  ✅    | Arq 7 has no parent-commit field; chronologically newer records under ``backuprecords/<bucket>/<num>`` are implicitly children of older ones |

#### 5.3 Backup planning / configuration

| Arq 7 capability                                              | Status | Notes |
|---------------------------------------------------------------|:------:|-------|
| Plan creation (``backupplan.json``)                           |  ✅    | ``arq_writer.json_configs.build_backupplan`` |
| Plan listing / editing                                        |  ❌    | No high-level API to add/remove folders from an existing plan |
| Folder exclusions (file patterns / glob / regex)              |  ❌    | Writer walks the full source tree without exclusion rules |
| File-size skip rules                                          |  ❌    | All files are backed up regardless of size |
| ``.gitignore``-style filters                                  |  ❌    | Not honored |
| ``excludedDrives`` / ``excludedNetworkInterfaces`` / ``excludedWiFiNetworkNames`` | ⚠️ | Fields emitted as empty arrays; not actually consulted by the writer |
| Plan retention / pruning of old commits                       |  ❌    | No prune tooling — destinations grow unboundedly |

#### 5.4 Operational

| Arq 7 capability                                              | Status | Notes |
|---------------------------------------------------------------|:------:|-------|
| Schedule-driven runs (cron-like)                              |  🔴   | Out of scope: lives in Arq.app's policy layer, not the on-disk format |
| Bandwidth / CPU throttling                                    |  🔴   | Same |
| Pause / resume mid-backup                                     |  ❌    | No checkpoint mechanism; a kill mid-run is unsafe |
| Wake-from-sleep / sleep-prevention integration                |  🔴   | OS-specific concern |
| Email or system notifications                                 |  🔴   | App-layer concern |
| Activity log / status icons                                   |  🔴   | App-layer concern |

### 6. Restore (read path)

#### 6.1 Core restore

| Arq 7 capability                                              | Status | Notes |
|---------------------------------------------------------------|:------:|-------|
| Walk computer / folder list                                   |  ✅    | ``arq_reader.restore.Restore.list_folders`` |
| ``master`` ref → backuprecord → tree traversal                |  ✅    | ``arq_reader.restore`` |
| File restore (regular files)                                  |  ✅    | |
| Pack-stored blob retrieval (``isPacked: true``)               |  ✅    | Range read into the pack file; pack header / index not consulted |
| Multi-folder restore from one keyset                          |  ✅    | |

#### 6.2 Selective / historical

| Arq 7 capability                                              | Status | Notes |
|---------------------------------------------------------------|:------:|-------|
| Restore a specific historical commit (by date / index)        |  ❌    | ``Restore.restore`` always selects the latest backuprecord for the folder |
| Restore a single path or pattern                              |  ❌    | Whole-folder restore only |
| Restore-as-mounted-filesystem (FUSE)                          |  ❌    | Out of scope; would require macFUSE / fusepy |
| Browse-without-restore (TUI / GUI)                            |  ❌    | TUI not implemented; ``arq-reader list`` exists for CLI listing |

#### 6.3 File metadata application

| Arq 7 capability                                              | Status | Notes |
|---------------------------------------------------------------|:------:|-------|
| mtime / ctime preservation on restore                         |  ⚠️    | Times are stored in the FileNode but the restorer doesn't currently call ``utime``; restored files take "now" mtime |
| Unix mode (perm bits)                                         |  ⚠️    | Mode stored; restorer honors it for new files but doesn't ``chmod`` after the fact |
| Symlinks                                                      |  ❌    | Source-side: not followed (good); restore-side: not physically emitted as symlinks |
| Hardlinks                                                     |  ❌    | Each file treated as a separate blob; matches Arq.app's behavior |
| Extended attributes (xattrs)                                  |  ❌    | Parsed and exposed in ``Node`` but not applied to restored files |
| ACLs (POSIX or NFSv4)                                         |  ❌    | Same |
| macOS resource forks                                          |  ❌    | Out of scope (cross-platform stance) |
| macOS Finder metadata (Spotlight comments, color labels, ...) |  ❌    | Out of scope |
| Windows file attributes                                       |  ❌    | ``win_attrs`` field exists in ``FileNode``; not honored on restore |

### 7. Validation

| Arq 7 capability                                              | Status | Notes |
|---------------------------------------------------------------|:------:|-------|
| L0 — directory-layout shape check                             |  ✅    | ``arq_validator.tiers.run_l0`` |
| L1a — ARQO magic-byte sample sweep                            |  ✅    | ``arq_validator.tiers.run_l1a`` |
| L1b — keyset decrypt + latest backuprecord HMAC               |  ✅    | ``arq_validator.tiers.run_l1b`` |
| L2 — full HMAC sweep over every EncryptedObject               |  ✅    | ``arq_validator.tiers.run_l2`` |
| Resumable audit-drip (cursor + throttle + state file)         |  ✅    | ``arq_validator.audit_drip`` |
| Pluggable storage backend (read-side)                         |  ✅    | ``arq_validator.backend.Backend`` Protocol |
| Verify a specific historical record                           |  ⚠️    | L1b verifies the latest record per folder; pointing at an older one needs manual code |
| Cross-check restored bytes against backup checksum            |  🔴   | Validator and restorer are separate paths; round-trip tests cover this in CI |

### 8. Storage backends (where the destination lives)

**Project scope**: Local filesystem, NAS (treated as local), and SFTP.
Every other Arq 7 backend (S3, Wasabi, B2, Storj, GCS, Azure Blob,
OneDrive, Dropbox, Box, Google Drive, pCloud, …) is **out of scope**
— independent of how Arq.app supports them. Users who need a cloud
destination can either use Arq.app for that backup or expose the
destination through ``rclone mount`` and point our local backend at
the FUSE mount.

| Backend                                                       | Validator | Reader | Writer | Notes |
|---------------------------------------------------------------|:---------:|:------:|:------:|-------|
| Local filesystem                                              |    ✅     |  ✅    |  ✅    | Default; ``LocalBackend`` |
| NAS (any local-mounted network filesystem)                    |    ✅     |  ✅    |  ✅    | Indistinguishable from local; same ``LocalBackend`` path |
| SFTP                                                          |    ✅     |  ✅    |  ✅    | ``SftpBackend`` (extended with ``mkdir`` + ``write_all``) is injectable into both ``Restore(..., backend=...)`` and ``Backup(..., backend=...)``. All writer I/O — keyset, JSON sidecars, standalone blobs, pack files, backuprecords — routes through the backend. Cross-run dedup (``standardobjects/`` scan + per-folder backuprecord recursive walk + ``PriorTreeIndex``) is also backend-aware |
| S3 (any class) / Wasabi / Backblaze B2 / Storj / Google Cloud / Azure Blob / OneDrive / Dropbox / Box / Google Drive / pCloud | 🔴 | 🔴 | 🔴 | **Out of scope.** Native cloud-API clients are not part of this project's goals. Arq.app is the supported tool for cloud destinations |
| Any cloud backend via ``rclone mount``                        |    ✅     |  ✅    |  ✅    | Workaround, not a built-in feature: a FUSE mount makes the cloud destination look local to ``LocalBackend`` |

### 9. CLI / TUI

| Component                                                     | Status | Notes |
|---------------------------------------------------------------|:------:|-------|
| ``arq-validator`` CLI (run validation tiers)                  |  ✅    | ``arq_validator.cli`` |
| ``arq-backup`` CLI (one-shot backup)                          |  ✅    | ``arq_writer.cli`` |
| ``arq-reader`` CLI (one-shot restore + listing)               |  ✅    | ``arq_reader.cli`` |
| ``arq-buzhash-find`` CLI (RE toolkit subcommands)             |  ✅    | ``arq_writer.buzhash_re_cli`` |
| TUI (interactive frontend)                                    |  ✅    | Full M1–M6 stack landed: ``arq_tui`` package + Textual ``ArqTuiApp``. Screens: Home (plan list + quick actions) / PlanWizard (multi-source create) / BackupSetList (local + SFTP browser) / RecordBrowser (lazy tree walk + per-file metadata + mark/restore) / BackupRun + RestoreRun + ValidateRun (live progress via the ``WorkerEvent`` bridge). Activated via ``pip install -e ".[tui]"`` then ``arq-tui`` |
| Progress callback hooks (suitable for any frontend)           |  ✅    | All three components emit ``ProgressCb(kind, payload)`` events |

### 10. Reverse-engineering tooling

| Capability                                                    | Status | Notes |
|---------------------------------------------------------------|:------:|-------|
| Mach-O T-table + numeric-constant scanner                     |  ✅    | ``arq_writer.macho_buzhash_finder`` |
| Behavioral chunker-parameter inference                        |  ✅    | ``infer_parameters_from_chunk_sizes`` |
| (min, max) co-located pair-search heuristic                   |  ✅    | ``find_min_max_pairs`` |
| Falsification harness for chunker parameters                  |  ✅    | ``arq_writer.chunker_oracle`` |

## Boundary explanations (why some "❌"s are intentional)

- **xattrs / ACLs / hardlinks / resource forks / Finder
  metadata**: cross-platform stance. The metadata is parsed and
  exposed in the Node where Arq itself stores it; physically
  applying it is OS-specific. The writer's round-trip use cases
  don't depend on any of these, and our reader's primary
  consumer (a TUI) gets full visibility through the parsed data.

- **mtime / mode preservation on restore**: planned but currently
  the restorer creates files with default mtime/perm. The data
  to restore them is in every FileNode; only the ``utime`` /
  ``chmod`` calls are missing. Marked ⚠️ rather than ❌ because
  the restored bytes are correct; only the metadata around them
  needs a follow-up commit.

- **Cloud storage backends (S3 / Wasabi / B2 / Storj / GCS /
  Azure / OneDrive / Dropbox / Box / Google Drive / pCloud)**:
  **deliberately out of scope.** This project's storage targets
  are local filesystem, NAS, and SFTP — Arq.app is the tool for
  cloud destinations, both because each backend is a separate
  auth + transport stack and because the project's audience
  (independent validation + restore + write of self-hosted
  destinations) doesn't overlap meaningfully with cloud-only
  users. The ``rclone mount`` workaround stays available for
  anyone who wants a cloud target anyway.

- **Schedule / throttling / notifications / wake-from-sleep**:
  these belong to Arq.app's policy layer, not the on-disk format.
  A standalone scheduler can wrap ``arq-backup`` if needed; the
  format itself is unaffected.

- **Folder exclusions / size limits / ``.gitignore`` rules**:
  the writer's ``Backup.add_folder`` walks the full source tree
  unconditionally. A pre-filter pass (or an exclude argument to
  ``add_folder``) would be the natural extension; not yet
  implemented.

- **Incremental backup**: cross-run dedup + tree-walk reuse
  cover the meaningful incremental case (no re-encryption +
  no re-read of unchanged content). Pause / resume mid-backup
  is the remaining gap; a kill mid-write leaves the destination
  in a state that is still valid — partial pack files have a
  flush boundary at their last successful ``add()`` — but the
  current backuprecord wasn't yet emitted.

- **Restore selectivity (single path / single historical
  commit)**: out today; the ``Restore.restore`` API hard-codes
  "latest record, full folder". Both extensions would slot into
  the existing API without format changes — pass an explicit
  ``backuprecord_path`` instead of always finding the latest;
  filter the tree walk by a path prefix.

- **TUI**: described in DESIGN.md as the project's eventual
  primary interface. Not yet started; the library APIs
  (``ProgressCb`` callbacks, the ``Restore`` / ``Backup`` classes,
  the validator's tiered events) are designed to be embedded in
  one without changes.

- **``largeblobpacks/`` write routing**: the spec ships a
  ``maxPackedItemLength`` setting (default ~256 KiB) that
  governs whether a blob lives in ``blobpacks/`` or
  ``largeblobpacks/``. Our writer puts every non-tree blob into
  ``blobpacks/`` regardless of size. The destination is still
  valid (the reader resolves both), only on-disk pack-file size
  distribution differs from Arq.app's.

- **Unencrypted backups**: Arq 7's spec allows
  ``isEncrypted: false``. Our writer always emits encrypted
  destinations; our reader checks for the ARQO magic before
  attempting decrypt and would fall through to "raw bytes are
  the plaintext" for an unencrypted destination, but this code
  path isn't tested. The encrypted case has 200+ tests behind it.

## What "covers" actually means here

This project's correctness target for Arq 7 is:

1. **Read**: byte-identical reconstruction of any file in any
   commit, given the keyset password + an Arq 7 destination.
2. **Validate**: detect corruption (bit-flip, truncation, partial
   upload) before restore, without exfiltrating plaintext.
3. **Write**: produce a destination that Arq.app and ``arq_restore``
   both accept and round-trip identically to its input source tree,
   with correct cross-run + cross-folder deduplication.

A ✅ in the matrix means the corresponding test suite covers the
happy path and the major branches. ⚠️ means the on-disk metadata
is preserved but the runtime effect (e.g. emitting a symlink)
is missing. ❌ means it isn't there and a test would fail (or
doesn't exist). 🔴 means it lives outside the on-disk format
spec entirely — Arq.app concern, not ours.
