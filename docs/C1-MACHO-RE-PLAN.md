# C1 — Tree v4 38-byte trailing-block Mach-O RE plan

## Status

**Deferred.** This work needs `/Applications/Arq.app` installed on
the working machine; the current development host does not have
Arq.app and we do not have legitimate access to install it for
RE purposes. This document captures everything an operator with
Arq.app installed would need to resume the work.

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
