# Arq 7 on-disk format compatibility

This document maps every Arq 7 format invariant to the test that
locks it in. Run `python -m arq_validator.compatibility` (or use
:func:`arq_validator.check_arq7_compatibility` programmatically)
against any destination to get a structured pass/fail report
covering every entry below.

## ✅ Bidirectional byte-perfect interoperability with Arq 7 — proven (2026-05-24)

Within the scope an individual can exercise — one Mac running the **Arq 7
GUI (Arq.app 7.44.1)** plus the operator's real Arq.app v8 destination —
**byte-perfect interoperability with Arq 7 is proven in both directions:**

| Direction | Claim | Status | Evidence |
|---|---|---|---|
| **A. our writer → Arq 7** | A destination this project's writer emits is read **and restored by Arq.app's own GUI**, with restored content **byte-identical** to the source. | ✅ **Proven** | Strategy I — `docs/COMPAT-VERIFICATION.md` §5.9 (GUI restore, per-file SHA-256 match); V1 same-source fingerprint (file/tree/chunk shape identical) |
| **B. Arq 7 → our reader** | A destination **Arq.app created** is read and restored by this project's reader, **byte-perfect**. | ✅ **Proven** | Strategy B §3.4 (127,222 files / 3.17 GB, `verify.failures: []`); re-confirmed 2026-05-24 by a fresh `--verify-after` restore of real Arq-created data |

**Precision on Direction A.** Our writer's emit matches Arq.app's emit
byte-for-byte at every format-defined layer — JSON sidecars, the
BackupRecord, ARQO envelopes, Tree v4 nodes, content-addressed blob_ids,
and chunk boundaries. The *only* bytes that differ are the v4 trailing
block's engine-internal **scan-timestamp** field (bytes 0–15): a runtime
value Arq stamps per walk, not a file-metadata field, and **not validated
by Arq.app's reader** (confirmed by the Strategy I GUI restore succeeding).
Reproducing it would also defeat our writer's blob-level dedup, so it is
intentionally not reproduced. Every byte that carries restorable meaning
is identical.

This closes the project's headline goal from both sides. The full session
account is in `docs/ARQ7-GUI-INTEROP-2026-05-24.md`; the two production
bugs the GUI surfaced (backuprecord path divisor, default folder==planUUID)
landed in PR #184, and keyset-rotation parity in PR #185.

## Sources

- **Published spec**:
  https://www.arqbackup.com/documentation/arq7/English.lproj/dataFormat.html
- **Reference implementation**: `arq_restore` (BSD), specifically
  `arq7restore/Arq7BlobReader.m::dataForBlobLoc:` for the pack /
  standalone read path.
- **Empirical corrections**: `arq_validator.constants` (25-byte
  unpadded keyset magic, 32-byte key fields). These supersede the
  published spec where they disagree, per cross-checks against
  live Hetzner SFTP destinations.

## Invariant table

| ID  | Invariant                                                                 | Locked by |
|-----|---------------------------------------------------------------------------|-----------|
| L1  | At least one computer-UUID directory at top level (8-4-4-4-12 hex)        | `test_simple_tree_in_standalone_mode_passes_all_invariants` |
| L2  | `<cu>/encryptedkeyset.dat` present                                        | `test_simple_tree_*`, `test_missing_backupconfig_fails_L3` (negative regression) |
| L3  | `<cu>/backupconfig.json` parseable + every required key present + correct types | `test_simple_tree_*`, `test_missing_backupconfig_fails_L3` |
| L4  | `<cu>/backupplan.json` parseable + every required key + folder-plan shape | `test_simple_tree_*`, `test_corrupted_backupplan_field_fails_L4` |
| L5  | `<cu>/backupfolders.json` parseable + 5 storage-class arrays              | `test_simple_tree_*` |
| L6  | At least one folder UUID under `<cu>/backupfolders/`                      | `test_simple_tree_*`, `test_two_folders_in_one_computer_pass_invariants` |
| L7  | Each `<cu>/backupfolders/<fu>/backupfolder.json` parseable + 8 required keys | `test_simple_tree_*` |
| L8  | Each `backuprecords/<bucket>/<num>.backuprecord` filename is digits + suffix | `test_simple_tree_*` |
| C1  | Keyset starts with `ARQ_ENCRYPTED_MASTER_KEYS` (25-byte literal, no NUL pad) | `test_corrupt_keyset_magic_fails_C1` |
| C2  | Keyset layout = 25 magic + 8 salt + 32 HMAC + 16 IV + AES-block-aligned ciphertext | `test_simple_tree_*` |
| C3  | Keyset decrypts under password (PBKDF2-SHA256, 200 000 iterations) + HMAC verifies | `test_simple_tree_*`, `test_wrong_password_fails_C3` |
| C4  | Keyset plaintext = version 3 + three 32-byte fields (encryption_key, hmac_key, blob_id_salt) | `test_simple_tree_*` |
| A1  | Every standalone object starts with `ARQO` magic                          | `test_corrupt_arqo_magic_fails_A1` |
| A2  | ARQO HMAC-SHA256 over body[36:] verifies under keyset HMAC key            | `test_simple_tree_*` |
| B1  | Each backuprecord starts with ARQO magic + decrypts cleanly               | `test_simple_tree_*` |
| B2  | Decrypted backuprecord plist has all required keys (`node`, `creationDate`, `arqVersion`, `computerOSType`, `backupFolderUUID`, `backupPlanUUID`, `backupPlanJSON`, `version`, `isComplete`) | `test_simple_tree_*`, `test_backuprecord_version_is_100` |
| B3  | `node` field is a dict; if `isTree=True`, has a `treeBlobLoc` dict        | `test_simple_tree_*` |
| P1  | Pack files (when present) follow the `[6 hex]-[4 hex]-[4 hex]-[4 hex]-[12 hex].pack` shape | `test_simple_tree_in_packed_mode_passes_all_invariants` |
| P2  | Pack files start with ARQO magic at offset 0                              | `test_simple_tree_in_packed_mode_*` |
| S1  | `standardobjects/<2-hex>/` shard names are 2 lowercase hex chars           | `test_simple_tree_*` |
| ID1 | Standalone object filenames match `^[0-9a-f]{62}$`                        | `test_simple_tree_*` |
| ID2 | For each sampled standalone object, blob_id == SHA-256(blob_id_salt + plaintext) | `test_simple_tree_*` |
| SV1 | `chunkerVersion` ∈ {1, 2, 3}                                              | `test_fixed_values_in_backupconfig_match_spec` |
| SV2 | `blobIdentifierType` ∈ {1=SHA-1, 2=SHA-256}                                | `test_fixed_values_in_backupconfig_match_spec` |
| SV3 | `version` in backuprecord is 100 (current Arq 7) or 200 (forward-compat)  | `test_backuprecord_version_is_100` |

Every check ID is sourced inline in
`arq_validator/compatibility.py` next to the implementation, so a
reviewer can cross-reference without leaving the file.

## Coverage matrix vs. backup scenarios

| Scenario                              | Standalone | Packed | Verdict |
|---------------------------------------|:----------:|:------:|---------|
| Simple tree (3 files, 1 subdir)       | ✅         | ✅     | All invariants pass |
| Empty source tree                     | ✅         | -      | All invariants pass |
| Single root file                      | ✅         | -      | All invariants pass |
| Multi-folder (2 folders, 1 computer)  | -          | ✅     | All invariants pass + both folder UUIDs surface |
| Korean / Japanese / emoji filenames   | ✅         | -      | All invariants pass; UTF-8 round-trips through Tree blobs |
| Large file (3 MiB) with Arq.app v7.41 chunker | -  | ✅     | All invariants pass; multi-chunk file resolves through the recursive tree walk |

## Negative regression coverage

The compatibility checker only earns its keep if it actually
flags malformed destinations. These tests intentionally damage a
correctly-shaped backup and assert the right invariant fires:

| Test                                              | Damage applied                              | Flag fires |
|---------------------------------------------------|----------------------------------------------|------------|
| `test_corrupt_keyset_magic_fails_C1`              | Flip first byte of `encryptedkeyset.dat`     | C1         |
| `test_wrong_password_fails_C3`                    | Pass `"WRONG"` as the encryption password    | C3         |
| `test_corrupt_arqo_magic_fails_A1`                | Overwrite first 4 bytes of a standalone blob | A1         |
| `test_missing_backupconfig_fails_L3`              | Delete `<cu>/backupconfig.json`              | L3         |
| `test_corrupted_backupplan_field_fails_L4`        | Remove `planUUID` from `backupplan.json`     | L4 (field-level) |

If any of these stop flagging the right invariant, the checker
has a hole.

## Known scope omissions (deliberately not enforced)

These are documented gaps the checker accepts without flagging, in
line with the project scope decision in `docs/COVERAGE.md`:

- **`largeblobpacks/` write routing**: writer puts every non-tree
  blob into `blobpacks/` regardless of size; the checker accepts
  `largeblobpacks/` if present but doesn't require its use.
- **Per-folder `useBuzhash` toggle**: writer applies one
  chunker config to a `Backup` instance.
- **Unencrypted backups (`isEncrypted: false`)**: supported
  (2026-05-25) — writer `--no-encrypt` and reader (no password),
  byte-perfect vs Arq.app's "Continue Without Encryption" output.
  See `docs/UNENCRYPTED-FORMAT-RE.md`.
- **Cloud-only metadata fields** (`s3GlacierObjectDirs`,
  `s3DeepArchiveObjectDirs`, `archiveUploadedDate`): emitted as
  empty arrays by the writer; not consulted further.

## Running the checker against your own destination

Programmatic use:

```python
from arq_validator import LocalBackend, check_arq7_compatibility

backend = LocalBackend("/Volumes/arqbackup1")
report = check_arq7_compatibility(
    backend, "/", encryption_password="...",
)
print(report.summary())
for c in report.failed_checks:
    print(f"  [{c.id}] {c.name}: {c.message}")
```

For an SFTP destination:

```python
from arq_validator import SftpBackend, check_arq7_compatibility

with SftpBackend(
    host="storage.example.com", user="u123",
    identity_file="~/.ssh/id_ed25519",
    root="/home/u123/arq",
) as backend:
    report = check_arq7_compatibility(
        backend, "/", encryption_password="...",
    )
    print(report.summary())
```

The checker never raises for format failures; everything lands in
the report.

### Strict mode — round-trip byte equivalence

The default invocation runs **schema-level** checks: the right
keys exist with the right types and values inside the expected
ranges. ``strict=True`` adds **byte-level** round-trip checks on
every parseable artefact (backuprecord, tree binary, xattr blob)
in the spirit of §5.6 (Strategy F + R4):

```python
report = check_arq7_compatibility(
    backend, "/", encryption_password="...",
    strict=True,
    strict_sample_cap=64,   # standardobjects per sweep; None = all
)
```

Strict mode adds three new check IDs to the report:

| ID  | What it checks                                       |
|-----|------------------------------------------------------|
| RT1 | BackupRecord ``parse → serialize`` is byte-identical |
| RT2 | Tree binary ``parse_tree → write_tree`` is byte-identical |
| RT3 | Xattr blob ``deserialize → serialize`` is byte-identical |

Drift in any of those would indicate a serialise-layer regression
the default checker can't catch (a refactor that drops compact
JSON separators, sorts xattr dict keys, or loses Tree v4 trailing
bytes). Strict mode is opt-in because it decrypts every sampled
blob; the default sample cap (64 per sweep) keeps runtime bounded
on large destinations.

## Limits of this verification

- **No live Arq.app round-trip in CI**: the checker validates
  conformance to the documented format, not behavioral
  compatibility with a specific Arq.app build. A
  byte-for-byte-conforming destination should be acceptable to
  Arq.app, but CI does not run Arq.app to prove it. (The operator
  has done a static Mach-O RE pass against Arq.app v8 confirming
  the Tree v4 emit choice — see below.)
- **Sampling for ID2 / A1**: standalone-object checks sample up
  to 32 blobs per run; full-suite verification is the L2 audit
  in `arq_validator.tiers.run_full_audit`, which HMACs every object.
  An incremental ledger
  (`arq_validator.incremental_audit.AuditLedger`) lets sweeps after
  the first skip already-confirmed blob_ids, surfaced via
  `--incremental` on both audit + record tiers (PRs #36, #39).
- **No cryptographic guarantees beyond what the checker tests**:
  e.g. PBKDF2 iteration count is asserted indirectly by C3
  (decryption succeeds with the same iteration count the writer
  emits). A future revision should pin the iteration count
  explicitly.

## Operator-side cross-checks against Arq.app

The operator has independently verified two binary-level
invariants by inspecting their installed Arq.app v8 (see
`docs/C1-MACHO-RE-PLAN.md` §"Findings (2026-05-10 RE session)"):

- **`nodeTreeVersion = 4` is hard-coded** in `-[BackupRecord init]`
  (`movl $0x4, 0x1c(%rax)`). Confirms our writer's
  `TREE_VERSION_V4_TRAILING_BLOCK = 4` matches Arq.app v8's emit
  default. Pinned via `tests/test_arq_app_tree_version_pin.py`.
- **`scannedAt` / `lastVerifiedAt` is NOT a stored Node property**
  on either `initWithDataBlobLocs:` or `initWithTreeBlobLoc:`.
  This means the 38-byte trailing block in Tree v4 is
  serializer-only metadata (writer-side data not propagated back
  into a Node attribute on read) — our reader's "treat as opaque"
  approach is correct.
