# Arq GUI round-trip compatibility suite

A re-runnable harness that checks byte-level interoperability with **the
installed Arq.app** in both directions, across many file-edge-case
scenarios, and **accumulates a per-version result matrix**. Run it after
every Arq.app update to catch format incompatibilities a new version might
introduce.

- Driver: `scripts/arq_compat/run.py`
- Scenarios: `scripts/arq_compat/scenarios.py`
- Accumulating results: `docs/arq-compat/MATRIX.md` (one row per run) +
  `docs/arq-compat/runs/<version>_<date>.md` (per-run detail) +
  `docs/arq-compat/baselines/<version>.fp.json` (format-drift baselines)

## What it tests

### Three legs

| Leg | Direction | Automation | What it proves |
|---|---|---|---|
| **Direction A** | our writer → Arq | **semi-auto** | a destination our writer emits is readable/restorable as Arq-7 format |
| **Direction B** | Arq → our reader | **auto** (needs one-time plan) | a destination Arq.app creates is restored byte-perfectly by our reader |
| **Format drift** | Arq's emit vs prior version | **auto** | a new Arq version hasn't changed the on-disk destination schema/shape |
| **server.db schema drift** | Arq's local config DB vs prior version | **auto** | a new Arq version hasn't changed the local `server.db` schema the read-only mirror depends on |

### Automation boundary (important)

`arqc` (Arq.app's bundled CLI, `/Applications/Arq.app/Contents/Resources/arqc`)
can **start a backup** (`startBackupPlan`) but has **no restore command**.
So:

- **Direction B is fully automatable** — we drive `arqc startBackupPlan`, then
  detect completion precisely from `arqc latestBackupActivityJSON`
  (`finishedTime` + `message=="Idle"` + not `aborted`, distinct from the prior
  run), cross-check Arq's `server.db` `activities` row, and **capture Arq's own
  `errorCount` / `maxErrorSeverity`** into the report. Then our reader restores
  Arq's output and diffs it per scenario. (`--computer-uuid` targets one plan
  in a shared destination; `--skip-backup` reuses a GUI-run backup.)
- **Direction A's *Arq-reads-our-output* leg cannot be CLI-driven.** The
  harness covers it three ways:
  1. **our reader round-trip** (auto) — our writer's emit must be
     self-restorable, content byte-identical;
  2. **patched `arq_restore`** (auto, optional) — an *independent* Arq-spec
     reader (`scripts/arq_restore_v4/`); build once with `build.sh`;
  3. **real Arq.app GUI restore** (manual, ~2 min) — the harness prints the
     exact steps and then `confirm-gui-restore` diffs the operator's restore.
     Pass `--plan-uuid` to also attach Arq's own restore-activity evidence
     (from the `activities` table: `restore_destination` / `error_count` /
     finished) so the pass rests on both our diff and Arq's success record.

### Scenarios (`scenarios.py`)

ascii · unicode NFC · unicode NFD · empty (0-byte) · binary/all-byte-values ·
12 MB multi-blob · **41 MB crossing the 40,000,000-byte fixed-chunk boundary**
· deep nesting · special names (spaces/emoji/200-char) · extended attributes ·
symlinks (rel+abs) · hardlinks · sparse file · varied permission bits. Each
lives in its own `<scenario>/` subdir so results are scored per scenario. Add
scenarios by appending to `SCENARIOS`.

Direction A runs each scenario through three writer configs — `v4-buzhash`,
`v4-fixed` (Arq.app's `useBuzhash=False` 40 MB chunker), and `v3-buzhash`
(Tree v3) — so tree-version × chunker combinations are covered.

A `PASS*` in the matrix means content was byte-identical but a filename came
back NFC/NFD-normalised — an Arq restore-side behaviour (Arq decomposes to
NFD on restore; our reader preserves the stored form), not a data gap.

## One-time setup

1. **Build the optional `arq_restore` proxy** (independent Direction-A check):
   ```sh
   scripts/arq_restore_v4/build.sh      # clones upstream + applies patch + clang
   ```
2. **Generate the fixtures, then create the Direction-B round-trip Arq plan**
   (the only manual setup; `arqc` cannot create plans).
   - First materialise the corpus so the source path exists:
     ```sh
     python3 scripts/arq_compat/run.py direction-a   # also runs Direction A
     ```
     The default workdir is **`<repo>/arq_compat_run/`** (under the project
     directory, **not** `/tmp` — the Arq.app GUI folder picker can't reach
     `/tmp`). Run artifacts there are git-ignored.
   - In Arq.app: New backup plan, **source =
     `<repo>/arq_compat_run/fixtures`** (`<repo>` = this project's directory).
     For the **destination** either:
     - a fresh local-folder storage location (cleanest), **or**
     - an existing storage location (e.g. `arqbackup1`) — Arq 7 isolates each
       plan in its own `<planUUID>/` folder with its own keyset, so the test
       data never mixes with real backups. When the destination is shared,
       pass `--computer-uuid <planUUID>` so the reader/fingerprint target the
       round-trip plan's folder (not a real one).
   - Note the plan UUID (`arqc listBackupPlans`) + the destination root + its
     encryption password. When the destination folder name == planUUID (Arq's
     layout), `--computer-uuid` is that same UUID.

## Running (every Arq version)

The Arq encryption password is read from a file (`--arq-pw-file`, default
`.secrets/dest_password`) — never passed inline, so it never lands in `ps`.
Internally every reader/writer/fingerprint subprocess receives the password
through its environment (`--password-env`), not on the command line. The
default `--workdir` is `<repo>/arq_compat_run` (omit it to use that).

```sh
# Automatable legs + report + matrix (Direction A + drift baseline):
python3 scripts/arq_compat/run.py all \
    --arq-dest /Volumes/arqbackup1            # --arq-pw-file defaults to .secrets/dest_password

# Full Direction B (after the one-time round-trip plan exists).
# `--skip-backup` reuses a backup you already ran in the GUI (otherwise the
# suite triggers `arqc startBackupPlan`); `--computer-uuid` targets the plan's
# folder when the destination is shared with real backups.
python3 scripts/arq_compat/run.py all \
    --plan-uuid <ROUND-TRIP-PLAN-UUID> \
    --arq-dest /Volumes/arqbackup1 \
    --computer-uuid <ROUND-TRIP-PLAN-UUID> --skip-backup \
    --arq-pw-file /path/to/round-trip-plan-password

# Direction-A GUI leg (manual): in Arq.app add a writer_* dir from
# <repo>/arq_compat_run as a storage location, restore it to <dir>, then:
python3 scripts/arq_compat/run.py confirm-gui-restore --restored <dir>
```

The version is auto-detected from `Arq.app/Contents/Info.plist`. Each run
writes/refreshes `runs/<version>_<date>.md` and appends a row to `MATRIX.md`.
The drift baseline for the current version is stored under `baselines/`; the
next version's run diffs against it automatically and flags any schema change.

## Periodic / scheduled use

`run.py all` is **idempotent per Arq version**: on start it auto-detects the
installed Arq.app version and, **if a report already exists for it
(`runs/<version>_*.md`), prints a notice and exits 0 without doing any work.**
It only runs the checks + generates a report when it sees a version it hasn't
tested yet. So it is safe to invoke on a schedule (cron / `launchd` /
`/loop`) — it stays quiet until an Arq.app update appears, then captures that
version's row automatically. Pass `--force` to re-run a version that already
has a report. Add **`--notify`** to fire a macOS notification on any FAIL /
drift (content failure, Arq-side `errorCount`, or `server.db` schema drift —
the benign plan-config fingerprint polymorphism is not alarmed), so an
unattended run surfaces a regression without watching the log.

```sh
# cron-friendly: no-op until a new Arq version shows up
*/30 * * * *  cd <repo> && python3 scripts/arq_compat/run.py all \
    --plan-uuid <UUID> --arq-dest /Volumes/arqbackup1 \
    --computer-uuid <UUID> --notify >> /tmp/arq_compat.log 2>&1
```

A ready-to-edit **launchd** LaunchAgent is provided at
`scripts/arq_compat/com.arqcompat.runner.plist.example` (replace the
`REPO_DIR` / `PLAN_UUID` / `ARQ_DEST` / `PW_FILE` placeholders, copy to
`~/Library/LaunchAgents/`, `launchctl load`). It runs `all … --notify` every
6 hours; idempotency keeps it quiet until an Arq update lands.

## Interpreting drift

`Format drift = none` means the JSON sidecar / record / blob schema Arq emits
is unchanged from the prior captured version. `DRIFT` lists the added/removed/
changed keys (see the run report's `drift_detail`) — investigate before
trusting the new Arq version for round-trips, and update the writer/validator
to match if the change is real.
