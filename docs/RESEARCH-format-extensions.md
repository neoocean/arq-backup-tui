# RE Feasibility for Arq formats beyond v0

The v0 reader/writer pair (`arq_writer/`, `arq_reader/`) handles
**standalone-objects** Arq 7 backups. This document tracks
investigation of the formats we deferred — pack containers, the
chunker, Arq 5/6, Arq Cloud — and records what additional capability
turned out to be feasible.

The investigation was iterative: each section is updated as code is
written and tested. Citations point to upstream `arq_restore`
(BSD 3-Clause) source paths so the format claims can be verified
without re-running the local clone.

## TL;DR

| Target | Status | Effort |
| --- | --- | --- |
| Read Arq 7 pack-stored blobs (`isPacked: true`) | ✅ Implemented + tested | Trivial — only need `BlobLoc.offset` / `length` |
| Arq 5/6 `.pack` / `.index` parsers + builders | ✅ Implemented + tested | Spec is fully documented |
| Arq 7 pack file emission (write-side) | 🟡 Format known, not yet implemented | Format is "concatenated ARQOs"; tractable |
| Arq 5/6 `Tree` / `Commit` / `Node` binary parser | 🟡 Spec is documented but multi-version (Tree v12–22 each have different field sets) | Multi-day port; each version variant must be supported |
| Chunker (`chunkerVersion: 3`, `useBuzhash`) | 🔴 No public source | Not feasible to RE; needed only for write-side dedup parity with Arq.app |
| Arq Cloud Backup format | 🔴 Out of scope | Separate product, separate restore tool |

The big wins this iteration: the v0 reader gained `isPacked: true`
support **without needing to know the pack file's framing at all**,
and Arq 5/6 `.pack` / `.index` files are now fully round-trippable
through `arq_reader.arq5_pack`.

---

## 1. Reading pack-based blobs (no pack-format knowledge needed)

**Verdict**: ✅ **Implemented + tested** —
[`arq_reader/restore.py`](../arq_reader/restore.py)`::_fetch_blob`,
[`tests/test_reader_pack.py`](../tests/test_reader_pack.py).

### Hypothesis (confirmed)

A `BlobLoc` with `isPacked: true`, `relativePath`, `offset`, and
`length` is all the information needed to extract a blob from a pack
file. The pack file's overall header / footer / index is **irrelevant
for the read path**; the reader slices the `[offset, offset+length)`
range and treats it identically to a standalone object (ARQO magic
check → decrypt → LZ4-unwrap).

### Confirmation from arq_restore source

`arq7restore/Arq7BlobReader.m::dataForBlobLoc:` performs exactly this:

```objc
if (theBlobLoc.isPacked) {
    NSRange range = NSMakeRange((NSUInteger)theBlobLoc.offset,
                                 (NSUInteger)theBlobLoc.length);
    rawData = [_conn contentsOfRange:range ofFileAtPath:relativePath
                            delegate:_delegate error:error];
} else {
    rawData = [_conn contentsOfFileAtPath:relativePath
                                  delegate:_delegate error:error];
}
if ([Arq7EncryptedObjectDecryptor isEncryptedData:rawData]) {
    rawData = [dec decryptData:rawData error:error];
}
if (theBlobLoc.compressionType == kArq7CompressionTypeLZ4) {
    rawData = [self lz4Decompress:rawData error:error];
}
```

The entire `arq7restore/` directory contains zero references to
`treepacks`, `blobpacks`, `largeblobpacks`, `standardobjects`, or
`.pack` — the Arq 7 read path is 100% BlobLoc-driven.

### Implementation

`_fetch_blob` previously raised `NotImplementedError` on
`isPacked: true`. We removed that restriction and rewrote the function
to mirror `Arq7BlobReader.m` exactly:

- Pack-based: `Backend.read_range(path, offset, length)`.
- Standalone: `Backend.read_all(path)`.
- Magic-gated decrypt — bytes that don't start with `b"ARQO"` are
  treated as plaintext (legal per spec for unencrypted backups).
- `compressionType`: 0 (none), 1 (Gzip via stdlib), 2 (LZ4) — all
  three branches implemented.

### Tests

`tests/test_reader_pack.py` — 5 tests covering:

- Multiple ARQOs concatenated into one pack file with recorded
  offsets, read individually via `BlobLoc(isPacked=True, ...)`.
- Padded pack with non-sequential read order.
- `compressionType=0` and `compressionType=1` (Gzip legacy) blobs.
- Unencrypted bytes (no ARQO wrapper) — pass through unchanged.

All 5 pass, demonstrating that the reader handles every pack-stored
blob configuration arq_restore handles.

---

## 2. Arq 5 / Arq 6 `.pack` and `.index` formats

**Verdict**: ✅ **Implemented + tested** —
[`arq_reader/arq5_pack.py`](../arq_reader/arq5_pack.py),
[`tests/test_arq5_pack.py`](../tests/test_arq5_pack.py).

### Format (verified against spec + arq_restore source)

The Arq 5 spec (`arq5_data_format.txt`, §"Pack Index Format" /
"Pack File Format") and the `repo/PackIndex.m` /
`repo/PackBuilder.m` source agree exactly on the byte layout.

**`.index` file**:

```
4 bytes      magic 0xff 0x74 0x4f 0x63
4 bytes BE   version (= 2)
1024 bytes   fanout[0..255]: cumulative count of objects whose first
             SHA-1 byte is ≤ index. fanout[255] = total object count.
N × 40 bytes object entries (sorted by SHA-1):
                 8 BE  offset in pack
                 8 BE  data length
                 20    SHA-1 of object plaintext
                 4     padding (zero)
[Glacier-only optional metadata block]
20 bytes     SHA-1 trailer of all bytes preceding it
```

Cite: `repo/PackIndex.m`'s `index_object` / `pack_index` C structs
and the parse loop. The Glacier extension is observed in spec but
not in `PackIndex.m`'s read path; we tolerate it but don't
synthesize it.

**`.pack` file**:

```
4 bytes      signature "PACK" (0x50 0x41 0x43 0x4b)
4 bytes BE   version (= 2)
8 bytes BE   object count
N entries:
                 [String: mimetype]      (Arq emits null = 1 byte 0x00)
                 [String: downloadName]  (null)
                 8 BE     data length
                 N bytes  data
20 bytes     SHA-1 trailer of all bytes preceding it
```

Cite: `repo/PackBuilder.m::writeIndex:pack:`.

### Implementation

`arq5_pack.py` exposes:

- `parse_pack_index(bytes) -> List[PackIndexEntry]` — verifies magic,
  version, and SHA-1 trailer; returns sorted entries.
- `parse_pack_file(bytes) -> List[PackEntry]` — verifies signature,
  trailer, and per-entry framing; returns `(mimetype, name, data)`
  tuples.
- `build_pack_index(entries) -> bytes` — builds a fresh `.index`,
  computing fanout + sorting + SHA-1 trailer. Useful for write-side
  parity work.
- `build_pack_file(entries) -> bytes` — builds a fresh `.pack`.

### Tests

`tests/test_arq5_pack.py` — 10 tests:

- Empty index/pack edge cases.
- Three-entry round-trip: build → parse → assert sorted.
- Fanout correctness against a synthetic SHA-1 distribution.
- Corrupted-trailer rejection (both files).
- Bad-magic / bad-signature rejection.
- **Cross-reference test**: build a pack, build a matching index,
  and verify each index entry's offset, when used to slice the
  pack, lands on the expected per-entry header followed by the
  recorded data. This is the spec invariant arq_restore relies on.

### What's missing

- Arq 5 **content parser** (`Tree`, `Commit`, `Node` binary). Spec
  is fully documented but multi-version: `TreeV022` is just one
  variant — Tree versions 11–22 each gate or remove fields, and
  Commit versions 3–12 do likewise. Writing one parser per version
  is mechanical but bulky (probably 600–800 LOC). Deferred until
  there's a concrete use case.

---

## 3. Arq 7 pack file format (write-side)

**Verdict**: 🟡 Format known with high confidence, not yet
implemented as a writer feature.

### Evidence

`arq7restore/Arq7BlobReader.m` reads pack-stored Arq 7 blobs by
slicing `[offset, offset+length)` and decrypting the result as an
ARQO directly — no per-entry header, no skipped bytes. Combined with:

- The example BlobLoc in the official spec (length = 356 for a small
  tree blob, consistent with a single small ARQO without per-entry
  framing — the Arq 5/6 per-entry header would add 10 bytes per
  entry, but the spec example shows length = bare 356).
- The complete absence of pack-format references in the entire
  `arq7restore/` directory.
- `repo/PackBuilder.m` is in the legacy Arq 5/6 code path (uses
  SHA-1, emits the "PACK" signature) — there's no Arq 7 counterpart
  in arq_restore (because arq_restore doesn't write Arq 7 backups).

The simplest hypothesis consistent with all evidence: an Arq 7
`.pack` file is a plain concatenation of `EncryptedObject` blobs
with no per-entry framing. The matching `BlobLoc.offset` records
the absolute byte offset of each ARQO's start; `BlobLoc.length`
records its size.

### Already proven by current tests

`tests/test_reader_pack.py::PackByOffsetTests::test_three_blobs_in_one_pack`
synthesizes exactly that: concatenates three writer-produced ARQOs
into one file, builds matching BlobLocs with computed offsets, and
demonstrates the reader can extract each one. So the "concatenated
ARQOs" hypothesis is **producer-side proven** — what's missing is
upgrading `arq_writer` to emit packs by default instead of standalone
objects.

### Implementation sketch (deferred)

```text
PackBuilder accumulates ARQOs into an in-memory buffer:
    .add_arqo(arqo_bytes) -> (offset, length)
When buffer ≥ maxPackedItemLength threshold or close():
    .flush() -> writes buffer to <treepacks|blobpacks|...>/<shard>/<id>.pack
    plus a parallel .index file (if Arq 7 has one — open question).

Backup orchestrator:
    instead of writing each blob standalone, route through a
    PackBuilder per family. The resulting BlobLoc has
    isPacked=true, relativePath=<pack path>, offset=<recorded>,
    length=<recorded>.
```

The remaining unknown is the Arq 7 `.index` format. arq_restore
doesn't ship Arq 7 index code, the spec doesn't document one, and
inspection of operator-supplied real Arq 7 destinations (which we
don't have) would be required. The reader doesn't need it (BlobLoc
is sufficient), and the writer can also skip it if Arq.app builds
its own index on first read — to be tested empirically against a
real Arq.app once such testing is feasible.

---

## 4. Chunker (`chunkerVersion: 3`, `useBuzhash`)

**Verdict**: 🔴 **Not feasible to RE without leaked source or live
Arq.app inspection**. Required only for write-side dedup parity
with Arq.app — not for reading or for fresh-backup writing.

### What's published

- `backupconfig.json` records `chunkerVersion: 3` and the plan
  carries a `useBuzhash` boolean. (Buzhash is a known content-defined
  chunking algorithm — a polynomial-rolling hash similar to Rabin's,
  invented by Robert Uzgalis.)
- The Arq 5 spec mentions "rolling checksum algorithm" and
  "breaks up large files into multiple blobs", with no parameters.
- arq_restore ships **zero** chunker source code: `find . -iname
  "*chunk*" -o -iname "*buzhash*"` returns one match,
  `cocoastack/io/ChunkedInputStream.m`, which is HTTP chunked
  transfer encoding, not file content chunking.

This is consistent with arq_restore being read-only — restoration
just concatenates `dataBlobLocs` in the order the writer emitted
them. The chunker is purely a write-side artifact.

### Implications

- For **reading any Arq backup**: chunker is irrelevant. Done.
- For **writing a fresh Arq backup that Arq.app can restore**: the
  chunker doesn't have to match Arq.app's. Any deterministic
  chunking strategy (or no chunking — one blob per file) produces
  a valid backup; restore concatenates chunks regardless of how
  they were split. ✅ Already what the v0 writer does.
- For **appending to an existing Arq.app backup with proper dedup
  of modified-in-place files**: the chunker would have to match.
  Without leaked source, we can only RE this by:
  1. Inspecting real Arq.app `dataBlobLocs` patterns across
     versions of a slowly-modified file. (Possible in principle;
     requires operator data.)
  2. Black-box testing: feed Arq.app inputs of varying sizes and
     observe chunk boundaries via the resulting blob_id sequence.

Either path is feasible but neither is sandbox-runnable today.

---

## 5. Arq 5 / Arq 6 read path

**Verdict**: 🟡 Tractable but bulky. Deferred.

### What's known (fully)

- Backup folder UUIDs live under `bucketdata/<folder_uuid>/`.
- Latest commit SHA-1 is at `bucketdata/<folder_uuid>/refs/heads/master`
  with a trailing `Y`.
- Commit binary format ("CommitV012", many versions back to V003).
- Tree binary format ("TreeV022", many versions: V011–V022).
- Node binary format (within Trees, also version-gated).
- Blob storage: under `objects/<sha1>` for large files, `packsets/
  <folder_uuid>-(blobs|trees)/<sha1>.{pack,index}` for small.
- `BlobKey` (Arq 5's BlobLoc analogue): SHA-1, encryption-key
  stretching flag, optional Glacier metadata.

### Why it's bulky

Each Tree version adds / removes fields (`xattrs_are_compressed`,
`acl_compression_type`, `aggregate_size_on_disk`, `create_time_*`,
`missing_node_*`). A correct parser needs to dispatch on the version
prefix string and apply the right field schema. Estimated size:
~600–800 LOC for full Tree/Commit/Node coverage.

### What's already in place

`arq_reader.arq5_pack` (this iteration) handles the
`.pack` / `.index` retrieval layer. A future iteration would add a
`Tree`/`Commit`/`Node` binary parser on top, plus an Arq 5 walker
analogous to `arq_reader.restore.Restore` that follows BlobKey
references through commits → trees → file blobs.

---

## 6. Arq Cloud Backup format

**Verdict**: 🔴 **Out of scope**. Separate product
(`arqbackup/arqcloudrestore`), separate format (documented at
[arqbackup.com/docs/arqcloudbackup/...](https://www.arqbackup.com/docs/arqcloudbackup/English.lproj/dataFormat.html)
), separate restore tool. Not addressable by reusing arq_writer /
arq_reader infrastructure without significant new format work.

---

## Source map (offline copies)

Cloned to `/tmp/arq-re/arq_restore/` during this investigation:

| File | What's there |
| --- | --- |
| `arq7restore/Arq7BlobReader.m` | Pack vs. standalone read + ARQO decrypt + LZ4 decompress orchestration |
| `arq7restore/Arq7BlobLoc.{h,m}` | BlobLoc model |
| `arq7restore/Arq7EncryptedObjectDecryptor.{h,m}` | ARQO format internals |
| `arq7restore/Arq7Tree.{h,m}` | Arq 7 Tree binary parser (matches our serialize/parse modules) |
| `repo/PackIndex.{h,m}` | `.index` file reader + `index_object` / `pack_index` C structs |
| `repo/PackIndexEntry.{h,m}` | Single index entry |
| `repo/PackIndexGenerator.{h,m}` | Arq 5/6 `.index` writer |
| `repo/PackBuilder.{h,m}` | Arq 5/6 `.pack` writer + per-entry header layout |
| `arq5_data_format.txt` | Published Arq 5 spec |
| `arq7_data_format.html` | Published Arq 7 spec |
| `cocoastack/io/ChunkedInputStream.{h,m}` | HTTP chunked-transfer parser (not file chunking) |

All paths are upstream-stable:
`https://github.com/arqbackup/arq_restore/blob/master/<path>`.
