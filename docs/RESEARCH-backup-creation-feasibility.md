# Arq Backup Creation Feasibility — Research Notes

## Executive summary

Building an Arq-7-format backup *writer* is realistic for a constrained
operator-side tool but meaningfully harder than the validator we already have.
The cryptographic envelope (`encryptedkeyset.dat`, `EncryptedObject` / `ARQO`,
HMAC-SHA256, PBKDF2-SHA256), the binary `Tree`/`Node`/`BlobLoc` format, and the
JSON config files are all fully published. The two genuinely under-documented
pieces are (a) the on-disk container layout of Arq 7 `treepacks/` and
`blobpacks/` `.pack` files (the Arq 5 `.pack`/`.index` format *is* documented;
Arq 7's is not), and (b) the chunker — `chunkerVersion: 3` plus a `useBuzhash`
flag, with no published parameters. **No public open-source project writes Arq
backups today.** All third-party tools are read/restore only. A
"standalone-objects-only" creator that skips the chunker by emitting one blob
per file is plausible without any reverse engineering; full Arq.app round-trip
is plausible but carries real RE risk.

---

## 1. Official Arq documentation

The canonical spec ships inside the `arq_restore` repo (BSD 3-Clause):

- Arq 7: [`arq7_data_format.html`](https://github.com/arqbackup/arq_restore/blob/master/arq7_data_format.html) (mirror of [the canonical](https://www.arqbackup.com/documentation/arq7/English.lproj/dataFormat.html)).
- Arq 5: [`arq5_data_format.txt`](https://github.com/arqbackup/arq_restore/blob/master/arq5_data_format.txt) (mirror of [the canonical](https://www.arqbackup.com/arq_data_format.txt)).
- Arq Cloud Backup is a separate product with its own [format spec](https://www.arqbackup.com/docs/arqcloudbackup/English.lproj/dataFormat.html).

**Fully documented in Arq 7 spec:** the four JSON config files
(`backupconfig.json`, `backupfolders.json`, `backupfolder.json`, `backupplan.json`);
the `backuprecord` file (LZ4 + optional `ARQO`-wrapped property-list dict); the
`Node` binary layout (every field — isTree, treeBlobLoc, dataBlobLocs,
xattrsBlobLocs, mtime/ctime ns, mac_st_*, win_attrs, etc.); the `Tree` binary
(`UInt32 version`, `UInt64 count`, `[String name][Node]*`); `BlobLoc`
(blobIdentifier, isPacked, relativePath, offset, length, stretchEncryptionKey,
compressionType); the `EncryptedObject`/`ARQO` envelope (HMAC-SHA256 over
master-IV ‖ encrypted-IV+session-key ‖ ciphertext, AES-256-CBC + PKCS7);
`encryptedkeyset.dat` (PBKDF2-SHA256 200k rounds, three 64-byte master keys —
encryption, HMAC, blob-identifier salt); LZ4 wrapping (4-byte big-endian length
+ LZ4 block); and the `[Bool][String][UInt32][Int32][UInt64][Int64]` pseudo-types.

**Not documented in Arq 7 spec:**

- The byte layout of `.pack` files in `treepacks/` / `blobpacks/`. The spec just
  says "stored ... in a 'pack' file." The Arq 5 spec, by contrast, fully
  documents Arq 5's `.pack` (PACK magic, per-object mimetype/name/data, trailing
  SHA1) and `.index` (`ff 74 4f 63` magic, 256-entry fanout). `arq_restore`'s
  BSD-licensed `repo/PackIndex.m`, `repo/PackIndexGenerator.m`, and
  `arq7restore/Arq7BlobLoc.m` are the de-facto references for the Arq 7 case.
- The chunker. `backupconfig.json` carries `"chunkerVersion": 3`; the plan
  carries `"useBuzhash": 0|1`. Window size, mask, target chunk size, and
  polynomial are all unpublished — the largest undocumented piece for
  byte-identical dedup with Arq.app.
- Any third-party backup-creation API. The only Arq-shipped CLI,
  [`arqc`](https://www.arqbackup.com/documentation/arq7/English.lproj/arqc.html),
  is a control utility (start/stop plans, set password, get status) for the
  proprietary `Arq.app` engine — not a writer library.

Arq's marketing advertises an "open, published data format," meaning open
*to read*, not an SDK.

---

## 2. Public Arq-related projects on GitHub

| Project | URL | Last update | Scope | Lang | License |
|---|---|---|---|---|---|
| arqbackup/arq_restore | https://github.com/arqbackup/arq_restore | 2026-04 | Restore Arq 5/6/7. Canonical reference; ships the spec. | C / Obj-C | BSD 3-Clause |
| arqbackup/arqcloudrestore | https://github.com/arqbackup/arqcloudrestore | active | Restore from Arq Cloud Backup (different product). | Obj-C | BSD-style |
| asimihsan/arqinator | https://github.com/asimihsan/arqinator | 2026-03 | Restore only. Tested on Arq 4.14.5, S3/GCS/SFTP. | Go | Apache-2.0 |
| nlopes/arq | https://github.com/nlopes/arq | 2026-03 | Read-only library. README: "allows reading files but never writing, so it's not possible to build a full replacement of Arq with this library." | Rust | MIT |
| nlopes/evu | https://github.com/nlopes/evu | 2025-07 | Restore CLI on `nlopes/arq`. | Rust | MIT |
| stevekstevek/arqfastrestore | https://github.com/stevekstevek/arqfastrestore | 2026-04 | Restore Arq 5 + Arq 7, parallel/sort-by-pack. Newest serious effort. | Rust | repo |
| sholiday/arq | https://github.com/sholiday/arq | 2023-07 | Read/explore/restore library. Stale. | Go | repo |
| tcsc/larq | https://github.com/tcsc/larq | 2021-04 | Self-described "Cleanroom reader (and **eventually writer**)." Only repo we found that even mentions writing. 0 stars, ~5 years dormant; never reached writer. | Rust | repo |
| larcho/arq_restore, a5an0/arq_restore | (forks) | varies | Forks of the official restore. | Obj-C | BSD |

What did **not** turn up:

- No PyPI / npm / crates.io package that creates Arq backups. (PyPI `arq` is
  unrelated — it's an async job queue.)
- GitHub topic `arq-backup` returns zero repositories.
- Code search for `"ARQO" "encryptedkeyset"` returns 7 hits, all readers,
  validators, or spec mirrors — no writer.

---

## 3. Arq's publicly shipped code

One production tool: `arqbackup/arq_restore` (BSD 3-Clause, Haystack Software,
2009-2026). Restore-only, Arq 5/6/7, S3 or local. Most relevant BSD-licensed
source for our purposes: [`repo/PackIndex.m`](https://github.com/arqbackup/arq_restore/blob/master/repo/PackIndex.m)
(defines `pack_index { magic_number; nbo_version; nbo_fanout[256]; index_object[]; }`),
[`repo/PackIndexGenerator.m`](https://github.com/arqbackup/arq_restore/blob/master/repo/PackIndexGenerator.m)
(actually *generates* an index — proves no legal/cleanroom obstacle to writing),
and [`arq7restore/Arq7BlobLoc.m`](https://github.com/arqbackup/arq_restore/blob/master/arq7restore/Arq7BlobLoc.m)
(authoritative Arq 7 BlobLoc parser). No Arq-shipped writer code; `arqc` is an
IPC client, not a library.

## 4. Reverse-engineered format documentation

Limited — the official spec is good enough that nobody has published deeper RE:

- Michael Tsai, [Advantages of the Arq 6 File Format](https://mjtsai.com/blog/2020/05/06/advantages-of-the-arq-6-file-format/) (May 2020) — Arq 5 → Arq 6 moved from "many index files" to "snapshot directly carries path+offset+length of every tree/blob." No new format detail.
- [ArchiveTeam ARQ wiki](http://fileformats.archiveteam.org/wiki/ARQ) — pointer page only.
- [Gist wags/583ab6e...](https://gist.github.com/wags/583ab6ed33caef60ae48297e5100a08e) — verbatim copy of the spec.
- `nlopes/arq` Rust source is effectively working RE notes for byte parsing — but reader-only.

No public document covers the chunker parameters.

---

## 5. What backup creation actually needs

| Piece | Documented? | Risk |
|---|---|---|
| `encryptedkeyset.dat` (PBKDF2-SHA256 200k → 64B key → AES-256-CBC + HMAC over 3×64B keys) | Yes, fully | Low — symmetric inverse of validator. |
| `EncryptedObject` / `ARQO` envelope | Yes, fully | Low. |
| LZ4 wrapping | Yes | Low. |
| `Node` / `Tree` / `BlobLoc` binaries | Yes | Low; mechanical. |
| `backuprecord` file | Sample given but format is property-list-style (ASCII plist syntax in spec example). Exact serializer (binary plist? ASCII plist?) not stated. | **Medium** — round-trip a real backuprecord through `arq_restore` to verify. |
| `blobIdentifier` = SHA-256(salt ‖ data), salt = third 64B keyset entry | Yes | Low. |
| JSON config files | Yes (literal samples) | Low. |
| **Arq 7 `.pack` containers in `treepacks/`/`blobpacks/`** | **No.** Arq 5 `.pack`/`.index` documented; Arq 7 only implicitly via `arq_restore` source. | **Medium**, fully **avoidable**: spec lets you store every blob as a standalone unpacked object under `standardobjects/` with `BlobLoc.isPacked = false`. |
| **Chunker (`chunkerVersion: 3`, `useBuzhash`)** | **No.** No window/mask/target size published. | **High** for byte-identical dedup with Arq.app; **avoidable** by using one-blob-per-file or fixed-size chunks. Restore reassembles from `dataBlobLocs` in order — Arq.app does not re-derive chunk boundaries at restore. |
| `.index` files for packs | Documented for Arq 5, not Arq 7 | Not needed if going all-unpacked-objects. Arq 7 puts location info into `BlobLoc` (path/offset/length) inside each `Node`, so packs+indexes are an optimization, not a requirement. |
| `backuprecords/<5-digit-bucket>/<rest>.backuprecord` sharding | Yes (sample shown) | Low. |

The "anything Arq.app does at backup time that isn't published," concretely:

1. **The chunker** (confirmed undocumented).
2. **Pack write/finalize sequence**: when does Arq.app seal a pack and emit its
   index? Not specified.
3. **Dedup heuristic / scope** (per-folder, per-plan, per-bucket). Hinted at via
   `backupfolders.json["standardObjectDirs"]`, not specified.
4. APFS snapshots, VSS, etc. — outside on-disk format; irrelevant for an
   operator-side TUI.

---

## 6. Compatibility verdicts

**a. Could we generate a backup our validator accepts?** **Yes, high confidence.**
Both ends implement the same published spec. A writer that produces standalone
unpacked `EncryptedObject`s under `standardobjects/` plus `Node`/`Tree`/
`BlobLoc` per spec will pass the validator by construction.

**b. Could we generate a backup that Arq.app restores cleanly?** **Probably yes
for a constrained subset, with non-trivial empirical work.** Specifically:

- Crypto + Tree/Node/BlobLoc + JSON: deterministic from spec, low risk.
- Pack files: avoidable. Set `isPacked: false` and write blobs as standalone
  files under `standardobjects/<2-hex>/<blobid>`. The Arq 7 spec explicitly
  allows this; Arq.app should follow `BlobLoc` regardless of where bytes live.
  Needs empirical verification with real Arq.app.
- Chunker: avoidable. With `dataBlobLocs` of length 1 and the blob = the whole
  file, restore is a straight concatenation of one blob. For files >
  `maxPackedItemLength` (default 256 KB), interim choice: fixed-size 1 MB chunks
  with no rolling hash. Dedup of byte-identical files still works via
  SHA-256 blob ID; modified-in-place files dedup poorly. Acceptable tradeoff.
- `backuprecord` plist syntax: medium risk. Need to round-trip a real
  Arq.app-produced `backuprecord` through our parser/serializer to confirm
  formatting (whitespace, key order, escaping) is acceptable.

**c. High-risk unknowns:**

1. Chunker parameters (avoid: don't try to match; produce TUI-marked plans only).
2. Arq 7 `.pack` container layout (avoid: emit unpacked objects only).
3. `backuprecord` exact plist serialization (mitigate: empirical round-trip vs `arq_restore`).
4. Edge cases in `Node` fields cross-platform (`mac_st_*` from Linux, `aclBlobLoc`).
5. No published test vectors. Validator + end-to-end round-trip against
   `arq_restore` is the only correctness oracle.

---

## Concrete next steps

**Tractable now (no RE):**

1. `encryptedkeyset.dat` writer (mirror of validator's reader).
2. `EncryptedObject` writer.
3. `BlobLoc` / `Node` / `Tree` binary serializers from spec.
4. JSON config emitters.
5. v0 backup strategy: walk source tree, encrypt each file as a single blob
   under `standardobjects/<2-hex>/<blobid>`, build `Node`s with one `BlobLoc`
   each (`isPacked: false`), build `Tree`s bottom-up, write the
   `backuprecord` under `backupfolders/<UUID>/backuprecords/<bucket>/<rest>.backuprecord`.
6. Validate output with our existing validator (verdict-A milestone).

**Empirical verification (RE-light):**

7. Round-trip a real Arq.app `backuprecord` through our codec; diff.
8. Build `arq_restore` (BSD, macOS) and restore from our v0 output; byte-compare.
   This is the cheapest verdict-B check, since `arq_restore` is the official
   reference reader.

**Blockers — do not attempt without dedicated RE:**

9. Byte-identical dedup with native Arq.app backup sets (requires chunker RE).
   Don't interleave-write into a backup set Arq.app is also using.
10. Writing Arq 7 `.pack` containers. Avoidable via standalone-objects mode.
    If later required, the Arq 5 pack format in `arq5_data_format.txt` plus
    `arq_restore`'s `PackIndex.m` / `PackIndexGenerator.m` are legal,
    BSD-licensed references for a clean implementation.

---

## Sources

- Arq 7 spec mirror: https://github.com/arqbackup/arq_restore/blob/master/arq7_data_format.html
- Arq 5 spec mirror: https://github.com/arqbackup/arq_restore/blob/master/arq5_data_format.txt
- Arq 7 spec canonical: https://www.arqbackup.com/documentation/arq7/English.lproj/dataFormat.html
- Arq 5 spec canonical: https://www.arqbackup.com/arq_data_format.txt
- arq_restore: https://github.com/arqbackup/arq_restore
- arq_restore README: https://github.com/arqbackup/arq_restore/blob/master/README.markdown
- arq_restore PackIndex source: https://github.com/arqbackup/arq_restore/blob/master/repo/PackIndex.m
- arq_restore PackIndexGenerator source: https://github.com/arqbackup/arq_restore/blob/master/repo/PackIndexGenerator.m
- arq_restore Arq7BlobLoc source: https://github.com/arqbackup/arq_restore/blob/master/arq7restore/Arq7BlobLoc.m
- arqc CLI docs: https://www.arqbackup.com/documentation/arq7/English.lproj/arqc.html
- arqcloudrestore: https://github.com/arqbackup/arqcloudrestore
- Arq Cloud Backup format: https://www.arqbackup.com/docs/arqcloudbackup/English.lproj/dataFormat.html
- arqinator: https://github.com/asimihsan/arqinator
- nlopes/arq: https://github.com/nlopes/arq
- nlopes/evu: https://github.com/nlopes/evu
- stevekstevek/arqfastrestore: https://github.com/stevekstevek/arqfastrestore
- sholiday/arq: https://github.com/sholiday/arq
- tcsc/larq (only repo mentioning a writer; never built one): https://github.com/tcsc/larq
- Michael Tsai on Arq 6 format: https://mjtsai.com/blog/2020/05/06/advantages-of-the-arq-6-file-format/
- ArchiveTeam ARQ wiki: http://fileformats.archiveteam.org/wiki/ARQ
- Gist (verbatim spec copy): https://gist.github.com/wags/583ab6ed33caef60ae48297e5100a08e
