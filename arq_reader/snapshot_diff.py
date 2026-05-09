"""Compare two backuprecords (snapshots) to surface what changed
between them.

The classic operator question after a long-running backup chain
is "what changed last night?". Without this module the answer is
to restore both snapshots and `diff -r` them — minutes of bytes
shuffled across the network for an answer that's structurally
in the trees themselves.

:func:`diff_snapshots` walks both records' root trees in parallel
and yields one :class:`SnapshotDiffEntry` per file/dir that's
different between the two — added in B, removed since A, content
or metadata changed.

Comparison is **content-fingerprint-based**, not byte-content
based: a file is "modified" iff its set of dataBlobLocs differs
between A and B. That catches every real change (any byte change
flips the SHA over the chunk), and avoids fetching + decrypting
file bytes purely to compare them.

The diff reports paths relative to the source root so a TUI
showing the result doesn't need to know where the backup came
from.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Tuple


@dataclass
class SnapshotDiffEntry:
    """One change between two snapshots.

    ``kind`` is one of:
    - "added"     — entry exists in B but not A
    - "removed"   — entry exists in A but not B
    - "modified"  — entry exists in both with different content
    - "type_changed" — entry exists in both but file ↔ dir
    """

    kind: str
    rel_path: str
    a_size: Optional[int] = None
    b_size: Optional[int] = None
    a_mtime_sec: Optional[int] = None
    b_mtime_sec: Optional[int] = None


@dataclass
class SnapshotDiffResult:
    """Aggregate result of :func:`diff_snapshots`. Operators
    typically just iterate over ``entries``; the tally counts are
    convenient for headline reporting."""

    entries: List[SnapshotDiffEntry] = field(default_factory=list)
    counts: Dict[str, int] = field(default_factory=dict)

    def add(self, entry: SnapshotDiffEntry) -> None:
        self.entries.append(entry)
        self.counts[entry.kind] = self.counts.get(entry.kind, 0) + 1


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------


def diff_snapshots(
    restore,
    *,
    record_path_a: str,
    record_path_b: str,
    computer_uuid: Optional[str] = None,
) -> SnapshotDiffResult:
    """Walk both backuprecords' trees and report every difference.

    ``restore`` is an open :class:`arq_reader.Restore` instance
    (we reuse its keyset cache + backend rather than opening a
    second connection).

    The walk is parallel-by-name: at each tree level, line up
    children by name and emit one DiffEntry per name that
    differs. Identical content (same blob_id set) at the same
    name is silent.
    """
    from arq_reader.decrypt import decrypt_lz4_arqo
    from arq_reader.parse import parse_tree
    from arq_writer.backuprecord import parse_backuprecord
    from arq_writer.types import FileNode, TreeNode

    if computer_uuid is None:
        # Infer from the path, same as record_validator does.
        parts = [p for p in record_path_a.split("/") if p]
        if not parts:
            raise ValueError(
                "cannot infer computer_uuid from path: "
                f"{record_path_a!r}"
            )
        computer_uuid = parts[0]

    keyset = restore.keyset(computer_uuid)
    backend = restore.backend

    def _load_record(path: str):
        rec_arqo = backend.read_all(path)
        rec_plain = decrypt_lz4_arqo(
            rec_arqo, keyset.encryption_key, keyset.hmac_key,
        )
        return parse_backuprecord(rec_plain)

    rec_a = _load_record(record_path_a)
    rec_b = _load_record(record_path_b)
    node_a = rec_a.get("node") or {}
    node_b = rec_b.get("node") or {}
    if not node_a or not node_b:
        raise ValueError(
            "one or both records missing the root 'node' field"
        )

    result = SnapshotDiffResult()

    def _fetch_tree(blob_loc_dict):
        """Fetch + decrypt + parse a tree blob from a JSON-shape
        BlobLoc dict (the form record root nodes carry)."""
        if blob_loc_dict.get("isPacked"):
            raw = backend.read_range(
                blob_loc_dict["relativePath"],
                int(blob_loc_dict["offset"]),
                int(blob_loc_dict["length"]),
            )
        else:
            raw = backend.read_all(blob_loc_dict["relativePath"])
        plain = decrypt_lz4_arqo(
            raw, keyset.encryption_key, keyset.hmac_key,
        )
        return parse_tree(plain)

    def _fetch_tree_from_dataclass(loc) -> Optional["object"]:
        """Same but for a parse_blobloc-derived BlobLoc dataclass
        (camelCase attrs)."""
        rel = getattr(loc, "relativePath", "") or ""
        if not rel:
            return None
        if getattr(loc, "isPacked", False):
            raw = backend.read_range(
                rel,
                int(getattr(loc, "offset", 0)),
                int(getattr(loc, "length", 0)),
            )
        else:
            raw = backend.read_all(rel)
        plain = decrypt_lz4_arqo(
            raw, keyset.encryption_key, keyset.hmac_key,
        )
        return parse_tree(plain)

    def _walk(name_a: dict, name_b: dict, *, rel: str) -> None:
        """name_a / name_b: dicts of {child_name: child_node} for
        the level being compared. Each walk pass emits diff
        entries + recurses into subtree pairs that exist in both."""
        names = sorted(set(name_a) | set(name_b))
        for n in names:
            child_rel = f"{rel}/{n}" if rel else n
            a = name_a.get(n)
            b = name_b.get(n)
            if a is None:
                # Added in B.
                result.add(SnapshotDiffEntry(
                    kind="added", rel_path=child_rel,
                    b_size=_size_of(b),
                    b_mtime_sec=_mtime_of(b),
                ))
                continue
            if b is None:
                result.add(SnapshotDiffEntry(
                    kind="removed", rel_path=child_rel,
                    a_size=_size_of(a),
                    a_mtime_sec=_mtime_of(a),
                ))
                continue
            a_is_tree = isinstance(a, TreeNode)
            b_is_tree = isinstance(b, TreeNode)
            if a_is_tree != b_is_tree:
                result.add(SnapshotDiffEntry(
                    kind="type_changed", rel_path=child_rel,
                    a_size=_size_of(a), b_size=_size_of(b),
                    a_mtime_sec=_mtime_of(a),
                    b_mtime_sec=_mtime_of(b),
                ))
                continue
            if a_is_tree and b_is_tree:
                # Recurse into the subtree pair only when their
                # tree blob_ids actually differ — when they match,
                # the subtrees are byte-identical and we can skip
                # the fetch entirely.
                a_tid = getattr(a.treeBlobLoc, "blobIdentifier", "")
                b_tid = getattr(b.treeBlobLoc, "blobIdentifier", "")
                if a_tid == b_tid:
                    continue
                ta = _fetch_tree_from_dataclass(a.treeBlobLoc)
                tb = _fetch_tree_from_dataclass(b.treeBlobLoc)
                if ta is None or tb is None:
                    continue
                _walk(
                    {c.name: c.node for c in ta.children},
                    {c.name: c.node for c in tb.children},
                    rel=child_rel,
                )
            else:
                # Both file nodes — compare their dataBlobLocs as
                # an unordered set (chunk order is stable so this
                # is technically over-strict for chunked files,
                # but in practice the chunker output is order-
                # preserving and any reorder = real change).
                a_ids = tuple(
                    getattr(loc, "blobIdentifier", "")
                    for loc in (a.dataBlobLocs or [])
                )
                b_ids = tuple(
                    getattr(loc, "blobIdentifier", "")
                    for loc in (b.dataBlobLocs or [])
                )
                if a_ids != b_ids:
                    result.add(SnapshotDiffEntry(
                        kind="modified", rel_path=child_rel,
                        a_size=_size_of(a), b_size=_size_of(b),
                        a_mtime_sec=_mtime_of(a),
                        b_mtime_sec=_mtime_of(b),
                    ))

    # Both root nodes must be tree nodes for a meaningful diff;
    # the rare "single-file root" case is handled by treating it
    # as a one-entry tree.
    if not node_a.get("isTree") or not node_b.get("isTree"):
        # Single-file roots — compare dataBlobLocs directly.
        a_ids = tuple(
            b.get("blobIdentifier", "")
            for b in (node_a.get("dataBlobLocs") or [])
        )
        b_ids = tuple(
            b.get("blobIdentifier", "")
            for b in (node_b.get("dataBlobLocs") or [])
        )
        if a_ids != b_ids:
            result.add(SnapshotDiffEntry(
                kind="modified", rel_path="",
                a_size=int(node_a.get("itemSize") or 0),
                b_size=int(node_b.get("itemSize") or 0),
            ))
        return result

    # Tree-rooted: fetch both root trees + walk.
    tree_loc_a = node_a["treeBlobLoc"]
    tree_loc_b = node_b["treeBlobLoc"]
    if tree_loc_a.get("blobIdentifier") == tree_loc_b.get(
        "blobIdentifier",
    ):
        # Snapshots are byte-identical at the root — no diff.
        return result
    ta = _fetch_tree(tree_loc_a)
    tb = _fetch_tree(tree_loc_b)
    _walk(
        {c.name: c.node for c in ta.children},
        {c.name: c.node for c in tb.children},
        rel="",
    )
    return result


@dataclass
class DedupReport:
    """Output of :func:`measure_dedup_ratio`.

    Operators want to know "how much disk did dedup actually
    save me?" The numerator is the count + bytes of blobs that
    appear in BOTH snapshots; the denominator is the union
    (every distinct blob across the pair). High shared_ratio
    means most of B was already in A — typical for incremental
    daily backups of a stable source.
    """

    snapshot_a: str = ""        # record_path or label
    snapshot_b: str = ""
    a_blob_count: int = 0       # distinct blobs reachable from A
    b_blob_count: int = 0       # distinct blobs reachable from B
    shared_blob_count: int = 0  # blobs in BOTH
    a_unique_count: int = 0     # blobs only in A (= deleted/superseded)
    b_unique_count: int = 0     # blobs only in B (= newly added)
    a_blob_bytes: int = 0       # sum of lengths in A
    b_blob_bytes: int = 0
    shared_blob_bytes: int = 0

    @property
    def shared_ratio(self) -> float:
        """Fraction of B that was already in A. Range 0..1.
        At 0.95+ the operator's incremental backups are
        ~90%+ dedup'd; at 0.5 something significant changed
        between the two snapshots."""
        if self.b_blob_count == 0:
            return 0.0
        return self.shared_blob_count / self.b_blob_count

    @property
    def bytes_saved_by_dedup(self) -> int:
        """How many bytes B would have written if there was
        NO dedup against A. Same as shared_blob_bytes; surfaced
        as a named property for operator-readable output."""
        return self.shared_blob_bytes


def measure_dedup_ratio(
    restore,
    *,
    record_path_a: str,
    record_path_b: str,
    computer_uuid: Optional[str] = None,
) -> DedupReport:
    """Walk both snapshots' trees + report the shared/unique
    blob breakdown.

    Same backend assumption as :func:`diff_snapshots` — pass
    an open :class:`arq_reader.Restore` instance + two
    backuprecord paths.

    Implementation note: this collects the FULL set of
    blob_ids reachable from each snapshot (data blobs +
    tree blobs + xattr blobs + ACL blob). Memory use is
    proportional to total distinct blobs across both
    snapshots; on a typical destination this is a few MB
    of Python set overhead — well within budget for the
    operator-on-laptop scenario.
    """
    from arq_reader.decrypt import decrypt_lz4_arqo
    from arq_reader.parse import parse_tree
    from arq_writer.backuprecord import parse_backuprecord
    from arq_writer.types import FileNode, TreeNode

    if computer_uuid is None:
        parts = [p for p in record_path_a.split("/") if p]
        if not parts:
            raise ValueError(
                "cannot infer computer_uuid from "
                f"{record_path_a!r}"
            )
        computer_uuid = parts[0]
    keyset = restore.keyset(computer_uuid)
    backend = restore.backend

    def _collect(record_path: str) -> Dict[str, int]:
        """Walk the snapshot rooted at ``record_path`` + return
        ``{blob_id: blob_byte_length}`` for every reachable blob.
        Tree blobs are fetched + parsed; data/xattr/acl blobs
        are NOT fetched (we use the BlobLoc.length field for
        size). This keeps the cost down on remote backends."""
        rec_arqo = backend.read_all(record_path)
        rec = parse_backuprecord(decrypt_lz4_arqo(
            rec_arqo, keyset.encryption_key, keyset.hmac_key,
        ))
        node = rec.get("node") or {}
        out: Dict[str, int] = {}

        def _add_loc(loc):
            """Accept either dict (JSON) or BlobLoc (dataclass)."""
            blob_id = (
                loc.get("blobIdentifier")
                if isinstance(loc, dict)
                else getattr(loc, "blobIdentifier", "")
            ) or ""
            length = int(
                loc.get("length")
                if isinstance(loc, dict)
                else getattr(loc, "length", 0) or 0
            )
            if blob_id:
                # Same blob_id may appear twice with different
                # 'length' (rare — same content stored unpacked
                # vs packed). Keep the first observation.
                out.setdefault(blob_id, length)

        # Root xattrs + ACL.
        for xloc in node.get("xattrsBlobLocs") or []:
            _add_loc(xloc)
        if node.get("aclBlobLoc"):
            _add_loc(node["aclBlobLoc"])

        if not node.get("isTree"):
            for loc in node.get("dataBlobLocs") or []:
                _add_loc(loc)
            return out

        # Tree-rooted: BFS over the tree blob graph.
        from collections import deque
        stack = deque(
            [node["treeBlobLoc"]] if node.get("treeBlobLoc")
            else []
        )
        seen_trees: set = set()
        while stack:
            loc = stack.popleft()
            blob_id = (
                loc.get("blobIdentifier")
                if isinstance(loc, dict)
                else getattr(loc, "blobIdentifier", "")
            ) or ""
            if blob_id in seen_trees:
                continue
            seen_trees.add(blob_id)
            _add_loc(loc)
            # Fetch + parse the tree.
            try:
                if loc.get("isPacked", False) if isinstance(loc, dict) else getattr(loc, "isPacked", False):
                    rel = (
                        loc["relativePath"]
                        if isinstance(loc, dict)
                        else loc.relativePath
                    )
                    off = int(
                        loc["offset"] if isinstance(loc, dict)
                        else loc.offset
                    )
                    ln = int(
                        loc["length"] if isinstance(loc, dict)
                        else loc.length
                    )
                    raw = backend.read_range(rel, off, ln)
                else:
                    rel = (
                        loc["relativePath"]
                        if isinstance(loc, dict)
                        else loc.relativePath
                    )
                    raw = backend.read_all(rel)
                tree = parse_tree(decrypt_lz4_arqo(
                    raw,
                    keyset.encryption_key, keyset.hmac_key,
                ))
            except Exception:
                continue
            for child in tree.children:
                cn = child.node
                # ACL + xattrs on every child.
                for xloc in (
                    getattr(cn, "xattrsBlobLocs", []) or []
                ):
                    _add_loc(xloc)
                acl = getattr(cn, "aclBlobLoc", None)
                if acl:
                    _add_loc(acl)
                if isinstance(cn, TreeNode):
                    stack.append(cn.treeBlobLoc)
                elif isinstance(cn, FileNode):
                    for loc in cn.dataBlobLocs or []:
                        _add_loc(loc)
        return out

    blobs_a = _collect(record_path_a)
    blobs_b = _collect(record_path_b)
    set_a = set(blobs_a.keys())
    set_b = set(blobs_b.keys())
    shared = set_a & set_b

    report = DedupReport(
        snapshot_a=record_path_a,
        snapshot_b=record_path_b,
        a_blob_count=len(set_a),
        b_blob_count=len(set_b),
        shared_blob_count=len(shared),
        a_unique_count=len(set_a - set_b),
        b_unique_count=len(set_b - set_a),
        a_blob_bytes=sum(blobs_a.values()),
        b_blob_bytes=sum(blobs_b.values()),
        # For shared blobs we can pick either side's recorded
        # length — they should match (same content → same
        # blob_id → same byte size). Use A's value.
        shared_blob_bytes=sum(
            blobs_a.get(b, 0) for b in shared
        ),
    )
    return report


def _size_of(node) -> int:
    return int(getattr(node, "itemSize", 0) or 0)


def _mtime_of(node) -> int:
    return int(getattr(node, "mtime_sec", 0) or 0)
