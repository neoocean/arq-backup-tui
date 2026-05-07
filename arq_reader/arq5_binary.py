"""Arq 5 / Arq 6 ``Tree`` / ``Node`` / ``Commit`` / ``BlobKey`` parsers.

Format reverse-engineered from two cross-checked sources:

- ``arq5_data_format.txt`` — the published spec.
- ``arq_restore/repo/Tree.m`` / ``Node.m`` / ``Commit.m`` / ``BlobKeyIO.m``
  — the reference Objective-C reader.

Where the spec and the source disagreed on optional-field gating
(specifically, the v19+ vs v20+ compression-type fields), the source
wins; the spec is informational. The parsers here accept all
documented Tree versions 10–22 (skipping v13, which arq_restore
explicitly rejects) and Commit versions 3–12.

This module deliberately mirrors arq_restore's branching rather than
rewriting it cleanly: an Arq 5 backup the operator hands us must be
parsed exactly the way the official reader parses it, even where the
choices look idiosyncratic.

Public API:

    BinaryStream(buf)              — small reader with primitive ops.
    BlobKey                        — dataclass for an Arq 5 blob handle.
    Arq5Node                       — dataclass for one file/dir entry.
    Arq5Tree                       — top-level dir entry holding nodes.
    Arq5Commit                     — root metadata pointing at a Tree.
    parse_blobkey(stream, tv)      — version-gated BlobKey read.
    parse_node(stream, tv)         — version-gated Node read.
    parse_tree(data)               — full ``TreeVNNN`` parse.
    parse_commit(data)             — full ``CommitVNNN`` parse.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional


TREE_HEADER_LENGTH = 8                    # "TreeVNNN" or shorter padded
COMMIT_HEADER_LENGTH = 10                 # "CommitVNNN"

MIN_SUPPORTED_TREE_VERSION = 10
INVALID_TREE_VERSION = 13                 # arq_restore explicitly rejects
MAX_KNOWN_TREE_VERSION = 22

MIN_SUPPORTED_COMMIT_VERSION = 3
MAX_KNOWN_COMMIT_VERSION = 12

COMPRESSION_NONE = 0
COMPRESSION_GZIP = 1
COMPRESSION_LZ4 = 2


class Arq5ParseError(ValueError):
    """Raised when the input doesn't match the Arq 5 spec."""


# ---------------------------------------------------------------------------
# BinaryStream — primitive readers
# ---------------------------------------------------------------------------


class BinaryStream:
    """Stateful byte reader for Arq 5/6 binary records.

    Mirrors arq_restore's IntegerIO / StringIO / BooleanIO / DateIO
    primitives. Every reader advances ``pos`` and raises
    :class:`Arq5ParseError` on truncation.
    """

    __slots__ = ("data", "pos")

    def __init__(self, data: bytes, pos: int = 0) -> None:
        self.data = data
        self.pos = pos

    def remaining(self) -> int:
        return len(self.data) - self.pos

    def _need(self, n: int) -> None:
        if self.pos + n > len(self.data):
            raise Arq5ParseError(
                f"unexpected EOF at pos={self.pos}: need {n} bytes, "
                f"have {self.remaining()}"
            )

    def read_bool(self) -> bool:
        self._need(1)
        v = self.data[self.pos]
        self.pos += 1
        if v not in (0, 1):
            raise Arq5ParseError(f"bad Bool {v} at pos={self.pos - 1}")
        return v == 1

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

    def read_string(self) -> Optional[str]:
        """Returns ``None`` for null strings, otherwise UTF-8 text."""
        self._need(1)
        flag = self.data[self.pos]
        self.pos += 1
        if flag == 0:
            return None
        if flag != 1:
            raise Arq5ParseError(f"bad String flag {flag} at pos={self.pos - 1}")
        length = self.read_uint64()
        self._need(length)
        s = self.data[self.pos : self.pos + length].decode(
            "utf-8", errors="replace",
        )
        self.pos += length
        return s

    def read_date(self) -> Optional[datetime]:
        """``Date`` = 1-byte not-null flag + 8-byte BE millis since epoch."""
        self._need(1)
        flag = self.data[self.pos]
        self.pos += 1
        if flag == 0:
            return None
        if flag != 1:
            raise Arq5ParseError(f"bad Date flag {flag} at pos={self.pos - 1}")
        millis = self.read_int64()
        return datetime.fromtimestamp(millis / 1000.0, tz=timezone.utc)

    def read_data(self) -> bytes:
        """``Data`` = 8-byte BE length + bytes."""
        length = self.read_uint64()
        self._need(length)
        out = self.data[self.pos : self.pos + length]
        self.pos += length
        return out


# ---------------------------------------------------------------------------
# BlobKey — version-gated optional fields
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BlobKey:
    """An Arq 5/6 blob reference.

    ``sha1`` is mandatory; the rest are optional and present only for
    specific Tree-version ranges:

    - ``stretchEncryptionKey``: Tree v14+ (Commit v4+ for Commit's own key)
    - ``storageType`` / ``archiveId`` / ``archiveSize`` /
      ``archiveUploadedDate``: Tree v17+ (Glacier metadata)

    ``compressionType`` is NOT part of the binary BlobKey — it's
    inherited from the parent Tree's compression-type fields and
    threaded through by the parser.
    """

    sha1: str
    stretchEncryptionKey: bool = False
    storageType: int = 1                  # 1=S3, 2=Glacier
    archiveId: Optional[str] = None
    archiveSize: int = 0
    archiveUploadedDate: Optional[datetime] = None
    compressionType: int = COMPRESSION_NONE


def parse_blobkey(
    s: BinaryStream, tree_version: int, *, compression_type: int = COMPRESSION_NONE,
) -> Optional[BlobKey]:
    """Parse a ``BlobKey`` from ``s``.

    Returns ``None`` when the embedded SHA-1 is null — arq_restore's
    convention for "no blob here". The compression type isn't on the
    wire; the caller threads it through from the parent Tree.
    """
    sha1 = s.read_string()
    stretch = False
    if tree_version >= 14:
        stretch = s.read_bool()
    storage = 1
    archive_id: Optional[str] = None
    archive_size = 0
    archive_uploaded: Optional[datetime] = None
    if tree_version >= 17:
        storage = s.read_uint32()
        archive_id = s.read_string()
        archive_size = s.read_uint64()
        archive_uploaded = s.read_date()
    if sha1 is None:
        return None
    return BlobKey(
        sha1=sha1,
        stretchEncryptionKey=stretch,
        storageType=storage,
        archiveId=archive_id,
        archiveSize=archive_size,
        archiveUploadedDate=archive_uploaded,
        compressionType=compression_type,
    )


# ---------------------------------------------------------------------------
# Node — version-gated, parsed under a parent Tree
# ---------------------------------------------------------------------------


@dataclass
class Arq5Node:
    """One Arq 5/6 file or directory entry inside a Tree."""

    isTree: bool = False
    treeContainsMissingItems: bool = False
    dataCompressionType: int = COMPRESSION_NONE
    xattrsCompressionType: int = COMPRESSION_NONE
    aclCompressionType: int = COMPRESSION_NONE
    dataBlobKeys: List[BlobKey] = field(default_factory=list)
    uncompressedDataSize: int = 0
    xattrsBlobKey: Optional[BlobKey] = None
    xattrsSize: int = 0
    aclBlobKey: Optional[BlobKey] = None
    uid: int = 0
    gid: int = 0
    mode: int = 0
    mtime_sec: int = 0
    mtime_nsec: int = 0
    flags: int = 0
    finderFlags: int = 0
    extendedFinderFlags: int = 0
    finderFileType: Optional[str] = None
    finderFileCreator: Optional[str] = None
    isFileExtensionHidden: bool = False
    st_dev: int = 0
    st_ino: int = 0
    st_nlink: int = 0
    st_rdev: int = 0
    ctime_sec: int = 0
    ctime_nsec: int = 0
    createTime_sec: int = 0
    createTime_nsec: int = 0
    st_blocks: int = 0
    st_blksize: int = 0


def parse_node(s: BinaryStream, tree_version: int) -> Arq5Node:
    """Parse one Node inside a Tree of version ``tree_version``."""
    n = Arq5Node()
    n.isTree = s.read_bool()
    if tree_version >= 18:
        n.treeContainsMissingItems = s.read_bool()
    if 12 <= tree_version <= 18:
        # Three Bools, mapped to Gzip/None.
        n.dataCompressionType = (
            COMPRESSION_GZIP if s.read_bool() else COMPRESSION_NONE
        )
        n.xattrsCompressionType = (
            COMPRESSION_GZIP if s.read_bool() else COMPRESSION_NONE
        )
        n.aclCompressionType = (
            COMPRESSION_GZIP if s.read_bool() else COMPRESSION_NONE
        )
    if tree_version >= 19:
        n.dataCompressionType = s.read_int32()
        n.xattrsCompressionType = s.read_int32()
        n.aclCompressionType = s.read_int32()

    count = s.read_int32()
    for _ in range(count):
        bk = parse_blobkey(
            s, tree_version, compression_type=n.dataCompressionType,
        )
        if bk is not None:
            n.dataBlobKeys.append(bk)
    n.uncompressedDataSize = s.read_uint64()

    if tree_version < 18:
        # Unused thumbnail / preview SHA-1 placeholders. Read + drop.
        _ = parse_blobkey(s, tree_version)
        _ = parse_blobkey(s, tree_version)

    n.xattrsBlobKey = parse_blobkey(
        s, tree_version, compression_type=n.xattrsCompressionType,
    )
    n.xattrsSize = s.read_uint64()
    n.aclBlobKey = parse_blobkey(
        s, tree_version, compression_type=n.aclCompressionType,
    )
    n.uid = s.read_int32()
    n.gid = s.read_int32()
    n.mode = s.read_int32()
    n.mtime_sec = s.read_int64()
    n.mtime_nsec = s.read_int64()
    n.flags = s.read_int64()
    n.finderFlags = s.read_int32()
    n.extendedFinderFlags = s.read_int32()
    n.finderFileType = s.read_string()
    n.finderFileCreator = s.read_string()
    n.isFileExtensionHidden = s.read_bool()
    n.st_dev = s.read_int32()
    n.st_ino = s.read_int32()
    n.st_nlink = s.read_uint32()
    n.st_rdev = s.read_int32()
    n.ctime_sec = s.read_int64()
    n.ctime_nsec = s.read_int64()
    n.createTime_sec = s.read_int64()
    n.createTime_nsec = s.read_int64()
    n.st_blocks = s.read_int64()
    n.st_blksize = s.read_uint32()
    return n


# ---------------------------------------------------------------------------
# Tree — top-level container
# ---------------------------------------------------------------------------


@dataclass
class Arq5Tree:
    """Decoded Arq 5/6 Tree."""

    version: int
    xattrsCompressionType: int = COMPRESSION_NONE
    aclCompressionType: int = COMPRESSION_NONE
    xattrsBlobKey: Optional[BlobKey] = None
    xattrsSize: int = 0
    aclBlobKey: Optional[BlobKey] = None
    uid: int = 0
    gid: int = 0
    mode: int = 0
    mtime_sec: int = 0
    mtime_nsec: int = 0
    flags: int = 0
    finderFlags: int = 0
    extendedFinderFlags: int = 0
    st_dev: int = 0
    st_ino: int = 0
    st_nlink: int = 0
    st_rdev: int = 0
    ctime_sec: int = 0
    ctime_nsec: int = 0
    st_blocks: int = 0
    st_blksize: int = 0
    createTime_sec: int = 0
    createTime_nsec: int = 0
    missing_nodes: Dict[str, Arq5Node] = field(default_factory=dict)
    nodes: Dict[str, Arq5Node] = field(default_factory=dict)


def _parse_tree_header(s: BinaryStream) -> int:
    if s.remaining() < TREE_HEADER_LENGTH:
        raise Arq5ParseError(
            f"Tree too short: {s.remaining()} bytes (header is "
            f"{TREE_HEADER_LENGTH})"
        )
    raw = s.data[s.pos : s.pos + TREE_HEADER_LENGTH]
    s.pos += TREE_HEADER_LENGTH
    try:
        header = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise Arq5ParseError(f"non-ASCII Tree header: {raw!r}") from exc
    if not header.startswith("TreeV") or len(header) < 6:
        raise Arq5ParseError(f"bad Tree header: {header!r}")
    digits = header[5:].rstrip("\x00 ")
    try:
        version = int(digits)
    except ValueError as exc:
        raise Arq5ParseError(
            f"bad Tree version digits: {digits!r}"
        ) from exc
    if version < MIN_SUPPORTED_TREE_VERSION:
        raise Arq5ParseError(f"Tree version {version} unsupported")
    if version == INVALID_TREE_VERSION:
        raise Arq5ParseError("Tree version 13 is reserved")
    if version > MAX_KNOWN_TREE_VERSION:
        # Tolerate forward-compatible reads — fields beyond v22 are
        # unknown to us. The parser will likely fail on first
        # gated-field mismatch; the operator can downgrade if needed.
        pass
    return version


def parse_tree(data: bytes) -> Arq5Tree:
    """Parse an Arq 5/6 Tree (the LZ4-/Gzip-decompressed payload of
    a tree blob, minus the EncryptedObject envelope)."""
    s = BinaryStream(data)
    v = _parse_tree_header(s)
    t = Arq5Tree(version=v)

    if 12 <= v <= 18:
        # Two Bools, mapped to Gzip/None.
        t.xattrsCompressionType = (
            COMPRESSION_GZIP if s.read_bool() else COMPRESSION_NONE
        )
        t.aclCompressionType = (
            COMPRESSION_GZIP if s.read_bool() else COMPRESSION_NONE
        )
    if v >= 19:
        t.xattrsCompressionType = s.read_int32()
        t.aclCompressionType = s.read_int32()

    t.xattrsBlobKey = parse_blobkey(
        s, v, compression_type=t.xattrsCompressionType,
    )
    t.xattrsSize = s.read_uint64()
    t.aclBlobKey = parse_blobkey(
        s, v, compression_type=t.aclCompressionType,
    )
    t.uid = s.read_int32()
    t.gid = s.read_int32()
    t.mode = s.read_int32()
    t.mtime_sec = s.read_int64()
    t.mtime_nsec = s.read_int64()
    t.flags = s.read_int64()
    t.finderFlags = s.read_int32()
    t.extendedFinderFlags = s.read_int32()
    t.st_dev = s.read_int32()
    t.st_ino = s.read_int32()
    t.st_nlink = s.read_uint32()
    t.st_rdev = s.read_int32()
    t.ctime_sec = s.read_int64()
    t.ctime_nsec = s.read_int64()
    t.st_blocks = s.read_int64()
    t.st_blksize = s.read_uint32()

    if 11 <= v <= 16:
        # aggregate_size_on_disk — present but unused.
        _ = s.read_uint64()
    if v >= 15:
        t.createTime_sec = s.read_int64()
        t.createTime_nsec = s.read_int64()
    if v >= 18:
        missing = s.read_uint32()
        for _ in range(missing):
            name = s.read_string() or ""
            t.missing_nodes[name] = parse_node(s, v)

    node_count = s.read_uint32()
    for _ in range(node_count):
        name = s.read_string() or ""
        t.nodes[name] = parse_node(s, v)
    return t


# ---------------------------------------------------------------------------
# Commit — root metadata pointing at a Tree
# ---------------------------------------------------------------------------


@dataclass
class Arq5Commit:
    """Decoded Arq 5/6 Commit."""

    version: int
    author: Optional[str] = None
    comment: Optional[str] = None
    parentCommitBlobKey: Optional[BlobKey] = None
    treeBlobKey: Optional[BlobKey] = None
    location: Optional[str] = None
    creationDate: Optional[datetime] = None
    failedFiles: List[Dict[str, Optional[str]]] = field(default_factory=list)
    hasMissingNodes: bool = False
    isComplete: bool = True
    bucketXmlData: bytes = b""
    arqVersion: Optional[str] = None


def _parse_commit_header(s: BinaryStream) -> int:
    if s.remaining() < COMMIT_HEADER_LENGTH:
        raise Arq5ParseError(
            f"Commit too short: {s.remaining()} bytes"
        )
    raw = s.data[s.pos : s.pos + COMMIT_HEADER_LENGTH]
    s.pos += COMMIT_HEADER_LENGTH
    header = raw.decode("ascii", errors="replace")
    if not header.startswith("CommitV"):
        raise Arq5ParseError(f"bad Commit header: {header!r}")
    digits = header[7:].rstrip("\x00 ")
    try:
        v = int(digits)
    except ValueError as exc:
        raise Arq5ParseError(f"bad Commit version: {digits!r}") from exc
    if v < MIN_SUPPORTED_COMMIT_VERSION:
        raise Arq5ParseError(f"Commit version {v} unsupported")
    return v


def parse_commit(data: bytes) -> Arq5Commit:
    """Parse an Arq 5/6 Commit blob plaintext."""
    s = BinaryStream(data)
    v = _parse_commit_header(s)
    c = Arq5Commit(version=v)
    c.author = s.read_string()
    c.comment = s.read_string()

    parent_count = s.read_uint64()
    for i in range(parent_count):
        sha = s.read_string()
        stretched = False
        if v >= 4:
            stretched = s.read_bool()
        if i == 0 and sha is not None:
            c.parentCommitBlobKey = BlobKey(
                sha1=sha, stretchEncryptionKey=stretched,
            )

    tree_sha = s.read_string()
    tree_stretched = False
    if v >= 4:
        tree_stretched = s.read_bool()
    if v == 8 or v == 9:
        # Bool tree_is_compressed.
        s.read_bool()
    tree_compression = COMPRESSION_NONE
    if v >= 10:
        tree_compression = s.read_int32()
    if tree_sha is not None:
        c.treeBlobKey = BlobKey(
            sha1=tree_sha,
            stretchEncryptionKey=tree_stretched,
            compressionType=tree_compression,
        )

    c.location = s.read_string()
    if 3 <= v <= 7:
        # merge_common_ancestor_sha1 — read + discard.
        s.read_string()
    if 4 <= v <= 7:
        s.read_bool()
    c.creationDate = s.read_date()
    if v >= 3:
        n_failed = s.read_uint64()
        for _ in range(n_failed):
            rel = s.read_string()
            err = s.read_string()
            c.failedFiles.append({"relativePath": rel, "errorMessage": err})
    if v >= 8:
        c.hasMissingNodes = s.read_bool()
    if v >= 9:
        c.isComplete = s.read_bool()
    c.bucketXmlData = s.read_data()
    c.arqVersion = s.read_string()
    return c
