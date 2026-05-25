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
from arq_writer.xattrs import apply_xattrs, deserialize_xattrs

from .decrypt import DecryptError, decrypt_encrypted_object, decrypt_lz4_arqo
from .parse import parse_tree


# Planning-phase progress drip cadence: one ``restore_planning``
# event per N files counted. Set to 200 so a 10 000-file restore
# emits ~50 ticks during planning — enough for the TUI to look
# alive without spamming the message queue.
_PLANNING_TICK_FILES = 200


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
class DryRunRestoreResult:
    """Output of :meth:`Restore.dry_run_restore`.

    No bytes were written to the destination — this is a
    list-only walk over the backuprecord's tree. Use
    ``files_listed + bytes_would_restore`` to size a real
    restore; use ``sample_paths`` (first 10) to spot-check that
    your ``paths=`` filter resolved the way you expected before
    committing to the I/O.
    """

    src: Path
    folder_uuid: str
    backuprecord_path: str = ""
    files_listed: int = 0
    dirs_listed: int = 0
    bytes_would_restore: int = 0
    # First 10 paths the walk would restore — kept short so
    # the dataclass round-trips cheaply through JSON. Operators
    # who want the full list can capture the would_restore_file
    # events from the callback.
    sample_paths: List[str] = field(default_factory=list)


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


def _load_record_plain(raw: bytes, keyset: Keyset,
                       openssl_path: str = "openssl") -> bytes:
    """ARQO-magic-gated backuprecord load → JSON/plist bytes.

    Encrypted: ``ARQO( lz4_wrap(record) )`` → decrypt + lz4-unwrap.
    Unencrypted ("Continue Without Encryption"): the record is
    ``lz4_wrap(record)`` with no ARQO envelope (docs/UNENCRYPTED-FORMAT-RE.md)."""
    if raw[:4] == b"ARQO":
        return decrypt_lz4_arqo(
            raw, keyset.encryption_key, keyset.hmac_key,
            openssl_path=openssl_path,
        )
    from arq_writer.lz4_block import lz4_unwrap
    return lz4_unwrap(raw)


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
        on_conflict: str = "overwrite",
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

        ``on_conflict`` controls what happens when a restored
        file would land on top of an existing one in the
        destination. Three policies:

        - ``"overwrite"`` (default, legacy behaviour): the
          existing file is silently replaced with the restored
          bytes.
        - ``"skip"``: the restored file is dropped silently +
          a ``conflict_skipped`` event fires on the callback.
        - ``"rename"``: the restored file is written to a
          sibling path with a ``.restored-<N>`` suffix where
          ``N`` is the smallest integer that doesn't already
          exist. ``conflict_renamed`` event carries the new
          name. The original file stays untouched.

        Operators on a partial-restore-into-live-tree workflow
        ("restore just /home/me/Documents from last week's
        snapshot, on top of my current home dir") usually want
        ``"rename"`` so they can compare the two side-by-side
        before deleting either.
        """
        if on_conflict not in ("overwrite", "skip", "rename"):
            raise ValueError(
                f"on_conflict must be 'overwrite' / 'skip' / "
                f"'rename', got {on_conflict!r}"
            )
        self.on_conflict = on_conflict
        self.src = Path(src) if backend is None else src
        self.password = encryption_password
        self.openssl_path = openssl_path
        if backend is not None:
            self.backend = backend
        else:
            self.backend = LocalBackend(Path(src).resolve())
        self._layouts = None
        self._keyset_by_computer: Dict[str, Keyset] = {}
        # Per-instance plaintext cache for tree blobs. The pre-walk
        # phase (_count_tree) and the restore phase
        # (_restore_dir_node) each fetch every reachable tree
        # blob — without this cache we round-trip every tree
        # twice over SFTP. Trees are small (<<1MB each) so the
        # in-memory cost is negligible compared to the network
        # savings on large remote backups. Keyed by blobIdentifier.
        self._tree_plain_cache: Dict[str, bytes] = {}
        # Hardlink restore: maps the original source inode (as
        # stored in the FileNode's mac_st_ino) to the first path
        # we materialised for that inode. Subsequent FileNodes
        # carrying the same mac_st_ino are restored as os.link()
        # to the first path so the link relationship survives
        # the round-trip.
        self._inode_to_restored_path: Dict[int, Path] = {}

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
            if not self.backend.exists(kp):
                # Unencrypted destination ("Continue Without Encryption"):
                # no keyset at all. Return an empty Keyset — the blob + record
                # read paths are ARQO-magic-gated, so they never use these
                # keys for an unencrypted destination. See
                # docs/UNENCRYPTED-FORMAT-RE.md.
                self._keyset_by_computer[computer_uuid] = Keyset(
                    encryption_key=b"", hmac_key=b"", blob_id_salt=b"",
                )
            else:
                blob = self.backend.read_all(kp)
                self._keyset_by_computer[computer_uuid] = decrypt_keyset(
                    blob, self.password, openssl_path=self.openssl_path,
                )
        return self._keyset_by_computer[computer_uuid]

    # ------------------------------------------------------------------
    # Single-restore helpers
    # ------------------------------------------------------------------

    def _fetch_tree_blob_cached(
        self, loc: BlobLoc, keyset: Keyset,
    ) -> bytes:
        """Cached version of :meth:`_fetch_blob` specialised for
        tree blobs. The pre-walk + restore phase both fetch the
        same trees; this halves the round-trip count without
        risking memory blow-up the way caching every blob would
        (file blobs can be GBs each — only tree blobs, which
        are <<1MB, get cached).

        Keyed by blob_id (== plaintext SHA-256), so two different
        BlobLocs that point at the same content (cross-snapshot
        dedup) also share the cache entry."""
        blob_id = getattr(loc, "blobIdentifier", "") or ""
        if blob_id and blob_id in self._tree_plain_cache:
            return self._tree_plain_cache[blob_id]
        plain = self._fetch_blob(loc, keyset)
        if blob_id:
            self._tree_plain_cache[blob_id] = plain
        return plain

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

    def _resolve_conflict_target(
        self,
        out_path: Path,
        callback: Optional[ProgressCb],
    ) -> Optional[Path]:
        """Apply ``self.on_conflict`` to ``out_path``.

        Returns the path the caller should actually write to, or
        ``None`` when the policy says to skip. Always emits one
        of ``conflict_skipped`` / ``conflict_renamed`` /
        ``conflict_overwritten`` when a conflict is observed so
        the operator can see what happened in the log tail.
        """
        if not (out_path.exists() or out_path.is_symlink()):
            return out_path
        if self.on_conflict == "skip":
            _emit(callback, "conflict_skipped",
                  path=str(out_path))
            return None
        if self.on_conflict == "rename":
            for n in range(1, 1000):
                candidate = out_path.with_name(
                    f"{out_path.name}.restored-{n}",
                )
                if not (
                    candidate.exists()
                    or candidate.is_symlink()
                ):
                    _emit(callback, "conflict_renamed",
                          path=str(out_path),
                          renamed_to=str(candidate))
                    return candidate
            # 1000 collisions is hopeless; surface as a
            # failure-equivalent skip.
            _emit(callback, "conflict_skipped",
                  path=str(out_path),
                  reason="rename_exhausted")
            return None
        # "overwrite" (default) — silently replace.
        _emit(callback, "conflict_overwritten",
              path=str(out_path))
        return out_path

    def _apply_acl_to(
        self,
        node,
        out_path: Path,
        keyset: Keyset,
        result: RestoreResult,
        callback: Optional[ProgressCb],
    ) -> bool:
        """Fetch + decrypt + apply the ACL blob attached to a
        Node, if any. Returns True iff the apply actually ran
        (False = no ACL, wrong-platform blob, or fetch error).

        Same per-error policy as xattrs — failures surface as
        callback events but never abort the file.
        """
        loc = getattr(node, "aclBlobLoc", None)
        if loc is None:
            return False
        try:
            blob = self._fetch_blob(loc, keyset)
        except (DecryptError, OSError, NotImplementedError, ValueError) as exc:
            _emit(callback, "acl_fetch_error",
                  path=str(out_path), error=str(exc))
            return False
        from arq_writer.acl import apply_acl
        try:
            return apply_acl(
                out_path, blob,
                callback=lambda kind, payload: _emit(
                    callback, kind, **payload,
                ),
            )
        except Exception as exc:
            _emit(callback, "acl_apply_error",
                  path=str(out_path), error=str(exc))
            return False

    def _apply_xattrs_to(
        self,
        node,
        out_path: Path,
        keyset: Keyset,
        result: RestoreResult,
        callback: Optional[ProgressCb],
    ) -> int:
        """Fetch + decode + apply every xattr blob attached to a Node.

        Returns the number of xattrs actually applied (0 when the
        node has no xattrs, the host doesn't support xattrs, or
        every entry hit a per-attr error). Per-attr OSErrors are
        surfaced through ``callback("xattr_apply_error", …)`` from
        :func:`apply_xattrs` directly so a single bad attr can't
        wreck the file.
        """
        locs = getattr(node, "xattrsBlobLocs", None) or []
        if not locs:
            return 0
        total = 0
        for loc in locs:
            try:
                blob = self._fetch_blob(loc, keyset)
            except (DecryptError, OSError, NotImplementedError, ValueError) as exc:
                _emit(callback, "xattr_fetch_error",
                      path=str(out_path), error=str(exc))
                continue
            try:
                xattrs = deserialize_xattrs(blob)
            except Exception as exc:
                _emit(callback, "xattr_decode_error",
                      path=str(out_path), error=str(exc))
                continue
            total += apply_xattrs(
                out_path, xattrs,
                callback=lambda kind, payload: _emit(
                    callback, kind, **payload,
                ),
            )
        return total

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
        tree_node: "Optional[TreeNode]" = None,
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
            tree_bytes = self._fetch_tree_blob_cached(
                tree_blob_loc, keyset,
            )
            result.blobs_fetched += 1
        except (DecryptError, OSError, NotImplementedError, ValueError) as exc:
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
                    tree_node=child.node,
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
        # Re-apply directory-level xattrs after children are placed.
        # If we did this before children, recursive directory creation
        # could overwrite the xattrs (some FS/xattr namespaces are
        # cleared on subsequent inode mutations).
        if tree_node is not None:
            self._apply_xattrs_to(
                tree_node, out_dir, keyset, result, callback,
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

        # Hardlink reconstruction. The writer cached
        # (st_dev, st_ino) -> FileNode in this run, so multiple
        # children that originally shared an inode now share the
        # same FileNode object — same mac_st_ino + same
        # mac_st_nlink > 1. When we see the second-and-later
        # occurrence, link to the first restored path instead of
        # writing the body again.
        nlink = int(getattr(node, "mac_st_nlink", 1) or 1)
        ino = int(getattr(node, "mac_st_ino", 0) or 0)
        if nlink > 1 and ino:
            prev_path = self._inode_to_restored_path.get(ino)
            if prev_path is not None and prev_path.exists():
                try:
                    if out_path.exists() or out_path.is_symlink():
                        out_path.unlink()
                except OSError:
                    pass
                try:
                    os.link(prev_path, out_path)
                    result.files_restored += 1
                    _emit(callback, "file_restored",
                          path=str(out_path),
                          size=int(getattr(node, "itemSize", 0)),
                          hardlink_to=str(prev_path))
                    return
                except OSError as exc:
                    # Fall through to normal write — links can
                    # fail across filesystems or with permissions.
                    _emit(callback, "hardlink_fallback",
                          path=str(out_path),
                          target=str(prev_path),
                          error=str(exc))

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
        except (DecryptError, OSError, NotImplementedError, ValueError) as exc:
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

        # Conflict resolution: if a file already exists at
        # out_path, the policy chosen at Restore.__init__ time
        # decides what to do.
        write_target = self._resolve_conflict_target(
            out_path, callback,
        )
        if write_target is None:
            # Skip policy → don't write, don't tally bytes.
            return
        write_target.write_bytes(body)
        result.bytes_restored += len(body)
        result.files_restored += 1

        # Remember this path as the link target for any
        # subsequent FileNode carrying the same inode.
        if nlink > 1 and ino:
            self._inode_to_restored_path[ino] = write_target
        # Subsequent metadata steps (chmod / chown / xattrs /
        # ACL) act on the actual write target, which may be
        # a renamed sibling. Update out_path to point at it.
        out_path = write_target

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
        # uid / gid restore — best-effort. os.chown only succeeds
        # when running as root (or when the target uid/gid match
        # the caller's already). For non-root operators the
        # numeric uid/gid stays whatever the OS assigned at
        # write_bytes(); the metadata is preserved on the Node
        # regardless so a future root-mode restore could pick
        # them up. Errors are silently absorbed — there's nothing
        # the per-file walker can do about EPERM.
        uid = int(getattr(node, "mac_st_uid", 0) or 0)
        gid = int(getattr(node, "mac_st_gid", 0) or 0)
        if uid or gid:
            try:
                os.chown(out_path, uid, gid)
            except OSError as exc:
                _emit(callback, "chown_failed",
                      path=str(out_path),
                      uid=uid, gid=gid, error=str(exc))
        # Re-apply xattrs the writer captured. Per-attr failures
        # surface via the callback (xattr_apply_error) but don't
        # abort the file — same policy as the chmod above.
        applied = self._apply_xattrs_to(
            node, out_path, keyset, result, callback,
        )
        # Re-apply ACL (NFSv4 on macOS, POSIX on Linux). Same
        # error policy as xattr — per-entry failures surface as
        # callback events but never abort the file.
        acl_applied = self._apply_acl_to(
            node, out_path, keyset, result, callback,
        )
        _emit(callback, "file_restored",
              path=str(out_path), size=len(body),
              xattrs_applied=applied,
              acl_applied=acl_applied)

    def _count_tree(
        self,
        tree_blob_loc: BlobLoc,
        keyset: Keyset,
        path_filter: "Optional[_PathFilter]",
        *,
        rel_path: str = "",
        callback: "Optional[ProgressCb]" = None,
        progress: "Optional[Dict[str, int]]" = None,
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

        When ``callback`` is supplied, emits ``restore_planning``
        events every PLANNING_TICK_FILES files counted so the TUI
        can show "Planning… 1234 files, 567 MB" instead of a
        silent stall during the pre-walk (which on a big SFTP
        backup can take tens of seconds before the first file is
        actually restored).
        """
        if path_filter is not None and not path_filter.descend(rel_path):
            return (0, 0)
        try:
            tree_bytes = self._fetch_tree_blob_cached(
                tree_blob_loc, keyset,
            )
        except (DecryptError, OSError, NotImplementedError, ValueError):
            return (0, 0)
        tree = parse_tree(tree_bytes)
        files = 0
        total_bytes = 0
        # Local progress accumulator threaded through the recursion
        # so the planning callback can report cumulative counts
        # (without it, every recursive call would only see its own
        # subtree's contribution).
        if progress is None:
            progress = {"files": 0, "bytes": 0, "ticks": 0}
        for child in tree.children:
            child_rel = (
                f"{rel_path}/{child.name}" if rel_path else child.name
            )
            if isinstance(child.node, TreeNode):
                f, b = self._count_tree(
                    child.node.treeBlobLoc, keyset, path_filter,
                    rel_path=child_rel,
                    callback=callback, progress=progress,
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
                progress["files"] += 1
                progress["bytes"] += int(child.node.itemSize or 0)
                # Drip a planning event every N files so the TUI
                # can update without re-rendering on every single
                # FileNode (which would be wasteful on big trees).
                if (
                    callback is not None
                    and progress["files"]
                    - progress["ticks"] * _PLANNING_TICK_FILES
                    >= _PLANNING_TICK_FILES
                ):
                    progress["ticks"] += 1
                    _emit(callback, "restore_planning",
                          files=progress["files"],
                          bytes=progress["bytes"])
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
                plain = _load_record_plain(
                    arqo, keyset, self.openssl_path,
                )
                rec = _parse_backuprecord(plain)
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

    def dry_run_restore(
        self,
        *,
        folder_uuid: str,
        computer_uuid: Optional[str] = None,
        backuprecord_path: Optional[str] = None,
        paths: Optional[List[str]] = None,
        callback: Optional[ProgressCb] = None,
    ) -> "DryRunRestoreResult":
        """Walk the backuprecord's tree + emit one
        ``would_restore_file`` event per file (and
        ``would_restore_dir`` per directory) WITHOUT writing
        anything to disk.

        Operators use this to verify ``paths=`` filtering,
        confirm a snapshot's contents before committing to a
        full restore, and answer "how big is this restore?"
        without paying for the file-blob reads.

        The walk fetches every tree blob (cheap; trees are
        small) but never fetches a file blob — the per-file
        ``itemSize`` from the FileNode is used as the
        authoritative reported size.

        Returns a :class:`DryRunRestoreResult` with
        ``files_listed`` / ``dirs_listed`` /
        ``bytes_would_restore`` / sample of the first 10 paths
        so a CLI can render a one-line summary without holding
        every event in memory.
        """
        if computer_uuid is None:
            computer_uuid = self._resolve_single_computer(folder_uuid)
        keyset = self.keyset(computer_uuid)
        if backuprecord_path is not None:
            record_path = backuprecord_path
        else:
            record_path = find_latest_backuprecord(
                self.backend, "/", computer_uuid, folder_uuid,
            )
        if record_path is None:
            raise ValueError(
                f"no backuprecord found for "
                f"{computer_uuid}/{folder_uuid}"
            )
        record_arqo = self.backend.read_all(record_path)
        record_plain = _load_record_plain(
            record_arqo, keyset, self.openssl_path,
        )
        record = _parse_backuprecord(record_plain)
        node_dict = record.get("node")
        if not isinstance(node_dict, dict):
            raise ValueError(
                "backuprecord missing or malformed `node` field"
            )
        path_filter = _build_path_filter(paths)
        result = DryRunRestoreResult(
            src=self.src, folder_uuid=folder_uuid,
            backuprecord_path=record_path,
        )
        _emit(callback, "dry_run_restore_started",
              path=record_path,
              computer=computer_uuid, folder=folder_uuid)
        if node_dict.get("isTree"):
            tree_loc = self._blobloc_from_dict(node_dict["treeBlobLoc"])
            self._dry_run_walk_tree(
                tree_loc, keyset, path_filter,
                rel_path="", result=result, callback=callback,
            )
        else:
            # Single-file root.
            if (
                path_filter is None
                or path_filter.matches("")
            ):
                size = int(node_dict.get("itemSize") or 0)
                result.files_listed += 1
                result.bytes_would_restore += size
                if len(result.sample_paths) < 10:
                    result.sample_paths.append("")
                _emit(callback, "would_restore_file",
                      rel_path="", size=size)
        _emit(callback, "dry_run_restore_finished",
              files=result.files_listed,
              dirs=result.dirs_listed,
              bytes=result.bytes_would_restore)
        return result

    def _dry_run_walk_tree(
        self,
        tree_loc: BlobLoc,
        keyset: Keyset,
        path_filter: "Optional[_PathFilter]",
        *,
        rel_path: str,
        result: "DryRunRestoreResult",
        callback: Optional[ProgressCb],
    ) -> None:
        if path_filter is not None and not path_filter.descend(rel_path):
            return
        try:
            tree_bytes = self._fetch_tree_blob_cached(tree_loc, keyset)
        except Exception as exc:
            _emit(callback, "dry_run_tree_error",
                  rel_path=rel_path, error=str(exc))
            return
        tree = parse_tree(tree_bytes)
        for child in tree.children:
            child_rel = (
                f"{rel_path}/{child.name}" if rel_path else child.name
            )
            if isinstance(child.node, TreeNode):
                result.dirs_listed += 1
                _emit(callback, "would_restore_dir",
                      rel_path=child_rel)
                self._dry_run_walk_tree(
                    child.node.treeBlobLoc, keyset, path_filter,
                    rel_path=child_rel, result=result, callback=callback,
                )
            elif isinstance(child.node, FileNode):
                if (
                    path_filter is not None
                    and not path_filter.matches(child_rel)
                ):
                    continue
                size = int(child.node.itemSize or 0)
                result.files_listed += 1
                result.bytes_would_restore += size
                if len(result.sample_paths) < 10:
                    result.sample_paths.append(child_rel)
                _emit(callback, "would_restore_file",
                      rel_path=child_rel, size=size)

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
        record_plain = _load_record_plain(
            record_arqo, keyset, self.openssl_path,
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
                    callback=callback,
                )
                _emit(callback, "restore_planned",
                      total_files=total_files,
                      total_bytes=total_bytes)
            # Build a minimal root TreeNode just so the recursion's
            # xattr-application + ACL-application steps have
            # something to read. Two fields are consumed at the
            # root level:
            #
            # - ``xattrsBlobLocs`` — covers the source-root dir's
            #   xattrs
            # - ``aclBlobLoc`` — covers the source-root dir's ACL
            #   (D2 added the JSON emit + A보완-1 closes the
            #   reader-side consumption gap here)
            #
            # Every other field is rebuilt from the freshly-parsed
            # Tree binaries below.
            root_xattr_locs = [
                self._blobloc_from_dict(b)
                for b in (node_dict.get("xattrsBlobLocs") or [])
            ]
            root_acl_dict = node_dict.get("aclBlobLoc")
            root_acl_loc = (
                self._blobloc_from_dict(root_acl_dict)
                if isinstance(root_acl_dict, dict) else None
            )
            root_tree_node = TreeNode(
                treeBlobLoc=tree_blob_loc,
                xattrsBlobLocs=root_xattr_locs,
                aclBlobLoc=root_acl_loc,
            )
            self._restore_dir_node(
                tree_blob_loc, out_dir, keyset, result, callback,
                rel_path="", path_filter=path_filter,
                tree_node=root_tree_node,
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
