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
        help="Create a new backup of <source...> at <--dest> "
             "(local) or <--sftp-host> (remote).",
    )
    create.add_argument(
        "source",
        type=Path, nargs="+",
        help=(
            "One or more source directories to back up. Multiple "
            "sources land as separate backupfolders under the same "
            "computer-uuid + plan-uuid (the writer's add_folder is "
            "called once per source)."
        ),
    )
    create.add_argument(
        "--dest",
        type=Path,
        default=None,
        help=(
            "Local destination root (a fresh empty directory or an "
            "existing Arq backup destination — will append a new "
            "computer subtree). Mutually exclusive with --sftp-host."
        ),
    )
    create.add_argument(
        "--sftp-host",
        default=None,
        help=(
            "SFTP destination host. When set, the backup goes to "
            "the remote server via the existing SftpBackend rather "
            "than to a local --dest path."
        ),
    )
    create.add_argument(
        "--sftp-port",
        type=int, default=22,
        help="SFTP port (default 22).",
    )
    create.add_argument(
        "--sftp-user",
        default="",
        help="SSH username for --sftp-host.",
    )
    create.add_argument(
        "--sftp-path",
        default="",
        help=(
            "Server-side root path under which the backup is "
            "anchored (e.g. /home/u504460/arq-backup). "
            "Equivalent to SftpBackend(root=...)."
        ),
    )
    create.add_argument(
        "--sftp-identity-file",
        type=Path, default=None,
        help="SSH private key for --sftp-host (key-based auth).",
    )
    create.add_argument(
        "--sftp-password-env",
        default=None,
        help=(
            "Env var holding the SSH password for --sftp-host "
            "(used via SSH_ASKPASS). Mutually exclusive with "
            "--sftp-identity-file."
        ),
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
    create.add_argument(
        "--tree-version",
        type=int, choices=(3, 4), default=3,
        help=(
            "Tree binary format version to emit. 3 (default) "
            "matches the Arq 7 spec; 4 also writes the 38-byte "
            "trailing-block per Node we observed in Arq.app v8 "
            "destinations (see docs/REAL-DATA-DISCOVERIES.md §7). "
            "The reader handles both transparently — pick 4 only "
            "when you want the writer's output to match Arq.app "
            "v8's binary shape exactly."
        ),
    )
    create.add_argument(
        "--state-file", type=Path, default=None,
        help=(
            "Path to a JSON state file the CLI updates as work "
            "progresses. The file is overwritten atomically on "
            "every flush so a TUI / monitoring process can poll it "
            "safely. Filename stem is used as the run-id. When "
            "omitted, no state file is written. See "
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


def _validate_destination_args(
    args: argparse.Namespace,
) -> Optional[str]:
    """Return None when --dest / --sftp-host are valid, else a
    user-facing error message."""
    if args.dest is not None and args.sftp_host:
        return (
            "--dest and --sftp-host are mutually exclusive; "
            "pick one"
        )
    if args.dest is None and not args.sftp_host:
        return (
            "either --dest <path> (local) or --sftp-host <host> "
            "(remote) must be supplied"
        )
    if args.sftp_host and not args.sftp_path:
        return (
            "--sftp-path is required when --sftp-host is set "
            "(server-side root the backup is anchored under)"
        )
    return None


def _open_sftp_backend(args: argparse.Namespace):
    """Build + open an SftpBackend from the --sftp-* args."""
    from arq_validator.sftp import SftpBackend
    sftp_password = None
    if args.sftp_password_env:
        sftp_password = os.environ.get(args.sftp_password_env, "")
        if not sftp_password:
            print(
                f"warning: --sftp-password-env "
                f"{args.sftp_password_env!r} is unset; "
                f"falling back to identity-based auth",
                file=sys.stderr,
            )
            sftp_password = None
    backend = SftpBackend(
        args.sftp_host, port=args.sftp_port, user=args.sftp_user,
        password=sftp_password,
        identity_file=args.sftp_identity_file,
        root=args.sftp_path,
    )
    backend.__enter__()
    return backend


def _run_backup_call(
    *, sources, args, password, callback, exclusions, chunker_config,
):
    """Drive ``Backup`` directly so we can call ``add_folder`` once
    per source and so we can attach an SftpBackend when --sftp-host
    is set. Returns a dict suitable for the JSON output (mirrors
    the dataclass shape ``build_backup`` used to return)."""
    from .backup import Backup

    use_sftp = bool(args.sftp_host)
    backend = _open_sftp_backend(args) if use_sftp else None
    try:
        bk = Backup(
            dest_root=Path("/") if use_sftp else args.dest,
            encryption_password=password,
            backup_name=args.backup_name,
            callback=callback,
            openssl_path=args.openssl_path,
            computer_uuid=args.computer_uuid,
            plan_uuid=args.plan_uuid,
            use_packs=args.use_packs,
            chunker_config=chunker_config,
            dedup_against_existing=args.dedup_against_existing,
            max_file_bytes=args.max_file_bytes,
            exclusions=exclusions,
            backend=backend,
            tree_version=args.tree_version,
        )
        bk.init_plan()
        import time as _time
        started = _time.time()
        recs = []
        folder_uuids: list = []
        for i, src in enumerate(sources):
            # First source can keep the operator-supplied
            # --folder-uuid / --folder-name; later sources get
            # auto-generated UUIDs + their basename so they don't
            # collide.
            if i == 0:
                fu = args.folder_uuid
                fn = args.folder_name
            else:
                fu = None
                fn = src.name
            rec_path = bk.add_folder(
                src, folder_uuid=fu, folder_name=fn,
            )
            recs.append(str(rec_path))
            # The actual UUID Backup picked (whether it was the
            # explicit --folder-uuid or an autogenerated one)
            # lives in _folder_plans[-1]["folder_uuid"]; surface
            # it so single-source callers can look up the
            # backupfolder dir without parsing rec_path.
            # build_folder_plan emits "backupFolderUUID" (camelCase
            # matching Arq's JSON schema) — not "folder_uuid".
            try:
                folder_uuids.append(
                    bk._folder_plans[-1]["backupFolderUUID"],
                )
            except (IndexError, KeyError):
                folder_uuids.append("")
        elapsed = _time.time() - started
        # Single-source callers (the historical CLI shape) get
        # the legacy keys (folder_uuid, backuprecord_path); multi-
        # source callers see the lists. Both shapes are present so
        # downstream JSON consumers can pick whichever they need.
        return {
            "dest_root": str(bk.dest_root),
            "computer_uuid": bk.computer_uuid,
            "plan_uuid": bk.plan_uuid,
            "folder_uuid": folder_uuids[0] if folder_uuids else "",
            "folder_uuids": folder_uuids,
            "backuprecord_path": recs[0] if recs else "",
            "backuprecord_paths": recs,
            "files_written": bk.files_written,
            "files_reused": bk.files_reused,
            "trees_written": bk.trees_written,
            "bytes_plaintext": bk.bytes_plaintext,
            "bytes_on_disk": bk.bytes_on_disk,
            "blob_count": len(bk.blob_ids),
            "elapsed_sec": elapsed,
        }
    finally:
        if backend is not None:
            try:
                backend.__exit__(None, None, None)
            except Exception:
                pass


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command != "create":
        print(f"error: unknown command {args.command!r}", file=sys.stderr)
        return 2

    err = _validate_destination_args(args)
    if err is not None:
        print(f"error: {err}", file=sys.stderr)
        return 2

    # Validate every source is a directory before we open any
    # backend or prompt for passwords; better to fail fast than
    # to leak an SSH connection / askpass tempfile.
    for src in args.source:
        if not src.exists():
            print(
                f"error: source does not exist: {src}",
                file=sys.stderr,
            )
            return 2
        if not src.is_dir():
            print(
                f"error: source must be a directory: {src}",
                file=sys.stderr,
            )
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

    user_cb = _make_callback(args)
    chunker_config = _resolve_chunker(args.chunker)
    exclusions = _resolve_exclusions(args)

    dest_kind = "sftp" if args.sftp_host else "local"
    dest_label = (
        f"sftp://{args.sftp_user}@{args.sftp_host}{args.sftp_path}"
        if args.sftp_host else str(args.dest)
    )

    # Run the backup with optional state-file IPC. The state writer
    # wraps the whole call so any exception (incl. Ctrl-C) flips the
    # file's status to FAILED / CANCELLED automatically; on clean
    # exit it lands as COMPLETED.
    if args.state_file is not None:
        from arq_tui.runs import RunKind, run_writer_context

        try:
            with run_writer_context(
                kind=RunKind.BACKUP,
                plan_id=args.plan_uuid or "",
                plan_name=args.backup_name,
                state_file=args.state_file,
            ) as rw:
                rw.set_destination(
                    kind=dest_kind, label=dest_label,
                    computer_uuid=args.computer_uuid or "",
                )

                def cb(kind: str, payload: dict) -> None:
                    rw.event(kind, **payload)
                    if user_cb is not None:
                        user_cb(kind, payload)

                summary = _run_backup_call(
                    sources=args.source, args=args, password=password,
                    callback=cb, exclusions=exclusions,
                    chunker_config=chunker_config,
                )
                rw.set_result({
                    "computer_uuid": summary["computer_uuid"],
                    "files_written": summary["files_written"],
                    "files_reused": summary["files_reused"],
                    "bytes_plaintext": summary["bytes_plaintext"],
                })
        except Exception as exc:
            print(f"error: backup failed: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            return 4
    else:
        try:
            summary = _run_backup_call(
                sources=args.source, args=args, password=password,
                callback=user_cb, exclusions=exclusions,
                chunker_config=chunker_config,
            )
        except Exception as exc:
            print(f"error: backup failed: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            return 4

    print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
