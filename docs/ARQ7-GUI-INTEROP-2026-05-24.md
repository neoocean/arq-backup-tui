# Arq 7 GUI interop verification — 2026-05-24

The operator installed the **Arq 7 GUI (Arq.app 7.44.1, macOS 15.7.3)** on
the working machine, which finally unblocked every verification that needs
a real Arq.app GUI. This document records, in detail, everything done in the
2026-05-24 session: the headline bidirectional byte-perfect interop result,
two production bugs the GUI surfaced, four supporting verifications, the
keyset-rotation parity fix, the scope decisions, and the final state of all
GUI-requiring items.

Companion records: `docs/COMPAT-VERIFICATION.md` (canonical strategy log),
`docs/WEEKEND-HUMAN-INTERVENTION-PLAN.md` (per-item P1–P8 tracker), git
PRs **#184** + **#185**, Perforce CLs **54128** + **54140**.

---

## 0. Headline — bidirectional byte-perfect interop with Arq 7

Within what an individual can do (one Mac, the operator's real Arq.app v8
destination, and a freshly-installed Arq 7 GUI), both directions are proven:

| Direction | Meaning | Result | Evidence |
|---|---|---|---|
| **A. our writer → Arq 7** | a destination our writer emits is read + restored by Arq.app's own GUI, byte-perfect | ✅ **PASS** | §1 Strategy I (GUI restore, content byte-identical) + §3 V1 (shape match) |
| **B. Arq 7 → our reader** | a destination Arq.app created is read + restored by our reader, byte-perfect | ✅ **PASS** | §7 fresh restore+verify of real Arq data + historical Strategy B (127,222 files) |

The format + behaviour layer has **zero known remaining gaps**. The two
production bugs below were invisible to all 20 prior verification surfaces
and only manifested through Arq.app's actual GUI restore path.

---

## 1. Strategy I (P1) — our fresh-walk Tree v4 → Arq.app GUI restore: **GREEN**

**Question:** does Arq.app's own GUI read and restore a fresh-walk Tree v4
destination our writer emits, and is the restored content byte-identical?
This was the last GUI-only headline and the definitive test of whether
Arq.app validates the v4 trailing-block scan-timestamp bytes our writer
cannot reproduce.

**Setup (automated):** synthetic source `/tmp/arq_stratI_src` (6 files: ASCII,
NFC Hangul name, 256-byte binary, 64 KB random, two nested levels, one xattr);
writer emit with `--use-packs --tree-version 4 --chunker arq_v7_41`; our reader
self-restore was byte-identical before handing to the GUI.

**Operator GUI steps:** added the destination as a local-folder storage
location, unlocked with the encryption password, browsed the Tree v4 tree,
restored to a target dir.

**Result:** Arq.app 7.44.1 recognised the destination, unlocked the keyset,
listed the v4 tree, and restored. `diff -r` + per-file SHA-256: **5/6 files
byte-identical; the 6th (Hangul) had identical content** (SHA-256 match) and
differed only in filename normalisation (§4). **Strategy I is GREEN** — Arq.app
does not reject our fresh-walk v4 nodes.

Two bugs had to be fixed first (§2); the first attempt failed at the restore
step and the second at the password prompt, both diagnosed and fixed below.

---

## 2. Production bugs surfaced by the GUI restore (PR #184 / p4 CL 54128)

### 2.1 backuprecord path divisor — 10⁵ → 10⁷

Arq names each backuprecord file by its creation-time Unix epoch (seconds),
split as:

```
backuprecords/<epoch // 10_000_000 :05d>/<epoch % 10_000_000 :07d>.backuprecord
```

Our writer used divisor **10⁵** with no zero-pad, so a 2026 epoch (~1.78×10⁹)
landed at e.g. `17796/1422.backuprecord` instead of `00177/9601422.backuprecord`.
Directory listing still worked (Arq globs `backuprecords/*/*.backuprecord`) but
GUI **restore** failed with `…backuprecord not found` because Arq recomputes the
canonical path from the record's `creationDate`.

Verified against `/Volumes/arqbackup1`: all 163 real records have 5-digit
buckets + 7-digit filenames; the 10⁷ decode yields valid 2026 timestamps.
Fixed in `arq_writer/backup.py`; corrected `tests/test_backuprecord_numbering.py`
(it had pinned the wrong 10⁵ encoding).

### 2.2 default `computer_uuid == plan_uuid` (folder name == planUUID)

Real Arq 7 always names a plan's top-level destination folder by its
`planUUID` (verified by decrypting `/Volumes/arqbackup1`'s backupplan.json:
folder `2DAC24D1-…` == `planUUID`). Our writer named the top folder by
`computer_uuid`, defaulted to a *separate* random UUID from `plan_uuid`, so a
default-config destination had `folder_name != planUUID` and Arq.app's GUI did
**not** recognise it as a backup location. Now coupled by default (explicit
distinct values still honoured): `arq_writer/backup.py`,
`tests/test_writer_folder_planuuid.py`.

### 2.3 pyright `str | None` (folded into PR #184)

The coupling logic initially inferred `str | None`, failing the pyright CI
gate on Python 3.12. Collapsed to an `or`-chain with a terminal generated UUID
so every assignment is statically `str`.

PR #184 merged to `main` (squash); the change was submitted to Perforce as
numbered CL **54128** scoped to exactly the three files.

---

## 3. V1 — same-source paired fingerprint (our writer vs Arq.app)

Backed up an identical synthetic source via both Arq.app (new plan → local
scratch destination) and our writer, then `arq-fingerprint compare`:

- **file_shape_diffs / chunk_pattern_diffs / folder / record counts: all
  identical** — the actual backup data shape is a perfect match.
- `match: false` came **only** from `backupplan.json`: `scheduleJSON` Hourly
  (Arq's new-plan default) vs Daily (our default) — a known, already-supported
  polymorphism — plus two optional keys (`lastBackedUp` Arq-only, a runtime
  field; `budgetGB` ours-only). These are config/optionality nuances, not data
  divergence. (Candidate minor parity follow-ups.)

---

## 4. NFC/NFD filename normalisation — resolved (no writer bug)

The Strategy I restore showed the Hangul filename came back **NFD**
(decomposed) while the source was **NFC**. Decrypting Arq's own V1 tree showed
Arq **stores** the node name in **NFC** — identical to our writer's stored form
and to the on-disk source. So neither tool normalises at backup time; the NFD
was purely **Arq's restore-output normalisation**. Our reader preserves NFC
(more faithful). Writer emit parity confirmed; content is always byte-identical.

---

## 5. K4 — Tree v4 trailing block on a fresh first-walk

Analysed the trailing block of Arq's fresh v4 emit (the V1 backup). On this
fresh first-walk the trailing-block seconds equalled the file's
btime/ctime/mtime and were ~9.5 min **before** the actual walk wall-clock —
i.e. the trailing block tracks the **file timestamp, not the walk time**,
supporting K3's "btime predictor" and validating our writer's `create_time`
fallback. Caveat: degenerate sample (all file timestamps coincident,
sub-second walk); separating btime/ctime/mtime needs a time-aged source.

---

## 6. P6 — keyset-rotation cross-tool + keyset_history parity (PR #185 / p4 CL 54140)

Verified keyset password rotation in **both directions** via the GUI:

- **H1 (ours → Arq):** `rotate_keyset_password` re-wraps the same master keys
  under a new password; Arq.app GUI unlocked the rotated destination with the
  new password.
- **H2 (Arq → ours):** the operator changed the encryption password in Arq's
  GUI; our reader unlocked + restored byte-identical with the new password.
  Master keys (incl. `blob_id_salt`) preserved.

**Parity gap found + fixed:** on a password change Arq archives the superseded
keyset to `keyset_history/encryptedkeyset_<unix-epoch>.dat` (epoch = rotation
time) before writing the new live keyset. Our rotation overwrote
`encryptedkeyset.dat` without archiving. Added
`rotate_keyset_password_on_disk(backend, computer_uuid, *, old_password,
new_password, archive_old=True)` in `arq_writer/crypto_write.py` (read →
re-wrap → archive OLD to keyset_history before overwriting → write new); the
TUI maintenance rotation screen now routes through it.
`tests/test_keyset_history_rotation.py` pins the behaviour. PR #185 merged;
Perforce CL **54140** scoped to exactly the four files.

---

## 7. Direction B — fresh restore of real Arq-created data (this session)

Restoring from the operator's real Arq.app v8 destination
`/Volumes/arqbackup1` (smallest folder `402790CC`, ~3.2 GB / 127,222 files)
with `--verify-after` — which recomputes each restored file's SHA-256 and
compares it to the blob_id Arq recorded, a byte-perfect check needing no
separate original (`failures: []` == byte-perfect).

| Run | Scope | Result |
|---|---|---|
| Fresh bounded verify (2026-05-24) | `data/comkey` + `data/direct-uploads` (3 files, 43 KB) | **3/3 verified, restore failures `[]`, verify failures `[]`** ✅ |
| Fresh full-folder restore (2026-05-24, interrupted for time) | first 4,813 files (~136 MB) before stop | restored cleanly, no content errors (`[chown_failed]` warnings are expected on a cross-user restore and do not affect content) |
| Historical Strategy B (§3.4) | full folder, 127,222 files / 3.17 GB | byte-perfect, `verify.failures: []` |

`--verify-after` confirms Arq-created file content matches Arq's own recorded
content hashes after our reader decrypts + decompresses + reassembles it —
i.e. **we read and restore Arq 7 backups byte-perfectly.**

---

## 8. Scope decisions (2026-05-24)

- **Arq 5/6 unsupported** — operator decision. The project targets Arq 7 only;
  the GUI item **P3** (mixed Arq5/6/7 destination shown in GUI) is permanently
  out of scope.
- **P4 (Arq.app daemon concurrency race) — declined** — operator decision; not
  pursued (low value, risks interfering with the operator's real backup daemon).
- **P5 (binary-plist GUI acceptance) — moot** — our default emit is JSON (which
  Arq.app itself emits and reads), and Strategy I verified the JSON path, so the
  legacy binary-plist path needs no separate GUI test unless we change the
  default.

---

## 9. GUI-requiring verification — final status

| Item | Requires Arq 7 GUI? | Status |
|---|---|---|
| **P1 Strategy I** (GUI restore of our v4 emit) | yes | ✅ done — GREEN |
| **P2 K4 first-walk-time** | yes | ✅ done |
| **P6 keyset rotation (H1+H2)** | yes | ✅ done + parity fix landed |
| V1 same-source paired fingerprint | yes | ✅ done |
| **P3 mixed Arq5/6/7** | yes | 🚫 out of scope (Arq 5/6 unsupported) |
| **P5 binary-plist GUI acceptance** | yes | ⚪ moot (default is JSON, P1-verified) |
| **P4 daemon concurrency (A1/A2/A3)** | yes | ⚠️ declined |
| P7 clock jump / P8 network latency | no (VM / network) | out of GUI scope |

**Every GUI-requiring verification with standing value is complete.** Nothing
GUI-blocked remains except items explicitly declined or out of scope.

---

## 10. Artifacts

| Kind | Reference |
|---|---|
| Bug fixes (divisor + folder==planUUID + pyright) | GitHub PR **#184** → main; Perforce CL **54128** |
| keyset_history archival on rotation | GitHub PR **#185** → main; Perforce CL **54140** |
| Strategy I detail | this doc §1; `docs/COMPAT-VERIFICATION.md` |
| GUI task tracker | `docs/WEEKEND-HUMAN-INTERVENTION-PLAN.md` (P1/P2/P6 done) |
