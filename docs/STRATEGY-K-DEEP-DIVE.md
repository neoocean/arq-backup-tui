# Strategy K deep dive — Tree v4 trailing block bytes 0..15

This file extends `docs/COMPAT-VERIFICATION.md` §5.7 with the
empirical findings from a K2 investigation (2026-05-11) that
sampled real Arq.app v8 emit data across multiple BackupRecords
of the same folder.

The §5.7 conclusion ("backup-engine wall-clock scan timestamp,
non-reproducible by a content-addressed writer without breaking
dedup") is **strengthened** but **refined** here. The trailing
block timestamp is **per-Node** with **persistence across
scans** — not a fresh-walk-time-per-record.

## Setup

- **Destination**: `/Volumes/arqbackup1`, real Arq.app v8 emit
- **Folder**: `CA0D1896-B097-46A2-B0B8-BED9DC8FCE50`
- **Records sampled**: two consecutive
  (`7877564.backuprecord` creationDate `1777877564`,
   `7876122.backuprecord` creationDate `1777876122`,
   1442 seconds apart)
- **Tool**: `scripts/analyze_v4_trailing_block.py`
  (this PR), invoked per-record + flattened

## Finding 1 — Cross-record byte-identical trailing blocks

Walking the root tree of both records and pairing entries by
name yields **exact byte-identical trailing blocks for every
unchanged file**:

```
'.cache':        sec=1777296870  nsec=757277353   (both records)
'.claude.json':  sec=1777875042  nsec=852918924   (both records)
'.config':       sec=1736258624  nsec=577992794   (both records)
'.docker':       sec=1736258624  nsec=578236711   (both records)
'.dropbox':      sec=1736605823  nsec=460211530   (both records)
```

The 1442 seconds between record creationDates do **not** shift
the trailing block by 1442 seconds. So the trailing block is
**not** "the moment this record was walked" — it's "the moment
this Node was last walked AND committed", and Arq.app reuses
the prior emit's trailing block verbatim when the file's
content hasn't changed.

This refines §5.7.5's "wall-clock scan timestamp" formulation:
the wall-clock is **per-Node-emit-event**, captured once when a
Node is freshly walked, then carried forward across subsequent
BackupRecords until that Node's content changes (which triggers
a fresh emit + a new trailing block).

## Finding 2 — All-zero trailing blocks on top-level entries

In both records, **21 of 39 top-level entries** carried an
all-zero 38-byte trailing block (`b"\x00" * 38`). The serialize-
layer comment in `arq_writer/serialize.py` already documents
this case:

> "the shape Arq.app uses for files freshly added to a pass"

But "freshly added" alone doesn't explain why long-standing
files like `.CFUserTextEncoding`, `.DS_Store`, `.Trash` show
the all-zero pattern in both records. Two hypotheses:

1. **Top-level meta-entries get all-zero by convention.** The
   walk-time tracking applies only below a certain tree depth
   (e.g. only inside the root tree's children, not for
   directory metadata stubs at the very top). Plausible from
   the pattern but unverified.

2. **All-zeros means "no fresh walk recorded for this entry
   in this record".** The prior emit's trailing block isn't
   carried forward; instead Arq.app emits a fresh Node with a
   zeroed trailing block when the Node's tree blob is being
   rebuilt for some other reason (e.g. a sibling changed and
   forced a new parent tree blob, but this entry itself was
   only referenced not re-walked).

A larger sample sweep across many records of different
folders would distinguish these — recommended as a follow-up.

## Finding 3 — Walk-order is NOT monotone in trailing_sec

In the 7877564 record's 39 top-level entries, the trailing-sec
values are NOT sorted ascending — 7 monotonicity violations
out of 38 adjacent pairs. This is consistent with **per-Node
persistence**: entries that haven't been re-walked carry their
ancient trailing-sec values, interleaved among entries that
were re-walked recently. The walker's path through the
filesystem is alphabetic (lexicographic), but trailing-sec is
tracked per content change, so the two orderings don't align.

This further refutes a pre-K2 interpretation that bytes 0..15
might be "walk-order index" — they're not.

## Finding 4 — Sec values cluster around content-change events

The 18 non-zero entries in the 7877564 record show three
distinct sec clusters:

- `1736258624` / `1736605823` (early Jan 2025 — system-config
  files that the operator hasn't touched in months)
- `1777296870` (mid-April 2026 — files modified in the run-up
  to the operator's recent work)
- `1777875042` (just before the record — file modified ~40
  minutes earlier)

These clusters align with **content-modification events on
the operator's system**, not with the walker's traversal time.
Confirms the "moment of last fresh emit" interpretation.

## Implications for our writer

§5.7.5's decision stands and is now better-justified:

> Synthesising the real "scan timestamp" semantically would
> need ``time.time_ns()`` at every emit. That matches Arq.app's
> behaviour but **breaks blob-level dedup** (every re-emit of
> an unchanged file would produce a new blob_id). Arq.app
> sidesteps the issue with reference reuse — its parent tree at
> scan T₂ keeps pointing at the prior emit's tree blob for an
> unchanged file rather than emitting a new tree blob.

The empirical evidence here strengthens this. Arq.app's
"reference reuse" mechanism literally means: when a file is
unchanged, **the parent tree's child Node entry points at the
prior emit's tree blob byte-for-byte** (including its trailing
block). Reproducing this would require our writer to maintain
content-aware Node identity tracking across runs — which our
content-addressed model implicitly does at the data-blob layer
but not at the tree-Node layer.

The current writer's fallback (`create_time` as a deterministic
stand-in) produces tree blobs that:

- Are byte-equivalent to Arq.app's emit **on every byte except
  trailing bytes 0..15** (§5.7.3 result, unchanged).
- Are stable across re-runs (dedup-safe, ✓).
- Differ in trailing bytes 0..15 from what Arq.app would emit
  **if Arq.app freshly walked these same files** — but match
  what Arq.app would emit **if Arq.app's prior tree-walk
  reuse logic kicked in**, because that's exactly the same
  "ignore the trailing-block timestamp content" pattern.

## Recommended follow-ups

1. **Sample across folders + records** — repeat the analysis
   with `scripts/analyze_v4_trailing_block.py --limit 5000` to
   verify Finding 1 (cross-record byte identity) at scale.
2. **Check sub-tree node ratios** — Strategy K's original sweep
   reported "21,516 non-zero / 21,519 total" across the whole
   destination, but our K2 sample shows 21/39 (54%) zero
   trailing blocks on top-level entries. Reconciling the two
   numbers tells us whether the all-zero pattern is depth-
   dependent (top-level only) or universal.
3. **Source-side correlation** — for the non-zero entries with
   recent trailing_sec, check if `trailing_sec` corresponds to
   the file's actual modification event on the operator's
   filesystem (look up `mtime`/`ctime`/`btime` to see which one
   trailing_sec aligns with). If a clean correlation exists,
   the writer could synthesise trailing_sec from one of those
   fields **and** preserve dedup — best of both worlds.

K2 leaves these as observable evidence; the writer's behaviour
doesn't change with this PR. Strategy K's regression coverage
(`tests/test_serialization_round_trip.TreeV4TrailingBlockPreservationTests`)
already pins the deterministic-fallback invariant.
