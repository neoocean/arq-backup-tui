# PLAN — TUI implementation plan

This document is the implementation plan for the **TUI frontend**, the
core stage of the `arq-backup-tui` project. The libraries (validator /
reader / writer) are nearly complete, so this document describes the
design and stage-by-stage implementation plan for the interactive
interface that sits on top of them.

Target features (user request):

1. Backup plan configuration (source folders, destination, password,
   chunker, etc.)
2. Backup execution
3. Backup progress display
4. Local + remote (SFTP) backup set viewing
5. Browsing records inside a backup set (tree walk, metadata)
6. Restore
7. Restore progress display
8. Backup validation (L0/L1a/L1b/L2/audit-drip)

## 1. Non-goals

The following are not addressed in this stage — separate decisions or
requests will reopen the discussion.

- **Automatic scheduling** (cron-like): policy layer, OS-specific. The
  TUI only supports immediate user-triggered execution.
- **Cloud backends** (S3 / B2 / Dropbox, etc.): out of scope per the
  scope decisions in COVERAGE.md.
- **Mid-backup pause/resume**: the consistency guaranteed by pack flush
  boundaries is preserved, but no TUI-level pause UI is provided.
- **GUI notifications / menubar integration**: TUI only.
- **i18n infrastructure**: Korean first, future decision.

## 2. Tech stack decisions

### 2.1 TUI library

Candidates:

| Library | Pros | Cons |
|----------|------|------|
| **Textual** | reactive widgets, async-native, mouse support, CSS-style theming, devtools, snapshot tests | Adds dependencies (rich + its dependencies) |
| **urwid** | mature, mature event loop | Many widgets must be hand-built; async integration is heavy |
| **prompt-toolkit** | excellent forms / command line | Multi-screen layout is extra work |
| **stdlib `curses`** | 0 deps | Every widget has to be hand-written — too costly for this scope |

**Decision: adopt Textual.** Reasons:

- All library code in this project stays stdlib-only, and the Textual
  dependency is **isolated inside the TUI package (`arq_tui`)**.
  Third-party users embedding the validator / reader / writer libraries
  are unaffected.
- The progress callback model (`ProgressCb(kind, payload)`) maps 1:1 onto
  Textual's reactive attributes. The event streams of backup / restore /
  validate wire directly into reactive widgets, making live updates
  natural.
- The Textual `pilot` (headless test driver) lets every screen be
  verified in CI without interaction.
- Mouse + keyboard support; tree / table / Modal widgets available out of
  the box.

### 2.2 Dependency-add policy

`pyproject.toml`:

```toml
[project.optional-dependencies]
test = []
tui = ["textual>=0.50"]   # new
```

Install: only adds `pip install -e ".[tui]"`. The CLI / library has **no
dependency changes**.

### 2.3 Password / SFTP credential storage

- Option A: prompt every use (most secure, most inconvenient)
- Option B: OS keyring (the `keyring` library, optional dep)
- Option C: encrypted config file

**Decision: A by default, B optionally enabled.** To avoid adding a new
dependency, always prompt; if the user installs the `[tui-keyring]`
extra and turns it on in settings, use the keyring. Neither path stores
plaintext.

## 3. Package structure

```
arq_tui/
├── __init__.py              # Exposes ArqTuiApp
├── __main__.py              # python -m arq_tui
├── app.py                   # Defines ArqTuiApp(textual.App)
├── backend_open.py          # LocalBackend / SftpBackend open/close
├── cli.py                   # plans list/show/delete headless subcommand
├── screens/
│   ├── __init__.py
│   ├── home.py              # Dashboard
│   ├── plan_wizard.py       # New backup plan flow (6 steps, including Advanced)
│   ├── backup_run.py        # Backup execution + progress
│   ├── backup_sets.py       # Local/remote destination list + [m] enters maintenance
│   ├── record_browser.py    # Tree walk inside one record
│   ├── restore_run.py       # Restore execution + progress
│   ├── validate_run.py      # 4-tier validation + audit-drip
│   ├── maintenance.py       # Password rotation + retention apply (PR #12)
│   └── help.py
├── widgets/
│   ├── __init__.py
│   ├── progress_panel.py    # Shared by backup / restore / validate
│   ├── source_picker.py     # Multi-select source folders
│   ├── destination_modal.py # Local path / SFTP entry modal
│   ├── password_modal.py    # Password prompt modal
│   └── restore_target_modal.py
├── state.py                 # Plan / Destination / PlanRegistry / DestinationStore / CredentialCache
├── workers.py               # Threaded work + ProgressCb bridge (BackupWorker / RestoreWorker / ValidateWorker)
└── theming.css              # Colors, padding, etc. CSS

# Plus, at the repo root:
arq-tui.py                   # Root entry point (run immediately with ./arq-tui.py)
```

`arq_tui/` is a **consumer** that imports `arq_validator` /
`arq_reader` / `arq_writer`. There are no library → TUI imports.

## 4. Screen catalog

Every screen is a Textual `Screen` subclass. Navigation uses the
`app.push_screen` / `pop_screen` stack.

### 4.1 Home (`home.py`)

Layout:

```
┌─ arq-backup-tui ──────────────────────────────────────────┐
│                                                            │
│  Plans                                                     │
│  ─────────────────────────                                 │
│  ▶ home-laptop-to-nas        last run: 2026-05-08 03:14   │
│    docs-to-sftp              never run                     │
│    + New plan                                              │
│                                                            │
│  Quick actions                                             │
│  ─────────────────────────                                 │
│    Browse backup sets   [b]                                │
│    Validate destination [v]                                │
│    Quit                 [q]                                │
│                                                            │
└────────────────────────────────────────────────────────────┘
```

Key bindings: `n` new plan, `r` run, `b` browse, `v` validate, `q` quit.

State: load plans from `state.PlanRegistry`. Each plan's last-run time is
determined from the mtime of the most recent backuprecord at the
destination (only metadata that can be obtained without prompting).

### 4.2 Plan wizard (`plan_wizard.py`)

A `Screen` with 6 steps (PR #12 expanded from 5 → 6 steps):

1. **Sources** — `SourcePicker` widget. Multi-select in a tree view.
   Accumulates the absolute paths the user selects.
2. **Destination** — `DestinationPicker`:
   - Local: directory picker
   - SFTP: host / port / user / authentication method (password /
     identity_file) / remote root path
3. **Encryption** — Password input (with confirmation, masked). Cached
   in `CredentialCache` for the session so no mid-run prompt occurs.
4. **Chunker** — Radio buttons:
   - "Generic Buzhash (default)" — `ChunkerConfig()` defaults
   - "Match Arq.app v7.41" — imports `arq_writer.arq_chunker_params`
   - "No chunking (single blob per file)" — `chunker_config=None`
   + Storage layout (packs vs standalone) + Cross-run dedup (on/off)
   radio buttons.
5. **Advanced** (PR #12) — all optional:
   - Exclude wildcards / regexes / .gitignore lines (TextArea each, one
     pattern per line)
   - Skip files larger than (bytes; blank = unlimited)
   - Use APFS snapshot (macOS only; falls back automatically on
     non-macOS)
   - Retention policy: keep_last_n / keep_daily / keep_weekly /
     keep_monthly / keep_yearly
6. **Review + Save** — show summary, enter plan name, save.

Storage location: `~/.config/arq-backup-tui/plans/<plan-id>.json`
(password not stored; SFTP credentials follow the § 2.3 policy).

### 4.3 Backup run (`backup_run.py`)

Entry: from Home → select plan → "Run" → password prompt (if needed) →
this screen.

Layout:

```
┌─ Backup: home-laptop-to-nas ──────────────────────────────┐
│ ┌─ Progress ──────────────────┐ ┌─ Stats ──────────────┐  │
│ │ ▓▓▓▓▓▓▓▓░░░░░░░░░░░░░░ 42 % │ │ Files written:  1234 │  │
│ │                              │ │ Files reused:    987 │  │
│ │ Current file:                │ │ Bytes plain:    2 GB │  │
│ │   /home/.../foo.bin          │ │ Bytes on disk:  1 GB │  │
│ │                              │ │ Dedup ratio:   1.97x │  │
│ │ Throughput:  12 MB/s         │ │ Packs flushed:    14 │  │
│ │ ETA:         00:03:14        │ │ Trees written:   124 │  │
│ └──────────────────────────────┘ └──────────────────────┘  │
│ ┌─ Live events (last 50) ──────────────────────────────┐  │
│ │ 03:14:22 file_written /a/b.txt size=1234 chunks=1    │  │
│ │ 03:14:22 file_reused  /a/c.txt rel_path=a/c.txt      │  │
│ │ ...                                                    │  │
│ └────────────────────────────────────────────────────────┘  │
│                              [Esc] cancel    [Enter] OK     │
└────────────────────────────────────────────────────────────┘
```

Internal behavior:

- `workers.run_backup(plan, password, callback)` calls
  `arq_writer.build_backup` via `asyncio.to_thread`.
- The callback pushes events to the main loop via
  `app.call_from_thread(self._on_event, kind, payload)`.
- `Reactive` counters propagate to widgets automatically.
- On completion, the BackupResult summary is shown in a modal.
- Cancel: `Esc` → cancel event to the worker → after the in-progress
  chunk finishes, no pack flush + no partial backuprecord written (safe
  abort).

### 4.4 Backup set list (`backup_sets.py`)

Source: registered destinations (extracted from plans) + recently
manually entered destinations.

Layout: destinations list on the left; on the right, the
computer_uuid → folder_uuid → backuprecord tree for the selected
destination.

```
┌─ Backup sets ─────────────────────────────────────────────┐
│ Destinations           │ /Volumes/arqbackup1               │
│ ─────────────────────  │ ───────────────────────────────── │
│ ▶ /Volumes/arqbackup1  │ ▼ A714-... (laptop)               │
│   sftp:hetzner:/store  │   ▼ Folder 1: home-laptop-to-nas  │
│   + Open destination   │     ◦ 2026-05-08 03:14 [latest]   │
│                        │     ◦ 2026-05-07 03:14            │
│                        │     ◦ 2026-05-06 03:14            │
│                        │   ▶ Folder 2: docs-to-...         │
│                        │ ▶ B832-... (workstation)          │
│ [a] add  [v] validate  [m] maintenance  [Esc] back         │
└────────────────────────────────────────────────────────────┘
```

Key binding `[m]` (PR #12): enter `MaintenanceScreen` (§ 4.8) using the
cached password for the current destination.

Library usage:

- Local: `arq_validator.layout.discover_layout(LocalBackend(path), "/")`
- SFTP: `discover_layout(SftpBackend(host=..., root=path), "/")`

Record metadata (creation_date, file_count, etc.) is displayed using the
information populated by `Restore.layouts()` plus the `creationDate`
field of the backuprecord.

### 4.5 Record browser (`record_browser.py`)

Tree walk of a selected backuprecord. Tree blobs are fetched lazily and
expanded like a filesystem tree.

Layout: tree on the left; metadata of the selected node on the right.

```
┌─ Record: 2026-05-08 03:14 (home-laptop-to-nas / A714-...)─┐
│ Tree                       │ Selected                       │
│ ─────────────────────────  │ ─────────────────────────────  │
│ ▼ /home/me                 │ Path: /home/me/Documents/      │
│   ▼ Documents              │       resume.pdf               │
│     ◦ resume.pdf           │ Size: 234,567 bytes            │
│     ◦ taxes/               │ mtime: 2026-04-22 11:03        │
│   ▶ Pictures               │ Mode: 0644                     │
│   ▶ Videos                 │ Blob ID: 0dde...f15f15         │
│                            │ Chunks: 3 dataBlobLocs         │
│                            │   [0] packed offset=0  len=...│
│                            │   [1] packed offset=... len=..│
│                            │   [2] packed offset=... len=..│
│ [r] mark-restore  [Esc] back  [Space] expand                │
└────────────────────────────────────────────────────────────┘
```

Multiple items can be marked with `r` and then routed to the Restore
screen.

Library usage:

- Reuse the lazy-walk logic of `Restore` instance +
  `arq_writer.prior_tree.PriorTreeIndex` (or extract a separate
  `RecordWalker`).
- Tree-blob fetch results are cached in memory until the screen exits.

### 4.6 Restore run (`restore_run.py`)

Entry:

- Marked items in the record browser → "Restore selected" → this screen,
  or
- Home → Plans → "Restore latest" shortcut path (full-folder restore).

Layout: same progress panel as backup_run plus separate stats:

```
Files restored:  ###      Bytes restored:    ###
Symlinks set:    ###      Errors:             ###
ETA:           ##:##      Throughput:    ## MB/s
```

Internal behavior: `arq_reader.Restore.restore` + callback. The backend
is reused from the backup-set screen.

### 4.7 Validate run (`validate_run.py`)

Layout: tier picker at the top (`L0`/`L1a`/`L1b`/`L2`/`audit-drip`),
progress in the middle, event log at the bottom.

Additional fields in audit-drip mode:
- state file path
- throttle (max bytes/s, max wall-clock)
- pause / resume buttons

### 4.8 Maintenance (`maintenance.py`, PR #12)

Entry: select a destination in the backup-set browser, then press `[m]`.
Two operational tasks are offered on one screen — both reuse the
destination's cached password and the already-open backend, so no
mid-flow credential re-entry is needed.

1. **Rotate keyset password** — enter current and new passwords, then
   press the "Rotate password" button. Internally,
   `arq_writer.rotate_keyset_password(blob, old_password, new_password)`
   re-encrypts only `<computer-uuid>/encryptedkeyset.dat` (the
   (encryption_key, hmac_key, blob_id_salt) triple is preserved).
   Existing backuprecords / blobs remain decryptable. After the operation
   the `CredentialCache` is updated with the new password.
2. **Apply retention** — enter keep_last_n / keep_daily / keep_weekly /
   keep_monthly / keep_yearly + a "Dry run / Real run" radio + a "Run
   blob GC after pruning" toggle. Calls `apply_retention(backend,
   encryption_password=..., policy=RetentionPolicy(...), run_gc=...,
   dry_run=...)`. Callback events (`record_deleted`, `blob_deleted`,
   `pack_deleted`) stream into the log panel at the bottom of the screen.

Both tasks run in sibling threads and their results are marshaled back
to the event loop with `call_from_thread` — the UI does not lose
responsiveness.

## 5. Progress callback integration

The libraries already use a consistent `ProgressCb(kind: str, payload:
dict)` model (across writer / reader / validator).

### 5.1 Worker thread → UI bridge

```python
# arq_tui/workers.py
async def run_backup(plan, password, app):
    def callback(kind, payload):
        # textual.App.call_from_thread is thread-safe to invoke
        # from any worker.
        app.call_from_thread(app.post_message, BackupEvent(kind, payload))
    result = await asyncio.to_thread(
        arq_writer.build_backup,
        source=plan.sources[0],
        dest_root=plan.dest,
        encryption_password=password,
        callback=callback,
        # ... + chunker_config, dedup_against_existing, etc.
    )
    app.post_message(BackupFinished(result))
```

`BackupEvent` / `BackupFinished` are Textual `Message` subclasses. The
relevant screen receives them in an `on_backup_event` handler and updates
its reactive attributes.

### 5.2 Reactive progress widget

```python
# arq_tui/widgets/progress_panel.py
class ProgressPanel(Widget):
    files_written = reactive(0)
    files_reused = reactive(0)
    bytes_plaintext = reactive(0)
    current_file = reactive("")
    ...

    def on_event(self, kind: str, payload: dict) -> None:
        if kind == "file_written":
            self.files_written += 1
            self.current_file = payload["path"]
            self.bytes_plaintext += payload["size"]
        elif kind == "file_reused":
            self.files_reused += 1
        # ... etc.
```

Each reactive change triggers Textual to re-render the affected widget
region automatically.

### 5.3 Throughput / ETA computation

`workers` pushes a `ThroughputTick` message every second. ProgressPanel
computes EMA over a deque(60) window.

## 6. Persistent state

### 6.1 Directory layout

```
~/.config/arq-backup-tui/
├── config.toml             # Global settings (theme, whether keyring is enabled)
├── plans/
│   ├── <plan-uuid>.json    # one plan = one file
│   └── ...
├── recent_destinations.json
└── audit_drip/
    └── <state-file>.json   # validator audit_drip state
```

### 6.2 Plan JSON schema

Final form after PR #12 added the Advanced step fields:

```json
{
  "plan_id": "UUID",
  "name": "home-laptop-to-nas",
  "sources": ["/home/me/Documents", "/home/me/Pictures"],
  "destination_kind": "local",      // "local" | "sftp"
  "destination": {
    "path": "/Volumes/arqbackup1"
  },
  "chunker": "arq_v7_41",           // "default" | "arq_v7_41" | "none"
  "per_source_chunkers": {},        // optional: source path → chunker name
  "use_packs": true,
  "dedup_against_existing": true,
  "exclude_globs": ["*.log", "__pycache__"],
  "exclude_regexes": [],
  "exclude_gitignore_lines": ["build/", "!build/keep.txt"],
  "max_file_bytes": null,           // null = no limit; integer = cutoff
  "use_apfs_snapshot": false,        // macOS only; falls back automatically on non-macOS
  "retention": {                    // empty dict = keep everything
    "keep_last_n": 10,
    "keep_daily": 7,
    "keep_weekly": 0,
    "keep_monthly": 0,
    "keep_yearly": 0
  },
  "last_run_iso": "2026-05-08T03:14:22Z"
}
```

Existing (M3) plan JSON loads with backward compatibility using
default-empty values for missing advanced fields (guaranteed by the
regression test `test_legacy_plan_loads_with_default_advanced_fields`
added in PR #12).

For SFTP destinations:

```json
"destination_kind": "sftp",
"destination": {
  "host": "u123.your-storagebox.de",
  "port": 23,
  "user": "u123",
  "identity_file": "~/.ssh/id_ed25519",
  "path": "/home/u123/arq"          // remote root (consistent field name)
}
```

### 6.3 Password handling

- Held in memory only (for the duration of the TUI session); never
  written to disk.
- Prompted only once for consecutive operations against the same
  destination — session cache.
- Optional: `keyring` integration (separate extra dep, the user must
  install it explicitly).

## 7. Error handling

- Library exceptions are caught in the worker and forwarded to the UI as
  an `ErrorEvent(message, traceback)` message.
- Modal error dialog — return to the previous screen on confirm.
- SFTP connection failures are retried with backoff (up to 3 times),
  after which the user is offered retry / cancel.
- Partial failures during long-running tasks like L2 / audit-drip: mark
  the affected record as failed and proceed (bisect-friendly).

## 8. Implementation milestones

Each milestone is independently pushable, and provides some user value
when complete.

### M1 — Skeleton (1 day)

- `arq_tui/` package + Textual App entry point
- `theming.css` (default colors / fonts / key bindings)
- `Home` screen (static placeholder)
- `tui` extra in `pyproject.toml` + `arq-tui` console script
- `tests/test_tui_m1_smoke.py` — verify app start/stop with `pilot`

### M2 — Backup set viewing (2 days)

- `BackupSetListScreen` + `RecordBrowserScreen`
- Both local and SFTP destinations supported (already backend-aware)
- `widgets/tree_view.py`
- Password prompt modal
- Integration test: verify records / tree exposure on a pre-built backup
  destination

At this point the user can **inspect a backup destination**.

### M3 — Backup execution (2 days)

- `PlanWizardScreen` (5 steps)
- `BackupRunScreen` + `progress_panel.py`
- `workers.run_backup` bridge
- Plan save/load (`state.PlanRegistry`)
- Integration test: synthetic source tree → run backup → verify progress
  events → restore round-trip

At this point the user can **create a new backup + execute**.

### M4 — Restore (1.5 days)

- `RestoreRunScreen` (full-folder + selected-paths)
- Selected nodes from record_browser → restore flow
- Reuse the progress panel

### M5 — Validation (1.5 days)

- `ValidateRunScreen` (4 tiers + audit-drip)
- audit-drip throttle / state-file UI
- pause / resume

### M6 — Polish (1 day)

- Tidy up key bindings
- Color theme + dark/light toggle
- Consistent error dialogs
- Keyring integration (optional)
- Empty-state messages, loading spinners, and other micro-UX

### M7 — Advanced features (subsequently shipped)

Originally not in the M1–M6 plan, the following landed in PRs
following the M-series:

- `MaintenanceScreen` (`[m]` from backup-set browser): retention apply
  + dry-run toggle, blob GC toggle, password rotation
- `RunsMonitorScreen` (`[a]ctivity` / `:activity`): passively watches
  state files written by CLI / cron / TUI processes
- Plan editing on `[e]`: PlanWizardScreen pre-populated with existing
  plan; saves overwrite same `plan_id`
- Slash-command console (quake-style slide-down): `:browse`, `:activity`,
  `:validate`, `:plan`, …
- `Sidebar` widget with `section_for_screen()` helper: Arq.app-style
  left rail with active-section highlight in lockstep with the open screen
  (the per-section *launcher* model here was later superseded by the M9
  persistent shell — see below; the `section_for_screen()` helper survives
  as a standalone sidebar utility)
- `SchedulingScreen`: install/remove cron + launchd entries for plans
  + auto-gc schedule for finished-runs cleanup
- `BackupRunScreen` integrations (PR #36): disk-precheck on mount,
  macOS Notification Center toasts at start / 10% milestones / completion,
  `_stamp_plan_last_run` on worker finish/fail
- `DestinationModal` "Save SFTP credentials to .secrets/sftp.json"
  checkbox (PR #36): one-click sync of typed credentials so cron
  workflows can reuse them

**Total estimate: 8–9 days for M1–M6.** M7 features were added
incrementally across ~30 subsequent PRs.

### M8 — Arq.app mirror (read-only sync with a local Arq 7 install) — ✅ shipped (PR #199 / CL 56798)

When Arq.app is installed on the same machine, the TUI mirrors what the
Arq GUI shows — the same destinations, plans, and activity log — by
reading Arq's `ArqAgent/server.db` read-only. Mirrored items join the
home plan list / backup-set destinations / runs monitor in a single
unified list, badged `◆ Arq`, and are guarded read-only (edit / cancel
refused; cloud plans not runnable; local + SFTP plans runnable through
the normal password prompt). When Arq isn't installed the mirror is
simply absent and the TUI works standalone.

Full design + field mapping: **`docs/ARQ-APP-MIRROR.md`**. Module:
`arq_tui/arq_app.py`; tests: `tests/test_tui_arq_app.py` +
`tests/test_tui_arq_mirror.py`.

### M9 — persistent sidebar shell (master-detail navigation) — ✅ shipped (PR #200 / CL 56799)

The sidebar is no longer a launcher that pushes a fresh full screen
per section. Instead `HomeScreen` is a **persistent shell**: a fixed
`Sidebar` on the left drives a `ContentSwitcher` on the right, and
selecting a section swaps only the right-hand panel in place — the
macOS-Arq source-list model. The sections map to reusable panel
widgets in the switcher:

| Sidebar section    | Panel id         | Widget |
|--------------------|------------------|--------|
| Backup Plans       | `panel-plans`    | inline plans list + quick actions |
| Activity Log       | `panel-activity` | `ActivityPanel` (runs_monitor) |
| Storage Locations  | `panel-browse`   | `StoragePanel` (backup_sets) |
| Validate           | `panel-validate` | `ValidatePanel` (signpost → Storage) |
| Help               | `panel-help`     | `HelpPanel` |

The complex sections keep their standalone `Screen` wrappers
(`RunsMonitorScreen`, `BackupSetListScreen`) for the slash-command
console + direct pushes; the panel widget holds the UI + logic and is
shared by both. Transient flows (plan wizard, backup / restore /
validate runs, record browser, maintenance, scheduling, modals) stay
as pushed overlays that pop back to the shell.

**Sidebar interaction.** The sidebar is focusable, so `Tab` /
`Shift+Tab` include it in the focus cycle (it's the initial focus).
The arrow keys (`up` / `down`) move a keyboard cursor (`-cursor`
highlight, reverse-video — not a colour) between rows; `Enter` /
`Space` commits the cursor row as the active section. Mouse click
selects directly. Selecting a section also moves focus into the new
panel so its keys work immediately.

**Command palette removed.** `ENABLE_COMMAND_PALETTE = False` — the
generic Textual `Ctrl+P` palette is off; this TUI uses its own
slash-command console + sidebar instead.

## 9. Test strategy

### 9.1 Unit tests

- Verify each screen's reactive logic (event → state transition) headless
  via Textual `pilot`.
- `state.PlanRegistry` is plain unittest.

### 9.2 Integration tests

- `tests/test_tui_m3_backup.py`: end-to-end of plan create → execute →
  restore. The library is invoked for real; the backup destination is a
  temporary directory.
- `tests/test_tui_m2_browser.py`: load a pre-built destination, then
  expand tree nodes + verify metadata display.
- `tests/test_tui_m5_validate.py`: run all 4 tiers and verify the
  result screen.

### 9.3 SFTP integration

- `tests/test_sftp_backend_wiring.py`: inject a mocked SftpBackend
  (LocalBackend on a temp dir) and verify the destination_picker → list
  → record browser flow.
- Real SFTP server tests follow the `tests/test_sftp.py` pattern,
  with full end-to-end coverage in
  `tests/integration/test_arq_real_destination.py` (requires
  `.secrets/sftp.json` — see `docs/COMPAT-SFTP-TESTING.md`).

### 9.4 Snapshot tests

Use Textual's `pilot.snapshot()` to save ASCII snapshots of major
screens to guard against regressions.

## 10. Library-side enhancements (all implemented)

Small library gaps surfaced while embedding into the TUI — all
implemented:

1. **`Restore.list_records(folder_uuid) -> List[RecordInfo]`** ✅
2. **`Restore.restore(*, backuprecord_path=...)` option** ✅
3. **`Restore.restore(*, paths=[...])` option** ✅
4. **`Backup.cancel()`** ✅
5. **PR #12 additions**: the following were also added for
   `MaintenanceScreen` integration:
   - `arq_writer.rotate_keyset_password(blob, old_password, new_password)`
   - `arq_writer.apply_retention(...)` + `RetentionPolicy`
   - `Backend.unlink()` (LocalBackend + SftpBackend)
   - `arq_writer.with_apfs_snapshot()` + `NotMacOSError` (PR #8)
   - `arq_writer.ExclusionRules` + `Backup(exclusions=..., max_file_bytes=...)`

## 11. Resolved decisions

- **Local Korean UI alongside?** → Korean labels + English key-binding
  text hard-coded.
- **mtime / mode restore?** → Both ✅ implemented (`os.utime` +
  `os.chmod`).
- **Multi-folder plan with multiple sources?** → ✅ multi-source
  supported from M3 (`Backup.add_folder` is multi-folder per plan; the
  wizard also accepts multiple source inputs).
- **Plan edit / delete UI?** → Delete is ✅ (`arq-tui plans delete`);
  edit is deferred to v1.x (recreate via wizard + delete recommended;
  direct JSON editing possible).

## 12. Security / privacy notes

- Passwords: not persisted to disk. While retained in memory, used as
  `bytes` only; any `str`-level caching is `del`-ed at the moment of
  escape.
- Logs / screen captures: the `path` field in `payload` exposes user file
  paths, so always redact in snapshot tests.
- When SFTP keyring is enabled, locking is via the user's OS auth — the
  TUI does not add its own auth logic.

---

This plan will be implemented sequentially from M1 after user approval.
Each milestone end will get its own commit + push, and the TUI row of
COVERAGE.md will be updated incrementally from ❌ → ✅.
