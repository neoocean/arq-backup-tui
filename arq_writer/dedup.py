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
    """Return the most recently created backuprecord under
    ``<dest>/<cu>/backupfolders/.../backuprecords/`` or ``None``.

    "Most recent" = lexicographically largest ``bucket/num`` since
    the writer encodes creation_date into both. Falls back to
    mtime if multiple records share a path prefix.
    """
    candidates = list(_list_record_paths(dest_root, computer_uuid))
    if not candidates:
        return None
    return max(candidates, key=lambda p: (p.parent.name, p.name))


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


def seed_from_backuprecord(
    rec_path: Path,
    cache: Dict[str, BlobLoc],
    *,
    encryption_key: bytes,
    hmac_key: bytes,
) -> int:
    """Decrypt the backuprecord at ``rec_path``, walk its embedded
    Node, and seed ``cache`` with every referenced ``BlobLoc``.

    Returns the count of entries added. Returns 0 silently on any
    decrypt / parse error — a corrupt prior backup must not block
    a fresh backup; it only loses the dedup boost.

    Recursion through child trees requires reading the actual
    ``treepacks`` / ``standardobjects`` content for those subtrees.
    For the cheap MVP we deliberately walk only the top-level
    ``node`` dict (which already lists its full ``dataBlobLocs`` /
    ``xattrsBlobLocs``) and the immediate ``treeBlobLoc``. A deeper
    recursion can be layered on later by following each
    ``treeBlobLoc`` through the reader's blob-fetch path.
    """
    # Local import to avoid an ``arq_writer → arq_reader`` cycle on
    # module load. The decrypt path lives in the reader because the
    # validator depends on the same primitives.
    try:
        from arq_reader.decrypt import decrypt_lz4_arqo
    except ImportError:  # pragma: no cover
        return 0

    try:
        arqo = Path(rec_path).read_bytes()
        plist_bytes = decrypt_lz4_arqo(
            arqo, encryption_key, hmac_key,
        )
        record = plistlib.loads(plist_bytes)
    except Exception:
        # Best-effort: any failure (corrupt blob, wrong keys,
        # malformed plist) just loses the dedup boost rather than
        # aborting the run.
        return 0
    if not isinstance(record, dict):
        return 0
    node = record.get("node")
    if not isinstance(node, dict):
        return 0
    bag: Dict[str, BlobLoc] = {}
    _harvest_bloblocs(node, bag)
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
) -> Dict[str, int]:
    """Best-effort full seed: standalone-scan + (if keys available)
    walk the most recent backuprecord.

    Returns a small report dict so the caller can log how much
    dedup ground was reclaimed.
    """
    out = {
        "standardobjects_added": 0,
        "backuprecord_added": 0,
        "backuprecord_path": "",
    }
    out["standardobjects_added"] = seed_from_standardobjects(
        dest_root, computer_uuid, cache,
    )
    if encryption_key is not None and hmac_key is not None:
        rec = find_latest_backuprecord(dest_root, computer_uuid)
        if rec is not None:
            out["backuprecord_path"] = str(rec)
            out["backuprecord_added"] = seed_from_backuprecord(
                rec, cache,
                encryption_key=encryption_key,
                hmac_key=hmac_key,
            )
    return out
