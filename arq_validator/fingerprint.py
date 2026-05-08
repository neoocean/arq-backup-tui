"""Shape fingerprint of an Arq 7 destination.

The fingerprint is a structural summary that two destinations of
the **same source tree** should agree on, regardless of which tool
produced them. It deliberately avoids salt-dependent values
(blob_ids, encryption-IV-derived payloads) so that fingerprints
from an Arq.app-produced destination and a destination from this
writer can be compared directly — the only thing that matters is
"do they describe the same logical shape?"

Fields included:

- **Layout**: counts of computers / folders / records / blobs / packs
- **JSON sidecar**: only the **schema** (keys + types) of
  ``backupconfig.json`` / ``backupplan.json`` /
  ``backupfolders.json`` / ``backupfolder.json`` — values are
  intentionally dropped because they vary per-machine
- **Per-record tree shape**: walked recursively for every
  backuprecord; for every file we record
  ``(rel_path, item_size, mtime_sec, mode_perms, num_chunks,
  [chunk_size_1, chunk_size_2, ...])``
- **Backuprecord plist**: the set of top-level keys + value types

What's excluded (intentionally):
- blob_id values (depend on blob_id_salt)
- encryption_key / hmac_key bytes (per-keyset)
- backupplan UUID / computerName / macAddress
- mtime nanosecond fractions (filesystem-resolution-dependent)

Two fingerprints will be **identical** for the same logical
backup produced by different tools iff the tools agree on:

1. directory walk order
2. file-content chunking (boundaries → chunk sizes)
3. Tree / Node / BlobLoc binary format
4. JSON sidecar keys + types
5. backuprecord plist keys + types

If 1-3 differ between tools, the fingerprint diff pinpoints
exactly which file's chunk pattern disagrees — making
chunker-parameter mismatch easy to spot.

See ``docs/COMPAT-VERIFICATION.md`` for the operator-paste
workflow.
"""

from __future__ import annotations

import hashlib
import json
import plistlib
import stat as stat_mod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from arq_reader.parse import parse_tree
from arq_writer.types import BlobLoc, FileNode, TreeNode

from . import constants as C
from .backend import Backend
from .crypto import decrypt_keyset
from .layout import discover_layout, list_backuprecords


SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Shape capture
# ---------------------------------------------------------------------------


def _parse_record(plain):
    # Lazy import to avoid arq_validator/__init__ → arq_writer/__init__
    # circular import.
    from arq_writer.backuprecord import parse_backuprecord
    return parse_backuprecord(plain)



@dataclass
class FileFingerprint:
    """Per-file shape entry. ``chunk_sizes`` is the in-order list
    of plaintext lengths the chunker produced."""

    rel_path: str
    item_size: int
    mtime_sec: int
    mode_perms: int               # mac_st_mode & 0o7777
    is_symlink: bool
    chunk_sizes: List[int] = field(default_factory=list)


def _python_type_name(v: Any) -> str:
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, str):
        return "str"
    if isinstance(v, bytes):
        return "bytes"
    if isinstance(v, list):
        return "list"
    if isinstance(v, dict):
        return "dict"
    if v is None:
        return "null"
    return type(v).__name__


def _schema_of_dict(d: Dict[str, Any], depth: int = 2) -> Dict[str, Any]:
    """Return a {key: type-name} skeleton for ``d``. Recurses into
    nested dicts up to ``depth`` levels; deeper layers are summarized
    by their type only."""
    out: Dict[str, Any] = {}
    for k in sorted(d.keys()):
        v = d[k]
        if isinstance(v, dict) and depth > 0:
            out[k] = _schema_of_dict(v, depth=depth - 1)
        elif isinstance(v, list) and v and isinstance(v[0], dict) and depth > 0:
            out[k] = [_schema_of_dict(v[0], depth=depth - 1)]
        else:
            out[k] = _python_type_name(v)
    return out


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def compute_shape_fingerprint(
    backend: Backend,
    *,
    encryption_password: str,
    root: str = "/",
    computer_uuid: Optional[str] = None,
    openssl_path: str = "openssl",
) -> Dict[str, Any]:
    """Walk ``backend`` rooted at ``root`` and return a JSON-
    serializable dict describing the destination's structural shape.

    The dict is salt-independent: same source + same tool
    settings → identical dict regardless of encryption keys.
    """
    # Local imports to keep module-load deps lean.
    from arq_reader.decrypt import (
        decrypt_encrypted_object, decrypt_lz4_arqo,
    )
    from arq_writer.lz4_block import lz4_unwrap

    out: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "source": "arq-backup-tui.compute_shape_fingerprint",
        "computers": [],
    }

    layouts = discover_layout(backend, root)
    if computer_uuid is not None:
        layouts = [
            lay for lay in layouts if lay.computer_uuid == computer_uuid
        ]
    for lay in layouts:
        cu = lay.computer_uuid
        cu_root = f"{root.rstrip('/')}/{cu}"
        keyset_blob = backend.read_all(f"{cu_root}/{C.KEYSET_FILE}")
        keyset = decrypt_keyset(
            keyset_blob, encryption_password,
            openssl_path=openssl_path,
        )

        comp = {
            "uuid": "REDACTED",
            "config_schema": _schema_of_json(
                backend, f"{cu_root}/backupconfig.json",
            ),
            "plan_schema": _schema_of_json(
                backend, f"{cu_root}/backupplan.json",
            ),
            "folders_index_schema": _schema_of_json(
                backend, f"{cu_root}/backupfolders.json",
            ),
            "object_storage": {
                "standardobjects_count": _count_files(
                    backend,
                    f"{cu_root}/{C.STANDARDOBJECTS_DIR}",
                    nested=True,
                ),
                "treepacks_count": _count_files(
                    backend, f"{cu_root}/{C.TREEPACKS_DIR}",
                    nested=True,
                ),
                "blobpacks_count": _count_files(
                    backend, f"{cu_root}/{C.BLOBPACKS_DIR}",
                    nested=True,
                ),
                "largeblobpacks_count": _count_files(
                    backend, f"{cu_root}/{C.LARGEBLOBPACKS_DIR}",
                    nested=True,
                ),
            },
            "folders": [],
        }

        for folder_uuid in sorted(lay.backup_folder_uuids):
            folder = {
                "uuid": "REDACTED",
                "folder_schema": _schema_of_json(
                    backend,
                    f"{cu_root}/{C.BACKUPFOLDERS_DIR}/{folder_uuid}/"
                    "backupfolder.json",
                ),
                "records": [],
            }
            rec_paths = list_backuprecords(
                backend, root, cu, folder_uuid,
            )
            for rec_path in rec_paths:
                folder["records"].append(_record_fingerprint(
                    backend, rec_path, keyset,
                    openssl_path=openssl_path,
                ))
            comp["folders"].append(folder)
        out["computers"].append(comp)
    return out


def _schema_of_json(backend: Backend, path: str) -> Dict[str, Any]:
    try:
        raw = backend.read_all(path)
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return {"_error": "missing_or_unparseable"}
    if not isinstance(data, dict):
        return {"_error": "not_dict"}
    return _schema_of_dict(data)


def _count_files(backend: Backend, path: str, *, nested: bool) -> int:
    if not backend.is_dir(path):
        return 0
    total = 0
    try:
        for sub in backend.list_dir(path):
            sub_path = f"{path}/{sub}"
            if nested and backend.is_dir(sub_path):
                try:
                    total += len(backend.list_dir(sub_path))
                except Exception:
                    pass
            else:
                total += 1
    except Exception:
        pass
    return total


def _record_fingerprint(
    backend: Backend, rec_path: str, keyset,
    *, openssl_path: str = "openssl",
) -> Dict[str, Any]:
    from arq_reader.decrypt import decrypt_lz4_arqo

    out: Dict[str, Any] = {
        "creation_date": None,
        "version": None,
        "is_complete": None,
        "computer_os_type": None,
        "node_schema": None,
        "files": [],
        "tree_count": 0,
    }
    try:
        arqo = backend.read_all(rec_path)
        plist_bytes = decrypt_lz4_arqo(
            arqo, keyset.encryption_key, keyset.hmac_key,
            openssl_path=openssl_path,
        )
        record = _parse_record(plist_bytes)
    except Exception as exc:
        out["_error"] = f"{type(exc).__name__}: {exc}"
        return out
    if not isinstance(record, dict):
        out["_error"] = "record_not_dict"
        return out

    out["creation_date"] = (
        int(record.get("creationDate") or 0) if "creationDate" in record else None
    )
    out["version"] = record.get("version")
    out["is_complete"] = record.get("isComplete")
    out["computer_os_type"] = record.get("computerOSType")
    out["node_schema"] = sorted(record.keys())

    node = record.get("node")
    if not isinstance(node, dict):
        return out

    files: List[FileFingerprint] = []
    tree_count = [0]
    if node.get("isTree"):
        tree_loc = _blobloc_from_dict(node.get("treeBlobLoc") or {})
        if tree_loc is not None:
            _walk_tree_for_fingerprint(
                tree_loc, "", backend, keyset,
                files, tree_count,
                openssl_path=openssl_path,
            )
    else:
        # Root is a single file.
        files.append(FileFingerprint(
            rel_path="",
            item_size=int(node.get("itemSize") or 0),
            mtime_sec=int(node.get("modificationTime_sec") or 0),
            mode_perms=stat_mod.S_IMODE(int(node.get("mac_st_mode") or 0)),
            is_symlink=bool(
                stat_mod.S_ISLNK(int(node.get("mac_st_mode") or 0))
            ),
            chunk_sizes=[
                int(b.get("length") or 0)
                for b in node.get("dataBlobLocs") or []
            ],
        ))

    files.sort(key=lambda f: f.rel_path)
    out["files"] = [_serialize_file_fp(f) for f in files]
    out["tree_count"] = tree_count[0]
    return out


def _blobloc_from_dict(d: Dict[str, Any]) -> Optional[BlobLoc]:
    bid = d.get("blobIdentifier")
    if not isinstance(bid, str):
        return None
    try:
        return BlobLoc(
            blobIdentifier=bid,
            isPacked=bool(d.get("isPacked", False)),
            relativePath=str(d.get("relativePath") or ""),
            offset=int(d.get("offset") or 0),
            length=int(d.get("length") or 0),
            stretchEncryptionKey=bool(
                d.get("stretchEncryptionKey", True)
            ),
            compressionType=int(d.get("compressionType") or 2),
        )
    except (TypeError, ValueError):
        return None


def _walk_tree_for_fingerprint(
    tree_loc: BlobLoc,
    rel_path: str,
    backend: Backend,
    keyset,
    files: List[FileFingerprint],
    tree_count: List[int],
    *,
    openssl_path: str = "openssl",
    seen: Optional[set] = None,
) -> None:
    from arq_reader.decrypt import decrypt_encrypted_object
    from arq_writer.lz4_block import lz4_unwrap

    if seen is None:
        seen = set()
    if tree_loc.blobIdentifier in seen:
        return
    seen.add(tree_loc.blobIdentifier)
    tree_count[0] += 1
    try:
        if tree_loc.isPacked:
            raw = backend.read_range(
                tree_loc.relativePath, tree_loc.offset,
                tree_loc.length,
            )
        else:
            raw = backend.read_all(tree_loc.relativePath)
        if raw[:4] == b"ARQO":
            raw = decrypt_encrypted_object(
                raw, keyset.encryption_key, keyset.hmac_key,
                openssl_path=openssl_path,
            )
        if tree_loc.compressionType == 2:
            raw = lz4_unwrap(raw)
        tree = parse_tree(raw)
    except Exception:
        return

    for child in tree.children:
        child_rel = (
            f"{rel_path}/{child.name}" if rel_path else child.name
        )
        if isinstance(child.node, TreeNode):
            _walk_tree_for_fingerprint(
                child.node.treeBlobLoc, child_rel,
                backend, keyset, files, tree_count,
                openssl_path=openssl_path, seen=seen,
            )
        elif isinstance(child.node, FileNode):
            files.append(FileFingerprint(
                rel_path=child_rel,
                item_size=int(child.node.itemSize),
                mtime_sec=int(child.node.mtime_sec),
                mode_perms=stat_mod.S_IMODE(
                    int(child.node.mac_st_mode),
                ),
                is_symlink=bool(stat_mod.S_ISLNK(
                    int(child.node.mac_st_mode),
                )),
                chunk_sizes=[
                    int(b.length) for b in child.node.dataBlobLocs
                ],
            ))


def _serialize_file_fp(f: FileFingerprint) -> Dict[str, Any]:
    return {
        "rel_path": f.rel_path,
        "item_size": f.item_size,
        "mtime_sec": f.mtime_sec,
        "mode_perms": f.mode_perms,
        "is_symlink": f.is_symlink,
        "chunk_sizes": list(f.chunk_sizes),
    }


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


def diff_fingerprints(
    a: Dict[str, Any], b: Dict[str, Any],
) -> Dict[str, Any]:
    """Compare two fingerprints and return a structured diff.

    Counts mismatches per category (sidecar schema diffs, missing
    files, file-shape diffs, chunk-pattern diffs) plus a sample of
    representative diffs in each category. Designed for
    operator-paste workflow: paste both fingerprints in, eyeball
    the diff, decide whether the chunker / format / metadata
    needs adjustment.
    """
    diff: Dict[str, Any] = {
        "schema_version_match": (
            a.get("schema_version") == b.get("schema_version")
        ),
        "computer_count": {
            "a": len(a.get("computers", [])),
            "b": len(b.get("computers", [])),
        },
        "sidecar_schema_diffs": [],
        "folder_count_diffs": [],
        "record_count_diffs": [],
        "missing_files_in_a": [],
        "missing_files_in_b": [],
        "file_shape_diffs": [],
        "chunk_pattern_diffs": [],
        "summary": {},
    }

    # Compare each computer pairwise (both sides sorted).
    a_comps = a.get("computers") or []
    b_comps = b.get("computers") or []
    for ac, bc in zip(a_comps, b_comps):
        for key in ("config_schema", "plan_schema",
                    "folders_index_schema"):
            if ac.get(key) != bc.get(key):
                diff["sidecar_schema_diffs"].append({
                    "key": key,
                    "a_only": _dict_diff(ac.get(key) or {},
                                         bc.get(key) or {}),
                    "b_only": _dict_diff(bc.get(key) or {},
                                         ac.get(key) or {}),
                })
        a_folders = ac.get("folders") or []
        b_folders = bc.get("folders") or []
        if len(a_folders) != len(b_folders):
            diff["folder_count_diffs"].append({
                "a": len(a_folders), "b": len(b_folders),
            })
        for af, bf in zip(a_folders, b_folders):
            a_recs = af.get("records") or []
            b_recs = bf.get("records") or []
            if len(a_recs) != len(b_recs):
                diff["record_count_diffs"].append({
                    "a": len(a_recs), "b": len(b_recs),
                })
            for ar, br in zip(a_recs, b_recs):
                _diff_record(ar, br, diff)

    # Roll up totals.
    diff["summary"] = {
        "sidecar_schema_diffs": len(diff["sidecar_schema_diffs"]),
        "folder_count_diffs": len(diff["folder_count_diffs"]),
        "record_count_diffs": len(diff["record_count_diffs"]),
        "missing_files_in_a": len(diff["missing_files_in_a"]),
        "missing_files_in_b": len(diff["missing_files_in_b"]),
        "file_shape_diffs": len(diff["file_shape_diffs"]),
        "chunk_pattern_diffs": len(diff["chunk_pattern_diffs"]),
    }
    diff["match"] = all(v == 0 for v in diff["summary"].values()) and \
        diff["schema_version_match"] and \
        diff["computer_count"]["a"] == diff["computer_count"]["b"]
    return diff


def _diff_record(a: Dict[str, Any], b: Dict[str, Any], out: Dict[str, Any]):
    a_files = {f["rel_path"]: f for f in a.get("files") or []}
    b_files = {f["rel_path"]: f for f in b.get("files") or []}
    for rel in a_files:
        if rel not in b_files:
            out["missing_files_in_b"].append(rel)
    for rel in b_files:
        if rel not in a_files:
            out["missing_files_in_a"].append(rel)
    for rel in a_files.keys() & b_files.keys():
        af = a_files[rel]
        bf = b_files[rel]
        # File-shape (size + mode + symlink flag)
        for key in ("item_size", "mode_perms", "is_symlink"):
            if af[key] != bf[key]:
                out["file_shape_diffs"].append({
                    "rel_path": rel, "key": key,
                    "a": af[key], "b": bf[key],
                })
        # Chunk pattern (the chunker-comparison signal)
        if af["chunk_sizes"] != bf["chunk_sizes"]:
            out["chunk_pattern_diffs"].append({
                "rel_path": rel,
                "a_chunks": af["chunk_sizes"],
                "b_chunks": bf["chunk_sizes"],
            })


def _dict_diff(left: Dict[str, Any], right: Dict[str, Any]) -> Dict[str, Any]:
    """Keys present in ``left`` but not in ``right`` (or with
    different value types)."""
    out: Dict[str, Any] = {}
    for k, v in left.items():
        if k not in right:
            out[k] = v
        elif isinstance(v, dict) and isinstance(right[k], dict):
            sub = _dict_diff(v, right[k])
            if sub:
                out[k] = sub
        elif right[k] != v:
            out[k] = {"a": v, "b": right[k]}
    return out
