"""Storage backend abstraction.

Validation logic talks to a backup destination through a tiny set of
methods (``list_dir``, ``stat_size``, ``read_range``, ``read_all``).
The bundled implementation, :class:`LocalBackend`, serves the local
filesystem; future remote backends (SFTP, S3, etc.) drop in by
implementing the same interface.

Paths handed to backend methods are POSIX-style strings rooted at the
backup destination (e.g. ``"/<computer-uuid>/blobpacks/00/...pack"``).
``LocalBackend`` resolves them under its configured root.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Protocol, runtime_checkable


@runtime_checkable
class Backend(Protocol):
    """Minimal interface a storage backend must implement.

    Read methods (``list_dir`` / ``stat_size`` / ``read_range`` /
    ``read_all`` / ``exists`` / ``is_dir``) are required for both
    validator and reader use.

    Write methods (``mkdir`` / ``write_all``) are required for the
    writer. Read-only backends can leave them as no-ops or raisers;
    callers that mutate the destination assume both are available.
    """

    def list_dir(self, path: str) -> List[str]:
        """Return the entries (names only) under ``path``, sorted."""
        ...

    def stat_size(self, path: str) -> int:
        """Return the size of the file at ``path`` in bytes."""
        ...

    def read_range(self, path: str, offset: int, length: int) -> bytes:
        """Read ``length`` bytes starting at ``offset``."""
        ...

    def read_all(self, path: str) -> bytes:
        """Read the entire file."""
        ...

    def exists(self, path: str) -> bool:
        """Return True iff a file or directory exists at ``path``."""
        ...

    def is_dir(self, path: str) -> bool:
        """Return True iff ``path`` is an existing directory."""
        ...

    def mkdir(
        self, path: str, *,
        parents: bool = True, exist_ok: bool = True,
    ) -> None:
        """Create a directory. With ``parents=True``, missing
        intermediates are created. With ``exist_ok=True``, an
        existing directory at ``path`` is silently OK."""
        ...

    def write_all(self, path: str, data: bytes) -> None:
        """Write ``data`` as the full content of the file at
        ``path``. Atomicity guarantees are backend-defined; for
        ``LocalBackend`` writes go straight to the path, for
        ``SftpBackend`` they go through a local temp file plus
        ``sftp put``."""
        ...


class LocalBackend:
    """Local filesystem backend rooted at a directory.

    The validator resolves all backup-relative paths against this root,
    so a destination at ``/Volumes/arqbackup1`` containing a backup at
    ``/Volumes/arqbackup1/<UUID>/blobpacks/...`` is exposed as the
    POSIX path ``/<UUID>/blobpacks/...``.

    Resolved paths are checked to lie under the root to prevent
    backend-relative paths from escaping via "..", which matters when
    the validator inputs originate from on-disk listings.
    """

    def __init__(self, root: os.PathLike) -> None:
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise NotADirectoryError(
                f"backup root is not a directory: {self.root}"
            )

    def _resolve(self, path: str) -> Path:
        rel = path.lstrip("/")
        full = (self.root / rel).resolve()
        try:
            full.relative_to(self.root)
        except ValueError as exc:
            raise PermissionError(
                f"path escapes backup root: {path!r}"
            ) from exc
        return full

    def list_dir(self, path: str) -> List[str]:
        p = self._resolve(path)
        if not p.is_dir():
            raise NotADirectoryError(f"not a directory: {p}")
        return sorted(e.name for e in p.iterdir())

    def stat_size(self, path: str) -> int:
        return self._resolve(path).stat().st_size

    def read_range(self, path: str, offset: int, length: int) -> bytes:
        if offset < 0 or length < 0:
            raise ValueError(
                f"read_range bad args: offset={offset} length={length}"
            )
        if length == 0:
            return b""
        p = self._resolve(path)
        with p.open("rb") as f:
            f.seek(offset)
            return f.read(length)

    def read_all(self, path: str) -> bytes:
        return self._resolve(path).read_bytes()

    def exists(self, path: str) -> bool:
        try:
            return self._resolve(path).exists()
        except (PermissionError, OSError):
            return False

    def is_dir(self, path: str) -> bool:
        try:
            return self._resolve(path).is_dir()
        except (PermissionError, OSError):
            return False

    def mkdir(
        self, path: str, *,
        parents: bool = True, exist_ok: bool = True,
    ) -> None:
        # _resolve requires the parent to exist (uses .resolve()),
        # so build the path manually for missing-tree creation.
        rel = path.lstrip("/")
        full = self.root / rel
        full.mkdir(parents=parents, exist_ok=exist_ok)

    def write_all(self, path: str, data: bytes) -> None:
        rel = path.lstrip("/")
        full = self.root / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_bytes(data)
