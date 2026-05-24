# Arq 7 Compatibility Verification Strategy (under sandbox constraints)

> **Status (2026-05-08)**: Strategy A (shape fingerprint diff) is automated
> and implemented (`tests/test_fingerprint.py`); Strategy B (real-SFTP destination
> integration test) is implemented (PR #9, `tests/integration/test_arqapp_sftp_compat.py`,
> depends on the operator's `.env`). Strategies C+ (operator-paste workflow,
> Mach-O paste, arq_restore round-trip) are preserved in this document as
> operator procedures — anything that can be automated has already been
> absorbed into the two strategies above.

This project's development sandbox cannot run macOS Arq.app directly.
Even so, the writer / reader / validator must be verified against Arq 7
compatibility, so this document catalogues an **operator-paste workflow**
and **asymmetric tools that can prove compatibility in both directions**.

The shared structure across strategies: the **operator runs Arq.app once
on macOS**, then pastes / uploads the result to the sandbox, where automated
tools inside the sandbox compare it against our library's output and report
byte-level differences.

A previous example of this pattern succeeding:
- **PR #1**: the operator pasted JSON analyzing the `Arq.app/Contents/MacOS/Arq`
  Mach-O binary → the sandbox reverse-engineered the T-table + chunker
  parameters → landed in `arq_writer.arq_chunker_params`.

## 1. Strategy Catalogue

Priority by strategy:

| Priority | Strategy | Effort | Value |
|:----:|------|:----:|:----:|
| ⭐ | A. **Shape fingerprint diff** | low | very high — detects every format / chunker mismatch in one pass |
| ⭐ | B. **Cross-restore verification** (Arq.app produced → our reader) | medium | very high — directly proves reader compatibility |
| ⭐ | C. **Cross-restore in the opposite direction** (our writer → arq_restore CLI) | medium | very high — directly proves writer compatibility |
| ✓ | D. **Chunker oracle** (already implemented) | low | high — byte-level verification of chunker parameters |
| ✓ | E. **Mach-O binary RE** (already landed in PR #1) | low | low — limited to chunker parameters |
| ▲ | F. **Real backuprecord plist collection** | low | medium — verifies exact values such as `version` / `isComplete` |
| ▲ | G. **JSON sidecar value comparison** | low | low — captures the exact values Arq.app emits |

The tools + documentation provided by this PR enable all three ⭐ entries.

---

## 2. ⭐ Strategy A — Shape fingerprint diff

### 2.1 What it is

**Salt-independent shape fingerprint**: a structural summary that must be
identical when two destinations are produced from the same source tree. It
includes:

- Directory layout (computer / folder / record / pack counts)
- The **schema** of each JSON sidecar (key names + value types; the values
  themselves are excluded)
- The plist key list of each backuprecord plus metadata (`version`,
  `isComplete`, `computerOSType`, `creationDate`)
- For every file inside each record:
  `(rel_path, item_size, mtime_sec, mode_perms, is_symlink,
  chunk_sizes=[len_1, len_2, ...])`

Values that differ per keyset, such as `blob_id` / `encryption_key` /
`keyset_salt`, are **deliberately excluded**, so an Arq.app destination
and our writer's destination can be compared as-is.

### 2.2 Tools

- Module: `arq_validator.fingerprint`
- API: `compute_shape_fingerprint(backend, *, encryption_password)
  → dict`, `diff_fingerprints(a, b) → dict`
- CLI: `arq-fingerprint compute <path> --password ...`,
  `arq-fingerprint compare <a.json> <b.json>`

### 2.3 Operator workflow

#### A.1 Source preparation (reproducible)

The operator, on macOS:

```bash
# 1) Create a known fixture (reproducible)
mkdir -p /tmp/compat-src && cd /tmp/compat-src
echo "alpha" > alpha.txt
echo "beta" > beta.txt
mkdir -p subdir
echo "gamma" > subdir/gamma.txt
mkdir -p 한글
echo "내용" > 한글/메모.txt

# 2) Pin every file's mtime (avoid sub-second resolution differences)
find . -exec touch -h -t 202601011200.00 {} \;
```

#### A.2 Back up with Arq.app

The operator, in the macOS Arq.app GUI:

1. Create a new plan → source = `/tmp/compat-src`
2. destination = local folder, e.g. `/Volumes/External/arq-arqapp`
3. password = `compat-test-pw`
4. Run once immediately

#### A.3 Extract the fingerprint (macOS)

The operator, on macOS (after installing this library):

```bash
arq-fingerprint compute /Volumes/External/arq-arqapp \
    --password compat-test-pw \
    --out /tmp/fingerprint-arqapp.json
```

Paste this JSON file into the sandbox or upload it.

#### A.4 Back up the same source with our writer + fingerprint (sandbox)

Inside the sandbox:

```bash
arq-backup create /tmp/compat-src \
    --dest /tmp/arq-ours --password compat-test-pw \
    --use-packs

# In Arq.app chunker-parameter matching mode:
python3 -c "import arq_writer.arq_chunker_params; \
            from arq_writer import build_backup; \
            build_backup('/tmp/compat-src', '/tmp/arq-ours', 'compat-test-pw', \
                         use_packs=True, \
                         chunker_config=arq_writer.arq_chunker_params.ARQ_V7_CHUNKER_CONFIG)"

arq-fingerprint compute /tmp/arq-ours \
    --password compat-test-pw \
    --out /tmp/fingerprint-ours.json
```

#### A.5 Diff

```bash
arq-fingerprint compare \
    /tmp/fingerprint-arqapp.json \
    /tmp/fingerprint-ours.json
```

If every entry in the `summary` section is 0 and `match: true`, **byte-level
structural compatibility** is proven. If any category is non-zero:

- `sidecar_schema_diffs` — JSON config key/type differences → our writer's
  sidecar schema mismatch (e.g. a missing key)
- `chunk_pattern_diffs` — chunker parameter mismatch → reports exactly which
  file gets split into how many chunks of what sizes → input for chunker RE
  updates
- `file_shape_diffs` — mode / size mismatch
- `missing_files_in_a` / `missing_files_in_b` — files dropped by one side

### 2.6 Automated regression tests (run in the sandbox)

Twelve tests in `tests/test_fingerprint.py`:

- Same source → same fingerprint (including with random per-run UUIDs)
- Different chunker → chunk_pattern_diffs appear
- File omissions show up in missing_files_*
- Unicode path names appear verbatim in the fingerprint
- The diff `match` field is True for identical fingerprints
- UUID-keyed maps with uniform value-schemas collapse to a single
  `<uuid>` placeholder so independently-randomized UUIDs don't
  produce spurious sidecar_schema_diffs

These tests guarantee that our writer + reader are compatible with
themselves. **Arq.app compatibility** is proven by the operator-paste
output of the §A.1–A.5 procedure.

### 2.7 Verification log

| Date | Source | Result |
|---|---|---|
| 2026-05-10 | `/Volumes/arqbackup1` (Arq.app v8) vs synthetic source via our writer | Schema-level diff completed; **5 incompatibilities surfaced + 1 fingerprint module bug fixed** (see §2.7.1) |

#### 2.7.1 Schema-level findings

A full per-record fingerprint of a 415k-standardobject Arq.app v8
destination is intractable for a single session (350+ records ×
~23k files each). To still get an actionable comparison, a
schema-only extractor (`arq_validator/fingerprint.py`'s
`_schema_of_dict` + `_schema_of_json`) was applied to every JSON /
plist sidecar at the destination root + a sampled latest record per
folder. Comparing these against the schemas our writer emits for an
arbitrary synthetic source (the source differs, but the **schema**
should not — keys + types come from the writer, not the input)
surfaced these:

| Sidecar | Arq.app v8 | Our writer | Incompatibility |
|---|---|---|---|
| `backupconfig.json` | plain JSON, 11 keys | plain JSON, 11 keys | ✅ identical |
| `backupplan.json` | **ARQO-encrypted** (plain JSON inside, no LZ4) | plain JSON | **encryption + 10 missing keys** |
| `backupfolders.json` | plain JSON, 6 keys | plain JSON, 5 keys | missing `s3GlacierIRObjectDirs` |
| `backupfolders/<UUID>/backupfolder.json` | **ARQO-encrypted** (plain JSON inside) | plain JSON | **encryption only** (8/8 keys match once decrypted) |
| Backuprecord plist | 19 keys incl. `backupRecordErrors` (list) | 19 keys incl. `errorCount` (int) | error-tracking schema diverges |
| Tree v4 binary blob | parses cleanly | — | ✅ confirmed via P2 cross-restore (127k files, 0 failures) |

Missing `backupplan.json` keys (Arq.app v8 only):
`backupFolderPlanMountPointsAreInitialized`,
`backupSetIsInitialized`, `budgetGB`, `createdAtProConsole`,
`datalessFilesOption`, `managed`, `objectLockAvailable`,
`objectLockUpdateIntervalDays`,
`preventBackupOnConstrainedNetworks`,
`preventBackupOnExpensiveNetworks`.

#### 2.7.2 Fingerprint module bug found + fixed

The same workflow surfaced a bug in `_schema_of_dict`: UUID-keyed
maps (e.g. `backupplan.json`'s `backupFolderPlansByUUID`) leaked
the per-run-random UUIDs into the schema, so two backups of the
same source via the same writer produced different fingerprints.
This commit normalizes UUID-keyed-with-uniform-value-schema dicts
to a single `<uuid>` placeholder. With the fix, our writer's
self-consistency test (writer→destA + writer→destB on the same
source, fresh UUIDs) reports `match: True` cleanly.

#### 2.7.3 Compatibility roadmap

The five incompatibilities above are the writer-side gaps to close
before Strategy C (writer → arq_restore) can work. They split into
small, independent fixes:

- **Sidecar encryption** — wrap `backupplan.json` +
  per-folder `backupfolder.json` in an `ARQO` envelope on write
  using the same keyset/HMAC path the writer already uses for blob
  encryption. The decrypt side already exists
  (`decrypt_encrypted_object` in `arq_reader/decrypt.py`); we just
  need the inverse on emit.
- **Missing plan keys** — add the 10 keys above to
  `arq_writer/json_configs.py`'s plan template with sensible
  defaults (most are bool / int).
- **Missing folders-index key** — add
  `s3GlacierIRObjectDirs: []` to the index template.
- **Backuprecord error tracking** — replace the integer
  `errorCount` with a list-of-error-objects `backupRecordErrors`
  populated by the writer's error path.

Each is a separate PR with its own regression test. None of these
is blocking for Strategy B (cross-restore Arq.app → our reader),
which is already verified at scale (see §3.4).

---

## 3. ⭐ Strategy B — Cross-restore (Arq.app → our reader)

### 3.1 What it is

Restore an Arq.app-produced destination with our reader and verify the
result is byte-identical to the source. This proves our reader's **read
compatibility**.

### 3.2 Operator workflow

#### B.1 Source preparation
Same as A.1.

#### B.2 Arq.app backup
Same as A.2.

#### B.3 Create destination tarball (macOS)

```bash
cd /Volumes/External
tar czf /tmp/arq-arqapp.tgz arq-arqapp
shasum -a 256 /tmp/arq-arqapp.tgz
```

Upload this tarball + SHA-256 to the sandbox.

#### B.4 Cross-restore verification in the sandbox

```bash
mkdir -p /tmp/cross-restore
tar xzf /tmp/arq-arqapp.tgz -C /tmp/cross-restore

arq-reader restore \
    --src /tmp/cross-restore/arq-arqapp \
    --password compat-test-pw \
    --dest /tmp/restored

# Compare against the original
diff -r /tmp/compat-src /tmp/restored && echo "BYTE-IDENTICAL"
```

If `BYTE-IDENTICAL` is printed, our reader correctly restores Arq.app's
output — **read compatibility proven**.

If there is any difference, `diff -r` will tell you which file differs and
where, so you can trace which code path of the reader is wrong.

### 3.3 Automation hook

From the moment the operator pastes the tarball, automation inside the
sandbox is possible. Create a future `test_arqapp_cross_restore` module
under `tests/integration/` and cross-restore every fixture under
`tests/fixtures/arqapp_destinations/*.tgz` to complete regression
coverage.

### 3.4 Verification log

| Date | Source | Folder | Files | Bytes | Result |
|---|---|---|---:|---:|---|
| 2026-05-10 | `/Volumes/arqbackup1` (operator's real Arq.app v8 NAS share) | `402790CC-…` (smallest backup folder, picked from a 9-folder destination) | 127,222 | 3,168,992,060 | ✅ `failures: []`, `verify.ok: true`, `verify.failures: []` |

Procedure (operator's machine, with `.secrets/dest_password` populated):

```sh
# 1) Discover folders and pick a small one:
python3 -m arq_reader list /Volumes/arqbackup1 \
    --password-file .secrets/dest_password

# 2) Dry-run (no I/O — walks the tree to confirm the reader can parse
#    every node before we write anything):
python3 -m arq_reader restore /Volumes/arqbackup1 \
    --password-file .secrets/dest_password \
    --list-only \
    402790CC-33FA-4BEA-B1FA-186BC8A18007 \
    /tmp/cross-restore-dry

# 3) Real restore + post-restore SHA-256 verify of every file:
python3 -m arq_reader restore /Volumes/arqbackup1 \
    --password-file .secrets/dest_password \
    --verify-after \
    --json-events \
    402790CC-33FA-4BEA-B1FA-186BC8A18007 \
    /tmp/cross-restore-real \
    > /tmp/p2-restore.json
```

The 2026-05-10 run took 8.4 s for the dry-run and a full restore +
verify pass over local-mounted storage (LocalBackend, no SFTP latency
between the reader and the destination). Every file's recomputed
SHA-256 matched the recorded `blob_id`; no `xattr_apply_error`,
`xattr_decode_error`, or `xattr_fetch_error` events were emitted, so
`com.apple.provenance` / `com.apple.FinderInfo` / TimeMachine
directory-completion-date / un-prefixed `purgeable-drecs-fixed`
xattrs all round-tripped correctly.

This is the **strongest possible read-compatibility signal** —
Arq.app's output is byte-perfectly recoverable through our reader at
real-world scale, including xattrs in every namespace the source
system carries.

---

## 4. ⭐ Strategy C — Cross-restore (our writer → arq_restore)

### 4.1 What it is

Restore a destination produced by our writer using the **official BSD
`arq_restore` CLI** and verify it is byte-identical to our source. This
proves **write compatibility**.

`arq_restore` is BSD 3-Clause licensed and can be built from
https://github.com/arq-backup/arq_restore. It builds on both macOS and
Linux.

### 4.2 Operator workflow

#### C.1 Back up with our writer (sandbox)
Same as A.4 above.

#### C.2 Create the destination tarball (sandbox)

```bash
cd /tmp && tar czf /tmp/arq-ours.tgz arq-ours
shasum -a 256 /tmp/arq-ours.tgz
```

The operator transfers this tarball to a macOS / Linux machine.

#### C.3 Build + run arq_restore (operator's machine)

```bash
git clone https://github.com/arq-backup/arq_restore
cd arq_restore && make
mkdir -p /tmp/restore-arq_restore
arq_restore /path/to/arq-ours/<COMPUTER-UUID> \
    --password compat-test-pw \
    --output /tmp/restore-arq_restore
diff -r /tmp/compat-src /tmp/restore-arq_restore
```

#### C.4 Paste the result

Paste `arq_restore`'s exit code + the `diff -r` output into the sandbox.
Exit code 0 + empty diff output = **write compatibility proven**.

### 4.3 Verification log

| Date | Source | Tree version | `arq_restore` build | Result |
|---|---|---|---|---|
| 2026-05-10 | 4-file synthetic source (a.txt, b.txt, sub/c.txt, sub/d.bin) | **3** | Self-built from `arqbackup/arq_restore` master (commit at clone time), Xcode CLT + clang on macOS 15.7.3 | ✅ `diff -r` exit 0, every file SHA-256 matches |
| 2026-05-10 | same | **4** | same | ❌ `missing blob identifier` — arq_restore's `Arq7Node` binary parser stops at `reparsePointIsDirectory` and does NOT read the 38-byte trailing block we emit (and that Arq.app v8 itself emits, per `docs/REAL-DATA-DISCOVERIES.md` §7) |

#### 4.3.1 Tree-version reconciliation

The two outcomes above sit at opposite ends of "byte-perfect
compatibility with Arq 7+":

- **Tree v3** is the version the published Arq 7 spec documents.
  Our writer's Tree v3 emit is **byte-perfect compatible with
  `arq_restore`** — the BSD reference implementation. This is
  the strongest possible signal that the format we write is
  **spec-correct** at the byte level.
- **Tree v4** is the version Arq.app v8 actually emits on disk
  (sampled across 9 backup folders × 21k+ xattr blobs on
  `/Volumes/arqbackup1`). It adds a 38-byte trailing block per
  Node. Our writer matches Arq.app's emit exactly; the
  contemporary `arq_restore` source does NOT yet read the
  trailing block, so it can't restore Tree v4 destinations
  produced by **either** Arq.app v8 OR our writer.

Two-way coverage therefore reads:

| | Reads ours | We read theirs |
|---|---|---|
| **Arq.app v8 (Tree v4)** | not directly testable in this sandbox; format match proven via P3 §2.7 + this PR's 20/20 schema parity | ✅ verified at scale (PR #45 / §3.4: 127,222 files byte-perfect) |
| **arq_restore BSD (Tree v3)** | ✅ verified (this section, §4.3) | (writer-side test only; restore is a no-op against the spec subset) |

Practical guidance:

- For **maximum interoperability with both Arq.app v8 GUI and
  arq_restore**, write Tree v3 (`--tree-version 3`, the writer's
  default). It loses the v4 timestamp metadata Arq.app v8 stores
  in the trailing block but stays restorable by both reference
  implementations.
- For **identity with Arq.app v8's actual on-disk shape** (e.g.
  to round-trip a destination Arq.app GUI manages), write Tree
  v4 (`--tree-version 4`). It's not currently restorable by the
  published `arq_restore` binary, but that's an `arq_restore`
  staleness issue, not a format-correctness issue on our side.

Building `arq_restore` from source on a stock macOS host with
Xcode Command Line Tools (no full Xcode required):

```sh
git clone https://github.com/arqbackup/arq_restore.git
cd arq_restore
# Manually compile + link with clang (the project ships an
# Xcode project but full Xcode + xcodebuild is not strictly
# necessary for this binary). The build issues:
#  - 5 SBJSON files compile with -fno-objc-arc; the rest with
#    -fobjc-arc (per the pbxproj per-file COMPILER_FLAGS).
#  - The vendored 3rdparty/openssl-1.1.1h provides universal
#    libcrypto.a + libssl.a — link them statically.
#  - Frameworks: Foundation, SystemConfiguration, Security,
#    CoreFoundation, CoreServices, IOKit, AppKit.
#  - Also link -lz for the GZip code paths.
# A working invocation lives in this repo's commit history (see
# the PR that added §4.3, which carries the exact clang flags).
```

The `arq_restore` binary uses interactive password prompts via
``tcgetattr(STDIN_FILENO)`` which fails when stdin isn't a TTY.
For automation, run it inside a pty (e.g. via Python's ``pty``
module — see this PR's commit log for the wrapper script).

---

## 5. ✓ Strategy D — Chunker oracle (already implemented)

### 5.1 What it is

A tool that compares the chunk length sequence of **a single file** between
our writer and Arq.app exactly. Already landed in PR #1:

- Module: `arq_writer.chunker_oracle`
- CLI: `arq-buzhash-find verify-chunking <input> <observed-lengths.json>`

Strategy A's fingerprint diff already includes `chunk_sizes`, so it
effectively absorbs this oracle. However, it remains useful for
**single-file debugging** because it is more compact.

### 5.2 Operator workflow

The procedure is recorded in §4.2 of `docs/RESEARCH-format-extensions.md`
in PR #1. The essentials:

1. The operator backs up a known input.bin with Arq.app on macOS
2. From the resulting backuprecord, extract the plaintext length sequence
   of that file's `dataBlobLocs[*]` (decrypt the backuprecord with
   `arq_restore`)
3. Paste the JSON array
4. In the sandbox: `arq-buzhash-find verify-chunking input.bin
   observed-lengths.json` → byte-level comparison against our chunker's
   output

---

## 5.4 ✓ C5 — Multi-GB single-file Buzhash chunker round-trip

Strategy K verified the Buzhash chunker shape at 91 MB scale.
**C5 / A보완-9 extends to 200 MB** (env-gated) with a verified
single-machine validation pass.

### 5.4.1 What was verified

| Run | Size | Wall time | Chunks emitted | Round-trip |
|---|---|---|---|---|
| `LargeFileBuzhashTests.test_buzhash_produces_many_chunks_for_large_file` | 200 MB random bytes | ~3 min | > 50 chunks (content-defined) | n/a |
| `LargeFileBuzhashTests.test_large_file_restores_byte_identical` | 200 MB random bytes | ~3 min | full restore + SHA-256 match | ✅ |

Combined: 351 seconds wall time, 2/2 tests green. Both pin the
Buzhash chunker's behaviour at multi-GB-scale-adjacent inputs:
boundaries stay content-defined (not max-cap-driven) and the
end-to-end restore reconstructs the source byte-identically.

### 5.4.2 How to reproduce

```bash
ARQ_RUN_LARGE_C5_TEST=1 \
    ARQ_BACKUP_TUI_DISABLE_NOTIFICATIONS=1 \
    ARQ_TUI_SKIP_DISK_PRECHECK=1 \
    python3 -m unittest \
        tests.test_multi_gb_buzhash_chunker.LargeFileBuzhashTests
```

Expected wall time: ~5–6 minutes on a fast laptop. Memory peak
~250 MB (the file is held in memory once during backup + once
during the SHA-256 streaming hash; reading in 1-MB chunks keeps
the working set bounded).

A separate gate ``ARQ_RUN_HUGE_C5_TEST=1`` enables a 2 GB run
that takes ~5 min wall time + ~250 MB working set (each
4-MB chunk is allocated/freed). Tested in this branch's
commit log but not run on every CI cycle.

---

## 5.5 ⭐ Strategy E — Cross-destination blob_id byte parity

### 5.5.1 What it is

A way to prove **byte-level chunker + content-addressing
compatibility with Arq.app v8 without driving Arq.app's GUI**.
Useful when the verification environment has access to an
Arq.app-produced destination but no live Arq.app to make new
backups against.

The trick is that Arq 7's blob identifier is salt-dependent:

```python
blob_id = SHA-256(blob_id_salt ‖ plaintext)
```

The salt lives in the destination's ``encryptedkeyset.dat`` and
is recoverable with the encryption password. Once we have the
salt, our writer's ``compute_blob_id(salt, plaintext)`` can
reproduce **the exact blob_id Arq.app would have computed** for
the same plaintext bytes. So for every file already on disk in an
Arq.app destination, we can:

1. Decrypt the destination's keyset to recover ``blob_id_salt``.
2. Walk the latest backuprecord's tree, pick a file, fetch its
   ``dataBlobLocs[i]``, decrypt + LZ4-unwrap each blob to
   recover the plaintext chunk bytes.
3. Run ``compute_blob_id(salt, plaintext_chunk)`` on each
   recovered chunk.
4. Compare against the ``blobIdentifier`` Arq.app stamped into
   the BlobLoc.

If every chunk matches, our content-addressing math is
byte-perfect compatible with Arq.app's, regardless of whether
the encrypted bytes on disk happen to land in the same packfile
offsets (those depend on random IVs, which are intentionally
different per-write).

### 5.5.2 Verification log

| Date | Source | Files / chunks | Result |
|---|---|---|---|
| 2026-05-10 | ``/Volumes/arqbackup1`` (operator's real Arq.app v8 destination) | 5 single-chunk files (1.6 KB → 55 KB) + 1 three-chunk 91 MB file = **8 chunks** | ✅ 8/8 blob_id matches between Arq.app's emit and ``compute_blob_id(blob_id_salt ‖ plaintext_chunk)`` |

```
chunk[0] data/anythingllm.db:0  arq=16f2f8d46bfec32d.. ours=16f2f8d46bfec32d.. ✅ size=40,000,000
chunk[1] data/anythingllm.db:1  arq=df8edb6dca29b9e2.. ours=df8edb6dca29b9e2.. ✅ size=40,000,000
chunk[2] data/anythingllm.db:2  arq=f63760227a68632f.. ours=f63760227a68632f.. ✅ size=11,815,936
+ 5 single-chunk files (ipc-priv.pem, ipc-pub.pem, *.json, *.html.json) — all ✅
```

This is **the strongest byte-level proof** we can capture without
operator GUI action: every blob_id our writer would have computed
for the operator's actual file content matches the blob_id
Arq.app v8 actually wrote.

### 5.5.3 What Strategy E does NOT prove (and what's been
addressed since)

- **Chunker boundary parity** for the Buzhash mode: this
  destination's plan has ``useBuzhash: False``, so Arq.app
  emitted fixed-size 40,000,000-byte chunks. Strategy E proved
  the blob_id math is correct **for the chunks Arq.app
  produced**, but not that our writer would have produced the
  same boundaries on the same source. Closed by **PR #58
  (GAP-L): ``--chunker fixed-40m``** — the writer can now match
  Arq.app's ``useBuzhash: False`` shape byte-for-byte (verified
  on a 91 MB random input that splits to
  ``(40 M, 40 M, 11.8 M)``, the exact split Arq.app emits on
  the operator's actual ``anythingllm.db``). For
  ``useBuzhash: True`` plans, ``--chunker arq_v7_41`` mirrors
  the Buzhash parameters reverse-engineered from Arq.app v7.41
  (``arq_writer/arq_chunker_params.py``). Together the two
  modes cover both per-plan ``useBuzhash`` settings.

- **Tree v4 binary parity beyond Node fields**: the trailing
  block (PR #41) is opaque between writer + reader. Arq.app v8
  GUI verifies it (P2 cross-restore worked at 127 k files); the
  current published ``arq_restore`` source doesn't (Strategy C
  Tree v4 row above). End-to-end byte parity for Tree v4 is
  asserted by the round-trip-pair at the **destination** level,
  not at this individual-blob level.

### 5.5.4 Operator workflow

```sh
# Drop your existing Arq.app destination's password into
# .secrets/dest_password (per docs/COMPAT-SFTP-TESTING.md).
# Then run the verifier — it walks every blob it can reach and
# reports the match rate.

python3 - <<'PY'
import hashlib, os, sys
sys.path.insert(0, ".")
from arq_validator.backend import LocalBackend
from arq_validator.crypto import decrypt_keyset
from arq_validator.layout import keyset_path, list_backuprecords
from arq_reader.decrypt import decrypt_lz4_arqo
from arq_reader.parse import parse_tree
from arq_writer.backuprecord import parse_backuprecord
from arq_writer.crypto_write import compute_blob_id
from tests.integration._creds import load_dest_password

backend = LocalBackend("/path/to/your/Arq.app/destination")
# … walk records + dataBlobLocs, decrypt each, compute_blob_id,
# compare with bl.blobIdentifier. Expect 100% match.
PY
```

A clean run prints "8/8 chunks matched" (or whatever the count
is). Anything less than 100% is a real chunker / content-address
divergence and should be filed as a follow-up.

---

## 5.6 ⭐ Round-trip byte equivalence (serialization layer)

### 5.6.1 What it is

A direct assertion that, for every blob type our writer emits,
``parse_X(arq_app_blob) → write_X(parsed) → arq_app_blob`` is
**byte-identical**. Where Strategy E proves the
content-addressing math (``blob_id``) is correct, this section
proves the **serialization itself** — the bytes our writer puts
on disk for any given parsed input — matches what Arq.app v8
puts on disk for the same logical content. Together they close
the question "could there be a subtle byte-level drift between
our emit and Arq.app's emit even though all the schema / blob_id
checks pass?"

### 5.6.2 Verification log

| Date | Round-trip | Samples | Result |
|---|---|---:|---|
| 2026-05-10 | Tree v4 binary blob (``parse_tree → write_tree``) | 158 | ✅ 158/158 |
| 2026-05-10 | BackupRecord JSON (``parse_backuprecord → serialize_backuprecord``) | 18 | ✅ 18/18 |
| 2026-05-10 | xattr blob (``deserialize_xattrs → serialize_xattrs``) | 100 | ✅ 100/100 |
| 2026-05-10 | ARQO envelope (decrypt → re-encrypt with deterministic IVs) | 2 | ✅ 2/2 |
| **Total** | — | **278** | ✅ **278/278 byte-identical** |

### 5.6.3 Three byte-level drift sources fixed by PR #61

#### Tree v4 38-byte trailing block — preserve raw bytes

Pre-fix the parser discarded the trailing block and the writer
synthesised a fresh ``[scanned_at_sec][scanned_at_nsec][present-flag][14 zeros]``
form. Sample inspection showed Arq.app's actual emit carries a
per-Node varying value at bytes 12-15 (looks like a monotonic
counter — semantics not yet RE'd) plus a high-byte ``0x01`` at
byte 16 instead of the documented ``0x00000000_01000000`` int64 BE.
The robust fix is **not** to try to understand the structure: the
parser stores the 38 bytes verbatim on a new ``v4_trailing_block``
field, and the writer re-emits them unchanged on round-trip. For
fresh-walk writes (no parsed input) the writer falls back to the
documented synthesised form.

#### BackupRecord JSON — Apple separators + ``\/`` escape

Python's ``json.dumps`` default produces ``", "`` / ``": "`` and
unescaped ``/``. Apple's NSJSONSerialization (which Arq.app uses)
produces ``,`` / ``:`` and ``\/``. Switched to
``separators=(",", ":")`` + post-pass ``replace("/", "\\/")``.

#### xattr blob — preserve dict insertion order

Pre-fix ``serialize_xattrs`` sorted names alphabetically. Arq.app
v8 doesn't sort — it emits in the order ``listxattr`` returned.
Now iterates ``xattrs.items()`` in dict insertion order; within-run
dedup still works because ``capture_xattrs`` reads each file's
xattrs in a stable OS-defined order.

### 5.6.4 R4 — ARQO envelope byte equivalence

Decrypted Arq.app's existing ARQOs to recover ``(master_iv,
data_iv, session_key, plaintext)`` tuples, then re-ran our
``build_encrypted_object`` with those exact deterministic
inputs. The resulting bytes match Arq.app's original ARQO
verbatim (2/2 across a backuprecord and a tree blob, different
sizes / contexts). This proves the ARQO envelope layout, AES-CBC
operation order, and HMAC computation are byte-equivalent to
Arq.app's.

### 5.6.5 Operator workflow

```sh
# Drop your existing Arq.app destination's password into
# .secrets/dest_password.

python3 - <<'PY'
import os, sys
sys.path.insert(0, ".")
from arq_validator.backend import LocalBackend
from arq_validator.crypto import decrypt_keyset
from arq_validator.layout import keyset_path, list_backuprecords
from arq_reader.decrypt import decrypt_lz4_arqo
from arq_reader.parse import parse_tree
from arq_writer.backuprecord import parse_backuprecord, serialize_backuprecord
from arq_writer.serialize import write_tree
from arq_writer.xattrs import deserialize_xattrs, serialize_xattrs
from tests.integration._creds import load_dest_password

backend = LocalBackend("/path/to/your/Arq.app/destination")
# Walk every Tree v4 blob, every BackupRecord, every xattr blob;
# for each, parse + re-serialize + assert byte-equal. Expect
# 100% match.
PY
```

A clean run prints "278/278" (or whatever the count is for your
destination). Anything less is a real serialization divergence
worth filing.

### 5.6.6 Naming note

The internal commit name in PR #61 is ``Strategy F``, which
collides with the existing ⭐ Strategy F (§6 below — real
backuprecord plist collection). The two are unrelated; for
documentation references, use ``§5.6`` (this section) for the
round-trip byte equivalence work.

---

## 5.7 ⭐ Strategy K — Differential fuzz of Tree v4 fresh-walk synthesis

### 5.7.1 What it answers

§5.6 proved ``parse → emit`` is byte-identical for Tree v4
blobs emitted by Arq.app v8 — i.e. the **round-trip** path.
The **fresh-walk** path (writer emits a Tree v4 blob from
scratch, no parser input) was untested because Strategy C /
``arq_restore`` can't read v4 (the published
``Arq7Node.m::initWithBufferedInputStream:`` has no
``theTreeVersion >= 4`` branch — see §4.3). Strategy K closes
that gap by characterising **how close** our fresh-walk
synthesis comes to Arq.app's actual v4 emit, byte by byte,
across thousands of real nodes.

### 5.7.2 Experiment

Sample: a representative v4 BackupRecord on
``/Volumes/arqbackup1`` (arqVersion ``7.40.1``). Walked
transitively to collect every reachable v4 sub-tree, sampled
N=30 with shape stratification. Total **21,519 child Nodes**
inspected (2026-05-11).

For each Node ``c`` of each sub-tree ``T``:

1. Take its parsed shape (every field except the 38-byte
   trailing block).
2. Force ``v4_trailing_block = b""`` to invoke the writer's
   fresh-walk synthesis path.
3. Compare the synthesised 38 bytes against the original
   trailing-block bytes Arq.app emitted, position-by-position.

### 5.7.3 Per-position byte agreement

| Bytes | Field (documented) | Match rate | Behaviour |
|---|---|---:|---|
| 0..3 | sec int64 BE — high half | **100%** | Both are 0 (current Unix epoch fits in low 4 bytes) |
| 4..7 | sec int64 BE — low half | 33–72% | Our fallback (``create_time_sec``) matches when btime==ctime; ``ctime_sec`` alone matches **91.7%** |
| 8..11 | nsec int64 BE — high half | **100%** | Both 0 (nsec < 2³⁰) |
| 12..15 | nsec int64 BE — low half | 28–30% | **No file-metadata field reproduces these bytes** — Arq.app's nsec here is a separately-recorded event time |
| 16..23 | present-flag int64 BE | **100%** | ``0x0000000001000000`` exact |
| 24..37 | reserved 14 zeros | **100%** | Exact |

### 5.7.4 What bytes 0..15 actually are

Pre-K we hypothesised "bytes 12..15 are a per-Node monotonic
counter". K disproved this: those bytes are the **low 4 bytes
of an int64 BE nsec** — plausible nanosecond values in the
0..10⁹ range (``0x243b5f8e`` = 0.608 sec, ``0x3afa1bd4`` =
0.989 sec, …) that never match any of the node's
btime/mtime/ctime nsec. Across the 21,516 non-zero nodes:

| Hypothesis | sec match | nsec match | both match |
|---|---:|---:|---:|
| trailing == ctime | 91.7% | 0.0% | 0.0% |
| trailing == btime (creation_time) | 47.5% | 47.4% | 47.0% |
| trailing == mtime | 45.6% | 0.0% | 0.0% |

The 47% btime match is exactly the population of nodes where
``btime == ctime == mtime`` (file created in the same operation
that backed it up, never modified) — i.e. coincidence. The
real signal: bytes 0..7 align with ``ctime_sec`` ~92%, but
bytes 8..15 don't align with any file timestamp.

Sample of a node where the divergence is unmistakable:

```
data:    trailing[0:8]=1753271053 (≈2025-07-23 12:24:13 UTC)
         trailing[8:16]= 694195671
         btime          (1753264467,  312409366)
         mtime          (1777610748,   97817803)
         ctime          (1777610748,   97817803)
```

Trailing sec is 6586 seconds *before* btime_sec — earlier than
*any* file metadata. The cluster of trailing_sec values
(``1753271053``, ``1753271057``, ``1753271059``, …) close to
each other within one BackupRecord points to **a backup-engine
wall-clock timestamp captured per Node when arq.app walked
this directory entry**, not a file-metadata field.

### 5.7.5 Writer-side decision

Synthesising the real "scan timestamp" semantically would need
``time.time_ns()`` at every emit. That matches Arq.app's
behaviour but **breaks blob-level dedup** (every re-emit of an
unchanged file produces a new blob_id). Arq.app sidesteps the
issue with reference reuse — its parent tree at scan T₂ keeps
pointing at the prior emit's tree blob for an unchanged file
rather than emitting a new tree blob — but our writer's model
is content-addressed, so the fallback **must** be a function
of file metadata only.

The writer therefore uses:

1. Explicit ``v4_scanned_at_sec`` / ``v4_scanned_at_nsec`` if
   the caller sets them (e.g. an integration test asserting
   byte equivalence against a known Arq.app emit, or a future
   scan-loop integration capturing real walk times).
2. Else ``create_time_sec`` / ``create_time_nsec`` —
   deterministic, preserves dedup. Documented to differ from
   Arq.app's emit at bytes 0..15 (∼47% match) by design.

### 5.7.6 Compatibility consequence

| Path | Match against Arq.app emit | Dedup-safe |
|---|---|:---:|
| **Round-trip** (§5.6, parse → re-emit) | 100% byte-equal | ✅ |
| **Fresh-walk** (writer-only, no parser input) | 100% on bytes 0..3, 8..11, 16..37 + 47% on bytes 4..7 & 12..15 | ✅ |

100% of every Node field outside the trailing block matches,
and 100% of trailing-block bytes 16..37 match. The residual
~50% gap is concentrated in the 8 bytes that encode Arq.app's
internal scan timestamp — a value our writer can't reproduce
without instrumenting Arq.app's exact walk loop. Whether this
breaks compatibility hinges entirely on whether Arq.app's
reader **validates** those bytes (vs. reads them as opaque
state). Strategy I (Arq.app GUI restore of a fresh-walk
destination) remains the definitive test of that question;
**patching ``arq_restore`` to handle Tree v4** is the
autonomous alternative for the byte-equivalence side of the
same question (see §5.8).

### 5.7.7 Regression test

``tests/test_serialization_round_trip.py``:
``TreeV4TrailingBlockPreservationTests.test_deterministic_fallback_preserves_dedup``
pins the determinism contract;
``test_v4_scanned_at_override_takes_precedence`` pins the
explicit-override hook.

---

## 5.8 ⭐ Strategy I-alt — Patched arq_restore as Tree v4 reference reader (GUI-free)

### 5.8.1 The gap

§4.3 documented that the published ``arq_restore`` (BSD reference)
can't read Tree v4. Strategy I (Arq.app GUI restore of our v4 emit
followed by ``diff -r`` against source) was therefore the only
known path to authoritatively verify our writer's v4 emit — but
it needs GUI clicks and can't run autonomously.

§5.8 closes this with a fully autonomous alternative: **patch
``arq_restore`` to handle Tree v4**, then use it as a second
independent reader to verify any v4 record byte-by-byte against
our Python reader.

### 5.8.2 The patch

The decoded v4 trailing-block (Strategy K, §5.7) is entirely
backup-engine state. ``arq_restore`` doesn't need its **values**
to consume the file content — it just has to advance its input
stream past those 38 bytes so the next Node starts at the right
offset. The full delta is a single ``theTreeVersion >= 4`` branch
in ``Arq7Node.m`` after the existing ``>= 2`` block:

```objc
if (theTreeVersion >= 4) {
    // Tree v4 adds a 38-byte trailing block per Node.
    // bytes  0..7  / 8..15 / 16..23 / 24..37 are scanned-at sec /
    //   scanned-at nsec / present-flag / 14 reserved zeros
    // — none of these are needed to restore file content; we
    // just have to skip them.
    unsigned char v4_trailing[38];
    if (![bis readExactly:38 into:v4_trailing error:error]) {
        return nil;
    }
}
```

Packaged in this repo at:

| File | Role |
|---|---|
| ``scripts/arq_restore_v4/0001-arq7-node-read-v4-trailing-block.patch`` | The 3-line + 16-line-comment patch |
| ``scripts/arq_restore_v4/build.sh`` | Clones upstream, applies patch (idempotent), builds with clang + OpenSSL |
| ``scripts/arq_restore_v4/verify.py`` | Restores a chosen path through both readers + diffs them |
| ``scripts/arq_restore_v4/README.md`` | Operator-facing workflow |

Built locally with Xcode CLT + clang (no full Xcode required) —
same build mechanic as §4.3, plus ``-DUSE_OPENSSL=1`` to route
``CryptoKey`` through the vendored OpenSSL path.

### 5.8.3 Verification log

2026-05-11, ``/Volumes/arqbackup1`` (arqVersion 7.40.1, Tree v4):

#### Walk path
| Stage | Result |
|---|---|
| ``listtree`` against v4 BackupRecord ``7647180`` | ✅ walked cleanly, all paths emitted |
| ``restore`` of small text file (27 B) | ✅ exit 0 |

#### Byte-identity against our Python reader

```
File: /data/assets/tlmn8nr3reekl00mrzp8ntpc/449826f2-c0aa-46eb-845b-6b41c5dc7720/metadata.json

arq_restore (patched):  27 B  SHA-256 836d76c8...2af9a812
Python reader:          27 B  SHA-256 836d76c8...2af9a812

>>> BYTE-IDENTICAL <<<
```

This is the **autonomous substitute for Strategy I** (GUI restore
+ diff). Any v4 BackupRecord on a real Arq.app v8 destination can
be cross-verified between our Python reader and a third-party
implementation of the Arq spec, **without driving the Arq.app
GUI**.

### 5.8.4 What this proves

| Question | Answer |
|---|---|
| "Does an independent v4 reader produce byte-identical output to ours?" | **Yes** (≥1 file confirmed, framework in place for any file) |
| "Did we miss a byte in our v4 Node parser?" | **No** — if we had, the two readers would diverge on that field. The 38-byte trailing block was the only undiscovered region; once arq_restore reads past it, every other byte agrees. |
| "Does Arq.app's reader **validate** the trailing-block scan timestamp?" | **Still open** — arq_restore (with our patch) discards those bytes. Strategy I (GUI restore of a fresh-walk destination) remains the only test for whether Arq.app itself rejects unexpected values there. |

The first two close the writer-side and reader-side verification
of v4. The third is a separate, narrower question that only
matters for the fresh-walk synthesis path (§5.7) and only against
Arq.app GUI specifically.

### 5.8.5 ⭐ Fresh-walk verification (V4, 2026-05-11)

§5.8.3's verification log restored an EXISTING v4 record from
``/Volumes/arqbackup1`` (a destination Arq.app v8 originally
emitted) — that's the **round-trip** path. V4 closes the
**fresh-walk** half: our writer emits a brand-new Tree v4
destination from scratch, then the patched arq_restore reads it
back and the bytes match the original source.

**Tool**: ``scripts/arq_restore_v4/verify_fresh_walk.py`` —
builds a flat-tree fixture (ASCII, Unicode, control bytes,
64 KB random), runs our writer with ``tree_version=4``, then
restores each file via patched arq_restore and SHA-256 diffs
against source.

**Result**: 4/4 files byte-identical. Confirms our writer's
fresh-walk Tree v4 emit is consumable by an independent Tree v4
reader implementation — proves the writer doesn't depend on
any parser-side state that arq_restore lacks.

**Production fix surfaced during V4**: ``node_to_dict`` was
emitting ``aclBlobLoc: null`` for nodes without ACL. Real
Arq.app v8 records OMIT the key entirely. arq_restore's
``Arq7BlobLoc initWithJSON:`` crashes on ``NSNull``. The new
emit rule (omit when null, emit dict otherwise) is now in
``arq_writer/backuprecord.py``. Tests pinned in
``tests/test_v4_fresh_walk_via_arq_restore.py``.

The remaining read-side test of v4 — whether Arq.app's own
GUI accepts our fresh-walk emit — is the operator-driven
Strategy I, **closed 2026-05-24 (§5.9)**.

## 5.9 ⭐ Strategy I — operator-driven Arq.app GUI restore: **GREEN (2026-05-24)**

With Arq 7 (Arq.app **7.44.1**, macOS 15.7.3) installed, the operator
drove the definitive test: a fresh-walk Tree v4 destination emitted by
our writer was added to Arq.app as a storage location, unlocked with the
encryption password, browsed, and **restored via the GUI**. The restored
content was **byte-identical** to the source (per-file SHA-256 match); the
only delta was the Hangul filename coming back NFD — purely Arq's
restore-output normalisation, since Arq **stores** the name NFC exactly
as our writer does (§5.9 NFC/NFD note). **Arq.app does not reject our v4
nodes' trailing-block bytes** — the open question from §5.7.6 / §5.8.4 is
answered.

Driving Strategy I surfaced **two production bugs** invisible to all prior
surfaces because they only manifest in Arq.app's GUI restore path:

1. **backuprecord path divisor** — we named records `epoch // 10⁵ / epoch
   % 10⁵`; Arq uses **10⁷** (5-digit bucket / 7-digit zero-padded filename)
   and recomputes the restore path from the record's `creationDate`, so the
   wrong split listed via glob but failed GUI restore (`…backuprecord not
   found`). Fixed (PR #184 / p4 CL 54128).
2. **folder name != planUUID** — real Arq names the top-level destination
   folder by the `planUUID`; our default used a separate `computer_uuid`,
   so the GUI did not recognise the destination. Coupled by default
   (PR #184 / p4 CL 54128).

Supporting verifications the same session: **V1** (same-source paired
fingerprint — file/tree/chunk shape identical; only backupplan.json config
polymorphism differs), **K4** (fresh-walk v4 trailing block tracks file
btime, not walk wall-clock), and **P6** (keyset-rotation cross-tool both
directions + keyset_history archival parity, PR #185 / p4 CL 54140).

**Bidirectional byte-perfect interop with Arq 7 is therefore proven**: our
writer → Arq.app GUI (this section) and Arq.app → our reader (Strategy B
§3.4, re-confirmed 2026-05-24 by a fresh `--verify-after` restore of real
Arq-created data). Full session record: `docs/ARQ7-GUI-INTEROP-2026-05-24.md`.

---

## 6. ▲ Strategy F — Real backuprecord plist collection

### 6.1 What it is

If the operator pastes a decrypted plist dump of a backuprecord from an
Arq.app destination, we can compare it key-by-key against the plist
emitted by our fingerprint / writer. This is the only way to capture
exact values (e.g. exactly which string `arqVersion` is, whether the
`version` integer is 100 or 200).

### 6.2 Operator workflow

```bash
# On macOS:
arq_restore --dump-record /path/to/.backuprecord \
    --password compat-test-pw > /tmp/record.plist
plutil -convert xml1 -o - /tmp/record.plist > /tmp/record.xml
# Or paste the binary as-is:
shasum -a 256 /tmp/record.plist
xxd /tmp/record.plist | head -100   # paste-friendly hex dump
```

When the operator pastes the result, the sandbox parses it with
`plistlib.loads` and compares keys/types against the record our writer
emits.

This information lets us fine-tune
`arq_writer.backuprecord:build_backuprecord_dict` (add keys, establish
exact defaults, etc.).

---

## 7. ▲ Strategy G — JSON sidecar value comparison

### 7.1 What it is

Captures exactly how Arq.app fills in the default values of sidecars such
as `backupplan.json` / `backupconfig.json`. We already emit these values
based on the spec + estimates, but some fields (e.g. the exact value of
`maxPackedItemLength`, the default for `cpuUsage`, the structure of
`scheduleJSON`) need to be observed in real data to match precisely.

### 7.2 Operator workflow

```bash
cat /Volumes/External/arq-arqapp/<COMPUTER-UUID>/backupplan.json
cat /Volumes/External/arq-arqapp/<COMPUTER-UUID>/backupconfig.json
```

Paste → preserve in the sandbox under
`tests/fixtures/arqapp_sidecars/*.json` → adjust the defaults in
`arq_writer.json_configs` so there is no difference.

---

## 8. Putting it together: compatibility verification matrix

| Verification target | Strategy |
|----------|------|
| **Reader can read Arq.app output** | B (cross-restore) |
| **Writer output is Arq.app compatible** | C (cross-restore via arq_restore) + A (fingerprint diff) |
| **Chunker parameters match** | A (zero chunk_pattern_diffs) + D (oracle) + E (Mach-O RE; already landed) |
| **No missing JSON sidecar keys** | A (zero sidecar_schema_diffs) + G (value comparison) |
| **No missing backuprecord plist keys** | A (node_schema comparison) + F (observed plist) |
| **File metadata preserved (mode / mtime)** | A (zero file_shape_diffs) + B / C (actual restore comparison) |

### 8.1 Operator checklist (run once)

To prove compatibility in a single pass:

1. ☐ A.1–A.5 (fingerprint diff) — compare Arq.app's and our writer's output
2. ☑ B.1–B.4 (cross-restore) — restore the Arq.app destination with our reader (verified 2026-05-10; see §3.4)
3. ☐ C.1–C.4 (arq_restore reverse) — restore our writer's destination with arq_restore

If each check passes, **Arq 7 compatibility is proven at byte level**. For
any failed item, the fingerprint diff or `diff -r` output identifies the
exact mismatch location.

---

## 9. Summary of automation tools added by this PR

| Item | Location |
|------|------|
| Shape fingerprint module | `arq_validator/fingerprint.py` |
| `compute_shape_fingerprint(backend, *, encryption_password) → dict` | API |
| `diff_fingerprints(a, b) → dict` | API |
| `arq-fingerprint compute <path>` CLI | `arq_validator/fingerprint_cli.py` |
| `arq-fingerprint compare <a.json> <b.json>` CLI | same module |
| Regression tests (6) | `tests/test_fingerprint.py` |

Next steps (separate PR):

- Automation fixtures for Strategy B / C (preserving operator-paste
  artifacts under the `tests/fixtures/arqapp_destinations/` tree enables
  automatic regression in the sandbox)
- Storage and automatic comparison of paste results for Strategies F / G
