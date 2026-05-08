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
