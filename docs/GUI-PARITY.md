# Arq 7 GUI Feature Parity

This document catalogs the full feature set of the **Arq 7 desktop app (GUI)**
and indicates the implementation status in the current codebase. For unimplemented
items that fall within the project scope, priority and implementation notes
are recorded alongside.

Scope decisions (cumulative from prior decisions):

- **Local / NAS / SFTP storage only**: cloud backends (S3 / Wasabi
  / B2 / Dropbox / OneDrive / Box / GCS / Azure / Google Drive /
  pCloud …) are all 🔴 **out of scope**
- **Backup host**: policy-layer features (schedule / throttling /
  notifications / menu bar / wake-from-sleep) are 🔴 **out of scope**
- **TUI English-only**, backed-up file/path names are unicode-safe
- **Plan editing UI** is deferred to v1.x (recreate is recommended)
- **xattrs / ACLs / resource forks / Finder metadata**: 🔴 out of scope
  by cross-platform stance

## 1. Headline Parity Table

| Area | Arq 7 GUI feature | Current status | Notes |
|------|----------------|:--------:|------|
| Backup plan | Create | ✅ | TUI Wizard (M3) |
| Backup plan | Edit | ❌ | Deferred to v1.x (recreate recommended) |
| Backup plan | Delete | ✅ | `arq-tui plans delete <id-or-name>` |
| Backup plan | List | ✅ | TUI Home + `PlanRegistry.list_plans` |
| Backup plan | Multiple plans | ✅ | Multiple plans on a single computer |
| Source selection | GUI folder picker | ✅ | TUI SourcePicker |
| Source selection | Multiple sources | ✅ | Supported since M3 |
| Source selection | File size limit | ✅ | `Backup(max_file_bytes=...)` |
| Source selection | Wildcard exclusion | ✅ | `ExclusionRules.of(wildcard=...)` |
| Source selection | Regex exclusion | ✅ | `ExclusionRules.of(regex=...)` |
| Source selection | .gitignore compatibility | ✅ | `ExclusionRules.of(gitignore_lines=...)` partial implementation |
| Source selection | Drive / WiFi exclusion | 🔴 | Policy layer |
| Backup execution | Manual run | ✅ | CLI / TUI |
| Backup execution | Scheduled run | 🔴 | External cron recommended |
| Backup execution | Pause / resume | ✅ | Cooperative checkpointing at blob boundaries (PR #30); subprocess workers forward via SIGUSR1/SIGUSR2. TUI `[p]` toggles state. |
| Backup execution | Cooperative cancel | ✅ | `Backup.cancel()` |
| Backup execution | CPU throttling | 🔴 | Policy layer |
| Backup execution | Network throttling | ⚠️ | Only audit-drip can be throttled |
| Backup execution | Battery awareness | 🔴 | Policy layer |
| Backup execution | wake-from-sleep | 🔴 | OS level |
| Encryption | Initial password | ✅ | Wizard step 3 |
| Encryption | Password change | ✅ | `arq_writer.rotate_keyset_password(blob, old_pw, new_pw)` |
| Encryption | Password recovery | 🔴 | Arq Cloud only |
| Storage | Local / NAS | ✅ | LocalBackend |
| Storage | SFTP | ✅ | SftpBackend (read+write) |
| Storage | Cloud (S3 / B2 / GCS / Azure …) | 🔴 | Out of scope; rclone mount workaround |
| Storage | Multiple destinations | ❌ | v1.x — one plan = one destination |
| Chunker | Generic Buzhash | ✅ | `arq_writer.chunker` |
| Chunker | Arq.app v7.41 matching | ✅ | `arq_writer.arq_chunker_params` |
| Chunker | Per-folder useBuzhash toggle | ✅ | `Backup.add_folder(..., chunker_config=...)` + `Plan.per_source_chunkers` |
| Object storage | standardobjects/ | ✅ | Default mode |
| Object storage | treepacks / blobpacks | ✅ | `use_packs=True` |
| Object storage | largeblobpacks read | ✅ | Read works |
| Object storage | largeblobpacks **write** | ✅ | `Backup(large_blob_threshold=...)` automatic routing |
| Restore | Full restore | ✅ | RestoreRunScreen |
| Restore | Selective path restore | ✅ | `paths=[...]` |
| Restore | Point-in-time selection (historical) | ✅ | `backuprecord_path=...` |
| Restore | Restore to alternate location | ✅ | `dest=...` |
| Restore | Preserve mtime | ✅ | `Restore._restore_file_node` calls utime |
| Restore | Preserve mode (perm) | ✅ | `os.chmod(out_path, S_IMODE(node.mac_st_mode))` |
| Restore | Preserve uid / gid | ❌ | Cross-platform; outside-UI policy |
| Restore | Physical symlink creation | ✅ | Writer stores link target + S_IFLNK; restorer calls `os.symlink` |
| Restore | Hardlink detection | ❌ | Arq.app also handles them as separate files |
| Restore | xattr / ACL | 🔴 | Cross-platform stance |
| Restore | Resource forks / Finder | 🔴 | Cross-platform stance |
| Restore | FUSE mount | 🔴 | Cross-platform; rclone recommended |
| Restore | Quick Look preview | 🔴 | macOS-only |
| Restore | Diff between snapshots | ❌ | v1.x |
| Validation | Manual verification | ✅ | 4-tier + audit-drip |
| Validation | Automatic (monthly) verification | 🔴 | External cron recommended |
| Validation | Format conformance | ✅ | `check_arq7_compatibility` |
| Monitoring | Activity log | ⚠️ | Callback events exposed, no GUI viewer |
| Monitoring | Email reports | 🔴 | Policy layer |
| Monitoring | System notifications | 🔴 | OS-specific |
| Monitoring | Menu bar / system tray | 🔴 | OS-specific |
| Retention | hourly / daily / monthly policy | ✅ | `RetentionPolicy(keep_last_n=, keep_hourly=, keep_daily=, keep_weekly=, keep_monthly=, keep_yearly=)` (PR #11). TUI: `MaintenanceScreen` (`[m]`, PR #12). Use external cron for automatic scheduling |
| Retention | Manual deletion of old commits | ✅ | `prune_records(backend, encryption_password=..., policy=...)` (PR #11). Supports dry-run + callback events |
| Retention | blob GC / vacuum | ✅ | `gc_orphan_blobs()` conservative pack-level (PR #11). Only deletes packs in which all blobs are orphans — no partial rewrites |
| Multi-computer | Sharing one destination | ⚠️ | Reader auto-discovers; writer is single |
| Multi-computer | Per-computer keyset | ✅ | Each `<CU>/encryptedkeyset.dat` is independent |
| Export / import | Plan settings export | ❌ | v1.x — workaround by copying `~/.config/arq-backup-tui/plans/<id>.json` directly |
| Export / import | Plan settings import | ⚠️ | Same workaround as above |

## 2. Implementation History (Phase-to-PR Mapping)

The phase 1–5 priorities defined in the initial version of this document
have all been implemented. The "Headline Parity Table" in §1 above is the
current state of truth, and the per-phase mapping is preserved as follows:

| Phase | Item | PR | Notes |
| --- | --- | --- | --- |
| 1 | Restore metadata (mode / symlink) | (Pre-M2 / writer initial) | `Restore._restore_file_node` + S_IFLNK branch |
| 2 | Source filtering (`max_file_bytes`, `ExclusionRules`) | #10 | CLI flags + TUI Advanced step (PR #12) |
| 3 | Storage refinements (`largeblobpacks` / per-folder chunker) | #5 | `Backup(use_packs=True, large_blob_threshold=...)`; `Plan.per_source_chunkers` |
| 4 | Plan / keyset management | (CLI: M3 series) / `rotate_keyset_password` | TUI: `MaintenanceScreen` (PR #12) |
| 5 | Retention policy + blob GC | #11 | `RetentionPolicy` + `prune_records` + `gc_orphan_blobs` + `apply_retention`; TUI integration PR #12 |

**Parity items still unimplemented** (❌ / ⚠️ in the §1 table):

- Backup plan editing UI (`v1.x`; workaround via wizard recreate + delete CLI)
- Multiple destinations per plan (`v1.x`)
- Per-other-computer history isolation (`reader auto-discovers; writer is single`)
- Plan settings export/import UI (currently worked around by copying `~/.config/arq-backup-tui/plans/<id>.json` directly)
- Diff view between snapshots
- uid/gid preservation (cross-platform stance)

**Out of scope (🔴)** is exactly as in the §1 table: cloud backends, policy layer
(schedule / throttling / notifications / menu bar / wake-from-sleep), xattr / ACL / resource forks,
macFUSE / Quick Look, etc.
