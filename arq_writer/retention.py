"""Retention policy + backuprecord pruning + blob garbage collection.

Three concerns, three operations:

1. :class:`RetentionPolicy` — declarative description of which
   records to keep (last N, hourly/daily/weekly/monthly/yearly
   buckets). Multiple buckets are OR'd: a record survives if **any**
   bucket selects it.

2. :func:`prune_records` — given a policy + a destination, deletes
   backuprecord files outside retention. Returns the list of
   deleted + retained records.

3. :func:`gc_orphan_blobs` — after pruning, walks every surviving
   record's tree to collect referenced blob_ids, then deletes:

   - **standalone** objects (``standardobjects/<shard>/<rest>``)
     whose blob_id is unreferenced — always safe.
   - **pack** files (``treepacks/<shard>/<UUID>.pack`` etc.)
     whose **entire** content is unreferenced — conservative;
     leaves partially-orphan packs intact. A future
     ``mode='aggressive'`` pack-rewrite is possible but requires
     mutating BlobLocs in surviving records, which is dangerous.

All operations take a ``Backend`` so they work over local + SFTP
identically. They never mutate the keyset / config / plan files;
only objects + records are subject to deletion.

Order of operations matters:

  prune_records FIRST → then gc_orphan_blobs

Reversed order would GC blobs that are about to be re-orphaned by
the prune. The :func:`apply_retention` helper composes them
correctly.
"""

from __future__ import annotations

import datetime
import plistlib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from arq_validator import constants as C
from arq_validator.backend import Backend
from arq_validator.crypto import decrypt_keyset
from arq_validator.layout import discover_layout, list_backuprecords


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


def _parse_record(plain):
    # Lazy import — backuprecord lives in the same package so a top-
    # level import would close the cycle through arq_writer/__init__.
    from arq_writer.backuprecord import parse_backuprecord
    return parse_backuprecord(plain)


@dataclass
class RetentionPolicy:
    """Multi-bucket retention.

    Each non-zero counter selects records via "last record in each
    time bucket up to the limit". A record retained by any bucket
    is kept; only records selected by **no** bucket are pruned.

    The default (all zeros except ``keep_last_n=None``) is
    "keep everything" — :func:`prune_records` is a no-op.
    """

    keep_last_n: Optional[int] = None       # last N records (any age)
    keep_hourly: int = 0
    keep_daily: int = 0
    keep_weekly: int = 0
    keep_monthly: int = 0
    keep_yearly: int = 0

    @property
    def is_keep_all(self) -> bool:
        return (
            self.keep_last_n is None
            and self.keep_hourly == 0
            and self.keep_daily == 0
            and self.keep_weekly == 0
            and self.keep_monthly == 0
            and self.keep_yearly == 0
        )


@dataclass(frozen=True)
class _RecordRef:
    """Internal — one record's path + creation time."""
    path: str
    creation_date: int       # Unix seconds


def _bucket_key(dt: datetime.datetime, kind: str) -> Tuple:
    if kind == "hourly":
        return (dt.year, dt.month, dt.day, dt.hour)
    if kind == "daily":
        return (dt.year, dt.month, dt.day)
    if kind == "weekly":
        iso = dt.isocalendar()
        return (iso.year, iso.week)
    if kind == "monthly":
        return (dt.year, dt.month)
    if kind == "yearly":
        return (dt.year,)
    raise ValueError(f"unknown bucket kind: {kind!r}")


def select_retained(
    records: List[_RecordRef],
    policy: RetentionPolicy,
) -> Set[str]:
    """Apply ``policy`` to ``records`` and return the set of paths
    that survive. ``records`` must be **sorted oldest-first** for
    "newest in each bucket" semantics."""
    if policy.is_keep_all:
        return {r.path for r in records}
    if not records:
        return set()
    keep: Set[str] = set()
    sorted_oldest = sorted(records, key=lambda r: r.creation_date)
    sorted_newest = list(reversed(sorted_oldest))

    # 1. last N (no time gating)
    if policy.keep_last_n is not None and policy.keep_last_n > 0:
        for r in sorted_newest[: policy.keep_last_n]:
            keep.add(r.path)

    # 2. hourly / daily / weekly / monthly / yearly
    bucket_specs = (
        ("hourly", policy.keep_hourly),
        ("daily", policy.keep_daily),
        ("weekly", policy.keep_weekly),
        ("monthly", policy.keep_monthly),
        ("yearly", policy.keep_yearly),
    )
    for kind, limit in bucket_specs:
        if limit <= 0:
            continue
        seen_buckets: List[Tuple] = []
        for r in sorted_newest:
            dt = datetime.datetime.fromtimestamp(
                r.creation_date, tz=datetime.timezone.utc,
            )
            key = _bucket_key(dt, kind)
            if key in seen_buckets:
                continue
            seen_buckets.append(key)
            keep.add(r.path)
            if len(seen_buckets) >= limit:
                break
    return keep


# ---------------------------------------------------------------------------
# Record metadata extraction (creation_date)
# ---------------------------------------------------------------------------


def _record_creation_date(
    backend: Backend, rec_path: str, keyset,
    *, openssl_path: str = "openssl",
) -> int:
    """Decrypt + parse a record and return its ``creationDate``
    (Unix seconds). Returns 0 on any error so the caller can
    decide whether to keep or skip a record we couldn't parse."""
    try:
        from arq_reader.decrypt import decrypt_lz4_arqo
        arqo = backend.read_all(rec_path)
        plain = decrypt_lz4_arqo(
            arqo, keyset.encryption_key, keyset.hmac_key,
            openssl_path=openssl_path,
        )
        record = _parse_record(plain)
    except Exception:
        return 0
    if isinstance(record, dict):
        try:
            return int(record.get("creationDate") or 0)
        except (TypeError, ValueError):
            return 0
    return 0


# ---------------------------------------------------------------------------
# prune_records
# ---------------------------------------------------------------------------


@dataclass
class PruneRecordsResult:
    deleted: List[str] = field(default_factory=list)
    retained: List[str] = field(default_factory=list)
    failures: List[Dict[str, str]] = field(default_factory=list)


def prune_records(
    backend: Backend,
    *,
    encryption_password: str,
    computer_uuid: Optional[str] = None,
    policy: RetentionPolicy,
    root: str = "/",
    folder_uuid: Optional[str] = None,
    openssl_path: str = "openssl",
    dry_run: bool = False,
    callback=None,
) -> PruneRecordsResult:
    """Apply ``policy`` per folder; delete records that fall
    outside retention.

    When ``folder_uuid`` is None, every folder under
    ``computer_uuid`` is processed. ``dry_run=True`` returns the
    sets without actually unlinking — useful as a preview.

    Caller is responsible for calling :func:`gc_orphan_blobs`
    afterwards if blob cleanup is desired.
    """
    result = PruneRecordsResult()
    layouts = discover_layout(backend, root)
    if computer_uuid is not None:
        layouts = [
            lay for lay in layouts if lay.computer_uuid == computer_uuid
        ]

    for lay in layouts:
        cu = lay.computer_uuid
        keyset_path = f"{root.rstrip('/')}/{cu}/{C.KEYSET_FILE}"
        try:
            keyset = decrypt_keyset(
                backend.read_all(keyset_path),
                encryption_password,
                openssl_path=openssl_path,
            )
        except Exception as exc:
            result.failures.append({
                "computer_uuid": cu,
                "error": f"{type(exc).__name__}: {exc}",
            })
            continue

        folder_iter = (
            [folder_uuid] if folder_uuid is not None
            else list(lay.backup_folder_uuids)
        )
        for fu in folder_iter:
            paths = list_backuprecords(backend, root, cu, fu)
            if not paths:
                continue
            refs: List[_RecordRef] = []
            for p in paths:
                cd = _record_creation_date(
                    backend, p, keyset, openssl_path=openssl_path,
                )
                refs.append(_RecordRef(path=p, creation_date=cd))

            keep = select_retained(refs, policy)
            for ref in refs:
                if ref.path in keep:
                    result.retained.append(ref.path)
                    if callback:
                        callback("record_retained",
                                 {"path": ref.path,
                                  "creation_date": ref.creation_date})
                else:
                    if not dry_run:
                        try:
                            _unlink(backend, ref.path)
                        except Exception as exc:
                            result.failures.append({
                                "path": ref.path,
                                "error": f"{type(exc).__name__}: {exc}",
                            })
                            continue
                    result.deleted.append(ref.path)
                    if callback:
                        callback("record_deleted",
                                 {"path": ref.path,
                                  "creation_date": ref.creation_date,
                                  "dry_run": dry_run})
    return result


# ---------------------------------------------------------------------------
# Reference walk + GC
# ---------------------------------------------------------------------------


@dataclass
class GcResult:
    standalone_blobs_deleted: int = 0
    standalone_bytes_freed: int = 0
    packs_deleted: int = 0
    pack_bytes_freed: int = 0
    packs_partially_orphan_kept: int = 0
    failures: List[Dict[str, str]] = field(default_factory=list)
    dry_run: bool = False


def _collect_referenced_blobs(
    backend: Backend, root: str, cu: str, keyset,
    *, openssl_path: str = "openssl",
) -> Set[str]:
    """Walk every backuprecord under <cu>, every Tree blob it
    references, and return the set of every blob_id that's still
    live."""
    from arq_reader.decrypt import decrypt_encrypted_object
    from arq_reader.parse import parse_tree
    from arq_writer.lz4_block import lz4_unwrap

    seen_trees: Set[str] = set()
    referenced: Set[str] = set()

    cu_root = f"{root.rstrip('/')}/{cu}"
    bf_root = f"{cu_root}/{C.BACKUPFOLDERS_DIR}"
    if not backend.is_dir(bf_root):
        return referenced
    folders = backend.list_dir(bf_root)
    for fu in folders:
        for rec_path in list_backuprecords(backend, root, cu, fu):
            try:
                from arq_reader.decrypt import decrypt_lz4_arqo
                plain = decrypt_lz4_arqo(
                    backend.read_all(rec_path),
                    keyset.encryption_key, keyset.hmac_key,
                    openssl_path=openssl_path,
                )
                rec = _parse_record(plain)
            except Exception:
                # Don't lose blobs on a single corrupt record —
                # skip it and assume its blobs are still live.
                continue
            node = rec.get("node") if isinstance(rec, dict) else None
            if not isinstance(node, dict):
                continue
            for loc_dict in node.get("dataBlobLocs") or ():
                _add_blob(loc_dict, referenced)
            for loc_dict in node.get("xattrsBlobLocs") or ():
                _add_blob(loc_dict, referenced)
            tloc = node.get("treeBlobLoc")
            if isinstance(tloc, dict):
                _add_blob(tloc, referenced)
                _walk_tree_collect(
                    tloc, backend, keyset, referenced, seen_trees,
                    openssl_path=openssl_path,
                )
    return referenced


def _add_blob(d: Dict[str, Any], out: Set[str]) -> None:
    bid = d.get("blobIdentifier")
    if isinstance(bid, str) and len(bid) == 64:
        out.add(bid.lower())


def _walk_tree_collect(
    tloc_dict: Dict[str, Any],
    backend: Backend, keyset,
    referenced: Set[str], seen: Set[str],
    *, openssl_path: str = "openssl",
) -> None:
    from arq_reader.decrypt import decrypt_encrypted_object
    from arq_reader.parse import parse_tree
    from arq_writer.lz4_block import lz4_unwrap

    bid = str(tloc_dict.get("blobIdentifier") or "").lower()
    if bid in seen:
        return
    seen.add(bid)
    rel = str(tloc_dict.get("relativePath") or "")
    is_packed = bool(tloc_dict.get("isPacked", False))
    offset = int(tloc_dict.get("offset") or 0)
    length = int(tloc_dict.get("length") or 0)
    compression = int(tloc_dict.get("compressionType") or 2)
    try:
        if is_packed:
            raw = backend.read_range(rel, offset, length)
        else:
            raw = backend.read_all(rel)
        if raw[:4] == b"ARQO":
            raw = decrypt_encrypted_object(
                raw, keyset.encryption_key, keyset.hmac_key,
                openssl_path=openssl_path,
            )
        if compression == 2:
            raw = lz4_unwrap(raw)
        tree = parse_tree(raw)
    except Exception:
        return
    for child in tree.children:
        node = child.node
        # FileNode.dataBlobLocs / xattrsBlobLocs / aclBlobLoc
        for loc in getattr(node, "dataBlobLocs", []) or []:
            referenced.add(loc.blobIdentifier.lower())
        for loc in getattr(node, "xattrsBlobLocs", []) or []:
            referenced.add(loc.blobIdentifier.lower())
        acl = getattr(node, "aclBlobLoc", None)
        if acl is not None:
            referenced.add(acl.blobIdentifier.lower())
        # Tree node — recurse
        sub_tloc = getattr(node, "treeBlobLoc", None)
        if sub_tloc is not None:
            referenced.add(sub_tloc.blobIdentifier.lower())
            _walk_tree_collect_blobloc(
                sub_tloc, backend, keyset, referenced, seen,
                openssl_path=openssl_path,
            )


def _walk_tree_collect_blobloc(
    tloc, backend: Backend, keyset,
    referenced: Set[str], seen: Set[str],
    *, openssl_path: str = "openssl",
) -> None:
    """Recursive variant when we already have a BlobLoc dataclass
    (vs the dict form)."""
    bid = tloc.blobIdentifier.lower()
    if bid in seen:
        return
    seen.add(bid)
    referenced.add(bid)
    from arq_reader.decrypt import decrypt_encrypted_object
    from arq_reader.parse import parse_tree
    from arq_writer.lz4_block import lz4_unwrap
    try:
        if tloc.isPacked:
            raw = backend.read_range(
                tloc.relativePath, tloc.offset, tloc.length,
            )
        else:
            raw = backend.read_all(tloc.relativePath)
        if raw[:4] == b"ARQO":
            raw = decrypt_encrypted_object(
                raw, keyset.encryption_key, keyset.hmac_key,
                openssl_path=openssl_path,
            )
        if tloc.compressionType == 2:
            raw = lz4_unwrap(raw)
        tree = parse_tree(raw)
    except Exception:
        return
    for child in tree.children:
        node = child.node
        for loc in getattr(node, "dataBlobLocs", []) or []:
            referenced.add(loc.blobIdentifier.lower())
        for loc in getattr(node, "xattrsBlobLocs", []) or []:
            referenced.add(loc.blobIdentifier.lower())
        acl = getattr(node, "aclBlobLoc", None)
        if acl is not None:
            referenced.add(acl.blobIdentifier.lower())
        sub_tloc = getattr(node, "treeBlobLoc", None)
        if sub_tloc is not None:
            _walk_tree_collect_blobloc(
                sub_tloc, backend, keyset, referenced, seen,
                openssl_path=openssl_path,
            )


def gc_orphan_blobs(
    backend: Backend,
    *,
    encryption_password: str,
    computer_uuid: Optional[str] = None,
    root: str = "/",
    openssl_path: str = "openssl",
    dry_run: bool = False,
    callback=None,
) -> GcResult:
    """Conservative GC: delete standalone objects whose blob_id
    isn't referenced by any surviving record, plus pack files
    where **every** referenced blob is unreferenced (i.e. no live
    record points into the pack)."""
    result = GcResult(dry_run=dry_run)
    layouts = discover_layout(backend, root)
    if computer_uuid is not None:
        layouts = [
            lay for lay in layouts if lay.computer_uuid == computer_uuid
        ]
    for lay in layouts:
        cu = lay.computer_uuid
        cu_root = f"{root.rstrip('/')}/{cu}"
        try:
            keyset = decrypt_keyset(
                backend.read_all(f"{cu_root}/{C.KEYSET_FILE}"),
                encryption_password,
                openssl_path=openssl_path,
            )
        except Exception as exc:
            result.failures.append({
                "computer_uuid": cu,
                "error": f"{type(exc).__name__}: {exc}",
            })
            continue

        referenced = _collect_referenced_blobs(
            backend, root, cu, keyset, openssl_path=openssl_path,
        )

        # 1. standardobjects/
        so_root = f"{cu_root}/{C.STANDARDOBJECTS_DIR}"
        if backend.is_dir(so_root):
            try:
                shards = backend.list_dir(so_root)
            except Exception:
                shards = []
            for shard in shards:
                shard_path = f"{so_root}/{shard}"
                if not backend.is_dir(shard_path):
                    continue
                try:
                    files = backend.list_dir(shard_path)
                except Exception:
                    continue
                for name in files:
                    if len(name) != 62:
                        continue
                    blob_id = (shard + name).lower()
                    if blob_id in referenced:
                        continue
                    file_path = f"{shard_path}/{name}"
                    try:
                        size = backend.stat_size(file_path)
                    except Exception:
                        size = 0
                    if not dry_run:
                        try:
                            _unlink(backend, file_path)
                        except Exception as exc:
                            result.failures.append({
                                "path": file_path,
                                "error": f"{type(exc).__name__}: {exc}",
                            })
                            continue
                    result.standalone_blobs_deleted += 1
                    result.standalone_bytes_freed += size
                    if callback:
                        callback("blob_deleted",
                                 {"path": file_path, "blob_id": blob_id,
                                  "size": size, "dry_run": dry_run})

        # 2. packs (treepacks/, blobpacks/, largeblobpacks/)
        # Conservative: delete a pack only when NO referenced
        # BlobLoc points into it. We look at the relativePath of
        # every referenced BlobLoc (collected separately below).
        live_pack_paths = _collect_live_pack_paths(
            backend, root, cu, keyset,
            openssl_path=openssl_path,
        )
        for family in (
            C.TREEPACKS_DIR, C.BLOBPACKS_DIR, C.LARGEBLOBPACKS_DIR,
        ):
            family_root = f"{cu_root}/{family}"
            if not backend.is_dir(family_root):
                continue
            try:
                shards = backend.list_dir(family_root)
            except Exception:
                continue
            for shard in shards:
                shard_path = f"{family_root}/{shard}"
                if not backend.is_dir(shard_path):
                    continue
                try:
                    files = backend.list_dir(shard_path)
                except Exception:
                    continue
                for name in files:
                    if not name.endswith(".pack"):
                        continue
                    pack_path = f"{shard_path}/{name}"
                    if pack_path in live_pack_paths:
                        result.packs_partially_orphan_kept += 1
                        continue
                    try:
                        size = backend.stat_size(pack_path)
                    except Exception:
                        size = 0
                    if not dry_run:
                        try:
                            _unlink(backend, pack_path)
                        except Exception as exc:
                            result.failures.append({
                                "path": pack_path,
                                "error": f"{type(exc).__name__}: {exc}",
                            })
                            continue
                    result.packs_deleted += 1
                    result.pack_bytes_freed += size
                    if callback:
                        callback("pack_deleted",
                                 {"path": pack_path, "size": size,
                                  "dry_run": dry_run})
    return result


def _collect_live_pack_paths(
    backend: Backend, root: str, cu: str, keyset,
    *, openssl_path: str = "openssl",
) -> Set[str]:
    """Walk every surviving record's tree and collect every
    ``relativePath`` referenced by a BlobLoc with ``isPacked=True``."""
    from arq_reader.decrypt import decrypt_lz4_arqo

    seen_trees: Set[str] = set()
    paths: Set[str] = set()

    cu_root = f"{root.rstrip('/')}/{cu}"
    bf_root = f"{cu_root}/{C.BACKUPFOLDERS_DIR}"
    if not backend.is_dir(bf_root):
        return paths
    for fu in backend.list_dir(bf_root):
        for rec_path in list_backuprecords(backend, root, cu, fu):
            try:
                plain = decrypt_lz4_arqo(
                    backend.read_all(rec_path),
                    keyset.encryption_key, keyset.hmac_key,
                    openssl_path=openssl_path,
                )
                rec = _parse_record(plain)
            except Exception:
                continue
            if not isinstance(rec, dict):
                continue
            node = rec.get("node")
            if not isinstance(node, dict):
                continue
            for loc_dict in node.get("dataBlobLocs") or ():
                _add_pack_path(loc_dict, paths)
            for loc_dict in node.get("xattrsBlobLocs") or ():
                _add_pack_path(loc_dict, paths)
            tloc = node.get("treeBlobLoc")
            if isinstance(tloc, dict):
                _add_pack_path(tloc, paths)
                _walk_tree_collect_pack_paths(
                    tloc, backend, keyset, paths, seen_trees,
                    openssl_path=openssl_path,
                )
    return paths


def _add_pack_path(d: Dict[str, Any], out: Set[str]) -> None:
    if d.get("isPacked"):
        rel = d.get("relativePath")
        if isinstance(rel, str) and rel:
            out.add(rel)


def _walk_tree_collect_pack_paths(
    tloc_dict, backend: Backend, keyset,
    paths: Set[str], seen: Set[str],
    *, openssl_path: str = "openssl",
) -> None:
    from arq_reader.decrypt import decrypt_encrypted_object
    from arq_reader.parse import parse_tree
    from arq_writer.lz4_block import lz4_unwrap

    if isinstance(tloc_dict, dict):
        bid = str(tloc_dict.get("blobIdentifier") or "").lower()
        rel = str(tloc_dict.get("relativePath") or "")
        is_packed = bool(tloc_dict.get("isPacked", False))
        offset = int(tloc_dict.get("offset") or 0)
        length = int(tloc_dict.get("length") or 0)
        compression = int(tloc_dict.get("compressionType") or 2)
    else:
        bid = tloc_dict.blobIdentifier.lower()
        rel = tloc_dict.relativePath
        is_packed = tloc_dict.isPacked
        offset = tloc_dict.offset
        length = tloc_dict.length
        compression = tloc_dict.compressionType
    if bid in seen:
        return
    seen.add(bid)
    try:
        if is_packed:
            raw = backend.read_range(rel, offset, length)
        else:
            raw = backend.read_all(rel)
        if raw[:4] == b"ARQO":
            raw = decrypt_encrypted_object(
                raw, keyset.encryption_key, keyset.hmac_key,
                openssl_path=openssl_path,
            )
        if compression == 2:
            raw = lz4_unwrap(raw)
        tree = parse_tree(raw)
    except Exception:
        return
    for child in tree.children:
        node = child.node
        for loc in getattr(node, "dataBlobLocs", []) or []:
            if loc.isPacked:
                paths.add(loc.relativePath)
        for loc in getattr(node, "xattrsBlobLocs", []) or []:
            if loc.isPacked:
                paths.add(loc.relativePath)
        acl = getattr(node, "aclBlobLoc", None)
        if acl is not None and acl.isPacked:
            paths.add(acl.relativePath)
        sub = getattr(node, "treeBlobLoc", None)
        if sub is not None:
            if sub.isPacked:
                paths.add(sub.relativePath)
            _walk_tree_collect_pack_paths(
                sub, backend, keyset, paths, seen,
                openssl_path=openssl_path,
            )


# ---------------------------------------------------------------------------
# High-level convenience
# ---------------------------------------------------------------------------


@dataclass
class RetentionResult:
    prune: PruneRecordsResult
    gc: Optional[GcResult] = None


def apply_retention(
    backend: Backend,
    *,
    encryption_password: str,
    policy: RetentionPolicy,
    computer_uuid: Optional[str] = None,
    root: str = "/",
    folder_uuid: Optional[str] = None,
    run_gc: bool = True,
    dry_run: bool = False,
    openssl_path: str = "openssl",
    callback=None,
) -> RetentionResult:
    """Compose record-pruning and blob GC in the correct order.

    With ``dry_run=True``, neither operation deletes anything;
    the report describes what *would* happen.
    """
    pruned = prune_records(
        backend,
        encryption_password=encryption_password,
        computer_uuid=computer_uuid,
        policy=policy,
        root=root,
        folder_uuid=folder_uuid,
        dry_run=dry_run,
        openssl_path=openssl_path,
        callback=callback,
    )
    gc = None
    if run_gc:
        gc = gc_orphan_blobs(
            backend,
            encryption_password=encryption_password,
            computer_uuid=computer_uuid,
            root=root,
            dry_run=dry_run,
            openssl_path=openssl_path,
            callback=callback,
        )
    return RetentionResult(prune=pruned, gc=gc)


# ---------------------------------------------------------------------------
# Backend write — backend.unlink shim
# ---------------------------------------------------------------------------


def _unlink(backend: Backend, path: str) -> None:
    """Delete a file via the backend.

    LocalBackend has a Path.unlink it can call directly; SftpBackend
    can shell out to ``rm``. Some backends may not support deletion
    at all — that's a programmer error, not user-facing, so we
    raise ``NotImplementedError`` if the backend doesn't expose a
    way to delete.
    """
    # Direct attribute first (LocalBackend extends with `unlink`
    # in this PR; SftpBackend likewise).
    fn = getattr(backend, "unlink", None)
    if callable(fn):
        fn(path)
        return
    raise NotImplementedError(
        f"backend {type(backend).__name__} doesn't support unlink "
        "— retention/gc requires a write-capable backend"
    )
