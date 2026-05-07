# Arq Backup Creation Feasibility — Research Notes

## Executive summary

Building an Arq-7-format backup *writer* is technically realistic for a constrained
"operator-side" tool but is meaningfully harder than the validator we already have.
The cryptographic envelope (`encryptedkeyset.dat`, `EncryptedObject` / `ARQO`,
HMAC-SHA256 chain, PBKDF2-SHA256 key derivation), the binary `Tree` / `Node` /
`BlobLoc` format, and the JSON config files are all fully published in
[Arq's official Arq 7 data format spec](https://github.com/arqbackup/arq_restore/blob/master/arq7_data_format.html)
and the [Arq 5 spec](https://github.com/arqbackup/arq_restore/blob/master/arq5_data_format.txt).
The two genuinely under-documented pieces are (a) the on-disk container layout of
Arq 7 `treepacks/` and `blobpacks/` `.pack` files (the spec describes what they
*contain* but never gives a header/index byte layout for the Arq 7 variant the way
the Arq 5 spec does for `.pack`/`.index`), and (b) the chunker — `chunkerVersion: 3`
plus a `useBuzhash` flag, with no published parameters for window size, target chunk
size, hash mask, or content-defined-chunking polynomial. **No public open-source
project writes Arq backups today.** All third-party tools are read/restore only,
and several explicitly say so. A "store every file as one or more standalone
unpacked objects under `standardobjects/`" creator that skips the chunker is
plausible without RE; full Arq.app round-trip restore is plausible but carries
real RE risk on the pack containers and chunker.

---

## 1. Official Arq documentation (what is and isn't published)

The canonical spec is shipped *inside the `arq_restore` repo* (which is Haystack's
own open-source restore tool), so we can read it without scraping arqbackup.com:

- **Arq 7 data format** — [`arq_restore/arq7_data_format.html`](https://github.com/arqbackup/arq_restore/blob/master/arq7_data_format.html)
  (mirrors `https://www.arqbackup.com/documentation/arq7/English.lproj/dataFormat.html`).
- **Arq 5 data format** — [`arq_restore/arq5_data_format.txt`](https://github.com/arqbackup/arq_restore/blob/master/arq5_data_format.txt)
  (mirrors `https://www.arqbackup.com/arq_data_format.txt`).
- **Arq Cloud Backup format** — [arqbackup.com/docs/arqcloudbackup/...](https://www.arqbackup.com/docs/arqcloudbackup/English.lproj/dataFormat.html)
  (separate product, separate repo `arqbackup/arqcloudrestore`).

What the Arq 7 spec **fully documents**:

- `backupconfig.json`, `backupfolders.json`, `backupfolder.json`, `backupplan.json`
  (literal sample JSON shown).
- `backuprecord` file: a property-list-like dict whose `node` key is a `Node`,
  LZ4-compressed and (optionally) `EncryptedObject`-wrapped.
- `Node` binary layout (full field-by-field listing including `dataBlobLocs`,
  `xattrsBlobLocs`, mtime/ctime ns, mac_st_*, win_attrs, etc.).
- `Tree` binary layout (`UInt32 version`, `UInt64 count`, repeated
  `[String:childName] [Node:childNode]`).
- `BlobLoc` binary layout (`blobIdentifier`, `isPacked`, `relativePath`, `offset`,
  `length`, `stretchEncryptionKey`, `compressionType`).
- `EncryptedObject` `ARQO` envelope: HMAC-SHA256 over (master IV ‖ encrypted
  data-IV+session-key ‖ ciphertext), AES-256-CBC + PKCS7, 256-bit session key
  reused for up to 256 objects.
- `encryptedkeyset.dat`: PBKDF2-SHA256, 200,000 rounds, 64-byte derived key, 64-byte
  encryption key, 64-byte HMAC key, 64-byte blob-identifier salt.
- LZ4 wrapper (4-byte big-endian length + LZ4 block).
- The pseudo-types `[Bool]`, `[String]`, `[UInt32]`, `[Int32]`, `[UInt64]`,
  `[Int64]`.

What the Arq 7 spec **does not document**:

- The byte layout of `.pack` files in `treepacks/` and `blobpacks/`. The spec only
  says "stored ... in a 'pack' file within the 'treepacks' subdirectory." The
  Arq 5 spec does fully document its older `.pack` and `.index` format
  (`PACK` magic, 4-byte version, 8-byte object count, per-object mimetype/name/data,
  trailing SHA1; index has `ff 74 4f 63` magic, 256-entry fanout, etc. — see
  arq5_data_format.txt lines ~416–520). The Arq 7 pack container appears to be
  *implicit* — readers like `arq_restore`'s `repo/PackIndex.m` and
  `arq7restore/Arq7BlobLoc.m` (BSD 3-Clause source) reuse very similar logic.
- The chunker. `backupconfig.json` carries `"chunkerVersion": 3` and the backup
  plan carries `"useBuzhash": 0|1`. Neither the parameters (window, mask, target
  size) nor a reference implementation are published. This is the single biggest
  undocumented piece for byte-identical round-trip with Arq.app.
- Any "third-party backup-creation API." The only Arq-shipped CLI is
  [`arqc`](https://www.arqbackup.com/documentation/arq7/English.lproj/arqc.html),
  which is a *control* utility (start/stop plans, set password, retrieve status)
  for the proprietary `Arq.app`. It does not expose backup writing as a library.

Marketing / openness commitments: Arq's site advertises an "open, published data
format" (linked from arqbackup.com top nav) — open *to read*, not an SDK.

---

## 2. Public Arq-related projects on GitHub

| Project | URL | Last update | Scope | Lang | License |
|---|---|---|---|---|---|
| arqbackup/arq_restore | https://github.com/arqbackup/arq_restore | 2026-04-16 | **Restore only**, Arq 5/6/7. Canonical reference impl. Includes the Arq 7 spec HTML and Arq 5 spec txt. | C / Objective-C | BSD 3-Clause |
| arqbackup/arqcloudrestore | https://github.com/arqbackup/arqcloudrestore | active | **Restore only** for the separate "Arq Cloud Backup" product. | Objective-C | BSD-style (Haystack) |
| asimihsan/arqinator | https://github.com/asimihsan/arqinator | 2026-03-10 | **Restore only**. Tested only against Arq 4.14.5; cross-platform (S3/GCS/SFTP). | Go | Apache-2.0 |
| nlopes/arq | https://github.com/nlopes/arq | 2026-03-19 | **Read-only library** for Arq 4.5+. README explicitly: "this library allows reading files but never writing, so it's not possible to build a full replacement of Arq with this library." | Rust | MIT |
| nlopes/evu | https://github.com/nlopes/evu | 2025-07-09 | **Restore only** CLI on top of `nlopes/arq`. | Rust | MIT |
| stevekstevek/arqfastrestore | https://github.com/stevekstevek/arqfastrestore | 2026-04-23 | **Restore only**, Arq 5 + Arq 7, parallel/sort-by-pack. Newest serious effort. | Rust | (repo) |
| sholiday/arq | https://github.com/sholiday/arq | 2023-07 | **Read/explore/restore** library. Stale. | Go | (repo) |
| tcsc/larq | https://github.com/tcsc/larq | 2021-04 | Self-described "Cleanroom reader (and **eventually writer**) for the Arq backup tool" — **the only repo we found that even mentions writing**. 0 stars, no commits in ~5 years; never reached writer functionality. | Rust | (repo) |
| larcho/arq_restore, a5an0/arq_restore | (forks) | varies | Forks of the official restore. | Obj-C | BSD |

Notes on what the search did **not** turn up:

- No PyPI package named `arq-backup` or `arq7` for backup creation. (The PyPI
  package `arq` is unrelated — it's a Python async job-queue library by samuelcolvin.)
- No npm package, no Rust crate, no Homebrew formula that creates Arq backups.
- The GitHub topic `arq-backup` returns **zero** repositories
  (`mcp__github__search_repositories topic:arq-backup` total_count = 0).
- Code search for `"ARQO" "encryptedkeyset"` across GitHub returns 7 hits, all of
  which are either the spec mirror, validator/restore tools, or references — no
  writer.

Sources for the above: GitHub repo search via MCP, plus README content fetched
from `raw.githubusercontent.com/arqbackup/arq_restore/master/README.markdown`,
`asimihsan/arqinator/master/README.md`, `nlopes/arq/master/README.md`,
`nlopes/evu/master/README.md`, `stevekstevek/arqfastrestore/master/README.md`.

---

## 3. Arq's own publicly shipped code

Only one production tool: **`arqbackup/arq_restore`** (BSD 3-Clause, "Copyright
2009-2026 Haystack Software LLC"; latest README copyright year is 2026, last
release v5.7 in 2017 but the master branch keeps receiving updates — the spec
HTML inside the repo shows commit `0911ce27...`). Supports Arq 5, 6, and 7
*for restore*, against AWS S3 or local filesystem. README at
[arq_restore/README.markdown](https://github.com/arqbackup/arq_restore/blob/master/README.markdown).

Highly relevant source files for our purposes (BSD 3-Clause, redistributable):

- `repo/PackIndex.h` / `PackIndex.m` — Arq 5/6 `.index` parser. Defines the
  `pack_index` C struct with `magic_number`, `nbo_version`, `nbo_fanout[256]`,
  and an array of `index_object { nbo_offset; nbo_datalength; sha1[20]; filler[4]; }`.
- `repo/PackIndexGenerator.m` — actually *generates* an index (proves there is no
  legal/cleanroom obstacle to writing one).
- `repo/PackSet.h`, `PackSetDB.m`, `PIELoader.m` — repo abstraction for sets of packs.
- `arq7restore/Arq7BlobLoc.h` / `.m` — Arq 7 `BlobLoc` parser, which is the
  authoritative reference for the binary layout described in the spec.

There is no public Arq-shipped *writer* code. `arqc` is a launcher/IPC client that
talks to the proprietary `Arq.app` engine; it is not a library.

---

## 4. Reverse-engineered / third-party format documentation

Limited. The spec is good enough that almost no one has bothered to publish a
deeper reverse-engineering write-up:

- Michael Tsai blog, "Advantages of the Arq 6 File Format" (May 2020),
  `https://mjtsai.com/blog/2020/05/06/advantages-of-the-arq-6-file-format/` —
  brief commentary; explains the move from "many index files plus list of unpacked
  blobs" (Arq 5) to "snapshot directly carries path + offset + length of every
  tree/blob it needs" (Arq 6+). Not new format detail beyond the spec. (Page is
  403 to our fetcher; cited via search snippet.)
- Archive Team file format wiki, `http://fileformats.archiveteam.org/wiki/ARQ` —
  pointer page; no independent RE.
- Gist [wags/583ab6ed33caef60ae48297e5100a08e](https://gist.github.com/wags/583ab6ed33caef60ae48297e5100a08e)
  — verbatim copy of `arq_data_format.txt`, no original analysis.
- The `nlopes/arq` Rust crate's source (e.g. `src/object_encryption.rs`,
  `src/tree.rs`) is a working reader and effectively *is* the reverse-engineering
  notes for anyone who wants concrete byte parsing — but it stops at reading.

No public blog post, gist, or paper documents Arq's chunker parameters
(`chunkerVersion: 3`, `useBuzhash`).

---

## 5. What backup creation actually needs

| Piece | Documented? | Risk to creating |
|---|---|---|
| `encryptedkeyset.dat` (PBKDF2-SHA256 200k, 3×64-byte keys) | Yes, fully (Arq 7 spec) | Low. Validator already does this in reverse. |
| `EncryptedObject` / `ARQO` envelope | Yes, fully (HMAC, IVs, session key, AES-CBC PKCS7) | Low. Validator already verifies this; encrypting is symmetric. |
| LZ4 wrapping (4-byte big-endian length + block) | Yes | Low. |
| `Node` binary | Yes, complete | Low. Tedious but mechanical. |
| `Tree` binary (`version`, `count`, `[String name][Node]*`) | Yes | Low. |
| `BlobLoc` binary | Yes | Low. |
| `backuprecord` file | Yes (property-list-style dict; sample given) | Medium. The exact serializer (Apple binary plist vs ASCII plist? Arq's example uses ASCII-plist syntax; the spec doesn't say which.) needs verification by reading what `arq_restore` accepts. |
| `backupconfig.json`, `backupfolders.json`, `backupfolder.json`, `backupplan.json` | Yes (sample JSON) | Low. Pure JSON. |
| `blobIdentifier` (SHA-256 with stretching) | Yes — the "blob identifier salt" is the third 64-byte key in the keyset; spec says blob ID = hash of (salt ‖ data) | Low. Identical scheme to validator's check. |
| **`.pack` file container in `treepacks/`, `blobpacks/`, `largeblobpacks/`** | **Partially** — Arq 5 `.pack`/`.index` format documented in arq5_data_format.txt (PACK magic, version 2, count, per-object mimetype/name/data, trailing SHA1; index has fanout + objects + trailing SHA1). Arq 7 spec just says "stored in a pack file." `arq_restore` source (`repo/PackIndex.m`, `repo/PackIndexGenerator.m`) is the de-facto reference. | **Medium.** Probably the same Arq 5 format reused; needs empirical confirmation against a real backup. |
| **Chunker (`chunkerVersion: 3`, `useBuzhash`)** | **No.** Window size, hash mask, target chunk size, polynomial — none published. | **High** if we want byte-identical dedup with Arq.app. **Avoidable** if we accept a degraded mode (one chunk per file, or a fixed-size chunker), at the cost of dedup quality. Restore from such a backup will still work because every file Node just lists its `dataBlobLocs` in order; Arq.app does not re-derive chunks at restore time. |
| Pack vs unpacked-object decision (`maxPackedItemLength`, `standardobjects/`) | Partially — JSON config has `maxPackedItemLength: 256000` and the spec lists the directories | Low-medium. Simplest creator: store every blob as a standalone `EncryptedObject` under `standardobjects/<2-char-prefix>/<blobid>` and set `isPacked: false` in every `BlobLoc`. Avoids the pack container question entirely. The spec explicitly allows this ("stored either in a pack file ... or as a standalone file in the 'standardobjects' ..."). |
| Index files (`.index`) | Documented for Arq 5; not described for Arq 7 | Low if we go all-unpacked-objects (no index needed). Arq 7's design intentionally moves location info into Trees/Nodes (`BlobLoc` carries `relativePath`, `offset`, `length`), so the validator already does not require pack-side indexes; restoring from unpacked objects means `isPacked=false`, `offset=0`, `length=encrypted-object-length`. |
| `backuprecords/<bucket>/<seconds>.backuprecord` directory sharding | Yes (sample shows `00161/4294169.backuprecord` = first 5 digits of seconds-since-epoch as bucket, remaining as filename) | Low. |

The "anything Arq.app does at backup time that isn't published" list, concretely:

1. The chunker. Confirmed undocumented.
2. The exact pack-write/finalize sequence (when does Arq seal a pack and write
   its index? what's the temporary-file naming? does Arq.app re-read its own
   pack output to build the index, or build it inline?). Not in the spec.
3. The dedup heuristic: which blobs are checked against existing data, scope
   of dedup (per-folder, per-plan, per-bucket). Spec hints via
   `backupfolders.json["standardObjectDirs"]` etc., but the algorithm isn't
   specified.
4. APFS snapshotting (`useAPFSSnapshots`), VSS on Windows, etc. — not part
   of the on-disk format, but part of "what Arq.app does"; irrelevant for an
   operator-side TUI that backs up an arbitrary tree.

---

## 6. Compatibility verdicts

### a. Could we generate a backup our own validator would accept?

**Yes, with high confidence.** The validator and the writer would both be
implementing the same published spec end of the contract (HMAC over
`master IV ‖ encrypted-IV+session-key ‖ ciphertext`, PBKDF2 from
`encryptedkeyset.dat`, blob ID = `SHA-256(salt ‖ plaintext)`). If the validator
verifies a backup, then a writer that produces those same bytes by construction
will pass. Pack containers can be sidestepped by writing only standalone
objects under `standardobjects/`, which the spec explicitly permits.

### b. Could we generate a backup that **Arq.app** restores cleanly?

**Probably yes for the simple subset, but with non-trivial RE work and
empirical testing.** Specifically:

- Cryptography, `Tree`/`Node`/`BlobLoc`, JSON config, `backuprecord`: all from
  the spec, deterministic, low risk.
- Pack files: medium risk. We can avoid this entirely by emitting every blob as
  a standalone unpacked object with `isPacked: false`. The Arq 7 spec lists
  `standardobjects/` as a peer of `blobpacks/` and the `BlobLoc.isPacked` flag
  exists exactly for this case. Arq.app should follow the `BlobLoc` regardless
  of where the bytes live. ("Should" — needs empirical verification with real
  Arq.app on a Mac.)
- Chunker: medium risk. Setting `useBuzhash: 0` and using one chunk per file
  (i.e. `dataBlobLocs` of length 1, blob = whole file) sidesteps the chunker
  entirely. Concatenation order is what restore uses (per the `Node`
  spec: "ordered list of 'chunks' to ... assemble the file"), so a single
  blob that *is* the file is the trivial valid case. We get poor dedup, but
  correctness should hold. For files larger than `maxPackedItemLength` (default
  256 KB), we likely want to split — and that's where we're guessing chunker
  parameters. A safe interim choice: fixed-size 1 MB chunks with no rolling hash.
  Dedup will be poor on modified-in-place files, but byte-identical files still
  dedup via the SHA-256 blob ID.
- `backuprecord` plist serialization: medium risk. The sample in the spec uses
  ASCII property list (`{ key = value; ... }`), not JSON or binary plist. We'd
  need to round-trip an existing real `backuprecord` through our parser to make
  sure formatting (whitespace, key ordering, escaping) is acceptable to
  `arq_restore` and `Arq.app`.

### c. High-risk unknowns

1. **Chunker parameters** for `chunkerVersion: 3` / `useBuzhash`. Mitigation:
   don't try to match. Use single-blob-per-file or fixed-size chunks. Mark
   backups produced by the TUI with a distinguishing `arqVersion` string and
   a fresh `backupPlanUUID` so they don't get accidentally extended by Arq.app.
2. **Arq 7 `.pack` container layout.** Mitigation: emit standalone unpacked
   objects only.
3. **`backuprecord` plist exact serialization.** Mitigation: empirical
   round-trip testing against `arq_restore` (BSD-licensed, runnable in CI on a
   macOS runner).
4. **`Tree` / `Node` field semantics that are accepted but undocumented in
   edge cases** — e.g., `mac_st_*` on a Linux source, `win_attrs` from Linux,
   `aclBlobLoc` formatting. Mitigation: zero them out and confirm restore
   works.
5. **No reference test vectors.** There is no published "given password X and
   plaintext Y, the encryptedkeyset.dat / ARQO bytes are Z." We rely entirely
   on the validator + an end-to-end round-trip test against `arq_restore` for
   correctness.

---

## Concrete next steps for a backup-creation feature

Tractable now (no RE):

1. Implement `encryptedkeyset.dat` writer (PBKDF2-SHA256 → 64-byte key →
   AES-256-CBC PKCS7 encrypt 3×64-byte keyset → HMAC-SHA256). Validator
   already does the inverse; share a `crypto.py` symmetric layer.
2. Implement `EncryptedObject` writer (mirror of validator's reader).
3. Implement `BlobLoc`, `Node`, `Tree` binary serializers from the spec.
4. Emit JSON config files (`backupconfig.json`, `backupfolders.json`,
   `backupfolder.json`, `backupplan.json`) as JSON literals matching the
   sample shapes.
5. Backup strategy v0: walk the source tree; for each file, encrypt+wrap as
   a single `EncryptedObject`, write under
   `standardobjects/<2-hex>/<blobid>`, build `Node` with one `BlobLoc`
   pointing at that file with `isPacked: false`. Build `Tree`s bottom-up.
   Serialize the root `backuprecord` and write into
   `backupfolders/<UUID>/backuprecords/<5-digit-bucket>/<rest>.backuprecord`.
6. Validate the result with our existing validator. (This is the verdict-A
   milestone.)

Requires verification (RE-light, mostly empirical):

7. `backuprecord` exact plist syntax — round-trip a real Arq.app-produced
   `backuprecord` and diff.
8. End-to-end: install `arq_restore` (BSD, builds on macOS), point it at our
   emitted bucket, restore, compare bytes. (Verdict-B milestone — the cheapest
   way to confirm Arq.app compatibility, since `arq_restore` is the official
   reference reader and shares format code with the proprietary engine.)

Blockers (don't attempt without dedicated RE):

9. Byte-identical dedup with native Arq.app backups — requires reverse-
   engineering the chunker. Not needed for a standalone TUI-produced backup
   set. Don't try to interleave-write into a backup set Arq.app is also
   writing to.
10. Writing into `treepacks/`/`blobpacks/` `.pack` files in the Arq 7 layout —
    avoidable via standalone-objects-only mode. If later needed, the
    Arq 5 pack format in `arq5_data_format.txt` plus `arq_restore`'s
    `repo/PackIndex.m` and `PackIndexGenerator.m` (BSD 3-Clause source we can
    legally read and base a clean implementation on) are the references.

## Sources

- Arq 7 spec (mirror): https://github.com/arqbackup/arq_restore/blob/master/arq7_data_format.html
- Arq 5 spec (mirror): https://github.com/arqbackup/arq_restore/blob/master/arq5_data_format.txt
- Arq 7 spec (canonical): https://www.arqbackup.com/documentation/arq7/English.lproj/dataFormat.html
- Arq 5 spec (canonical): https://www.arqbackup.com/arq_data_format.txt
- arq_restore: https://github.com/arqbackup/arq_restore
- arq_restore README: https://github.com/arqbackup/arq_restore/blob/master/README.markdown
- arqc CLI docs: https://www.arqbackup.com/documentation/arq7/English.lproj/arqc.html
- arqcloudrestore: https://github.com/arqbackup/arqcloudrestore
- arqinator: https://github.com/asimihsan/arqinator
- nlopes/arq: https://github.com/nlopes/arq
- nlopes/evu: https://github.com/nlopes/evu
- stevekstevek/arqfastrestore: https://github.com/stevekstevek/arqfastrestore
- sholiday/arq: https://github.com/sholiday/arq
- tcsc/larq (only repo mentioning a writer; never built one): https://github.com/tcsc/larq
- Michael Tsai on Arq 6 format: https://mjtsai.com/blog/2020/05/06/advantages-of-the-arq-6-file-format/
- ArchiveTeam ARQ wiki: http://fileformats.archiveteam.org/wiki/ARQ
- Arq Cloud Backup format: https://www.arqbackup.com/docs/arqcloudbackup/English.lproj/dataFormat.html
- arq_restore PackIndex source: https://github.com/arqbackup/arq_restore/blob/master/repo/PackIndex.m
- arq_restore PackIndexGenerator source: https://github.com/arqbackup/arq_restore/blob/master/repo/PackIndexGenerator.m
- arq_restore Arq7BlobLoc source: https://github.com/arqbackup/arq_restore/blob/master/arq7restore/Arq7BlobLoc.m
