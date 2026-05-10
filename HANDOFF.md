# HANDOFF — session continuity notes

This file is a session-to-session bridge. **The 2026-05-10 session
closed the four operator-required priorities (P1–P4) from the
prior handoff.** P1–P2 unlocked compatibility evidence at scale;
P3 surfaced five concrete writer-side incompatibilities + one
fingerprint module bug (fixed); P4 verified the incremental audit
ledger end-to-end.

If you're a human reading this and want the authoritative state of
the project, see `CHANGELOG.md` (auto-generated from git) and
`docs/COVERAGE.md` (feature matrix). This file is the
**operational** state — what's queued up, what's blocked on what,
what to do first.

## Current state (2026-05-10 session end)

- **Main branch:** `50bdb35` "Fingerprint: collapse UUID-keyed
  maps + log P3 findings (#46)"
- **Recent merge sequence (this session):**
  - #44 Probe — `--local-root` + dict-shape `_fetch_blob` fix
        (P1 unblocker; surfaced a hidden bulk-probe bug while
        validating xattrs at scale).
  - #45 COMPAT-VERIFICATION — 2026-05-10 cross-restore log under
        Strategy B (P2 documentation: 127,222 files restored
        byte-perfect, `verify.failures: []`).
  - #46 Fingerprint — collapse UUID-keyed maps + log P3 findings
        under Strategy A §2.7 (fixes a salt-independence bug
        surfaced by the P3 schema-level diff).
- **CI baseline:** all 4 checks green on every recent PR
  (Python 3.9 + 3.11 + 3.12 + GitGuardian).
- **P4 sync:** runs at the git-mirror machine after each merge.

Quick verification:

```sh
git log --oneline -5
# expected first line: 50bdb35 Fingerprint: collapse UUID-keyed maps...

export ARQ_BACKUP_TUI_DISABLE_NOTIFICATIONS=1 ARQ_TUI_SKIP_DISK_PRECHECK=1
python3 -m unittest tests.test_fingerprint tests.test_probe_xattr_blob_bulk
# 20 tests, all PASS in <2s

python3 scripts/check_doc_links.py
# scanned 21 markdown files with 349 code-shaped references
# no stale references found
```

## Verified against the operator's real Arq.app v8 destination

The operator's destination (`/Volumes/arqbackup1`,
9 backup folders, 1 computer subtree, ~415k standardobjects + 38k
blobpacks + 15k treepacks + 64k largeblobpacks) was used as the
ground-truth reference for P1–P4. Findings landed across the three
PRs above; concise summary:

| Priority | Result | PR / artifact |
|---|---|---|
| **P1** Bulk xattr probe at scale | `XAttrSetV002` confirmed across **n=21,318** xattr blobs (was n=1 from PR #25), 0 anomalies, 4 distinct attr names; dict-shape `_fetch_blob` bug fixed along the way. | #44, `docs/XATTR-BULK-PROBE.md` §2026-05-10-follow-up |
| **P2** Cross-restore (Arq.app → our reader) | Smallest folder (`402790CC`, 127,222 files / 3.17 GB) restored byte-perfect with `--verify-after`; `verify.failures: []`. | #45, `docs/COMPAT-VERIFICATION.md` §3.4 |
| **P3** Fingerprint diff | Schema-level diff completed (full per-record diff intractable at this scale); 5 incompatibilities + 1 fingerprint module bug surfaced; bug fixed. | #46, `docs/COMPAT-VERIFICATION.md` §2.7 |
| **P4** Incremental ledger smoke | Pass-1 ledger of 2,522 blobs created → pass-2 reports `files_skipped_by_ledger: 2,522` exactly; `files_fail: 0`, `inner_arqos_ok: 53,507/53,507`. | (no PR — operational verification only) |

## Pending — writer-side compatibility fixes from P3 §2.7.3

Each is small, independent, has a clear before/after in `docs/COMPAT-VERIFICATION.md`, and ships as a separate PR with a regression test. Together they're the work needed to unblock Strategy C (writer → arq_restore).

### T1 — ARQO-encrypt sidecar JSON

`backupplan.json` and per-folder `backupfolder.json` must be wrapped in an `ARQO` envelope on write. Arq.app v8 does this; our writer emits plain JSON. The decrypt side already exists (`arq_reader.decrypt.decrypt_encrypted_object`). Note: the inner payload is **plain UTF-8 JSON, NOT LZ4-compressed** — distinct from the `decrypt_lz4_arqo` path used for tree blobs.

```sh
# To validate the discovery for yourself:
python3 -c "
import os, sys; sys.path.insert(0, '.')
from arq_validator.backend import LocalBackend
from arq_validator.crypto import decrypt_keyset
from arq_validator.layout import keyset_path
from arq_reader.decrypt import decrypt_encrypted_object
from tests.integration._creds import load_dest_password
b = LocalBackend('/Volumes/arqbackup1')
cu = '2DAC24D1-DA89-46C4-8B26-DE7A4D1DE019'
ks = decrypt_keyset(b.read_all(keyset_path('/', cu)), load_dest_password())
blob = b.read_all(f'/{cu}/backupplan.json')
print('magic:', blob[:4])  # b'ARQO'
print('decrypted head:', decrypt_encrypted_object(blob, ks.encryption_key, ks.hmac_key)[:60])
"
```

### T2 — Add 10 missing `backupplan.json` keys

Compared to Arq.app v8, our writer's plan template omits:
`backupFolderPlanMountPointsAreInitialized`,
`backupSetIsInitialized`, `budgetGB`, `createdAtProConsole`,
`datalessFilesOption`, `managed`, `objectLockAvailable`,
`objectLockUpdateIntervalDays`,
`preventBackupOnConstrainedNetworks`,
`preventBackupOnExpensiveNetworks`. Most are `bool` or `int` with
obvious defaults; `objectLockUpdateIntervalDays` is the only
ambiguous one (operator-side it's `0`).

Edit `arq_writer/json_configs.py`'s plan template + add them to the
fingerprint's expected schema.

### T3 — Add `s3GlacierIRObjectDirs` to `backupfolders.json`

Single missing key on our side — Arq.app v8 emits it as an empty
list `[]`. Companion to the existing
`s3DeepArchiveObjectDirs` / `s3GlacierObjectDirs` /
`standardIAObjectDirs` / `onezoneIAObjectDirs` keys.

### T4 — Replace `errorCount: int` with `backupRecordErrors: list`

Arq.app v8's backuprecord plist tracks errors as a structured list
of error objects; ours emits a single integer count. Other 18/19
keys agree, so this is a focused diff. Need to verify the
per-error structure (operator's records have `backupRecordErrors:
[]` for the latest folder we sampled, so the schema-of-empty-list
isn't enough — sample a record from a folder where errors actually
occurred, e.g. by introducing one in a synthetic test).

### T5 — Plan: full per-record fingerprint diff (when feasible)

The 2026-05-10 attempt at a full fingerprint of
`/Volumes/arqbackup1` was killed at 1h22min still walking — too
slow for a single session at 350+ records × ~23k files each. The
schema-only extractor was used as a workaround. To enable a future
full diff:

- Add `--max-records-per-folder N` (or `--latest-record-only`) to
  `arq_validator.fingerprint_cli compute`.
- Or land record-level `--include-paths`/`--exclude-paths` so the
  extractor can target a synthetic source the operator backed up
  via Arq.app GUI to a fresh small destination (Strategy A.1–A.5
  workflow).

## Pending — out-of-scope without further operator action

- **C1 cross-OS** (Windows/Linux Arq.app variants emit same Tree
  v4 shape?): only macOS Arq.app accessible. Defer until operator
  has cross-platform Arq.app installs.
- **C1 dynamic RE** (lldb attach + breakpoint on
  `[Tree writeData:]`): static-analysis findings sufficient
  (PR #41); skip unless a compatibility bug surfaces.
- **Full byte-level Strategy A diff** (writer vs Arq.app on the
  *same* synthetic source): needs the operator to perform an
  Arq.app GUI backup of a small synthetic source to a fresh
  destination, then ship the destination root for diffing.

## Known landmines

- **macOS env-specific failures** (CI passes on Linux):
  - `tests/test_xattrs.py::FilesystemRoundTripTests::test_capture_returns_empty_for_file_with_no_xattrs`
    fails on recent macOS because the OS auto-attaches
    `com.apple.provenance` to files Python's tempfile creates.
  - `tests/test_sftp_backend_wiring.py` (1 test) and
    `tests/test_record_tier_ledger.py::RecordTierLedgerSkipsTests::test_ledger_short_circuits_already_known_blob_ids`
    fail for the same root cause (xattr drift between dedupe runs).
  - `tests/test_tui_m7_advanced.py::test_use_apfs_snapshot_falls_back_on_linux`
    fails on macOS because it tries `tmutil localsnapshot` which
    needs sudo. Pre-existing.
- **Auto-mode classifier** sometimes blocks `gh pr merge` even on
  confirmed-green CI. Workaround: re-run `gh pr checks N` to
  confirm green explicitly, then retry merge.
- **`--audit-max-runtime-sec` is a soft cap.** The 60s value used
  in the 2026-05-10 P4 verification actually took ~430s (audit
  walks chunks of work between runtime checks). Plan for ~5–10×
  the requested cap when bounding wall time.
- Full `unittest discover` takes ~140s; some subprocess-spawning
  tests can hang if the subprocess isn't reaped. Prefer running
  specific test modules.
- Never `git stash` without explicit user approval (auto-mode
  classifier blocks).
- The `.secrets/sftp.json` file holds operator credentials. NEVER
  echo / cat / print its contents. Reading it via the
  secrets-loader helper is fine. The encryption password file is
  `.secrets/dest_password` (was typo'd as `desc_password`; renamed
  on 2026-05-10).

## Workflow conventions (from prior sessions)

See `~/.claude/projects/<this-repo>/memory/user-workflow-preferences.md`
for the canonical list. Highlights:

- One PR per logical unit; descriptive commit messages with
  Co-Authored-By
- D5 (PyPI packaging) deliberately excluded from autonomous chains
- Korean responses fine for status updates; English for code/docs
- Always offer A/B/C choice at decision points, don't silently pick
- Confirm CI green before merging even if user said "merge"

## Where to find more context

- `CHANGELOG.md` — every PR entry, auto-generated
- `DESIGN.md` §9.5 — new operational hardening summary table
- `docs/COVERAGE.md` — feature matrix vs. Arq 5/6/7
- `docs/MECHANISM.md` Appendix D — how the recent operational
  hardening fits into the existing flows
- `docs/REAL-DATA-DISCOVERIES.md` §7 — Tree v4 trailing block + RE
- `docs/C1-MACHO-RE-PLAN.md` — full Mach-O RE transcript from PR #41
- `docs/XATTR-BULK-PROBE.md` — operator guide for the probe
  (P1 verification log under §2026-05-10-follow-up)
- `docs/COMPAT-VERIFICATION.md` — Strategies A–G; **§2.7** is the
  P3 verification log + writer-side roadmap, **§3.4** is the P2
  cross-restore log
- `docs/PLAN-cli-tui-split.md` — Scenarios A–F + event taxonomy
