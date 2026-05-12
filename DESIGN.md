# arq-backup-tui — DESIGN

This document records the goals, structure, and design decisions of the
project as agreed and implemented to date. Future changes will be reflected
in this document via PRs.

## 1. Project goals

`arq-backup-tui` is a TUI application that aims to let you work with
[Arq Backup](https://www.arqbackup.com/) 7 format backup destinations
**without the official Arq.app**. At the current stage, it provides the
first building block — a **standalone Validator** — as a library plus a CLI.

### 1.1 Operator-side use cases

- **Verify integrity of off-site backups faster than Arq.app's monthly
  self-validation** (catch bit-rot / partial transfers / structural damage
  on a days-to-weeks cadence).
- Apply identical validation logic to both a local mirror (e.g.
  `/Volumes/arqbackup1`) and a remote SFTP destination (e.g. Hetzner
  Storage Box).
- In future stages, expand into an operator tool that visualizes backup
  state via TUI and integrates validation, recovery, and creation.

### 1.2 Reference origin inherited by this repository

`scripts/arq-validate.py` (in `neoocean/docker-monitor`, 2,673 LOC) is a
standalone validator that was written specifically for the operator's
environment. This project adopts the validation logic, format
interpretation, and empirical correction values from that reference, but
restructures them as follows to suit a TUI/library:

| Item | reference (`docker-monitor`) | This project |
| --- | --- | --- |
| Packaging | Single-file script | `arq_validator/` package |
| Secret store | File-based `.secrets/` mandatory | TUI/CLI passes secrets as call arguments |
| Progress | stderr text | `ProgressCallback` events + stderr |
| Hetzner specifics | Connection-rate-limit detection built in | Backend abstraction (reusable for other destinations) |
| Backend | SFTP-only + local variant | `Backend` protocol + `LocalBackend` / `SftpBackend` |
| audit-drip | Coupled to operator LaunchAgent | Callable from TUI / CLI / scheduler anywhere |

The core reverse-engineering results from the reference — the unpadded
25-byte keyset magic, 32-byte (the official spec says 64-byte) key fields,
ARQO multi-object container detection, and so on — are adopted verbatim
in this project. (See the comments in `arq_validator/constants.py`.)

### 1.3 Documentation conventions

- **Diagrams** use [Mermaid](https://mermaid.js.org/) (` ```mermaid `
  fenced code blocks). Flow / state / sequence / class diagrams render
  inline on GitHub + on most modern Markdown viewers. ASCII box-drawing
  is reserved for cases where the visual layout is itself the point
  (terminal-UI mockups, file-system tree listings) — anything that
  represents control flow, data flow, or state transitions should be
  Mermaid so it stays readable as it grows.
- **Code references** in prose use inline `code` formatting. The
  `scripts/check_doc_links.py` checker (wired into CI) resolves every
  ``arq_*/...py`` path + ``arq_*.module.symbol`` ref against the live
  codebase + fails on stale references.
- **External-repo references** (e.g. ``scripts/arq-validate.py`` in
  ``neoocean/docker-monitor``) are listed in the checker's
  ``_EXTERNAL_REF_PATHS`` set so they don't trip CI.

## 2. Package structure

```
arq-backup-tui/
├── arq_validator/                # Validation library (imported by the TUI)
│   ├── __init__.py               # Public API surface
│   ├── __main__.py               # `python -m arq_validator`
│   ├── constants.py              # Arq 7 format constants (regex, magic, offset, ...)
│   ├── crypto.py                 # PBKDF2 / HMAC / openssl AES-256-CBC
│   ├── backend.py                # Backend Protocol + LocalBackend
│   ├── sftp.py                   # SftpBackend (wraps the OpenSSH client)
│   ├── layout.py                 # Directory discovery + backup record traversal
│   ├── events.py                 # ProgressCallback + Event definitions
│   ├── tiers.py                  # L0/L1a/L1b/L2 validation functions
│   ├── audit_drip.py             # Resumable nightly audit (cursor + throttle)
│   ├── machine_info.py           # Source machine identification (compares backupconfig.json + backupplan.json + host)
│   ├── runner.py                 # ValidationTier enum + validate() orchestrator
│   └── cli.py                    # argparse CLI
├── arq_writer/                   # Backup creation library (Arq.app compatible)
│   ├── __init__.py               # Public API surface
│   ├── __main__.py               # `python -m arq_writer`
│   ├── constants.py              # Compression types, Tree version, ... (re-exports validator constants)
│   ├── lz4_block.py              # Pure-Python LZ4 block codec
│   ├── types.py                  # BlobLoc / FileNode / TreeNode / Tree dataclasses
│   ├── serialize.py              # Binary serialization for Node / Tree / BlobLoc
│   ├── crypto_write.py           # ARQO encoder + encryptedkeyset.dat builder + AES encrypt + rotate_keyset_password
│   ├── json_configs.py           # backupconfig / backupplan / backupfolders builders
│   ├── backuprecord.py           # backuprecord plist + LZ4 + ARQO pipeline
│   ├── pack_builder.py           # Arq 7 PackBuilder — produces treepacks/blobpacks (use_packs=True mode)
│   ├── chunker.py                # Buzhash content-defined chunker + multi-version registry
│   ├── arq_chunker_params.py     # Arq.app v7.41 RE parameters + ChunkerConfig registry
│   ├── chunker_oracle.py         # Chunker selection heuristic (size-based fallback)
│   ├── prior_tree_index.py       # PriorTreeIndex — tree-walk dedup cache seed
│   ├── dedup.py                  # Builds the dedup-against-existing blob cache
│   ├── exclusions.py             # ExclusionRules (glob + regex + .gitignore parsing)
│   ├── macos_snapshot.py         # macOS APFS snapshot support (with_apfs_snapshot, is_macos*)
│   ├── retention.py              # RetentionPolicy + prune_records + gc_orphan_blobs + apply_retention
│   ├── macho_buzhash_finder.py   # Arq.app Mach-O static analysis + chunk-size behavior inference
│   ├── buzhash_re_cli.py         # `arq-buzhash-find` CLI
│   ├── backup.py                 # Backup class + build_backup() orchestrator
│   └── cli.py                    # argparse CLI (`arq-backup create`, including 8 new flags)
├── arq_reader/                   # Backup restore library (writer's inverse)
│   ├── __init__.py
│   ├── __main__.py               # `python -m arq_reader`
│   ├── decrypt.py                # Full ARQO decrypt (HMAC verify then 2-stage AES)
│   ├── parse.py                  # BinaryReader + Arq 7 Node/Tree/BlobLoc binary parser
│   ├── restore.py                # Restore class (supports both standalone and isPacked=true)
│   ├── arq5_pack.py              # Arq 5/6 .pack/.index parser + builder (including SHA-1 fanout)
│   ├── arq5_binary.py            # Arq 5/6 Tree v10-v22 / Commit v3-v12 / Node / BlobKey binary parser
│   ├── arq5_keyset.py            # Arq 5/6 encryptionvN.dat decryption (PBKDF2-SHA1)
│   ├── arq5_restore.py           # Arq 5/6 backup restore orchestrator (commit→tree→files)
│   └── cli.py                    # argparse CLI (`arq-reader list`/`restore`)
├── arq_tui/                      # Textual TUI (writer + reader + validator integration)
│   ├── __init__.py               # Exposes ArqTuiApp
│   ├── __main__.py               # `python -m arq_tui` entry point
│   ├── app.py                    # Top-level app (PlanRegistry, CredentialCache, DestinationStore)
│   ├── state.py                  # Plan / Destination dataclasses + persistent store
│   ├── workers.py                # BackupWorker / RestoreWorker / ValidateWorker (in-process worker thread bridge)
│   ├── runs.py                   # State-file IPC: RunWriter / enumerate_runs / signal_cancel / gc
│   ├── console_commands.py       # Slash-command dispatch for the quake-style console
│   ├── backend_open.py           # Backend open/close (LocalBackend / SftpBackend)
│   ├── cli.py                    # `plans` / `runs` / `machine-info` headless subcommands
│   ├── theming.css               # Colors, padding, etc. CSS
│   ├── screens/
│   │   ├── home.py               # Landing (plan list + quick actions)
│   │   ├── plan_wizard.py        # 6-step wizard (sources / dest / enc / chunker / advanced / review)
│   │   ├── backup_run.py         # Execution + ProgressPanel
│   │   ├── backup_sets.py        # destination/layout browser (enter maintenance with [m] from below)
│   │   ├── record_browser.py     # Tree walk for a single backuprecord
│   │   ├── restore_run.py        # Restore execution + ProgressPanel
│   │   ├── validate_run.py       # Validation execution + ProgressPanel
│   │   ├── maintenance.py        # Password rotation + retention application
│   │   ├── runs_monitor.py       # Activity screen — polls external process state files at 1Hz
│   │   └── help.py
│   └── widgets/
│       ├── source_picker.py / destination_modal.py
│       ├── password_modal.py / restore_target_modal.py
│       ├── console.py            # Quake-style slash-command console (slide-down)
│       └── progress_panel.py
├── tests/                        # Synthetic / round-trip unit + integration tests (355 cases, ~140s; 7 skipped = depend on SFTP credentials)
│   ├── fixtures.py               # Arq 7 tree builder for validator tests
│   ├── integration/              # Real Arq.app SFTP destination compatibility checks (.env-based)
│   │   ├── _creds.py             # Environment + .env credentials loader
│   │   └── test_arqapp_sftp_compat.py
│   ├── test_crypto.py / test_layout.py / test_runner.py
│   ├── test_audit_drip.py / test_sftp.py
│   ├── test_writer_lz4.py        # Pure LZ4 codec round-trip
│   ├── test_writer_format.py     # Binary serialization + crypto round-trip
│   ├── test_writer_e2e.py        # Writer → validator 4-tier round-trip
│   ├── test_writer_packed.py     # Packed mode (treepacks/blobpacks) round-trip
│   ├── test_writer_chunker.py    # Buzhash chunker round-trip
│   ├── test_writer_dedup.py      # cross-run dedup verification
│   ├── test_writer_tree_walk_reuse.py # PriorTreeIndex-based walk-reuse verification
│   ├── test_writer_exclusions.py # ExclusionRules glob/regex/gitignore
│   ├── test_writer_cli_flags.py  # arq-backup CLI 8 new flags
│   ├── test_retention.py         # RetentionPolicy + prune + GC round-trip
│   ├── test_fingerprint.py       # Shape fingerprint compatibility verification
│   ├── test_reader_e2e.py        # Reader byte-identical restore verification
│   └── test_tui_m{1..7}_*.py     # TUI per-stage smoke + functional tests
├── docs/
│   ├── DESIGN.md                                  # ← this document lives at the repo root
│   ├── COMPATIBILITY.md / COVERAGE.md / GUI-PARITY.md
│   ├── MECHANISM.md / PLAN-tui.md
│   ├── COMPAT-VERIFICATION.md / COMPAT-SFTP-TESTING.md
│   ├── APFS-SNAPSHOTS.md / UNICODE.md
│   ├── RESEARCH-backup-creation-feasibility.md    # Pre-implementation feasibility (now implemented)
│   └── RESEARCH-format-extensions.md              # pack / chunker / Arq5 RE notes (mostly implemented)
├── arq-tui.py                    # Root entry point (run TUI via ./arq-tui.py)
├── pyproject.toml                # Console scripts registration
├── DESIGN.md                     # ← this document
└── LICENSE
```

## 3. Validation tier model

We provide four validation tiers that map one-to-one with Arq.app's
internal validation tiers. Each tier subsumes all tiers below it.

| Tier | Name | What it inspects | Cost | Frequency (operator-recommended) |
| --- | --- | --- | --- | --- |
| **L0** | `dry-run` | Directory shape (computer UUID, 4 object families, backupfolders) + keyset existence | I/O ~hundreds of ms | Real-time |
| **L1a** | `quick` | ARQO magic-byte sample sweep (default 5%, full sweep with `--sample-fraction 1.0`) | sample × 4-byte RTT | Weekly |
| **L1b** | `deep` | `encryptedkeyset.dat` decrypt + HMAC of the latest backuprecord per folder | keyset 1 time + ≤50 MB per folder | Quarterly / 90 days |
| **L2** | `audit` | HMAC of every EncryptedObject (multi-object container aware) | Full download of every object | Once a year, or on suspicion |

Because L2 can take several hours per run for large destinations, a
separate **audit-drip** mode is provided (see § 5) and a parallel
worker-pool mode is provided for thread-safe backends (see § 3.2).

### 3.1 What kinds of defects each tier catches

- **L0**: Missing mounts, wrong root path, missing computer UUID
- **L1a**: Truncation from partial transfer, file replacement, structural
  damage
- **L1b**: Wrong password, keyset corruption, bit-rot in the latest backup
  metadata
- **L2**: Bit-rot / tampering / damage at encryption time across every
  object (the same guarantee as Arq's monthly self-validation)

### 3.2 Parallel L2 audit

L2 historically ran every per-file HMAC verification on the calling
thread.  For a populated `LocalBackend` mirror (tens of GB of small
blob/tree packs) HMAC is the CPU-bound bottleneck and serial execution
caps throughput at one core's worth of work.

`run_full_audit(..., audit_concurrency=N)` runs N worker threads via a
`ThreadPoolExecutor`.  Each worker computes a per-file `_AuditDelta`
without touching shared state; the driver merges deltas into the
shared `ObjectAuditResult` under a lock.  In-flight work is bounded
to `N * 2` so budget checks (`max_runtime_sec` / `max_bytes`) stay
responsive and memory stays constant.

Safety contract:

- `Backend.supports_concurrent_reads` is the protocol-level switch.
  `LocalBackend` declares it `True` (every read opens a fresh
  descriptor); `SftpBackend` defaults to `False` (single channel,
  not thread-safe).  Requesting `audit_concurrency > 1` on a
  non-concurrent backend silently clamps to 1 and emits a `LOG`
  event so the operator sees the downgrade.

- Defensive bounds: `audit_concurrency` is clamped to `[1, 64]` to
  avoid fd-table exhaustion on extreme inputs.

- Event ordering: `AUDIT_FILE_VERIFIED` / `AUDIT_FILE_FAILED` events
  may arrive out of order in parallel mode.  `AUDIT_PROGRESS` events
  remain serialized at the merge point and always reflect a
  consistent snapshot.

- Ledger (`AuditLedger`) writes happen under the merge lock, so
  `contains()` / `record()` interactions are race-free regardless of
  worker count.

CLI flag: `--audit-concurrency N` (default 1).  Recommended values:
4-8 for a multi-core local mirror; 1 (default) for SFTP.

## 4. Core abstractions

### 4.1 Backend protocol (`backend.py`)

```python
class Backend(Protocol):
    def list_dir(self, path: str) -> list[str]: ...
    def stat_size(self, path: str) -> int: ...
    def read_range(self, path: str, offset: int, length: int) -> bytes: ...
    def read_all(self, path: str) -> bytes: ...
    def exists(self, path: str) -> bool: ...
    def is_dir(self, path: str) -> bool: ...
```

The validation logic depends only on these six methods. Adding a new
backend (S3, B2, WebDAV, …) is therefore a matter of implementing this
class.

#### Bundled implementations

- **`LocalBackend(root_path)`**: Local filesystem. Built-in defense
  against path escape (`..`).
- **`SftpBackend(host, port, user, password|identity_file, ...)`**:
  OpenSSH `ssh -N -M` master + ControlPath multiplexing. Password
  authentication uses an SSH_ASKPASS shim (no exposure on argv).
  `read_range` uses `head -c N` when offset is 0, otherwise
  `dd bs=1 skip=K count=N status=none` — a combination confirmed to work
  even under Hetzner's restricted shell.

### 4.2 Crypto strategy (`crypto.py`)

- **PBKDF2-SHA256, HMAC-SHA256**: Python stdlib (`hashlib`, `hmac`)
- **AES-256-CBC**: only via the `openssl` CLI subprocess
- **Zero Python third-party crypto package dependencies** — no
  `cryptography`, `pycryptodome`, etc. A reasonable trade-off given that
  `openssl` ships standard on macOS / Linux, and that the AES call is only
  needed once for keyset decryption.
- ARQO HMAC verification can be done with stdlib alone, so the core (HMAC)
  parts of L0/L1a/L2 work even without `openssl`.

### 4.3 Progress events (`events.py`)

```python
class EventKind(Enum):
    RUN_STARTED, RUN_FINISHED, TIER_STARTED, TIER_FINISHED,
    LAYOUT_DISCOVERED, COMPUTER_FOUND,
    MAGIC_CHECK_PROGRESS, MAGIC_CHECK_FAILED,
    KEYSET_DECRYPTED, KEYSET_FAILED,
    BACKUPRECORD_VERIFIED, BACKUPRECORD_FAILED,
    AUDIT_FILE_VERIFIED, AUDIT_FILE_FAILED, AUDIT_FILE_SKIPPED,
    AUDIT_PROGRESS,
    AUDIT_DRIP_FIRE_STARTED, AUDIT_DRIP_FIRE_FINISHED,
    AUDIT_DRIP_SWEEP_STARTED, AUDIT_DRIP_SWEEP_COMPLETED,
    AUDIT_DRIP_PROGRESS, AUDIT_DRIP_ABORTED, AUDIT_DRIP_PAUSED,
    LOG, ...

@dataclass
class Event:
    kind: EventKind
    message: str
    payload: dict

ProgressCallback = Callable[[Event], None]
```

The TUI passes a callback via `validate(..., callback=on_event)` to receive
live progress. Exceptions raised inside the callback are absorbed in
`events.emit()` — the validation loop does not stop even if a UI handler
throws.

### 4.4 Validation orchestrator (`runner.py`)

```python
class ValidationTier(Enum):
    DRY_RUN = "dry-run"   # L0 only
    QUICK   = "quick"     # L0 + L1a magic sweep
    DEEP    = "deep"      # + L1b backuprecord HMAC
    AUDIT   = "audit"     # + L2 full HMAC sweep

@dataclass
class ValidationReport:
    tier: str
    started_at, finished_at: float
    layout: LayoutResult | None
    magic_check: MagicCheckResult | None
    backuprecord: BackupRecordResult | None
    audit: ObjectAuditResult | None
    error: str | None
    def has_failures(self) -> bool: ...
    def to_dict(self) -> dict: ...

def validate(backend, *, tier, root="/", encryption_password=None,
             sample_fraction=0.05, audit_skip_larger_than=...,
             callback=None, ...) -> ValidationReport: ...
```

## 5. audit-drip — resumable L2 audit

For large destinations, L2 cannot be finished in a single fire (tens to
hundreds of GB). `audit_drip.run_audit_drip()` implements a nightly-fire
model that makes a little progress every day under a **30 min – 1 hour
nightly budget**.

### 5.1 Walk order

```python
walk = [
    (computer_uuid, family, shard, file_name)
    for computer_uuid in sorted(computers)
    for family in ("blobpacks", "treepacks", "largeblobpacks", "standardobjects")
    for shard, file_name in sorted(items_in(family, computer_uuid))
]
```

Because the order is deterministic, the cursor remains meaningful even if
the directory grows or shrinks between fires.

### 5.2 Cursor + resume

- The cursor is updated immediately after each file is processed (success /
  failure / error / skip — regardless):
  `(cursor_computer, cursor_kind, cursor_shard, cursor_file_name)`
- The next fire starts from the first item that is **lexicographically
  larger** than the cursor — safe to advance even if the file at the
  cursor position has disappeared.
- When the walk reaches the end, `sweep_completed_at` is recorded; the
  next fire begins a new sweep (`sweep_count += 1`).

### 5.3 Soft limits

| Option | Meaning | Default |
| --- | --- | --- |
| `max_runtime_sec` | Wall-clock budget per fire | 0 (unlimited) |
| `rate_files_per_min` | Minimum gap between files (throttle) | None (no limit) |
| `paused_until_epoch` | Silently skip until this epoch | None |
| `skip_larger_than` | Skip validation above this size | 256 KB (Arq's `maxPackedItemLength`) |

`failed_files_this_sweep` is capped at 100 entries so that runaway
corruption does not bloat the state file.

### 5.4 State file

```jsonc
{
  "target": "hetzner",
  "sweep_started_at": 1715000000.0,
  "sweep_completed_at": null,
  "sweep_count": 3,
  "cursor_computer": "12345678-...",
  "cursor_kind": "blobpacks",
  "cursor_shard": "ff",
  "cursor_file_name": "0000FF-...-...pack",
  "files_audited_this_sweep": 12345,
  "files_total_this_sweep": 462000,
  "fails_this_sweep": 0,
  "errors_this_sweep": 0,
  "failed_files_this_sweep": [],
  "last_fire_aborted_reason": "max_runtime",
  "paused_until_epoch": null,
  "error": null
}
```

A separate file is used per target (`./arq_audit_drip_local.json`,
`./arq_audit_drip_hetzner.json`), so local and remote sweeps can run in
parallel without conflict.

## 6. CLI (`arq_validator.cli`)

```
arq-validator <tier> [path] [options]

tier:
  dry-run | quick | deep | audit | audit-drip

path:
  Backup root for local mode. Omit when using --sftp.

options:
  # Password (required for deep/audit/audit-drip)
  --password / --password-file / --password-env

  # SFTP destination
  --sftp user@host[:port]:/root
  --sftp-password / --sftp-password-env / --sftp-password-file
  --sftp-identity-file
  --sftp-known-hosts

  # Per-tier knobs
  --sample-fraction 0.05   # quick/deep/audit
  --audit-skip-larger-than 256000
  --audit-max-runtime-sec / --audit-max-bytes

  # audit-drip
  --target {free-form label}
  --state-file ./arq_audit_drip_<target>.json
  --max-runtime-sec / --rate-files-per-min

  # Output
  --quiet / --json-events
```

Exit codes:

- `0` — validation passed
- `2` — invocation error (arguments, path, missing password)
- `3` — backend / IO error
- `4` — validation failed, or audit-drip has failed entries

## 7. Test strategy

`tests/fixtures.py` builds synthetic Arq 7 trees — every tier can be
round-trip validated without a real Arq backup.

- **Synthetic keysets**: build `encryptedkeyset.dat` bytes from a known
  password + random keys, then round-trip decrypt with the validator
- **Synthetic ARQOs**: build ARQO objects with a valid HMAC and cover all
  failure paths via single / multi container variants and corruption cases
  (bit flips)
- 355 tests (~140s; 7 skipped = depend on real SFTP credentials). Because
  no real SFTP server is present in the sandbox, the default unit tests
  only verify build-time validation, the spec parser, and the contract
  that blocks calls before `__enter__`. A **harness for operators to run
  integration tests against real destinations using `.env` credentials**
  (`tests/integration/`, `docs/COMPAT-SFTP-TESTING.md`) is also provided.

## 8. Dependencies and runtime environment

- **Runtime**: Python ≥ 3.9 + the `openssl` CLI (on PATH or via
  `--openssl-path`)
- **For SFTP**: the system OpenSSH `ssh` / `sftp` clients
- **Python third-party packages**: none (runtime and tests are stdlib-only)
- **OS verification**: macOS, Linux. Windows is unsupported (the OpenSSH /
  openssl behavior details differ).

## 9. Already-implemented extensions (PR #5–#12)

Earlier versions of DESIGN.md marked chunkers, pack containers, the TUI,
and retention policies as deferred, but a subsequent series of PRs has
implemented them all. The historical record is preserved in
`docs/RESEARCH-format-extensions.md` and
`docs/RESEARCH-backup-creation-feasibility.md`; the current state is
summarized below.

### 9.1 Backup-write advanced features

| Feature | PR | Activation |
| --- | --- | --- |
| Buzhash content-defined chunking | #5 | `Backup(chunker_config=...)` / CLI `--chunker {none\|default\|arq_v7_41}` |
| Arq.app v7.41 RE chunker parameters | #5 | `arq_chunker_params.ARQ_V7_CHUNKER_CONFIG` |
| Pack mode (treepacks/blobpacks) | #5 | `Backup(use_packs=True)` / CLI `--use-packs` |
| Cross-run dedup | #5 | `Backup(dedup_against_existing=True)` / CLI `--dedup-against-existing` |
| Tree-walk reuse (`PriorTreeIndex`) | #5 | Automatic when dedup-against-existing is enabled |
| `ExclusionRules` (glob/regex/.gitignore) | #10 | `Backup(exclusions=...)` / CLI `--exclude-glob/--exclude-regex/--exclude-from` |
| max-file-bytes cutoff | #10 | `Backup(max_file_bytes=N)` / CLI `--max-file-bytes` |
| macOS APFS snapshot | #8 | `with_apfs_snapshot()` / CLI `--use-apfs-snapshot` |

### 9.2 Maintenance features

| Feature | PR | Entry point |
| --- | --- | --- |
| `RetentionPolicy` (keep_last_n + time buckets) | #11 | `apply_retention(backend, policy=...)` |
| `prune_records()` (backup record pruning) | #11 | First step of retention |
| `gc_orphan_blobs()` (conservative pack-level GC) | #11 | Second step of retention |
| `Backend.unlink()` (Local + Sftp) | #11 | Called by retention/gc |
| `rotate_keyset_password()` (password change) | #5/#7 | Preserves master keys; only regenerates salt+IV+ciphertext+HMAC |

### 9.3 TUI integration

| Feature | PR | Location |
| --- | --- | --- |
| `arq_tui/` package (M1–M6) | (M-series) | Home / wizard / backup-set browser / record browser / restore / validate |
| Plan wizard "Advanced" step (6 steps) | #12 | Exposes exclusions / max-file-bytes / APFS / retention |
| `MaintenanceScreen` (`[m]`) | #12 | Password rotation + retention apply + dry-run/real-run + GC toggle |
| Root `arq-tui.py` entry point | #12 | Run immediately with `./arq-tui.py` (self-injects sys.path) |
| New fields on the `Plan` dataclass | #12 | `exclude_globs` / `exclude_regexes` / `exclude_gitignore_lines` / `max_file_bytes` / `use_apfs_snapshot` / `retention` |

### 9.4 Compatibility verification

| Feature | PR | Location |
| --- | --- | --- |
| Shape fingerprint helpers | #7 | `tests/test_fingerprint.py` (salt-independent structural comparison) |
| Real Arq.app SFTP integration test harness | #9 | `tests/integration/`, `.env.example`, `docs/COMPAT-SFTP-TESTING.md` |
| Arq.app v8 Mach-O RE confirms Tree v4 + Node init signatures | #41 | `docs/C1-MACHO-RE-PLAN.md` §"Findings (2026-05-10 RE session)", `tests/test_arq_app_tree_version_pin.py` |

### 9.5 Operational hardening (PRs #36–#41)

The following six PRs landed after the 9-group autonomous chain
(#28–#35) closed; together they connect previously-built modules to
their actual call sites and harden the walker against silent
corruption.

| PR | Feature | Location |
| --- | --- | --- |
| #36 | Wire-up bundle 2: AuditLedger → `run_full_audit`, `notify_run_finished` → `RunWriter.__exit__`, `estimate_for_plan` → `BackupRunScreen` precheck, `macos_progress` toasts → BackupRun lifecycle, `secrets_setup` → DestinationModal checkbox, sidebar `section_for_screen` helper | `arq_tui/runs.py:347`, `arq_tui/screens/backup_run.py:170`, `arq_tui/widgets/destination_modal.py`, `arq_validator/tiers.py:_audit_one_file`, `arq_validator/cli.py` (`--incremental`, `--ledger-path`) |
| #37 | Walker safety: silent 0-byte corruption → explicit `file_read_error` / `file_stat_error` events + `Backup.files_with_errors` counter + `BackupResult.files_with_errors` field | `arq_writer/backup.py:_walk_file`, `arq_writer/backup.py:BackupResult`, `tests/test_walker_safety.py` |
| #38 | Restore `--list-only` dry-run (`Restore.dry_run_restore` + `DryRunRestoreResult`) + `PlanRegistry.mark_run` stamping last_run_iso on worker finish/fail | `arq_reader/restore.py`, `arq_reader/cli.py`, `arq_tui/state.py:PlanRegistry`, `arq_tui/screens/backup_run.py:_stamp_plan_last_run` |
| #39 | Validator record-tier ledger integration: `validate_record(ledger=…)` + `_check_one_loc` short-circuit on `ledger.contains(blob_id)` + `--ledger-prune-days N` flag | `arq_validator/record_validator.py`, `arq_validator/cli.py` |
| #40 | `PriorTreeIndex._tree_cache` LRU-bounded (default 1024 trees, `max_cache_trees=` ctor kwarg, `ARQ_PRIOR_TREE_CACHE_MAX` env override). 100k-tree destinations: ~100× memory reduction. | `arq_writer/prior_tree.py`, `tests/test_prior_tree_cache_bound.py` |
| #41 | C1 Mach-O RE findings against operator's Arq.app v8: `nodeTreeVersion = 4` confirmed hard-coded in `-[BackupRecord init]`; `scannedAt` is NOT a Node property (38-byte trailing block is serializer-only metadata). | `docs/C1-MACHO-RE-PLAN.md`, `tests/test_arq_app_tree_version_pin.py` |

## 10. Future work (currently unimplemented)

The following items are deliberately left **deferred** within the scope
of this document.

### 10.1 Additional backends

S3, Backblaze B2, WebDAV, Dropbox, etc. The full existing logic can be
reused by implementing the 7 methods of the `Backend` protocol
(`list_dir`/`stat_size`/`read_range`/`read_all`/`exists`/`is_dir`/`unlink`
+ the `mkdir`/`write_all` used by the writer).

### 10.2 Writer — backup creation (background)

The `arq_writer/` package provides a v0 backup writer — adopting the
strategy recommended by the research
(`docs/RESEARCH-backup-creation-feasibility.md`): "bypass chunker / pack
containers, store every object as a standalone EncryptedObject under
`standardobjects/<shard>/<blobid>`."

#### Writer execution flow

1. Generate random 32-byte `encryption_key` / `hmac_key` / `blob_id_salt`
   → `encryptedkeyset.dat` (PBKDF2-SHA256 / AES-256-CBC / HMAC)
2. Write the 4 root JSON files (`backupconfig.json`, `backupplan.json`,
   `backupfolders.json`, and one `backupfolder.json` per folder)
3. Recursively walk the source directory:
   - Files: contents → LZ4 wrap → ARQO encrypt →
     `standardobjects/<2hex>/<62hex>`
     (blob_id = `SHA-256(blob_id_salt || plaintext)`)
   - Directories: gather child nodes → serialize Tree binary → store the
     same way as above
4. Embed the root TreeNode in a backuprecord plist (binary plist) → LZ4
   wrap → ARQO encrypt →
   `backupfolders/<folder>/backuprecords/<NNNNN>/<num>.backuprecord`

Byte-identical files share the same SHA-256 blob_id, providing natural
dedup. Files modified in place do not dedup because there is no chunker
(acceptable for an operator tool).

#### Compatibility verification status

| Verdict | Status | Basis |
| --- | --- | --- |
| **A**: Round-trip with this validator | ✅ pass | `tests/test_writer_e2e.py` covers all 4 tiers (dry-run / quick / deep / audit) |
| **A'**: Byte-identical restore with this reader | ✅ pass | `tests/test_reader_e2e.py` — backups built by the writer round-trip via reader and pass `diff -r` |
| **B**: arq_restore (BSD) round-trip | ⚠️ unverified | arq_restore depends on macOS-only APIs (CommonCrypto, Security framework, Mach headers, Apple xattr APIs) — porting to Linux is estimated to be a multi-day effort. Direct verification by the operator on macOS required |
| **C**: Arq.app GUI restore | ⚠️ unverified | Manual verification on macOS GUI required |

This reader's byte-identical restore is its own assurance that all of the
writer's binary formats are consistent across the spec, validator, and
reader. arq_restore is written against the same official spec, so passing
the reader strongly implies arq_restore would pass too — but a formal
guarantee requires building arq_restore on macOS and verifying directly.

A test (`test_backuprecord_decrypts_and_parses_as_plist`) that unwraps
every layer of binary plist + LZ4 + ARQO produced by the writer and
confirms the plist keys (`archived`, `arqVersion`, `node`,
`treeBlobLoc.blobIdentifier`, etc.) match the spec also passes.

#### Known limitations (current)

- **windowsattrs / xattr / ACL metadata zero-filled**: Default behavior
  preserves only file contents and basic stat. The node builder can be
  extended if needed.
- **Consistency snapshots on non-macOS**: Snapshots outside APFS (Linux
  btrfs/LVM, Windows VSS) are unsupported. On macOS, frozen-source backups
  are available with `--use-apfs-snapshot`.

> The **chunker** and **pack containers** previously listed as limitations
> were implemented in PR #5 and are activated via the CLI
> `--chunker {none|default|arq_v7_41}` / `--use-packs`. The Arq.app v7.41
> parameters were derived through Mach-O RE
> (`macho_buzhash_finder.py`). See § 10 "Already-implemented extensions"
> for detailed implementation status.

### 10.3 Hetzner-specific safeguards

The reference's automatic connection-rate-limit detection (tracking
`Connection refused` and `mux_client_request_session` patterns, early
abort after 20 consecutive failures) has not yet been ported to
SftpBackend. Will be added in a generalized form for operators using
non-Hetzner destinations.

### 10.4 Arq 5/6 writing (write-side)

Arq 5/6 currently only supports **read/restore** (`arq_reader/arq5_*.py`).
A writer that creates backups in Arq 5/6 format is not implemented. Since
Arq.app itself creates all new backups in Arq 7 format, this is low
priority.

### 10.5 Consistency snapshots — non-macOS

Currently only macOS APFS is supported (`with_apfs_snapshot()`). Linux
btrfs / LVM thin / ZFS, and Windows VSS are not yet implemented. Will be
added incrementally based on operator-environment filesystem types.

### 10.6 Scheduling and automatic execution

Automatic application of retention policies, cron / launchd integration
for audit-drip, periodic execution of backups — all of these are
currently manual operator invocations. Will be added in a future PR as a
policy layer.

## 11. References

- Arq 7 data format (official): <https://www.arqbackup.com/documentation/arq7/English.lproj/dataFormat.html>
- Arq 5 format (source for the reused PBKDF2/HMAC rules): <https://www.arqbackup.com/arq_data_format.txt>
- Reference implementation for this validator: `neoocean/docker-monitor` →
  `scripts/arq-validate.py` (2,673 LOC)
- Primary source of reverse-engineered correction values: live
  measurements in the operator environment (Hetzner Storage Box)
  (`docker-monitor` SCENARIO § 13, 2026-05-04 ~ 2026-05-05)
