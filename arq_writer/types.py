"""Dataclasses mirroring Arq 7's ``Node`` / ``Tree`` / ``BlobLoc`` types.

All field names match the names the official Arq 7 spec uses, so a
reader who has the spec open can recognize them at a glance. Default
values mirror what Arq.app fills in for files / directories that don't
have the corresponding metadata.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Union

from .constants import COMPRESSION_LZ4


@dataclass
class BlobLoc:
    """Pointer to a blob's bytes inside a pack file or standalone object.

    For the v0 writer ``isPacked`` is always ``False`` and
    ``relativePath`` points at a file under
    ``standardobjects/<2-hex-shard>/<62-hex-blobid>``.
    """

    blobIdentifier: str            # SHA-256 hex (lowercase)
    isPacked: bool = False
    # Distinguishes ``largeblobpacks/`` blobs from ``treepacks/`` /
    # ``blobpacks/`` ones. The on-disk binary layout puts this flag
    # immediately after ``isPacked``; mirrored on the JSON side as
    # ``isLargePack``. Defaults to False so writers that don't route
    # to largeblobpacks produce the same byte sequence as before.
    isLargePack: bool = False
    relativePath: str = ""
    offset: int = 0
    length: int = 0
    stretchEncryptionKey: bool = True
    compressionType: int = COMPRESSION_LZ4


@dataclass
class FileNode:
    """``Node`` describing a regular file (``isTree = false``)."""

    dataBlobLocs: List[BlobLoc] = field(default_factory=list)
    itemSize: int = 0
    containedFilesCount: int = 1
    mtime_sec: int = 0
    mtime_nsec: int = 0
    ctime_sec: int = 0
    ctime_nsec: int = 0
    create_time_sec: int = 0
    create_time_nsec: int = 0
    username: Optional[str] = None
    groupName: Optional[str] = None
    deleted: bool = False
    mac_st_dev: int = 0
    mac_st_ino: int = 0
    mac_st_mode: int = 0
    mac_st_nlink: int = 1
    mac_st_uid: int = 0
    mac_st_gid: int = 0
    mac_st_rdev: int = 0
    mac_st_flags: int = 0
    win_attrs: int = 0
    win_reparse_tag: int = 0
    win_reparse_point_is_directory: bool = False
    computerOSType: int = 1   # 1 = macOS, 2 = Windows
    aclBlobLoc: Optional[BlobLoc] = None
    xattrsBlobLocs: List[BlobLoc] = field(default_factory=list)
    # Tree v4 38-byte trailing block. Preserved verbatim through
    # parse so a round-trip ``parse_tree → write_tree`` is binary-
    # identical to Arq.app v8's emit (Strategy F-1 verified
    # 2026-05-10 against /Volumes/arqbackup1).
    #
    # The block's full structure is only partly understood — sec
    # and nsec fields plus a ``0x01`` present-flag at byte 16 are
    # documented in ``docs/REAL-DATA-DISCOVERIES.md`` §7, but
    # bytes 12-15 carry a per-Node varying value (looks like a
    # monotonic counter or sequence number from sample inspection)
    # that doesn't fit a simple semantic mapping. Preserving raw
    # bytes is therefore safer than re-synthesising structure.
    #
    # When the parser observed a non-zero block, ``v4_trailing_block``
    # holds the exact 38 bytes. When it observed all zeros (the
    # shape Arq.app uses for files freshly added to a pass),
    # ``v4_trailing_block`` is ``b"\x00" * 38``. An empty bytes
    # value means "no parser ran" — the writer's fresh-walk path
    # then falls back to synthesising the structured form using
    # the scanned-at hints below (and finally ``time.time_ns()``).
    v4_trailing_block: bytes = b""

    # Optional scanned-at override for the v4 trailing block bytes
    # 0..15. Strategy K (2026-05-11) established these bytes are
    # the backup engine's walk-time of this entry, not a file
    # timestamp; integration tests / a future scan-loop integration
    # can pin them by setting these explicitly. When both are 0 the
    # writer falls back to ``time.time_ns()``.
    v4_scanned_at_sec: int = 0
    v4_scanned_at_nsec: int = 0


@dataclass
class TreeNode:
    """``Node`` describing a directory (``isTree = true``)."""

    treeBlobLoc: BlobLoc
    itemSize: int = 0
    containedFilesCount: int = 0
    mtime_sec: int = 0
    mtime_nsec: int = 0
    ctime_sec: int = 0
    ctime_nsec: int = 0
    create_time_sec: int = 0
    create_time_nsec: int = 0
    username: Optional[str] = None
    groupName: Optional[str] = None
    deleted: bool = False
    mac_st_dev: int = 0
    mac_st_ino: int = 0
    mac_st_mode: int = 0o040755
    mac_st_nlink: int = 1
    mac_st_uid: int = 0
    mac_st_gid: int = 0
    mac_st_rdev: int = 0
    mac_st_flags: int = 0
    win_attrs: int = 0
    win_reparse_tag: int = 0
    win_reparse_point_is_directory: bool = False
    computerOSType: int = 1
    aclBlobLoc: Optional[BlobLoc] = None
    xattrsBlobLocs: List[BlobLoc] = field(default_factory=list)
    # See FileNode for trailing-block field semantics.
    v4_trailing_block: bytes = b""
    v4_scanned_at_sec: int = 0
    v4_scanned_at_nsec: int = 0


# Convenient union for "either kind of Node". Most APIs accept either
# transparently.
Node = Union[FileNode, TreeNode]


@dataclass
class Tree:
    """``Tree`` — directory metadata + child Nodes by name."""

    children: List["TreeChild"] = field(default_factory=list)
    version: int = 0   # filled in by serializer with TREE_VERSION


@dataclass
class TreeChild:
    name: str
    node: Node
