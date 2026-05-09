"""Extended-attribute capture + restore.

On macOS / Linux every filesystem entry can carry a set of named
binary attributes (``com.apple.FinderInfo``, ``user.foo``, etc.).
Arq.app preserves them; until this module landed our writer
captured zero xattrs and our restore wrote zero xattrs, leaving
files visibly different post-restore (Finder labels, Safari
quarantine, custom user.* metadata all wiped).

Format choice — single blob per Node containing every xattr
name+data. The Arq schema declares ``xattrsBlobLocs`` as a list,
but in practice one consolidated blob is far simpler than one
BlobLoc per xattr (no per-attr ARQO + HMAC overhead, no awkward
"how do you know which BlobLoc maps to which name" question
since the schema doesn't store names alongside the locs). The
blob payload is a binary plist ``{name_str: bytes_value, …}`` so
it round-trips through Apple-compatible tooling and is
self-describing for forward-compat.

Compatibility note: this is OUR writer/reader contract. Arq.app
may use a different per-attr blob scheme; cross-vendor xattr
interop is a separate Mach-O RE follow-up. The validator's
existing xattr-presence checks remain neutral (count + per-blob
HMAC, not payload shape).

Cross-platform behaviour:
- macOS: ``os.listxattr`` / ``os.getxattr`` / ``os.setxattr``
  honour the ``follow_symlinks`` arg the standard library exposes;
  we always capture xattrs of the entry itself, not of its target.
- Linux: same APIs work; xattr namespace is ``user.*`` /
  ``trusted.*`` etc. Capture all that the kernel exposes to the
  caller.
- Windows + any host whose stdlib lacks the xattr APIs: capture
  silently returns an empty dict, restore silently no-ops.

Errors are best-effort: a single failed ``getxattr`` (e.g.
permission denied on a ``trusted.*`` namespace running unprivileged)
emits ``xattr_capture_error`` on the progress callback and skips
just that one attribute, never aborts the file.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import platform
import plistlib
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple


# Two paths to xattr APIs. The Python stdlib exposes ``os.listxattr``
# / ``os.getxattr`` / ``os.setxattr`` only on Linux — its macOS
# build doesn't include them despite macOS having the C symbols.
# To support macOS (the operator's primary host) we bind libc's
# native xattr functions via ctypes; Linux still uses the stdlib
# wrappers when present.
_OS_HAS_XATTR_STDLIB = (
    hasattr(os, "listxattr")
    and hasattr(os, "getxattr")
    and hasattr(os, "setxattr")
)


# ---------------------------------------------------------------------------
# macOS ctypes binding
# ---------------------------------------------------------------------------
# macOS C signatures (man 2 listxattr / getxattr / setxattr):
#   ssize_t  listxattr(const char *path, char *namebuf,
#                       size_t size, int options);
#   ssize_t  getxattr(const char *path, const char *name,
#                      void *value, size_t size,
#                      u_int32_t position, int options);
#   int      setxattr(const char *path, const char *name,
#                      const void *value, size_t size,
#                      u_int32_t position, int options);
#
# ``options`` flags (xattr.h): XATTR_NOFOLLOW = 0x0001 stops the
# call from following a symlink — matches Linux's ``l*xattr``
# variants and our writer's intent of capturing the link's own
# xattrs, not the target's.
_XATTR_NOFOLLOW = 0x0001


_libc = None
if sys.platform == "darwin":
    try:
        _libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.dylib")
        _libc.listxattr.argtypes = [
            ctypes.c_char_p, ctypes.c_char_p,
            ctypes.c_size_t, ctypes.c_int,
        ]
        _libc.listxattr.restype = ctypes.c_ssize_t
        _libc.getxattr.argtypes = [
            ctypes.c_char_p, ctypes.c_char_p,
            ctypes.c_void_p, ctypes.c_size_t,
            ctypes.c_uint32, ctypes.c_int,
        ]
        _libc.getxattr.restype = ctypes.c_ssize_t
        _libc.setxattr.argtypes = [
            ctypes.c_char_p, ctypes.c_char_p,
            ctypes.c_void_p, ctypes.c_size_t,
            ctypes.c_uint32, ctypes.c_int,
        ]
        _libc.setxattr.restype = ctypes.c_int
    except OSError:
        _libc = None


def _macos_listxattr(path: bytes) -> List[str]:
    if _libc is None:
        return []
    # Two-phase: ask for required size (passing buf=NULL, size=0),
    # then allocate + fetch.
    needed = _libc.listxattr(
        path, None, 0, _XATTR_NOFOLLOW,
    )
    if needed < 0:
        # ENOTSUP / ENOENT etc. — treat as "no xattrs visible".
        return []
    if needed == 0:
        return []
    buf = ctypes.create_string_buffer(needed)
    written = _libc.listxattr(
        path, buf, needed, _XATTR_NOFOLLOW,
    )
    if written < 0:
        return []
    raw = buf.raw[:written]
    # listxattr returns a NUL-separated list, with a trailing NUL.
    names = raw.split(b"\x00")
    return [n.decode("utf-8", "replace") for n in names if n]


def _macos_getxattr(path: bytes, name: str) -> Optional[bytes]:
    if _libc is None:
        return None
    name_bytes = name.encode("utf-8")
    needed = _libc.getxattr(
        path, name_bytes, None, 0, 0, _XATTR_NOFOLLOW,
    )
    if needed < 0:
        return None
    if needed == 0:
        return b""
    buf = ctypes.create_string_buffer(needed)
    got = _libc.getxattr(
        path, name_bytes, buf, needed, 0, _XATTR_NOFOLLOW,
    )
    if got < 0:
        return None
    return buf.raw[:got]


def _macos_setxattr(path: bytes, name: str, value: bytes) -> bool:
    if _libc is None:
        return False
    rc = _libc.setxattr(
        path, name.encode("utf-8"),
        value, len(value),
        0, _XATTR_NOFOLLOW,
    )
    return rc == 0


_HAS_XATTR = _OS_HAS_XATTR_STDLIB or (_libc is not None)


def has_xattr_support() -> bool:
    """Return True iff the current host exposes xattr APIs.

    True on Linux (via Python stdlib's ``os.*xattr``), True on
    macOS (via libc ctypes), False on Windows / hosts where libc
    couldn't be loaded."""
    return _HAS_XATTR


# ---------------------------------------------------------------------------
# Capture (writer side)
# ---------------------------------------------------------------------------


def capture_xattrs(
    path: os.PathLike,
    *,
    callback: Optional[Callable[..., None]] = None,
) -> Dict[str, bytes]:
    """Read every extended attribute on ``path`` and return a
    name → value dict.

    On platforms without xattr APIs returns ``{}``. On platforms
    that have them but the entry has no xattrs, also returns ``{}``.
    Per-attribute read failures are surfaced via
    ``callback("xattr_capture_error", path=…, name=…, error=…)``
    rather than raising — a single inaccessible xattr (e.g.
    ``trusted.*`` namespace under unprivileged user) shouldn't abort
    the file.

    ``follow_symlinks=False`` so a symlink's own xattrs are
    captured, not the target's. This matches how the writer's
    ``_walk_file`` uses ``lstat`` for symlinks: the link entity
    is what gets backed up, not what it points at.
    """
    if not _HAS_XATTR:
        return {}
    fspath = os.fsencode(os.fspath(path))
    if _OS_HAS_XATTR_STDLIB:
        try:
            names = os.listxattr(  # type: ignore[attr-defined]
                fspath, follow_symlinks=False,
            )
        except OSError:
            return {}
    else:
        names = _macos_listxattr(fspath)
    out: Dict[str, bytes] = {}
    for name in names:
        try:
            if _OS_HAS_XATTR_STDLIB:
                value = os.getxattr(  # type: ignore[attr-defined]
                    fspath, name, follow_symlinks=False,
                )
            else:
                value = _macos_getxattr(fspath, name)
                if value is None:
                    raise OSError(
                        f"getxattr returned no value for {name!r}"
                    )
            out[name] = value
        except OSError as exc:
            if callback is not None:
                try:
                    callback(
                        "xattr_capture_error",
                        {"path": str(path), "name": name,
                         "error": str(exc)},
                    )
                except Exception:
                    pass
    return out


def serialize_xattrs(xattrs: Dict[str, bytes]) -> bytes:
    """Encode a name → value dict as the on-disk xattr blob.

    Apple binary plist so the bytes are self-describing + can be
    read with stock plistlib on either side. Empty input returns
    ``b""`` so the caller can short-circuit "no xattrs → no blob".
    """
    if not xattrs:
        return b""
    # plistlib accepts bytes values directly under FMT_BINARY; that
    # avoids any base64-or-string encoding round-trip and lets
    # Finder-style binary xattrs survive byte-perfect.
    return plistlib.dumps(xattrs, fmt=plistlib.FMT_BINARY)


# ---------------------------------------------------------------------------
# Apply (reader / restore side)
# ---------------------------------------------------------------------------


def deserialize_xattrs(blob: bytes) -> Dict[str, bytes]:
    """Decode a blob written by :func:`serialize_xattrs`.

    Empty / missing input → ``{}``. Malformed plist raises
    ``plistlib.InvalidFileException`` (the standard library's own
    exception type so callers can pin it without importing this
    module's error hierarchy).
    """
    if not blob:
        return {}
    parsed = plistlib.loads(blob, fmt=plistlib.FMT_BINARY)
    if not isinstance(parsed, dict):
        raise plistlib.InvalidFileException(
            f"xattr blob is not a dict: type={type(parsed).__name__}"
        )
    out: Dict[str, bytes] = {}
    for k, v in parsed.items():
        if not isinstance(k, str):
            continue
        if isinstance(v, (bytes, bytearray)):
            out[k] = bytes(v)
        elif isinstance(v, str):
            # Some sources may emit utf-8-decoded strings; preserve
            # bytes round-trip by re-encoding.
            out[k] = v.encode("utf-8")
    return out


def apply_xattrs(
    path: os.PathLike,
    xattrs: Dict[str, bytes],
    *,
    callback: Optional[Callable[..., None]] = None,
) -> int:
    """Write each ``name → value`` to ``path`` via ``os.setxattr``.

    Returns the count actually applied. Per-attr failures are
    surfaced via ``callback("xattr_apply_error", …)`` rather than
    raising so one bad attr (e.g. a name the target FS rejects)
    doesn't abort the whole restore.

    ``follow_symlinks=False`` so symlink restores re-establish
    xattrs on the link itself, not on the target.
    """
    if not _HAS_XATTR or not xattrs:
        return 0
    fspath = os.fsencode(os.fspath(path))
    applied = 0
    for name, value in xattrs.items():
        try:
            if _OS_HAS_XATTR_STDLIB:
                os.setxattr(  # type: ignore[attr-defined]
                    fspath, name, value, follow_symlinks=False,
                )
            else:
                if not _macos_setxattr(fspath, name, value):
                    raise OSError(
                        f"setxattr failed for {name!r}"
                    )
            applied += 1
        except OSError as exc:
            if callback is not None:
                try:
                    callback(
                        "xattr_apply_error",
                        {"path": str(path), "name": name,
                         "error": str(exc)},
                    )
                except Exception:
                    pass
    return applied
