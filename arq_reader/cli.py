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
    p_restore.add_argument(
        "--verify-after",
        action="store_true",
        help=(
            "After the restore, walk the destination + recompute "
            "SHA-256 of each restored file + compare to the "
            "recorded blob_id. Catches truncation, byte corruption, "
            "and incomplete writes the restore step itself didn't "
            "surface. Adds one full re-read of every restored file "
            "(roughly doubles wall time on local restores; "
            "negligible vs SFTP fetch time on remote restores). "
            "Chunked files are flagged 'verify_skipped_chunked' "
            "rather than re-chunked."
        ),
    )
    p_restore.add_argument(
        "--on-conflict",
        choices=("overwrite", "skip", "rename"),
        default="overwrite",
        help=(
            "Policy when a restored file would land on top of "
            "an existing file in the destination. 'overwrite' "
            "(default, legacy behaviour) silently replaces; "
            "'skip' leaves the existing file alone + drops the "
            "restored bytes (with a 'conflict_skipped' event); "
            "'rename' writes the restored bytes to a sibling "
            "path with a '.restored-N' suffix so both versions "
            "remain (with a 'conflict_renamed' event)."
        ),
    )
    p_restore.add_argument(
        "--list-only",
        action="store_true",
        help=(
            "Walk the backuprecord's tree + emit one "
            "would_restore_file event per file WITHOUT touching "
            "the destination. Use to verify a --paths filter, "
            "size a restore, or spot-check a snapshot's contents "
            "before committing to the I/O. Exits 0 even when no "
            "files match (the dry-run completed cleanly); the "
            "JSON summary on stdout has files_listed + "
            "bytes_would_restore + sample_paths."
        ),
    )
    p_restore.add_argument(
        "--paths",
        action="append",
        default=None,
        help=(
            "Restrict the (real or dry-run) restore to source-"
            "relative POSIX paths, repeatable. A path that names "
            "a directory recursively includes its subtree. "
            "Without --paths, the full tree is restored."
        ),
    )

    for sp in (p, *sub.choices.values()):
        sp.add_argument("--quiet", action="store_true")
        sp.add_argument("--json-events", action="store_true")
        sp.add_argument(
            "--debug", default=None, nargs="?", const="all",
            metavar="SUBSYSTEMS",
            help=(
                "Verbose debug logging. Pass alone for all "
                "subsystems, or comma-separated names "
                "(sftp,blob,tree,cli,backend,crypto). Goes to "
                "stderr so --json-events stdout stays parseable."
            ),
        )
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


def _has_unencrypted_subtree(src: Path) -> bool:
    """True if any computer subtree under ``src`` is an unencrypted
    destination (backupconfig ``isEncrypted: false`` and/or no keyset file),
    in which case a password is not required. See docs/UNENCRYPTED-FORMAT-RE.md."""
    try:
        for d in Path(src).iterdir():
            if not d.is_dir():
                continue
            bc = d / "backupconfig.json"
            if not bc.is_file():
                continue
            try:
                if json.loads(bc.read_text()).get("isEncrypted") is False:
                    return True
            except Exception:
                pass
            if not (d / "encryptedkeyset.dat").exists():
                return True
    except OSError:
        pass
    return False


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
    # Wire --debug as early as possible so any error path (path
    # validation, password lookup, backend open) gets logged.
    if getattr(args, "debug", None) is not None:
        from arq_validator.debug_logging import (
            enable_debug_logging, parse_debug_flag,
        )
        enable_debug_logging(
            subsystems=parse_debug_flag(args.debug),
        )
    if not args.src.is_dir():
        print(f"error: src is not a directory: {args.src}", file=sys.stderr)
        return 2

    password = _resolve_password(args)
    # Unencrypted destinations ("Continue Without Encryption") have no keyset
    # and need no password — only prompt/require when encryption is in play.
    unencrypted = _has_unencrypted_subtree(args.src)
    if password is None and not unencrypted and sys.stdin.isatty():
        try:
            password = getpass.getpass("Backup encryption password: ")
        except (KeyboardInterrupt, EOFError):
            print("\naborted", file=sys.stderr)
            return 2
    if not password and not unencrypted:
        print(
            "error: encryption password required; pass --password / "
            "--password-file / --password-env or run from a TTY.",
            file=sys.stderr,
        )
        return 2
    password = password or ""

    cb = _make_callback(args)
    restorer = Restore(
        args.src, password, openssl_path=args.openssl_path,
        on_conflict=getattr(args, "on_conflict", "overwrite"),
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
        # --list-only short-circuits the whole restore path; we
        # never open the destination, never fetch a file blob,
        # never write a byte. The dest argument is still
        # required positionally (so the CLI parses uniformly)
        # but it's only used to echo back in the JSON summary.
        if getattr(args, "list_only", False):
            try:
                dr = restorer.dry_run_restore(
                    folder_uuid=args.folder_uuid,
                    computer_uuid=args.computer_uuid,
                    paths=getattr(args, "paths", None),
                    callback=cb,
                )
            except Exception as exc:
                print(
                    f"error: dry-run failed: "
                    f"{type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
                return 4
            out = {
                "src": str(dr.src),
                "folder_uuid": dr.folder_uuid,
                "backuprecord_path": dr.backuprecord_path,
                "files_listed": dr.files_listed,
                "dirs_listed": dr.dirs_listed,
                "bytes_would_restore": dr.bytes_would_restore,
                "sample_paths": list(dr.sample_paths),
                "list_only": True,
            }
            print(json.dumps(out, indent=2, ensure_ascii=False))
            return 0
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
                        paths=getattr(args, "paths", None),
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
                    paths=getattr(args, "paths", None),
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

        # Optional post-restore verification. Walks the
        # destination + checks each restored file's bytes match
        # the source's recorded blob_id chain. Catches
        # truncation / silent corruption that the restore step
        # itself didn't surface.
        if getattr(args, "verify_after", False):
            from .restore_verify import verify_restored_walk
            verify = verify_restored_walk(
                args.dest,
                expected_size_total=result.bytes_restored,
                expected_file_count=result.files_restored,
            )
            out["verify"] = {
                "ok": verify.ok,
                "files_verified": verify.files_verified,
                "files_skipped_chunked": verify.files_skipped_chunked,
                "failures": [
                    {
                        "path": f.path, "kind": f.kind,
                        "expected": f.expected, "actual": f.actual,
                    } for f in verify.failures
                ],
            }
            if cb is not None:
                cb(
                    "verify_completed",
                    {"ok": verify.ok,
                     "files_verified": verify.files_verified,
                     "failure_count": len(verify.failures)},
                )
            print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
            if not verify.ok:
                return 5      # distinct from restore-failed (4)
            return 0 if not result.failures else 4
        print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
        return 4 if result.failures else 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
