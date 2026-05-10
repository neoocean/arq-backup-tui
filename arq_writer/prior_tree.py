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


def _parse_record(plain):
    # Lazy import — backuprecord lives in the same package so a top-
    # level import would close the cycle through arq_writer/__init__.
    from arq_writer.backuprecord import parse_backuprecord
    return parse_backuprecord(plain)



@dataclass
class PriorMatch:
    """Result of comparing a source ``stat`` to a prior FileNode."""

    node: FileNode
    matches: bool


def _list_record_paths_for_folder(
    dest_root: Path,
    computer_uuid: str,
    folder_uuid: Optional[str],
    *,
    backend=None,
) -> list:
    """Return every backuprecord path under
    ``<dest>/<cu>/backupfolders/[<folder>/]backuprecords/<bucket>/<num>.backuprecord``.

    With ``backend`` set, traversal uses ``list_dir`` / ``is_dir`` so
    SFTP destinations work; otherwise falls back to local
    ``os.scandir``.
    """
    if backend is not None:
        bf_rel = f"/{computer_uuid}/{BACKUPFOLDERS_DIR}"
        if not backend.is_dir(bf_rel):
            return []
        out = []
        try:
            folders = (
                [folder_uuid] if folder_uuid is not None
                else [
                    f for f in backend.list_dir(bf_rel)
                    if backend.is_dir(f"{bf_rel}/{f}")
                ]
            )
        except Exception:
            return []
        for f in folders:
            rec_root_rel = f"{bf_rel}/{f}/{BACKUPRECORDS_DIR}"
            if not backend.is_dir(rec_root_rel):
                continue
            try:
                buckets = backend.list_dir(rec_root_rel)
            except Exception:
                continue
            for bucket in buckets:
                bucket_rel = f"{rec_root_rel}/{bucket}"
                if not backend.is_dir(bucket_rel):
                    continue
                try:
                    recs = backend.list_dir(bucket_rel)
                except Exception:
                    continue
                for rec in recs:
                    if rec.endswith(".backuprecord"):
                        out.append(Path(f"{bucket_rel}/{rec}"))
        return out
    # Local fallback path.
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
    *, openssl_path: str = "openssl", backend=None,
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
        if backend is not None:
            arqo = backend.read_all(str(rec_path))
        else:
            arqo = rec_path.read_bytes()
        plist_bytes = decrypt_lz4_arqo(
            arqo, encryption_key, hmac_key,
            openssl_path=openssl_path,
        )
        record = _parse_record(plist_bytes)
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


# Default upper bound on the number of decoded trees held in the
# PriorTreeIndex cache. Tree objects are typically a few KB each
# (small lists of child references), so 1024 ≈ low single-digit MB
# in the worst case + comfortably covers most plan-walks. Operators
# with truly massive destinations can override via the
# ARQ_PRIOR_TREE_CACHE_MAX env var or the ``max_cache_trees`` ctor
# kwarg.
_DEFAULT_PRIOR_TREE_CACHE_MAX = 1024


class PriorTreeIndex:
    """Lazy, path-keyed view over a prior backup's tree.

    Construction picks the most recent backuprecord for the
    requested ``folder_uuid`` and decrypts its envelope to learn
    the root tree's BlobLoc. Subsequent ``lookup_file`` calls walk
    down lazily, fetching exactly the tree blobs on the lookup path.

    The decoded-tree cache is bounded LRU: once it reaches
    ``max_cache_trees`` entries, the least-recently-used tree
    is evicted on each new fetch. Without this, walking a backup
    against a destination with hundreds of thousands of trees
    would accumulate every Tree in memory for the duration of the
    walk; with it, memory stays roughly proportional to the cap
    regardless of destination size.

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
        backend=None,
        max_cache_trees: Optional[int] = None,
    ) -> None:
        self.dest_root = Path(dest_root)
        self.computer_uuid = computer_uuid
        self.encryption_key = encryption_key
        self.hmac_key = hmac_key
        self.openssl_path = openssl_path
        self.backend = backend
        # LRU-bounded tree cache. OrderedDict gives O(1) move-to-end
        # + popitem(last=False) for the eviction path.
        from collections import OrderedDict
        self._tree_cache: "OrderedDict[str, Tree]" = OrderedDict()
        self._max_cache = self._resolve_cache_max(max_cache_trees)
        self.cache_evictions = 0    # operator-facing diagnostic
        self._root_tree_loc: Optional[BlobLoc] = None

        recs = _list_record_paths_for_folder(
            self.dest_root, computer_uuid, folder_uuid,
            backend=backend,
        )
        if not recs:
            return
        # Lexicographic max on (bucket, filename) corresponds to
        # chronological max because both encode creation_date.
        rec = max(recs, key=lambda p: (p.parent.name, p.name))
        self._root_tree_loc = _load_prior_root_tree_loc(
            rec, encryption_key, hmac_key,
            openssl_path=openssl_path,
            backend=backend,
        )

    @staticmethod
    def _resolve_cache_max(override: Optional[int]) -> int:
        """Pick the LRU cap. Explicit ctor arg wins; else the
        ARQ_PRIOR_TREE_CACHE_MAX env var (so operators can tune
        without a code change); else the module default. Values
        <=0 disable the cap entirely (legacy unbounded behaviour
        — useful as an escape hatch but not the default)."""
        if override is not None:
            return int(override)
        env = os.environ.get("ARQ_PRIOR_TREE_CACHE_MAX")
        if env:
            try:
                return int(env)
            except ValueError:
                pass
        return _DEFAULT_PRIOR_TREE_CACHE_MAX

    @property
    def is_usable(self) -> bool:
        return self._root_tree_loc is not None

    @property
    def cache_size(self) -> int:
        """Current number of decoded trees held in memory."""
        return len(self._tree_cache)

    def _fetch_tree(self, loc: BlobLoc) -> Optional[Tree]:
        cached = self._tree_cache.get(loc.blobIdentifier)
        if cached is not None:
            # LRU bookkeeping: bump to most-recent on hit so
            # frequently-accessed trees stay resident.
            self._tree_cache.move_to_end(loc.blobIdentifier)
            return cached
        try:
            from arq_reader.decrypt import (
                decrypt_encrypted_object,
            )
            from arq_writer.lz4_block import lz4_unwrap
        except ImportError:  # pragma: no cover
            return None
        try:
            if self.backend is not None:
                if loc.isPacked:
                    raw = self.backend.read_range(
                        loc.relativePath, loc.offset, loc.length,
                    )
                else:
                    raw = self.backend.read_all(loc.relativePath)
            elif loc.isPacked:
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
        # Evict the oldest entry once we exceed the cap. The cap
        # may be 0 / negative — interpreted as "unbounded" for
        # operators who explicitly want legacy behaviour.
        if self._max_cache > 0:
            while len(self._tree_cache) > self._max_cache:
                self._tree_cache.popitem(last=False)
                self.cache_evictions += 1
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
    file contents.

    Resolves ``username`` / ``groupName`` from the system's
    ``pwd`` / ``grp`` modules so the reused FileNode is byte-
    equivalent to one a fresh ``Backup._walk_file`` call would
    produce. Without this, a re-run with ``dedup_against_existing``
    serializes Trees whose binary differs from run 1 only in the
    owner-name strings → fresh tree blob_id → no dedup at the
    Tree level.

    Also carries over ``xattrsBlobLocs`` and ``aclBlobLoc`` —
    omitting them was a real correctness bug (a re-run via
    ``dedup_against_existing`` would silently drop the file's
    xattrs from the restored copy) AND a dedup-defeating bug
    (fresh-walk FileNode carries those refs, reused FileNode
    didn't → different serialized bytes → different tree blob_id).
    The bug only surfaced on macOS in unit tests because Sequoia ≥
    auto-attaches ``com.apple.provenance`` to every file the
    kernel sees written, so test files always have a non-empty
    xattr blob; Linux test files typically had no xattrs at all
    so the fields were both empty either way.
    """
    from .backup import _resolve_owner
    uid = int(src_stat.st_uid) if hasattr(src_stat, "st_uid") else 0
    gid = int(src_stat.st_gid) if hasattr(src_stat, "st_gid") else 0
    uname, gname = _resolve_owner(uid, gid)
    return FileNode(
        dataBlobLocs=list(prior.dataBlobLocs),
        xattrsBlobLocs=list(prior.xattrsBlobLocs or []),
        aclBlobLoc=prior.aclBlobLoc,
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
        username=uname,
        groupName=gname,
        mac_st_mode=int(src_stat.st_mode),
        mac_st_uid=uid,
        mac_st_gid=gid,
        mac_st_ino=int(src_stat.st_ino),
        mac_st_nlink=int(src_stat.st_nlink),
    )
