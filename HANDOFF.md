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

- **Main branch:** `a1a7d27` "F1 + F2: per-file structured error
  collection + Tree v4 record shape (#51)"
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
  - #47 HANDOFF — record P1–P4 closure + writer-side roadmap.
  - #48 T2 + T3 + T5 — `backupplan.json` 10 missing keys,
        `backupfolders.json` `s3GlacierIRObjectDirs`,
        `arq-fingerprint compute --max-records-per-folder N`.
  - #49 T1 + T4 — ARQO-encrypt `backupplan.json` + per-folder
        `backupfolder.json`, replace `errorCount: int` with
        `backupRecordErrors: list`.
  - #50 HANDOFF — record T1–T5 closure + queue F1–F3.
  - #51 F1 + F2 — per-file structured error collection +
        backuprecord ``version=101``/``nodeTreeVersion=4``
        coupling with Tree v4.
- **CI baseline:** all 4 checks green on every recent PR
  (Python 3.9 + 3.11 + 3.12 + GitGuardian).
- **P4 sync:** runs at the git-mirror machine after each merge.

Quick verification:

```sh
git log --oneline -8
# expected first line: a1a7d27 F1 + F2: per-file structured error...

export ARQ_BACKUP_TUI_DISABLE_NOTIFICATIONS=1 ARQ_TUI_SKIP_DISK_PRECHECK=1
python3 -m unittest \
    tests.test_sidecar_encryption \
    tests.test_backuprecord_errors \
    tests.test_walker_safety \
    tests.test_json_configs \
    tests.test_fingerprint \
    tests.test_probe_xattr_blob_bulk
# all PASS

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

## All five P3 follow-ups (T1–T5) — closed in this session

The 2026-05-10 schema diff surfaced five writer-side compatibility
gaps. All are now landed:

| ID | Title | PR | Status |
|---|---|---|---|
| **T1** | ARQO-encrypt `backupplan.json` + per-folder `backupfolder.json` | #49 | ✅ |
| **T2** | Add 10 missing `backupplan.json` keys | #48 | ✅ |
| **T3** | Add `s3GlacierIRObjectDirs` to `backupfolders.json` | #48 | ✅ |
| **T4** | Replace `errorCount: int` with `backupRecordErrors: list` | #49 | ✅ |
| **T5** | `arq-fingerprint compute --max-records-per-folder N` | #48 | ✅ |

After-T1–T5 schema diff against `/Volumes/arqbackup1`:

| Sidecar / plist | Before T-series | After T-series |
|---|---|---|
| `backupconfig.json` | 11/11 keys match | ✅ 11/11 |
| `backupplan.json` | 37/47 keys match (10 missing) | ✅ **47/47** |
| `backupfolders.json` | 5/6 keys match (1 missing) | ✅ **6/6** |
| per-folder `backupfolder.json` | 8/8 keys match (but plain) | ✅ **8/8** + ARQO-encrypted |
| backuprecord plist | 18/19 keys match (`errorCount` ↔ `backupRecordErrors`) | 18/19 (`nodeTreeVersion` / `volumeName` differ per folder Tree v3 vs v4) |

`backupplan.json` and per-folder `backupfolder.json` now ARQO-wrap
their JSON the way Arq.app v8 does. `read_sidecar` in
`arq_validator/sidecar.py` auto-detects the envelope so older
plain-JSON destinations our writer produced before T1 keep parsing
unchanged.

## All seven post-P3 follow-ups (T1–T5 + F1–F2) — closed

| ID | Title | PR | Status |
|---|---|---|---|
| **T1** | ARQO-encrypt `backupplan.json` + per-folder `backupfolder.json` | #49 | ✅ |
| **T2** | Add 10 missing `backupplan.json` keys | #48 | ✅ |
| **T3** | Add `s3GlacierIRObjectDirs` to `backupfolders.json` | #48 | ✅ |
| **T4** | Replace `errorCount: int` with `backupRecordErrors: list` | #49 | ✅ |
| **T5** | `arq-fingerprint compute --max-records-per-folder N` | #48 | ✅ |
| **F1** | Per-file structured error collection (5 walk sites) | #51 | ✅ |
| **F2** | Tree v4 → record `version=101` + `nodeTreeVersion=4` | #51 | ✅ |

After-T1–T5 + F1–F2 schema parity vs `/Volumes/arqbackup1`:

| Sidecar / plist | Status |
|---|---|
| `backupconfig.json` | ✅ 11/11 keys |
| `backupplan.json` | ✅ **47/47 keys** + ARQO-encrypted |
| `backupfolders.json` | ✅ **6/6 keys** |
| per-folder `backupfolder.json` | ✅ **8/8 keys** + ARQO-encrypted |
| backuprecord plist (v101 / Tree v4) | ✅ **20/20 keys** |

Every top-level JSON sidecar AND the backuprecord plist match
Arq.app v8 schema-for-schema when the writer uses Tree v4.
Strategy C (writer → arq_restore) is **schema-level unblocked**;
only byte-level differences (random IVs, salt-based blob_ids,
content-derived chunker boundaries) remain — and those are
expected design properties, not compatibility gaps.

## Remaining for full Strategy C parity

### F3 — Full byte-level Strategy A diff (operator GUI required)

The schema-only diff is now zero. The remaining verification is
the **byte-level fingerprint diff** against an Arq.app-produced
destination of the **same source**:

1. Pick a small synthetic source (~10 files / 1 MB).
2. Back it up via Arq.app GUI to a fresh local destination.
3. Back up the same source via our writer (``--use-packs
   --chunker arq_v7_41 --tree-version 4``) to another fresh
   destination.
4. ``arq-fingerprint compute --max-records-per-folder 1`` on
   each (the ``--max-records-per-folder`` flag landed in T5).
5. ``arq-fingerprint compare`` — expect ``match: true`` with
   zero ``chunk_pattern_diffs`` / ``file_shape_diffs`` /
   ``missing_files_*`` entries.

This is operator-side work because we can't drive Arq.app's GUI
from here. With the schema-level diffs all closed by T1–T5 + F1
+ F2, F3 is the last remaining check before declaring full
Strategy C compatibility.

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
