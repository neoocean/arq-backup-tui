# Compatibility Items Discovered and Improved Through Real-SFTP-data Verification

> **Summary**: A reader / writer / validator that passed every synthetic
> unit test immediately exposed **four incompatibility areas and one
> performance bottleneck** the moment we started verifying against a real
> Arq.app v8 destination (Hetzner Storage Box). Every issue was treated as
> a bidirectional compatibility fix rather than a partial one, securing
> the possibility of future round-trips with Arq.app.

This document records each difference, its impact, and the rationale for
the fix in **Before / After** form. These are the discoveries made from
the moment we connected to a real destination via `.secrets/` credentials;
without these fixes, neither `arq_restore` (the BSD reference
implementation) nor the Arq.app GUI can read our writer's output.

## 0. Verification environment

- Operator's real destination: Hetzner Storage Box (chrooted SFTP-only server)
- Arq.app writer: v8.x (based on the `arqVersion` field in the destination)
- Computer UUIDs: 1, Backup folders: 5
- Sharded directories: standardobjects/treepacks/blobpacks/largeblobpacks, 256 shards each
- Credential channel: `.secrets/sftp.json` (identity_file or password) +
  `.secrets/dest_password` (Arq encryption password)

Test entry points:
- `tests/integration/test_arqapp_sftp_compat.py` — format / shape verification (PR #9)
- `tests/integration/test_arq_real_destination.py` — runtime reader/validator/writer (PR #16)
- `tests/integration/test_arq_real_destination_deep.py` — automatic discovery of format invariants (this work)

## 1. SftpBackend fails entirely on chrooted SFTP-only servers

### Before

Seven methods of `SftpBackend` (`is_dir`, `exists`, `stat_size`, `read_range`,
`mkdir`, `unlink`, the partial cleanup in `write_all`) depended on arbitrary
SSH commands:

```python
def is_dir(self, path):
    cp = self._run_ssh(f"test -d {shlex.quote(path)} && echo Y || echo N")
    return cp.returncode == 0 and cp.stdout.decode().strip() == "Y"
```

Looking only at synthetic (LocalBackend mock) tests, this looked fine, but
on chrooted SFTP-only servers like Hetzner Storage Box **every SSH command
is rejected**:

```
ssh ... -- test -d /home/...
→ rc=8
→ stderr: "Command not found. Use 'help' to get a list of available commands."
```

This single line broke layout discovery, restore, validate, and writer
destination initialization. Synthetic tests only used LocalBackend, so they
never exercised this code path and the bug was never caught.

### After (commit `792e521`)

Rewrote all seven methods using the sftp protocol:

| Method | New implementation |
|---|---|
| `is_dir(path)` | `sftp cd <path>` (rc=0 ⇒ dir, rc=1 ⇒ file/missing) |
| `exists(path)` | `sftp cd <path>` or `sftp ls -l <path>` succeeds |
| `stat_size(path)` | the 5th column of `sftp ls -l <path>` |
| `read_range(path, off, len)` | try ssh `head -c`/`dd` → on rc≠0 fall back to sftp `get` (full download) and slice in memory |
| `mkdir(path, parents)` | create ancestors step by step with sftp `mkdir`, probe existence with `cd` |
| `unlink(path)` | sftp `rm`; "No such file" stderr is silently OK (rm -f semantics) |
| `write_all` partial cleanup | `_run_sftp_batch("rm <partial>\nbye\n")` |

Verification: `test_layout_discovers_computer`, `test_keyset_decrypts`, etc.
all return `OK` against the operator's destination.

## 2. Backuprecord is UTF-8 JSON, not a binary plist

### Before

Following the spec's "binary plist" representation, our writer and reader
used these lines:

```python
# writer
return plistlib.dumps(record, fmt=plistlib.FMT_BINARY)

# reader
record = plistlib.loads(record_plain)
```

The self round-trip unit test passed because both sides used binary plist.

### After (commit `399480a`)

Decoding the first 80 bytes of the operator's record:

```
b'{"backupFolderUUID":"0830DA4E-3EB6-4342-A3F3-33E99E19D005","diskIdentifier":"5F2...
```

Arq.app v8 records the backuprecord as **a single line of UTF-8 JSON**
(no BOM). `plistlib.loads` immediately fails with `InvalidFileException`.

Fix:
- **reader** (`arq_reader/restore.py`): a new `_parse_backuprecord(plain)`
  helper — try plist first, on failure fall back to
  `json.loads(plain.decode("utf-8"))`. Accepting both formats lets us read
  both our (legacy) plist backups and Arq.app JSON backups.
- **writer** (`arq_writer/backuprecord.py`): set
  `serialize_backuprecord(fmt='json')` as the new default. `fmt='binary-plist'`
  is retained for backward compatibility.

## 3. The BlobLoc binary layout was missing the `isLargePack` field

### Before

The field order in our `parse_blobloc` and `write_blobloc`:

```python
# our reader
def parse_blobloc(reader):
    blob_id    = reader.read_string()
    is_packed  = reader.read_bool()       # next field after 7 bytes
    rel_path   = reader.read_string()
    ...
```

Self round-trip passed because both sides agreed.

### After (commit `399480a`)

Confirmed by hex-dumping the first tree blob from the operator's destination:

```
... 31 31 62 | 01 | 00 | 01 00 00 00 00 00 00 00 5a 2f 45 31 42 44 ...
              ^    ^    ^                                    ^
              |    |    isNotNull=1                          rel_path begins "/E1BD..."
              |    is_large_pack=False  ← the byte we missed
              is_packed=True
```

Arq.app's actual BlobLoc layout:
```
blob_id, isPacked, isLargePack, rel_path, offset, length, stretch, compression
                  ^^^^^^^^^^^
                  missing from the spec; discovered in real data
```

Because of this single missing byte, every following field was off by one
byte and the next `read_string` exploded with
`bad [String] isNotNull byte: 45 (= '-')` — a hyphen from a UUID
misinterpreted as the isNotNull byte.

Fix:
- `arq_writer/types.py`: add the `BlobLoc.isLargePack: bool = False` field.
- `arq_reader/parse.py:parse_blobloc`: one more `read_bool()`.
- `arq_writer/serialize.py:write_blobloc`: add `write_bool(loc.isLargePack)`.
- `arq_writer/backuprecord.py:blobloc_to_dict`: also emit `"isLargePack"` in JSON.
- `arq_reader/restore.py:_blobloc_from_dict`: read `isLargePack` from JSON.

Locked in by `test_blobloc_keys_overlap`.

## 4. Node serialization was missing `userName` / `groupName` fields

### Before

Keys emitted by `node_to_dict`:
```python
{
  "isTree", "computerOSType", "containedFilesCount", "itemSize",
  "modificationTime_sec", "modificationTime_nsec",
  "changeTime_sec", "changeTime_nsec",
  "creationTime_sec", "creationTime_nsec",
  "deleted",
  "mac_st_dev", "mac_st_ino", "mac_st_mode", "mac_st_nlink",
  "mac_st_uid", "mac_st_gid", "mac_st_rdev", "mac_st_flags",
  "winAttrs",
  "treeBlobLoc"/"dataBlobLocs", "xattrsBlobLocs",
}
```

### After (commit `399480a`)

Keys of the operator record's `node` dict:
```
{... same as above ..., 'groupName', 'userName', 'reparseTag',
 'reparsePointIsDirectory', ...}
```

`userName`/`groupName` were missing. With only numeric uid/gid, Arq.app's
GUI restore may fail to display ownership, or some code paths in
`arq_restore` may fail.

Fix:
- `arq_writer/backuprecord.py:node_to_dict`: add the two keys (values are
  `node.username or ""`, `node.groupName or ""`).
- `arq_writer/backup.py`: a new `_resolve_owner(uid, gid)` helper — uses
  the POSIX `pwd`/`grp` modules to convert uid → username and gid →
  groupname. On failure (LDAP-only environments, Windows) returns `None`
  → the writer emits an empty string.
- `Backup._walk_file` / `_walk_symlink` call `_resolve_owner` at the two
  FileNode construction points.

Locked in by `test_node_keys_overlap_with_arq_app`.

## 5. SFTP partial-read downloads the entire pack for every packed blob

### Before

On chrooted servers like Hetzner, `ssh ... -- head -c <length>` is
rejected, so the `read_range` fallback uses sftp `get` to **download the
entire file**:

```python
def _read_range_via_sftp_get(self, path, offset, length, *, timeout):
    fd, tmp = tempfile.mkstemp(...)
    self._run_sftp_batch(f"get {path} {tmp}\nbye\n")
    with open(tmp, "rb") as f:
        f.seek(offset)
        return f.read(length)
```

Problem: a single pack file holds dozens of blobs, but every blob's range
read during a restore **re-downloads the same pack file**. The result:
- Pack 50MB × N reads = hundreds of MB of duplicate downloads
- A single restore = 30+ minute hang
- Even simple tests never reach the end

### After (commit `399480a`)

Added a per-session pack file cache:

```python
class SftpBackend:
    def __init__(self, ...):
        self._read_cache: Dict[str, Path] = {}   # new field

    def _read_range_via_sftp_get(self, path, offset, length, *, timeout):
        cached = self._read_cache.get(path)
        if cached is None or not cached.is_file():
            fd, tmp = tempfile.mkstemp(prefix="arq-sftp-cache-")
            os.close(fd)
            cached = Path(tmp)
            self._run_sftp_batch(f"get {path} {cached}\nbye\n", timeout=timeout)
            self._read_cache[path] = cached
        with open(cached, "rb") as f:
            f.seek(offset)
            return f.read(length)

    def _cleanup(self):
        for cached in list(self._read_cache.values()):
            try: cached.unlink()
            except OSError: pass
        self._read_cache.clear()
```

The Nth read of the same pack file becomes a local seek (microseconds).
`__exit__` / `close` clears every cached file — no disk leak.

Verification: writer round-trip 8 minutes (pre-cache 30+ minutes →
post-cache 8 minutes — a 60-70% reduction).

## 6. Wrong assumption in the List_backuprecords bucket formula

### Before (commit `7ded492`)

The previous docstring:
```python
def list_backuprecords(...):
    """...
    The lexicographic ordering on (bucket, num) matches chronological
    ordering because both encode creation_date (bucket = floor(creation_date /
    100000), num = creation_date % 100000).
    """
```

A test against the real destination found `bucket=176, creationDate=1761965143`:
- `floor(1761965143 / 100000) = 17619` ≠ 176
- The observed ratio is closer to **`/ 10_000_000`** (176.1965...)
- `num=2351653` cannot be explained as `creationDate % anything` either —
  it is a separate sequence

### After

The spec's bucket formula is demoted to an Arq.app internal implementation
detail. Callers only depend on **chronological ordering**, so we verify
only that:

- docstring: changed to "Arq.app picks (bucket, num) such that lexicographic
  ordering matches creationDate order". The exact-formula claim is removed.
- New test `test_record_paths_sort_chronologically`: decrypt 5 folders ×
  5 records each, then verify path ordering matches creationDate ordering
  (PASS on the real destination).

## 7. The 38-byte trailing block in the Tree v4 binary format

### Before

Our `parse_node` only knew about Tree v3. Four of the operator's five
folders were recorded as Tree v4, so even walking the root tree of those
folders exploded immediately with errors like
`bad [String] isNotNull byte: 55 at pos=680`.

### After (commit `60496a1` + this PR)

Hex-diff analysis revealed that v4 **adds a 38-byte trailing block at the
end of every Node**. Initial sampling showed all zeros, but a wider walk
(`scripts/probe_tree_v4_block.py`, sampling 30 nodes) **revealed non-zero
patterns**.

#### Estimated structure of the 38-byte block (based on 30 observed nodes)

```
bytes 0..7   int64 BE  scanned-at sec   (≈ backup pass time, distributed
                                         within a ~7-minute window across
                                         nodes — unrelated to the file's
                                         own mtime/ctime/create)
bytes 8..15  int64 BE  scanned-at nsec
bytes 16..23 int64 BE  0x00000000_01000000 (identical across every
                                         non-zero block — presumed a
                                         present-flag or version marker)
bytes 24..37 14 bytes  reserved (all zero)
```

Notable observations:
- 3 of the 30 nodes (all `.DS_Store`) had **all-zero** 38 bytes. These
  three nodes have mtime == ctime == create_sec (same instant) — all share
  the trait of being files newly added in the most recent backup pass.
- The remaining 27 nodes follow the structure pattern above — every
  scanned-at timestamp falls between 2025-07-23 18:33-18:40 UTC (the
  time of that backup pass).

#### Conclusion

The reader simply skips the 38 bytes opaquely — `tests/test_tree_v4_trailing_block.py`
pins that both shapes align exactly with the next child. Determining the
exact field semantics (lastVerifiedAt? scannedAt? reverifiedAt?) is
deferred to follow-up Arq.app Mach-O RE work — for our implementation to
produce binary-perfect backups against Arq.app, the writer would have to
emit the same 38 bytes, but the current writer only emits v3 trees, so
there is no impact today.

Fix:
- (PR #18) `arq_reader/parse.py:parse_node`: `if tree_version >= 4:
  reader.read_raw(38)`
- (PR #18) new `BinaryReader.read_raw(n)` method — consumes N
  uninterpreted bytes
- (this PR) recorded the estimated structure as inline comments in parse_node
- (this PR) `tests/test_tree_v4_trailing_block.py` — 4 tests covering the
  all-zero shape + structured shape + v3 binary-compat regression
- (this PR) `scripts/probe_tree_v4_block.py` — a tool to collect more
  observed samples from the operator's destination
- (PR #25) writer side: `_v4_trailing_block` emits the structured form
  when called with `tree_version=4`
- (PR #41) Mach-O RE against operator's installed Arq.app v8 confirms
  `nodeTreeVersion = 4` is hard-coded in `-[BackupRecord init]`
  (writes `movl $0x4, 0x1c(%rax)`). The Node initializer signatures
  (visible as `initWithDataBlobLocs:…` and `initWithTreeBlobLoc:…`)
  contain NO `scannedAt` or `lastVerifiedAt` parameter — confirming
  the 38-byte trailing block is **serializer-only metadata**, not
  a stored Node attribute the reader reconstructs into a property.
  See `docs/C1-MACHO-RE-PLAN.md` §"Findings (2026-05-10 RE session)"
  for the full transcript.

Verification: all five operator folders (1 v3 + 4 v4) pass `parse_tree`.
Arq.app v8 emits Tree v4 with hard-coded version stamp → our writer's
`tree_version=4` choice is operator-confirmed compatible.
Field-semantics rabbit hole partially closed: the only
remaining unknown is the exact runtime source of the 0x01000000
constant at bytes 16..23 (no `movabsq $0x1000000, …` writer found
in static analysis).

## Impact matrix

| Area | Before | After | Affected code |
|---|---|---|---|
| Hetzner-style SFTP compatibility | ❌ every backend op fails | ✅ works using the sftp protocol alone | `arq_validator/sftp.py` |
| Backuprecord serialization | binary plist (not Arq.app) | JSON default + plist back-compat | `arq_writer/backuprecord.py`, `arq_reader/restore.py` |
| BlobLoc binary layout | followed the spec (differs from reality) | added `isLargePack`, matches Arq.app | `arq_writer/serialize.py`, `arq_reader/parse.py`, dict converters |
| Node serialization | uid/gid only | also userName/groupName | `arq_writer/backup.py`, `arq_writer/backuprecord.py` |
| SFTP partial-read performance | re-downloaded the full pack each time | session cache — local seek | `arq_validator/sftp.py` |
| Bucket formula docs | wrong by a factor of 100 | the formula itself is removed from the contract | `arq_validator/layout.py` |
| Tree v4 binary compatibility | ❌ every v4 folder walk failed | ✅ opaquely skips the 38-byte trailing block | `arq_reader/parse.py` |

## Catalogue of new integration tests

`tests/integration/test_arq_real_destination_deep.py` (new):

| Test class | Test | What it verifies |
|---|---|---|
| `FolderAndHistoryParseTests` | `test_every_folder_has_decryptable_latest_record` | latest record JSON parse for all 5 folders |
| | `test_oldest_record_still_decrypts` | the oldest record still decrypts — keyset rotation integrity |
| | `test_record_paths_sort_chronologically` | path-ordering == creationDate-ordering invariant |
| `TreeBinaryParseTests` | `test_top_level_tree_parses` | binary parse of every folder's root tree blob |
| | `test_nested_trees_parse_cleanly` | 50 nested trees of the first folder (`isLargePack` regression test) |
| `WriterFormatCompatTests` | `test_node_keys_overlap_with_arq_app` | our `node_to_dict` emits every Arq.app node key |
| | `test_record_top_level_keys_overlap` | our `build_backuprecord_dict` emits every Arq.app top-level key |
| | `test_blobloc_keys_overlap` | our `blobloc_to_dict` emits every Arq.app BlobLoc key (isLargePack regression) |

`tests/integration/test_arq_real_destination.py` (PR #16):

| Test | What it verifies |
|---|---|
| `test_restore_latest_record_of_first_folder` | reader restores the operator's real data |
| `test_audit_drip_capped_at_a_few_megabytes` | validator L2 audit-drip on real data |
| `test_round_trip_via_real_sftp` | writer → reader → validator end-to-end (sandbox) |

`tests/integration/test_arqapp_sftp_compat.py` (PR #9, attribute names corrected):

| Test | What it verifies |
|---|---|
| `test_layout_discovers_computer` | discover_layout(enumerate_objects=False) — fast UUID discovery |
| `test_keyset_decrypts` | operator keyset PBKDF2-SHA256 + AES-CBC decryption |
| `test_compatibility_audit_passes` | all 25 invariants pass |
| `test_validator_l0_l1a_l1b_tiers_pass` | DEEP tier passes |
| `test_fingerprint_is_well_formed_json` | shape fingerprint JSON serialization |
| `test_records_list_at_least_one` | folder contains at least 1 record |
| `test_sample_standalone_object_arqo_valid` | standalone object ARQO + HMAC + blob_id verification |

## Key takeaways

1. **Synthetic tests guarantee only self-consistency.** Even when both
   directions (reader+writer) pass, true compatibility can only be verified
   by comparing against an external reference.
2. **Spec documents can differ from reality.** The "binary plist"
   representation in the official Arq 7 spec is actually emitted as JSON.
   `isLargePack` is also missing from the spec.
3. **SFTP-only servers are common.** Beyond Hetzner, most cloud storage
   boxes are chrooted and block SSH commands. The backend must work using
   the sftp protocol alone.
4. **A framework that lets the operator verify against their own
   destination** (`.secrets/` + integration tests) immediately exposes
   compatibility bugs the sandbox cannot find.

## Operator guide

For the procedure operators should follow to run the above tests against
their own destination, see the "0. Credential sources" section of
`docs/COMPAT-SFTP-TESTING.md`. Summary:

```sh
cd /path/to/arq-backup-tui
git checkout claude/secrets-real-destination-tests
cp .secrets/sftp.json.example .secrets/sftp.json
cp .secrets/dest_password.example .secrets/dest_password
chmod 600 .secrets/sftp.json .secrets/dest_password
$EDITOR .secrets/sftp.json .secrets/dest_password

python3 -m unittest tests.integration -v   # entire integration suite
```

If credentials are empty, every integration test auto-skips — no impact
on regular regression runs.
