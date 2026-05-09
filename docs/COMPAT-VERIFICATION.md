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

Six tests in `tests/test_fingerprint.py`:

- Same source → same fingerprint
- Different chunker → chunk_pattern_diffs appear
- File omissions show up in missing_files_*
- Unicode path names appear verbatim in the fingerprint
- The diff `match` field is True for identical fingerprints

These tests guarantee that our writer + reader are compatible with
themselves. **Arq.app compatibility** is proven by the operator-paste
output of the §A.1–A.5 procedure.

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
2. ☐ B.1–B.4 (cross-restore) — restore the Arq.app destination with our reader
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
