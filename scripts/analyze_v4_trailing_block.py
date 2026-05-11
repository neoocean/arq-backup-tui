"""K2 — Tree v4 trailing-block bytes 0..15 deep analysis.

Strategy K (``docs/COMPAT-VERIFICATION.md`` §5.7) established that
trailing-block bytes 0..7 encode a sec-since-Unix-epoch int64 BE,
bytes 8..15 encode a nsec int64 BE, and the pair is a
**backup-engine wall-clock scan timestamp** — not a file-metadata
field. Pre-K hypotheses (per-Node monotonic counter, btime/ctime
correlation) were disproved in §5.7.4.

This script extends Strategy K with three additional analyses
operators can run against their own destination to inspect:

1. **Cluster pattern**: how tightly do trailing_sec values cluster
   inside a single BackupRecord? Spread = total walk-time the
   engine took to traverse that record. Within-cluster gap
   distribution.

2. **Cross-record drift**: between two records of the same
   folder, how does the trailing-block timestamp shift? Should
   roughly equal the wall-clock gap between the two record's
   ``creationDate`` values.

3. **Monotonicity within a record**: are trailing_sec values
   sorted ascending as the walker traverses? Confirms the
   wall-clock-during-walk semantics.

The script does NOT change the writer's behaviour. Strategy K
(§5.7.5) settled the writer-side decision: explicit override or
deterministic ``create_time`` fallback. K2 is investigative —
operators with an Arq.app destination can run this to gather
their own evidence for the "wall-clock scan timestamp"
interpretation OR find new structure if it exists.

Usage
-----

::

    python3 scripts/analyze_v4_trailing_block.py \\
        --destination /Volumes/arqbackup1 \\
        --password-file .secrets/dest_password \\
        --limit 1000 \\
        [--computer <UUID>]
        [--folder <UUID>]

Outputs a text summary to stdout. Pass ``--json`` for machine-
readable output suitable for further analysis.
"""

from __future__ import annotations

import argparse
import json
import statistics
import struct
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

# Make the repo importable when invoked directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _read_password(password_file: Path) -> str:
    return password_file.read_text().strip()


def _walk_v4_records(
    backend, cu: str, password: str, *,
    folder_filter: Optional[str] = None,
    limit: int = 1000,
) -> Iterator[Dict[str, Any]]:
    """Yield {'record_path', 'creation_date', 'nodes': [...]} for
    each v4 BackupRecord reachable under ``cu``.

    Each ``nodes`` entry is a dict with ``trailing_sec``,
    ``trailing_nsec``, ``ctime_sec``, ``ctime_nsec``, ``mtime_sec``,
    ``mtime_nsec``, ``btime_sec``, ``btime_nsec``, plus the
    walk-order index of the node within its record.
    """
    from arq_reader.decrypt import decrypt_lz4_arqo
    from arq_reader.parse import parse_tree
    from arq_validator.constants import (
        BACKUPFOLDERS_DIR, BACKUPRECORDS_DIR,
    )
    from arq_validator.crypto import decrypt_keyset

    keyset_bytes = backend.read_all(f"/{cu}/encryptedkeyset.dat")
    keyset = decrypt_keyset(keyset_bytes, password)

    walked = 0
    bf_root = f"/{cu}/{BACKUPFOLDERS_DIR}"
    folders = [
        f for f in backend.list_dir(bf_root)
        if backend.is_dir(f"{bf_root}/{f}")
    ]
    if folder_filter is not None:
        folders = [f for f in folders if f == folder_filter]

    for fu in folders:
        rec_root = f"{bf_root}/{fu}/{BACKUPRECORDS_DIR}"
        if not backend.is_dir(rec_root):
            continue
        for bucket in sorted(backend.list_dir(rec_root)):
            for rec_name in sorted(
                backend.list_dir(f"{rec_root}/{bucket}"),
            ):
                if not rec_name.endswith(".backuprecord"):
                    continue
                rec_path = f"{rec_root}/{bucket}/{rec_name}"
                try:
                    arqo = backend.read_all(rec_path)
                    plain = decrypt_lz4_arqo(
                        arqo, keyset.encryption_key, keyset.hmac_key,
                    )
                    record = json.loads(plain.decode("utf-8"))
                except Exception:
                    # Skip plist-formatted or undecodable records;
                    # K2's analysis assumes the v4/JSON shape Arq.app
                    # v8 emits.
                    continue
                if record.get("nodeTreeVersion") != 4:
                    continue
                node = record.get("node") or {}
                tree_loc = node.get("treeBlobLoc") or {}
                tree_id = tree_loc.get("blobIdentifier") or ""
                if not tree_id:
                    continue
                nodes = list(_walk_v4_tree_collect(
                    backend, cu, tree_id, tree_loc,
                    keyset, limit_remaining=limit - walked,
                ))
                walked += len(nodes)
                yield {
                    "record_path": rec_path,
                    "folder_uuid": fu,
                    "creation_date": record.get("creationDate"),
                    "nodes": nodes,
                }
                if walked >= limit:
                    return


def _walk_v4_tree_collect(
    backend, cu: str, tree_id: str, tree_loc: dict,
    keyset, *, limit_remaining: int,
) -> Iterator[Dict[str, Any]]:
    """Recurse into a v4 tree, yielding one summary dict per child
    Node with both the trailing-block fields and the file-metadata
    timestamps we want to correlate against.
    """
    from arq_reader.decrypt import decrypt_lz4_arqo
    from arq_reader.parse import parse_tree
    from arq_writer.serialize import _v4_trailing_block  # noqa

    if limit_remaining <= 0:
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
    except Exception:
        return
    try:
        plain = decrypt_lz4_arqo(
            raw, keyset.encryption_key, keyset.hmac_key,
        )
        tree = parse_tree(plain)
    except Exception:
        return

    walk_idx = 0
    subtrees: List[Tuple[str, dict]] = []
    for child in tree.children:
        walk_idx += 1
        node = child.node
        # 38-byte trailing block; bytes 0..15 are sec+nsec int64 BE.
        tb = getattr(node, "v4_trailing_block", b"") or b""
        if len(tb) < 16:
            continue
        sec = struct.unpack(">q", tb[0:8])[0]
        nsec = struct.unpack(">q", tb[8:16])[0]
        yield {
            "trailing_sec": sec,
            "trailing_nsec": nsec,
            "ctime_sec": int(getattr(node, "ctime_sec", 0) or 0),
            "ctime_nsec": int(getattr(node, "ctime_nsec", 0) or 0),
            "mtime_sec": int(getattr(node, "mtime_sec", 0) or 0),
            "mtime_nsec": int(getattr(node, "mtime_nsec", 0) or 0),
            "btime_sec": int(getattr(node, "create_time_sec", 0) or 0),
            "btime_nsec": int(getattr(node, "create_time_nsec", 0) or 0),
            "name": child.name,
            "walk_idx": walk_idx,
        }
        # Descend into sub-trees later; collecting them first keeps
        # the walk-order index meaningful for the cluster analysis
        # (depth-first inside one tree).
        if getattr(node, "treeBlobLoc", None):
            tloc = node.treeBlobLoc
            if isinstance(tloc, dict):
                sub_id = tloc.get("blobIdentifier") or ""
            else:
                sub_id = getattr(tloc, "blobIdentifier", "")
            if sub_id:
                tloc_dict = (
                    tloc if isinstance(tloc, dict)
                    else {
                        "blobIdentifier": tloc.blobIdentifier,
                        "relativePath": tloc.relativePath,
                        "offset": tloc.offset,
                        "length": tloc.length,
                    }
                )
                subtrees.append((sub_id, tloc_dict))
    for sub_id, sub_loc in subtrees:
        if limit_remaining <= 0:
            return
        yield from _walk_v4_tree_collect(
            backend, cu, sub_id, sub_loc, keyset,
            limit_remaining=limit_remaining,
        )


def _analyze_records(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Build the K2 stats summary across a list of {nodes, ...}
    record dicts. Returns a JSON-serializable dict."""
    out: Dict[str, Any] = {
        "records": len(records),
        "total_nodes": sum(len(r["nodes"]) for r in records),
        "per_record": [],
        "cross_record": {},
    }
    record_centroids: List[float] = []
    for r in records:
        sec_values = [n["trailing_sec"] for n in r["nodes"]]
        nsec_values = [n["trailing_nsec"] for n in r["nodes"]]
        if not sec_values:
            continue
        sec_min = min(sec_values)
        sec_max = max(sec_values)
        sec_spread = sec_max - sec_min
        walk_seq = [n["trailing_sec"] for n in r["nodes"]]
        monotone_inc = all(
            a <= b for a, b in zip(walk_seq, walk_seq[1:])
        )
        # ctime / mtime / btime correlation counts.
        ctime_match = sum(
            1 for n in r["nodes"]
            if n["trailing_sec"] == n["ctime_sec"]
        )
        mtime_match = sum(
            1 for n in r["nodes"]
            if n["trailing_sec"] == n["mtime_sec"]
        )
        btime_match = sum(
            1 for n in r["nodes"]
            if n["trailing_sec"] == n["btime_sec"]
        )
        # Centroid + nsec stats.
        sec_mean = statistics.mean(sec_values)
        sec_stdev = (
            statistics.stdev(sec_values) if len(sec_values) > 1
            else 0.0
        )
        nsec_mean = statistics.mean(nsec_values) if nsec_values else 0
        # Nsec distribution: is it uniform in [0, 10^9) (real wall-
        # clock) or concentrated (counter / pre-computed)?
        nsec_buckets = [0] * 10
        for v in nsec_values:
            b = min(9, int(v // 10_000_000) // 10) if v else 0
            nsec_buckets[b] += 1
        record_centroids.append(sec_mean)
        out["per_record"].append({
            "record_path": r["record_path"],
            "folder_uuid": r["folder_uuid"],
            "creation_date": r.get("creation_date"),
            "node_count": len(r["nodes"]),
            "sec_min": sec_min,
            "sec_max": sec_max,
            "sec_spread_seconds": sec_spread,
            "sec_mean": sec_mean,
            "sec_stdev": sec_stdev,
            "nsec_mean": nsec_mean,
            "walk_monotonic_increasing": monotone_inc,
            "ctime_sec_match_count": ctime_match,
            "mtime_sec_match_count": mtime_match,
            "btime_sec_match_count": btime_match,
            "nsec_bucket_counts": nsec_buckets,
        })
    if len(record_centroids) > 1:
        # Sort by creationDate to compute drift between adjacent
        # records.
        ordered = sorted(
            ((r.get("creation_date") or 0, c)
             for r, c in zip(records, record_centroids)),
            key=lambda kv: kv[0],
        )
        gaps_sec = [
            (ordered[i + 1][1] - ordered[i][1])
            for i in range(len(ordered) - 1)
        ]
        cd_gaps = [
            (ordered[i + 1][0] - ordered[i][0])
            for i in range(len(ordered) - 1)
        ]
        out["cross_record"] = {
            "trailing_centroid_drift_seconds": gaps_sec,
            "creation_date_drift_seconds": cd_gaps,
        }
    return out


def _main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description=(
            "Analyze Tree v4 trailing-block bytes 0..15 against "
            "real Arq.app v8 destination data — extends Strategy K."
        ),
    )
    p.add_argument("--destination", required=True,
                   help="Local path to the Arq.app destination root")
    p.add_argument("--password-file", required=True, type=Path,
                   help="File containing the encryption password")
    p.add_argument("--limit", type=int, default=1000,
                   help="max v4 child Nodes to inspect (default 1000)")
    p.add_argument("--computer", default=None,
                   help="restrict to a specific computer UUID")
    p.add_argument("--folder", default=None,
                   help="restrict to a specific folder UUID")
    p.add_argument("--json", action="store_true",
                   help="emit JSON to stdout instead of a text summary")
    args = p.parse_args(argv)

    from arq_validator import LocalBackend
    from arq_validator.layout import discover_layout

    backend = LocalBackend(args.destination)
    password = _read_password(args.password_file)

    if args.computer:
        cus = [args.computer]
    else:
        cus = [
            lt.computer_uuid for lt in discover_layout(
                backend, "/", enumerate_objects=False,
            )
        ]

    all_records: List[Dict[str, Any]] = []
    for cu in cus:
        try:
            for r in _walk_v4_records(
                backend, cu, password,
                folder_filter=args.folder, limit=args.limit,
            ):
                all_records.append(r)
        except Exception as exc:
            print(
                f"walk for {cu} interrupted by "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )

    stats = _analyze_records(all_records)
    if args.json:
        json.dump(stats, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    # Text summary.
    print(f"records walked: {stats['records']}")
    print(f"total nodes:    {stats['total_nodes']}")
    print()
    for r in stats["per_record"]:
        print(f"--- {r['record_path']}")
        print(f"  nodes:                 {r['node_count']}")
        print(f"  sec min/max:           {r['sec_min']} / {r['sec_max']}")
        print(f"  sec spread (seconds):  {r['sec_spread_seconds']}")
        print(f"  walk-order monotone:   {r['walk_monotonic_increasing']}")
        print(f"  ctime_sec matches:     {r['ctime_sec_match_count']}/{r['node_count']}")
        print(f"  mtime_sec matches:     {r['mtime_sec_match_count']}/{r['node_count']}")
        print(f"  btime_sec matches:     {r['btime_sec_match_count']}/{r['node_count']}")
        print(f"  nsec buckets (10×10⁸): {r['nsec_bucket_counts']}")
    if stats["cross_record"]:
        gaps_t = stats["cross_record"]["trailing_centroid_drift_seconds"]
        gaps_c = stats["cross_record"]["creation_date_drift_seconds"]
        print()
        print("Cross-record drift (trailing centroid vs creationDate):")
        for t, c in zip(gaps_t, gaps_c):
            print(f"  trailing Δ = {t:>10.1f}s   "
                  f"creationDate Δ = {c:>10.1f}s   "
                  f"ratio = {(t/c if c else float('inf')):>6.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
