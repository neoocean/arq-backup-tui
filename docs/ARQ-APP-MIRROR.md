# Arq.app mirror — reading a local Arq 7 install

When the operator already runs **Arq.app** on the same machine, the TUI
mirrors what the Arq GUI shows — the same backup **destinations**, the
same backup **plans**, and the same **activity log** — so the two tools
feel nearly synchronised. With only one of the two present, each keeps
working independently (the mirror is simply absent).

This is implemented by `arq_tui/arq_app.py` (a read-only adapter) plus
small merge hooks in the home / backup-set / runs-monitor screens.

## 1. Where Arq keeps its data

Arq 7's backup agent stores everything in one SQLite database:

    /Library/Application Support/ArqAgent/server.db

It is root-owned but **world-readable** (`-rw-r--r--`) and uses
`journal_mode=delete` (no WAL sidecar), so a read-only connection sees a
consistent view while the agent writes. Three tables are mirrored:

| Arq table            | Rows mean                  | Maps to            |
|----------------------|----------------------------|--------------------|
| `storage_locations`  | backup destinations        | `state.Destination`|
| `backup_plans`       | backup plans               | `state.Plan`       |
| `activities`         | activity log (per run)     | `runs.RunRecord`   |

Both `storage_locations` and `backup_plans` carry the real config in a
per-row **`json`** column; the older scalar columns are mostly empty in
current Arq versions. The `backup_plans.json` blob is the *same* 47-key
`backupplan.json` schema this project already reads/writes, with
`backupFolderPlansByUUID` holding the source folders.

## 2. Strictly read-only

The adapter never writes to `server.db`:

- The agent owns the file and writes to it live; injecting our own
  writes would race with it and risk corruption.
- It is root-owned, so we couldn't write without `sudo` anyway.

We open `file:…?mode=ro` (with a short busy-timeout), falling back to a
private snapshot **copy** of the DB if the agent ever holds a write lock
— so a busy agent can never block the TUI from showing its data.

**Secrets never appear here.** Encryption / SFTP passwords live in the
macOS Keychain + root-only sidecar files; the DB only carries a
`hasPassword` boolean. Acting on a mirrored plan therefore still goes
through the TUI's normal session password prompt (`CredentialCache`).

## 3. Field mapping

### Storage location → Destination

`providerType` decides the kind:

- `folder` → `kind="local"`. The path `<mount>:<rel>` (e.g.
  `/Volumes/arqbackup1:/`) is resolved to a local fs root.
- `sftp` → `kind="sftp"` (hostname / port / username / path).
- `arqpremium` / `s3` / `wasabi` / … (cloud) → **listed but not
  openable** (`to_destination()` returns `None`). Cloud backends are
  out of scope (README §1).

### Backup plan → Plan

- `plan_id` = `planUUID` — matches the real Arq layout where the
  top-level destination folder is named by the planUUID, so running a
  mirrored plan through our writer lines the folder up
  (`computer_uuid == plan_uuid == planUUID`).
- `sources` = each `backupFolderPlansByUUID[*].localPath`.
- `chunker` follows `useBuzhash` exactly as the writer's GAP-L logic:
  `True → arq_v7_41`, `False → fixed-40m`.
- `exclude_globs` / `exclude_regexes` aggregate the per-folder
  `wildcardExcludes` / `regexExcludes`; `skip_tm_excludes` is the OR of
  per-folder `skipTMExcludes`.
- `retention` maps `retain{Hours,Days,Weeks,Months}` →
  `keep_{hourly,daily,weekly,monthly}`; `retainAll` → empty (keep all).
- `use_apfs_snapshot` ← `useAPFSSnapshots`.

### Activity → RunRecord

`is_running` (not finished + not aborted) → `RUNNING`; `aborted` →
`CANCELLED`; finished with `error_count` → `FAILED`; else `COMPLETED`.
Progress comes from `processed/total` files+bytes. The plan UUID is
resolved to its plan name for display.

## 4. How it surfaces in the TUI

- **Detection** (`detect_arq_app`): requires the `Arq.app` bundle **and**
  a readable `server.db`. The app auto-detects only in a real session;
  tests run with an isolated `config_dir` and get no mirror unless they
  inject an `ArqAppSource` fixture, so the operator's real Arq plans
  never leak into test assertions.
- **Unified list + origin badge** (operator's choice): mirrored items
  share the home plan list / backup-set destination list / activity
  monitor with the operator's own, tagged `origin="arq"` and badged
  `◆ Arq`. An own item with the same id/coordinates wins (no dupes), so
  the operator can "adopt" an Arq plan by saving their own copy.
- **Read-only guards**: editing a mirrored plan is refused (steer to
  Arq.app); cancelling a mirrored run is refused (Arq's agent owns it);
  running a mirrored *cloud* plan is refused (out of scope). A mirrored
  local / SFTP plan **is** runnable — it goes through the standard
  password prompt and writes to the same destination Arq uses, byte-
  compatibly.

## 5. Code + tests

- Adapter: `arq_tui/arq_app.py` (`ArqAppSource`, `ArqStorageLocation`,
  `ArqPlan`, `ArqActivity`, `detect_arq_app`).
- Screen hooks (land with the M9 sidebar shell):
  `screens/home.py::_load_plans`,
  `screens/backup_sets.py::_merged_destinations`,
  `screens/runs_monitor.py::_arq_run_records`.
- Adapter tests: `tests/test_tui_arq_app.py` (hermetic fixture DB + a
  real-DB smoke test that auto-skips without an Arq install). The
  screen-level merge / badge / read-only-guard tests ship with M9.

## 6. One-way only — Arq never sees TUI-created config

The mirror is strictly Arq → TUI. There is no write-back, so anything the
TUI creates stays private to the TUI:

- A backup **plan** created in the TUI lives in
  `~/.config/arq-backup-tui/plans/` and never appears in the Arq GUI;
  Arq won't run it.
- A **storage location** added in the TUI is private to the TUI too.
- `◆ Arq` plans / locations are read-only here (edit in Arq.app);
  deleting a *TUI-owned* location only forgets the entry, never deletes
  backup data.

The one shared layer is the **on-disk backup data**: the writer is
byte-compatible with Arq 7, so a destination the TUI backs up to is a
valid Arq 7 backup set. Point Arq.app at that destination and its restore
browser can read the records — but Arq still won't show the plan that
produced them. See README "Coexistence with Arq.app".
