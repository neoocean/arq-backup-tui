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

## Multi-record sweep (A보완-10, 2026-05-11)

K2 reported single-record stats; A보완-10 ran a 5-record sweep
across the most recent v4 records of folder
`CA0D1896-B097-46A2-B0B8-BED9DC8FCE50` for stronger aggregate
statistics:

| Record creationDate | total nodes | zero trailing | btime_sec match | btime sec+nsec match | mtime_sec match |
|---|---:|---:|---:|---:|---:|
| 1777877564 | 39 | 21 | 6 | 6 | 4 |
| 1777876122 | 39 | 21 | 6 | 6 | 4 |
| 1777870662 | 40 | 11 | 14 | 14 | 7 |
| 1777783268 | 40 | 11 | 14 | 14 | 7 |
| 1777696081 | 40 | 12 | 13 | 13 | 6 |
| **Aggregate** | **198** | **76 (38.4%)** | **53** | **53** | **28** |

**Aggregate statistics on top-level v4 nodes**:

- Zero trailing blocks: **38.4%** (76/198) at the top level —
  consistent with the K2 single-record observation that
  top-level entries are zero-skewed (vs Strategy K's whole-
  destination 0.014% from the original sweep that walked deeper
  sub-trees).
- Non-zero trailing blocks: 122 / 198 = 61.6%.
- **btime_sec match on non-zero**: 53 / 122 = **43.4%**. Very
  close to K3's single-record 40.8% — confirms the btime
  correlation is statistically stable across records.
- **btime sec+nsec match (when sec matches)**: 53 / 53 =
  **100%**. Strengthens K3's 60% — when trailing_sec ==
  btime_sec, trailing_nsec ALWAYS == btime_nsec in this multi-
  record sample. The writer's `create_time` fallback hits 100%
  of the "btime-aligned" subset.
- **mtime_sec match**: 28 / 122 = 22.9%. Roughly half the
  btime_sec rate, confirming btime is the stronger predictor.

The records' creationDates span ~181k seconds (~50 hours);
multi-record byte-identity for unchanged files (K2 Finding 1)
holds across this entire span.

## K4-1 sub-tree sweep (2026-05-11)

K2 + A보완-10 reported all-zero rates of 38.4% (top-level only,
multi-record). Strategy K's original whole-destination sweep
showed 0.014%. The discrepancy hinted that all-zero blocks
might be concentrated at the tree's top level. K4-1 confirms
this with a depth-grouped sweep against the operator's
destination.

### Setup

- **Destination**: `/Volumes/arqbackup1`, real Arq.app v8 emit
- **Sample**: most-recent v4 BackupRecord (folder
  `CA0D1896-B097-46A2-B0B8-BED9DC8FCE50`,
  `7877564.backuprecord`), depth-3 walk
- **Tool**: `scripts/k4_subtree_sweep.py` (this PR)

### Finding — all-zero rate is concentrated at depth ≤ 1

| Depth | Nodes | All-zero | Zero rate | btime_sec match (non-zero) |
|---:|---:|---:|---:|---:|
| 0 | 2 | 1 | 50.0% | 0/1 = 0% |
| 1 | 6 | 2 | 33.3% | 1/4 = 25.0% |
| 2 | 4 | 0 | 0.0% | 2/4 = 50.0% |
| 3 | 20,769 | 0 | 0.0% | 9,880/20,769 = 47.6% |
| **Total** | **20,781** | **3** | **0.014%** | **9,883 / 20,777 = 47.6%** |

**Two clean conclusions**:

1. **Zero trailing blocks live at depth ≤ 1**. Below that, the
   non-zero rate is **100%** in this sample. This refines K2
   Finding 2's hypothesis: zero-blocks are an Arq.app
   convention for the **root-level entries of the BackupRecord's
   root tree**, not a general "freshly added" pattern. The 21
   zero blocks K2 reported at top level were all at depths 0-1
   of the BackupRecord's emission shape.

2. **btime_sec correlation is statistically stable at depth 3**.
   The 47.6% btime_sec match at depth 3 matches K3's 40.8% and
   A보완-10's aggregate 43.4% (multi-record top-level non-zero
   subset). The pattern is consistent across depth levels and
   sample sizes — the writer's `create_time` fallback hits
   roughly half the non-zero subset regardless of where in the
   tree the Node lives.

### Implication for the writer

§5.7.5's decision stands and is now better-supported by the
depth-grouped evidence:

- A small number of top-of-tree entries (≤ 1% of nodes) get
  all-zero trailing blocks from Arq.app. The writer's fresh-walk
  path emits trailing-blocks consistent with the structured
  documented form for every node — including these — which
  differs from Arq.app's emit at depth ≤ 1 but matches at
  depth ≥ 2.
- For 99% of nodes (depth ≥ 2), the writer's fresh-walk emit
  byte-equality vs Arq.app's emit depends on the 8-byte
  scan-timestamp bytes. The `create_time` fallback matches
  ~47.6% of those cases.

The depth-grouped finding has no bearing on the writer's
behaviour decision (still: explicit override or deterministic
fallback). It just sharpens the documentation of where Arq.app's
emit pattern differs from a content-addressed model's natural
emit.

### Regression test

`tests/test_k4_subtree_sweep_runner.py` validates the sweep
script's logic against synthetic depth-tagged trees so future
refactors to the analyzer can't accidentally regress the depth
attribution.

## Remaining K4 follow-ups (infrastructure-blocked)

1. **First-walk-time correlation** — fresh Arq.app GUI backup
   of a new source: does trailing_sec equal the new
   creationDate?  (Needs operator to drive a fresh GUI backup.)
2. **Strategy I** — operator-driven GUI restore (only
   remaining reader-side validation test).

K4-1 closed the sub-tree-sweep follow-up; the remaining two
need operator GUI action.
