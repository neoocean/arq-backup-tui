"""Standalone CLI: ``python -m arq_reader {list,restore} ...``."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path
from typing import List, Optional

from .restore import Restore


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="arq-reader",
        description=(
            "Restore Arq 7 backups produced by arq_writer (and any "
            "other writer that uses the same standalone-objects layout)."
        ),
    )
    sub = p.add_subparsers(dest="command", required=True)

    p_common = argparse.ArgumentParser(add_help=False)
    p_common.add_argument(
        "src", type=Path, help="Backup destination root.",
    )
    p_common.add_argument("--password", default=None)
    p_common.add_argument("--password-file", default=None, type=Path)
    p_common.add_argument("--password-env", default=None)
    p_common.add_argument("--openssl-path", default="openssl")

    sub.add_parser(
        "list", parents=[p_common],
        help="List computer UUIDs + backup folders at <src>.",
    )

    p_restore = sub.add_parser(
        "restore", parents=[p_common],
        help="Restore the latest backuprecord of <folder-uuid> into <dest>.",
    )
    p_restore.add_argument("folder_uuid")
    p_restore.add_argument("dest", type=Path)
    p_restore.add_argument(
        "--computer-uuid",
        default=None,
        help="Disambiguates when multiple computer subtrees share a folder UUID.",
    )

    for sp in (p, *sub.choices.values()):
        sp.add_argument("--quiet", action="store_true")
        sp.add_argument("--json-events", action="store_true")
        sp.add_argument(
            "--state-file", type=Path, default=None,
            help=(
                "Path to a JSON state file the CLI updates as work "
                "progresses (atomic writes; safe to poll). The "
                "filename stem is used as the run-id. See "
                "docs/PLAN-cli-tui-split.md."
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


def _make_callback(args: argparse.Namespace):
    if args.quiet:
        return None

    def cb(kind: str, payload: dict) -> None:
        if args.json_events:
            print(json.dumps({"kind": kind, **payload}, ensure_ascii=False),
                  file=sys.stderr)
            return
        # Skip per-file noise unless json events are requested.
        if kind in ("file_restored", "tree_restored"):
            return
        msg = payload.get("path") or payload.get("error") or ""
        print(f"[{kind}] {msg}", file=sys.stderr)

    return cb


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    if not args.src.is_dir():
        print(f"error: src is not a directory: {args.src}", file=sys.stderr)
        return 2

    password = _resolve_password(args)
    if password is None and sys.stdin.isatty():
        try:
            password = getpass.getpass("Backup encryption password: ")
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
    restorer = Restore(
        args.src, password, openssl_path=args.openssl_path,
    )

    if args.command == "list":
        out = {
            "src": str(args.src),
            "computers": [],
        }
        for lay in restorer.layouts():
            out["computers"].append({
                "computer_uuid": lay.computer_uuid,
                "has_keyset": lay.has_keyset,
                "blobpack_count": len(lay.blobpacks),
                "treepack_count": len(lay.treepacks),
                "standardobject_count": len(lay.standardobjects),
                "folders": lay.backup_folder_uuids,
            })
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0

    if args.command == "restore":
        # Wrap in optional state-file IPC so cron / TUI can monitor.
        if getattr(args, "state_file", None) is not None:
            from arq_tui.runs import RunKind, run_writer_context

            try:
                with run_writer_context(
                    kind=RunKind.RESTORE,
                    state_file=args.state_file,
                ) as rw:
                    rw.set_destination(
                        kind="local", label=str(args.src),
                        computer_uuid=args.computer_uuid or "",
                    )

                    def cb_with_state(kind: str, payload: dict) -> None:
                        rw.event(kind, **payload)
                        if cb is not None:
                            cb(kind, payload)

                    result = restorer.restore(
                        folder_uuid=args.folder_uuid,
                        dest=args.dest,
                        computer_uuid=args.computer_uuid,
                        callback=cb_with_state,
                    )
                    rw.set_result({
                        "files_restored": result.files_restored,
                        "dirs_restored": result.dirs_restored,
                        "bytes_restored": result.bytes_restored,
                        "blobs_fetched": result.blobs_fetched,
                        "failures": len(result.failures),
                    })
            except Exception as exc:
                print(
                    f"error: restore failed: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
                return 4
        else:
            try:
                result = restorer.restore(
                    folder_uuid=args.folder_uuid,
                    dest=args.dest,
                    computer_uuid=args.computer_uuid,
                    callback=cb,
                )
            except Exception as exc:
                print(
                    f"error: restore failed: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
                return 4
        out = asdict(result)
        out["src"] = str(out["src"])
        out["dest"] = str(out["dest"])
        print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
        return 4 if result.failures else 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
