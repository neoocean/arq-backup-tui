# How it works: backup creation / validation / restore

This document traces step by step **what** the current `arq-backup-tui`
codebase does and **how**. Every paragraph references real
modules / functions / lines so you can immediately open the source code
that corresponds to each step.

Three core flows:

1. **Backup creation**: `arq_writer.Backup.add_folder` (or the
   one-shot `arq_writer.build_backup`)
2. **Validation**: `arq_validator.validate` (4 tiers) + `audit_drip` +
   `arq_validator.check_arq7_compatibility` (format conformance)
3. **Restore**: `arq_reader.Restore.restore`

Each flow's inputs / byproducts / outputs / call graph is described in
turn.

---

## 0. Common concepts

We start by laying out the core concepts and modules that all three
flows rely on.

### 0.1 Directory layout (Arq 7)

```
<dest_root>/
└── <COMPUTER-UUID>/                        # 8-4-4-4-12 hex (uppercase)
    ├── encryptedkeyset.dat                 # Master keys (encrypted)
    ├── backupconfig.json                   # Per-computer settings
    ├── backupplan.json                     # Backup plan snapshot
    ├── backupfolders.json                  # Folder index
    ├── standardobjects/<2-hex>/<62-hex>    # Single-blob storage (optional)
    ├── treepacks/<2-hex>/<UUID>.pack       # Tree blob bundles (optional)
    ├── blobpacks/<2-hex>/<UUID>.pack       # Data blob bundles (optional)
    ├── largeblobpacks/<2-hex>/<UUID>.pack  # Large blob bundles (read-only)
    └── backupfolders/<FOLDER-UUID>/
        ├── backupfolder.json
        └── backuprecords/<5-digit bucket>/<num>.backuprecord
```

`bucket` = `floor(creation_date / 100000)` (zero-padded to 5 digits),
`num` = `creation_date % 100000`. Combined, the two values form the
chronological record order.

### 0.2 Core byte-level formats

#### EncryptedObject (`ARQO`) — outer envelope of every blob

```
0..4    "ARQO" (magic)
4..36   HMAC-SHA256(hmac_key, body[36:end])
36..52  master_iv (16B)
52..116 AES-256-CBC( encryption_key, master_iv,
                     data_iv (16B) ‖ session_key (32B) )
                                                   = 64B (PKCS7-padded)
116..end AES-256-CBC( session_key, data_iv, plaintext )
```

`encryption_key` / `hmac_key` are the master keys from the keyset.
`session_key` and `data_iv` are freshly generated per blob.

#### LZ4 wrap

Before going inside the ARQO body, plaintext is wrapped in the LZ4 block
format:

```
0..4    big-endian uint32 = decompressed_length
4..end  LZ4 block (raw, no frame)
```

On decompress, the length is used to pre-allocate the output buffer; the
LZ4 block is decoded and its length verified.

#### encryptedkeyset.dat — encrypts the master keys themselves

```
0..25   "ARQ_ENCRYPTED_MASTER_KEYS"  (25B literal, no NUL pad)
25..33  PBKDF2 salt (8B)
33..65  HMAC-SHA256( derived[32:64], iv ‖ ciphertext ) (32B)
65..81  AES-256-CBC IV (16B)
81..end AES-256-CBC( derived[0:32], iv,  plaintext )
```

Where `derived` = `PBKDF2-HMAC-SHA256(password, salt,
iterations=200_000, dklen=64)`.

Plaintext layout (binary):

```
[uint32 BE: version=3]
[uint64 BE: 32] [encryption_key (32B)]
[uint64 BE: 32] [hmac_key       (32B)]
[uint64 BE: 32] [blob_id_salt   (32B)]
```

### 0.3 blob_id (content addressing)

```
blob_id = SHA-256( blob_id_salt ‖ plaintext ).hexdigest()  # 64-char lowercase hex
```

`plaintext` here is the data before LZ4 wrap — i.e. raw file chunks,
serialized Trees, or backuprecord plists. Identical content yields an
identical blob_id, so dedup happens naturally.

### 0.4 Backend abstraction

`arq_validator.backend.Backend` Protocol — 6 read methods and 2 write
methods:

```python
list_dir / stat_size / read_range / read_all / exists / is_dir
mkdir / write_all
```

Implementations are `LocalBackend` (local filesystem) or `SftpBackend`
(OpenSSH master + SFTP). All writer / reader / validator I/O flows
through the backend.

---

## 1. Backup creation flow

CLI entry: `arq-backup` (`arq_writer.cli`) or the TUI's
`BackupRunScreen`. Programmatic entry: a single call to
`arq_writer.build_backup(...)` or direct use of the
`arq_writer.Backup` class.

### 1.1 `Backup.__init__` — keys / backend / cache initialization

In the `Backup` class constructor (`arq_writer/backup.py`):

1. **Backend selection**:
   - If the `backend` argument is None, create `dest_root` and wrap it as
     `LocalBackend(dest_root)`
   - If the argument is provided, use it as-is (typically SftpBackend)

2. **Master key selection** (`dedup_against_existing` branch):
   - `dedup_against_existing=False` or first-time backup: generate
     `encryption_key` / `hmac_key` / `blob_id_salt` randomly as 32 bytes
     each (`secrets.token_bytes`)
   - `dedup_against_existing=True` + all key arguments None: call
     `_try_load_existing_keyset(dest_root, computer_uuid, password)` →
     decrypt the destination's existing encryptedkeyset.dat and reuse
     the existing keys. This is required so that the SHA-256 blob_ids
     of new blobs match those of the previous run, allowing dedup cache
     hits.

3. **Chunker initialization** (when `chunker_config` is provided):
   - Create a `Buzhash(config)` instance. Holds the T table + window
     size + boundary mask.
   - Importing `arq_writer.arq_chunker_params` automatically registers
     the Arq.app v7.41 measured parameters (256-byte window, 16-bit
     mask, 128 KiB max, reverse-engineered T table); fetch via
     `chunker_for_arq(3, True)`.

4. **Per-run accumulator initialization**:
   - `_written_blobs: Dict[blob_id, BlobLoc]` — in-run dedup cache
   - Counters such as `files_written` / `files_reused` /
     `bytes_plaintext`
   - `_blob_pack` / `_tree_pack` — lazily-created PackBuilders in
     packing mode

### 1.2 `init_plan()` — write metadata files + seed dedup

`Backup.init_plan` (`arq_writer/backup.py`):

1. **Create directories**:
   - `<cu>/standardobjects/`
   - `<cu>/backupfolders/`
   - All via backend.mkdir (LocalBackend or SftpBackend actually runs
     mkdir / `mkdir -p`)

2. **Write encryptedkeyset.dat** (only when `_keyset_was_reused` is False):
   - Call `crypto_write.build_encrypted_keyset(password, encryption_key,
     hmac_key, blob_id_salt)`
   - Internal steps (`crypto_write.py`):
     a. Build plaintext: version=3 + 3 × (uint64 length + 32B key)
     b. Generate 8B salt (`secrets.token_bytes(8)`)
     c. Generate 16B IV
     d. derived = `hashlib.pbkdf2_hmac("sha256", password, salt,
        200_000, dklen=64)`
     e. ciphertext = `aes_256_cbc_encrypt(derived[:32], iv,
        plaintext)` (calls the host `openssl` CLI)
     f. mac = `HMAC-SHA256(derived[32:], iv ‖ ciphertext)`
     g. Final: `KEYSET_MAGIC ‖ salt ‖ mac ‖ iv ‖ ciphertext`
   - Write to disk via backend.write_all

3. **Write the 3 JSON sidecars**:
   - `backupconfig.json`: chunkerVersion=3, blobIdentifierType=2
     (SHA-256), maxPackedItemLength=256000, isEncrypted=True, etc.
   - `backupplan.json`: planUUID, version=2, scheduleJSON,
     transferRateJSON, emailReportJSON, backupFolderPlansByUUID
     (currently an empty dict; updated when add_folder is called)
   - `backupfolders.json`: 1 standardObjectDirs + 4 other storage class
     arrays as empty arrays

4. **Seed dedup** (when `dedup_against_existing=True`):
   - Call `arq_writer.dedup.seed_existing_destination(...)`
   - Two seedings:
     a. `seed_from_standardobjects`: walk the entirety of
        `standardobjects/<shard>/<file>` via backend.list_dir. If a
        filename matches the 62-hex pattern, derive
        `blob_id = shard + filename` and add a
        `BlobLoc(isPacked=False, relativePath=..., length=stat_size)` to
        the cache.
     b. `find_latest_backuprecord_per_folder`: collect the latest
        backuprecord path of each folder. For each one, call
        `seed_from_backuprecord(rec_path, cache, encryption_key,
        hmac_key, dest_root, backend)`:
        - read_all the backuprecord ARQO + decrypt + LZ4-unwrap +
          plistlib.loads
        - extract the root Node dict → if isTree=True, run
          `_harvest_tree_recursive` to fetch + decrypt + parse_tree
          every child Tree blob and gather all BlobLocs
        - In packed mode, BlobLocs that point at
          `<cu>/blobpacks/<shard>/<UUID>.pack` also enter the cache →
          on the next run, those blobs will not be created again

### 1.3 `add_folder(source)` — back up one folder

`Backup.add_folder` (`arq_writer/backup.py`):

1. **Create folder directories**:
   - `<cu>/backupfolders/<folder_uuid>/`
   - Write `backupfolder.json` (localPath, name, uuid, etc.)

2. **Update backupplan.json**:
   - Append a new entry to `_folder_plans` (the result of
     build_folder_plan)
   - Call `_write_plan_json()` → rewrite backupplan.json

3. **Build PriorTreeIndex** (`dedup_against_existing` + reused keyset):
   - `arq_writer.prior_tree.PriorTreeIndex(dest_root,
     computer_uuid, encryption_key, hmac_key, folder_uuid, backend)`
   - Find the most recent backuprecord and obtain the root Node's
     treeBlobLoc. Subsequently, when `lookup_file(rel_path)` is called,
     lazily fetch + parse the tree blobs along the path and return the
     prior FileNode. Tree blobs are cached by blob_id.

4. **Source tree walk** (`_walk` → `_walk_dir` / `_walk_file`):

   At every directory boundary in the recursion, `_check_cancel()`
   inspects the cooperative cancel flag (may raise `BackupCancelled`).

   **`_walk_file(src, rel_path)`**:
   - **PriorTreeIndex hit check**: look up rel_path in the prior tree;
     if the discovered FileNode's `(mtime_sec, mtime_nsec, itemSize,
     mac_st_mode & 0o7777)` matches the current src's stat, reuse
     dataBlobLocs as-is → skip `read_bytes` / chunker / hash → emit
     `files_reused += 1` + `file_reused` callback.
   - **Otherwise (changed file or no prior)**:
     - `data = src.read_bytes()`
     - Chunker enabled: call `Buzhash(config).chunk(data)`
       - Internal algorithm (`arq_writer/chunker.py`):
         a. If data is ≤ `min_chunk_size`, yield it as a single chunk
         b. Otherwise, advance by `min_chunk_size`, then compute the
            initial Buzhash over a `window_size`-byte window
         c. Slide one byte at a time and update
            `H = ROL(H_old, 1) ⊕ ROL(T[byte_out], n) ⊕ T[byte_in]`
         d. If `H & boundary_mask == 0`, cleave at a chunk boundary →
            yield, then start the next chunk
         e. If `max_chunk_size` is reached without finding a boundary,
            forcibly cleave
     - Chunker disabled: the entire content as one chunk
     - For each chunk, `_write_blob(piece)` → returned BlobLoc → append
       to `dataBlobLocs`
   - **Build FileNode**: dataBlobLocs + (mtime/ctime/mode/uid/
     gid/nlink/itemSize/...)
   - `files_written += 1` + `file_written` callback

   **`_walk_dir(src, rel_path)`**:
   - Sort children and recurse into `_walk(child)` for each
   - Gather the children's `(name, Node)` tuples and build
     `Tree(children=..., version=3)`
   - `serialize.write_tree(tree, version=3)` for binary serialization
     (network byte order, [String] 8-byte length prefix, etc. — exactly
     spec convention)
   - `_write_blob(tree_bytes, is_tree=True)` → write the tree blob →
     using the returned BlobLoc, build `TreeNode(treeBlobLoc=...,
     itemSize=..., containedFilesCount=..., directory's
     mtime/ctime/mode/...)`

   **`_write_blob(plaintext, is_tree=False)`**:
   - Compute `blob_id = compute_blob_id(blob_id_salt, plaintext)`
   - If `_written_blobs.get(blob_id)` hits, return the cached BlobLoc
     immediately (dedup with no delay)
   - On miss:
     a. `lz4_wrap(plaintext)`: 4B BE original_length + LZ4 block
     b. `build_encrypted_object(lz4_bytes, encryption_key, hmac_key)`:
        - Generate 32B random session_key, 16B random data_iv, 16B
          random master_iv
        - encrypted_session = AES-256-CBC(encryption_key, master_iv,
          data_iv ‖ session_key)
        - ciphertext = AES-256-CBC(session_key, data_iv, lz4_bytes)
        - body = master_iv ‖ encrypted_session ‖ ciphertext
        - mac = HMAC-SHA256(hmac_key, body)
        - return `b"ARQO" + mac + body`
     c. **Standard-object mode** (`use_packs=False`):
        - path = `/<cu>/standardobjects/<blob_id[:2]>/<blob_id[2:]>`
        - backend.mkdir(parent) → backend.write_all(path, arqo)
        - BlobLoc(isPacked=False, relativePath=path, offset=0,
          length=len(arqo))
     d. **Packing mode** (`use_packs=True`):
        - For tree blobs `_tree_pack` (lazily created), for data blobs
          `_blob_pack`
        - PackBuilder.add(blob_id, arqo):
          - On first call, allocate a new pack path (UUID-based)
          - Append the ARQO bytes verbatim to the in-memory buffer
          - When the buffer exceeds `max_pack_bytes` (default 10 MiB),
            flush via backend.write_all + start the next pack
          - BlobLoc(isPacked=True,
                   relativePath=current_pack_path,
                   offset=offset_in_buffer, length=len(arqo))
   - `_written_blobs[blob_id] = loc` + counter updates

5. **flush_packs()**: write all in-flight pack buffers to disk. Ensures
   the BlobLoc offsets that go into the backuprecord point to the actual
   on-disk locations.

6. **Write backuprecord**:
   - bucket = `f"{int(time):05d/100000:05d}"`, rec_num = `int(time) %
     100000`
   - mkdir directory `<cu>/backupfolders/<fu>/backuprecords/<bucket>/`
   - `build_backuprecord_dict(...)` builds the dict:
     - node = root TreeNode converted to dict (treeBlobLoc also as a
       dict)
     - creationDate, arqVersion, computerOSType, backupFolderUUID,
       backupPlanUUID, backupPlanJSON (current plan snapshot),
       version=100, isComplete=True, etc.
   - `serialize_backuprecord(record_dict)`: binary plist serialization
     via `plistlib.dumps(..., fmt=plistlib.FMT_BINARY)`
   - `build_backuprecord_arqo(plist_bytes, ...)`: LZ4-wrap + ARQO
     envelope
   - backend.write_all writes to `<bucket>/<rec_num>.backuprecord`

7. **Successful return**: `rec_path` (Path or backend-relative). However,
   if `BackupCancelled` is raised mid-walk:
   - `flush_packs` is not called → in-memory pack buffers are lost (any
     packs already flushed to disk are a strict subset of valid blobs,
     so this is harmless)
   - The backuprecord is not written → the destination's prior state is
     preserved (consistent)

### 1.4 Call graph summary

```
build_backup
└── Backup.__init__              (key selection + backend setup)
└── Backup.init_plan
    ├── backend.mkdir × N
    ├── build_encrypted_keyset   (PBKDF2 + AES-CBC + HMAC)
    ├── backend.write_all        (encryptedkeyset.dat)
    ├── build_backupconfig + write_all
    ├── build_backupfolders_json + write_all
    ├── build_backupplan + write_all
    └── seed_existing_destination (optional)
        ├── seed_from_standardobjects
        └── for each folder:
            └── seed_from_backuprecord
                └── _harvest_tree_recursive (recursive tree walk)
└── Backup.add_folder
    ├── backend.mkdir (folder dir)
    ├── build_backupfolder_json + write_all
    ├── build_folder_plan + _write_plan_json
    ├── PriorTreeIndex(...)      (optional)
    └── _walk(source, "")
        └── _walk_dir / _walk_file (recursive)
            ├── prior_tree.stat_matches → reuse
            ├── chunker.chunk → chunks
            └── _write_blob × N
                ├── compute_blob_id
                ├── lz4_wrap
                ├── build_encrypted_object
                └── backend.write_all OR PackBuilder.add
    ├── flush_packs
    ├── build_backuprecord_dict + arqo
    └── backend.write_all (backuprecord)
```

### 1.5 Which files end up on disk

The simplest backup (`source/a.txt`, standalone mode):

```
<dest>/<CU>/encryptedkeyset.dat
<dest>/<CU>/backupconfig.json
<dest>/<CU>/backupplan.json
<dest>/<CU>/backupfolders.json
<dest>/<CU>/backupfolders/<FU>/backupfolder.json
<dest>/<CU>/standardobjects/<a.txt blob_id[:2]>/<...>     # a.txt body
<dest>/<CU>/standardobjects/<root tree blob_id[:2]>/<...> # root tree
<dest>/<CU>/backupfolders/<FU>/backuprecords/<bucket>/<num>.backuprecord
```

In packing mode the two standardobjects/ lines disappear and instead:

```
<dest>/<CU>/blobpacks/<2-hex>/<UUID>.pack          # a.txt + other blobs
<dest>/<CU>/treepacks/<2-hex>/<UUID>.pack          # trees
```

---

## 2. Validation flow

Validation is offered in two flavors:

- **`arq_validator.validate(backend, root, *, tier=...)`** — 4-tier
  layered validation (L0/L1a/L1b/L2). Library + CLI (`arq-validator`).
- **`arq_validator.check_arq7_compatibility(backend, root, *,
  encryption_password=...)`** — single function for format conformance.
  Inspects all 25 spec invariants.

Neither function raises on format/hash failures. Findings are gathered
into the result object (`ValidationReport` / `ComplianceReport`) and
returned.

### 2.1 Tier hierarchy (`arq_validator/tiers.py`)

Each higher tier subsumes all lower tiers. Costs are cumulative.

#### 2.1.1 L0 (DRY_RUN) — layout shape

`run_l0(backend, root, callback=None)`:

1. backend.list_dir(root) → directories matching the 8-4-4-4-12 hex UUID
   pattern are recognized as computer UUIDs (`COMPUTER_UUID_RE`)
2. For each computer, `discover_layout` collects:
   - whether the keyset file exists
   - the 4 object family directories (standardobjects/treepacks/
     blobpacks/largeblobpacks) and their internal shard counts
   - the number of record files inside each
     backupfolders/<FU>/backuprecords/
3. Result: `LayoutResult(layout_ok=bool, computers=[...])`

This stage performs no decryption. A lightweight validation impacted
only by backend round-trip latency.

#### 2.1.2 L1a (QUICK) — ARQO magic sample

`run_l1a(backend, layouts, root, sample_fraction=0.05)`:

1. Sample (default 5%) from each object family
2. For each sample, `backend.read_range(path, 0, 4)` → read the first 4
   bytes
3. Compare against `ARQO_MAGIC` (b"ARQO")
4. Record the count + paths of files that don't match

Use case: cheaply detect bit-rot / partial transfers / 0-byte files.

#### 2.1.3 L1b (DEEP) — keyset + latest backuprecord HMAC

`run_l1b(backend, layouts, root, encryption_password, openssl_path)`:

1. For each computer, decrypt the keyset:
   - `parse_keyset_storage(blob)`:
     - Verify the first 25 bytes are `ARQ_ENCRYPTED_MASTER_KEYS`
     - 25..33 = salt, 33..65 = stored_mac, 65..81 = iv, 81..end = ct
   - `derived = pbkdf2_hmac_sha256(password, salt, 200_000,
     dklen=64)`
   - `aes_key = derived[:32]`, `mac_key = derived[32:]`
   - Compare `actual_mac = HMAC-SHA256(mac_key, iv ‖ ct)` → on
     mismatch, classify as "wrong password OR file corruption"
   - `plaintext = AES-256-CBC-decrypt(aes_key, iv, ct)` (calls host
     `openssl enc -d`)
   - `parse_keyset_plaintext`: validate the version=3 + 3 × (uint64
     length + 32-byte field) format and return (encryption_key,
     hmac_key, blob_id_salt)
2. For each folder's latest backuprecord:
   - `find_latest_backuprecord(backend, root, cu, fu)` to determine the
     path
   - Read the ARQO body
   - `verify_arqo_hmac(arqo, hmac_key)`: HMAC verify magic + length +
     body[36:]
3. Result: `BackupRecordResult(total, ok, fail, failures=[...])`

Uses only the password and the most recent record. Deeper than L1a but
still fast (does not HMAC over many blobs).

#### 2.1.4 L2 (AUDIT) — full HMAC sweep

`run_l2(backend, layouts, keyset, root, audit_skip_larger_than=...,
audit_max_runtime_sec=..., audit_max_bytes=...)`:

1. Walk every file in each object family
2. For each file, `read_all` → ARQO HMAC verification
3. For pack files, additionally walk along the BlobLoc index and HMAC
   each slice (in practice this is currently one HMAC over the whole
   pack file rather than per-ARQO slices)
4. Options:
   - `audit_skip_larger_than`: skip very large blobs
   - `audit_max_runtime_sec` / `audit_max_bytes`: partial audit (early
     exit)
5. Result: `ObjectAuditResult(files_total, files_ok, files_fail,
   bytes_ok, ...)`

Takes a long time on large destinations, so the `audit_drip` pattern is
provided separately.

#### 2.1.5 audit-drip — resumable L2

`arq_validator.run_audit_drip(backend, target, state_file,
encryption_password, max_runtime_sec, rate_files_per_min, ...)`:

1. Load the cursor from `state_file` (where the previous fire stopped)
2. Resume the walk from the cursor
3. Each fire proceeds within the `max_runtime_sec` or `audit_max_bytes`
   limit → if it ends before completion, save the cursor + counters to
   the state file
4. Throttle: if `rate_files_per_min` is given, sleep is inserted between
   file processings

**Use case**: split nightly audits into short windows on environments
with limited read throughput such as NAS / cloud storage boxes. Even on
interruption, the next round resumes exactly from the cursor with no
loss.

### 2.2 Format conformance verification (`arq_validator.compatibility`)

`check_arq7_compatibility(backend, root, *, encryption_password,
computer_uuid=None)`:

Inspects 25 spec invariants in one go. Each invariant is reported as a
`CheckResult` object with a stable id (L1~L8 / C1~C4 / A1~A2 / B1~B3 /
P1~P2 / S1 / ID1~ID2 / SV1~SV3). The detailed invariant table is in
`docs/COMPATIBILITY.md`.

Flow:

1. **L1**: confirm one or more top-level computer UUID directories exist
2. **L2 + C1~C4**: keyset file exists / magic / layout / decrypt + HMAC
   / plaintext shape
3. **L3 + SV1~SV2**: backupconfig.json all required keys + types +
   chunkerVersion ∈ {1, 2, 3} + blobIdentifierType ∈ {1, 2}
4. **L4**: backupplan.json all required keys (planUUID,
   backupFolderPlansByUUID, scheduleJSON, etc.) + required keys of
   each folder plan entry
5. **L5**: backupfolders.json 5 storage class arrays
6. **L6 + L7 + L8**: folder directories + backupfolder.json + record
   path shape
7. **B1~B3 + SV3**: each backuprecord's ARQO + plist parsing + 9
   required keys + node shape + version ∈ {100, 200}
8. **A1~A2 + S1 + ID1~ID2**: extract up to 32 samples from
   standardobjects/ → ARQO + HMAC + filename regex + verify
   `blob_id == SHA-256(salt ‖ plaintext)`
9. **P1~P2**: if pack files exist, UUID-format names + ARQO magic at
   offset 0

Each step is wrapped in try/except so that a single invariant's failure
does not block subsequent invariants. All findings are gathered into a
`ComplianceReport` and returned → the caller handles them via
`report.passed` / `report.failed_checks`.

---

## 3. Restore flow

CLI entry: `arq-reader` (`arq_reader.cli`) or the TUI's
`RestoreRunScreen`. Programmatic entry: `arq_reader.Restore`.

### 3.1 `Restore.__init__` — backend selection

`arq_reader/restore.py`:

```python
Restore(src, encryption_password, *, backend=None, openssl_path=...)
```

- `backend=None`: create `LocalBackend(Path(src).resolve())`
- `backend` provided: use as-is. `src` is a backend-namespace path
  (typically a path on the SFTP server, or just `"/"`)

The password is held in memory only for the lifetime of the class
instance (never written to disk).

### 3.2 Layout discovery

`Restore.layouts()`:

```python
self._layouts = discover_layout(self.backend, "/")
```

Walks computer UUID directories the same as L0. Each
`Arq7ComputerLayout` exposes `computer_uuid`, `backup_folder_uuids`,
the existence of object family directories, etc. Computed lazily once
and cached.

### 3.3 `Restore.restore(...)` — restore one folder

Entry point:

```python
restore(*, folder_uuid, dest, computer_uuid=None,
        backuprecord_path=None, paths=None, callback=None)
```

1. **Computer selection**: if `computer_uuid` is None, search the
   layouts for a single computer using
   `_resolve_single_computer(folder_uuid)` (raises `ValueError` if
   ambiguous / missing)

2. **Load keyset**: `self.keyset(computer_uuid)`:
   - Check the cache (`_keyset_by_computer`)
   - On cache miss: read encryptedkeyset.dat and run
     `decrypt_keyset(blob, password)` (same procedure as L1b) → store
     in cache
   - Return `Keyset(encryption_key, hmac_key, blob_id_salt)`

3. **backuprecord selection**:
   - If `backuprecord_path` is provided, use it (point-in-time restore)
   - Otherwise `find_latest_backuprecord(backend, "/", cu, fu)` →
     chronologically latest record path per folder

4. **Build path filter** (when the `paths` argument is not None):
   - `_build_path_filter(paths)` creates a `_PathFilter`
   - Each path is stored with stripped slashes
   - `matches(rel_path)`: exact match or prefix match (for directory
     marking)
   - `descend(rel_path)`: whether matching descendants might exist in
     this directory → lets the walk skip irrelevant subtrees

5. **Decrypt backuprecord**:
   - `self.backend.read_all(record_path)` → ARQO bytes
   - `decrypt_lz4_arqo(arqo, encryption_key, hmac_key,
     openssl_path=...)`:
     a. Inside `decrypt_encrypted_object(arqo, ...)`:
        - Verify magic 4 bytes + HMAC 32 bytes + body
        - body[0:16] = master_iv
        - body[16:80] = encrypted_session (64B)
        - body[80:] = ciphertext
        - `data_iv ‖ session_key = AES-256-CBC-decrypt(encryption_key,
          master_iv, encrypted_session)`
        - `plaintext = AES-256-CBC-decrypt(session_key, data_iv,
          ciphertext)`
     b. `lz4_unwrap(plaintext)`: 4B BE length + LZ4 block decompress
   - `plistlib.loads(plist_bytes)` → record dict
   - extract `record["node"]`

6. **Root node handling branch**:
   - **Root is TreeNode** (`isTree=True`):
     recurse into `_restore_dir_node(tree_blob_loc, out_dir, keyset,
     result, callback, rel_path="", path_filter, check_cancel)`.
   - **Root is FileNode** (rare but possible):
     call `_restore_file_node(file_node, out_dir, ...)`.

### 3.4 `_restore_dir_node` — directory restore

In each call:

1. Use `path_filter.descend(rel_path)` to check whether any matches
   could exist in this subtree. If not, return immediately (an
   important optimization — even the tree blob fetch itself is
   skipped).
2. `out_dir.mkdir(parents=True, exist_ok=True)` (actually create the
   local filesystem directory)
3. `tree_bytes = self._fetch_blob(tree_blob_loc, keyset)`:
   - `loc.isPacked=True`: `backend.read_range(loc.relativePath,
     loc.offset, loc.length)` slices only the ARQO inside the pack
   - `loc.isPacked=False`: `backend.read_all(loc.relativePath)`
   - If the first 4 bytes are `ARQO`, run `decrypt_encrypted_object`
     (HMAC verify + decrypt)
   - `loc.compressionType == 2`: `lz4_unwrap`
   - `loc.compressionType == 1`: stdlib `gzip.decompress` (Arq 5
     legacy)
   - `loc.compressionType == 0`: as-is
4. `parse_tree(tree_bytes)`: spec-convention binary parser → returns
   `Tree(version, children)`
5. For each child:
   - `child_rel = f"{rel_path}/{child.name}"` (just child.name if
     rel_path is empty)
   - **TreeNode**: recurse into `_restore_dir_node(child.node.treeBlobLoc,
     out_dir / child.name, ..., child_rel, path_filter)`
   - **FileNode**:
     - skip if `path_filter.matches(child_rel)` is False
     - `_restore_file_node(child.node, out_dir / child.name, ...)`

### 3.5 `_restore_file_node` — single file restore

1. `out_path.parent.mkdir(parents=True, exist_ok=True)`
2. Fetch all dataBlobLocs in order (call `_fetch_blob`):
   - each blob → ARQO verify + decrypt + (LZ4/gzip) decompress
3. `chunks = [bytes_per_blob, ...]`
4. `out_path.write_bytes(b"".join(chunks))` — concatenate chunks to
   reassemble the original
5. `os.utime(out_path, (mtime, mtime))` — restore mtime (failure is
   handled non-fatally)
6. `result.files_restored += 1` + `file_restored` callback

**Currently unimplemented** (`docs/COVERAGE.md` ⚠️ / ❌):
- Symbolic links: the FileNode mode bits are preserved, but
  `os.symlink` is not actually called → restored as a regular file
- xattr / ACL application: preserved on the Node but `setxattr` /
  `setfacl` are not actually invoked
- Hard links: restored as separate files (Arq.app does the same)
- Ownership (`mac_st_uid` / `mac_st_gid`): preserved in metadata, not
  applied at restore time

### 3.6 Call graph summary

```
Restore.restore
├── _resolve_single_computer / explicit argument
├── self.keyset(computer_uuid)
│   └── decrypt_keyset (PBKDF2 + AES-CBC + HMAC)
├── find_latest_backuprecord OR use backuprecord_path
├── _build_path_filter (optional)
├── backend.read_all (record)
├── decrypt_lz4_arqo
│   ├── decrypt_encrypted_object
│   │   ├── HMAC-SHA256 verify
│   │   ├── AES-256-CBC-decrypt × 2 (session + body)
│   ├── lz4_unwrap
├── plistlib.loads → record dict
├── _restore_dir_node (root tree)
│   ├── path_filter.descend (early exit)
│   ├── out_dir.mkdir
│   ├── _fetch_blob (tree blob)
│   │   ├── backend.read_range / read_all
│   │   ├── decrypt_encrypted_object
│   │   └── lz4_unwrap
│   ├── parse_tree
│   └── for each child:
│       └── _restore_dir_node (recursive) / _restore_file_node
└── _restore_file_node
    ├── for each dataBlobLoc: _fetch_blob (decrypt + decompress)
    ├── concat chunks → out_path.write_bytes
    └── os.utime
```

### 3.7 What happens differently in other modes

**SFTP destination**:
- All `backend.read_all` / `read_range` calls translate to `head -c N` /
  `dd skip=K count=N` or `sftp get` over the SSH master
- All blob fetches multiplex over a single master SSH session → no new
  TCP/SSH handshake cost per blob
- Partial reads (`read_range`) on pack files use the same master

**packed-mode destination**:
- Each BlobLoc is a `(pack_path, offset, length)` triple
- `read_range` fetches only the exact slice of the pack file → does not
  download the entire pack
- Even when one pack contains chunks from multiple files, BlobLoc.offset
  points exactly to that chunk's location

**path-filtered restore**:
- Specify partial paths like `paths=["문서/이력서.txt", "사진"]`
- Byte-for-byte UTF-8 comparison → non-ASCII path names also match
  as-is
- `descend` skips the tree-blob fetch of irrelevant subtrees entirely
  → partial restores of large backups are efficient

**historical record restore**:
- Use `Restore.list_records(folder_uuid)` to query record history
- Pass each `RecordInfo`'s `relative_path` to `restore(...,
  backuprecord_path=...)` → restore a point-in-time snapshot

---

## 4. One-line summary

| Flow | Input | Core steps | Output |
|------|------|----------|------|
| **Backup** | Source tree + password + destination | walk → chunk → ARQO envelope → backend.write_all | `<dest>/<CU>/...` directory tree + backuprecord |
| **Validation** | destination + (optional) password | per-tier checks (layout / magic / HMAC / full audit) OR 25 format invariants | `ValidationReport` or `ComplianceReport` |
| **Restore** | destination + password + target directory | decrypt keyset → decrypt backuprecord → tree walk → fetch + decrypt + LZ4-unwrap each blob → concat → write file | reassembled file tree on the local filesystem |

Common denominators of the three flows:

- **byte-level format**: ARQO envelope + LZ4 wrap + binary plist /
  binary Tree + JSON sidecar
- **content addressing**: SHA-256(salt ‖ plaintext) 64-char hex
- **encryption**: AES-256-CBC + HMAC-SHA256 + PBKDF2-SHA256
- **storage**: everything works on top of the backend Protocol's 6+2
  methods → Local / NAS / SFTP all share the same code path

---

## Appendix A. Quick reference for the main modules

| Module | What it does |
|------|--------------|
| `arq_writer.backup` | Backup orchestrator (Backup class + build_backup) |
| `arq_writer.crypto_write` | Builds encryptedkeyset.dat, builds ARQO, computes blob_id, `rotate_keyset_password` |
| `arq_writer.serialize` | binary Tree / Node / BlobLoc serialization |
| `arq_writer.json_configs` | backupconfig / backupplan / backupfolder JSON |
| `arq_writer.backuprecord` | binary plist backuprecord builder |
| `arq_writer.lz4_block` | 4B-prefix LZ4 wrap / unwrap |
| `arq_writer.chunker` | Buzhash content-defined chunker |
| `arq_writer.arq_chunker_params` | RE'd Arq.app v7.41 chunker parameters |
| `arq_writer.pack_builder` | treepacks/blobpacks/largeblobpacks builder |
| `arq_writer.dedup` | cross-run dedup seed helpers |
| `arq_writer.prior_tree_index` | PriorTreeIndex for tree-walk reuse |
| `arq_writer.exclusions` | `ExclusionRules` (glob + regex + .gitignore-subset) |
| `arq_writer.macos_snapshot` | macOS APFS snapshot context manager (`with_apfs_snapshot`) |
| `arq_writer.retention` | `RetentionPolicy` + `prune_records` + `gc_orphan_blobs` + `apply_retention` |
| `arq_validator.crypto` | keyset decrypt + HMAC verify + ARQO decrypt |
| `arq_validator.tiers` | L0 / L1a / L1b / L2 implementations |
| `arq_validator.runner` | 4-tier orchestrator (`validate(...)`) |
| `arq_validator.audit_drip` | resumable L2 sweep |
| `arq_validator.compatibility` | 25 spec invariants checker |
| `arq_validator.layout` | computer/folder discovery + record path |
| `arq_validator.backend` | Backend Protocol + LocalBackend |
| `arq_validator.sftp` | SftpBackend (SSH master + sftp put/rename) |
| `arq_reader.restore` | Restore class + restore walk |
| `arq_reader.decrypt` | ARQO decrypt helper (inverse of write side) |
| `arq_reader.parse` | binary Tree / Node / BlobLoc parser |

---

## Appendix B. Where and how errors arise

| Symptom | Possible cause | In which function it is caught |
|------|-------------|----------------------|
| `DecryptError: HMAC mismatch` | wrong password OR keyset tampering | `decrypt_keyset` (verify step) |
| `DecryptError` (record) | hmac_key mismatch OR blob tampering | `verify_arqo_hmac` |
| `lz4 unwrap failed` | compressed data corruption OR length-prefix tampering | `lz4_unwrap` |
| `parse_tree: bad version` | tree blob tampering / version mismatch | `parse_tree` |
| `BackupCancelled` | mid-walk after `Backup.cancel()` was called | `_walk` (`_check_cancel`) |
| `ValueError: folder UUID not found` | invalid folder_uuid | `Restore._resolve_single_computer` |
| `RuntimeError: ssh master ... not ready` | SFTP connection failure | `SftpBackend.__enter__` |

All three flows handle errors best-effort: a single corrupted blob does
not block the entire backup / audit / restore — findings are gathered
into a `failures` list and left for the user's judgment.

---

## Appendix C. Maintenance flow (PR #11–#12)

The above § 1–3 cover the three flows of writing, validation, and
restore. The behavior of the two **maintenance tasks** added in PR #11 /
#12 is as follows.

### C.1 Password rotation — `rotate_keyset_password`

- Input: raw bytes of the existing `encryptedkeyset.dat` + old/new
  passwords
- Procedure:
  1. `decrypt_keyset(blob, old_password)` → extract `(encryption_key,
     hmac_key, blob_id_salt)`
  2. Generate a new 8-byte salt + IV
  3. Re-encrypt with the same master keys + new salt/IV via
     `build_encrypted_keyset(new_password, encryption_key, hmac_key,
     blob_id_salt)`
- Result: master keys unchanged → all existing backuprecords / blobs
  remain decryptable. Only the new keyset bytes need to be rewritten to
  the destination (`backend.write_all(...)`).

### C.2 Retention / pruning / blob GC — `apply_retention`

- Input: backend + password + `RetentionPolicy` (keep_last_n + 5 time
  buckets)
- Step 1 `prune_records()`:
  - Enumerate every
    `<CU>/backupfolders/<folder>/backuprecords/...backuprecord`
  - `select_retained()` decides the retained set per policy (time
    buckets are OR-combined)
  - Records outside the retained set are deleted via
    `backend.unlink(path)`
- Step 2 `gc_orphan_blobs()` (optional):
  - Walk the tree of every retained record → collect the set of
    referenced standalone blob IDs + the set of referenced pack paths
  - Delete blobs under `<CU>/standardobjects/<2hex>/<60hex>` whose IDs
    are not in the referenced set
  - Delete only packs under
    `<CU>/treepacks/`/`blobpacks/`/`largeblobpacks/` whose paths are
    not in the referenced pack set
    (conservative — even if part of a pack is orphan, that pack is
    kept)
- Callback events: `record_deleted` / `blob_deleted` / `pack_deleted`
  (emitted identically in dry-run mode)

The TUI's `MaintenanceScreen` (`arq_tui/screens/maintenance.py`) calls
both in sibling threads, and marshals results back to the main loop via
`call_from_thread`.
