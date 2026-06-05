# INCIDENTS — arq-backup-tui

Catalog of notable bugs / incidents in the arq-backup-tui
validator + reader + writer. Concise record; full analysis lives
in the linked docs / CLs. (First entry 2026-05-27.)

| Date | ID | Component | Title | Severity | Fix |
|---|---|---|---|---|---|
| 2026-05-27 | A-01 | `arq_validator` audit-drip | One keyset applied to all backup sets → spurious whole-set HMAC failures on a multi-set destination | false-alarm (no data loss); surfaced as a CRIT in the consumer | CL 56797 (DESIGN §5) |

---

## A-01 — audit-drip applied one keyset to every backup set (2026-05-27)

**Where it surfaced**: the sibling consumer **docker-monitor**
runs a port of `arq_validator`'s audit-drip on the operator's
local Arq destination `/Volumes/arqbackup1` and alerts on the
result. It fired a recurring
`[CRIT] Arq audit-drip (local) crit: 5 fail` — a
backup-integrity CRIT implying corruption.

**Root cause (in `arq_validator/audit_drip.py`)**: a single
destination can host several independent Arq backup sets — each
its own computer-UUID with its own `encryptedkeyset.dat`, or an
*unencrypted* set with no keyset. `_decrypt_first_keyset`
decrypted the **first** keyset that opened and `run_audit_drip`
applied that single `hmac_key` to **every** pack across **all**
computer-UUIDs.

The operator's destination held three sets: `2DAC24D1`
(encrypted, password A), `78EF71EE` (encrypted, a *different*
password / plan), and `AC8FAB4D` (**unencrypted**). So
`78EF71EE`'s packs were HMAC-checked under `2DAC24D1`'s key
(every inner object fails — 23/23, 27/27, 26/26 from offset 0),
and `AC8FAB4D`'s unencrypted packs aren't ARQO-wrapped ("no ARQO
magic"). **Zero actual corruption**: control = `2DAC24D1`'s own
pack + its own key verifies 43/43; ~99.9% of objects passed;
the keyset decrypted fine. Only *which* keyset was applied was
wrong.

Notably the L2 `tiers.run_full_audit` tier **already** decrypted
the keyset per computer-UUID — audit-drip's
`_decrypt_first_keyset` was the lone single-keyset shortcut.

**Fix (CL 56797)**: `_decrypt_first_keyset` →
`_decrypt_keysets_per_cu`, returning a `{cu: Keyset}` map plus a
`{cu: reason}` skip map. `run_audit_drip` verifies each pack
with **its** set's keyset and **skips** (never HMAC-fails) sets
that are unencrypted (no keyset file) or whose keyset doesn't
open with the supplied password (a different-password set). The
fire only errors (`KEYSET_FAILED`) when *no* set decrypts.
Skipped sets + reasons are recorded in the new
`AuditDripState.last_fire_skipped_backup_sets` and reported once
per fire. DESIGN §5 documents the per-cu behavior. The consumer
docker-monitor re-ported the fix (its CL 54694 / §B.320) to keep
the two validators in lockstep.

**Lesson**: an auditor that assumes one key / password / config
for an entire storage location false-positives on a multi-tenant
store. Enumerate the tenants and resolve config **per-tenant**;
skip (don't fail) tenants you can't authenticate, and surface
the skip explicitly. FP-vs-corruption tells: *all* objects in a
unit fail (vs scattered), a control unit passes with the right
config, an independent copy is clean, and the bulk passes.

**Full consumer-side analysis** (evidence, control/reproduce,
validator comparison):
`docker-monitor/docs/INVESTIGATION-2026-05-26-arq-local-audit-hmac.md` +
`docker-monitor/docs/INVESTIGATION-2026-05-27-arq-validator-vs-sibling.md`,
incident catalog entry I-18.
