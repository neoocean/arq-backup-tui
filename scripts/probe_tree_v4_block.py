#!/usr/bin/env python3
"""Walk a real Arq destination and dump the 38-byte trailing
block per Node so we can RE its field decomposition.

Background
==========

Arq Tree v4 (Arq.app v8+) adds a 38-byte block at the end of
each Node, after the existing macOS / Windows-specific fields.
Our reader currently consumes it as opaque bytes (see
:func:`arq_reader.parse.parse_node`'s ``read_raw(38)``). On the
operator's Hetzner destination every byte of this block has been
zero, but we want a wider sample — any single non-zero byte
gives us a field offset to investigate.

This script:

1. Connects to the operator's destination via the existing
   ``tests/integration/_creds.py`` resolver (no creds in source
   control).
2. Walks each computer subtree, finds every Tree v4 blob.
3. For each Node, instead of skipping the 38 bytes, captures the
   raw bytes + position, prints a histogram, and groups identical
   blocks.
4. Cross-reference any non-zero blocks back to the Node metadata
   (path, size, mtime, type) so we can guess what's encoded.

Usage
=====

  python3 scripts/probe_tree_v4_block.py [--root /<computer-uuid>]
                                         [--limit 1000]
                                         [--full] [--json]

The ``--root`` option scopes the walk to a single computer-uuid
subtree if you want faster iteration. ``--limit`` caps the number
of Nodes inspected. ``--full`` prints every distinct block; the
default summary keeps the output one screen tall.

Exits 0 if every block was all-zero; non-zero exit means the
script found at least one non-zero block — an interesting sample
worth committing to ``docs/REAL-DATA-DISCOVERIES.md``.
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

# Make the repo importable when invoked directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _load_backend():
    """Resolve the operator's destination via the integration
    test creds helper. Aborts with a clear message if creds are
    missing rather than printing stack traces."""
    try:
        from tests.integration._creds import resolve_creds, skip_reason
    except ImportError as exc:
        sys.exit(f"can't import creds helper: {exc}")
    creds = resolve_creds()
    if creds is None:
        reason = skip_reason() or "no creds resolved"
        sys.exit(f"creds unavailable: {reason}")
    from arq_validator.sftp import SftpBackend
    backend = SftpBackend(
        creds.host, port=creds.port, user=creds.user,
        password=creds.sftp_password,
        identity_file=creds.identity_file,
        root=creds.root,
    )
    backend.__enter__()
    return backend, creds


def _list_recursive(backend, path: str) -> list:
    """Walk an SFTP directory subtree and return every leaf path.
    The backend exposes list_dir + is_dir; pack a tiny BFS instead
    of relying on a dedicated recurse helper."""
    results: list = []
    queue: list = [path]
    while queue:
        cur = queue.pop()
        try:
            entries = backend.list_dir(cur)
        except OSError:
            continue
        for ent in entries:
            full = f"{cur.rstrip('/')}/{ent}"
            try:
                if backend.is_dir(full):
                    queue.append(full)
                    continue
            except OSError:
                pass
            results.append(full)
    return results


def _decrypt_blob(backend, *, blob_id: str, computer_uuid: str,
                  password: str, blob_loc=None):
    """Resolve a tree blob into its plaintext bytes.

    Tree blobs in Arq 7 land in ``treepacks/`` (when packed) or
    ``standardobjects/`` (when stored raw); we use the BlobLoc
    when present so we don't reimplement layout discovery here.
    """
    from arq_reader.decrypt import decrypt_lz4_arqo
    from arq_validator.crypto import decrypt_keyset
    keyset_path = f"/{computer_uuid}/encryptedkeyset.dat"
    keyset_blob = backend.read_all(keyset_path)
    keyset = decrypt_keyset(keyset_blob, password)

    # Caller may pass either the JSON dict shape (from a backuprecord
    # `node["treeBlobLoc"]`) or the dataclass shape (from
    # `arq_reader.parse.parse_blobloc`). Both use camelCase field
    # names — the dataclass attrs are camelCase too — so the lookup
    # is unified.
    def _g(field, default=None):
        if blob_loc is None:
            return default
        if isinstance(blob_loc, dict):
            return blob_loc.get(field, default)
        return getattr(blob_loc, field, default)

    rel = _g("relativePath", "")
    if rel:
        # Pack/object lookup honours offset/length when isPacked.
        if _g("isPacked", False):
            data = backend.read_range(
                rel,
                int(_g("offset", 0)),
                int(_g("length", 0)),
            )
        else:
            data = backend.read_all(rel)
    else:
        # Standalone-object fallback path. Arq 7 puts unpacked
        # blobs under ``standardobjects/``, sharded by the first
        # two hex chars of blob_id.
        prefix = blob_id[:2]
        rest = blob_id[2:]
        path = (
            f"/{computer_uuid}/standardobjects/{prefix}/{rest}"
        )
        data = backend.read_all(path)
    return decrypt_lz4_arqo(data, keyset.encryption_key, keyset.hmac_key)


def _walk_tree_v4(backend, computer_uuid: str, password: str,
                  *, limit: int):
    """Yield ``(node_dict, raw_38_bytes)`` for every node in every
    Tree v4 reachable from the computer's backup records, up to
    ``limit`` nodes total."""
    from arq_reader.parse import (
        BinaryReader, parse_blobloc,
        NODE_REPARSE_FIELDS_MIN_TREE_VERSION,
    )
    from arq_validator.layout import discover_layout
    layouts = list(discover_layout(
        backend, "/", enumerate_objects=False,
    ))
    target_layout = next(
        (lt for lt in layouts if lt.computer_uuid == computer_uuid),
        None,
    )
    if target_layout is None:
        sys.exit(f"no layout for computer {computer_uuid!r}")

    seen_trees: set = set()
    yielded = 0

    def _iter_tree(blob_id: str, blob_loc=None):
        """Recursive generator yielding (node_meta, tail38) for every
        Tree v4 node reachable from ``blob_id``. ``blob_loc`` is a
        BlobLoc dict (the JSON form Arq.app uses) when known, else
        we fall back to the standalone-object path."""
        nonlocal yielded
        if yielded >= limit:
            return
        if blob_id in seen_trees:
            return
        seen_trees.add(blob_id)
        try:
            plain = _decrypt_blob(
                backend, blob_id=blob_id,
                computer_uuid=computer_uuid,
                password=password, blob_loc=blob_loc,
            )
        except Exception as exc:
            print(f"  decrypt failed for {blob_id}: {exc}",
                  file=sys.stderr)
            return
        # Arq 7 trees have no magic prefix — see
        # arq_reader.parse.parse_tree: version (uint32 BE) followed
        # by node-count (uint64 BE), then per-node entries.
        r = BinaryReader(plain)
        version = r.read_uint32()
        if version < 4:
            return
        node_count = r.read_uint64()
        children = []
        for _ in range(node_count):
            name = r.read_string()
            # Inlined parse_node up to the 38-byte block so we
            # can capture the bytes verbatim instead of skipping.
            is_tree = r.read_bool()
            child_tree_loc = None
            if is_tree:
                child_tree_loc = parse_blobloc(r)
            r.read_uint32()  # computer_os
            data_count = r.read_uint64()
            data_locs = [parse_blobloc(r) for _ in range(data_count)]
            acl_present = r.read_bool()
            if acl_present:
                parse_blobloc(r)
            xattr_count = r.read_uint64()
            for _ in range(xattr_count):
                parse_blobloc(r)
            r.read_uint64()  # item_size
            r.read_uint64()  # contained
            mtime_sec = r.read_int64(); r.read_int64()
            ctime_sec = r.read_int64(); r.read_int64()
            cret_sec = r.read_int64(); r.read_int64()
            r.read_string(); r.read_string()  # uname / gname
            r.read_bool()                     # deleted
            r.read_int32(); r.read_uint64()
            mode = r.read_uint32()
            r.read_uint32(); r.read_uint32(); r.read_uint32()
            r.read_int32(); r.read_int32()    # rdev / flags
            r.read_uint32()                   # win_attrs
            if version >= NODE_REPARSE_FIELDS_MIN_TREE_VERSION:
                r.read_uint32(); r.read_bool()
            tail = r.read_raw(38)
            yield ({
                "name": name,
                "is_tree": is_tree,
                "mode_oct": oct(mode),
                "mtime_sec": mtime_sec,
                "ctime_sec": ctime_sec,
                "create_sec": cret_sec,
            }, tail)
            yielded += 1
            if yielded >= limit:
                return
            if is_tree and child_tree_loc is not None:
                children.append(child_tree_loc)
        # Recurse depth-first into subtrees. parse_blobloc returns
        # a BlobLoc dataclass whose blob field is ``blobIdentifier``.
        for child_loc in children:
            if yielded >= limit:
                return
            cid = getattr(child_loc, "blobIdentifier", "") or ""
            if not cid:
                continue
            yield from _iter_tree(cid, child_loc) or ()

    # Walk every backuprecord → root tree.
    from arq_writer.backuprecord import parse_backuprecord
    from arq_validator.crypto import decrypt_keyset
    from arq_reader.decrypt import decrypt_lz4_arqo

    # Decrypt the keyset once per computer — it doesn't change
    # between records and PBKDF2 + AES is comparatively expensive.
    keyset_blob = backend.read_all(
        f"/{computer_uuid}/encryptedkeyset.dat"
    )
    keyset = decrypt_keyset(keyset_blob, password)

    for folder_uuid in target_layout.backup_folder_uuids:
        if yielded >= limit:
            break
        recs_dir = (
            f"/{computer_uuid}/backupfolders/{folder_uuid}/"
            f"backuprecords"
        )
        entries = _list_recursive(backend, recs_dir)
        for path in sorted(entries):
            if not path.endswith(".backuprecord"):
                continue
            if yielded >= limit:
                break
            try:
                blob = backend.read_all(path)
                rec = parse_backuprecord(
                    decrypt_lz4_arqo(
                        blob,
                        keyset.encryption_key,
                        keyset.hmac_key,
                    )
                )
            except Exception as exc:
                print(f"  bad record {path}: {exc}", file=sys.stderr)
                continue
            # Backuprecord top-level: rec["node"] is the tree-typed
            # root Node dict, with rec["node"]["treeBlobLoc"] as the
            # BlobLoc pointing at the root tree blob.
            node = rec.get("node") or {}
            tree_loc = node.get("treeBlobLoc") or {}
            tree_id = tree_loc.get("blobIdentifier") or ""
            if not tree_id:
                continue
            yield from _iter_tree(tree_id, tree_loc) or ()
            if yielded >= limit:
                break


def _main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=1000,
                   help="max nodes to inspect")
    p.add_argument("--full", action="store_true",
                   help="print every distinct block, not just summary")
    p.add_argument("--json", action="store_true",
                   help="emit machine-readable JSON instead of text")
    p.add_argument("--computer", default=None,
                   help="restrict to a specific computer-uuid")
    args = p.parse_args(argv)

    backend, creds = _load_backend()
    try:
        from arq_validator.layout import discover_layout
        if args.computer:
            cuuids = [args.computer]
        else:
            cuuids = [
                lt.computer_uuid for lt in discover_layout(
                    backend, "/", enumerate_objects=False,
                )
            ]
        # Group identical 38-byte blocks; record one representative
        # node-dict per group so we can spot what differs.
        bucket: dict = collections.defaultdict(list)
        for cu in cuuids:
            try:
                for nodemeta, tail in _walk_tree_v4(
                    backend, cu, creds.dest_password,
                    limit=args.limit,
                ):
                    bucket[tail].append(nodemeta)
            except Exception as exc:
                # SFTP rate-limit drops are common against Hetzner;
                # print what we collected so far rather than losing
                # the whole pass.
                print(
                    f"walk for {cu} interrupted by {type(exc).__name__}: "
                    f"{exc}",
                    file=sys.stderr,
                )

        nonzero = [k for k in bucket.keys() if any(b for b in k)]
        out = {
            "computers": cuuids,
            "node_count": sum(len(v) for v in bucket.values()),
            "distinct_blocks": len(bucket),
            "all_zero": len(nonzero) == 0,
            "samples": [],
        }
        rows = sorted(
            bucket.items(), key=lambda it: (-len(it[1]), it[0]),
        )
        for tail, metas in rows:
            sample = {
                "block_hex": tail.hex(),
                "occurrences": len(metas),
                "example_node": metas[0],
            }
            out["samples"].append(sample)
            if not args.full and len(out["samples"]) >= 10:
                break

        if args.json:
            print(json.dumps(out, indent=2, ensure_ascii=False))
        else:
            print(f"computers scanned: {len(cuuids)}")
            print(f"nodes inspected:   {out['node_count']}")
            print(f"distinct blocks:   {out['distinct_blocks']}")
            print(f"all zero?          {out['all_zero']}")
            print()
            for s in out["samples"]:
                print(f"  ×{s['occurrences']:5d}  "
                      f"{s['block_hex']}")
                print(f"          → {s['example_node']}")
        return 1 if not out["all_zero"] else 0
    finally:
        try:
            backend.__exit__(None, None, None)
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(_main())
