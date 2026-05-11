"""K4-1 — Tree v4 trailing-block depth-grouped statistics.

Strategy K (`docs/COMPAT-VERIFICATION.md` §5.7) and K2/K3
characterized the trailing-block timestamp shape at the **top
level** of one BackupRecord (or 5 records, in A보완-10's multi-
sweep). The top-level result was **38.4% all-zero blocks**, very
different from Strategy K's original whole-destination sweep
which reported **0.014% all-zero**.

K4-1 closes that depth-discrepancy by walking each record's
**full sub-tree depth** and reporting stats grouped by depth
level — confirming (or refuting) the hypothesis that Arq.app
emits all-zero trailing blocks predominantly at the top level
of each record's root tree, and non-zero trailing blocks
everywhere deeper.

Usage::

    python3 scripts/k4_subtree_sweep.py \\
        --destination /Volumes/arqbackup1 \\
        --password-file .secrets/dest_password \\
        --records 5

Outputs a per-depth-level table with:
- node count
- all-zero trailing-block rate (%)
- btime_sec match rate among non-zero (%)

Plus an aggregate roll-up. The script is purely investigative —
it does NOT modify the writer or any destination.
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

# Make the repo importable when invoked directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _read_password(password_file: Path) -> str:
    return password_file.read_text().strip()


def _walk_with_depth(
    backend, cu: str, tree_id: str, tree_loc: dict,
    keyset, *, depth: int = 0,
    max_records_depth: int = 99,
) -> Iterator[Dict[str, Any]]:
    """Recursively yield one record per Node, tagged with the
    Node's depth within the record's root tree.

    Depth 0 = direct children of the record's root tree.
    Depth 1 = grandchildren. Etc.
    """
    from arq_reader.decrypt import decrypt_lz4_arqo
    from arq_reader.parse import parse_tree

    if depth > max_records_depth:
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

    subtrees: List[Tuple[str, dict]] = []
    for child in tree.children:
        node = child.node
        tb = getattr(node, "v4_trailing_block", b"") or b""
        if len(tb) < 38:
            continue
        is_zero = tb == b"\x00" * 38
        sec, nsec = struct.unpack(">qq", tb[0:16])
        yield {
            "depth": depth,
            "name": child.name,
            "is_zero": is_zero,
            "trailing_sec": sec,
            "trailing_nsec": nsec,
            "btime_sec": int(getattr(node, "create_time_sec", 0) or 0),
            "btime_nsec": int(getattr(node, "create_time_nsec", 0) or 0),
            "mtime_sec": int(getattr(node, "mtime_sec", 0) or 0),
            "ctime_sec": int(getattr(node, "ctime_sec", 0) or 0),
        }
        if getattr(node, "treeBlobLoc", None):
            tloc = node.treeBlobLoc
            sub_id = getattr(tloc, "blobIdentifier", "")
            if sub_id:
                subtrees.append((sub_id, {
                    "blobIdentifier": tloc.blobIdentifier,
                    "relativePath": tloc.relativePath,
                    "offset": tloc.offset,
                    "length": tloc.length,
                }))

    for sub_id, sub_loc in subtrees:
        yield from _walk_with_depth(
            backend, cu, sub_id, sub_loc, keyset,
            depth=depth + 1,
            max_records_depth=max_records_depth,
        )


def _walk_records_with_depth(
    backend, cu: str, password: str, *,
    folder_filter: Optional[str] = None,
    record_limit: int = 5,
    max_records_depth: int = 99,
) -> Iterator[Dict[str, Any]]:
    """Yield {'record_path', 'nodes_by_depth': {depth: [nodes]}}
    for each v4 BackupRecord."""
    from arq_reader.decrypt import decrypt_lz4_arqo
    from arq_validator.constants import (
        BACKUPFOLDERS_DIR, BACKUPRECORDS_DIR,
    )
    from arq_validator.crypto import decrypt_keyset

    keyset_bytes = backend.read_all(f"/{cu}/encryptedkeyset.dat")
    keyset = decrypt_keyset(keyset_bytes, password)

    bf_root = f"/{cu}/{BACKUPFOLDERS_DIR}"
    folders = [
        f for f in backend.list_dir(bf_root)
        if backend.is_dir(f"{bf_root}/{f}")
    ]
    if folder_filter is not None:
        folders = [f for f in folders if f == folder_filter]

    walked = 0
    for fu in folders:
        rec_root = f"{bf_root}/{fu}/{BACKUPRECORDS_DIR}"
        if not backend.is_dir(rec_root):
            continue
        # Walk newest record first per bucket.
        buckets = sorted(backend.list_dir(rec_root), reverse=True)
        for bucket in buckets:
            rec_names = sorted(
                backend.list_dir(f"{rec_root}/{bucket}"),
                reverse=True,
            )
            for rec_name in rec_names:
                if not rec_name.endswith(".backuprecord"):
                    continue
                if walked >= record_limit:
                    return
                rec_path = f"{rec_root}/{bucket}/{rec_name}"
                try:
                    arqo = backend.read_all(rec_path)
                    plain = decrypt_lz4_arqo(
                        arqo, keyset.encryption_key, keyset.hmac_key,
                    )
                    record = json.loads(plain.decode("utf-8"))
                except Exception:
                    continue
                if record.get("nodeTreeVersion") != 4:
                    continue
                node = record.get("node") or {}
                tree_loc = node.get("treeBlobLoc") or {}
                tree_id = tree_loc.get("blobIdentifier") or ""
                if not tree_id:
                    continue
                nodes_by_depth: Dict[int, List[Dict[str, Any]]] = (
                    defaultdict(list)
                )
                for n in _walk_with_depth(
                    backend, cu, tree_id, tree_loc, keyset,
                    max_records_depth=max_records_depth,
                ):
                    nodes_by_depth[n["depth"]].append(n)
                walked += 1
                yield {
                    "record_path": rec_path,
                    "creation_date": record.get("creationDate"),
                    "nodes_by_depth": dict(nodes_by_depth),
                }


def _print_depth_table(
    aggregated: Dict[int, List[Dict[str, Any]]],
) -> None:
    print()
    print(
        f"{'Depth':>5}  "
        f"{'Nodes':>8}  "
        f"{'Zero':>7}  "
        f"{'Zero%':>7}  "
        f"{'Btime_match':>13}  "
        f"{'Btime%':>8}"
    )
    print("-" * 60)
    totals = [0, 0, 0]   # nodes, zero, btime_match
    for depth in sorted(aggregated):
        nodes = aggregated[depth]
        n = len(nodes)
        zero = sum(1 for x in nodes if x["is_zero"])
        non_zero = [x for x in nodes if not x["is_zero"]]
        btime_match = sum(
            1 for x in non_zero
            if x["trailing_sec"] == x["btime_sec"]
        )
        zero_pct = (zero / n * 100) if n else 0
        btime_pct = (
            btime_match / len(non_zero) * 100
            if non_zero else 0
        )
        print(
            f"{depth:>5}  "
            f"{n:>8}  "
            f"{zero:>7}  "
            f"{zero_pct:>6.1f}%  "
            f"{btime_match:>13}  "
            f"{btime_pct:>7.1f}%"
        )
        totals[0] += n
        totals[1] += zero
        totals[2] += btime_match
    print("-" * 60)
    overall_zero_pct = (
        totals[1] / totals[0] * 100 if totals[0] else 0
    )
    print(
        f"{'Total':>5}  {totals[0]:>8}  {totals[1]:>7}  "
        f"{overall_zero_pct:>6.1f}%  {totals[2]:>13}"
    )


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description=(
            "K4-1: Tree v4 trailing-block depth-grouped sweep."
        ),
    )
    p.add_argument("--destination", required=True, type=Path)
    p.add_argument("--password-file", required=True, type=Path)
    p.add_argument("--computer", type=str, default=None)
    p.add_argument("--folder", type=str, default=None)
    p.add_argument(
        "--records", type=int, default=5,
        help="Max v4 records to sample (default: 5).",
    )
    p.add_argument(
        "--max-depth", type=int, default=99,
        help="Max sub-tree depth to descend (default: 99).",
    )
    p.add_argument(
        "--json", action="store_true",
        help="Emit JSON instead of the text table.",
    )
    args = p.parse_args(argv)

    from arq_validator.backend import LocalBackend
    from arq_validator.layout import discover_layout

    backend = LocalBackend(args.destination.resolve())
    layouts = discover_layout(backend, "/")
    if not layouts:
        print("no Arq destination layouts found", file=sys.stderr)
        return 1
    if args.computer:
        layouts = [
            lay for lay in layouts if lay.computer_uuid == args.computer
        ]
        if not layouts:
            print(
                f"computer {args.computer} not found",
                file=sys.stderr,
            )
            return 1
    cu = layouts[0].computer_uuid

    password = _read_password(args.password_file)

    aggregated: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    per_record: List[Dict[str, Any]] = []

    for r in _walk_records_with_depth(
        backend, cu, password,
        folder_filter=args.folder,
        record_limit=args.records,
        max_records_depth=args.max_depth,
    ):
        # Progress indicator so the operator sees one line per
        # record finishing — the sweep is I/O-bound on external
        # volumes (each tree blob = one openssl subprocess), so
        # an early "done with record 1" tells you the script
        # isn't hung.
        total_nodes = sum(
            len(v) for v in r["nodes_by_depth"].values()
        )
        print(
            f"# processed {Path(r['record_path']).name} "
            f"({total_nodes} nodes across "
            f"{len(r['nodes_by_depth'])} depths)",
            file=sys.stderr, flush=True,
        )
        per_record_aggregate: Dict[int, int] = defaultdict(int)
        per_record_zeros: Dict[int, int] = defaultdict(int)
        for depth, nodes in r["nodes_by_depth"].items():
            aggregated[depth].extend(nodes)
            per_record_aggregate[depth] += len(nodes)
            per_record_zeros[depth] += sum(
                1 for n in nodes if n["is_zero"]
            )
        per_record.append({
            "record_path": r["record_path"],
            "creation_date": r["creation_date"],
            "depth_counts": dict(per_record_aggregate),
            "depth_zero_counts": dict(per_record_zeros),
        })

    if args.json:
        # Convert defaultdict to plain dict for JSON.
        out = {
            "per_record": per_record,
            "depth_table": {
                str(d): {
                    "node_count": len(aggregated[d]),
                    "zero_count": sum(
                        1 for x in aggregated[d] if x["is_zero"]
                    ),
                }
                for d in sorted(aggregated)
            },
        }
        print(json.dumps(out, indent=2))
    else:
        print(
            f"K4-1 sub-tree sweep: "
            f"sampled {len(per_record)} v4 record(s)"
        )
        _print_depth_table(aggregated)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
