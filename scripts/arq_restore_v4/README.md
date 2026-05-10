# Tree v4 verification via patched `arq_restore` (BSD reference)

This directory packages an **autonomous, GUI-free** way to byte-verify
Tree v4 emit + read against the canonical Arq BSD reference
implementation.

## Background

The public `arq_restore` source
(<https://github.com/arqbackup/arq_restore>) predates Arq.app v8's
Tree v4 emit. Its `Arq7Node::initWithBufferedInputStream:` has a
`theTreeVersion >= 2` branch (reparse fields) but no `>= 4` branch
for the 38-byte trailing block Arq.app 7.40+ adds per Node. As a
result, the unpatched binary fails to walk any v4 BackupRecord —
`missing blob identifier` cascading from a 38-byte misalignment in
the input stream.

Strategy K (`docs/COMPAT-VERIFICATION.md` §5.7) reverse-engineered
the trailing block's structure on 21,519 real nodes from
`/Volumes/arqbackup1`. The fields are entirely backup-engine state
(scanned-at timestamp + present-flag + reserved zeros) — none of
them are needed to **consume** the file content. So a 3-line patch
to advance the input stream past those 38 bytes is sufficient to
unlock `arq_restore` for v4.

This unlocks **Strategy C extended to Tree v4**: writer → patched
`arq_restore` → `diff -r` source. And **Strategy I-alt**: a
GUI-free byte-equivalence test of our reader (any v4 record on a
real destination, restored two ways, diff'd) — the closest
substitute we have for Strategy I (full Arq.app GUI restore + diff)
without driving the GUI.

See `docs/COMPAT-VERIFICATION.md` §5.8 for the full workflow log.

## Files

- `0001-arq7-node-read-v4-trailing-block.patch` — the 3-line +
  16-line-comment patch that adds the `theTreeVersion >= 4` branch
  to `Arq7Node.m`.
- `build.sh` — clones `arqbackup/arq_restore`, applies the patch
  (idempotent), and builds with clang + OpenSSL. Xcode CLT is
  sufficient (no full Xcode required).
- `verify.py` — restores a chosen relative path from a chosen v4
  BackupRecord through both the patched binary and this project's
  Python reader, then diffs them. Exit code 0 + non-zero file count
  = Tree v4 byte-equivalence proven by two independent
  implementations.

## Workflow

```bash
# 1. Build patched arq_restore.
./scripts/arq_restore_v4/build.sh
# Produces /private/tmp/strategy-c/arq_restore.bin.v4

# 2. Pick a v4 BackupRecord on the destination. Quick way:
python3 - <<'PY'
import glob, json, sys
sys.path.insert(0, '.')
from pathlib import Path
from arq_validator.crypto import decrypt_keyset
from arq_reader.decrypt import decrypt_lz4_arqo
pw = Path('.secrets/dest_password').read_text().strip()
root = '/Volumes/arqbackup1'
for rp in sorted(glob.glob(f'{root}/*/backupfolders/*/backuprecords/*/*.backuprecord')):
    cu = rp.split('/')[3]
    ks = decrypt_keyset(open(f'{root}/{cu}/encryptedkeyset.dat','rb').read(), pw)
    try:
        rec = json.loads(decrypt_lz4_arqo(open(rp,'rb').read(), ks.encryption_key, ks.hmac_key))
    except Exception:
        continue
    if rec.get('arqVersion','').startswith('7.40'):
        parts = rp.split('/')
        print(f'--computer-uuid {cu}')
        print(f'--folder-uuid   {parts[5]}')
        print(f'--record-name   {parts[-1]}')
        break
PY

# 3. Run verify.
python3 scripts/arq_restore_v4/verify.py \
    --destination /Volumes/arqbackup1 \
    --password-file .secrets/dest_password \
    --arq-restore-bin /private/tmp/strategy-c/arq_restore.bin.v4 \
    --computer-uuid <CU> \
    --folder-uuid <FU> \
    --record-name <RECORD>.backuprecord \
    --relative-path /data/some/path \
    --work-dir /tmp/arq-v4-verify
```

## Verification log

Initial validation 2026-05-11 against `/Volumes/arqbackup1`:

```
Source:        /Volumes/arqbackup1 (Arq.app 7.40.1, Tree v4)
Record:        7647180.backuprecord
Path:          /data/assets/tlmn8nr3reekl00mrzp8ntpc/449826f2-c0aa-46eb-845b-6b41c5dc7720/metadata.json

arq_restore (patched):   metadata.json  27 B  SHA-256 836d76c8...2af9a812
Python reader:           metadata.json  27 B  SHA-256 836d76c8...2af9a812

>>> BYTE-IDENTICAL <<<
```
