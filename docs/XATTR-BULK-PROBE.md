# Operator guide: bulk xattr-format probe

## Why this exists

PR #25 reverse-engineered Arq.app's xattr binary format
(`XAttrSetV002`) from a single 68-byte blob. Our writer +
reader implement that format byte-for-byte, but we've only
formally validated **one** xattr name (`com.apple.provenance`)
on **one** node. To gain confidence the format hypothesis
holds across the full diversity of real-world xattrs (Finder
labels, Spotlight comments, ResourceFork, custom `user.*`
attributes, etc.), an operator needs to run the bulk probe
against their actual destination.

## Why we don't run it in CI

The probe walks every xattr-bearing node in a destination,
fetches each xattr blob via SFTP, and decodes it. On the
operator's Hetzner Storage Box this is hundreds of slow SFTP
round-trips — not something we want firing on every CI run,
and the operator's destination credentials shouldn't leave
their machine.

## How to run it

Two backend modes are supported.

### A. SFTP destination

Pre-requisite: `.secrets/sftp.json` + `.secrets/dest_password`
configured (see `docs/COMPAT-SFTP-TESTING.md`).

```sh
cd /path/to/arq-backup-tui
python3 scripts/probe_xattr_blob_bulk.py --max-walk 1000 --json \
    > /tmp/xattr-probe.json 2> /tmp/xattr-probe.err

# Or text summary:
python3 scripts/probe_xattr_blob_bulk.py --max-walk 1000
```

### B. Local-mounted destination (recommended for bulk runs)

When the destination is reachable as a local filesystem path (NAS
share, USB volume, locally-rsynced copy), `--local-root` skips
SFTP entirely. Only `.secrets/dest_password` (or
`ARQ_TEST_DEST_PASSWORD`) is needed; SFTP credentials are not
read.

```sh
python3 scripts/probe_xattr_blob_bulk.py \
    --local-root /Volumes/arqbackup1 \
    --max-walk 1000 --json \
    > /tmp/xattr-probe.json
```

`--local-root PATH` points at the destination root — the directory
that contains the `<computer-uuid>/` subdirectories, not the
computer-uuid directory itself.

## What to look for in the output

The JSON report has:

```json
{
  "total_xattr_blobs_observed": 247,
  "format_distribution": {"XAttrSetV002": 247},
  "decoded_cleanly": 247,
  "anomalies": [],
  "top_xattr_names": {
    "com.apple.provenance": 89,
    "com.apple.metadata:_kMDItemUserTags": 32,
    "com.apple.FinderInfo": 28,
    ...
  }
}
```

**Expected outcomes**:

- `format_distribution` is `{"XAttrSetV002": N}` for all N —
  every blob is in the format we expect. **Anything else
  invalidates the hypothesis** + needs a fresh RE pass.
- `decoded_cleanly` equals `total_xattr_blobs_observed` —
  every blob round-trips through `deserialize_xattrs`
  without error.
- `anomalies` is empty.

**If anomalies appear**:

1. Capture the failing blob's hex via the `samples_first_5`
   array.
2. Add the hex as a regression test fixture in
   `tests/test_xattrs.py::SerializeRoundTripTests::test_deserialize_real_operator_blob`.
3. Open an issue + paste the anomaly section so we can
   refine the format hypothesis.

## How to share findings safely

The probe output may include xattr **names** that reveal
filesystem structure (e.g.
`com.apple.metadata:kMDItemUserTags` confirms macOS, file
paths visible in some hex dumps). Before sharing publicly:

- Strip absolute paths from the `node` field in the JSON.
- Inspect any `text_preview` / `hex_preview` for sensitive
  content (Finder comments may contain personal notes).
- Prefer redacted summaries (counts + format-distribution +
  anonymous name list) over raw blob hex.

## Status as of latest run

The 30-node sample from PR #25's initial probe + the
single-blob bulk probe attempt in PR #28 both confirm
`XAttrSetV002` as the only observed format. Bulk probe at
1000+ nodes scale is awaiting a controlled-environment
re-run — see this document's "How to run it" section.

### 2026-05-10 attempt — operator-side run

Ran against operator's SFTP destination with progressively
smaller `--max-walk` caps:

| --max-walk | wall time | result |
|-----------:|----------:|--------|
| 500 | killed at 60min (still running) | n/a — too slow over this SFTP link |
| 50  | killed at ~30min (still running) | n/a — same |
| 10  | ~5min | completed; 0 xattr-bearing nodes in scope |

The 10-node run finished cleanly. The "anomalies" reported
were ARQO-too-short failures on the root nodes' xattr fetches.
We initially read these as a benign "root has no xattrs"
signal; the 2026-05-10 local-mount run below disproved that —
they were actually the dict-vs-dataclass `_fetch_blob` shape
bug fixed in this PR. With the bug fixed, root xattrs decode
correctly when present and are simply skipped (without an
anomaly entry) when the BlobLoc list is empty.

### 2026-05-10 follow-up — local-mount run

Ran against the same destination via `--local-root
/Volumes/arqbackup1` (a locally-mounted NAS share holding the
real Arq.app v8 output). Two passes:

| --max-walk | wall time | observed | decoded_cleanly | anomalies |
|-----------:|----------:|---------:|----------------:|----------:|
| 1,000      | <5s       | 11,874   | 11,874          | 0         |
| 10,000     | 6m14s     | 21,318   | 21,318          | 0         |

Both runs report `format_distribution: {"XAttrSetV002": N}`
exclusively. The 10k-walk numbers stabilise on the rare-name
counts (`com.apple.FinderInfo`: 4,
`com.apple.timemachine.private.directorycompletiondate`: 3,
`purgeable-drecs-fixed`: 2), indicating the BFS reached the
edges of every backup folder's tree.

**Hypothesis status: confirmed at n=21,318.** The single-blob
RE finding from PR #25 generalises across the full diversity
of xattrs the operator's macOS sources carry — single-attr
and multi-attr blobs, `com.apple.*` and un-prefixed names,
short (11-byte) and long (32-byte) values.
