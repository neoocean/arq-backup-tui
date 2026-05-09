"""ACL capture + restore.

Two ACL families operators care about:

- **macOS NFSv4 ACLs** (Mac-side, on HFS+/APFS). Each entry is a
  user-or-group name + a permission set (``read``, ``write``,
  ``execute``, ``delete``, etc.) with an inheritance flag. Read
  with ``ls -le`` (each entry on its own line); write with
  ``chmod +a "..."``.
- **Linux POSIX ACLs** (filesystems mounted with the ``acl``
  option). Read with ``getfacl``, write with ``setfacl``.

Arq.app stores ACLs as a per-Node ``aclBlobLoc`` — a single
encrypted blob containing the textual ACL representation. We
capture the raw output of the platform CLI (``ls -le`` on macOS
/ ``getfacl`` on Linux) so the on-disk ACL bytes are
self-describing + restorable through the same CLIs.

Limitations:

- Both CLIs require the ``chmod`` / ``setfacl`` binary to be on
  PATH at restore time. Fail-soft when missing — emit
  ``acl_apply_skipped`` and continue.
- Per-attr failures don't abort the file; same policy as xattr
  apply.
- Cross-platform (macOS ACL → Linux dest, Linux ACL → macOS
  dest) won't translate. The captured bytes are platform-
  specific. Restore on the wrong platform emits
  ``acl_apply_skipped(reason="wrong-platform")``.
- We don't currently capture default-ACLs separately on Linux
  (POSIX has both "access" and "default" ACLs); a future
  refinement could split them.
"""

from __future__ import annotations

import platform
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, Optional


def has_acl_support() -> bool:
    """True iff the host has the platform's ACL CLI available."""
    sys_name = platform.system()
    if sys_name == "Darwin":
        return _has_cmd("chmod") and _has_cmd("ls")
    if sys_name == "Linux":
        return _has_cmd("getfacl") and _has_cmd("setfacl")
    return False


def _has_cmd(name: str) -> bool:
    from shutil import which
    return which(name) is not None


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------


def capture_acl(
    path,
    *,
    callback: Optional[Callable[..., None]] = None,
) -> bytes:
    """Read the ACL of ``path`` + return its on-disk bytes.

    Returns ``b""`` when the entry has no ACL OR the host
    doesn't have the CLI installed. The bytes are the raw output
    of the platform's ACL listing command — restore reads them
    back via :func:`apply_acl` which feeds them to the matching
    setter.

    Format header:
        b"ACL_MACOS_NFSV4\\n" or b"ACL_LINUX_POSIX\\n"
    followed by the platform CLI's textual output verbatim.
    Self-describing so a future reader can reject mismatched
    platforms cleanly.
    """
    sys_name = platform.system()
    if sys_name == "Darwin":
        return _capture_macos(path, callback=callback)
    if sys_name == "Linux":
        return _capture_linux(path, callback=callback)
    return b""


def _capture_macos(
    path, *, callback,
) -> bytes:
    """``ls -le <path>`` outputs the ACL block after the regular
    long listing. We extract just the ACL lines (those starting
    with a digit + colon)."""
    if not _has_cmd("ls"):
        return b""
    try:
        cp = subprocess.run(
            ["ls", "-led", str(path)],
            capture_output=True, timeout=5, text=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        if callback is not None:
            try:
                callback("acl_capture_error",
                         {"path": str(path), "error": str(exc)})
            except Exception:
                pass
        return b""
    if cp.returncode != 0:
        return b""
    # ACL lines look like "0: user:foo allow ..." (numeric
    # index + colon). Skip the regular `ls -l` summary line.
    acl_lines = [
        ln for ln in cp.stdout.splitlines()
        if ln and ln[0].isdigit() and ":" in ln[:5]
    ]
    if not acl_lines:
        return b""
    body = "\n".join(acl_lines).encode("utf-8")
    return b"ACL_MACOS_NFSV4\n" + body


def _capture_linux(
    path, *, callback,
) -> bytes:
    """``getfacl --omit-header <path>`` outputs one ACL entry per
    line. Skip lines that just echo the default mode bits (those
    are already covered by mac_st_mode)."""
    if not _has_cmd("getfacl"):
        return b""
    try:
        cp = subprocess.run(
            ["getfacl", "--omit-header", str(path)],
            capture_output=True, timeout=5, text=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        if callback is not None:
            try:
                callback("acl_capture_error",
                         {"path": str(path), "error": str(exc)})
            except Exception:
                pass
        return b""
    if cp.returncode != 0:
        return b""
    # When the file has no extended ACL, getfacl emits only the
    # base owner/group/other lines — no point storing those, the
    # mac_st_mode field already covers them.
    lines = [
        ln for ln in cp.stdout.splitlines()
        if ln and not ln.startswith("#")
    ]
    extended = [
        ln for ln in lines
        if ":" in ln and not ln.startswith(("user::", "group::", "other::"))
    ]
    if not extended:
        return b""
    body = "\n".join(lines).encode("utf-8")
    return b"ACL_LINUX_POSIX\n" + body


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


def apply_acl(
    path,
    blob: bytes,
    *,
    callback: Optional[Callable[..., None]] = None,
) -> bool:
    """Apply an ACL blob captured by :func:`capture_acl`.

    Returns True iff the apply actually ran (False = skipped:
    wrong platform, missing CLI, or empty blob). Per-platform
    apply errors surface via ``callback("acl_apply_error", …)``.
    """
    if not blob:
        return False
    sys_name = platform.system()
    if blob.startswith(b"ACL_MACOS_NFSV4\n"):
        if sys_name != "Darwin":
            if callback:
                callback("acl_apply_skipped",
                         {"path": str(path),
                          "reason": "wrong-platform",
                          "blob_kind": "macos"})
            return False
        return _apply_macos(
            path, blob[len(b"ACL_MACOS_NFSV4\n"):],
            callback=callback,
        )
    if blob.startswith(b"ACL_LINUX_POSIX\n"):
        if sys_name != "Linux":
            if callback:
                callback("acl_apply_skipped",
                         {"path": str(path),
                          "reason": "wrong-platform",
                          "blob_kind": "linux"})
            return False
        return _apply_linux(
            path, blob[len(b"ACL_LINUX_POSIX\n"):],
            callback=callback,
        )
    if callback:
        callback("acl_apply_error",
                 {"path": str(path),
                  "error": f"unknown acl blob magic: {blob[:32]!r}"})
    return False


def _apply_macos(
    path, body: bytes, *, callback,
) -> bool:
    """Apply each entry via ``chmod +a ...``. macOS NFSv4 ACLs
    accept one ``+a`` per entry; we feed each ACL line back as a
    fresh chmod invocation."""
    if not _has_cmd("chmod"):
        if callback:
            callback("acl_apply_skipped",
                     {"path": str(path),
                      "reason": "no-chmod-cli"})
        return False
    text = body.decode("utf-8", "replace")
    applied_any = False
    for line in text.splitlines():
        # Lines look like "0: user:foo allow read"; strip the
        # numeric prefix + colon, the rest is the ACL entry
        # chmod expects after +a.
        idx = line.find(":")
        if idx <= 0:
            continue
        entry = line[idx + 1:].strip()
        if not entry:
            continue
        try:
            subprocess.run(
                ["chmod", "+a", entry, str(path)],
                check=True, capture_output=True, timeout=5,
            )
            applied_any = True
        except subprocess.CalledProcessError as exc:
            if callback:
                callback("acl_apply_error",
                         {"path": str(path), "entry": entry,
                          "error": exc.stderr.decode(
                              "utf-8", "replace",
                          ) if exc.stderr else str(exc)})
    return applied_any


def _apply_linux(
    path, body: bytes, *, callback,
) -> bool:
    """Apply via ``setfacl --modify-file=- <path>`` reading the
    captured getfacl output from stdin. setfacl accepts the same
    format getfacl emits."""
    if not _has_cmd("setfacl"):
        if callback:
            callback("acl_apply_skipped",
                     {"path": str(path),
                      "reason": "no-setfacl-cli"})
        return False
    try:
        subprocess.run(
            ["setfacl", "--modify-file=-", str(path)],
            input=body, check=True,
            capture_output=True, timeout=5,
        )
        return True
    except subprocess.CalledProcessError as exc:
        if callback:
            callback("acl_apply_error",
                     {"path": str(path),
                      "error": exc.stderr.decode(
                          "utf-8", "replace",
                      ) if exc.stderr else str(exc)})
        return False
