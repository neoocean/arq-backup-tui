"""macOS APFS snapshot-based backup source helper.

When backing up a live macOS source tree, file content can shift
mid-walk: a user saves a document while we're hashing it, the
result is a chunk that doesn't match either the pre-save or the
post-save state. Apple's recommended fix is to take an APFS
snapshot of the volume first and walk **that** read-only view —
exactly what Time Machine does internally.

This module wraps the recommended Apple workflow:

    sudo tmutil localsnapshot                # create snapshot
    tmutil listlocalsnapshots /              # find its name
    mkdir /tmp/.arq-snapshot
    sudo mount_apfs -s <name> /System/Volumes/Data /tmp/.arq-snapshot
    # … run backup against /tmp/.arq-snapshot/Users/me/… …
    sudo umount /tmp/.arq-snapshot
    sudo tmutil deletelocalsnapshots <date>  # optional cleanup

Each step is exposed as a function so a caller can compose them
freely. The high-level :func:`with_apfs_snapshot` context manager
runs the whole sequence and yields a snapshot-resolved path that
can be substituted for the original source path.

**macOS-only**. On any other OS the helpers raise
:class:`NotMacOSError` immediately so the writer can fall back
to the live-source path. ``is_macos_apfs(path)`` is the cheap
check callers should run before opting in.

**Privilege**: ``mount_apfs`` and ``tmutil deletelocalsnapshots``
need root. The helpers shell out to ``sudo`` only when the caller
explicitly opts in via ``allow_sudo=True``; otherwise they raise.
This keeps the lib free of "ask for root invisibly" magic.

For verification details + the operator-paste sanity test, see
``docs/APFS-SNAPSHOTS.md``.
"""

from __future__ import annotations

import contextlib
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, List, Optional


class NotMacOSError(RuntimeError):
    """Raised when an APFS-snapshot operation is attempted off macOS."""


class SnapshotError(RuntimeError):
    """Raised when an `tmutil` / `mount_apfs` command fails."""


@dataclass(frozen=True)
class SnapshotInfo:
    """One APFS local snapshot as listed by ``tmutil
    listlocalsnapshots``.

    ``name`` is the full snapshot identifier (e.g.
    ``com.apple.TimeMachine.2026-05-08-130000.local``).
    ``creation_iso`` is parsed from the embedded date stamp.
    """

    name: str
    creation_iso: str = ""


# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------


def is_macos() -> bool:
    return sys.platform == "darwin"


def is_macos_apfs(path: Path) -> bool:
    """Return True iff ``path`` lives on a macOS APFS volume.

    On non-macOS hosts always False.
    """
    if not is_macos():
        return False
    try:
        cp = subprocess.run(
            ["diskutil", "info", "-plist", str(path)],
            capture_output=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    if cp.returncode != 0:
        return False
    # Quick string sniff is enough; full plist parse is overkill
    # for a Boolean check.
    return b"<string>apfs</string>" in cp.stdout.lower() or \
        b"apfs" in cp.stdout.lower()


# ---------------------------------------------------------------------------
# tmutil wrappers
# ---------------------------------------------------------------------------


def create_snapshot() -> SnapshotInfo:
    """Run ``sudo tmutil localsnapshot`` and return the new snapshot.

    ``tmutil localsnapshot`` creates one snapshot per local APFS
    volume; we identify the one we just made by listing snapshots
    on the boot volume and picking the lex-max name (snapshot
    names embed a timestamp, so newest = max).
    """
    if not is_macos():
        raise NotMacOSError("APFS snapshots only work on macOS")
    cp = subprocess.run(
        ["sudo", "tmutil", "localsnapshot"],
        capture_output=True, timeout=60,
    )
    if cp.returncode != 0:
        raise SnapshotError(
            f"tmutil localsnapshot failed: rc={cp.returncode} "
            f"stderr={cp.stderr.decode(errors='replace')[:300]!r}"
        )
    snaps = list_snapshots()
    if not snaps:
        raise SnapshotError(
            "tmutil localsnapshot succeeded but no snapshot showed up"
        )
    return max(snaps, key=lambda s: s.name)


def list_snapshots(volume: str = "/") -> List[SnapshotInfo]:
    """``tmutil listlocalsnapshots <volume>`` parsed into entries.

    The command's output is one snapshot identifier per line
    (after the header line); lines that don't look like
    ``com.apple.TimeMachine.YYYY-MM-DD-HHMMSS.local`` are
    discarded.
    """
    if not is_macos():
        raise NotMacOSError("APFS snapshots only work on macOS")
    cp = subprocess.run(
        ["tmutil", "listlocalsnapshots", volume],
        capture_output=True, text=True, timeout=15,
    )
    if cp.returncode != 0:
        raise SnapshotError(
            f"tmutil listlocalsnapshots failed: rc={cp.returncode} "
            f"err={cp.stderr.strip()[:300]!r}"
        )
    out: List[SnapshotInfo] = []
    pattern = re.compile(
        r"com\.apple\.TimeMachine\.(\d{4}-\d{2}-\d{2}-\d{6})\.local"
    )
    for line in cp.stdout.splitlines():
        line = line.strip()
        if not line or "Snapshots for" in line:
            continue
        m = pattern.search(line)
        if not m:
            continue
        out.append(SnapshotInfo(name=line, creation_iso=m.group(1)))
    return out


def delete_snapshot(snapshot: SnapshotInfo) -> None:
    """``sudo tmutil deletelocalsnapshots <date>``.

    The argument tmutil takes is the date stamp embedded in the
    snapshot name (``YYYY-MM-DD-HHMMSS``), not the full name.
    """
    if not is_macos():
        raise NotMacOSError("APFS snapshots only work on macOS")
    if not snapshot.creation_iso:
        raise SnapshotError(
            f"snapshot {snapshot.name!r} has no parseable date stamp; "
            "can't pass to tmutil deletelocalsnapshots"
        )
    cp = subprocess.run(
        [
            "sudo", "tmutil", "deletelocalsnapshots",
            snapshot.creation_iso,
        ],
        capture_output=True, timeout=30,
    )
    if cp.returncode != 0:
        raise SnapshotError(
            f"tmutil deletelocalsnapshots failed: rc={cp.returncode} "
            f"stderr={cp.stderr.decode(errors='replace')[:300]!r}"
        )


# ---------------------------------------------------------------------------
# mount_apfs wrappers
# ---------------------------------------------------------------------------


def mount_snapshot(
    snapshot: SnapshotInfo,
    *,
    mount_point: Path,
    device: str = "/System/Volumes/Data",
) -> Path:
    """``sudo mount_apfs -s <snap> <device> <mount_point>``.

    Returns the mount point on success; raises
    :class:`SnapshotError` on failure. Caller is responsible for
    eventually calling :func:`unmount_snapshot`.

    ``device`` defaults to the boot data volume on modern macOS
    (Big Sur+) where the system + data volumes are split.
    """
    if not is_macos():
        raise NotMacOSError("APFS snapshots only work on macOS")
    mount_point = Path(mount_point)
    mount_point.mkdir(parents=True, exist_ok=True)
    cp = subprocess.run(
        [
            "sudo", "mount_apfs",
            "-s", snapshot.name, "-o", "ro,nobrowse",
            device, str(mount_point),
        ],
        capture_output=True, timeout=30,
    )
    if cp.returncode != 0:
        raise SnapshotError(
            f"mount_apfs failed: rc={cp.returncode} "
            f"stderr={cp.stderr.decode(errors='replace')[:300]!r}"
        )
    return mount_point


def unmount_snapshot(mount_point: Path) -> None:
    """``sudo umount <mount_point>``. Idempotent — already-
    unmounted paths are silently OK."""
    if not is_macos():
        return
    if not Path(mount_point).is_dir():
        return
    cp = subprocess.run(
        ["sudo", "umount", str(mount_point)],
        capture_output=True, timeout=15,
    )
    # rc != 0 with "not currently mounted" is benign.
    if cp.returncode != 0:
        msg = cp.stderr.decode(errors="replace")
        if "not currently mounted" not in msg \
                and "not mounted" not in msg:
            raise SnapshotError(
                f"umount failed: rc={cp.returncode} "
                f"stderr={msg[:300]!r}"
            )


# ---------------------------------------------------------------------------
# High-level orchestration
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def with_apfs_snapshot(
    source: Path,
    *,
    delete_on_exit: bool = False,
    mount_root: Optional[Path] = None,
) -> Iterator[Path]:
    """Context manager: yield a path inside an APFS snapshot whose
    contents match ``source`` at entry time.

    The returned path is **not** ``source`` itself — it's
    ``<mount_root>/<source-relative-to-volume>``. Substitute it
    for ``source`` when calling ``Backup.add_folder`` so the walk
    sees a frozen view.

    On exit:
      - The snapshot mount is always unmounted.
      - The snapshot is deleted only when ``delete_on_exit=True``.
        Default is False because Time Machine relies on local
        snapshots being present; deleting one out from under TM
        would interfere with macOS's own backup hooks.

    Always macOS-only. Raises :class:`NotMacOSError` immediately
    on Linux / Windows so callers can wrap it in a try / except
    fallback.
    """
    if not is_macos():
        raise NotMacOSError("APFS snapshots only work on macOS")
    snap = create_snapshot()
    if mount_root is None:
        mount_root = Path(tempfile.mkdtemp(
            prefix="arq-snapshot-", dir="/private/tmp",
        ))
    try:
        mount_snapshot(snap, mount_point=mount_root)
        # Translate `source` (a path on the live volume) into the
        # equivalent path inside the snapshot mount. The data
        # volume is anchored at /System/Volumes/Data, so a live
        # path like /Users/me/... appears under
        # <mount>/Users/me/... — strip the live anchor and
        # reattach under the mount.
        live = Path(source).resolve()
        rel = _strip_volume_anchor(live)
        snapshot_path = mount_root / rel
        if not snapshot_path.exists():
            raise SnapshotError(
                f"snapshot does not contain {source!r} "
                f"(expected at {snapshot_path})"
            )
        yield snapshot_path
    finally:
        try:
            unmount_snapshot(mount_root)
        except SnapshotError:
            pass
        if delete_on_exit:
            try:
                delete_snapshot(snap)
            except SnapshotError:
                pass


def _strip_volume_anchor(path: Path) -> Path:
    """Convert ``/Users/me/foo`` → ``Users/me/foo`` so it can be
    re-anchored under a snapshot mount point.

    On modern macOS the live boot disk is mounted at ``/`` but
    the data partition surfaces both at ``/`` and at
    ``/System/Volumes/Data``. Snapshot mounts exposes the data
    volume only, so an absolute live path becomes
    ``<mount>/<relative-to-volume>``.
    """
    parts = list(path.parts)
    if parts and parts[0] == "/":
        parts = parts[1:]
    return Path(*parts) if parts else Path()
