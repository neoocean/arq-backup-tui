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

Pre-requisite: `.secrets/sftp.json` + `.secrets/dest_password`
configured (see `docs/COMPAT-SFTP-TESTING.md`).

```sh
cd /path/to/arq-backup-tui
python3 scripts/probe_xattr_blob_bulk.py --max-walk 1000 --json \
    > /tmp/xattr-probe.json 2> /tmp/xattr-probe.err

# Or text summary:
python3 scripts/probe_xattr_blob_bulk.py --max-walk 1000
```

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
