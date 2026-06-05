# arq-backup-tui

An independent verifier + restorer + writer + TUI for Arq Backup 7 format
destinations. Pure Python ≥ 3.9 + stdlib (HMAC, AES, LZ4, etc. are all
implemented directly or invoked through the system `openssl`).

## 0. Downstream consumers

> **`docker-monitor` depends on this project** as of
> 2026-05-24.  The DR drill feature
> (`docker-monitor/_dr_drill_*.py`, DESIGN §B.231-§B.246)
> uses this project's read pipeline via a thin bridge
> module (`docker-monitor/_arq_restore.py`).  Surface
> in use:
>
>   - `arq_reader.Restore(src, encryption_password, *,
>     openssl_path=, backend=, on_conflict=)`
>   - `Restore.restore(*, folder_uuid, dest,
>     computer_uuid=, backuprecord_path=, paths=,
>     callback=, plan_totals=)` → `RestoreResult`
>   - `Restore.list_folders()` → `List[Tuple[str, str]]`
>   - `arq_validator.crypto.decrypt_keyset(raw,
>     password)` → `Keyset(encryption_key, hmac_key,
>     blob_id_salt)`
>   - `arq_validator.sftp.SftpBackend` — Backend
>     Protocol implementation
>
> Breaking changes to any of these surfaces require
> coordination with the docker-monitor project (cross-
> reference DESIGN §B.246).  Non-breaking additions
> (new optional kwargs, new methods) are safe.

## 1. Why This Project Exists

I've used Arq Backup as my primary backup solution for more than **15 years**.
Its offsite-backup story has bailed me out of several genuine crises — drive
failures, accidental deletions, ransomware-style incidents on adjacent
machines — and it remains the tool I trust most for the long-term safety of
my data. **I plan to keep using Arq as my main backup solution.** This
project is not a replacement for Arq.app; it's a companion built around it.

What it *is* trying to fix is a small set of personal-workflow gaps in the
Arq.app interface that, after a decade and a half of daily use, have started
to bite. This is a **TUI foundation** that fills those gaps without giving up
the on-disk format I've already committed multi-terabyte multi-year backups
to. The specific frustrations driving it:

- **Bulk-edit of exclusions across plans**. Arq.app has no good way to apply
  the same exclusion change (a regex, a wildcard, a path) to several backup
  plans at once. I want to edit excludes once and have them propagate.
- **Explicit handling of APFS-snapshot creation failures on macOS**.
  When `tmutil localsnapshot` (or the writer's APFS-snapshot path) fails
  mid-backup, I want a per-run choice: abort the whole backup so I notice the
  problem, or proceed without the snapshot consistency guarantee — knowingly,
  not silently. Right now the failure mode is opaque.
- **Convenient juggling of multiple plans and multiple destinations**. I run
  several plans across several destinations (local, NAS, offsite); the GUI
  pivots them one at a time. I want a single TUI surface that shows them all
  side by side and lets me act on any combination at once.

And — the deepest reason — **all of my long-term backups depend on this app**.
If anything goes wrong years from now (Arq.app drops a feature, the GUI stops
working on a future macOS, a destination turns out to have silent
corruption), I want to be able to **read, validate, and restore my own data
end-to-end without relying on any single tool**. So the TUI is built on top
of an independent reader / writer / validator that targets:

> **byte-perfect compatibility with Arq 7 and later.**

That's the goal. Anything I write here has to round-trip cleanly through the
official `arq_restore`, and anything Arq.app writes has to round-trip
cleanly through this reader. Without that property, the tool would just be
another way to make backups I can't trust.

This project has **no intent whatsoever** to infringe on the intellectual
property of [Arq Backup](https://www.arqbackup.com/). The supplementary
motivation behind the codebase — beyond fitting my personal workflow — is
**learning backup file-format design**: understanding at the source level
how encryption, compression, dedup, content-addressable storage, and
incremental snapshots actually combine in practice in a widely used
real-world backup tool.

For that reason the following items are **intentionally not implemented**,
because providing them would dilute the commercial value of Arq.app rather
than fill an interface gap:

- **Support for S3-compatible storage (S3, Wasabi, B2, Storj, GCS, Azure Blob,
  …)**. Bulk management of cloud backends is one of Arq Backup's core value
  propositions. **If you want cloud backends, please [purchase an Arq Backup
  license](https://www.arqbackup.com/).** (This project supports local / NAS /
  SFTP only; cloud destinations work via a `rclone mount` workaround.)
- **Operational features of the Arq.app GUI**: scheduling / notifications /
  menu bar / system tray / dashboard / cloud password recovery / license
  management, and so on, belong to the policy layer of Arq.app and are outside
  the scope of this project.
- **Brute-force tooling against the `encryptedkeyset.dat` file itself**: the
  encryption / decryption code in this project is for legitimate password
  holders to verify and restore their own backups. Do not use it as an attack
  tool against destinations whose passwords are unknown.

## 2. Relationship to Arq.app / arq_restore

This project uses the publicly available data-format specification of Arq
Backup as reference material:

- **Official Arq 7 data format documentation**: https://www.arqbackup.com/documentation/arq7/English.lproj/dataFormat.html
- **Official Arq 5 format documentation** (source of the PBKDF2 / HMAC rules
  used in Arq 7): https://www.arqbackup.com/arq_data_format.txt
- **`arq_restore` (BSD 3-Clause)**: a reference restore implementation released
  by Arq. This project used the source of `arq_restore` as a **verification
  reference for format claims** (for example, the branching logic in
  `Arq7BlobReader.m::dataForBlobLoc:`). The work is not file-by-file or
  line-by-line copying but a Python re-implementation of the binary layout of
  the alphanumeric format, and per the BSD license the source is acknowledged
  in this section.

A copy of the BSD 3-Clause license for `arq_restore` is available in the
official Haystack Software Inc. GitHub repository.

In addition, several items not specified by the reference (for example, the
fact that backuprecord is emitted as JSON rather than binary plist, the
`isLargePack` field in the BlobLoc binary layout, and the 38-byte trailing
block in Tree v4) were reverse-engineered by directly analyzing the bytes of
the operator's actual destinations (see `docs/REAL-DATA-DISCOVERIES.md`).

## 2.1 Arq 7 compatibility chart

Current state (2026-05-12, after 10 rounds of compat verification across
~20 different audit surfaces — see `docs/COVERAGE.md` for the per-PR
breakdown and `HANDOFF.md` for the round-by-round history).

### On-disk format

| Layer                                 | Read | Write | Round-trip verified |
|---------------------------------------|:----:|:-----:|:-------------------:|
| ARQO envelope (AES-256-CBC + HMAC)    | ✅   | ✅    | NIST + RFC vectors (N6) |
| PBKDF2-SHA-256 keyset derivation      | ✅   | ✅    | RFC 6070-equivalent vectors (N6) |
| Standalone-objects layout             | ✅   | ✅    | Strategy A fingerprint |
| Pack-file format (treepacks/blobpacks/largeblobpacks) | ✅ | ✅ | Strategy C at scale (65 files, byte-id) |
| Pack-size distribution                | n/a  | ✅    | 117k pack sample → 5 MB cap (N8) |
| LZ4 + gzip + uncompressed blobs       | ✅   | ✅    | C8 mixed-compression chunks |
| Tree v3 (`version=100`)               | ✅   | ✅    | Strategy C / arq_restore byte-id |
| Tree v4 (`version=101`, 38-byte trailing block) | ✅ | ✅ | Strategy F (round-trip) + I-alt (fresh-walk) |
| BackupRecord JSON (20 top-level keys) | ✅   | ✅    | R5 100% real-data parity |
| BackupRecord node v3 (27 keys) / v4 (34 keys) | ✅ | ✅ | R5 + V4 fix |
| `backupplan.json` (47 keys)           | ✅   | ✅    | R5 100% real-data parity |
| `backupconfig.json` (11 keys)         | ✅   | ✅    | D8 + R5 |
| `backupfolders.json` (6 keys)         | ✅   | ✅    | R5 |
| `backupfolder.json` (8 keys)          | ✅   | ✅    | R5 |
| `scheduleJSON` polymorphism (Daily / Hourly) | ✅ | ✅    | P2 |
| `transferRateJSON` polymorphism (Always / Scheduled) | ✅ | ✅ | P2 |
| `emailReportJSON` polymorphism (6-key / 12-key) | ✅ | ✅ | P3 |

### Source-side semantics

| Feature                               | Status | Notes |
|---------------------------------------|:------:|---|
| File content blobs (chunked + dedup)  | ✅     | Buzhash + FixedChunker; cross-run + cross-folder dedup |
| File metadata (mode, mtime, ctime, uid, gid, flags) | ✅ | Preserved through round-trip |
| Symlinks                              | ✅     | Stored as content with `S_IFLNK` mode |
| Hardlinks                             | ✅     | `(st_dev, st_ino)` cache; restored as `os.link` (N4) |
| macOS xattrs (general)                | ✅     | `XAttrSetV002` blob; ordering preserved |
| `com.apple.FinderInfo` (32-byte fork) | ✅     | F7 + N5 byte-perfect round-trip |
| `com.apple.ResourceFork`              | ✅     | N5 round-trip (incl. KB-scale payloads) |
| macOS NFSv4 ACLs                      | ✅     | F6 multi-entry + kernel round-trip |
| Linux POSIX ACLs                      | ✅     | `getfacl`/`setfacl` round-trip |
| Tree-walk reuse (incremental backup)  | ✅     | LRU-bounded `PriorTreeIndex` |
| APFS snapshot-based backup            | ✅     | `--use-apfs-snapshot` option |
| Time Machine `com_apple_backup_excludeItem` xattr | ✅ | E2 walker honours by default |
| `skipTMExcludes=true` override        | ✅     | Operator can override |
| Sparse files (`isSparse=true`)        | ⚠️     | Detection emits defaults (real Arq.app v8: 0 sparse files observed in 21 sampled records, so no-op default is acceptable) |
| macOS resource forks (data fork only) | ⚠️     | The legacy `<file>/..namedfork/rsrc` stream is captured via xattr but not restored as a separate fork on non-HFS+ filesystems |

### Operations

| Operation                             | Status | Module |
|---------------------------------------|:------:|---|
| Full backup of source tree            | ✅     | `arq_writer.backup.build_backup` |
| Incremental backup (cross-run dedup)  | ✅     | Same; uses `--dedup-against-existing` |
| Tree-version selection (3 / 4)        | ✅     | `--tree-version 3` / `--tree-version 4` |
| Chunker selection (none / default / arq_v7_41 / fixed-40m) | ✅ | `--chunker` |
| Walker cancel / pause / resume        | ✅     | `Backup.cancel()` / `.pause()` / `.resume()` |
| Mid-write crash safety (atomic temp+rename) | ✅ | N7 — SIGKILL no longer leaves truncated packs |
| Full restore                          | ✅     | `arq_reader.restore.Restore` |
| Selective restore (`paths=` filter)   | ✅     | G1 |
| Historical-record restore (non-latest) | ✅    | E9 (`backuprecord_path=`) |
| Restore conflict policies (overwrite / skip / rename) | ✅ | G2 |
| Hardlink reconstruction               | ✅     | N4 |
| Retention policy (hourly / daily / weekly / monthly / yearly / keep-last-N) | ✅ | `arq_writer.retention` |
| Orphan-blob GC                        | ✅     | `gc_orphan_blobs` |
| Format-conformance validation (L0-L4) | ✅     | `arq_validator.check_arq7_compatibility` |
| Audit-drip (resumable byte-level check) | ✅   | `audit_drip` + checkpoint state file |

### Storage backends

| Backend                               | Status |
|---------------------------------------|:------:|
| Local filesystem                      | ✅     |
| SFTP                                  | ✅     |
| Network-mounted destinations (SMB / AFP / NFS) | ⚠️ | Works via the local backend (the mount is transparent); no Arq.app-specific protocol used |
| S3 / Wasabi / B2 / Storj / GCS / Azure Blob / OneDrive / Dropbox / Box / GDrive / pCloud | ❌ | Out of scope — see README §1 |

### Deliberately out of scope

These are documented design choices, not gaps:

- **Cloud storage backends** (S3 et al.) — preserves Arq.app's commercial value.
- **Unencrypted backups** (`isEncrypted: false`) — writer always encrypts.
- **NSDictionary hash-bucket key emission order** (A11) — would require
  re-implementing Apple's NSDictionary runtime; our writer emits in Python's
  dict insertion order which is JSON-equivalent but byte-different.
- **Tree v4 trailing-block bytes 0-15 exact reproduction** — these encode
  Arq.app's per-Node first-emit-time which a content-addressed writer can't
  reproduce without sidecar state (K2-K4 analyses; 94% of bytes covered by
  the `create_time` deterministic fallback, K4-2 §5.7.5).

### Verification log (the highest-confidence proofs)

- **Strategy A** (shape fingerprint diff): 100% schema match across all 4
  sidecars + BackupRecord shapes against real `/Volumes/arqbackup1`.
- **Strategy B** (cross-restore Arq.app → our reader): 127,222 files
  byte-identical.
- **Strategy C** (writer → `arq_restore`): Tree v3 byte-perfect, scale-
  verified at 65+ files (C-S1).
- **Strategy F + R4** (round-trip byte equivalence parse → re-emit):
  278/278 blobs from real Arq.app v8 destination byte-identical.
- **Strategy I-alt** (writer → patched `arq_restore`): 4/4 fresh-walk
  Tree v4 files byte-identical via independent reader (V4).
- **Strategy K + K4-2** (Tree v4 trailing-block correlation): 94% of
  trailing_sec values explained as btime ⨯ ctime.
- **N6** (crypto primitives): 6/6 RFC/NIST test vectors pass.

For the full per-PR breakdown of each round see `docs/COVERAGE.md`'s
"Round N" sections; for the historical context behind each finding see
`HANDOFF.md`.

## 3. What It Can Do

The four libraries this package provides:

| Package | Role | Entry point |
|---|---|---|
| `arq_validator` | 4-tier integrity verification of an Arq 7 destination (L0/L1a/L1b/L2 + audit-drip) | `python -m arq_validator` |
| `arq_reader` | Restoring Arq 7 (+ 5/6) backups to local files | `python -m arq_reader` |
| `arq_writer` | Writing new Arq 7 backup destinations | `python -m arq_writer` |
| `arq_tui` | Integrating the three above into a single Textual TUI | `python -m arq_tui` or `./arq-tui.py` |

### Validation (validator)

```sh
python -m arq_validator --root /Volumes/arqbackup1 --tier deep \
    --password "$ARQ_PW"
```

The 4 tiers:
- **L0** (`dry-run`): directory shape only (computer-UUID, the 4 object families, backupfolders)
- **L1a** (`quick`): sample sweep of ARQO magic bytes (default 5%)
- **L1b** (`deep`): keyset decryption + HMAC of the latest backuprecord per backup folder
- **L2** (`audit`): HMAC of every EncryptedObject (+ resumable audit-drip mode)
- **`record`**: walk every BlobLoc reachable from one backuprecord (catches missing/corrupt deep blobs that L0–L2 don't)

**Incremental audit ledger** (`audit` + `record` tiers): pass `--incremental`
to skip blobs already confirmed in a per-destination ledger; pass
`--ledger-prune-days N` to drop entries older than N days so a quietly-bad
blob eventually gets re-audited. Ledger lives at
`~/.local/state/arq-backup-tui/audit-ledgers/<target>.json`. See
`arq_validator/incremental_audit.py`.

### Restore (reader)

```sh
python -m arq_reader restore /Volumes/arqbackup1 \
    --password "$ARQ_PW" \
    --folder-uuid <FU> --dest /tmp/restored
```

A specific historical record / specific path / specific source folder can
each be designated. Read is supported for Arq 5/6/7 alike.

**Dry-run preview** (`--list-only`): walk the backuprecord's tree + emit
`would_restore_file` events without touching the destination. Use to
verify a `--paths` filter, size a restore, or spot-check a snapshot's
contents before paying the I/O cost. The dest argument is still
positional but unused in dry-run mode.

```sh
python -m arq_reader restore /Volumes/arqbackup1 \
    --password "$ARQ_PW" \
    --list-only --paths Documents/notes \
    <folder-uuid> /tmp/dummy
```

**Conflict policy** (`--on-conflict`): choose `overwrite` (default,
silent), `skip` (drop restored bytes + emit `conflict_skipped`), or
`rename` (write to `name.restored-N` so both versions remain).

### Writing (writer)

```sh
# For Arq.app v8 plans with useBuzhash: True (Buzhash content-
# defined chunking, mean ~64 KiB):
arq-backup create ~/Documents \
    --dest /Volumes/arqbackup1 \
    --password "$ARQ_PW" \
    --use-packs --chunker arq_v7_41 --tree-version 4 \
    --exclude-glob '*.log' --max-file-bytes 1073741824

# For Arq.app v8 plans with useBuzhash: False (fixed
# 40,000,000-byte chunks — sampled directly from the operator's
# real plan, see HANDOFF.md GAP-L):
arq-backup create ~/Documents \
    --dest /Volumes/arqbackup1 \
    --password "$ARQ_PW" \
    --use-packs --chunker fixed-40m --tree-version 4
```

Byte-perfect round-trip with this verifier and reader is
guaranteed; compatibility with the Arq.app GUI side has now been
verified at the byte level via four independent strategies
(restore from Arq.app v8 destinations, restore through the BSD
``arq_restore`` reference at Tree v3, schema parity for every
JSON sidecar + plist, and cross-destination ``blob_id`` math). See
``docs/COMPAT-VERIFICATION.md`` for the full evidence trail.

### TUI

```sh
./arq-tui.py    # or python -m arq_tui
```

A Textual TUI that lets you handle the three above on a single screen. Backup
/ restore / verification / scouting / backup-set browser / retention policy
application / password rotation / plan editing / console (slash-command), etc.

### Coexistence with Arq.app — what to expect

If Arq.app is installed on the same machine, the TUI and the Arq GUI sit
side by side. **The relationship is a one-way, read-only mirror: Arq →
TUI.** Knowing exactly how they interact up front avoids surprises:

**The TUI reads Arq (read-only mirror).** On launch the TUI reads Arq's
local `ArqAgent/server.db` and shows the *same* **active** backup plans,
storage locations, and activity log the Arq GUI shows — merged into the
TUI's own lists and badged `◆ Arq`. So the two feel nearly in sync. The
TUI never writes to Arq's database (it's root-owned and the agent writes
it live), and never reads secrets (passwords stay in the Keychain; the
TUI prompts per session).

**Arq does NOT see anything the TUI creates.** The mirror does not go the
other way:

- A **backup plan created in the TUI** is stored only in the TUI's own
  config (`~/.config/arq-backup-tui/plans/`). It **does not appear in the
  Arq GUI**, and Arq will not run it.
- A **storage location added in the TUI** is likewise private to the TUI.
- Editing or deleting a `◆ Arq` plan / location from the TUI is blocked
  (read-only) — do those in Arq.app. Deleting a *TUI-owned* location only
  forgets it from the TUI's list; it never touches backup data on disk.

**The on-disk backup data is the shared, interoperable layer.** Because
the writer is byte-compatible with Arq 7, a backup the TUI writes to a
folder / SFTP destination *is* a valid Arq 7 backup set. If you point
Arq.app at that same destination (add it as a storage location in Arq),
Arq's restore browser can read those backup records — but Arq still won't
show the *plan* that produced them, and won't create one. Conversely the
TUI restores/validates Arq-written backups byte-perfectly.

**Summary:** plans + locations are private to whichever tool created
them; only the backup data on the destination is shared. With just one of
the two installed, that tool works fully on its own.

Full design + field mapping: `docs/ARQ-APP-MIRROR.md`.

## 3.5 Quick start (5 minutes from clone to first restore)

Goal: prove the round-trip on your own data without leaving your
machine. No SFTP, no cloud, no Arq.app interaction.

```sh
# 1. Clone + check the runtime is sane
git clone https://github.com/neoocean/arq-backup-tui.git
cd arq-backup-tui
python3 --version          # need ≥ 3.9
openssl version            # need any modern OpenSSL on PATH
python3 -m unittest discover tests   # 200+ tests should pass

# 2. Pick a small source folder + a fresh destination
SRC=~/Documents/sample-folder
DST=/tmp/arq-test-dst
mkdir -p "$DST"

# 3. Make your first backup
export ARQ_PW="hunter2"
python3 -m arq_writer create "$SRC" --dest "$DST" \
    --password-env ARQ_PW \
    --backup-name "first-backup" \
    --use-packs --dedup-against-existing

# 4. Validate it (HMAC the latest record + sample objects)
python3 -m arq_validator deep "$DST" --password-env ARQ_PW

# 5. Restore it elsewhere
mkdir -p /tmp/arq-restored
python3 -m arq_reader restore "$DST" \
    --password-env ARQ_PW \
    --dest /tmp/arq-restored

# 6. Compare bytes
diff -r "$SRC" /tmp/arq-restored
```

If step 6 prints nothing, the round-trip succeeded — your bytes
came back byte-identical through every layer.

For the **TUI** experience instead of CLI:

```sh
pip install -e ".[tui]"     # adds the textual dependency
python3 -m arq_tui          # launches the TUI; press [n] to make a plan
```

Inside the TUI:
- `[n]` create a backup plan
- `[r]` run the focused plan
- `[b]` browse a destination's history
- `[v]` validate a destination
- `[a]` watch every running backup / restore (cron-launched ones too)
- `[s]` manage cron / launchd schedules for plans
- `[p]` (inside a backup run) pause / resume

Operators with an existing Arq.app destination on Hetzner / NAS
SFTP: see `docs/COMPAT-SFTP-TESTING.md` for the `.secrets/`
credential setup; once configured, the same commands work
against the remote destination via `--sftp-host` etc.

## 4. Dependencies and Runtime Environment

- **Runtime**: Python ≥ 3.9 + system `openssl` (on PATH or via
  `--openssl-path`)
- **For SFTP**: the system's OpenSSH `ssh` / `sftp` client
- **Python third-party**: none (only the TUI optionally depends on `textual`)
- **OS**: macOS / Linux. Windows is unsupported due to OpenSSH/openssl
  behavior differences.

## 4.5 Development setup

If you intend to contribute or run the test/lint stack locally:

```sh
# 1. Install the dev extras
pip install -e ".[test,tui]" pyright pre-commit

# 2. Wire up the pre-commit hooks
pre-commit install

# 3. (Optional) Run all hooks against the current tree once,
#    so your first commit doesn't trip a check that's been
#    sitting unnoticed.
pre-commit run --all-files
```

Hooks that run on every commit (configured in
`.pre-commit-config.yaml`):

- `check-doc-links` — fails when a `.md` file references a
  renamed/removed `arq_*/...py` path or undefined symbol
  (the same checker CI runs on every PR).
- `pyright` — soft-skips when not installed locally; CI runs
  it unconditionally on every PR.
- Standard hygiene: trailing whitespace, missing EOF newlines,
  large file accidents, malformed YAML/TOML, merge-conflict
  markers.

CI also runs the static type check (`pyright`) and the doc-link
checker on every PR — see `.github/workflows/test.yml`. Local
hooks just shorten the feedback loop.

## 5. License

This repository itself is under the **MIT License** (`LICENSE`).

Arq Backup, "Arq", and related trademarks are property of Haystack Software
Inc. This project has never received sponsorship or official endorsement
from Haystack Software in any form.

## 6. Further Reading

- `DESIGN.md` — overall project design
- `docs/MECHANISM.md` — detailed inner workings of backup/restore/verification
- `docs/COVERAGE.md` — Arq 7 feature parity matrix
- `docs/COMPATIBILITY.md` — lock-in of the 25 Arq 7 invariants
- `docs/COMPAT-VERIFICATION.md` — catalog of compatibility verification strategies
- `docs/COMPAT-SFTP-TESTING.md` — integration tests based on operator credentials
- `docs/REAL-DATA-DISCOVERIES.md` — compatibility items discovered and fixed
  via real destinations (`isLargePack`, JSON backuprecord, Tree v4, etc.)
- `docs/PLAN-tui.md` — TUI design + screen catalog
- `docs/APFS-SNAPSHOTS.md` — macOS APFS snapshot integration
- `docs/UNICODE.md` — guarantees for multilingual / emoji / long-path handling
- `docs/RESEARCH-backup-creation-feasibility.md` — feasibility study before
  writing the writer
- `docs/RESEARCH-format-extensions.md` — RE notes on pack containers /
  chunkers / Arq 5–6 features
