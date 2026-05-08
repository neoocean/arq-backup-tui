"""High-level restore orchestrator.

Walks a backup destination produced by :mod:`arq_writer` (or any other
backup that conforms to the same standalone-objects layout) and
materializes the tree under ``dest``.

Workflow:

    1. Discover the computer UUID, decrypt ``encryptedkeyset.dat``
       using the user-supplied password (``arq_validator.crypto``
       handles the inverse).
    2. List backup folders, choose one, find its latest backuprecord.
    3. Decrypt + LZ4-unwrap + plist-parse the backuprecord. Read its
       ``node`` field (a dict) — that's the root of the file tree.
    4. Walk the root recursively. For each ``isTree=true`` node, fetch
       the binary Tree from ``standardobjects/<shard>/<rest>``,
       decrypt it, parse it, and recurse over its children. For each
       file node, fetch each of its ``dataBlobLocs``, decrypt them,
       and concatenate to reconstruct the file content.

The reader uses :class:`arq_validator.backend.LocalBackend` so the
same path-traversal guard applies — relativePath fields in BlobLocs
that try to escape the backup root are rejected before any I/O.
"""

from __future__ import annotations

import json
import os
import plistlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from arq_validator import constants as C
from arq_validator.backend import Backend, LocalBackend
from arq_validator.crypto import Keyset, decrypt_keyset
from arq_validator.layout import (
    discover_layout,
    find_latest_backuprecord,
    list_backuprecords,
)
from arq_writer.types import BlobLoc, FileNode, TreeNode

from .decrypt import DecryptError, decrypt_encrypted_object, decrypt_lz4_arqo
from .parse import parse_tree


ProgressCb = Callable[[str, dict], None]


@dataclass
class RecordInfo:
    """Summary of one backuprecord, suitable for record-history UIs.

    ``creation_date`` is the writer's ``creationDate`` field (Unix
    seconds, as the writer emits them). ``relative_path`` is the
    backend-relative POSIX path that other ``Restore`` methods
    accept as ``backuprecord_path``. ``computer_uuid`` /
    ``folder_uuid`` are the same coordinates used by ``layouts()``.
    """

    computer_uuid: str
    folder_uuid: str
    relative_path: str
    creation_date: int = 0
    arq_version: str = ""
    is_complete: bool = True


@dataclass
class RestoreResult:
    src: Path
    dest: Path
    folder_uuid: str
    files_restored: int = 0
    dirs_restored: int = 0
    bytes_restored: int = 0
    blobs_fetched: int = 0
    failures: List[Dict[str, str]] = field(default_factory=list)


@dataclass
class _PathFilter:
    """Restrict the restore walk to a fixed list of source-relative
    POSIX paths.

    The set is interpreted prefix-style: an entry ``"a/b"`` matches
    the file ``a/b`` exactly AND every descendant of the directory
    ``a/b/`` (so ``"a/b/c.txt"`` is restored too).

    All comparisons are byte-for-byte against the Tree's stored
    UTF-8 child names — this means non-ASCII filenames flow through
    transparently as long as the caller's ``paths`` argument was
    encoded the same way the source filesystem encoded them when
    the backup was written (which is the writer's contract).
    """

    keep: frozenset

    @classmethod
    def from_paths(cls, paths: List[str]) -> "_PathFilter":
        norm = frozenset(
            p.strip("/") for p in paths if p.strip("/")
        )
        return cls(keep=norm)

    def matches(self, rel_path: str) -> bool:
        """True iff ``rel_path`` is to be restored."""
        if not self.keep:
            return True
        rel_path = rel_path.strip("/")
        if rel_path in self.keep:
            return True
        for k in self.keep:
            if rel_path.startswith(k + "/"):
                return True
        return False

    def descend(self, rel_path: str) -> bool:
        """True iff the directory at ``rel_path`` could contain a
        matching descendant — used to skip whole subtrees that the
        filter excludes."""
        if not self.keep:
            return True
        rel_path = rel_path.strip("/")
        if rel_path == "":
            return True
        for k in self.keep:
            # Either the dir itself is a kept entry, OR it sits
            # above a kept entry, OR it sits below one (descendant
            # of an already-included subtree).
            if rel_path == k:
                return True
            if rel_path.startswith(k + "/"):
                return True
            if k.startswith(rel_path + "/"):
                return True
        return False


def _build_path_filter(paths: Optional[List[str]]) -> Optional[_PathFilter]:
    if paths is None:
        return None
    return _PathFilter.from_paths(paths)


def _parse_backuprecord(plain: bytes) -> Dict[str, Any]:
    """Backward-compat alias — :func:`arq_writer.backuprecord.parse_backuprecord`
    is the canonical public dual-format parser. Kept here so existing
    test imports continue to work."""
    from arq_writer.backuprecord import parse_backuprecord
    return parse_backuprecord(plain)


def _emit(cb: Optional[ProgressCb], kind: str, **payload: object) -> None:
    if cb is None:
        return
    try:
        cb(kind, dict(payload))
    except Exception:
        pass


class Restore:
    """Stateful restorer.

    One instance per (backup-source, password) pair. Reuse across
    multiple restore operations is safe — the keyset and discovered
    layout are cached internally on first call.
    """

    def __init__(
        self,
        src: Path,
        encryption_password: str,
        *,
        openssl_path: str = "openssl",
        backend: Optional[Backend] = None,
    ) -> None:
        """Open a backup destination for restore.

        ``backend`` opts out of the default ``LocalBackend(src)`` path
        and lets the caller use any object that satisfies the
        :class:`~arq_validator.backend.Backend` Protocol. The
        validator's :class:`~arq_validator.sftp.SftpBackend` is the
        primary alternative; in that mode ``src`` is the backend-
        relative root path inside the SFTP server (typically the
        absolute server path that points at the backup destination,
        e.g. ``"/home/arq/dest1"``), not a local filesystem path.
        """
        self.src = Path(src) if backend is None else src
        self.password = encryption_password
        self.openssl_path = openssl_path
        if backend is not None:
            self.backend = backend
        else:
            self.backend = LocalBackend(Path(src).resolve())
        self._layouts = None
        self._keyset_by_computer: Dict[str, Keyset] = {}

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def layouts(self):
        if self._layouts is None:
            self._layouts = discover_layout(self.backend, "/")
        return self._layouts

    def list_folders(self) -> List[Tuple[str, str]]:
        """Return ``[(computer_uuid, folder_uuid)]`` pairs."""
        out: List[Tuple[str, str]] = []
        for lay in self.layouts():
            for f in lay.backup_folder_uuids:
                out.append((lay.computer_uuid, f))
        return out

    def keyset(self, computer_uuid: str) -> Keyset:
        if computer_uuid not in self._keyset_by_computer:
            kp = f"/{computer_uuid}/{C.KEYSET_FILE}"
            blob = self.backend.read_all(kp)
            self._keyset_by_computer[computer_uuid] = decrypt_keyset(
                blob, self.password, openssl_path=self.openssl_path,
            )
        return self._keyset_by_computer[computer_uuid]

    # ------------------------------------------------------------------
    # Single-restore helpers
    # ------------------------------------------------------------------

    def _fetch_blob(self, loc: BlobLoc, keyset: Keyset) -> bytes:
        """Fetch + decrypt + (LZ4-unwrap if needed) a referenced blob.

        Mirrors ``Arq7BlobReader.m::dataForBlobLoc:`` exactly:

        - Packed blob (``isPacked=true``): read
          ``backend.read_range(relativePath, offset, length)``.
        - Standalone blob: read the whole file at ``relativePath``.
        - If the bytes start with ``b"ARQO"``, decrypt; otherwise the
          backup is unencrypted (legal per spec) and the bytes are
          taken as-is.
        - If ``compressionType == 2`` (LZ4), decompress.
          ``compressionType == 0`` is "none". ``compressionType == 1``
          (Gzip) appears only in legacy Arq 5 data — supported here
          via stdlib ``gzip``.

        We do **not** need to know the pack file's header / index /
        framing — ``BlobLoc.offset`` and ``length`` are the only
        information required to slice out a blob.
        """
        if loc.isPacked:
            raw = self.backend.read_range(
                loc.relativePath, loc.offset, loc.length,
            )
        else:
            raw = self.backend.read_all(loc.relativePath)

        if raw[:4] == b"ARQO":
            raw = decrypt_encrypted_object(
                raw, keyset.encryption_key, keyset.hmac_key,
                openssl_path=self.openssl_path,
            )

        if loc.compressionType == 2:
            from arq_writer.lz4_block import lz4_unwrap
            return lz4_unwrap(raw)
        if loc.compressionType == 1:
            import gzip
            return gzip.decompress(raw)
        if loc.compressionType == 0:
            return raw
        raise NotImplementedError(
            f"compressionType={loc.compressionType} not supported "
            f"(0=none, 1=Gzip, 2=LZ4 are implemented)"
        )

    @staticmethod
    def _blobloc_from_dict(d: Dict[str, Any]) -> BlobLoc:
        """Convert a plist-form BlobLoc dict to the dataclass form.

        Used when decoding the root Node dict embedded in a
        backuprecord plist.
        """
        return BlobLoc(
            blobIdentifier=d.get("blobIdentifier", "") or "",
            isPacked=bool(d.get("isPacked", False)),
            isLargePack=bool(d.get("isLargePack", False)),
            relativePath=d.get("relativePath", "") or "",
            offset=int(d.get("offset", 0)),
            length=int(d.get("length", 0)),
            stretchEncryptionKey=bool(d.get("stretchEncryptionKey", True)),
            compressionType=int(d.get("compressionType", 2)),
        )

    # ------------------------------------------------------------------
    # Walk + materialize
    # ------------------------------------------------------------------

    def _restore_dir_node(
        self,
        tree_blob_loc: BlobLoc,
        out_dir: Path,
        keyset: Keyset,
        result: RestoreResult,
        callback: Optional[ProgressCb],
        *,
        rel_path: str = "",
        path_filter: "Optional[_PathFilter]" = None,
        check_cancel: "Optional[Callable[[], None]]" = None,
    ) -> None:
        if check_cancel is not None:
            check_cancel()
        # When a filter is set and this whole subtree is excluded
        # by it, skip directory creation + tree fetch entirely.
        if path_filter is not None and not path_filter.descend(rel_path):
            return
        out_dir.mkdir(parents=True, exist_ok=True)
        result.dirs_restored += 1
        try:
            tree_bytes = self._fetch_blob(tree_blob_loc, keyset)
            result.blobs_fetched += 1
        except (DecryptError, OSError, NotImplementedError) as exc:
            result.failures.append({
                "path": str(out_dir),
                "kind": "tree_fetch",
                "error": f"{type(exc).__name__}: {exc}",
            })
            _emit(callback, "tree_failed",
                  path=str(out_dir), error=str(exc))
            return
        tree = parse_tree(tree_bytes)
        _emit(callback, "tree_restored",
              path=str(out_dir), children=len(tree.children))
        for child in tree.children:
            child_rel = (
                f"{rel_path}/{child.name}" if rel_path else child.name
            )
            child_out = out_dir / child.name
            if isinstance(child.node, TreeNode):
                self._restore_dir_node(
                    child.node.treeBlobLoc, child_out, keyset,
                    result, callback,
                    rel_path=child_rel,
                    path_filter=path_filter,
                    check_cancel=check_cancel,
                )
            elif isinstance(child.node, FileNode):
                if (
                    path_filter is not None
                    and not path_filter.matches(child_rel)
                ):
                    continue
                self._restore_file_node(
                    child.node, child_out, keyset, result, callback,
                )

    def _restore_file_node(
        self,
        node: FileNode,
        out_path: Path,
        keyset: Keyset,
        result: RestoreResult,
        callback: Optional[ProgressCb],
    ) -> None:
        import stat as _stat
        out_path.parent.mkdir(parents=True, exist_ok=True)

        # Symlinks: writer stores the link target as the file's
        # content (under the S_IFLNK mode bit) so a restorer can
        # rebuild the link as a symlink rather than as a regular
        # file holding the target string.
        is_symlink = (
            node.mac_st_mode
            and _stat.S_ISLNK(int(node.mac_st_mode))
        )
        chunks: List[bytes] = []
        try:
            for loc in node.dataBlobLocs:
                chunks.append(self._fetch_blob(loc, keyset))
                result.blobs_fetched += 1
        except (DecryptError, OSError, NotImplementedError) as exc:
            result.failures.append({
                "path": str(out_path),
                "kind": "file_fetch",
                "error": f"{type(exc).__name__}: {exc}",
            })
            _emit(callback, "file_failed",
                  path=str(out_path), error=str(exc))
            return
        body = b"".join(chunks)

        if is_symlink:
            # Replace any existing entry; os.symlink errors out
            # otherwise.
            try:
                if out_path.is_symlink() or out_path.exists():
                    out_path.unlink()
            except OSError:
                pass
            try:
                target = body.decode("utf-8", errors="replace")
                os.symlink(target, out_path)
            except OSError as exc:
                result.failures.append({
                    "path": str(out_path),
                    "kind": "symlink_create",
                    "error": f"{type(exc).__name__}: {exc}",
                })
                _emit(callback, "file_failed",
                      path=str(out_path), error=str(exc))
                return
            result.bytes_restored += len(body)
            result.files_restored += 1
            _emit(callback, "file_restored",
                  path=str(out_path), size=len(body),
                  symlink=True)
            return

        out_path.write_bytes(body)
        result.bytes_restored += len(body)
        result.files_restored += 1

        # Metadata restoration. Each step is wrapped in try/except
        # so a single permission denied (e.g. uid restore as a
        # non-root user) doesn't abort the whole file.
        # mtime + atime
        try:
            if node.mtime_sec:
                ts = node.mtime_sec + node.mtime_nsec / 1_000_000_000
                os.utime(out_path, (ts, ts))
        except OSError:
            pass
        # mode (perm bits only — file-type bits stay as the OS
        # default for a regular file)
        try:
            if node.mac_st_mode:
                os.chmod(
                    out_path,
                    _stat.S_IMODE(int(node.mac_st_mode)),
                )
        except OSError:
            pass
        _emit(callback, "file_restored",
              path=str(out_path), size=len(body))

    def _count_tree(
        self,
        tree_blob_loc: BlobLoc,
        keyset: Keyset,
        path_filter: "Optional[_PathFilter]",
        *,
        rel_path: str = "",
    ) -> "tuple[int, int]":
        """Walk ``tree_blob_loc`` recursively and return
        ``(file_count, total_bytes)`` covering everything that the
        full restore would process.

        Honours ``path_filter`` so a partial restore plans only the
        bytes it's about to materialize. Tree blobs are fetched once
        here and again during the actual restore — for remote
        backends this is a small cost (tree blobs are tiny relative
        to file blobs) in exchange for a real ETA. Tree-fetch
        failures are silently absorbed into a zero-count for that
        subtree; restore proper will surface them as
        ``tree_failed`` events when it tries again.
        """
        if path_filter is not None and not path_filter.descend(rel_path):
            return (0, 0)
        try:
            tree_bytes = self._fetch_blob(tree_blob_loc, keyset)
        except (DecryptError, OSError, NotImplementedError):
            return (0, 0)
        tree = parse_tree(tree_bytes)
        files = 0
        total_bytes = 0
        for child in tree.children:
            child_rel = (
                f"{rel_path}/{child.name}" if rel_path else child.name
            )
            if isinstance(child.node, TreeNode):
                f, b = self._count_tree(
                    child.node.treeBlobLoc, keyset, path_filter,
                    rel_path=child_rel,
                )
                files += f
                total_bytes += b
            elif isinstance(child.node, FileNode):
                if (
                    path_filter is not None
                    and not path_filter.matches(child_rel)
                ):
                    continue
                files += 1
                total_bytes += int(child.node.itemSize or 0)
        return (files, total_bytes)

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    def list_records(
        self, *, folder_uuid: str,
        computer_uuid: Optional[str] = None,
    ) -> List[RecordInfo]:
        """Return one :class:`RecordInfo` per backuprecord under
        ``folder_uuid``, oldest first.

        Each entry's ``creation_date`` / ``arq_version`` /
        ``is_complete`` are read from the record's plist envelope
        (decrypted in place); a UI typically renders the list as
        a chronological history selector.
        """
        if computer_uuid is None:
            computer_uuid = self._resolve_single_computer(folder_uuid)
        keyset = self.keyset(computer_uuid)
        paths = list_backuprecords(
            self.backend, "/", computer_uuid, folder_uuid,
        )
        records: List[RecordInfo] = []
        for p in paths:
            try:
                arqo = self.backend.read_all(p)
                plain = decrypt_lz4_arqo(
                    arqo, keyset.encryption_key, keyset.hmac_key,
                    openssl_path=self.openssl_path,
                )
                rec = plistlib.loads(plain)
            except Exception:
                # Corrupt record: surface it but with empty
                # metadata so the caller can still try it.
                records.append(RecordInfo(
                    computer_uuid=computer_uuid,
                    folder_uuid=folder_uuid,
                    relative_path=p,
                ))
                continue
            if not isinstance(rec, dict):
                continue
            records.append(RecordInfo(
                computer_uuid=computer_uuid,
                folder_uuid=folder_uuid,
                relative_path=p,
                creation_date=int(rec.get("creationDate") or 0),
                arq_version=str(rec.get("arqVersion") or ""),
                is_complete=bool(rec.get("isComplete", True)),
            ))
        return records

    def _resolve_single_computer(self, folder_uuid: str) -> str:
        layouts = self.layouts()
        matches = [
            lay for lay in layouts
            if folder_uuid in lay.backup_folder_uuids
        ]
        if len(matches) == 0:
            raise ValueError(
                f"folder UUID {folder_uuid!r} not found at {self.src}"
            )
        if len(matches) > 1:
            raise ValueError(
                f"folder UUID {folder_uuid!r} ambiguous across "
                f"{[m.computer_uuid for m in matches]}"
            )
        return matches[0].computer_uuid

    def restore(
        self,
        *,
        folder_uuid: str,
        dest: Path,
        computer_uuid: Optional[str] = None,
        backuprecord_path: Optional[str] = None,
        paths: Optional[List[str]] = None,
        callback: Optional[ProgressCb] = None,
        plan_totals: bool = True,
    ) -> RestoreResult:
        """Restore from a backup folder.

        Defaults restore the **most recent** record of
        ``folder_uuid`` in full. Two opt-in narrowings:

        - ``backuprecord_path``: an explicit record path (typically
          obtained from :meth:`list_records`). Use to restore a
          historical snapshot rather than the latest.
        - ``paths``: a list of source-relative POSIX paths
          (forward-slash separated, e.g. ``["Documents/notes.md"]``).
          When set, only those paths are restored; everything else
          is skipped. A ``paths`` entry that names a directory
          recursively restores its subtree. Names are matched
          byte-for-byte against the Tree's UTF-8 child names —
          non-ASCII source paths round-trip transparently.

        ``computer_uuid`` may be omitted when the destination has
        exactly one computer subtree.

        ``plan_totals`` (default True): walk the tree once before
        restore to count total files + total bytes, then emit a
        single ``restore_planned`` event so a progress UI can
        compute ETA. Set ``False`` for headless / streaming use
        where the extra tree-blob fetches aren't worth the up-front
        cost. The pre-walk respects ``paths`` so only the subtree
        that's actually about to be restored gets counted.
        """
        if computer_uuid is None:
            computer_uuid = self._resolve_single_computer(folder_uuid)

        result = RestoreResult(
            src=self.src, dest=Path(dest).resolve(), folder_uuid=folder_uuid,
        )
        keyset = self.keyset(computer_uuid)

        if backuprecord_path is not None:
            record_path = backuprecord_path
        else:
            record_path = find_latest_backuprecord(
                self.backend, "/", computer_uuid, folder_uuid,
            )
        if record_path is None:
            raise ValueError(
                f"no backuprecord found for {computer_uuid}/{folder_uuid}"
            )

        # Normalize the paths filter once so the recursive walker
        # only has to test prefix membership.
        path_filter = _build_path_filter(paths)
        _emit(callback, "backuprecord_found",
              path=record_path,
              computer=computer_uuid, folder=folder_uuid)

        record_arqo = self.backend.read_all(record_path)
        record_plain = decrypt_lz4_arqo(
            record_arqo, keyset.encryption_key, keyset.hmac_key,
            openssl_path=self.openssl_path,
        )
        record = _parse_backuprecord(record_plain)
        node_dict = record.get("node")
        if not isinstance(node_dict, dict):
            raise ValueError("backuprecord missing or malformed `node` field")

        out_dir = Path(dest).resolve()
        if node_dict.get("isTree"):
            tree_blob_loc = self._blobloc_from_dict(
                node_dict["treeBlobLoc"]
            )
            if plan_totals:
                total_files, total_bytes = self._count_tree(
                    tree_blob_loc, keyset, path_filter,
                )
                _emit(callback, "restore_planned",
                      total_files=total_files,
                      total_bytes=total_bytes)
            self._restore_dir_node(
                tree_blob_loc, out_dir, keyset, result, callback,
                rel_path="", path_filter=path_filter,
            )
        else:
            # Root is a single file (rare but representable).
            if (
                path_filter is not None
                and not path_filter.matches("")
            ):
                # User asked for a sub-path, but root is a file.
                # Nothing to do.
                pass
            else:
                file_node = FileNode(
                    dataBlobLocs=[
                        self._blobloc_from_dict(b)
                        for b in node_dict.get("dataBlobLocs", [])
                    ],
                    itemSize=int(node_dict.get("itemSize", 0)),
                    mtime_sec=int(node_dict.get("modificationTime_sec", 0)),
                    mtime_nsec=int(node_dict.get("modificationTime_nsec", 0)),
                )
                if plan_totals:
                    _emit(callback, "restore_planned",
                          total_files=1,
                          total_bytes=file_node.itemSize)
                self._restore_file_node(
                    file_node, out_dir, keyset, result, callback,
                )
        _emit(callback, "restore_finished",
              files=result.files_restored,
              dirs=result.dirs_restored,
              bytes=result.bytes_restored,
              failures=len(result.failures))
        return result
