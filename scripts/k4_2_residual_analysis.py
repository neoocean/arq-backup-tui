"""K4-2 — Tree v4 trailing-block residual (non-btime) correlation
analysis.

K3 + A보완-10 established that the trailing-block sec/nsec
(bytes 0..15) matches ``btime_sec`` for ~47% of non-zero nodes;
the residual ~53% are unexplained. K4-2 investigates whether
the residual nodes show a correlation with:

- ``mtime_sec`` (file content-modification time)
- ``ctime_sec`` (file metadata-change time)
- ``record.creationDate`` (this record's wall-clock emit time)
- ``creationDate - trailing_sec`` (constant offset?)

against the operator's real Arq.app v8 destination. Findings
inform whether a future writer-side first-emit-time tracking
sidecar (K3 Option B) could close more of the gap.

Usage::

    python3 scripts/k4_2_residual_analysis.py \\
        --destination /Volumes/arqbackup1 \\
        --password-file .secrets/dest_password \\
        --records 3
"""

from __future__ import annotations

import argparse
import json
import statistics
import struct
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _read_password(p: Path) -> str:
    return p.read_text().strip()


def _walk_with_metadata(
    backend, cu: str, tree_id: str, tree_loc: dict, keyset,
    *, depth: int = 0, max_depth: int = 99,
) -> Iterator[Dict[str, Any]]:
    from arq_reader.decrypt import decrypt_lz4_arqo
    from arq_reader.parse import parse_tree

    if depth > max_depth:
        return
    rel = tree_loc.get("relativePath", "")
    offset = int(tree_loc.get("offset", 0))
    length = int(tree_loc.get("length", 0))
    blob_path = (
        f"/{cu}/standardobjects/{tree_id[:2]}/{tree_id[2:]}"
        if not rel else rel
    )
    try:
        if rel:
            raw = backend.read_range(blob_path, offset, length)
        else:
            raw = backend.read_all(blob_path)
        plain = decrypt_lz4_arqo(
            raw, keyset.encryption_key, keyset.hmac_key,
        )
        tree = parse_tree(plain)
    except Exception:
        return

    subtrees = []
    for child in tree.children:
        n = child.node
        tb = getattr(n, "v4_trailing_block", b"") or b""
        if len(tb) < 38 or tb == b"\x00" * 38:
            continue
        sec, nsec = struct.unpack(">qq", tb[0:16])
        yield {
            "depth": depth,
            "name": child.name,
            "trailing_sec": sec,
            "trailing_nsec": nsec,
            "btime_sec": int(getattr(n, "create_time_sec", 0) or 0),
            "btime_nsec": int(getattr(n, "create_time_nsec", 0) or 0),
            "mtime_sec": int(getattr(n, "mtime_sec", 0) or 0),
            "mtime_nsec": int(getattr(n, "mtime_nsec", 0) or 0),
            "ctime_sec": int(getattr(n, "ctime_sec", 0) or 0),
            "ctime_nsec": int(getattr(n, "ctime_nsec", 0) or 0),
        }
        if getattr(n, "treeBlobLoc", None):
            tloc = n.treeBlobLoc
            sub_id = getattr(tloc, "blobIdentifier", "")
            if sub_id:
                subtrees.append((sub_id, {
                    "blobIdentifier": tloc.blobIdentifier,
                    "relativePath": tloc.relativePath,
                    "offset": tloc.offset,
                    "length": tloc.length,
                }))
    for sub_id, sub_loc in subtrees:
        yield from _walk_with_metadata(
            backend, cu, sub_id, sub_loc, keyset,
            depth=depth + 1, max_depth=max_depth,
        )


def _analyze_residual(
    nodes: List[Dict[str, Any]],
    record_creation_date: int,
) -> Dict[str, Any]:
    """Return correlation stats for non-btime nodes."""
    residual = [
        n for n in nodes
        if n["trailing_sec"] != n["btime_sec"]
    ]
    btime_match = [
        n for n in nodes
        if n["trailing_sec"] == n["btime_sec"]
    ]
    if not residual:
        return {
            "total_nodes": len(nodes),
            "btime_match_count": len(btime_match),
            "residual_count": 0,
        }
    mtime_match = sum(
        1 for n in residual
        if n["trailing_sec"] == n["mtime_sec"]
    )
    ctime_match = sum(
        1 for n in residual
        if n["trailing_sec"] == n["ctime_sec"]
    )
    # Offset against record.creationDate.
    offsets = [
        record_creation_date - n["trailing_sec"]
        for n in residual
    ]
    offset_mean = statistics.mean(offsets) if offsets else 0
    offset_stdev = (
        statistics.stdev(offsets) if len(offsets) > 1 else 0
    )
    # How many are within ±1 hour of creationDate?
    near_creation = sum(
        1 for o in offsets if abs(o) <= 3600
    )
    return {
        "total_nodes": len(nodes),
        "btime_match_count": len(btime_match),
        "btime_match_pct": (
            len(btime_match) / len(nodes) * 100
            if nodes else 0
        ),
        "residual_count": len(residual),
        "residual_mtime_match": mtime_match,
        "residual_mtime_pct": (
            mtime_match / len(residual) * 100
            if residual else 0
        ),
        "residual_ctime_match": ctime_match,
        "residual_ctime_pct": (
            ctime_match / len(residual) * 100
            if residual else 0
        ),
        "residual_offset_from_creationdate_mean": offset_mean,
        "residual_offset_from_creationdate_stdev": offset_stdev,
        "residual_within_one_hour_of_record": near_creation,
        "residual_within_one_hour_pct": (
            near_creation / len(residual) * 100
            if residual else 0
        ),
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--destination", required=True, type=Path)
    p.add_argument("--password-file", required=True, type=Path)
    p.add_argument("--records", type=int, default=3)
    p.add_argument("--max-depth", type=int, default=3)
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    from arq_validator.backend import LocalBackend
    from arq_validator.layout import discover_layout
    from arq_validator.crypto import decrypt_keyset
    from arq_validator.constants import (
        BACKUPFOLDERS_DIR, BACKUPRECORDS_DIR,
    )
    from arq_reader.decrypt import decrypt_lz4_arqo

    backend = LocalBackend(args.destination.resolve())
    layouts = discover_layout(backend, "/")
    if not layouts:
        print("no layouts found", file=sys.stderr)
        return 1
    cu = layouts[0].computer_uuid
    password = _read_password(args.password_file)
    ks = decrypt_keyset(
        backend.read_all(f"/{cu}/encryptedkeyset.dat"),
        password,
    )

    walked = 0
    per_record = []
    bf_root = f"/{cu}/{BACKUPFOLDERS_DIR}"
    folders = sorted(backend.list_dir(bf_root))
    for fu in folders:
        if walked >= args.records:
            break
        rec_root = f"{bf_root}/{fu}/{BACKUPRECORDS_DIR}"
        if not backend.is_dir(rec_root):
            continue
        for bucket in sorted(
            backend.list_dir(rec_root), reverse=True,
        ):
            for rname in sorted(
                backend.list_dir(f"{rec_root}/{bucket}"),
                reverse=True,
            ):
                if not rname.endswith(".backuprecord"):
                    continue
                if walked >= args.records:
                    break
                rp = f"{rec_root}/{bucket}/{rname}"
                try:
                    arqo = backend.read_all(rp)
                    plain = decrypt_lz4_arqo(
                        arqo, ks.encryption_key, ks.hmac_key,
                    )
                    rec = json.loads(plain.decode("utf-8"))
                except Exception:
                    continue
                if rec.get("nodeTreeVersion") != 4:
                    continue
                node = rec.get("node", {})
                tloc = node.get("treeBlobLoc", {})
                tree_id = tloc.get("blobIdentifier", "")
                if not tree_id:
                    continue
                print(
                    f"# walking {rname} "
                    f"(creationDate={rec.get('creationDate')})",
                    file=sys.stderr, flush=True,
                )
                nodes = list(_walk_with_metadata(
                    backend, cu, tree_id, tloc, ks,
                    max_depth=args.max_depth,
                ))
                stats = _analyze_residual(
                    nodes, rec.get("creationDate") or 0,
                )
                stats["record"] = rname
                per_record.append(stats)
                walked += 1

    if args.json:
        print(json.dumps(per_record, indent=2))
    else:
        for s in per_record:
            print(f"\nRecord {s['record']}:")
            print(f"  total non-zero nodes: {s['total_nodes']}")
            print(
                f"  btime_sec match:      {s['btime_match_count']}"
                f" ({s.get('btime_match_pct',0):.1f}%)"
            )
            if s["residual_count"]:
                print(
                    f"  residual (non-btime): {s['residual_count']}"
                )
                print(
                    f"    mtime_sec match:    {s['residual_mtime_match']}"
                    f" ({s['residual_mtime_pct']:.1f}%)"
                )
                print(
                    f"    ctime_sec match:    {s['residual_ctime_match']}"
                    f" ({s['residual_ctime_pct']:.1f}%)"
                )
                print(
                    f"    offset from creationDate: "
                    f"mean={s['residual_offset_from_creationdate_mean']:.0f}s "
                    f"stdev={s['residual_offset_from_creationdate_stdev']:.0f}s"
                )
                print(
                    f"    within 1h of record's creationDate: "
                    f"{s['residual_within_one_hour_of_record']}"
                    f" ({s['residual_within_one_hour_pct']:.1f}%)"
                )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
