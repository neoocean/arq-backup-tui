#!/usr/bin/env python3
"""Bulk xattr-blob probe to validate the XAttrSetV002 format
hypothesis across many real Arq.app destinations.

PR #25 reverse-engineered the XAttrSetV002 format from a single
68-byte blob (one ``com.apple.provenance`` xattr). This script
walks the operator's destination, collects EVERY xattr blob it
encounters across N nodes, and decodes each one to confirm the
hypothesis holds for all observed shapes — different attribute
namespaces (``com.apple.*``, ``user.*``, ``trusted.*``),
different value types (text, binary, FinderInfo bytes, resource
forks, plists), different counts per blob (single vs multi-attr).

Output:

- per-blob hex (capped at 256 bytes for readability)
- decoded name → value-length per attribute
- format-check: is it XAttrSetV002, binary plist, raw bytes?
- anomaly summary: any blob that doesn't decode cleanly

Operators see "checked N nodes, found M xattr-bearing blobs,
decoded all M cleanly" or a list of anomalies. Pin findings to
``docs/REAL-DATA-DISCOVERIES.md`` if any anomaly surfaces.

Usage::

    python3 scripts/probe_xattr_blob_bulk.py [--max-walk 1000]
                                              [--json]
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _open_backend(creds):
    from arq_validator.sftp import SftpBackend
    backend = SftpBackend(
        creds.host, port=creds.port, user=creds.user,
        password=creds.sftp_password,
        identity_file=creds.identity_file,
        root=creds.root,
    )
    backend.__enter__()
    return backend


def _walk_collect(
    backend, computer_uuid: str, password: str, *,
    max_walk: int,
) -> List[Tuple[str, "object"]]:
    """Walk nodes under ``computer_uuid`` and return
    ``[(node_rel_path, blob_loc_dataclass), ...]`` for every
    xattr-bearing node."""
    from arq_reader.parse import parse_tree
    from arq_reader.decrypt import decrypt_lz4_arqo
    from arq_validator.crypto import decrypt_keyset
    from arq_validator.layout import keyset_path, list_backuprecords
    from arq_validator import discover_layout
    from arq_writer.backuprecord import parse_backuprecord
    from arq_writer.types import FileNode, TreeNode

    keyset = decrypt_keyset(
        backend.read_all(keyset_path("/", computer_uuid)),
        password,
    )
    lay = next(
        lt for lt in discover_layout(
            backend, "/", enumerate_objects=False,
        ) if lt.computer_uuid == computer_uuid
    )
    found: List[Tuple[str, object]] = []
    walked = 0
    for fu in lay.backup_folder_uuids:
        records = list_backuprecords(backend, "/", computer_uuid, fu)
        if not records:
            continue
        # Latest record only — older records typically share xattr
        # patterns and we want fresh format observation.
        rec_blob = backend.read_all(records[-1])
        try:
            rec = parse_backuprecord(decrypt_lz4_arqo(
                rec_blob,
                keyset.encryption_key, keyset.hmac_key,
            ))
        except Exception as exc:
            print(
                f"  bad record {records[-1]}: {exc}",
                file=sys.stderr,
            )
            continue
        node = rec.get("node") or {}
        # Capture root xattrs.
        for xloc in node.get("xattrsBlobLocs") or []:
            found.append(("[root]", xloc))
        tree_loc = node.get("treeBlobLoc") or {}
        if not tree_loc.get("blobIdentifier"):
            continue
        # BFS into the tree.
        stack = [(tree_loc, "")]
        while stack and walked < max_walk:
            loc, parent_rel = stack.pop()
            walked += 1
            try:
                if loc.get("isPacked"):
                    raw = backend.read_range(
                        loc["relativePath"],
                        int(loc["offset"]),
                        int(loc["length"]),
                    )
                else:
                    raw = backend.read_all(loc["relativePath"])
                tree = parse_tree(decrypt_lz4_arqo(
                    raw,
                    keyset.encryption_key, keyset.hmac_key,
                ))
            except Exception:
                continue
            for child in tree.children:
                child_rel = (
                    f"{parent_rel}/{child.name}"
                    if parent_rel else child.name
                )
                child_node = child.node
                xattrs = getattr(
                    child_node, "xattrsBlobLocs", None,
                ) or []
                for xloc in xattrs:
                    found.append((child_rel, xloc))
                if isinstance(child_node, TreeNode):
                    sub = {
                        "blobIdentifier":
                            getattr(
                                child_node.treeBlobLoc,
                                "blobIdentifier", "",
                            ),
                        "relativePath":
                            getattr(
                                child_node.treeBlobLoc,
                                "relativePath", "",
                            ),
                        "offset": int(getattr(
                            child_node.treeBlobLoc, "offset", 0,
                        )),
                        "length": int(getattr(
                            child_node.treeBlobLoc, "length", 0,
                        )),
                        "isPacked": bool(getattr(
                            child_node.treeBlobLoc,
                            "isPacked", False,
                        )),
                    }
                    stack.append((sub, child_rel))
            if walked >= max_walk:
                break
    return found


def _classify_one(blob: bytes) -> Dict[str, Any]:
    """Decode + classify one xattr blob. Returns format kind +
    decoded name/value summary or anomaly notes."""
    from arq_writer.xattrs import deserialize_xattrs, _XATTR_MAGIC
    info: Dict[str, Any] = {
        "length": len(blob),
        "hex_preview": blob[:64].hex(),
    }
    if blob.startswith(_XATTR_MAGIC):
        info["format"] = "XAttrSetV002"
    elif blob.startswith(b"bplist00"):
        info["format"] = "binary-plist"
    elif not blob:
        info["format"] = "empty"
        return info
    else:
        info["format"] = f"unknown ({blob[:8].hex()})"
    try:
        decoded = deserialize_xattrs(blob)
        info["decoded"] = True
        info["xattr_count"] = len(decoded)
        info["names"] = sorted(decoded.keys())
        info["value_lengths"] = {
            name: len(val) for name, val in decoded.items()
        }
    except Exception as exc:
        info["decoded"] = False
        info["decode_error"] = (
            f"{type(exc).__name__}: {exc}"
        )
    return info


def _fetch_blob(backend, blob_loc) -> bytes:
    """Resolve + return the on-disk bytes for a BlobLoc dataclass."""
    rel = getattr(blob_loc, "relativePath", "") or ""
    if not rel:
        return b""
    if getattr(blob_loc, "isPacked", False):
        return backend.read_range(
            rel,
            int(getattr(blob_loc, "offset", 0)),
            int(getattr(blob_loc, "length", 0)),
        )
    return backend.read_all(rel)


def _main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--max-walk", type=int, default=1000)
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    try:
        from tests.integration._creds import (
            resolve_creds, skip_reason,
        )
    except ImportError as exc:
        sys.exit(f"can't import creds helper: {exc}")
    creds = resolve_creds()
    if creds is None:
        sys.exit(
            f"creds unavailable: {skip_reason() or 'no creds'}"
        )

    backend = _open_backend(creds)
    try:
        from arq_validator import discover_layout
        from arq_validator.crypto import decrypt_keyset
        from arq_validator.layout import keyset_path
        from arq_reader.decrypt import decrypt_lz4_arqo
        layouts = list(discover_layout(
            backend, "/", enumerate_objects=False,
        ))
        if not layouts:
            sys.exit("no computer subtrees found")

        all_findings: List[Dict[str, Any]] = []
        format_counter: Counter = Counter()
        anomalies: List[Dict[str, Any]] = []
        name_counter: Counter = Counter()

        for lay in layouts:
            cu = lay.computer_uuid
            keyset = decrypt_keyset(
                backend.read_all(keyset_path("/", cu)),
                creds.dest_password,
            )
            xattr_locs = _walk_collect(
                backend, cu, creds.dest_password,
                max_walk=args.max_walk,
            )
            for node_rel, loc in xattr_locs:
                try:
                    arqo = _fetch_blob(backend, loc)
                    plain = decrypt_lz4_arqo(
                        arqo,
                        keyset.encryption_key,
                        keyset.hmac_key,
                    )
                except Exception as exc:
                    anomalies.append({
                        "node": node_rel,
                        "kind": "fetch_or_decrypt_failed",
                        "error": str(exc),
                    })
                    continue
                info = _classify_one(plain)
                info["node"] = node_rel
                info["computer_uuid"] = cu
                format_counter[info["format"]] += 1
                if not info.get("decoded"):
                    anomalies.append(info)
                else:
                    for name in info.get("names", []):
                        name_counter[name] += 1
                all_findings.append(info)

        report = {
            "total_xattr_blobs_observed": len(all_findings),
            "format_distribution": dict(format_counter),
            "decoded_cleanly": sum(
                1 for f in all_findings if f.get("decoded")
            ),
            "anomalies": anomalies,
            "top_xattr_names": dict(name_counter.most_common(20)),
            "samples_first_5": all_findings[:5],
        }
        if args.json:
            print(json.dumps(report, indent=2,
                             ensure_ascii=False, default=str))
        else:
            print(f"observed:  {report['total_xattr_blobs_observed']} xattr blobs")
            print(f"decoded:   {report['decoded_cleanly']} (clean)")
            print(f"anomalies: {len(anomalies)}")
            print("\nformat distribution:")
            for fmt, n in format_counter.most_common():
                print(f"  {n:5d}  {fmt}")
            if name_counter:
                print("\ntop xattr names (across all blobs):")
                for name, n in name_counter.most_common(15):
                    print(f"  {n:5d}  {name}")
            if anomalies:
                print("\nanomalies (first 5):")
                for a in anomalies[:5]:
                    print(f"  {a}")
        return 1 if anomalies else 0
    finally:
        try:
            backend.__exit__(None, None, None)
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(_main())
