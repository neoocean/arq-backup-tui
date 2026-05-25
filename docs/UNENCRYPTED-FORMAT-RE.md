# Arq 7 unencrypted (`isEncrypted: false`) on-disk format — RE 2026-05-25

Reverse-engineered from a real unencrypted destination the operator created
via the Arq.app 7.44.1 GUI ("Continue Without Encryption"): plan
`arq_no_encryption_test` → `/Volumes/arqbackup1/AC8FAB4D-…/`. This is the
reference for implementing the writer's `isEncrypted: false` emit and the
reader's end-to-end unencrypted path.

## GUI support
Arq.app's new-backup flow offers **"Continue Without Encryption"** (string
confirmed in the app binary, alongside `setIsEncrypted:` / `_isEncrypted`), so
unencrypted is a first-class, GUI-creatable mode — not just a spec allowance.

## Differences vs the encrypted format

| Element | Encrypted | **Unencrypted** |
|---|---|---|
| `backupconfig.json` `isEncrypted` | `true` | **`false`** |
| `encryptedkeyset.dat` | present | **absent** (no keyset at all) |
| `backupplan.json` / per-folder `backupfolder.json` | ARQO envelope | **plaintext JSON** |
| `backupconfig.json` / `backupfolders.json` | plaintext JSON | plaintext JSON (same) |
| blobs (standardobjects + pack contents) | `ARQO( lz4_wrap(plaintext) )` | **`lz4_wrap(plaintext)`** — no ARQO wrapper |
| backuprecord | `ARQO( serialize(record) )` | **`lz4_wrap( serialize(record) )`** — no ARQO |
| **blob_id** | `SHA-256(blob_id_salt ‖ plaintext)` | **`SHA-256(plaintext)`** — no salt (there is no keyset/salt) |

Where `lz4_wrap(x)` is Arq's standard framing **`[BE32 uncompressed-length][LZ4 block]`** (`arq_writer/lz4_block.py`), and `compressionType` in the
`BlobLoc` is LZ4 (2) just as in the encrypted case. So the *only* structural
change per blob/record is dropping the ARQO envelope; the lz4 framing,
sharding (`<first-2-hex>/<rest>` of the SHA-256 blob_id), pack layout,
`backupconfig`/`backupfolders` schemas, and tree/record JSON shapes are
unchanged.

## Verified (against AC8FAB4D-…)
- `backupconfig.json`: `isEncrypted: false`, `blobIdentifierType: 2` (SHA-256),
  `chunkerVersion: 3`; no `encryptedkeyset.dat`.
- backuprecord `…/00177/9701348.backuprecord`: `[BE32=4180][LZ4]` →
  `lz4` decompress → valid JSON (`archived`, `arqVersion`, `backupPlanJSON`, …).
- standalone blob `standardobjects/48/c63321…2f` (a 40,000,000-byte chunk of
  the 41 MB fixture): stored `[BE32=40000000][LZ4]`; `lz4` decompress →
  40,000,000 bytes; **`SHA-256(decompressed) == blob_id` (`48c63321…`)** — i.e.
  blob_id is the SHA-256 of the *plaintext*, with no salt.

## Implementation implications
- **Writer (`isEncrypted=False`)**: skip the `build_encrypted_object` (ARQO)
  step in `_write_blob` (store `lz4_wrap(plaintext)` directly); blob_id =
  `compute_blob_id(b"", plaintext)` (empty salt → `SHA-256(plaintext)`); emit
  `backupplan.json` / `backupfolder.json` as plaintext JSON; write the
  backuprecord as `lz4_wrap(serialize_backuprecord(...))`; do **not** write
  `encryptedkeyset.dat`; set `isEncrypted: false`; make the password optional.
- **Reader (unencrypted destination)**: when `backupconfig.isEncrypted` is
  false / no keyset present, skip keyset load + the password requirement;
  parse the backuprecord via lz4-unwrap (not ARQO); the blob read path is
  already ARQO-magic-gated and applies the `BlobLoc` `compressionType`, so
  `lz4_wrap` blobs decompress unchanged.
