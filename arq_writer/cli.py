"""Standalone CLI: ``python -m arq_writer create <src> --dest <dest>``."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path
from typing import List, Optional

from .backup import build_backup


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="arq-backup",
        description=(
            "Independent Arq 7 backup writer. Creates Arq.app-compatible "
            "backups using only standalone-object storage (no pack "
            "containers, no chunker)."
        ),
    )
    sub = p.add_subparsers(dest="command", required=True)

    create = sub.add_parser(
        "create",
        help="Create a new backup of <source> at <--dest>.",
    )
    create.add_argument(
        "source",
        type=Path,
        help="Source directory to back up.",
    )
    create.add_argument(
        "--dest",
        type=Path,
        required=True,
        help="Destination root (a fresh empty directory or an existing "
             "Arq backup destination — will append a new computer subtree).",
    )
    create.add_argument(
        "--password",
        default=None,
        help="Encryption password. If omitted, prompts on TTY.",
    )
    create.add_argument(
        "--password-file",
        default=None,
        type=Path,
        help="File containing the encryption password (mode 0600 recommended).",
    )
    create.add_argument(
        "--password-env",
        default=None,
        help="Env var to read the encryption password from.",
    )
    create.add_argument(
        "--backup-name",
        default="TUI backup",
        help="Backup-set human-readable name.",
    )
    create.add_argument(
        "--folder-name",
        default=None,
        help="Per-folder display name. Defaults to source dir basename.",
    )
    create.add_argument(
        "--computer-uuid",
        default=None,
        help="Override the computer UUID (otherwise random).",
    )
    create.add_argument(
        "--plan-uuid",
        default=None,
        help="Override the plan UUID (otherwise random).",
    )
    create.add_argument(
        "--folder-uuid",
        default=None,
        help="Override the folder UUID (otherwise random).",
    )
    create.add_argument(
        "--openssl-path",
        default="openssl",
        help="Path to the openssl binary (used for AES-256-CBC).",
    )
    create.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress per-file progress on stderr.",
    )
    create.add_argument(
        "--json-events",
        action="store_true",
        help="Emit each progress event as a JSON line on stderr.",
    )
    create.add_argument(
        "--use-packs",
        action="store_true",
        help=(
            "Pack mode — emit treepacks/ + blobpacks/ instead of "
            "standardobjects/. Smaller per-folder file count, "
            "matches Arq.app's default packed layout."
        ),
    )
    create.add_argument(
        "--chunker",
        choices=("none", "default", "arq_v7_41"),
        default="none",
        help=(
            "Chunker selection. 'none' = single blob per file "
            "(default); 'default' = generic Buzhash; "
            "'arq_v7_41' = Arq.app v7.41-matching parameters."
        ),
    )
    create.add_argument(
        "--dedup-against-existing",
        action="store_true",
        help=(
            "Reuse the destination's existing keyset and seed the "
            "dedup cache from prior backups. Required for "
            "incremental re-runs against the same destination."
        ),
    )
    create.add_argument(
        "--max-file-bytes",
        type=int, default=None,
        help=(
            "Skip files larger than this many bytes. Symlinks "
            "are exempt."
        ),
    )
    create.add_argument(
        "--exclude-glob",
        action="append", default=[],
        metavar="PATTERN",
        help=(
            "Wildcard exclusion (fnmatch syntax). May be passed "
            "multiple times. Matched against entry name AND "
            "source-relative path."
        ),
    )
    create.add_argument(
        "--exclude-regex",
        action="append", default=[],
        metavar="PATTERN",
        help=(
            "Python regex exclusion, full-match against the "
            "source-relative POSIX path. May be passed multiple "
            "times."
        ),
    )
    create.add_argument(
        "--exclude-from",
        type=Path, default=None,
        metavar="FILE",
        help=(
            "Read .gitignore-style patterns from FILE (one per "
            "line; '#' comments and blank lines OK; '!' negates)."
        ),
    )
    create.add_argument(
        "--use-apfs-snapshot",
        action="store_true",
        help=(
            "macOS only — back up an APFS snapshot of the source "
            "instead of the live tree, so file content can't shift "
            "mid-walk. Falls through silently on non-macOS hosts. "
            "Requires sudo for tmutil + mount_apfs."
        ),
    )
    return p


def _resolve_password(args: argparse.Namespace) -> Optional[str]:
    if args.password is not None:
        return args.password
    if args.password_env:
        v = os.environ.get(args.password_env, "")
        if v:
            return v
    if args.password_file:
        try:
            return args.password_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            print(f"error: failed to read password file: {exc}",
                  file=sys.stderr)
            return None
    return None


def _resolve_chunker(name: str):
    if name == "default":
        from .chunker import ChunkerConfig
        return ChunkerConfig()
    if name == "arq_v7_41":
        from .arq_chunker_params import ARQ_V7_CHUNKER_CONFIG
        return ARQ_V7_CHUNKER_CONFIG
    return None


def _resolve_exclusions(args: argparse.Namespace):
    """Build an :class:`ExclusionRules` from the three CLI flags.

    Returns ``None`` (= no filtering) when nothing is set so the
    writer can short-circuit.
    """
    gitignore_lines = ()
    if args.exclude_from is not None:
        try:
            gitignore_lines = tuple(
                args.exclude_from.read_text(
                    encoding="utf-8",
                ).splitlines()
            )
        except OSError as exc:
            print(
                f"error: --exclude-from {args.exclude_from}: {exc}",
                file=sys.stderr,
            )
            sys.exit(2)
    if not (args.exclude_glob or args.exclude_regex or gitignore_lines):
        return None
    from .exclusions import ExclusionRules
    return ExclusionRules.of(
        wildcard=args.exclude_glob,
        regex=args.exclude_regex,
        gitignore_lines=gitignore_lines,
    )


def _make_callback(args: argparse.Namespace):
    if args.quiet:
        return None

    def cb(kind: str, payload: dict) -> None:
        if args.json_events:
            print(json.dumps({"kind": kind, **payload}, ensure_ascii=False),
                  file=sys.stderr)
            return
        # File-by-file progress is verbose; suppress all but writes / errors.
        if kind in ("file_written", "tree_written"):
            return
        message = payload.get("path") or payload.get("error") or ""
        print(f"[{kind}] {message}", file=sys.stderr)

    return cb


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command != "create":
        print(f"error: unknown command {args.command!r}", file=sys.stderr)
        return 2

    if not args.source.exists():
        print(f"error: source does not exist: {args.source}", file=sys.stderr)
        return 2
    if not args.source.is_dir():
        print(f"error: source must be a directory: {args.source}",
              file=sys.stderr)
        return 2

    password = _resolve_password(args)
    if password is None and sys.stdin.isatty():
        try:
            password = getpass.getpass("Backup encryption password: ")
            confirm = getpass.getpass("Confirm: ")
            if password != confirm:
                print("error: passwords do not match", file=sys.stderr)
                return 2
        except (KeyboardInterrupt, EOFError):
            print("\naborted", file=sys.stderr)
            return 2
    if not password:
        print(
            "error: encryption password required; pass --password / "
            "--password-file / --password-env or run from a TTY.",
            file=sys.stderr,
        )
        return 2

    cb = _make_callback(args)
    chunker_config = _resolve_chunker(args.chunker)
    exclusions = _resolve_exclusions(args)

    try:
        result = build_backup(
            args.source,
            args.dest,
            password,
            backup_name=args.backup_name,
            folder_name=args.folder_name,
            callback=cb,
            openssl_path=args.openssl_path,
            computer_uuid=args.computer_uuid,
            plan_uuid=args.plan_uuid,
            folder_uuid=args.folder_uuid,
            use_packs=args.use_packs,
            chunker_config=chunker_config,
            dedup_against_existing=args.dedup_against_existing,
            max_file_bytes=args.max_file_bytes,
            exclusions=exclusions,
            use_apfs_snapshot=args.use_apfs_snapshot,
        )
    except Exception as exc:
        print(f"error: backup failed: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 4

    out = asdict(result)
    out["dest_root"] = str(out["dest_root"])
    out["backuprecord_path"] = str(out["backuprecord_path"])
    out["elapsed_sec"] = result.elapsed_sec
    print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
