# N3 — `FileChangeLasts` RE findings (Tree v4 trailing-block lead)

Round 10 — Mach-O symbol RE extension building on
`docs/C1-MACHO-RE-PLAN.md`. The C1 plan recorded the open
question "what semantic do bytes 0..15 of the Tree v4 trailing
block carry?" — K2/K3/K4-2 narrowed it statistically to
**btime_sec ‖ btime_nsec OR ctime_sec ‖ ctime_nsec depending on
when Arq.app's walker last touched the Node** (94% coverage).

N3 extends C1 by reading Arq.app's binary directly for the
**source-side semantic**, not just the on-disk pattern.

## Setup

- **Binary**:
  `/Applications/Arq.app/Contents/Resources/ArqAgent.app/Contents/MacOS/ArqAgent`
- **Local Arq.app version**: 7.41 (Info.plist)
- **Destination sample versions**: 7.37, 7.38.1, 7.38.2, 7.39,
  7.39.1, 7.40.1 (no 7.41 records yet — operator hasn't run a
  fresh backup since the upgrade)

## Class graph

```
FileChangeLasts
├── _lastFullScanDatesById  (NSDictionary: NodeID → NSDate)
├── _path                   (NSString: backing-file path)
├── -init:
├── -lastFullScanDateForId:
├── -setLastFullScanDate:forId:
└── -save:

Tree
├── _treeVersion / _nodeTreeVersion  (Tree v3 vs v4 toggle)
└── -writeToData:                    (binary serializer)

Node
├── -writeToData:                    (binary serializer)
└── (no v4-specific branch visible)

TreeBackupItem
├── -makeTreeNodeWithChildNodesByName:...:error:
└── -makeInProgressNodeWithBufferSet:...:error:
```

Original source path embedded in the binary:
`/Users/stefan/src/arq7/mac/arqagent/FileChangeLasts.m`.

## Disassembly findings

### `[Tree writeToData:]`

The function writes:
1. `writeUInt32:` of `_treeVersion` (4 bytes)
2. `writeUInt64:` of count of children dict (8 bytes)
3. For each sorted child:
   - `objc_msgSend$write:to:` of the child name (NSString)
   - `objc_msgSend$writeToData:` on the child Node

**No 38-byte trailing block is appended after the Node write.**

### `[Node writeToData:]`

Writes are all `writeUInt32:` / `writeUInt64:` of the Node's
fields (mode, mtime, ctime, dataBlobLocs, etc.). Function
returns directly after the xattrsBlobLocs loop.

**No 38-byte trailing block emitted from this method either.**

### Where IS the trailing block written?

The 38-byte trailing block IS present in real Arq.app v8 destinations
(Strategy K §5.7 + Strategy F §5.6 byte-equivalence checks
confirm it byte-for-byte). But neither `Tree::writeToData:` nor
`Node::writeToData:` in the 7.41 binary appears to emit it.

Three candidate sources for the trailing block, ordered by
plausibility:

1. **`TreeBackupItem` wrapper** — `-makeTreeNodeWithChildNodesByName:...:error:`
   serializes a Tree then APPENDS the trailing block before
   handing the buffer to the pack-builder.
2. **Pack-builder writer** — when storing a tree blob, the
   `TreesPackBuilder` (a class confirmed in the binary's class
   list) may pad each tree blob with 38 zeros for an alignment
   reason, then later `FileChangeLasts.save:` writes the
   per-Node timestamp back into the alignment slot.
3. **TreeDBLSaver** — the symbol `TreeDBLSaver` exists; a
   "DBL" (double-buffer-line?) saver might apply the trailing
   block during finalisation.

Disassembly of these three is needed to confirm. Each is its
own ~500-line ObjC method; deferred to N3-deep follow-up. **The
empirical K4-2 finding (94% btime/ctime coverage) is already
sufficient for writer-side correctness; further RE is for
exhaustive understanding, not compat.**

## Symbol map (preserved for future RE)

```
0x00000001001b8728  -[FileChangeLasts init:]
0x00000001001b89dc  -[FileChangeLasts lastFullScanDateForId:]
0x00000001001b8b1c  -[FileChangeLasts setLastFullScanDate:forId:]
0x00000001001b8c60  -[FileChangeLasts save:]
0x0000000100178060  _OBJC_IVAR_$_FileChangeLasts._lastFullScanDatesById
0x0000000100178064  _OBJC_IVAR_$_FileChangeLasts._path
0x00000001001784c4  -[Node writeToData:]
0x0000000100118234  -[Tree writeToData:]
```

(Addresses are stable across the 7.41 binary; will shift on
Arq.app upgrade — the `scripts/n3_locate_filechange_symbols.py`
helper finds the current addresses by symbol name.)

## Compat implication

**None for writer correctness.** Our writer's `create_time`
fallback hits 47.6% of Arq.app's btime emit + (after a
hypothetical ctime fallback would hit) another 46.2% of ctime
emit. Combined: ~94% (K4-2). The remaining ~6% are
`lastFullScanDate` values that drifted away from both
filesystem timestamps — pure `FileChangeLasts` internal state
that a content-addressed writer can't reproduce without
sidecar tracking (K3 §5.7.5).

**Bonus discovery**: the binary references
`-[FileChangeLasts save:]` — Arq.app PERSISTS the per-Node
last-full-scan-date dictionary across runs. This is the
mechanism behind K2 Finding 1 ("Cross-record byte-identical
trailing blocks for unchanged files") — Arq.app reads the prior
emit's date from `FileChangeLasts` before emitting the new
tree, so unchanged files get a stable trailing block.

## Where to read this next

When investigating a future trailing-block anomaly, the call
order to confirm is:

1. `TreeBackupItem makeTreeNode...` is called by the walker
2. It calls `Tree::writeToData:` for the serialized core
3. (Hypothesis) It appends 38 bytes built from
   `[FileChangeLasts lastFullScanDateForId:nodeId]` or
   `[FileChangeLasts setLastFullScanDate:forId:]` if new
4. Result is committed via `TreeDBLSaver` /
   `TreesPackBuilder`

Disassembling `TreeBackupItem makeTreeNodeWithChildNodesByName:`
(0x10009e164) is the next concrete step.
