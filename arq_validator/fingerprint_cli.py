"""CLI for shape-fingerprint generation + comparison.

Two subcommands:

- ``compute <path>`` — build a fingerprint of the destination at
  ``<path>`` and emit it as JSON on stdout. ``<path>`` is a local
  filesystem path; for SFTP, mount the remote with rclone first.
- ``compare <a.json> <b.json>`` — diff two previously-saved
  fingerprints and emit a structured diff on stdout. Exits with
  code 0 on a clean match, 1 on any difference.

Use case: see ``docs/COMPAT-VERIFICATION.md`` — the operator
generates one fingerprint from an Arq.app destination on macOS
and one from this writer's destination of the same source, then
runs ``arq-fingerprint compare`` to spot any compatibility gaps.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path
from typing import List, Optional

from .backend import LocalBackend
from .fingerprint import compute_shape_fingerprint, diff_fingerprints


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="arq-fingerprint",
        description=(
            "Generate or compare shape fingerprints of Arq 7 "
            "destinations. Salt-independent: identical source + "
            "identical chunker → identical fingerprint regardless "
            "of which tool produced the destination."
        ),
    )
    sub = p.add_subparsers(dest="command", required=True)

    compute = sub.add_parser(
        "compute", help="Compute fingerprint of a destination.",
    )
    compute.add_argument(
        "path", type=Path,
        help="Local filesystem path to the destination root.",
    )
    compute.add_argument(
        "--password", default=None,
        help="Encryption password. Prompts on TTY if omitted.",
    )
    compute.add_argument(
        "--password-env", default=None,
        help="Env var to read the password from.",
    )
    compute.add_argument(
        "--computer-uuid", default=None,
        help="Restrict to one computer subtree.",
    )
    compute.add_argument(
        "--max-records-per-folder", type=int, default=None,
        metavar="N",
        help=(
            "Per backup folder, fingerprint only the latest N "
            "backuprecords instead of every one. Use against large "
            "real-world destinations where a full walk is "
            "intractable (e.g. --max-records-per-folder 1 for a "
            "latest-only diff). Both sides of a comparison should "
            "apply the same cap so record_count_diffs stays zero."
        ),
    )
    compute.add_argument(
        "--openssl-path", default="openssl",
    )
    compute.add_argument(
        "--out", type=Path, default=None,
        help="Write JSON to this path (default: stdout).",
    )

    cmp = sub.add_parser(
        "compare",
        help="Compare two fingerprint JSON files.",
    )
    cmp.add_argument("a", type=Path)
    cmp.add_argument("b", type=Path)

    return p


def _resolve_password(args: argparse.Namespace) -> Optional[str]:
    if args.password:
        return args.password
    if args.password_env:
        v = os.environ.get(args.password_env, "")
        if v:
            return v
    if sys.stdin.isatty():
        try:
            return getpass.getpass("Encryption password: ")
        except (KeyboardInterrupt, EOFError):
            return None
    return None


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "compute":
        if not args.path.is_dir():
            print(
                f"error: {args.path} is not a directory",
                file=sys.stderr,
            )
            return 2
        if (
            args.max_records_per_folder is not None
            and args.max_records_per_folder < 1
        ):
            print(
                "error: --max-records-per-folder must be >= 1",
                file=sys.stderr,
            )
            return 2
        password = _resolve_password(args)
        if not password:
            print(
                "error: encryption password required "
                "(--password / --password-env / TTY)",
                file=sys.stderr,
            )
            return 2
        backend = LocalBackend(args.path)
        try:
            fp = compute_shape_fingerprint(
                backend,
                encryption_password=password,
                computer_uuid=args.computer_uuid,
                max_records_per_folder=args.max_records_per_folder,
                openssl_path=args.openssl_path,
            )
        except Exception as exc:
            print(
                f"error: fingerprint failed: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            return 4
        text = json.dumps(fp, indent=2, ensure_ascii=False)
        if args.out:
            args.out.write_text(text + "\n", encoding="utf-8")
        else:
            print(text)
        return 0
    if args.command == "compare":
        try:
            a = json.loads(args.a.read_text(encoding="utf-8"))
            b = json.loads(args.b.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(
                f"error: could not read fingerprint files: {exc}",
                file=sys.stderr,
            )
            return 2
        diff = diff_fingerprints(a, b)
        print(json.dumps(diff, indent=2, ensure_ascii=False))
        return 0 if diff.get("match") else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
