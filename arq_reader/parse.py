"""Binary parsers for Arq 7's ``Node`` / ``Tree`` / ``BlobLoc``.

Inverse of :mod:`arq_writer.serialize`. The byte conventions come
from the Arq 7 spec (8-byte network-byte-order ``[String]`` length,
1-byte ``[Bool]``, 4-byte ``[UInt32]`` etc.).

All parsers raise :class:`ValueError` on malformed input — the caller
is expected to wrap that in a higher-level error if needed.
"""

from __future__ import annotations

import struct
from typing import List, Optional

from arq_writer.constants import NODE_REPARSE_FIELDS_MIN_TREE_VERSION
from arq_writer.types import BlobLoc, FileNode, Tree, TreeChild, TreeNode


class BinaryReader:
    """Tiny stateful reader over a bytes buffer.

    Tracks ``pos`` so unit tests / debuggers can pinpoint where parses
    fail. ``remaining()`` is handy when a parser optionally consumes
    trailing fields (e.g. Tree v2+ Node reparse fields).
    """

    __slots__ = ("data", "pos")

    def __init__(self, data: bytes, pos: int = 0) -> None:
        self.data = data
        self.pos = pos

    def remaining(self) -> int:
        return len(self.data) - self.pos

    def _need(self, n: int) -> None:
        if self.pos + n > len(self.data):
            raise ValueError(
                f"unexpected EOF at pos={self.pos}: need {n} bytes, "
                f"have {self.remaining()}"
            )

    def read_bool(self) -> bool:
        self._need(1)
        v = self.data[self.pos]
        self.pos += 1
        return v != 0

    def read_uint32(self) -> int:
        self._need(4)
        v = struct.unpack(">I", self.data[self.pos : self.pos + 4])[0]
        self.pos += 4
        return v

    def read_int32(self) -> int:
        self._need(4)
        v = struct.unpack(">i", self.data[self.pos : self.pos + 4])[0]
        self.pos += 4
        return v

    def read_uint64(self) -> int:
        self._need(8)
        v = struct.unpack(">Q", self.data[self.pos : self.pos + 8])[0]
        self.pos += 8
        return v

    def read_int64(self) -> int:
        self._need(8)
        v = struct.unpack(">q", self.data[self.pos : self.pos + 8])[0]
        self.pos += 8
        return v

    def read_raw(self, n: int) -> bytes:
        """Consume ``n`` raw bytes without interpreting them. Used
        for opaque trailing fields whose precise structure isn't
        known yet (e.g. Tree v4's per-node 38-byte extension)."""
        self._need(n)
        out = self.data[self.pos : self.pos + n]
        self.pos += n
        return out

    def read_string(self) -> Optional[str]:
        self._need(1)
        is_not_null = self.data[self.pos]
        self.pos += 1
        if is_not_null == 0:
            return None
        if is_not_null != 1:
            raise ValueError(
                f"bad [String] isNotNull byte: {is_not_null} at pos="
                f"{self.pos - 1}"
            )
        length = self.read_uint64()
        self._need(length)
        s = self.data[self.pos : self.pos + length].decode(
            "utf-8", errors="replace",
        )
        self.pos += length
        return s


# ---------------------------------------------------------------------------
# BlobLoc
# ---------------------------------------------------------------------------


def parse_blobloc(reader: BinaryReader) -> BlobLoc:
    """Parse one binary BlobLoc entry.

    The actual on-disk Arq 7 BlobLoc layout (discovered by walking
    real Arq.app-produced trees over a Hetzner Storage Box) carries
    an extra ``isLargePack`` boolean between ``isPacked`` and
    ``relativePath`` — the published spec / our earlier writer
    omitted it. Without consuming this byte every downstream field
    shifts by one and the parser explodes mid-string at the next
    ``read_string``. ``isLargePack`` distinguishes
    ``largeblobpacks/`` blobs from ordinary ``treepacks/`` /
    ``blobpacks/`` ones; the reader currently surfaces it via the
    ``BlobLoc.isLargePack`` attribute and downstream code routes
    largeblobpack reads the same way as regular pack reads.
    """
    blob_id = reader.read_string()
    is_packed = reader.read_bool()
    is_large_pack = reader.read_bool()
    rel_path = reader.read_string()
    offset = reader.read_uint64()
    length = reader.read_uint64()
    stretch = reader.read_bool()
    compression = reader.read_uint32()
    return BlobLoc(
        blobIdentifier=blob_id or "",
        isPacked=is_packed,
        isLargePack=is_large_pack,
        relativePath=rel_path or "",
        offset=offset,
        length=length,
        stretchEncryptionKey=stretch,
        compressionType=compression,
    )


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------


def parse_node(reader: BinaryReader, *, tree_version: int):
    """Parse a Node (file or directory).

    Returns either a :class:`FileNode` or :class:`TreeNode`. The
    discriminator is the first byte (``isTree``).
    """
    is_tree = reader.read_bool()
    tree_blob_loc: Optional[BlobLoc] = None
    if is_tree:
        tree_blob_loc = parse_blobloc(reader)
    computer_os = reader.read_uint32()
    data_count = reader.read_uint64()
    data_locs: List[BlobLoc] = [
        parse_blobloc(reader) for _ in range(data_count)
    ]
    acl_present = reader.read_bool()
    acl_loc: Optional[BlobLoc] = parse_blobloc(reader) if acl_present else None
    xattr_count = reader.read_uint64()
    xattr_locs: List[BlobLoc] = [
        parse_blobloc(reader) for _ in range(xattr_count)
    ]
    item_size = reader.read_uint64()
    contained = reader.read_uint64()
    mtime_sec = reader.read_int64()
    mtime_nsec = reader.read_int64()
    ctime_sec = reader.read_int64()
    ctime_nsec = reader.read_int64()
    create_sec = reader.read_int64()
    create_nsec = reader.read_int64()
    username = reader.read_string()
    group_name = reader.read_string()
    deleted = reader.read_bool()
    mac_st_dev = reader.read_int32()
    mac_st_ino = reader.read_uint64()
    mac_st_mode = reader.read_uint32()
    mac_st_nlink = reader.read_uint32()
    mac_st_uid = reader.read_uint32()
    mac_st_gid = reader.read_uint32()
    mac_st_rdev = reader.read_int32()
    mac_st_flags = reader.read_int32()
    win_attrs = reader.read_uint32()

    win_reparse_tag = 0
    win_reparse_is_dir = False
    if tree_version >= NODE_REPARSE_FIELDS_MIN_TREE_VERSION:
        win_reparse_tag = reader.read_uint32()
        win_reparse_is_dir = reader.read_bool()

    # Tree v4 added a 38-byte trailing block per Node whose internal
    # structure isn't documented in the public spec. From sampling
    # 30 Nodes on a real Hetzner destination via
    # ``scripts/probe_tree_v4_block.py`` we observed two shapes:
    #
    # - ALL-ZERO (3/30 cases — files where mtime == ctime ==
    #   create_sec, freshly created in the latest backup pass).
    # - Structured (27/30 cases). Within those:
    #     bytes  0..7  int64 BE    timestamp (sec) — values fall in
    #                              the ~7-minute window of the backup
    #                              pass that recorded this Node, NOT
    #                              the file's own mtime/ctime/create
    #     bytes  8..15 int64 BE    timestamp (nsec)
    #     bytes 16..23 int64 BE    constant 0x0000_0000_0100_0000 (a
    #                              present-flag / version marker?)
    #     bytes 24..37 14 zero bytes (reserved)
    #
    # Best guess: a "scanned-at" / "lastVerifiedAt" timestamp the v4
    # writer added to support Arq.app's repair / re-verification
    # heuristics. Confirmation requires Mach-O RE of Arq.app, which
    # is a follow-up task.
    #
    # Until that lands we keep skipping the block as opaque so
    # parsing continues to align with subsequent children. Tests in
    # ``tests/integration/test_arq_real_destination_deep.py``
    # exercise both shapes against the real destination.
    if tree_version >= 4:
        reader.read_raw(38)

    common = dict(
        itemSize=item_size,
        containedFilesCount=contained,
        mtime_sec=mtime_sec, mtime_nsec=mtime_nsec,
        ctime_sec=ctime_sec, ctime_nsec=ctime_nsec,
        create_time_sec=create_sec, create_time_nsec=create_nsec,
        username=username, groupName=group_name,
        deleted=deleted,
        mac_st_dev=mac_st_dev, mac_st_ino=mac_st_ino,
        mac_st_mode=mac_st_mode, mac_st_nlink=mac_st_nlink,
        mac_st_uid=mac_st_uid, mac_st_gid=mac_st_gid,
        mac_st_rdev=mac_st_rdev, mac_st_flags=mac_st_flags,
        win_attrs=win_attrs,
        win_reparse_tag=win_reparse_tag,
        win_reparse_point_is_directory=win_reparse_is_dir,
        computerOSType=computer_os,
        aclBlobLoc=acl_loc,
        xattrsBlobLocs=xattr_locs,
    )
    if is_tree:
        assert tree_blob_loc is not None
        return TreeNode(treeBlobLoc=tree_blob_loc, **common)
    return FileNode(dataBlobLocs=data_locs, **common)


# ---------------------------------------------------------------------------
# Tree
# ---------------------------------------------------------------------------


def parse_tree(data: bytes) -> Tree:
    """Parse a Tree's binary form (typically returned by
    ``decrypt_lz4_arqo`` for a treeBlobLoc target).
    """
    r = BinaryReader(data)
    version = r.read_uint32()
    count = r.read_uint64()
    children: List[TreeChild] = []
    for _ in range(count):
        name = r.read_string() or ""
        node = parse_node(r, tree_version=version)
        children.append(TreeChild(name=name, node=node))
    return Tree(children=children, version=version)
