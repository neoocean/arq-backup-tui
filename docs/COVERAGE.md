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
| Read              | ✅ End-to-end restorer (standalone + packed objects, multi-folder, tree walk, dry-run preview, --paths filter, conflict policies) |
| Validate          | ✅ All four tiers (L0 / L1a / L1b / L2) + resumable audit-drip + per-record walk + incremental ledger across both audit and record tiers |
| Write             | ✅ Standalone-objects mode + optional pack mode; chunker matches Arq.app v7.41; cross-run + cross-folder dedup with bounded LRU tree-walk reuse; walker emits explicit error events on per-file failures (no silent corruption) |
| Operate           | ✅ Schedule (cron + launchd + auto-gc), notifications (notify_run_finished wired to RunWriter), TUI (M1–M6 + maintenance + activity), retention + blob GC, disk-precheck on backup start, macOS progress toasts, .secrets/ wizard checkbox; throttle controllable via audit-drip rate flags |

The aggregate test count is **~810 unit tests** at the time this
table was last updated (Round 6 — PRs #131–#143 landed 13 derived
items spanning A/B/C/D/E/F/G series); the suite runs in
~150 s on a stdlib-only toolchain
(``python -m unittest discover``). TUI tests
(~50 / 355) require the optional ``textual`` dep; without it
they auto-skip and the rest of the suite (library + RE +
compatibility + GUI-parity + Unicode-stress) runs cleanly. **7
tests skip by default** because they require live SFTP credentials
(see ``docs/COMPAT-SFTP-TESTING.md``); operators with a real Arq
destination can run them with a ``.env`` file.

For a structured **Unicode / multi-language / emoji / long-path
audit** of every backup → validate → restore pipeline edge,
see ``docs/UNICODE.md`` and
``tests/test_unicode_path_stress.py``. The fixture generator at
``tests/fixtures_unicode.py`` covers 11 scripts (Latin / Hangul /
CJK / Arabic / Hebrew / Greek / Cyrillic / Thai / Devanagari /
Vietnamese), 8 emoji shapes (single + ZWJ + variation selectors
+ regional indicators + skin tones), 28 special-character
filenames, NFC/NFD normalization preservation, 250-byte filenames,
and 30-level deep paths. Every fixture round-trips byte-for-byte.

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
| Password change / keyset rotation                             |  ✅    | ``arq_writer.rotate_keyset_password(blob, old_password, new_password)``: re-encrypts the keyset under the new password without touching master keys, so existing records stay decryptable |

### 2. Object storage layout

| Arq 7 capability                                              | Status | Notes |
|---------------------------------------------------------------|:------:|-------|
| Computer-tree layout discovery                                |  ✅    | ``arq_validator.layout.discover_layout`` |
| ``standardobjects/<2-hex>/<62-hex>`` standalone blobs         |  ✅    | ``arq_writer.backup``, ``arq_reader.restore`` |
| ``treepacks/`` read + write                                   |  ✅    | ``arq_writer.pack_builder`` (write), ``arq_reader.restore`` (read) |
| ``blobpacks/`` read + write                                   |  ✅    | Same modules |
| ``largeblobpacks/`` read                                      |  ✅    | Reader treats it transparently via ``BlobLoc.relativePath`` |
| ``largeblobpacks/`` write (large-file routing)                |  ✅    | Writer routes blobs whose ARQO bytes exceed ``large_blob_threshold`` (default = ``maxPackedItemLength`` ≈ 256 KiB) to ``largeblobpacks/`` |
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
| Per-folder ``useBuzhash`` toggle                              |  ✅    | ``Backup.add_folder(..., chunker_config=...)`` overrides the constructor-level chunker for one folder; ``Plan.per_source_chunkers`` wires it into the registry |

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
| Plan listing / show / delete                                  |  ✅    | ``arq-tui plans list/show/delete`` headless CLI |
| Plan editing                                                  |  ✅    | TUI: ``[e]`` on a plan row opens PlanWizardScreen pre-populated with the existing plan; saves overwrite the same ``plan_id``. Plan ``last_run_iso`` field stamped automatically on backup finish/fail (PR #38) |
| Folder exclusions (file patterns / glob / regex / .gitignore) |  ✅    | ``ExclusionRules.of(wildcard=..., regex=..., gitignore_lines=...)`` passed via ``Backup(exclusions=...)`` / ``build_backup(..., exclusions=...)``; matched against full POSIX rel_path + basename |
| File-size skip rules                                          |  ✅    | ``Backup(max_file_bytes=...)``; symlinks are exempted (only target-string size, not target file size) |
| ``.gitignore``-style filters                                  |  ✅    | Minimal subset honored: ``# comments``, ``foo``, ``/foo``, ``foo/``, ``*.ext``, ``!negation``. Full ``**`` semantics absent — fall back to ``regex_excludes`` if needed |
| ``excludedDrives`` / ``excludedNetworkInterfaces`` / ``excludedWiFiNetworkNames`` | ⚠️ | Fields emitted as empty arrays; not actually consulted by the writer |
| Plan retention / pruning of old commits                       |  ✅    | ``RetentionPolicy`` (``keep_last_n`` + ``keep_hourly``/``keep_daily``/``keep_weekly``/``keep_monthly``/``keep_yearly``) + ``apply_retention()`` (PR #11). TUI: ``MaintenanceScreen`` (PR #12) reachable via ``[m]`` from the backup-set browser. **Scheduling/automation deferred** — operator runs it on demand |
| Orphan-blob garbage collection (post-prune)                   |  ✅    | ``gc_orphan_blobs()`` walks every retained record's tree, deletes standalone blobs not referenced + packs whose path is referenced by zero ``BlobLoc``. Conservative pack-level (no partial pack rewrite). PR #11 |
| Keyset password rotation                                      |  ✅    | ``rotate_keyset_password()`` re-encrypts ``encryptedkeyset.dat`` while keeping master keys intact, so existing records still decrypt. TUI: ``MaintenanceScreen`` |

#### 5.4 Operational

| Arq 7 capability                                              | Status | Notes |
|---------------------------------------------------------------|:------:|-------|
| Schedule-driven runs (cron-like)                              |  ✅    | ``arq_tui.scheduling`` writes cron / launchd entries for plans; auto-gc schedule (`install_gc_schedule`) bundles ``arq-tui runs gc`` so cron-driven backups don't accumulate state files indefinitely (Group 9 H4) |
| Bandwidth / CPU throttling                                    |  ⚠️    | Audit-drip ``--rate-files-per-min`` throttles validator sweeps (Hetzner-friendly defaults); per-blob backup throttling not exposed yet |
| Pause / resume mid-backup                                     |  ✅    | Cooperative: ``Backup.pause()`` / ``Backup.resume()`` checkpoint at blob boundaries. TUI ``[p]`` toggles state. Subprocess workers forward via SIGUSR1/SIGUSR2 to the writer CLI; both modes share the same Backup-level pause flag (PR #30, Group 3) |
| Wake-from-sleep / sleep-prevention integration                |  🔴   | OS-specific concern |
| Email or system notifications                                 |  ✅    | ``arq_tui.notifications.notify_run_finished`` fires from ``RunWriter.__exit__`` on every run finish; auto-detects macOS osascript / Linux notify-send / operator-supplied shell hook. Defaults filter to status ∈ {failed, cancelled} (PR #36 wire-up of Group 7's F2). ``ARQ_BACKUP_TUI_DISABLE_NOTIFICATIONS=1`` for tests |
| Disk-precheck before backup start                             |  ✅    | ``BackupRunScreen.on_mount`` calls ``estimate_for_plan`` (source bytes vs. destination free space + safety factor) and surfaces a warning notification if undersized — non-fatal so operator can override (PR #36 wire-up of Group 7's F3). ``ARQ_TUI_SKIP_DISK_PRECHECK=1`` opt-out |
| macOS Notification Center toasts (per-milestone progress)     |  ✅    | ``arq_tui.macos_progress`` fires Notification Center toasts at start, every 10% milestone, and at completion. Pure ``osascript`` (no PyObjC); no-op on non-macOS (PR #36 wire-up of Group 7's F5) |
| Activity log / status icons                                   |  ✅    | TUI ``RunsMonitorScreen`` (`[a]ctivity` / `:activity`) passively watches state files written by CLI / cron / TUI processes; ``arq-tui runs ls/show/cancel/gc`` is the headless equivalent |

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
| Restore a specific historical commit (by date / index)        |  ✅    | ``Restore.restore(backuprecord_path=…)`` accepts an explicit record path; ``Restore.list_records`` enumerates available history |
| Restore a single path or pattern                              |  ✅    | ``Restore.restore(paths=[…])`` filters the walk; CLI ``--paths`` (repeatable) |
| Dry-run / list-only restore                                   |  ✅    | ``Restore.dry_run_restore`` walks the tree + emits ``would_restore_file`` events without writing; CLI ``--list-only`` returns a ``DryRunRestoreResult`` JSON summary (PR #38) |
| Conflict policy on existing destination files                 |  ✅    | ``Restore(on_conflict={overwrite,skip,rename})``; CLI ``--on-conflict``. Rename writes to ``name.restored-N``; skip emits ``conflict_skipped`` event |
| Restore-as-mounted-filesystem (FUSE)                          |  ❌    | Out of scope; would require macFUSE / fusepy |
| Browse-without-restore (TUI)                                  |  ✅    | RecordBrowserScreen + BackupSetListScreen in TUI; ``arq-reader list`` for CLI |

#### 6.3 File metadata application

| Arq 7 capability                                              | Status | Notes |
|---------------------------------------------------------------|:------:|-------|
| mtime / ctime preservation on restore                         |  ✅    | Restorer calls ``os.utime`` after each file write |
| Unix mode (perm bits)                                         |  ✅    | Restorer calls ``os.chmod`` with ``S_IMODE(node.mac_st_mode)`` |
| uid / gid preservation                                        |  ✅    | Restorer applies ``os.chown`` from FileNode mac_st_uid/gid (matches uid by name lookup, falls back to numeric); honours ``--no-chown`` |
| Symlinks                                                      |  ✅    | Writer stores link target under ``S_IFLNK``; restorer recreates with ``os.symlink`` |
| Hardlinks                                                     |  ✅    | Writer caches ``(st_dev, st_ino) → FileNode``; subsequent links emit ``file_hardlinked`` event + share the FileNode. Restorer reads ``mac_st_ino`` to reconstruct via ``os.link`` |
| Extended attributes (xattrs)                                  |  ✅    | ``XAttrSetV002`` binary format (RE'd in PR #25); writer emits via ``arq_writer.xattrs.serialize_xattrs``; restorer applies via ``apply_xattrs`` (cross-platform) |
| ACLs (POSIX or NFSv4)                                         |  ✅    | macOS NFSv4 (``ACL_MACOS_NFSV4`` magic header, ``chmod +a`` / ``ls -le``); Linux POSIX (``ACL_LINUX_POSIX`` header, ``setfacl`` / ``getfacl``). Writer captures via ``arq_writer.acl.capture_acl`` per-FileNode + per-TreeNode |
| macOS resource forks                                          |  ❌    | Out of scope (cross-platform stance) |
| macOS Finder metadata (Spotlight comments, color labels, ...) |  ⚠️    | Stored as xattrs (``com.apple.metadata:*``) — round-trips via the xattrs path automatically |
| Windows file attributes                                       |  ⚠️    | ``win_attrs`` field on FileNode is preserved through round-trip but not actively applied on restore (Linux/macOS hosts have no equivalent) |

### 7. Validation

| Arq 7 capability                                              | Status | Notes |
|---------------------------------------------------------------|:------:|-------|
| L0 — directory-layout shape check                             |  ✅    | ``arq_validator.tiers.run_layout_check`` |
| L1a — ARQO magic-byte sample sweep                            |  ✅    | ``arq_validator.tiers.run_magic_check`` |
| L1b — keyset decrypt + latest backuprecord HMAC               |  ✅    | ``arq_validator.tiers.run_backuprecord_check`` |
| L2 — full HMAC sweep over every EncryptedObject               |  ✅    | ``arq_validator.tiers.run_full_audit`` (+ ``ledger=`` for incremental) |
| Per-record blob walk (``arq-validator record``)               |  ✅    | ``arq_validator.record_validator.validate_record`` walks every BlobLoc reachable from one backuprecord; ``--max-blobs`` for CI smoke; ``ledger=`` for incremental |
| Incremental audit ledger (skip already-confirmed blob_ids)   |  ✅    | ``arq_validator.incremental_audit.AuditLedger``; CLI ``--incremental`` + ``--ledger-path`` + ``--ledger-prune-days``. Per-destination JSON under ``~/.local/state/arq-backup-tui/audit-ledgers/``. Failed blobs NEVER ledgered so the next sweep retries them. Used by audit + record tiers (PR #36, #39) |
| Resumable audit-drip (cursor + throttle + state file)         |  ✅    | ``arq_validator.audit_drip`` |
| Pluggable storage backend (read-side)                         |  ✅    | ``arq_validator.backend.Backend`` Protocol |
| Verify a specific historical record                           |  ✅    | ``arq-validator record --record-path <path>`` walks any record's full blob graph |
| Cross-check restored bytes against backup checksum            |  ✅    | ``arq-reader restore --verify-after`` walks restore output + recomputes SHA-256 / blob-id chain |

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
| ``arq-validator`` CLI (run validation tiers)                  |  ✅    | ``arq_validator.cli``. Flags include ``--incremental`` + ``--ledger-path`` + ``--ledger-prune-days N`` (PR #36, #39) — incremental sweeps share a per-destination ledger of confirmed blob_ids |
| ``arq-backup`` CLI (one-shot backup)                          |  ✅    | ``arq_writer.cli``. Flags: ``--use-packs`` / ``--chunker {none,default,arq_v7_41,fixed-40m}`` (fixed-40m matches Arq.app v8's ``useBuzhash: False`` plans byte-for-byte, PR #58) / ``--tree-version {3,4}`` (v4 emits the 38-byte per-Node trailing block Arq.app v8 carries; v3 stays restorable by the published ``arq_restore`` BSD reference, PR #56) / ``--dedup-against-existing`` / ``--max-file-bytes`` / ``--exclude-glob`` / ``--exclude-regex`` / ``--exclude-from`` / ``--use-apfs-snapshot`` / ``--state-file`` (state-file IPC for TUI / cron monitoring) / ``--debug [SUBSYSTEMS]`` |
| ``arq-reader`` CLI (one-shot restore + listing)               |  ✅    | ``arq_reader.cli``; ``--state-file`` for restore-side monitoring; ``--list-only`` for dry-run preview without writing; ``--paths`` (repeatable) for selective restore; ``--on-conflict {overwrite,skip,rename}``; ``--verify-after`` for post-restore SHA-256 walk |
| ``arq-buzhash-find`` CLI (RE toolkit subcommands)             |  ✅    | ``arq_writer.buzhash_re_cli`` |
| ``arq-tui machine-info <root>``                               |  ✅    | Source-machine identification: print backupconfig.json/backupplan.json metadata + compare against current host (hostname / scutil / sw_vers). JSON output |
| ``arq-tui runs ls/show/cancel/gc``                            |  ✅    | Headless equivalent of the Activity monitor screen — list active+recent runs, send SIGTERM, GC old terminal records |
| TUI (interactive frontend)                                    |  ✅    | Full M1–M6 stack landed plus the slide-down quake-style command console (slash-commands), MaintenanceScreen, **and the new RunsMonitorScreen** ([a]ctivity / `:activity`) — passively watches state files written by CLI / cron processes. Plan editing on [e]. Launchers: ``arq-tui``, ``python -m arq_tui``, or root ``./arq-tui.py`` |
| Progress callback hooks (suitable for any frontend)           |  ✅    | All three components emit ``ProgressCb(kind, payload)`` events |
| State-file IPC (``arq_tui.runs``)                             |  ✅    | Atomic-write JSON state files under ``$XDG_STATE_HOME/arq-backup-tui/runs/``; producer side ``RunWriter`` context manager, consumer side ``enumerate_runs`` + ``mark_stale`` + ``signal_cancel`` + ``gc_finished_runs``. Schema in ``docs/PLAN-cli-tui-split.md`` |
| Real-Arq.app SFTP destination compat tests                    |  ✅    | ``tests/integration/test_arqapp_sftp_compat.py`` + ``test_arq_real_destination.py`` + ``test_arq_real_destination_deep.py``. Triggered the discoveries documented in ``docs/REAL-DATA-DISCOVERIES.md`` (Hetzner SFTP-only compat, JSON backuprecord, BlobLoc isLargePack, Node userName/groupName, Tree v4 trailing block) |

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

- **Sparse files (filesystem holes)**: content correctness ✅ —
  bytes round-trip identically including the zero-hole regions
  (covered by ``tests/test_sparse_files.py``). Sparseness
  preservation on restore ❌ — the restorer writes zeros
  explicitly rather than re-creating filesystem holes. A 2 GB
  sparse file with 8 KB of actual data restores as 2 GB on disk.
  Destination-side storage is **not** affected: Buzhash chunker
  + content-addressed dedup mean the destination footprint
  scales with unique content, not logical size. (See E1 entry
  in ``HANDOFF.md``.)

- **Schedule / throttling / notifications / wake-from-sleep**:
  most of these are now **implemented** as of PRs #29–#36. Schedule
  via ``arq_tui.scheduling`` (cron + launchd + auto-gc); notifications
  via ``arq_tui.notifications`` (osascript / notify-send / shell hook);
  audit-side throttle via ``--rate-files-per-min``. Per-blob backup
  throttling and wake-from-sleep integration remain out: both belong
  closer to OS-policy than to backup-format code.

- **Folder exclusions / size limits / ``.gitignore`` rules**:
  **implemented** (PR #10) via ``ExclusionRules.of(wildcard=,
  regex=, gitignore_lines=)``; CLI ``--exclude-glob`` /
  ``--exclude-regex`` / ``--exclude-from``; TUI plan wizard's
  Advanced step exposes all three.

- **Incremental backup**: cross-run dedup + tree-walk reuse
  cover the meaningful incremental case (no re-encryption +
  no re-read of unchanged content). **Pause / resume mid-backup
  is now implemented** (PR #30, Group 3): cooperative checkpointing
  at blob boundaries via ``Backup.pause()`` / ``resume()``; TUI ``[p]``
  toggles state; subprocess workers forward via SIGUSR1/SIGUSR2.
  A SIGKILL mid-write still leaves the destination valid (partial
  packs have a flush boundary at their last ``add()``) but the
  current backuprecord wasn't emitted.

- **Restore selectivity (single path / single historical
  commit)**: **fully implemented** (PRs #15, #38). ``Restore.restore``
  accepts ``backuprecord_path=`` for historical records and
  ``paths=[…]`` for path filtering; CLI mirrors as ``--paths``
  (repeatable) + the new ``--list-only`` dry-run preview.

- **TUI**: **shipped** (M1–M6 + maintenance + activity + plan-edit + console).
  Reachable via ``./arq-tui.py`` or ``python -m arq_tui``. Backup /
  restore / browse / validate / scheduling / maintenance / plan-edit
  all integrated. Sidebar with section_for_screen() routing keeps
  active highlight in lockstep with current screen. The library APIs
  (``ProgressCb`` callbacks, ``Restore`` / ``Backup`` classes,
  validator's tiered events) underpin the TUI without TUI-specific
  coupling.

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

## Round 6 derived-items batch (2026-05-11)

Round 6 derived **33 additional compatibility items** from
focused review of the writer / reader / validator triple after
Round 5's schema-completeness work landed. The 33 items were
addressed in 13 PRs (one logical group each):

| PR | Items | Subject |
|---:|---|---|
| #131 | A11 | JSON field ordering (NSDictionary byte-diff documented) |
| #132 | D9 | JSON encoding edge cases (non-ASCII, slash escape, separators) |
| #133 | C9 | stretchEncryptionKey per-blob flag handling |
| #134 | C6 | Tree deep-recursion safety (100-150 levels) |
| #135 | A13 | Mixed v100+v101 destination handling |
| #136 | B6+B7 | Mid-walk file mutation handling |
| #137 | A12+A14+A15 | BackupRecord edge fields (archived, OS type, empty source) |
| #138 | C7+C8+C10 | Reader defensive handling of malformed BlobLoc / packs |
| #139 | B8+B9+B10 | Walker races against a moving source tree |
| #140 | D6+D7+D8+D10 | Value-level config + plist↔JSON round-trip |
| #141 | F4+F5+F6+F7 | xattr / ACL / FinderInfo edge cases |
| #142 | G3+G4+G5 | Chunker boundary cases |
| #143 | E7+E9+E11+E12 | Operational CLI / restore / retention / gc edges |

Aggregate: **~95 new unit tests** pinning previously-unverified
edge cases. Two items deferred:

- **E8** (`arq-validator CLI tier × backend` matrix) — needs
  SFTP deployment infrastructure to test multi-tier × multi-
  backend combinations end-to-end. The matrix is already
  exercised at the Python API level by existing
  `tests/test_arq7_compatibility.py` (15 tests) + the SFTP
  backend smoke tests; an explicit CLI-level cross product
  would be redundant without an SFTP fixture.
- **E10** (`audit-drip resume mid-walk`) — needs a long-running
  audit fixture deployed in CI for deterministic checkpoint /
  resume coverage. The drip helper's checkpoint round-trip is
  already covered at the unit-test layer; a true mid-walk
  resume against a 500GB+ destination is operator-facing and
  would need real-world deployment to verify.

After Round 6's landing, **no derived compatibility items remain
unaddressed** at the format / behaviour layer. Future work
focuses on infrastructure (SFTP CI fixtures, deployment) rather
than format correctness.
