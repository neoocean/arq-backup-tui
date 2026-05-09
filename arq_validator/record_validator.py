"""Record-level data validation.

The existing validator tiers (L0-L2) cover layout shape, magic
bytes, latest-backuprecord HMAC, and a sampled object audit. None
of them follow the *actual blob graph* of a specific
backuprecord — so a backuprecord whose root tree references a
missing or corrupt blob deeper in the hierarchy passes L0-L2 but
would fail at restore time.

This module fills that gap. :func:`validate_record` takes one
backuprecord path, decrypts the keyset, parses the record + root
tree, then recursively walks every Tree, every FileNode's
``dataBlobLocs``, every Node's ``xattrsBlobLocs`` and ``aclBlobLoc``,
fetching + HMAC-verifying each one. The output is a
:class:`RecordValidationReport` with per-failure detail so an
operator can pinpoint which file's blob went missing.

Cost: O(every blob in the tree). For a multi-GB backup this is
slow — measured against the operator's destination, expect
minutes per record. Provide a ``max_blobs`` cap (default unlimited)
so a CI smoke run can stop after a few hundred fetches.

CLI: exposed as ``arq-validate record <record-path>`` (added in
``arq_validator/cli.py``). Cron operators can wire it to a
nightly check.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from . import constants as C
from . import layout as L
from .backend import Backend
from .crypto import (
    Keyset,
    decrypt_keyset,
    verify_encrypted_object_hmac,
)


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class RecordValidationFailure:
    """One blob that failed to fetch or verify. ``loc_path`` /
    ``loc_offset`` / ``loc_length`` come from the BlobLoc; ``kind``
    distinguishes fetch errors from HMAC mismatches from decode
    failures so triage can route accordingly."""

    blob_id: str
    rel_path: str
    offset: int
    length: int
    kind: str            # "fetch" | "hmac" | "decode" | "missing"
    error: str
    node_path: str = ""  # path-from-root of the file/dir whose
                         # BlobLoc this is (best-effort)


@dataclass
class RecordValidationReport:
    """Aggregate output of :func:`validate_record`. Operators
    typically just check ``ok`` and dump ``failures`` on red."""

    record_path: str = ""
    ok: bool = True
    blobs_walked: int = 0
    bytes_fetched: int = 0
    trees_walked: int = 0
    files_walked: int = 0
    elapsed_sec: float = 0.0
    truncated_after: int = 0   # 0 = walked the whole tree;
                                # >0 = stopped after this many
                                # blobs because of ``max_blobs``
    failures: List[RecordValidationFailure] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------


# Lazy imports so the validator package doesn't pull arq_reader at
# top level (would invert the established dependency direction).
def _import_helpers():
    from arq_reader.decrypt import decrypt_lz4_arqo, DecryptError
    from arq_reader.parse import parse_tree
    from arq_writer.backuprecord import parse_backuprecord
    from arq_writer.types import FileNode, TreeNode
    return (
        decrypt_lz4_arqo, DecryptError,
        parse_tree, parse_backuprecord,
        FileNode, TreeNode,
    )


def _resolve_blob_bytes(
    backend: Backend, blob_loc, *, computer_uuid: str,
) -> Optional[bytes]:
    """Fetch the raw on-disk bytes a BlobLoc points at.

    ``blob_loc`` may be either the dataclass form (camelCase
    attrs from arq_reader.parse.parse_blobloc) or the dict form
    (from a parsed backuprecord JSON). Returns None if the
    underlying file simply isn't there — caller logs a "missing"
    failure rather than raising.
    """
    def _g(field, default=None):
        if isinstance(blob_loc, dict):
            return blob_loc.get(field, default)
        return getattr(blob_loc, field, default)

    rel = _g("relativePath", "")
    if rel:
        if _g("isPacked", False):
            try:
                return backend.read_range(
                    rel,
                    int(_g("offset", 0)),
                    int(_g("length", 0)),
                )
            except OSError:
                return None
        try:
            return backend.read_all(rel)
        except OSError:
            return None
    # Standalone-object fallback. Not all destinations have the
    # same path layout — try the standardobjects tree first.
    blob_id = _g("blobIdentifier", "") or ""
    if not blob_id:
        return None
    path = (
        f"/{computer_uuid}/standardobjects/"
        f"{blob_id[:2]}/{blob_id[2:]}"
    )
    try:
        return backend.read_all(path)
    except OSError:
        return None


def _verify_blob(
    raw: bytes, hmac_key: bytes,
) -> Tuple[bool, Optional[str]]:
    """Return ``(ok, error)`` for one fetched ARQO blob.

    Magic + length sanity + HMAC. Doesn't decrypt the body — that
    would be O(per blob) AES + LZ4 work, and HMAC alone is enough
    to certify the bytes are intact.
    """
    if len(raw) < C.ARQO_HEADER_BYTES:
        return False, f"too short: {len(raw)} < {C.ARQO_HEADER_BYTES}"
    if raw[: len(C.ARQO_MAGIC)] != C.ARQO_MAGIC:
        return False, f"bad magic: {raw[:4]!r}"
    ok, _exp, _act = verify_encrypted_object_hmac(raw, hmac_key)
    if not ok:
        return False, "HMAC mismatch"
    return True, None


def validate_record(
    backend: Backend,
    record_path: str,
    encryption_password: str,
    *,
    computer_uuid: Optional[str] = None,
    openssl_path: str = "openssl",
    max_blobs: int = 0,        # 0 = walk everything
    callback=None,
) -> RecordValidationReport:
    """Walk every BlobLoc reachable from ``record_path`` and
    verify each one decrypts + passes HMAC.

    ``record_path`` is an absolute server-side path
    (e.g. ``/<cu>/backupfolders/<fu>/backuprecords/00177/…backuprecord``).
    When ``computer_uuid`` is omitted the function infers it from
    the path (the first directory component after the root).

    ``max_blobs`` lets a CI smoke run cap the work; 0 = unbounded.

    The returned :class:`RecordValidationReport` is always
    populated (never raises out) so the CLI can JSON-print it
    even on partial walks.
    """
    (
        decrypt_lz4_arqo, _DecryptError,
        parse_tree, parse_backuprecord,
        FileNode, TreeNode,
    ) = _import_helpers()
    started = time.time()
    report = RecordValidationReport(record_path=record_path)

    if computer_uuid is None:
        # /CUUID/backupfolders/...  → "CUUID"
        parts = [p for p in record_path.split("/") if p]
        if not parts:
            report.ok = False
            report.failures.append(RecordValidationFailure(
                blob_id="", rel_path=record_path,
                offset=0, length=0, kind="decode",
                error="cannot infer computer_uuid from path",
            ))
            report.elapsed_sec = time.time() - started
            return report
        computer_uuid = parts[0]

    # Decrypt the keyset once.
    keyset_path = f"/{computer_uuid}/encryptedkeyset.dat"
    try:
        keyset_blob = backend.read_all(keyset_path)
        keyset = decrypt_keyset(
            keyset_blob, encryption_password,
            openssl_path=openssl_path,
        )
    except Exception as exc:
        report.ok = False
        report.failures.append(RecordValidationFailure(
            blob_id="", rel_path=keyset_path,
            offset=0, length=0, kind="fetch",
            error=f"keyset: {type(exc).__name__}: {exc}",
        ))
        report.elapsed_sec = time.time() - started
        return report

    # Fetch + parse the record itself.
    try:
        record_arqo = backend.read_all(record_path)
        record_plain = decrypt_lz4_arqo(
            record_arqo, keyset.encryption_key, keyset.hmac_key,
            openssl_path=openssl_path,
        )
        record = parse_backuprecord(record_plain)
    except Exception as exc:
        report.ok = False
        report.failures.append(RecordValidationFailure(
            blob_id="", rel_path=record_path,
            offset=0, length=0, kind="decode",
            error=f"record: {type(exc).__name__}: {exc}",
        ))
        report.elapsed_sec = time.time() - started
        return report

    root_node = record.get("node") or {}
    if not root_node:
        report.failures.append(RecordValidationFailure(
            blob_id="", rel_path=record_path,
            offset=0, length=0, kind="decode",
            error="record has no root node",
        ))
        report.ok = False
        report.elapsed_sec = time.time() - started
        return report

    # Recursive walk. Use an explicit stack rather than recursion
    # so a deeply-nested tree can't blow Python's call stack.
    # Each entry: (node_or_dict, source_kind, node_rel_path)
    stack: List[Tuple[Any, str, str]] = [(root_node, "root", "")]
    seen_trees: set = set()

    def _emit(_event_kind: str, **payload):
        if callback is not None:
            try:
                callback(_event_kind, payload)
            except Exception:
                pass

    while stack:
        node, kind, node_rel = stack.pop()
        if max_blobs and report.blobs_walked >= max_blobs:
            report.truncated_after = report.blobs_walked
            break

        # Each "node" can carry one tree blob (TreeNode), N data
        # blobs (FileNode), N xattr blobs, and (rare) one ACL blob.
        # We treat them uniformly here; each Node iterates its own
        # blobs + queues child trees onto the stack.

        # tree blob (TreeNodes only)
        is_tree = bool(node.get("isTree")) if isinstance(node, dict) \
            else hasattr(node, "treeBlobLoc") and not hasattr(node, "dataBlobLocs")
        if is_tree:
            tree_loc = (node.get("treeBlobLoc")
                        if isinstance(node, dict)
                        else getattr(node, "treeBlobLoc", None))
            if tree_loc:
                tree_blob_id = (
                    tree_loc.get("blobIdentifier")
                    if isinstance(tree_loc, dict)
                    else getattr(tree_loc, "blobIdentifier", "")
                ) or ""
                if tree_blob_id and tree_blob_id in seen_trees:
                    # Cycle / dedup: don't re-walk the same tree.
                    continue
                if tree_blob_id:
                    seen_trees.add(tree_blob_id)
                ok = _check_one_loc(
                    backend, tree_loc, keyset, computer_uuid,
                    report, node_path=node_rel, blob_kind="tree",
                )
                _emit("blob_walked", path=node_rel, kind="tree", ok=ok)
                if ok:
                    # Decrypt + parse to enqueue children.
                    raw = _resolve_blob_bytes(
                        backend, tree_loc,
                        computer_uuid=computer_uuid,
                    )
                    if raw is not None:
                        try:
                            plain = decrypt_lz4_arqo(
                                raw,
                                keyset.encryption_key,
                                keyset.hmac_key,
                                openssl_path=openssl_path,
                            )
                            tree = parse_tree(plain)
                            report.trees_walked += 1
                            for child in tree.children:
                                child_rel = (
                                    f"{node_rel}/{child.name}"
                                    if node_rel else child.name
                                )
                                # Convert dataclass children to a
                                # uniform dict shape for the stack
                                # so the loop body has one code
                                # path.
                                stack.append((
                                    _node_to_walk_dict(child.node),
                                    "child", child_rel,
                                ))
                        except Exception as exc:
                            report.failures.append(
                                RecordValidationFailure(
                                    blob_id=tree_blob_id,
                                    rel_path=str(_locpath(tree_loc)),
                                    offset=int(_locint(tree_loc, "offset")),
                                    length=int(_locint(tree_loc, "length")),
                                    kind="decode",
                                    error=(
                                        f"tree decode: "
                                        f"{type(exc).__name__}: {exc}"
                                    ),
                                    node_path=node_rel,
                                )
                            )
                            report.ok = False
        else:
            # FileNode: data blobs.
            data_locs = (node.get("dataBlobLocs")
                         if isinstance(node, dict)
                         else getattr(node, "dataBlobLocs", []) or [])
            for loc in data_locs or []:
                if max_blobs and report.blobs_walked >= max_blobs:
                    break
                ok = _check_one_loc(
                    backend, loc, keyset, computer_uuid,
                    report, node_path=node_rel, blob_kind="data",
                )
                _emit("blob_walked", path=node_rel, kind="data", ok=ok)
            report.files_walked += 1

        # xattr blobs (both FileNode + TreeNode)
        xattr_locs = (node.get("xattrsBlobLocs")
                      if isinstance(node, dict)
                      else getattr(node, "xattrsBlobLocs", []) or [])
        for loc in xattr_locs or []:
            if max_blobs and report.blobs_walked >= max_blobs:
                break
            ok = _check_one_loc(
                backend, loc, keyset, computer_uuid,
                report, node_path=node_rel, blob_kind="xattr",
            )
            _emit("blob_walked", path=node_rel, kind="xattr", ok=ok)

        # ACL blob (singleton, optional)
        acl_loc = (node.get("aclBlobLoc")
                   if isinstance(node, dict)
                   else getattr(node, "aclBlobLoc", None))
        if acl_loc:
            ok = _check_one_loc(
                backend, acl_loc, keyset, computer_uuid,
                report, node_path=node_rel, blob_kind="acl",
            )
            _emit("blob_walked", path=node_rel, kind="acl", ok=ok)

    report.elapsed_sec = time.time() - started
    if report.failures:
        report.ok = False
    return report


def _check_one_loc(
    backend: Backend, loc, keyset: Keyset, computer_uuid: str,
    report: RecordValidationReport, *,
    node_path: str, blob_kind: str,
) -> bool:
    """Fetch + HMAC-verify one BlobLoc; record any failure on
    ``report`` and return whether it passed. Increments
    ``blobs_walked`` either way."""
    report.blobs_walked += 1
    blob_id = (
        loc.get("blobIdentifier")
        if isinstance(loc, dict)
        else getattr(loc, "blobIdentifier", "")
    ) or ""
    rel = _locpath(loc)
    offset = int(_locint(loc, "offset"))
    length = int(_locint(loc, "length"))
    raw = _resolve_blob_bytes(
        backend, loc, computer_uuid=computer_uuid,
    )
    if raw is None:
        report.failures.append(RecordValidationFailure(
            blob_id=blob_id, rel_path=rel,
            offset=offset, length=length,
            kind="missing", error="blob not found at location",
            node_path=node_path,
        ))
        return False
    report.bytes_fetched += len(raw)
    ok, err = _verify_blob(raw, keyset.hmac_key)
    if ok:
        return True
    report.failures.append(RecordValidationFailure(
        blob_id=blob_id, rel_path=rel,
        offset=offset, length=length,
        kind="hmac" if "HMAC" in (err or "") else "decode",
        error=err or "unknown",
        node_path=node_path,
    ))
    return False


def _locpath(loc) -> str:
    if isinstance(loc, dict):
        return str(loc.get("relativePath") or "")
    return str(getattr(loc, "relativePath", "") or "")


def _locint(loc, field: str) -> int:
    if isinstance(loc, dict):
        return int(loc.get(field) or 0)
    return int(getattr(loc, field, 0) or 0)


def _node_to_walk_dict(node) -> Dict[str, Any]:
    """Convert a parsed FileNode/TreeNode dataclass into the
    minimal dict shape ``validate_record``'s stack expects.

    We don't need every metadata field — just the BlobLocs."""
    if hasattr(node, "treeBlobLoc"):
        return {
            "isTree": True,
            "treeBlobLoc": getattr(node, "treeBlobLoc", None),
            "xattrsBlobLocs": getattr(node, "xattrsBlobLocs", []) or [],
            "aclBlobLoc": getattr(node, "aclBlobLoc", None),
        }
    return {
        "isTree": False,
        "dataBlobLocs": getattr(node, "dataBlobLocs", []) or [],
        "xattrsBlobLocs": getattr(node, "xattrsBlobLocs", []) or [],
        "aclBlobLoc": getattr(node, "aclBlobLoc", None),
    }
