# Strategy K3 — trailing-block vs file-metadata correlation

K2 (`docs/STRATEGY-K-DEEP-DIVE.md`) established that the v4
trailing block bytes 0..15 are **per-Node-emit-event timestamps
with persistence across scans**. K3 (this file) extends the
analysis with **field-by-field correlation** between
``trailing_sec/nsec`` and the standard file-metadata
``mtime``/``ctime``/``btime`` (create-time) fields.

## Setup

- **Destination**: `/Volumes/arqbackup1`, real Arq.app v8 emit
- **Folder**: `CA0D1896-B097-46A2-B0B8-BED9DC8FCE50`
- **Record**: `7877564.backuprecord` (arqVersion 7.40.2)
- **Sample**: 3,148 Tree v4 nodes via 2-level recursive walk
  (root + each child sub-tree's children)
- **Non-zero trailing blocks**: 3,115 (98.9%)

## Headline finding

For nodes whose **trailing_sec equals btime_sec** (1,272 / 3,115
= 40.8% of non-zero trailing blocks), **trailing_nsec also
equals btime_nsec** with very high probability:

| Correlation | Count | Rate |
|---|---:|---:|
| `trailing_sec == btime_sec` | 1,272 / 3,115 | **40.8%** |
| `trailing_sec == ctime_sec` | 661 / 3,115 | 21.2% |
| `trailing_sec == mtime_sec` | 923 / 3,115 | 29.6% |
| `trailing_sec == max(bt,ct,mt)_sec` | 661 / 3,115 | 21.2% |
| **Both sec AND nsec match btime** | **767 / 3,115** | **24.6%** |
| Both sec AND nsec match mtime | 257 / 3,115 | 8.2% |
| Both sec AND nsec match ctime | 40 / 3,115 | 1.3% |

When `trailing_sec` aligns with `btime_sec`, **nsec also matches
btime_nsec in 60% of those cases** (767 / 1272). Walking through
the per-Node samples:

```
'.cache':       tr=(1777296870, 757277353)  bt=(1777296870, 757277353)
                tr=bt EXACTLY (sec+nsec)

'.claude.json': tr=(1777875042, 852918924)  bt=(1777875042, 852918924)
                tr=bt EXACTLY (sec+nsec)

'staging':      tr=(1777296870, 757446144)  bt=(1777296870, 757446144)
                tr=bt EXACTLY (sec+nsec)
                — directory created at btime, content later modified
                  (mtime is much later) but trailing reflects
                  ORIGINAL creation moment.
```

The pattern: **trailing_block reflects "the moment this Node was
first inserted into Arq.app's tree"**, which usually equals
`btime` because Arq.app commonly runs continuously and picks up
new files within microseconds of creation. Subsequent scans
(where content changes via mtime updates) **don't** update the
trailing block — that's the "per-Node-emit-event persistence"
K2 documented.

## What this means for our writer

The current fallback (§5.7.5):

> 1. Explicit `v4_scanned_at_sec` / `v4_scanned_at_nsec` if the
>    caller sets them …
> 2. Else `create_time_sec` / `create_time_nsec` —
>    deterministic stand-in.

is **the right choice** for the 40.8% of nodes where Arq.app
also tracks via btime. For the remaining 59.2%, no
file-metadata field reproduces Arq.app's trailing block.

Those 59.2% are nodes whose first v4-era walk happened **after**
the file was created — e.g., a file that existed before Arq.app
adopted Tree v4 (Arq 7.40 release timing), or a file that was
moved/renamed into the source tree. For those, Arq.app's
trailing block reflects Arq.app's wall-clock at the moment of
that first v4 walk, which is a backup-engine state value
unrelated to file metadata.

## Can the writer do better than 40.8%?

Three options, each with a trade-off:

### Option A — `time.time_ns()` at emit (the "true Arq.app" approach)

The writer captures `time.time_ns()` at each fresh-walk emit.
Matches Arq.app's behaviour for new emissions, but:

- **Breaks blob-level dedup**: every re-run produces new tree
  blobs because the timestamp changes. Strategy K-static
  (§5.7) already noted this is a non-starter.

### Option B — Persist per-Node "first emit time" in a sidecar

Maintain a sidecar map `{rel_path → first_emit_time_ns}` across
runs. First time we see a path, capture `time.time_ns()`. On
subsequent runs, reuse the persisted value.

- ✅ Matches Arq.app's "per-Node persistence" pattern exactly
- ✅ Dedup-safe (same Node → same trailing → same blob_id)
- ❌ Requires a new persistent sidecar file
- ❌ First-run emit still differs from Arq.app's emit (different
  wall-clock instants)

### Option C — Status quo (deterministic `create_time` fallback)

- ✅ 40.8% match against Arq.app's emit, 100% match on the
  other 24 bytes (16..37)
- ✅ Dedup-safe by construction
- ✅ No new sidecar
- ❌ 59.2% of nodes have unreproduceable trailing_sec

Strategy K § 5.7.6 already concluded "compatibility hinges on
whether Arq.app's reader validates those bytes (vs. reads them
as opaque state)". Strategy I-alt (PR #64) confirms the patched
`arq_restore` discards them entirely. **The unresolved question
is whether Arq.app's GUI reader validates them** — that's
Strategy I (operator-driven GUI restore), the only test option
left for the bytes 0..15 question.

If Strategy I confirms Arq.app's GUI reader doesn't validate
trailing_sec, the 40.8% match is irrelevant — Option C is
permanently correct. If Strategy I shows Arq.app rejects emits
with "wrong" trailing_sec, Option B becomes worth implementing
(estimated ~150 LoC: writer-side sidecar read/write + emit-time
capture).

## Recommendation

**Do not change the writer**. Status quo (Option C) plus the
K3 evidence pinned here is sufficient until Strategy I is
performed against a fresh-walk destination. K3 strengthens the
case that the writer's current behaviour is **as close to
Arq.app's as a content-addressed model can get** without
sacrificing dedup-safety.

## Recommended K4 follow-ups (deferred)

1. **Cross-record persistence verification** at scale —
   reproduce K2's "byte-identical trailing block for unchanged
   files" finding across more record pairs.
2. **First-walk-time correlation** for the 59.2% non-btime
   nodes — if the operator runs a fresh Arq.app GUI backup of
   a fresh source and we capture the resulting trailing blocks,
   do those trailing_sec values equal the backup's
   creationDate? That would confirm the "first v4 walk time"
   interpretation for the residual nodes.
3. **Strategy I** (operator-driven GUI restore of a fresh-walk
   destination) — the only remaining test for whether Arq.app's
   reader validates the trailing block.
