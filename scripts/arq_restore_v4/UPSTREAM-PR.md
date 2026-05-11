# Upstreaming the Tree v4 patch to `arqbackup/arq_restore`

This file packages everything needed to **submit** the Tree v4
trailing-block patch in `0001-arq7-node-read-v4-trailing-block.patch`
to the canonical [arqbackup/arq_restore](https://github.com/arqbackup/arq_restore)
repository — a clean fork branch, a polished PR title + body, and
the verification artefacts to point reviewers at.

The submission itself is the **operator's call** (Stefan Reitshamer
maintains `arqbackup/arq_restore` directly; PR cadence is on his
schedule and at his discretion). Nothing here automates the actual
GitHub PR open — every step is a copy-paste the operator runs from
their own GitHub account.

## TL;DR

```bash
# 1. Fork on GitHub UI: https://github.com/arqbackup/arq_restore → Fork
# 2. Locally:
git clone git@github.com:<your-handle>/arq_restore.git
cd arq_restore
git checkout -b tree-v4-trailing-block

# 3. Apply the patch from this repo
git apply /path/to/arq-backup-tui/scripts/arq_restore_v4/0001-arq7-node-read-v4-trailing-block.patch

# 4. Build + smoke-test locally
../arq-backup-tui/scripts/arq_restore_v4/build.sh

# 5. Push + open PR via the GitHub UI
git push -u origin tree-v4-trailing-block
# UI: New pull request → use the title + body below
```

## Suggested PR title

```
Arq7Node: consume the 38-byte Tree v4 trailing block
```

## Suggested PR body

Copy-paste the block below into the GitHub PR description. All
external references are public and stable.

---

> Tree v4 (Arq.app 7.40+) appends a 38-byte trailing block to every
> Node. The current `arq7restore/Arq7Node.m::initWithBufferedInputStream:`
> handles `theTreeVersion >= 2` (reparse fields) but has no branch
> for `>= 4`, so consuming a v4 Node stops 38 bytes short of the
> next Node's start. From that point the parse cascade reads the
> tail of one Node as the head of the next, and the restore fails
> with `missing blob identifier`.
>
> The fix is to read those 38 bytes and discard them — `arq_restore`
> doesn't need any of their contents to produce the file output.
>
> ### What the 38 bytes hold
>
> | Offset | Type | Field |
> |---|---|---|
> | 0..7 | int64 BE | `scanned_at_sec` — backup-engine walk timestamp |
> | 8..15 | int64 BE | `scanned_at_nsec` |
> | 16..23 | int64 BE | present-flag (`0x0000000001000000`) |
> | 24..37 | 14 zero bytes | reserved |
>
> The fields are **backup-engine state**, not file metadata —
> `scanned_at` is the moment the engine walked this entry, not any
> stat-defined timestamp on the file itself.
>
> Decoded against 21,519 real Tree v4 nodes from a production Arq.app
> v8 destination. Reverse-engineering notes + the supporting
> statistical analysis are public at
> https://github.com/neoocean/arq-backup-tui/blob/main/docs/COMPAT-VERIFICATION.md#strategy-k
> (Strategy K).
>
> ### Verification
>
> With this patch applied, `arq_restore listtree` walks v4 trees
> cleanly and `arq_restore restore` produces files whose SHA-256
> matches what an independent Python reader produces from the same
> v4 BackupRecord. Tested against an Arq.app v8 destination
> (arqVersion 7.40.1 / 7.40.2, 18 v4 records, 21,519 v4 nodes).
> Example transcript (real record on the verifier's destination):
>
> ```
> Record: 7647180.backuprecord
> Path:   /data/assets/.../metadata.json
>
> arq_restore (patched):  27 B  SHA-256 836d76c8...2af9a812
> Independent reader:     27 B  SHA-256 836d76c8...2af9a812
> >>> BYTE-IDENTICAL <<<
> ```
>
> The full GUI-free verification harness lives at
> https://github.com/neoocean/arq-backup-tui/tree/main/scripts/arq_restore_v4
> (`build.sh` + `verify.py`).
>
> ### Compatibility
>
> - Tree v0–v3 behaviour unchanged (the new branch is gated on
>   `theTreeVersion >= 4`).
> - Trailing block is consumed but not parsed — no risk of mis-
>   interpreting future Arq.app changes to the field layout.

---

## Submission checklist

- [ ] PR title set to "Arq7Node: consume the 38-byte Tree v4 trailing block"
- [ ] PR body pasted (block above)
- [ ] Patch applies cleanly to current `arqbackup/arq_restore` HEAD
- [ ] `build.sh` succeeds on Xcode CLT (no full-Xcode required)
- [ ] `verify.py` against a real v4 destination prints `BYTE-IDENTICAL`
- [ ] (Optional) Cross-reference any related issue on the upstream
      repo — search for "tree v4" / "Arq 7.40" / "missing blob
      identifier" before opening.

## Risk + scope

The patch is **3 lines of code** + 13 lines of inline documentation.
It only adds a new `theTreeVersion >= 4` branch; no existing branch
is modified. The `unsigned char v4_trailing[38]` buffer is read and
immediately discarded — there is no parsing or interpretation of the
bytes, so the patch is robust against any future change to the
trailing-block layout (it would still advance the stream by exactly
38 bytes, which is the only contract that matters for the next
Node's offset).

Compatibility surface:

| Tree version | Before patch | After patch |
|---|---|---|
| v0 (Arq 5) | works | works (unchanged) |
| v1 (Arq 5) | works | works (unchanged) |
| v2 (Arq 7 pre-trailing-block) | works | works (unchanged) |
| v3 (Arq 7 pre-trailing-block) | works | works (unchanged) |
| v4 (Arq 7.40+) | fails | works |

## If the PR is declined or sits idle

This patch's value is GUI-free byte verification of Tree v4 emit
against the BSD reference implementation. If upstream prefers a
different approach (e.g. a dedicated v4 parser, or wiring the
scanned-at timestamp into the existing Arq7Node fields), the
verification value is preserved by maintaining the fork — see
`scripts/arq_restore_v4/build.sh` which builds from a local-clone
fallback when upstream-clone fails. The internal use case (Strategy
I-alt in `docs/COMPAT-VERIFICATION.md` §5.8) doesn't require the
patch to be merged upstream; it only requires the binary to exist.

## What this does NOT do

- It does not validate the trailing-block timestamp or present-flag
  values. If `arq_restore` ever grows a "reject untrusted record"
  mode that scrutinises the scanned-at timestamp, that's a separate
  upstream conversation.
- It does not write Tree v4. `arq_restore` is a restore-only tool;
  emitting v4 is the responsibility of Arq.app itself (or, in our
  case, the writer in `arq_writer/`).
- It does not change how data blobs, xattr blobs, or ACL blobs are
  fetched — those code paths are version-agnostic in the existing
  source.
