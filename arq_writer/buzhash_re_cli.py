"""CLI front-end for the Buzhash parameter RE workflow.

Two subcommands:

- ``analyze-binary <path>`` — scan a Mach-O (or any other binary)
  for candidate Buzhash T tables and chunker constants.
- ``infer-from-sizes <file>`` — read a list of chunk sizes (one
  integer per line, or a JSON array) from a file or stdin and
  output an estimated Buzhash parameter set.

Sandboxes that can't fetch Arq.app from arqbackup.com can still use
this CLI by piping in a binary that the user fetched on their local
machine, e.g.::

    arq-buzhash-find analyze-binary /Applications/Arq.app/Contents/MacOS/Arq

Or, given a real backup, derive chunk sizes by walking
``backuprecord`` files and recording each ``BlobLoc.length``, then::

    arq-buzhash-find infer-from-sizes ./chunk-sizes.json

A successful run produces JSON on stdout. Use the result to populate
``arq_writer.chunker.register_arq_chunker(...)``.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import List, Optional

from .macho_buzhash_finder import (
    analyze_macho_for_buzhash,
    infer_parameters_from_chunk_sizes,
)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="arq-buzhash-find",
        description=(
            "Reverse-engineering aids for Arq.app chunker parameters."
        ),
    )
    sub = p.add_subparsers(dest="command", required=True)

    p_bin = sub.add_parser(
        "analyze-binary",
        help="Static-scan a Mach-O binary for the Buzhash T table.",
    )
    p_bin.add_argument("path", type=Path)
    p_bin.add_argument(
        "--stride", default=16, type=int,
        help="Window stride. Smaller = more thorough, slower (default 16).",
    )
    p_bin.add_argument(
        "--top-k", default=10, type=int,
        help="How many top-scored candidates to return.",
    )

    p_inf = sub.add_parser(
        "infer-from-sizes",
        help=("Estimate Buzhash parameters from a list of observed "
              "chunk sizes."),
    )
    p_inf.add_argument(
        "path", nargs="?", type=Path, default=None,
        help=("Path to JSON array or text file (one int per line). "
              "If omitted, reads from stdin."),
    )

    return p


def _load_sizes(path: Optional[Path]) -> List[int]:
    raw = (
        path.read_text(encoding="utf-8")
        if path else sys.stdin.read()
    )
    raw = raw.strip()
    if not raw:
        return []
    if raw.startswith("["):
        return [int(x) for x in json.loads(raw)]
    return [int(line) for line in raw.splitlines() if line.strip()]


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "analyze-binary":
        report = analyze_macho_for_buzhash(
            args.path, table_search_stride=args.stride, top_k=args.top_k,
        )
        print(json.dumps(report.to_dict(), indent=2))
        return 0
    if args.command == "infer-from-sizes":
        sizes = _load_sizes(args.path)
        if not sizes:
            print("error: no chunk sizes provided", file=sys.stderr)
            return 2
        result = infer_parameters_from_chunk_sizes(sizes)
        print(json.dumps(asdict(result), indent=2))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
