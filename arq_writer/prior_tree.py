"""Lazy index over a prior backup's tree, keyed by source-relative path.

This is the "tree-walk reuse" half of cross-run dedup. The
``arq_writer.dedup`` module makes sure the second run *writes* fewer
bytes (cache hits skip re-encryption); this module makes sure the
second run *reads* fewer bytes — files whose ``stat`` triple
(mtime, size, mode) hasn't changed since the prior backup never get
opened, never get hashed, never run through the chunker.

Mechanics:

1. Locate the most recent backuprecord for the requested folder
   (or the most recent across all folders if ``folder_uuid`` is
   omitted).
2. Decrypt + LZ4-unwrap + plist-parse the record. The top-level
   ``node`` dict is the source-root TreeNode; its ``treeBlobLoc``
   points at the actual children-bearing Tree blob.
3. Lazily walk Tree blobs on demand: ``lookup_file("a/b/c.txt")``
   fetches each containing tree blob via ``arq_reader.parse``,
   caches it by its ``blobIdentifier``, and recurses until it hits
   a FileNode or determines the path is missing.

Cache strategy: parsed trees are cached forever (one ``Tree``
object per unique ``blobIdentifier``). Path lookups for a file
under the same directory therefore share the same parsed tree.

The class is read-only and side-effect-free except for the
internal cache; safe to share across threads but not particularly
useful to (the bottleneck is single-threaded I/O on the prior
backup's blob files).
"""

from __future__ import annotations

import os
import plistlib
import stat as stat_mod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from arq_reader.parse import parse_tree
from arq_writer.types import BlobLoc, FileNode, Tree, TreeChild, TreeNode

from .constants import BACKUPFOLDERS_DIR, BACKUPRECORDS_DIR


@dataclass
class PriorMatch:
    """Result of comparing a source ``stat`` to a prior FileNode."""

    node: FileNode
    matches: bool


def _list_record_paths_for_folder(
    dest_root: Path,
    computer_uuid: str,
    folder_uuid: Optional[str],
) -> list:
    """Return every backuprecord path under
    ``<dest>/<cu>/backupfolders/[<folder>/]backuprecords/<bucket>/<num>.backuprecord``.
    """
    bf_root = (
        Path(dest_root) / computer_uuid / BACKUPFOLDERS_DIR
    )
    if not bf_root.is_dir():
        return []
    out = []
    folder_iter = (
        [bf_root / folder_uuid] if folder_uuid is not None
        else [Path(e.path) for e in os.scandir(bf_root) if e.is_dir()]
    )
    for fdir in folder_iter:
        rec_root = fdir / BACKUPRECORDS_DIR
        if not rec_root.is_dir():
            continue
        try:
            for bucket in os.scandir(rec_root):
                if not bucket.is_dir():
                    continue
                for rec in os.scandir(bucket.path):
                    if rec.is_file() and rec.name.endswith(".backuprecord"):
                        out.append(Path(rec.path))
        except OSError:
            continue
    return out


def _load_prior_root_tree_loc(
    rec_path: Path, encryption_key: bytes, hmac_key: bytes,
    *, openssl_path: str = "openssl",
) -> Optional[BlobLoc]:
    """Decrypt the backuprecord and return its root ``treeBlobLoc``.

    Returns ``None`` on any failure — the caller falls through to a
    full walk, no harm done.
    """
    try:
        from arq_reader.decrypt import decrypt_lz4_arqo
    except ImportError:  # pragma: no cover
        return None
    try:
        plist_bytes = decrypt_lz4_arqo(
            rec_path.read_bytes(), encryption_key, hmac_key,
            openssl_path=openssl_path,
        )
        record = plistlib.loads(plist_bytes)
    except Exception:
        return None
    if not isinstance(record, dict):
        return None
    node = record.get("node")
    if not isinstance(node, dict):
        return None
    if not node.get("isTree"):
        # Source root was a single file; nothing to lazily walk into.
        return None
    tloc = node.get("treeBlobLoc")
    if not isinstance(tloc, dict):
        return None
    try:
        return BlobLoc(
            blobIdentifier=str(tloc["blobIdentifier"]),
            isPacked=bool(tloc.get("isPacked", False)),
            relativePath=str(tloc.get("relativePath", "")),
            offset=int(tloc.get("offset", 0)),
            length=int(tloc.get("length", 0)),
            stretchEncryptionKey=bool(
                tloc.get("stretchEncryptionKey", True)
            ),
            compressionType=int(tloc.get("compressionType", 2)),
        )
    except (KeyError, TypeError, ValueError):
        return None


class PriorTreeIndex:
    """Lazy, path-keyed view over a prior backup's tree.

    Construction picks the most recent backuprecord for the
    requested ``folder_uuid`` and decrypts its envelope to learn
    the root tree's BlobLoc. Subsequent ``lookup_file`` calls walk
    down lazily, fetching exactly the tree blobs on the lookup path.

    Use as a context-free helper; the class only caches reads, it
    never writes.
    """

    def __init__(
        self,
        dest_root: Path,
        computer_uuid: str,
        encryption_key: bytes,
        hmac_key: bytes,
        *,
        folder_uuid: Optional[str] = None,
        openssl_path: str = "openssl",
    ) -> None:
        self.dest_root = Path(dest_root)
        self.computer_uuid = computer_uuid
        self.encryption_key = encryption_key
        self.hmac_key = hmac_key
        self.openssl_path = openssl_path
        self._tree_cache: Dict[str, Tree] = {}
        self._root_tree_loc: Optional[BlobLoc] = None

        recs = _list_record_paths_for_folder(
            self.dest_root, computer_uuid, folder_uuid,
        )
        if not recs:
            return
        # Lexicographic max on (bucket, filename) corresponds to
        # chronological max because both encode creation_date.
        rec = max(recs, key=lambda p: (p.parent.name, p.name))
        self._root_tree_loc = _load_prior_root_tree_loc(
            rec, encryption_key, hmac_key,
            openssl_path=openssl_path,
        )

    @property
    def is_usable(self) -> bool:
        return self._root_tree_loc is not None

    def _fetch_tree(self, loc: BlobLoc) -> Optional[Tree]:
        cached = self._tree_cache.get(loc.blobIdentifier)
        if cached is not None:
            return cached
        try:
            from arq_reader.decrypt import (
                decrypt_encrypted_object,
            )
            from arq_writer.lz4_block import lz4_unwrap
        except ImportError:  # pragma: no cover
            return None
        try:
            if loc.isPacked:
                with open(
                    self.dest_root.joinpath(
                        loc.relativePath.lstrip("/")
                    ), "rb",
                ) as f:
                    f.seek(loc.offset)
                    raw = f.read(loc.length)
            else:
                raw = self.dest_root.joinpath(
                    loc.relativePath.lstrip("/")
                ).read_bytes()
            if raw[:4] == b"ARQO":
                raw = decrypt_encrypted_object(
                    raw, self.encryption_key, self.hmac_key,
                    openssl_path=self.openssl_path,
                )
            if loc.compressionType == 2:
                raw = lz4_unwrap(raw)
            tree = parse_tree(raw)
        except Exception:
            return None
        self._tree_cache[loc.blobIdentifier] = tree
        return tree

    def lookup_file(self, rel_path: str) -> Optional[FileNode]:
        """Return the prior FileNode at ``rel_path`` (a forward-slash-
        separated, source-relative path) or ``None`` if the prior
        tree has no entry for that path or the entry isn't a file.
        """
        if not self.is_usable:
            return None
        parts = [p for p in rel_path.split("/") if p]
        if not parts:
            return None
        # Walk down: each non-final segment must be a TreeNode.
        loc = self._root_tree_loc
        for seg in parts[:-1]:
            tree = self._fetch_tree(loc) if loc is not None else None
            if tree is None:
                return None
            child = _child_named(tree, seg)
            if child is None or not isinstance(child.node, TreeNode):
                return None
            loc = child.node.treeBlobLoc
        # Final segment must resolve to a FileNode.
        tree = self._fetch_tree(loc) if loc is not None else None
        if tree is None:
            return None
        leaf = _child_named(tree, parts[-1])
        if leaf is None or not isinstance(leaf.node, FileNode):
            return None
        return leaf.node

    def stat_matches(
        self, rel_path: str, source_stat: os.stat_result,
    ) -> Optional[FileNode]:
        """Return the prior FileNode iff a stat-based identity check
        passes — same mtime (sec + nsec), same size, same mode bits
        (mode & 0o7777, ignoring file-type flags).

        Returns ``None`` on any miss; the caller must walk the file
        normally in that case.
        """
        prior = self.lookup_file(rel_path)
        if prior is None:
            return None
        if not _stat_matches(source_stat, prior):
            return None
        return prior


def _child_named(tree: Tree, name: str) -> Optional[TreeChild]:
    for c in tree.children:
        if c.name == name:
            return c
    return None


def _stat_matches(st: os.stat_result, prior: FileNode) -> bool:
    """True iff ``st`` looks like the file represented by ``prior``.

    Compares: itemSize, mtime_sec, mtime_nsec, and the permission
    bits of mac_st_mode. ctime is intentionally excluded — it
    changes on metadata-only edits (e.g. ``chown``), which don't
    invalidate the file's content blobs.
    """
    if int(st.st_size) != int(prior.itemSize):
        return False
    if int(st.st_mtime) != int(prior.mtime_sec):
        return False
    src_nsec = int((st.st_mtime - int(st.st_mtime)) * 1_000_000_000)
    if src_nsec != int(prior.mtime_nsec):
        return False
    src_mode = stat_mod.S_IMODE(int(st.st_mode))
    prior_mode = stat_mod.S_IMODE(int(prior.mac_st_mode))
    if src_mode != prior_mode:
        return False
    return True


def reuse_file_node_for(
    src_stat: os.stat_result, prior: FileNode,
) -> FileNode:
    """Build a FileNode that shares the prior blob locations but
    reflects a fresh ``stat`` snapshot of the source — preserves
    inode-level metadata changes (uid/gid/nlink) without re-reading
    file contents."""
    return FileNode(
        dataBlobLocs=list(prior.dataBlobLocs),
        itemSize=int(prior.itemSize),
        containedFilesCount=1,
        mtime_sec=int(src_stat.st_mtime),
        mtime_nsec=int(
            (src_stat.st_mtime - int(src_stat.st_mtime)) * 1_000_000_000
        ),
        ctime_sec=int(src_stat.st_ctime),
        ctime_nsec=int(
            (src_stat.st_ctime - int(src_stat.st_ctime)) * 1_000_000_000
        ),
        mac_st_mode=int(src_stat.st_mode),
        mac_st_uid=int(src_stat.st_uid) if hasattr(src_stat, "st_uid") else 0,
        mac_st_gid=int(src_stat.st_gid) if hasattr(src_stat, "st_gid") else 0,
        mac_st_ino=int(src_stat.st_ino),
        mac_st_nlink=int(src_stat.st_nlink),
    )
