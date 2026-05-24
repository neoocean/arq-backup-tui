# Feasibility study: fully sharing Arq 7's config (plans + storage) read **and write**

**Goal (operator request, 2026-05-24).** Make `arq-backup-tui` operate on the
*same* configuration backend as the Arq 7 GUI — its backup plans, storage
locations, schedules, secrets — so that a plan created in the TUI and a plan
added in the Arq GUI are indistinguishable and behave identically. The
read-only `server.db` mirror in the TUI deliberately avoided write; the
operator wants to evaluate closing that gap, knowing it is risky.

This document maps the shared-config surface from direct inspection of the
installed Arq 7 (7.44.1) on this machine, analyses the barriers, evaluates
approaches, and proposes phased scenarios with go/no-go criteria. **It is an
assessment, not an implementation.**

---

## 1. What Arq's config actually is (measured, not assumed)

Evidence gathered 2026-05-24 against the live install:

| Aspect | Finding |
|---|---|
| Config store | `/Library/Application Support/ArqAgent/server.db` — SQLite, **`root:wheel`, mode `0644`** (world-readable, **root-write-only**), `journal_mode=delete` (not WAL), `user_version=0`, no migration/version table. |
| Owner process | **`ArqAgent` daemon runs as root** (observed PID), holds + writes `server.db`, caches config in memory. The GUI `Arq.app` runs as the user and talks to the daemon. |
| Plans / storage tables | `backup_plans` and `storage_locations`. Each row has **both** flat typed columns **and a canonical `json` TEXT blob** (the same object shape as `backupplan.json`), plus bookkeeping columns `sync_needed`, `last_backed_up`, `cpu_usage`. The live `name`/`plan_uuid` flat columns were empty on this install — Arq drives off the `json` blob; the flat columns are partial/legacy. |
| Secrets in the DB | **None.** `backup_plans.encryption_password` / `smb_password` and `storage_locations.password` / `private_key_passphrase` are **empty in every row.** A world-readable DB never holds the secrets. |
| Where secrets live | macOS **Keychain** (under Arq's own service/access group — `security find-generic-password -s Arq` finds nothing for the user) **+ root-only sidecars**: `secrets/backupplanderivedencryptionkey/`, `localkeysv2.dat` (0600 root), `computerKeys.json` (0600 root). |
| Sanctioned write API | **None for config.** `arqc` exposes only `acceptLicenseAgreement / activateLicense / setAppPassword / listBackupPlans / latest…Activity… / startBackupPlan / stopBackupPlan / pause/resumeBackups`. **No create-plan, add-storage, or import.** No `CFBundleURLTypes` (no URL scheme), no `.sdef` / `NSAppleScriptEnabled` (not scriptable). |
| Read access | Already solved: the TUI's read-only `server.db` mirror opens it with `mode=ro` and decodes `storage_locations` / `backup_plans` / `activities` (non-secret fields). |

**Why write was avoided** is now explicit: the DB is root-owned and
daemon-owned, the secrets aren't in it, and there is no supported write path.

---

## 2. Barriers to "TUI writes config Arq honors identically"

| # | Barrier | Severity | Notes |
|---|---|---|---|
| **B1** | **Privilege** — `server.db` is root-write-only | High | TUI runs as the user; every config write needs root (sudo / privileged helper). |
| **B2** | **Daemon ownership + concurrency** — ArqAgent owns the DB, caches in memory, `journal=delete` | High | An external write while the daemon runs risks lock contention, a stale in-memory cache that re-overwrites our row, or DB corruption. The daemon has no "reload config from disk" signal we know of. |
| **B3** | **Secret provisioning** — keyset/derived key + cloud/SFTP creds live in Keychain (Arq's access group, tied to Arq's code-signing identity) + root-only sidecars | **Critical** | A plan row without its keyset is inert — Arq can't back up or restore it. Reproducing Arq's Keychain items requires its entitlements/signing identity (a third-party process generally **cannot** write into another app's Keychain access group), and reproducing `secrets/backupplanderivedencryptionkey/` + `localkeysv2.dat` requires Arq's exact key-derivation. This is the dominant blocker for true parity. |
| **B4** | **Dual representation** — flat columns **and** canonical `json` blob + `sync_needed` | Medium | A write must populate the `json` blob in Arq's exact shape (we already emit this shape for `backupplan.json`), the flat columns Arq actually reads, and set `sync_needed` correctly, atomically. |
| **B5** | **Schema / json instability** — no `user_version` gate; Arq migrates in code | High (ongoing) | Arq can change columns or the `json` shape on any update. A direct writer couples us to an undocumented, unversioned internal schema → silent breakage after an Arq update. (Our destination-format compat suite does **not** cover this *local* DB.) |
| **B6** | **No sanctioned API → unsupported/tamper** | High | Writing the agent's private DB is outside any supported interface; could be treated as tampering, void support, or trip future integrity checks. |

---

## 3. Approaches considered

### A. Direct `server.db` + Keychain write-through (full parity)
Write plan/storage rows (flat + `json` + `sync_needed`) via a root helper and
provision secrets into Keychain + the root sidecars.
- Blocked primarily by **B3** (cannot reliably write Arq's Keychain
  access-group items / derived-key sidecars without Arq's identity) and
  fragile under **B2/B5**. **Verdict: not achievable as true parity today; the
  secret layer is effectively closed to third parties.**

### B. Drive Arq through a sanctioned interface (URL scheme / AppleScript / arqc)
Have Arq itself create the config (so Arq writes its own DB + Keychain).
- **Not available**: no URL scheme, not scriptable, and `arqc` has no
  create/import. **Verdict: impossible without Arq adding such an API.**

### C. Operator-mediated provisioning
TUI generates a complete plan/storage **spec**; the operator performs the
one-time "add" in the Arq GUI (which provisions secrets correctly); the TUI
then reads + drives it (`arqc startBackupPlan`).
- Sidesteps B1/B2/B3/B6 entirely. Not "identical authoring", but achieves
  "TUI and GUI converge on one shared config" with Arq owning writes.
  **Verdict: feasible and safe; partial UX.**

### D. Status quo — separate config, interoperate at the format/destination layer
TUI keeps its own plan model; interop is guaranteed where it matters: our
writer's destinations are byte-perfect-restorable by Arq's GUI and vice versa
(proven both directions, `docs/COMPATIBILITY.md`).
- **Verdict: lowest risk, already shipping; does not meet the "shared config"
  goal.**

---

## 4. Recommended phased scenario (risk-gated)

Treat "shared config" as a ladder; stop at the first rung whose risk the
operator won't accept. Every write rung is **opt-in, off by default**, and
must: back up `server.db` first, operate inside a single transaction, **pin
the live schema via a fingerprint** (abort on drift — analogous to the
destination compat suite, but for `server.db`), and prefer pausing the daemon
(`arqc pauseBackups` / stop ArqAgent) over racing it.

- **Phase 0 — Full READ parity (DONE / extend).** Mirror every plan + storage
  + activity field read-only. No risk. Already implemented as the TUI's
  read-only `server.db` mirror.
- **Phase 1 — Operator-mediated authoring (Approach C).** TUI composes a plan
  spec and walks the operator through adding it in the Arq GUI (secrets
  provisioned by Arq), then adopts it read+run. Safe; recommended next step.
- **Phase 2 — Secret-free *edits* of an existing Arq plan (guarded direct
  write).** Only fields that carry no secret and that Arq re-reads cleanly
  (e.g. schedule, retention, exclusions, thread count). Requires: root helper
  (B1), daemon paused (B2), DB backup + transaction + schema-fingerprint gate
  (B5), write both flat + `json` + `sync_needed` (B4), dry-run + diff +
  rollback. Still **experimental**; one Arq update can break it (B5).
- **Phase 3 — Full TUI-authored plans incl. secrets (Approach A).** Gated on a
  reproducible, verified path to provision Arq's Keychain items + derived-key
  sidecars (B3). **Currently believed infeasible** for a third-party tool;
  revisit only if Arq exposes a config-import API or the Keychain layer is
  shown to be reproducible.

---

## 5. Scenarios (concrete walk-throughs)

**S1 — "I create a plan in the TUI; it shows up in Arq identically." (the goal)**
What must happen: write `storage_locations` + `backup_plans` rows (flat +
`json` + `sync_needed`) **and** install the encryption keyset into Arq's
Keychain access group + `secrets/backupplanderivedencryptionkey/` +
`localkeysv2.dat`, as root, without the daemon clobbering it.
Where it breaks: **B3** (Keychain/derived-key provisioning) — the plan row
would appear in the GUI list but be **non-functional** (no usable keyset), and
B2 may revert the row on the daemon's next flush. *Not achievable today.*

**S2 — "Arq GUI creates a plan; the TUI sees and runs it." → WORKS NOW.**
TUI reads it via the read-only mirror; `arqc startBackupPlan <uuid>` runs it;
our reader restores its destination byte-perfectly. This direction is the
already-proven interop and needs nothing new.

**S3 — "I edit an existing Arq plan's schedule/retention from the TUI." →
Phase-2 candidate.** No secret involved. Feasible *only* behind the Phase-2
safeguards (root helper, daemon paused, DB backup + txn + schema-fingerprint
gate, write flat+json+sync_needed, verify by re-reading + GUI confirm). Medium
risk; breaks if Arq changes the schema (B5).

**S4 — Failure mode: concurrent write / daemon clobber.** TUI writes a row
while ArqAgent has the DB cached; the daemon's next flush overwrites it, or
the rollback journal collides → inconsistent/corrupt config. Mitigation:
pause/stop the agent for the write window; never write WAL-less SQLite under a
live owner; always snapshot `server.db` first and offer one-click restore.

**S5 — Failure mode: Arq update changes the schema/json.** A future Arq writes
a new column or json key; our writer produces rows the new daemon mis-reads →
silent config corruption. Mitigation: a `server.db` **schema fingerprint**
captured per Arq version (mirror of the destination compat suite); the writer
**refuses to write** unless the live schema matches a known-good fingerprint,
and the compat suite gains a "local-config schema drift" check.

---

## 6. Verdict

- **Reading + sharing the view: fully feasible (already done).**
- **Running GUI-authored plans from the TUI: works today** (read + `arqc`).
- **TUI authoring secret-free *edits*: feasible but experimental** (Phase 2),
  dominated by schema-drift maintenance risk (B5) and daemon coordination (B2).
- **TUI authoring complete plans with their secrets (true bidirectional
  parity): not achievable today** — blocked by the Keychain/derived-key secret
  layer (B3), the absence of any sanctioned config-write API (B6), and the
  unversioned root-owned daemon DB (B1/B2/B5).

**Recommendation:** pursue **Approach C (operator-mediated authoring)** for the
"shared config" UX now — it gives one converged config with Arq owning all
secret-bearing writes and zero corruption risk — and treat direct `server.db`
write-through (Phase 2) as an explicitly experimental, safeguarded opt-in.
Do **not** attempt Phase 3 until Arq exposes a config API or the secret layer
is demonstrably reproducible. Meanwhile the format/destination interop
(`docs/COMPATIBILITY.md`) already guarantees the data itself is fully shared
both ways.
