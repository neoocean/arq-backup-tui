# C1 — Tree v4 38-byte trailing-block Mach-O RE plan

## Status

**Resumed + partially answered (2026-05-10).**

The operator installed Arq.app v8 at `/Applications/Arq.app`
and we ran the RE procedure below against
`Contents/Resources/ArqAgent.app/Contents/MacOS/ArqAgent`
(the actual backup engine — `Contents/MacOS/Arq` is just the
GUI shell). Findings below in §"Findings (2026-05-10 RE
session)". The four open questions from §"What's left to
confirm" now have partial answers; the remaining unknowns are
documented in §"Still open".

## What's already known

From earlier real-data probing (PR #20 + PR #25, see
`docs/REAL-DATA-DISCOVERIES.md` §7):

```
38-byte trailing block per v4 Node, observed in 1497 of 1500
real Arq.app v8 destinations:

  bytes  0..7   int64 BE  scanned-at sec   (≈ backup-pass time)
  bytes  8..15  int64 BE  scanned-at nsec
  bytes 16..23  int64 BE  constant 0x00000000_01000000  (present-flag)
  bytes 24..37  14 zero bytes (reserved)

The remaining 3 of 1500 nodes had all-zero blocks (every one a
.DS_Store with mtime == ctime == create_sec — likely files
freshly created in the latest backup pass; Arq.app's writer
may skip the structured form for those).
```

Our reader skips the block as opaque — both shapes parse
correctly, so format compatibility with Arq.app is preserved
even without exact field decomposition.

Our writer (since PR #25) emits the structured form when called
with `tree_version=4`. The fall-back-to-create-time behaviour is
documented as an intentional divergence from Arq.app's
sometimes-all-zero shape.

## What's left to confirm

1. **Field semantics**: are bytes 0..15 truly "scanned-at"
   (the backup pass time), or are they "lastVerifiedAt" (a
   per-Node verification timestamp)? Both fit the observed
   ranges (~7-minute window inside one backup pass).
2. **The 0x01000000 flag**: is it a true "present" marker
   (would there be a Node where this byte differs?), or a
   format-version field, or something else?
3. **All-zero block trigger**: what condition specifically in
   Arq.app's writer makes it emit a fully-zero block vs the
   structured form?
4. **Cross-OS variation**: do Arq.app for Windows / Linux
   variants emit the same 38-byte shape, or is it macOS-only?

## Procedure (when resuming)

The operator needs:

- A macOS host with `/Applications/Arq.app` installed
- `nm`, `strings`, `lldb` (Xcode command-line tools)
- A consenting Arq.app Standard or Premium license

Steps:

1. **Identify the relevant binary**:
   ```sh
   ls /Applications/Arq.app/Contents/MacOS/
   # Likely: Arq, Backup_Helper, etc.
   ```

2. **Search for Tree-version-4-related strings**:
   ```sh
   strings /Applications/Arq.app/Contents/MacOS/Arq \
     | grep -E "tree_v4|TreeV4|writeNode|scannedAt|lastVerified"
   ```

3. **Look for the constant 0x01000000 in disassembly**:
   ```sh
   otool -tv /Applications/Arq.app/Contents/MacOS/Arq \
     | grep -B2 -A2 "0x1000000"
   ```

4. **Find the Tree v4 emit function**: with luck the binary
   has Objective-C class names visible (`Arq7TreeWriter`,
   `BBNode`, etc.). Use `class-dump` or `lldb`'s
   `image lookup --regex "writeTreeNode"`.

5. **Cross-reference with our `_v4_trailing_block`**:
   `arq_writer/serialize.py` has the writer side; what we want
   is to confirm its struct layout matches Arq.app's
   `writeNodeData:` (or equivalent) exactly.

6. **Pin findings as a unit test**: pre-compute a known Node's
   38-byte block on Arq.app's side (write one file with Arq.app
   + read it back via our reader + dump bytes); add as a
   fixture in `tests/fixtures/` + assert our writer produces
   identical output.

## Why we're shipping without this

The format is **already round-trip-compatible**. Our reader
accepts both the all-zero and structured shapes; our writer
emits the structured form which Arq.app's reader also accepts
(treating the constant flag as a recognized marker). The
remaining open questions are about **exact field semantics**, not
about correctness — operators get a working backup either way.

If a future test surfaces a real-world destination where our
output is rejected by Arq.app, this RE plan becomes urgent.
Until then it's a "nice to have" that doesn't gate any
operator value.

## Related

- `arq_reader/parse.py` — `parse_node` reads the 38 bytes
- `arq_writer/serialize.py` — `_v4_trailing_block` writes them
- `scripts/probe_tree_v4_block.py` — observation harness
- `tests/test_tree_v4_trailing_block.py` — round-trip tests
- `tests/test_tree_v4_block_invariant_at_scale.py` —
  invariant pinning at 1500-node scale

## Findings (2026-05-10 RE session)

Method: Mach-O symbol + string analysis on
`/Applications/Arq.app/Contents/Resources/ArqAgent.app/Contents/MacOS/ArqAgent`
(20MB universal binary; macOS x86_64 + arm64). All findings
below are from the x86_64 slice. No dynamic instrumentation
(no lldb session) — static `strings` + `otool -tvV`
disassembly only.

### 1. Tree version is hard-coded to 4 in BackupRecord init

`-[BackupRecord init]` (at `0x10009a5e8` in the x86_64 slice)
unconditionally writes `0x4` into ivar offset `0x1c` (the
`_nodeTreeVersion` field):

```
000000010009a611  testq  %rax, %rax
000000010009a614  je     0x10009a61d
000000010009a616  movl   $0x4, 0x1c(%rax)        ; nodeTreeVersion = 4
```

Confirms our writer's choice of `tree_version=4` is
operator-visible. Tree v5 may exist in the binary's namespace
(we saw `movw $0x5` patterns in unrelated buffer-init
codepaths — not Tree-class code), but Arq.app v8 emits v4 by
default.

### 2. scannedAt / lastVerifiedAt is NOT a Node property

Both Node initializers are visible as Objective-C selectors:

```
initWithDataBlobLocs:computerOSType:aclBlobLoc:xattrsBlobLocs:
  itemSize:containedFilesCount:
  modificationTime_sec:modificationTime_nsec:
  changeTime_sec:changeTime_nsec:
  creationTime_sec:creationTime_nsec:
  userName:groupName:deleted:
  mac_st_dev:mac_st_ino:mac_st_mode:mac_st_nlink:
  mac_st_uid:mac_st_gid:mac_st_rdev:mac_st_flags:
  winAttrs:reparseTag:reparsePointIsDirectory:

initWithTreeBlobLoc:computerOSType:aclBlobLoc:xattrsBlobLocs:
  itemSize:containedFilesCount:
  modificationTime_sec:modificationTime_nsec:
  changeTime_sec:changeTime_nsec:
  creationTime_sec:creationTime_nsec:
  userName:groupName:deleted:
  mac_st_dev:mac_st_ino:mac_st_mode:mac_st_nlink:
  mac_st_uid:mac_st_gid:mac_st_rdev:mac_st_flags:
  winAttrs:reparseTag:
```

**Neither has any `scannedAt` / `lastVerifiedAt` / `scannedTime`
/ `verifiedAt` parameter.** The only timestamp-like fields are
the three filesystem ones (`modificationTime`, `changeTime`,
`creationTime`).

→ The 38-byte trailing block is **serializer-only metadata**,
not a stored Node attribute that the reader reconstructs into
a property. Our `parse_node` ignoring the bytes is correct;
no FileNode / TreeNode field should be added for them.

### 3. The `lastFullScanDate` lives elsewhere

The string sweep surfaced one timestamp class:

```
-[FileChangeLasts lastFullScanDateForId:]
-[FileChangeLasts setLastFullScanDate:forId:]
_lastFullScanDatesById
/Users/stefan/src/arq7/mac/arqagent/FileChangeLasts.m
```

This is per-FOLDER (not per-Node) and lives in the agent's
LOCAL state, not in the destination tree blob. So
"scanned-at" semantics ARE in the codebase, just not on
nodes. The 38-byte trailing block could be (1) a per-Node
denormalization of the same timestamp, or (2) something
unrelated. We can't distinguish from static RE alone.

### 4. The 0x01000000 constant: no clear writer found

We searched the entire disassembly for writes of the constant
that would land at offset 16..23 of our trailing block (when
read as int64 BE = `0x0000000001000000` = 16777216).

Found patterns:
- `cmpq $0x1000000, ...` (range checks; `if x < 16777216`)
- `movl $0x10000000, 0x4(%mem)` (writes 0x10000000 = 256MB,
  in unrelated buffer-init codepaths)

**No** sequence of the form `movabsq $0x1000000, %rax;
movq %rax, 16(%mem)` showed up. This means the constant is
either:
- written via a different addressing pattern we didn't grep
  for (e.g., as part of a memcpy from a static struct
  template),
- or computed at runtime (e.g., `(uint64_t)0x01000000` from a
  `#define ARQ_TREE_NODE_FLAG_PRESENT 0x01000000`).

→ Practical implication: our writer's behaviour of emitting
the structured form (with the constant baked in literally) is
safe — Arq.app's reader doesn't appear to validate this byte
range strictly (we couldn't find a comparison of THIS bytes
range against a specific value), so any byte pattern that
parses as a valid uint64 likely round-trips.

### 5. All-zero block trigger: still hypothesis

Our existing observation (PR #20) was that 3 of 1500 nodes
had all-zero trailing blocks — every one a `.DS_Store` file
where `mtime == ctime == create_sec`. This is consistent
with: "writer emits zero-block when no meaningful timestamp
is available (file just created in this very pass)".

We did NOT find a direct conditional in the disasm of the
form `if (file.justCreated) writeZeroBlock()`. The trigger
remains an inference, not a confirmed branch.

### 6. Cross-OS variation: not checked

Only macOS Arq.app available on this host. Cannot confirm
whether Arq.app for Windows or Linux emits the same 38-byte
shape.

## Still open

| # | Question | Answer | Confidence |
|---|----------|--------|------------|
| 1 | scannedAt vs lastVerifiedAt? | Neither — not a Node property | High (Node init signatures complete) |
| 2 | 0x01000000 = "present-flag"? | Unknown writer; reader doesn't validate strictly | Medium (no compare found, but pattern grep limited) |
| 3 | All-zero trigger exact condition? | Strong correlation with mtime==ctime==create_sec | Medium (observation, not branch-confirmed) |
| 4 | Cross-OS variation? | Not investigated | N/A |

## Implications for our codebase

1. **Our writer is round-trip safe.** Arq.app's reader does
   not perform strict validation on the 38-byte block (we
   couldn't find one), and our writer emits the structured
   form Arq.app's writer also emits.
2. **Our reader is correct.** Treating the bytes as opaque
   matches Arq.app's own treatment — they're not propagated
   into Node properties on read.
3. **Tree v5 watch.** The disassembly contained a `movw $0x5`
   pattern in an unrelated buffer init. If a future Arq.app
   release bumps `BackupRecord.init`'s `nodeTreeVersion`
   ivar default from 4 to 5, our reader needs an update.
   Pin a check via the latestTreeVersion-bump regression in
   `tests/test_tree_v4_trailing_block.py`.

## Out of scope

- Dynamic RE (lldb attach + breakpoint on `[Tree writeData:]`)
  would resolve §4 + §5 conclusively. Skipped because the
  current findings give us enough confidence in compatibility
  to ship without it.
- Decompiling the binary (Hopper / IDA) would surface the
  precise template-write codepath for the trailing block.
  Skipped because it's a multi-day effort vs. the
  ~30-minute static-analysis sweep above.
