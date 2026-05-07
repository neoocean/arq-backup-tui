"""Standalone CLI for the Arq validator.

The TUI is the primary consumer of this library; the CLI exists for
quick checks and CI-style scripted runs. Output is JSON on stdout
plus a human-readable progress stream on stderr.

Examples
--------

    # L0 only — does the backup look like a real Arq 7 layout?
    python -m arq_validator dry-run /path/to/backup

    # L0 + 5% magic-byte sample
    python -m arq_validator quick /path/to/backup

    # + decrypt keyset + verify each folder's latest backuprecord
    python -m arq_validator deep /path/to/backup --password 'p455w0rd'

    # Full HMAC sweep over every object
    python -m arq_validator audit /path/to/backup --password-file ~/.arq.pw

Exit codes
----------

    0   validation completed without failures
    2   invocation error (bad args, missing password, missing backend)
    3   backend / IO error
    4   validation completed but with failures (see report JSON)
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path
from typing import List, Optional

from .backend import LocalBackend
from .events import Event, EventKind
from .runner import ValidationReport, ValidationTier, validate
from .tiers import AUDIT_DEFAULT_SKIP_LARGER_THAN


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="arq-validator",
        description=(
            "Independent validator for Arq 7 backups. Validates a "
            "backup destination without the official Arq.app."
        ),
    )
    p.add_argument(
        "tier",
        choices=[t.value for t in ValidationTier],
        help="Validation tier (cheaper -> deeper).",
    )
    p.add_argument(
        "path",
        type=Path,
        help="Path to the Arq backup destination root.",
    )
    p.add_argument(
        "--password",
        default=None,
        help=("Encryption password (deep / audit only). If omitted, "
              "and stdin is a TTY, you'll be prompted."),
    )
    p.add_argument(
        "--password-file",
        default=None,
        type=Path,
        help="File containing the encryption password (mode 0600 recommended).",
    )
    p.add_argument(
        "--password-env",
        default=None,
        help="Environment variable to read the encryption password from.",
    )
    p.add_argument(
        "--sample-fraction",
        default=0.05,
        type=float,
        help=("quick/deep/audit only — fraction of object files to "
              "magic-check (default 0.05). 1.0 = full sweep, 0 = skip "
              "the magic check entirely."),
    )
    p.add_argument(
        "--audit-skip-larger-than",
        default=AUDIT_DEFAULT_SKIP_LARGER_THAN,
        type=int,
        help=("audit only — skip files larger than this many bytes. "
              "0 disables the skip (every file is verified). Default "
              f"{AUDIT_DEFAULT_SKIP_LARGER_THAN}."),
    )
    p.add_argument(
        "--audit-max-runtime-sec",
        default=0,
        type=int,
        help="audit only — soft cap on wall time. 0 = unlimited.",
    )
    p.add_argument(
        "--audit-max-bytes",
        default=0,
        type=int,
        help="audit only — soft cap on bytes read. 0 = unlimited.",
    )
    p.add_argument(
        "--openssl-path",
        default="openssl",
        help="Path to the openssl binary (used for AES-256-CBC decrypt).",
    )
    p.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress the per-event human-readable progress on stderr.",
    )
    p.add_argument(
        "--json-events",
        action="store_true",
        help="Emit each progress event as a JSON line on stderr.",
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
            print(
                f"error: failed to read password file: {exc}",
                file=sys.stderr,
            )
            return None
    return None


def _make_callback(args: argparse.Namespace):
    if args.quiet:
        return None

    def cb(event: Event) -> None:
        if args.json_events:
            payload = {
                "kind": event.kind.value,
                "message": event.message,
                **event.payload,
            }
            print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
            return
        kind = event.kind
        if kind in (EventKind.LOG, EventKind.MAGIC_CHECK_PROGRESS,
                    EventKind.AUDIT_PROGRESS, EventKind.RUN_STARTED,
                    EventKind.RUN_FINISHED, EventKind.TIER_STARTED,
                    EventKind.TIER_FINISHED, EventKind.LAYOUT_DISCOVERED,
                    EventKind.KEYSET_DECRYPTED, EventKind.KEYSET_FAILED,
                    EventKind.MAGIC_CHECK_FAILED,
                    EventKind.BACKUPRECORD_FAILED,
                    EventKind.AUDIT_FILE_FAILED):
            print(f"[{kind.value}] {event.message}", file=sys.stderr)

    return cb


def _exit_code_for(report: ValidationReport) -> int:
    if report.error and any(
        s in report.error
        for s in ("FileNotFoundError", "NotADirectoryError",
                  "PermissionError", "IsADirectoryError")
    ):
        return 3
    if report.error:
        return 4 if "encryption_password" not in report.error else 2
    return 4 if report.has_failures() else 0


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    tier = ValidationTier(args.tier)

    try:
        backend = LocalBackend(args.path)
    except (FileNotFoundError, NotADirectoryError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    password: Optional[str] = None
    if tier in (ValidationTier.DEEP, ValidationTier.AUDIT):
        password = _resolve_password(args)
        if password is None and sys.stdin.isatty():
            try:
                password = getpass.getpass(
                    "Arq backup encryption password: "
                )
            except (KeyboardInterrupt, EOFError):
                print("\naborted", file=sys.stderr)
                return 2
        if not password:
            print(
                "error: encryption password required for "
                f"tier={tier.value}; pass --password / "
                "--password-file / --password-env, or run from a TTY.",
                file=sys.stderr,
            )
            return 2

    callback = _make_callback(args)

    report = validate(
        backend,
        tier=tier,
        root="/",
        encryption_password=password,
        sample_fraction=args.sample_fraction,
        audit_skip_larger_than=(
            args.audit_skip_larger_than
            if args.audit_skip_larger_than > 0 else None
        ),
        audit_max_runtime_sec=(
            args.audit_max_runtime_sec
            if args.audit_max_runtime_sec > 0 else None
        ),
        audit_max_bytes=(
            args.audit_max_bytes if args.audit_max_bytes > 0 else None
        ),
        openssl_path=args.openssl_path,
        callback=callback,
    )

    print(json.dumps(asdict(report), indent=2, ensure_ascii=False, default=str))
    return _exit_code_for(report)


if __name__ == "__main__":
    sys.exit(main())
