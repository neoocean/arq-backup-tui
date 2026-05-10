# HANDOFF — session continuity notes

This file is a session-to-session bridge. The previous Claude session
ended on 2026-05-10 after merging PR #42 (docs sweep). The next
session continues on a host with **local access to an `arqbackup1`
destination** (Arq.app's actual output, on a local filesystem or NAS),
which unlocks several previously-blocked items.

If you're a human reading this and want the autoritative state of the
project, see `CHANGELOG.md` (auto-generated from git) and
`docs/COVERAGE.md` (feature matrix). This file is the **operational**
state — what's queued up, what's blocked on what, what to do first.

## Current state (2026-05-10 session end)

- **Main branch:** `175ce90` "Merge pull request #42 from
  neoocean/claude/docs-update"
- **Recent merge sequence:**
  - #36 Wire-up bundle 2 (E3+F1+F2+F3+F5+Sidebar)
  - #37 Walker safety (silent corruption → explicit events)
  - #38 Restore `--list-only` + Plan.last_run_iso stamping
  - #39 Validator record-tier ledger integration
  - #40 PriorTreeIndex LRU bound (default 1024 trees)
  - #41 C1 Mach-O RE findings against Arq.app v8
  - #42 Docs sweep (10 files, 0 stale refs)
- **CI baseline:** all 7 checks green on every recent PR (Python 3.9
  + 3.11 + 3.12 × 2 jobs + GitGuardian).
- **P4 sync:** confirmed (CL 50880, 50924, 50976, 51035 etc.).

Quick verification:

```sh
git log --oneline -5
# expected first line: 175ce90 Merge pull request #42 ...

export ARQ_BACKUP_TUI_DISABLE_NOTIFICATIONS=1 ARQ_TUI_SKIP_DISK_PRECHECK=1
python3 -m unittest tests.test_wireup_bundle_2 tests.test_walker_safety \
    tests.test_restore_dry_run_and_plan_stamp tests.test_record_tier_ledger \
    tests.test_prior_tree_cache_bound tests.test_arq_app_tree_version_pin
# 35 tests, all PASS in <2s

python3 scripts/check_doc_links.py
# scanned 20 markdown files with 335 code-shaped references
# no stale references found
```

## Pending — operator-environment-required tasks

These were blocked in the previous session because:

1. The operator's SFTP destination was too slow for bulk operations
   (~30min for 50 nodes).
2. We had no local Arq.app destination to cross-check our writer
   against.

The next session has **local arqbackup1 access**, so all four
become unblocked.

### Priority 1 — Bulk xattr probe at scale (A1-(b))

`scripts/probe_xattr_blob_bulk.py` validates the `XAttrSetV002`
format hypothesis (RE'd in PR #25 from a single 68-byte sample)
across many real xattr blobs. Earlier session confirmed the
format on 1 blob × 30 nodes; we want 1000+ blobs × 100+ nodes
to be confident.

```sh
# Point the script at the local destination — either via .secrets/
# (operator workflow) or by pointing arq_validator.LocalBackend at
# the destination root path directly.
python3 scripts/probe_xattr_blob_bulk.py --max-walk 1000 --json \
    > /tmp/xattr-probe-1k.json 2> /tmp/xattr-probe.err

# Inspect the report:
python3 -c "
import json
d = json.load(open('/tmp/xattr-probe-1k.json'))
print('observed:', d['total_xattr_blobs_observed'])
print('decoded cleanly:', d['decoded_cleanly'])
print('format dist:', d['format_distribution'])
print('anomalies:', len(d['anomalies']))
"
```

**Expected** (per `docs/XATTR-BULK-PROBE.md`):
- `format_distribution: {"XAttrSetV002": N}` for ALL N
- `decoded_cleanly == total_xattr_blobs_observed`
- `anomalies: []`

**On any anomaly:**
1. Capture failing blob hex from `samples_first_5`.
2. Add as fixture in `tests/test_xattrs.py::SerializeRoundTripTests::test_deserialize_real_operator_blob`.
3. Refine `arq_writer/xattrs.py` accordingly.

### Priority 2 — End-to-end cross-restore (Arq.app → our reader)

Proves that backups Arq.app produced are restorable via our reader.
This is the strongest compatibility signal.

```sh
python3 -m arq_reader list /path/to/arqbackup1 --password-env ARQ_PW
# pick a small folder uuid

# Dry-run first (no I/O):
python3 -m arq_reader restore /path/to/arqbackup1 \
    --password-env ARQ_PW \
    --list-only <folder-uuid> /tmp/dummy

# Actual restore + verify:
python3 -m arq_reader restore /path/to/arqbackup1 \
    --password-env ARQ_PW --verify-after \
    <folder-uuid> /tmp/restored
```

If `--verify-after` reports `failures == []`, the cross-restore
worked end-to-end.

### Priority 3 — Fingerprint diff (our writer vs Arq.app's output)

```sh
# Back up the same source via Arq.app to /tmp/arq-app-out
# Then back up the same source via our writer:
python3 -m arq_writer create /tmp/test-source --dest /tmp/our-out \
    --password-env ARQ_PW --use-packs --chunker arq_v7_41

# Compare structural fingerprints (salt-independent):
python3 -m arq_validator fingerprint /path/to/arqbackup1 > /tmp/arq-app.fp
python3 -m arq_validator fingerprint /tmp/our-out > /tmp/our.fp
diff /tmp/arq-app.fp /tmp/our.fp
```

A clean diff confirms the structural shape match. See
`tests/test_fingerprint.py` for the underlying invariant.

### Priority 4 — Incremental ledger end-to-end smoke

```sh
# First sweep (empty ledger, walks everything):
python3 -m arq_validator audit /path/to/arqbackup1 \
    --password-env ARQ_PW --incremental

# Second sweep (everything ledger-skipped):
python3 -m arq_validator audit /path/to/arqbackup1 \
    --password-env ARQ_PW --incremental
# Should show files_skipped_by_ledger ≈ files_total

# Optional: prune entries older than 30d
python3 -m arq_validator audit /path/to/arqbackup1 \
    --password-env ARQ_PW --incremental --ledger-prune-days 30
```

Ledger lives at `~/.local/state/arq-backup-tui/audit-ledgers/<target>.json`.

## Pending — out-of-scope without operator action

- **C1 cross-OS** (Windows/Linux Arq.app variants emit same Tree v4
  shape?): only macOS Arq.app accessible. Defer until operator has
  cross-platform Arq.app installs.
- **C1 dynamic RE** (lldb attach + breakpoint on `[Tree writeData:]`):
  static-analysis findings sufficient (PR #41); skip unless a
  compatibility bug surfaces.

## Known landmines

- **Auto-mode classifier** sometimes blocks `gh pr merge` even on
  confirmed-green CI. Workaround: re-run `gh pr checks N` to confirm
  green explicitly, then retry merge.
- **`tests/test_tui_m7_advanced.py::test_use_apfs_snapshot_falls_back_on_linux`**
  fails on macOS (test name says "on linux" but expects Linux fallback;
  on macOS the APFS path tries `tmutil localsnapshot` which needs sudo).
  Pre-existing; not caused by our changes. Passes in CI on Linux.
- Full `unittest discover` takes ~140s; some subprocess-spawning tests
  can hang if the subprocess isn't reaped. Prefer running specific test
  modules.
- Never `git stash` without explicit user approval (auto-mode classifier
  blocks).
- The `.secrets/sftp.json` file holds operator credentials. NEVER
  echo / cat / print its contents. Reading it via the secrets-loader
  helper is fine.

## Workflow conventions (from prior sessions)

See `~/.claude/projects/<this-repo>/memory/user-workflow-preferences.md`
for the canonical list. Highlights:

- One PR per logical unit; descriptive commit messages with Co-Authored-By
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
- `docs/XATTR-BULK-PROBE.md` — operator guide for the probe (priority 1)
- `docs/PLAN-cli-tui-split.md` — Scenarios A–F + event taxonomy
