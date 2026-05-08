"""Cross-run deduplication seeding.

The within-run dedup cache (``Backup._written_blobs``) reuses
identical-content blobs encountered during one ``build_backup``
invocation. This module extends that to **prior runs** against the
same destination — without it, re-running ``build_backup`` on an
unchanged source tree wastes I/O re-encrypting every byte.

Two seeding strategies, each handling a different storage shape:

1. :func:`seed_from_standardobjects` walks
   ``<dest>/<computer-uuid>/standardobjects/<2-hex>/<62-hex>``
   and adds one ``BlobLoc`` per existing standalone object. The
   filename literally is the SHA-256 ``blob_id``, so we don't have
   to decrypt anything.

2. :func:`seed_from_backuprecord` decrypts the most recent
   backuprecord, walks its tree, and adds every ``BlobLoc`` it
   references — including packed locations. This is the only way
   to recover packed-mode dedup, because packed BlobLocs encode
   ``(relativePath, offset, length)`` tuples that aren't
   reconstructible from filesystem listings alone.

Both helpers are pure: they accept the cache dict (or anything
``Mapping[str, BlobLoc]``-shaped) and return the count of entries
added. They don't touch the network, don't decrypt unless explicitly
asked, and return early on any malformed input — corrupt
destinations don't crash the backup, they just dedup less.
"""

from __future__ import annotations

import os
import plistlib
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from .constants import (
    BACKUPFOLDERS_DIR,
    BACKUPRECORDS_DIR,
    BLOBPACKS_DIR,
    LARGEBLOBPACKS_DIR,
    STANDARDOBJECTS_DIR,
    TREEPACKS_DIR,
)
from .types import BlobLoc

BLOB_ID_HEX_LEN = 64


def _is_blob_id_path(shard: str, rest: str) -> bool:
    """Cheap filter: shard is 2 hex chars, rest is 62 hex chars,
    and concat is a valid hex blob_id."""
    if len(shard) != 2 or len(rest) != BLOB_ID_HEX_LEN - 2:
        return False
    cand = shard + rest
    try:
        int(cand, 16)
    except ValueError:
        return False
    return True


def seed_from_standardobjects(
    dest_root: Path,
    computer_uuid: str,
    cache: Dict[str, BlobLoc],
) -> int:
    """Scan ``standardobjects/`` and populate ``cache`` with one
    BlobLoc per existing file.

    Returns the count of entries added. Files already in the cache
    are not overwritten — the within-run cache always wins.

    Cheap: ``os.scandir`` based, no decrypt, no parse. On a
    destination with N standalone objects the cost is O(N) directory
    entries + 64 bytes per BlobLoc.
    """
    so_root = (
        Path(dest_root) / computer_uuid / STANDARDOBJECTS_DIR
    )
    if not so_root.is_dir():
        return 0
    added = 0
    try:
        shards = list(os.scandir(so_root))
    except OSError:
        return 0
    for shard_entry in shards:
        if not shard_entry.is_dir():
            continue
        shard = shard_entry.name
        try:
            files = list(os.scandir(shard_entry.path))
        except OSError:
            continue
        for file_entry in files:
            if not file_entry.is_file():
                continue
            rest = file_entry.name
            if not _is_blob_id_path(shard, rest):
                continue
            blob_id = shard + rest
            if blob_id in cache:
                continue
            try:
                size = file_entry.stat().st_size
            except OSError:
                continue
            cache[blob_id] = BlobLoc(
                blobIdentifier=blob_id,
                isPacked=False,
                relativePath=(
                    f"/{computer_uuid}/{STANDARDOBJECTS_DIR}/"
                    f"{shard}/{rest}"
                ),
                offset=0,
                length=size,
            )
            added += 1
    return added


# ---------------------------------------------------------------------------
# Backuprecord-walking seed (covers packed mode)
# ---------------------------------------------------------------------------


def _list_record_paths(
    dest_root: Path, computer_uuid: str,
) -> Iterable[Path]:
    """Yield every backuprecord file under
    ``<dest>/<cu>/backupfolders/<folder>/backuprecords/<bucket>/<num>.backuprecord``.
    """
    bf_root = (
        Path(dest_root) / computer_uuid / BACKUPFOLDERS_DIR
    )
    if not bf_root.is_dir():
        return
    for folder_entry in os.scandir(bf_root):
        if not folder_entry.is_dir():
            continue
        rec_root = (
            Path(folder_entry.path) / BACKUPRECORDS_DIR
        )
        if not rec_root.is_dir():
            continue
        for bucket in os.scandir(rec_root):
            if not bucket.is_dir():
                continue
            for rec in os.scandir(bucket.path):
                if rec.is_file() and rec.name.endswith(".backuprecord"):
                    yield Path(rec.path)


def find_latest_backuprecord(
    dest_root: Path, computer_uuid: str,
) -> Optional[Path]:
    """Return the most recently created backuprecord across every
    folder under ``<dest>/<cu>/backupfolders/.../backuprecords/``,
    or ``None`` if the destination has no records.

    "Most recent" = lexicographically largest ``bucket/num`` since
    the writer encodes creation_date into both. Falls back to
    mtime if multiple records share a path prefix.
    """
    candidates = list(_list_record_paths(dest_root, computer_uuid))
    if not candidates:
        return None
    return max(candidates, key=lambda p: (p.parent.name, p.name))


def find_latest_backuprecord_per_folder(
    dest_root: Path, computer_uuid: str,
) -> Dict[str, Path]:
    """Return ``{folder_uuid: latest_backuprecord_path}`` for every
    folder under the given computer.

    Arq 7 stores blobs at the **computer** level (one shared
    ``standardobjects/`` / ``treepacks/`` / ``blobpacks/`` /
    ``largeblobpacks/`` tree under the computer UUID), so dedup
    against existing pack-stored content needs to consider blobs
    from every folder, not just the globally-newest one. This
    helper drives the per-folder seed loop in
    :func:`seed_existing_destination`.

    Path layout: ``<dest>/<cu>/backupfolders/<folder_uuid>/backuprecords/<bucket>/<num>.backuprecord``.
    The folder's "latest" record is the lexicographic max of
    ``(bucket, num)``.
    """
    bf_root = Path(dest_root) / computer_uuid / BACKUPFOLDERS_DIR
    if not bf_root.is_dir():
        return {}
    out: Dict[str, Path] = {}
    try:
        folder_entries = list(os.scandir(bf_root))
    except OSError:
        return {}
    for folder_entry in folder_entries:
        if not folder_entry.is_dir():
            continue
        folder_uuid = folder_entry.name
        rec_root = Path(folder_entry.path) / BACKUPRECORDS_DIR
        if not rec_root.is_dir():
            continue
        latest: Optional[Path] = None
        try:
            for bucket in os.scandir(rec_root):
                if not bucket.is_dir():
                    continue
                for rec in os.scandir(bucket.path):
                    if not (
                        rec.is_file()
                        and rec.name.endswith(".backuprecord")
                    ):
                        continue
                    p = Path(rec.path)
                    if (
                        latest is None
                        or (p.parent.name, p.name)
                        > (latest.parent.name, latest.name)
                    ):
                        latest = p
        except OSError:
            continue
        if latest is not None:
            out[folder_uuid] = latest
    return out


def _harvest_bloblocs(
    node_dict: Dict[str, Any],
    bag: Dict[str, BlobLoc],
) -> None:
    """Walk a Node dict in a backuprecord plist and harvest every
    BlobLoc into ``bag`` keyed by ``blobIdentifier``."""
    for loc_dict in node_dict.get("dataBlobLocs", ()) or ():
        _try_add_loc(loc_dict, bag)
    for loc_dict in node_dict.get("xattrsBlobLocs", ()) or ():
        _try_add_loc(loc_dict, bag)
    tree_loc = node_dict.get("treeBlobLoc")
    if isinstance(tree_loc, dict):
        _try_add_loc(tree_loc, bag)


def _try_add_loc(d: Dict[str, Any], bag: Dict[str, BlobLoc]) -> None:
    bid = d.get("blobIdentifier")
    if not isinstance(bid, str) or len(bid) != BLOB_ID_HEX_LEN:
        return
    if bid in bag:
        return
    try:
        bag[bid] = BlobLoc(
            blobIdentifier=bid,
            isPacked=bool(d.get("isPacked", False)),
            relativePath=str(d.get("relativePath") or ""),
            offset=int(d.get("offset") or 0),
            length=int(d.get("length") or 0),
            stretchEncryptionKey=bool(
                d.get("stretchEncryptionKey", True)
            ),
            compressionType=int(d.get("compressionType") or 0),
        )
    except (TypeError, ValueError):
        pass


def _fetch_tree_blob(
    loc: BlobLoc,
    dest_root: Path,
    encryption_key: bytes,
    hmac_key: bytes,
    *,
    openssl_path: str = "openssl",
):
    """Fetch + decrypt + (LZ4-unwrap) a tree blob at ``loc`` and
    return a parsed ``Tree`` (or ``None`` on any failure)."""
    try:
        from arq_reader.decrypt import decrypt_encrypted_object
        from arq_reader.parse import parse_tree
        from .lz4_block import lz4_unwrap
    except ImportError:  # pragma: no cover
        return None
    try:
        if loc.isPacked:
            with open(
                Path(dest_root).joinpath(loc.relativePath.lstrip("/")),
                "rb",
            ) as f:
                f.seek(loc.offset)
                raw = f.read(loc.length)
        else:
            raw = Path(dest_root).joinpath(
                loc.relativePath.lstrip("/")
            ).read_bytes()
        if raw[:4] == b"ARQO":
            raw = decrypt_encrypted_object(
                raw, encryption_key, hmac_key,
                openssl_path=openssl_path,
            )
        if loc.compressionType == 2:
            raw = lz4_unwrap(raw)
        return parse_tree(raw)
    except Exception:
        return None


def _harvest_tree_recursive(
    tree_loc: BlobLoc,
    dest_root: Path,
    bag: Dict[str, BlobLoc],
    encryption_key: bytes,
    hmac_key: bytes,
    *,
    openssl_path: str = "openssl",
    visited: Optional[set] = None,
) -> None:
    """Walk a Tree and every nested Tree, accumulating every
    referenced BlobLoc into ``bag``.

    Each Tree blob is decrypted at most once per call (``visited``
    guards re-entry by ``blobIdentifier``).
    """
    if visited is None:
        visited = set()
    if tree_loc.blobIdentifier in visited:
        return
    visited.add(tree_loc.blobIdentifier)
    bag.setdefault(tree_loc.blobIdentifier, tree_loc)
    tree = _fetch_tree_blob(
        tree_loc, dest_root, encryption_key, hmac_key,
        openssl_path=openssl_path,
    )
    if tree is None:
        return
    # Local import to avoid module-import-time cycle.
    from .types import FileNode, TreeNode
    for child in tree.children:
        node = child.node
        if isinstance(node, TreeNode):
            _harvest_tree_recursive(
                node.treeBlobLoc, dest_root, bag,
                encryption_key, hmac_key,
                openssl_path=openssl_path,
                visited=visited,
            )
        elif isinstance(node, FileNode):
            for loc in node.dataBlobLocs:
                bag.setdefault(loc.blobIdentifier, loc)
            for loc in node.xattrsBlobLocs:
                bag.setdefault(loc.blobIdentifier, loc)
        # Some Node implementations (e.g. legacy ones) carry
        # xattrsBlobLocs at the Node level; both branches above
        # already cover that. ACL blob (if present) is exposed via
        # node.aclBlobLoc; harvest it too.
        acl = getattr(node, "aclBlobLoc", None)
        if acl is not None:
            bag.setdefault(acl.blobIdentifier, acl)


def seed_from_backuprecord(
    rec_path: Path,
    cache: Dict[str, BlobLoc],
    *,
    encryption_key: bytes,
    hmac_key: bytes,
    dest_root: Optional[Path] = None,
    openssl_path: str = "openssl",
) -> int:
    """Decrypt the backuprecord at ``rec_path``, walk its embedded
    Node and every reachable Tree blob, and seed ``cache`` with
    every referenced ``BlobLoc``.

    When ``dest_root`` is provided, the walk recursively fetches and
    parses every Tree blob the record references — covering the
    deep ``dataBlobLocs`` of files in subdirectories. Without it,
    only top-level locations (the root ``treeBlobLoc`` and any
    inlined data/xattrs locs) are harvested. Multi-folder packed-
    mode dedup against the destination's existing pack files
    requires the recursive form.

    Returns the count of entries added. Returns 0 silently on any
    decrypt / parse error — a corrupt prior backup must not block
    a fresh backup; it only loses the dedup boost.
    """
    try:
        from arq_reader.decrypt import decrypt_lz4_arqo
    except ImportError:  # pragma: no cover
        return 0

    try:
        arqo = Path(rec_path).read_bytes()
        plist_bytes = decrypt_lz4_arqo(
            arqo, encryption_key, hmac_key,
            openssl_path=openssl_path,
        )
        record = plistlib.loads(plist_bytes)
    except Exception:
        return 0
    if not isinstance(record, dict):
        return 0
    node = record.get("node")
    if not isinstance(node, dict):
        return 0
    bag: Dict[str, BlobLoc] = {}
    _harvest_bloblocs(node, bag)
    # Recursive walk for full coverage when dest_root is known.
    if dest_root is not None and node.get("isTree"):
        tloc_dict = node.get("treeBlobLoc")
        if isinstance(tloc_dict, dict):
            try:
                root_loc = BlobLoc(
                    blobIdentifier=str(tloc_dict["blobIdentifier"]),
                    isPacked=bool(tloc_dict.get("isPacked", False)),
                    relativePath=str(
                        tloc_dict.get("relativePath", "")
                    ),
                    offset=int(tloc_dict.get("offset", 0)),
                    length=int(tloc_dict.get("length", 0)),
                    stretchEncryptionKey=bool(
                        tloc_dict.get("stretchEncryptionKey", True)
                    ),
                    compressionType=int(
                        tloc_dict.get("compressionType", 2)
                    ),
                )
                _harvest_tree_recursive(
                    root_loc, Path(dest_root), bag,
                    encryption_key, hmac_key,
                    openssl_path=openssl_path,
                )
            except (KeyError, TypeError, ValueError):
                pass
    added = 0
    for bid, loc in bag.items():
        if bid in cache:
            continue
        cache[bid] = loc
        added += 1
    return added


# ---------------------------------------------------------------------------
# Convenience entry point
# ---------------------------------------------------------------------------


def seed_existing_destination(
    dest_root: Path,
    computer_uuid: str,
    cache: Dict[str, BlobLoc],
    *,
    encryption_key: Optional[bytes] = None,
    hmac_key: Optional[bytes] = None,
) -> Dict[str, Any]:
    """Best-effort full seed: standalone-scan + (if keys available)
    walk every folder's most recent backuprecord.

    Multi-folder coverage matches Arq 7's computer-scoped blob
    storage: ``<cu>/blobpacks`` / ``<cu>/treepacks`` are shared
    across every folder, so a new backup of folder A should dedup
    against pack-stored content from folder B. We therefore walk
    one record per folder rather than the globally-latest one.

    Returns a small report dict so the caller can log how much
    dedup ground was reclaimed.
    """
    out: Dict[str, Any] = {
        "standardobjects_added": 0,
        "folders_walked": 0,
        "backuprecord_added": 0,
        "backuprecord_paths": [],
    }
    out["standardobjects_added"] = seed_from_standardobjects(
        dest_root, computer_uuid, cache,
    )
    if encryption_key is None or hmac_key is None:
        return out
    per_folder = find_latest_backuprecord_per_folder(
        dest_root, computer_uuid,
    )
    paths_walked: list = []
    total_added = 0
    for _folder_uuid, rec in per_folder.items():
        added = seed_from_backuprecord(
            rec, cache,
            encryption_key=encryption_key,
            hmac_key=hmac_key,
            dest_root=Path(dest_root),
        )
        total_added += added
        paths_walked.append(str(rec))
    out["folders_walked"] = len(per_folder)
    out["backuprecord_paths"] = paths_walked
    out["backuprecord_added"] = total_added
    return out
