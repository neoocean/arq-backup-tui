# arq-backup-tui

An independent verifier + restorer + writer + TUI for Arq Backup 7 format
destinations. Pure Python ≥ 3.9 + stdlib (HMAC, AES, LZ4, etc. are all
implemented directly or invoked through the system `openssl`).

## 1. What This Project Intends — and What It Does Not

This project has **no intent whatsoever** to infringe on the intellectual
property of [Arq Backup](https://www.arqbackup.com/). It was created for
two motivations:

1. **Learning backup file-format design**. The core purpose of this repository
   is to understand at the source level how concepts such as encryption,
   compression, dedup, content-addressable storage, and incremental snapshots
   actually combine in practice. In one line: the code organizes a logical
   understanding of "how a widely used backup tool internally lays out its
   data."
2. **Long-term reliability assurance for Arq backups used for over 15 years**.
   It is not meant to replace Arq.app itself, but to ensure the operator can
   **verify the integrity of their Arq 7 destinations on their own at any
   time**, independent of Arq.app GUI's monthly self-check, and to keep the
   data **readable** even in environments where the GUI disappears or becomes
   incompatible. This is a second-source tool for trusting and continuing to
   use this data into the future.

For that reason this code is **not a tool that replaces the commercial value of
Arq Backup**, and the following items are **intentionally not implemented**:

- **Support for S3-compatible storage (S3, Wasabi, B2, Storj, GCS, Azure Blob,
  …)**. Bulk management of cloud backends is one of Arq Backup's core value
  propositions, and providing this feature here would dilute the incentive to
  purchase an Arq.app license. **If you want cloud backends, please [purchase an
  Arq Backup license](https://www.arqbackup.com/).** (For reference, this
  project supports only local / NAS / SFTP, and users who need a cloud
  destination must use a `rclone mount` workaround.)
- **Operational features of the Arq.app GUI**: scheduling / notifications /
  menu bar / system tray / dashboard / cloud password recovery / license
  management, and so on, belong to the policy layer of Arq.app and are outside
  the scope of this project.
- **Brute-force tooling against the `encryptedkeyset.dat` file itself**: the
  encryption / decryption code in this project is for legitimate password
  holders to verify and restore their own backups. Do not use it as an attack
  tool against destinations whose passwords are unknown.

## 2. Relationship to Arq.app / arq_restore

This project uses the publicly available data-format specification of Arq
Backup as reference material:

- **Official Arq 7 data format documentation**: https://www.arqbackup.com/documentation/arq7/English.lproj/dataFormat.html
- **Official Arq 5 format documentation** (source of the PBKDF2 / HMAC rules
  used in Arq 7): https://www.arqbackup.com/arq_data_format.txt
- **`arq_restore` (BSD 3-Clause)**: a reference restore implementation released
  by Arq. This project used the source of `arq_restore` as a **verification
  reference for format claims** (for example, the branching logic in
  `Arq7BlobReader.m::dataForBlobLoc:`). The work is not file-by-file or
  line-by-line copying but a Python re-implementation of the binary layout of
  the alphanumeric format, and per the BSD license the source is acknowledged
  in this section.

A copy of the BSD 3-Clause license for `arq_restore` is available in the
official Haystack Software Inc. GitHub repository.

In addition, several items not specified by the reference (for example, the
fact that backuprecord is emitted as JSON rather than binary plist, the
`isLargePack` field in the BlobLoc binary layout, and the 38-byte trailing
block in Tree v4) were reverse-engineered by directly analyzing the bytes of
the operator's actual destinations (see `docs/REAL-DATA-DISCOVERIES.md`).

## 3. What It Can Do

The four libraries this package provides:

| Package | Role | Entry point |
|---|---|---|
| `arq_validator` | 4-tier integrity verification of an Arq 7 destination (L0/L1a/L1b/L2 + audit-drip) | `python -m arq_validator` |
| `arq_reader` | Restoring Arq 7 (+ 5/6) backups to local files | `python -m arq_reader` |
| `arq_writer` | Writing new Arq 7 backup destinations | `python -m arq_writer` |
| `arq_tui` | Integrating the three above into a single Textual TUI | `python -m arq_tui` or `./arq-tui.py` |

### Validation (validator)

```sh
python -m arq_validator --root /Volumes/arqbackup1 --tier deep \
    --password "$ARQ_PW"
```

The 4 tiers:
- **L0** (`dry-run`): directory shape only (computer-UUID, the 4 object families, backupfolders)
- **L1a** (`quick`): sample sweep of ARQO magic bytes (default 5%)
- **L1b** (`deep`): keyset decryption + HMAC of the latest backuprecord per backup folder
- **L2** (`audit`): HMAC of every EncryptedObject (+ resumable audit-drip mode)
- **`record`**: walk every BlobLoc reachable from one backuprecord (catches missing/corrupt deep blobs that L0–L2 don't)

**Incremental audit ledger** (`audit` + `record` tiers): pass `--incremental`
to skip blobs already confirmed in a per-destination ledger; pass
`--ledger-prune-days N` to drop entries older than N days so a quietly-bad
blob eventually gets re-audited. Ledger lives at
`~/.local/state/arq-backup-tui/audit-ledgers/<target>.json`. See
`arq_validator/incremental_audit.py`.

### Restore (reader)

```sh
python -m arq_reader restore /Volumes/arqbackup1 \
    --password "$ARQ_PW" \
    --folder-uuid <FU> --dest /tmp/restored
```

A specific historical record / specific path / specific source folder can
each be designated. Read is supported for Arq 5/6/7 alike.

**Dry-run preview** (`--list-only`): walk the backuprecord's tree + emit
`would_restore_file` events without touching the destination. Use to
verify a `--paths` filter, size a restore, or spot-check a snapshot's
contents before paying the I/O cost. The dest argument is still
positional but unused in dry-run mode.

```sh
python -m arq_reader restore /Volumes/arqbackup1 \
    --password "$ARQ_PW" \
    --list-only --paths Documents/notes \
    <folder-uuid> /tmp/dummy
```

**Conflict policy** (`--on-conflict`): choose `overwrite` (default,
silent), `skip` (drop restored bytes + emit `conflict_skipped`), or
`rename` (write to `name.restored-N` so both versions remain).

### Writing (writer)

```sh
arq-backup create ~/Documents \
    --dest /Volumes/arqbackup1 \
    --password "$ARQ_PW" \
    --use-packs --chunker arq_v7_41 \
    --exclude-glob '*.log' --max-file-bytes 1073741824
```

The destination produced is guaranteed to round-trip byte-identically with
this verifier and reader; compatibility with the Arq.app GUI side is an area
that requires manual operator verification, since our reader cannot
distinguish it.

### TUI

```sh
./arq-tui.py    # or python -m arq_tui
```

A Textual TUI that lets you handle the three above on a single screen. Backup
/ restore / verification / scouting / backup-set browser / retention policy
application / password rotation / plan editing / console (slash-command), etc.

## 3.5 Quick start (5 minutes from clone to first restore)

Goal: prove the round-trip on your own data without leaving your
machine. No SFTP, no cloud, no Arq.app interaction.

```sh
# 1. Clone + check the runtime is sane
git clone https://github.com/neoocean/arq-backup-tui.git
cd arq-backup-tui
python3 --version          # need ≥ 3.9
openssl version            # need any modern OpenSSL on PATH
python3 -m unittest discover tests   # 200+ tests should pass

# 2. Pick a small source folder + a fresh destination
SRC=~/Documents/sample-folder
DST=/tmp/arq-test-dst
mkdir -p "$DST"

# 3. Make your first backup
export ARQ_PW="hunter2"
python3 -m arq_writer create "$SRC" --dest "$DST" \
    --password-env ARQ_PW \
    --backup-name "first-backup" \
    --use-packs --dedup-against-existing

# 4. Validate it (HMAC the latest record + sample objects)
python3 -m arq_validator deep "$DST" --password-env ARQ_PW

# 5. Restore it elsewhere
mkdir -p /tmp/arq-restored
python3 -m arq_reader restore "$DST" \
    --password-env ARQ_PW \
    --dest /tmp/arq-restored

# 6. Compare bytes
diff -r "$SRC" /tmp/arq-restored
```

If step 6 prints nothing, the round-trip succeeded — your bytes
came back byte-identical through every layer.

For the **TUI** experience instead of CLI:

```sh
pip install -e ".[tui]"     # adds the textual dependency
python3 -m arq_tui          # launches the TUI; press [n] to make a plan
```

Inside the TUI:
- `[n]` create a backup plan
- `[r]` run the focused plan
- `[b]` browse a destination's history
- `[v]` validate a destination
- `[a]` watch every running backup / restore (cron-launched ones too)
- `[s]` manage cron / launchd schedules for plans
- `[p]` (inside a backup run) pause / resume

Operators with an existing Arq.app destination on Hetzner / NAS
SFTP: see `docs/COMPAT-SFTP-TESTING.md` for the `.secrets/`
credential setup; once configured, the same commands work
against the remote destination via `--sftp-host` etc.

## 4. Dependencies and Runtime Environment

- **Runtime**: Python ≥ 3.9 + system `openssl` (on PATH or via
  `--openssl-path`)
- **For SFTP**: the system's OpenSSH `ssh` / `sftp` client
- **Python third-party**: none (only the TUI optionally depends on `textual`)
- **OS**: macOS / Linux. Windows is unsupported due to OpenSSH/openssl
  behavior differences.

## 4.5 Development setup

If you intend to contribute or run the test/lint stack locally:

```sh
# 1. Install the dev extras
pip install -e ".[test,tui]" pyright pre-commit

# 2. Wire up the pre-commit hooks
pre-commit install

# 3. (Optional) Run all hooks against the current tree once,
#    so your first commit doesn't trip a check that's been
#    sitting unnoticed.
pre-commit run --all-files
```

Hooks that run on every commit (configured in
`.pre-commit-config.yaml`):

- `check-doc-links` — fails when a `.md` file references a
  renamed/removed `arq_*/...py` path or undefined symbol
  (the same checker CI runs on every PR).
- `pyright` — soft-skips when not installed locally; CI runs
  it unconditionally on every PR.
- Standard hygiene: trailing whitespace, missing EOF newlines,
  large file accidents, malformed YAML/TOML, merge-conflict
  markers.

CI also runs the static type check (`pyright`) and the doc-link
checker on every PR — see `.github/workflows/test.yml`. Local
hooks just shorten the feedback loop.

## 5. License

This repository itself is under the **MIT License** (`LICENSE`).

Arq Backup, "Arq", and related trademarks are property of Haystack Software
Inc. This project has never received sponsorship or official endorsement
from Haystack Software in any form.

## 6. Further Reading

- `DESIGN.md` — overall project design
- `docs/MECHANISM.md` — detailed inner workings of backup/restore/verification
- `docs/COVERAGE.md` — Arq 7 feature parity matrix
- `docs/COMPATIBILITY.md` — lock-in of the 25 Arq 7 invariants
- `docs/COMPAT-VERIFICATION.md` — catalog of compatibility verification strategies
- `docs/COMPAT-SFTP-TESTING.md` — integration tests based on operator credentials
- `docs/REAL-DATA-DISCOVERIES.md` — compatibility items discovered and fixed
  via real destinations (`isLargePack`, JSON backuprecord, Tree v4, etc.)
- `docs/PLAN-tui.md` — TUI design + screen catalog
- `docs/APFS-SNAPSHOTS.md` — macOS APFS snapshot integration
- `docs/UNICODE.md` — guarantees for multilingual / emoji / long-path handling
- `docs/RESEARCH-backup-creation-feasibility.md` — feasibility study before
  writing the writer
- `docs/RESEARCH-format-extensions.md` — RE notes on pack containers /
  chunkers / Arq 5–6 features
