# Compatibility testing against a real SFTP destination

> **Status (2026-05-08)**: ✅ PR #9 introduced the format / shape
> compatibility-verification harness (`test_arqapp_sftp_compat.py`).
> Follow-up work added `test_arq_real_destination.py` and the
> `.secrets/` credentials directory, enabling **runtime verification
> of the three pillars — reader / validator / writer — against a real
> destination**. All integration tests skip in the default environment
> and only run for operators who supply credentials.

This document defines the procedure for an operator to use a **live,
production Arq 7 SFTP destination** from a sandbox to automatically
verify reader / validator / writer / fingerprint compatibility. It
turns the strategies marked with ⭐ in `docs/COMPAT-VERIFICATION.md`
(Strategy A + B) into automated regression tests.

## 0. Credential sources — `.secrets/` or `.env`

Credentials are resolved from these three sources in order (the first
value found wins):

1. **`.secrets/`** (recommended) — best for credentials kept on a
   workstation long-term. Centralizing them in one place makes
   auditing and rotation easier. Layout:
   ```
   .secrets/
   ├── README.md                  ← committed (instructions)
   ├── sftp.json.example          ← committed (template)
   ├── dest_password.example      ← committed (template)
   ├── sftp.json                  ← local only, real SFTP info
   └── dest_password              ← local only, Arq encryption password
   ```
   Setup:
   ```sh
   cp .secrets/sftp.json.example      .secrets/sftp.json
   cp .secrets/dest_password.example  .secrets/dest_password
   $EDITOR .secrets/sftp.json
   $EDITOR .secrets/dest_password
   chmod 600 .secrets/sftp.json .secrets/dest_password
   ```
2. **`.env`** (legacy) — `KEY=VALUE` per line, single file. Kept for
   PR #9 compatibility.
3. **`os.environ`** (CI / one-off) — export environment variables
   directly.

`.gitignore` / `.p4ignore` exclude the actual credential files
(`.secrets/sftp.json`, `.secrets/dest_password`, `.env`, `.env.local`)
from both git and Perforce. Only the templates (`README.md`,
`*.example`) are committed.

## 1. Security policy

### 1.1 Credential-handling principles

- **Never committed to Git**: `.env`, `.env.local`, and
  `tests/integration/.env` are all listed in `.gitignore`
- **Local files only**: `.env` lives on the operator's machine. It is
  not exposed to CI / GitHub / any remote
- **Read-only**: every integration test **performs no write
  operations against the destination** (it only calls SftpBackend's
  read_all / read_range / list_dir)
- **Log isolation**: the code is written so that SSH passwords and
  Arq passwords are never leaked to stdout, stderr, or failure
  messages

### 1.2 PII exposure prevention

The tests **never assert the exact contents** of any file.
- Structural checks only: file existence, size ranges, ARQO magic,
  HMAC match, blob_id self-consistency
- The operator's actual file contents stay in memory and are never
  written to disk or to the logs
- Sample restores write to a temporary directory
  (`tempfile.TemporaryDirectory`) and clean up immediately

### 1.3 Recommended credential practices

- Use a **read-only-only SFTP account** when possible
- Restrict access to just the destination directory via `chrooted` or
  `ChrootDirectory`
- Authenticate with an **SSH key** instead of a password (easier to
  revoke / rotate)
- Use a different Arq password per destination (do not reuse them)

## 2. Environment-variable contract

| Variable | Required | Description |
|------|:----:|------|
| `ARQ_TEST_SFTP_HOST` | ✓ | SFTP hostname or IP |
| `ARQ_TEST_SFTP_USER` | ✓ | SSH username |
| `ARQ_TEST_SFTP_PORT` | | Defaults to 22 |
| `ARQ_TEST_SFTP_ROOT` | ✓ | Server-side destination root path (e.g. `/home/u123/arq`). The `<COMPUTER-UUID>/` directories must live underneath it |
| `ARQ_TEST_SFTP_AUTH_PASSWORD` | △ | SSH password |
| `ARQ_TEST_SFTP_IDENTITY` | △ | Path to the SSH private-key file |
| `ARQ_TEST_DEST_PASSWORD` | ✓ | The Arq destination's encryption password (separate from the SSH password) |

At least **one** of `ARQ_TEST_SFTP_AUTH_PASSWORD` or
`ARQ_TEST_SFTP_IDENTITY` must be set. If both are empty the tests
auto-skip.

## 3. Setup procedure

### 3.1 Writing the .env file

```bash
cd /path/to/arq-backup-tui
cp .env.example .env
chmod 600 .env       # so other users cannot read it
# Open .env in an editor and fill in the real values
```

### 3.2 Sanity check

```bash
# Confirm the credentials are picked up (skipped automatically in CI)
python -m unittest discover -s tests/integration -v
```

If credentials are missing, every test skips with this message:

```
real-SFTP integration tests skipped — no credentials in env
(see docs/COMPAT-SFTP-TESTING.md)
```

If credentials are present, the SSH master is set up once and 7 tests run.

### 3.3 Operator-paste workflow (chat interface)

Because the sandbox does not have credentials, the workflow is for the
operator to **write `.env` locally and then paste the integration-test
output**:

```bash
# On the operator's machine:
git pull origin main
cp .env.example .env && chmod 600 .env
# Edit .env (enter credentials)
python -m unittest discover -s tests/integration -v 2>&1 | tail -50
```

Pasting that output (in full or the last 50 lines) into chat lets the
sandbox analyze the failure cause and land a fix.

**Important**: to make sure the SSH password and Arq password are not
included when you paste, **the tests themselves are written to never
print credentials** (`tests/integration/_creds.py`). A quick review
before pasting is still recommended.

## 4. Test catalog

### 4.1 Format and shape compatibility — `test_arqapp_sftp_compat.py` (PR #9, 7 tests)

| Test | What it verifies |
|--------|------------|
| `test_layout_discovers_computer` | `discover_layout` finds at least one computer plus folder |
| `test_keyset_decrypts` | `encryptedkeyset.dat` PBKDF2-SHA256 + AES-CBC + HMAC decrypt + 32B field shape |
| `test_compatibility_audit_passes` | All 25 invariants of `check_arq7_compatibility` pass |
| `test_validator_l0_l1a_l1b_tiers_pass` | QUICK + DEEP tiers pass (the L2 audit is too long-running, so it must be invoked separately) |
| `test_fingerprint_is_well_formed_json` | `compute_shape_fingerprint` output is JSON-serializable, schema_version=1 |
| `test_records_list_at_least_one` | At least one backuprecord exists |
| `test_sample_standalone_object_arqo_valid` | Sample 16 standalone objects up to 1 MiB → verify ARQO + HMAC + blob_id = SHA-256(salt+plaintext) |

### 4.2 Runtime behavior — `test_arq_real_destination.py` (3 tests)

Verifies the runtime behavior of the three pillars (reader / validator
/ writer) against the operator's real destination rather than a
sandbox. Note that **the writer never writes to the root of the
operator's destination** — it only operates inside the dot-prefixed
subdirectory `creds.write_subdir` (defaulting to
`.arq-backup-tui-write-test`).

| Test | Class | What it verifies |
|--------|--------|------------|
| `test_restore_latest_record_of_first_folder` | `RealDestinationReaderTests` | Restore the latest record of the first folder into a tempdir → at least one non-empty file in the tree (must pass if decryption is working) |
| `test_audit_drip_capped_at_a_few_megabytes` | `RealDestinationValidatorTests` | Run an L2 audit-drip with a 4 MiB / 20 s cap → 0 failures, cursor advances |
| `test_round_trip_via_real_sftp` | `RealDestinationWriterTests` | Write a synthetic backup into the sandbox directory → restore via the reader → byte-identical comparison (alpha.txt + 한글.txt + subdir/gamma.bin) → DEEP-tier validator |

The writer test re-initializes / cleans up the sandbox via `rm -rf` in
setUp / tearDown. The operator's real data is never modified.

Each test sets up and cleans up **its own SFTP master**, so test order
is independent.

## 5. Automation hook (does not run in CI — local only)

The default `python -m unittest discover` does pick up
`tests/integration/`, but if credentials are missing every test
auto-skips, so CI is unaffected. Real verification only happens on the
operator's machine where credentials are populated.

If you do want to register SFTP credentials as CI secrets, you can
add a separate workflow file (`.github/workflows/sftp-integration.yml`)
— `pull_request_target` security policy is required to prevent secret
values from leaking to external PRs.

## 6. Findings + fix flow

Depending on which invariant failed in the operator's pasted output,
apply the matching fix pattern:

| Failed invariant | Cause / resolution |
|---------------|------------|
| `L1` (no top-level UUID) | Wrong root path; correct `ARQ_TEST_SFTP_ROOT` in `.env` |
| `C1` (keyset magic mismatch) | File corruption; check the SFTP server filesystem |
| `C3` (keyset decrypt failure) | Wrong Arq password (`ARQ_TEST_DEST_PASSWORD`) |
| `L3` (config key missing) | A new Arq.app version added a new key our reader does not know about; add it to our `_BACKUPCONFIG_REQUIRED` |
| `L4` (plan key missing / type mismatch) | Same; update `_BACKUPPLAN_REQUIRED` or `_BACKUPFOLDER_REQUIRED` |
| `B2` (backuprecord key missing) | Update our `_BACKUPRECORD_REQUIRED_KEYS` |
| `A1` (ARQO magic mismatch) | File corruption, or an envelope format we do not know about; revisit the spec |
| `ID2` (blob_id mismatch) | File corruption, or our `compute_blob_id` algorithm needs to change |
| `validator l1a / l1b failure` | Same root causes; the tier output reveals the exact file path |

## 7. SFTP-credentials chat-paste guide

If the operator wants to grant **direct sandbox access** to SFTP
(chat-interface only):

Option A — paste `.env` contents into chat:

```
ARQ_TEST_SFTP_HOST=...
ARQ_TEST_SFTP_USER=...
ARQ_TEST_SFTP_PORT=22
ARQ_TEST_SFTP_ROOT=/...
ARQ_TEST_SFTP_AUTH_PASSWORD=... or ARQ_TEST_SFTP_IDENTITY=~/...
ARQ_TEST_DEST_PASSWORD=...
```

→ The sandbox exports those values as environment variables and runs
`python -m unittest discover -s tests/integration -v` → pastes the
results back.

**Use extreme caution because the chat message contains passwords**:
- Rotate the password immediately after the test
- Use a read-only-only SSH account when possible
- Limit to disposable credentials

Option B — paste an SSH key:

The operator pastes the **private-key contents** into chat:

```
-----BEGIN OPENSSH PRIVATE KEY-----
...key contents...
-----END OPENSSH PRIVATE KEY-----
```

→ The sandbox saves it as `/tmp/test-key` (mode 0600) → exports
`ARQ_TEST_SFTP_IDENTITY=/tmp/test-key` → runs the tests → deletes the
key file immediately when done.

Option B is safer than a password (easier to revoke / rotate; even if
it accidentally lands in a CSV / DB log, it cannot be used without
the key file).

Option C — operator runs locally, pastes results only:

The safest choice. The operator runs the tests locally with their own
`.env` → only pastes the result text into chat → the sandbox can
analyze + fix without ever holding credentials.

Recommendation: use Option C whenever possible. If Option A / B is
unavoidable, use disposable credentials and rotate immediately after.

## 8. Future work

- When the operator pastes a fixture, preserve it under
  `tests/fixtures/arqapp_real_sftp/` → CI then regresses it on every
  PR
- A local-destination version of `tests/integration/test_arqapp_local_compat.py`
  with the same structure (so regression is possible without an SFTP
  dependency)
- Allow the AUDIT (L2) tier to be run separately via the
  `ARQ_TEST_RUN_FULL_AUDIT=1` environment variable
