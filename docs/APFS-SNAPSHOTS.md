# macOS APFS Snapshot-Based Backup

## 1. Motivation

If you walk a live macOS source tree and back it up directly, file contents
can change while the walk is in progress — if the user saves a document
while it is being hashed, the result is a chunk that is neither the
pre-save nor the post-save state. Apple's recommended solution is to
first create an **APFS snapshot** and walk its read-only view, and **Time
Machine itself works exactly that way**. Arq.app uses the same pattern.

This project optionally provides the same protection on macOS.

## 2. Options Considered

### 2.1 macOS Options

| Mechanism | Tool | Notes |
|---------|------|---------|
| **Time Machine local snapshot** | `tmutil localsnapshot` | Name is `com.apple.TimeMachine.YYYY-MM-DD-HHMMSS.local`; auto-deleted after 24 hours; anyone can create one (sudo required) |
| **APFS native snapshot** | `fs_snapshot_create` syscall | Generally requires SIP exemption or an entitlement, making it hard to use from ordinary tools |
| **`mount_apfs -s <name>`** | mount tool | Any snapshot can be mounted read-only; sudo required |
| **`tmutil deletelocalsnapshots`** | tmutil | Deletes a TM snapshot; sudo required |

**Choice**: Time Machine local snapshot + mount_apfs.
- No unprivileged entitlement required
- A pattern officially supported by Apple
- Presumed to be the same approach Arq.app uses

### 2.2 Other OSes

- **Linux ext4**: no native snapshot. Workaround via LVM or btrfs.
- **Linux btrfs**: `btrfs subvolume snapshot` is possible but should be
  treated separately from this project's macOS-first stance (out of scope
  for this PR).
- **Windows NTFS**: VSS (Volume Shadow Copy) is possible; separate PR.

This PR supports **macOS only**; on other OSes it falls back to a live tree
walk (the option is ignored).

## 3. Implementation Overview

New module: `arq_writer.macos_snapshot`

### 3.1 Function Catalog

```python
from arq_writer import (
    is_macos, is_macos_apfs,
    create_snapshot, list_snapshots, delete_snapshot,
    mount_snapshot, unmount_snapshot,
    with_apfs_snapshot,
    SnapshotInfo, SnapshotError, NotMacOSError,
)
```

| Function | Privileges required | Behavior |
|------|----------|------|
| `is_macos()` | None | bool |
| `is_macos_apfs(path)` | None | bool (calls diskutil info -plist) |
| `create_snapshot()` | sudo (tmutil) | sudo tmutil localsnapshot → new SnapshotInfo |
| `list_snapshots(volume="/")` | None | tmutil listlocalsnapshots → List[SnapshotInfo] |
| `delete_snapshot(snap)` | sudo | sudo tmutil deletelocalsnapshots <date-stamp> |
| `mount_snapshot(snap, mount_point)` | sudo | sudo mount_apfs -s … -o ro,nobrowse |
| `unmount_snapshot(mount_point)` | sudo | sudo umount; already-unmounted is OK |
| `with_apfs_snapshot(source)` | sudo | Context manager: create + mount + cleanup |

### 3.2 build_backup Integration

An option is added to `build_backup`:

```python
build_backup(
    source, dest, password,
    use_apfs_snapshot=True,    # option
)
```

Flow:

1. If macOS + APFS, run `tmutil localsnapshot`
2. Mount at a temporary mount point with `mount_apfs -s … -o ro,nobrowse`
3. Translate the original source path to a mount-relative path
4. Walk and back up that frozen view
5. Cleanup (umount; the snapshot is preserved by default — TM relies on it)

If non-macOS:
- Emit an `apfs_snapshot_skipped` callback event
- Fall back to the live source (no failure)

### 3.3 Privilege Policy

`tmutil localsnapshot` / `tmutil deletelocalsnapshots` /
`mount_apfs` all require root. The library **invokes** `sudo`,
but the password prompt is handled by the OS's sudo.

When using cron / launchd automation, it is recommended that the
operator configure the following in `sudoers` as NOPASSWD:

```
yourusername ALL=(root) NOPASSWD: /usr/bin/tmutil localsnapshot
yourusername ALL=(root) NOPASSWD: /usr/bin/tmutil deletelocalsnapshots *
yourusername ALL=(root) NOPASSWD: /sbin/mount_apfs *
yourusername ALL=(root) NOPASSWD: /sbin/umount *
```

## 4. Guarantees + Limitations

### 4.1 Guarantees

- **Consistency at walk start**: every read after the snapshot is mounted is at the same timestamp
- **Chunker stability**: backing up the same source twice with the same chunker produces identical chunks (live walks may differ subtly because of mid-walk user saves)
- **mtime accuracy**: stat on the snapshot is accurate; live walks can change between stat and read

### 4.2 Limitations

- **macOS only**: Linux btrfs/LVM, Windows VSS are separate PRs
- **Boot volume scope**: a snapshot covers one entire APFS volume. When backing up multiple volumes in parallel, snapshot each one
- **sudo prompt**: without NOPASSWD configuration, a prompt occurs on every backup
- **Cannot verify in sandbox**: without macOS, mount_apfs integration tests are not possible; the sandbox has only mock-based unit tests

### 4.3 Safety

- The snapshot itself takes almost no disk space (CoW based). It is auto-deleted when the disk fills up.
- Cleanup of the snapshot mount is guaranteed by the `with_apfs_snapshot` context manager (it attempts unmount even on exception)
- The snapshot is **not deleted by default** — Time Machine may use that snapshot itself.

## 5. Operator Verification Procedure (Things That Cannot Be Tested in Sandbox)

What can be verified in the sandbox:
- ✅ Parsing tmutil output (mocked)
- ✅ mount_apfs argument sequence (mocked)
- ✅ Snapshot path translation (`/Users/me/foo` → `<mount>/Users/me/foo`)
- ✅ Linux fallback behavior (`apfs_snapshot_skipped` event)

What **cannot be verified** in the sandbox (a macOS operator pastes):

### 5.1 Real Mount + Walk Verification

Operator on macOS:

```bash
# 1) Known fixture
mkdir -p /tmp/snap-test
echo hello > /tmp/snap-test/a.txt

# 2) Backup (snapshot mode)
arq-backup create /tmp/snap-test \
    --dest /tmp/dest-snap \
    --password test \
    --use-apfs-snapshot

# 3) Verify — extract result fingerprint
arq-fingerprint compute /tmp/dest-snap \
    --password test \
    --out /tmp/fp-snap.json

# 4) Same source in live mode
arq-backup create /tmp/snap-test \
    --dest /tmp/dest-live \
    --password test
arq-fingerprint compute /tmp/dest-live \
    --password test \
    --out /tmp/fp-live.json

# 5) Compare — should be the same (since file contents are identical)
arq-fingerprint compare /tmp/fp-snap.json /tmp/fp-live.json
```

If `arq-fingerprint compare` outputs `match: true`, the snapshot path
is working correctly.

### 5.2 Mid-Walk Mutation Stress

Operator:

```bash
# 1) Big fixture
mkdir -p /tmp/big-test
for i in {1..1000}; do head -c 10240 </dev/urandom > /tmp/big-test/f$i.bin; done

# 2) Concurrently:
#    - Terminal A: arq-backup create /tmp/big-test --dest /tmp/dest --use-apfs-snapshot
#    - Terminal B: while true; do echo extra >> /tmp/big-test/f1.bin; done

# 3) When backup finishes, restore
Restore the destination produced by arq-backup
diff /tmp/big-test/f1.bin /restored/f1.bin   # Should be some single point in time
```

In snapshot mode, `f1.bin` should be reconstructed from the version at the
moment the backup started, so the diff should be empty (or correspond to
some single point in time during the backup run) — there should be no torn
write.

In live mode, the same stress test can produce an invalid result for
`f1.bin`, partly old version + partly new version mixed together.

## 6. Future Work

This PR covers macOS only. Extending the same pattern to other OSes:

| OS | Mechanism | Effort |
|----|---------|------|
| Linux btrfs | `btrfs subvolume snapshot` | Small (~50 LOC) |
| Linux LVM | `lvm lvcreate --snapshot` | Small |
| Linux ext4 (no snapshot) | unsupported; fallback only | N/A |
| Windows VSS | `wmic shadowcopy create` or PowerShell | Medium (~150 LOC) |

Each can be added in a separate PR. For now, macOS is the only priority.

## 7. CLI Exposure

This PR exposes the Python API `build_backup(use_apfs_snapshot=True)` along
with the option argument. Adding the `--use-apfs-snapshot` flag to the
CLI (`arq-backup`) is safer in a separate follow-up PR (because the CLI is
already a stable surface, so we avoid bundling too much into one PR).
