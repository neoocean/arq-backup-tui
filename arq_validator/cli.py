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

    # SFTP destination instead of a local mirror
    python -m arq_validator quick \\
        --sftp user@host:22:/home/<uuid>-root \\
        --sftp-password-env ARQ_SFTP_PW

    # Resumable nightly L2 audit (30 min budget per fire)
    python -m arq_validator audit-drip /path/to/backup \\
        --target local --password-file ~/.arq.pw \\
        --state-file ~/.arq-drip-local.json \\
        --max-runtime-sec 1800

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
from typing import List, Optional, Tuple

from .audit_drip import AuditDripState, run_audit_drip
from .backend import Backend, LocalBackend
from .events import Event, EventKind
from .runner import ValidationReport, ValidationTier, validate
from .sftp import SftpBackend, SftpConnectionError
from .tiers import AUDIT_DEFAULT_SKIP_LARGER_THAN


SFTP_TIERS = {t.value for t in ValidationTier} | {"audit-drip"}


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
        choices=[*[t.value for t in ValidationTier],
                 "audit-drip", "record"],
        help=(
            "Validation tier (cheaper -> deeper). 'record' walks "
            "every BlobLoc reachable from a single backuprecord; "
            "pair it with --record-path."
        ),
    )
    p.add_argument(
        "--record-path",
        default=None,
        help=(
            "[record tier only] Absolute server-side path of the "
            "backuprecord file to validate."
        ),
    )
    p.add_argument(
        "--record-max-blobs",
        type=int, default=0,
        help=(
            "[record tier only] Cap the walk after this many blobs "
            "(0 = unbounded). Useful for CI smoke runs."
        ),
    )
    p.add_argument(
        "path",
        nargs="?",
        type=Path,
        default=None,
        help=("Path to the Arq backup destination root (local mode). "
              "Pass --sftp <spec> instead for SFTP."),
    )
    # Password / encryption-password options.
    p.add_argument(
        "--password",
        default=None,
        help=("Encryption password (deep / audit / audit-drip). If "
              "omitted, and stdin is a TTY, you'll be prompted."),
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
    # SFTP options.
    p.add_argument(
        "--sftp",
        default=None,
        help=("SFTP destination as user@host[:port]:/root. When set, "
              "the positional path argument is ignored."),
    )
    p.add_argument(
        "--sftp-password",
        default=None,
        help="SSH password (forces password auth via SSH_ASKPASS).",
    )
    p.add_argument(
        "--sftp-password-env",
        default=None,
        help="Env var holding the SSH password.",
    )
    p.add_argument(
        "--sftp-password-file",
        default=None,
        type=Path,
        help="File containing the SSH password.",
    )
    p.add_argument(
        "--sftp-identity-file",
        default=None,
        type=Path,
        help="SSH private key for key-based auth.",
    )
    p.add_argument(
        "--sftp-known-hosts",
        default=None,
        type=Path,
        help="Custom known_hosts file.",
    )
    # Tier-specific knobs.
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
        help=("audit / audit-drip only — skip files larger than this "
              "many bytes. 0 disables the skip. Default "
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
    # Audit-drip specific.
    p.add_argument(
        "--target",
        default=None,
        help=("audit-drip only — free-form label (e.g. 'local', "
              "'hetzner') used in event payloads + state filename."),
    )
    p.add_argument(
        "--state-file",
        default=None,
        type=Path,
        help=("audit-drip only — JSON state file path. Defaults to "
              "./arq_audit_drip_<target>.json."),
    )
    p.add_argument(
        "--max-runtime-sec",
        default=0,
        type=int,
        help="audit-drip only — soft cap per fire. 0 = run to completion.",
    )
    p.add_argument(
        "--rate-files-per-min",
        default=None,
        type=float,
        help=("audit-drip only — throttle target. None / 0 = no "
              "throttle. Use ~35 for Hetzner-style remotes."),
    )
    # Connection / output knobs.
    p.add_argument(
        "--connect-timeout-sec",
        default=30,
        type=int,
        help="SFTP connect timeout.",
    )
    p.add_argument(
        "--op-timeout-sec",
        default=60,
        type=int,
        help="SFTP per-operation timeout.",
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
    p.add_argument(
        "--debug", default=None, nargs="?", const="all",
        metavar="SUBSYSTEMS",
        help=(
            "Verbose debug logging. Pass alone for all "
            "subsystems, or comma-separated names "
            "(sftp,blob,tree,cli,backend,crypto). Goes to "
            "stderr."
        ),
    )
    return p


def _resolve_password(
    direct: Optional[str],
    env_var: Optional[str],
    file_path: Optional[Path],
) -> Optional[str]:
    if direct is not None:
        return direct
    if env_var:
        v = os.environ.get(env_var, "")
        if v:
            return v
    if file_path:
        try:
            return file_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            print(
                f"error: failed to read password file: {exc}",
                file=sys.stderr,
            )
            return None
    return None


def _parse_sftp_spec(spec: str) -> Tuple[str, str, int, str]:
    """Parse ``user@host[:port]:/root`` into ``(user, host, port, root)``."""
    if "@" not in spec:
        raise ValueError("sftp spec must contain '@'")
    user, _, hostpart = spec.partition("@")
    if ":" not in hostpart:
        raise ValueError("sftp spec must contain ':<root>'")
    head, _, root = hostpart.partition(":")
    if root and not root.startswith("/"):
        # Format: user@host:port:/root
        port_str, _, root2 = root.partition(":")
        if root2 and root2.startswith("/"):
            try:
                port = int(port_str)
            except ValueError as exc:
                raise ValueError(
                    f"sftp spec port not an integer: {port_str!r}"
                ) from exc
            host = head
            root = root2
        else:
            raise ValueError(
                "sftp spec root must be absolute (start with '/')"
            )
    else:
        host = head
        port = 22
    if not user or not host or not root:
        raise ValueError("sftp spec missing user / host / root")
    return user, host, port, root


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
        # Render most events; skip per-file VERIFIED to keep stderr quiet.
        if event.kind is EventKind.AUDIT_FILE_VERIFIED:
            return
        print(f"[{event.kind.value}] {event.message}", file=sys.stderr)

    return cb


def _exit_code_for(report: ValidationReport) -> int:
    if report.error and any(
        s in report.error
        for s in ("FileNotFoundError", "NotADirectoryError",
                  "PermissionError", "IsADirectoryError",
                  "SftpConnectionError", "ConnectionError")
    ):
        return 3
    if report.error:
        return 4 if "encryption_password" not in report.error else 2
    return 4 if report.has_failures() else 0


def _exit_code_for_drip(state: AuditDripState) -> int:
    if state.error:
        if "ConnectionError" in state.error or "Sftp" in state.error:
            return 3
        return 4
    if state.fails_this_sweep or state.errors_this_sweep:
        return 4
    return 0


def _open_backend(
    args: argparse.Namespace,
) -> Tuple[Optional[Backend], Optional[str], int]:
    """Return ``(backend, root, return_code)``.

    ``return_code != 0`` indicates an early-exit error already printed
    to stderr. ``backend`` is wrapped: caller must use ``with`` or
    call ``close()`` to release SFTP resources.
    """
    if args.sftp:
        try:
            user, host, port, root = _parse_sftp_spec(args.sftp)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return None, None, 2
        password = _resolve_password(
            args.sftp_password, args.sftp_password_env,
            args.sftp_password_file,
        )
        try:
            backend = SftpBackend(
                host=host, port=port, user=user,
                password=password,
                identity_file=args.sftp_identity_file,
                known_hosts_file=args.sftp_known_hosts,
                connect_timeout_sec=args.connect_timeout_sec,
                op_timeout_sec=args.op_timeout_sec,
            )
            backend.__enter__()
        except SftpConnectionError as exc:
            print(f"error: SFTP connection failed: {exc}", file=sys.stderr)
            return None, None, 3
        return backend, root, 0
    if args.path is None:
        print(
            "error: provide a positional path or --sftp <spec>",
            file=sys.stderr,
        )
        return None, None, 2
    try:
        return LocalBackend(args.path), "/", 0
    except (FileNotFoundError, NotADirectoryError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None, None, 2


def _close_backend(backend: Backend) -> None:
    if isinstance(backend, SftpBackend):
        backend.close()


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    if getattr(args, "debug", None) is not None:
        from .debug_logging import (
            enable_debug_logging, parse_debug_flag,
        )
        enable_debug_logging(
            subsystems=parse_debug_flag(args.debug),
        )
    is_drip = args.tier == "audit-drip"
    is_record = args.tier == "record"
    tier = (
        None if (is_drip or is_record)
        else ValidationTier(args.tier)
    )

    backend, root, rc = _open_backend(args)
    if rc != 0:
        return rc
    assert backend is not None and root is not None

    needs_password = is_drip or is_record or tier in (
        ValidationTier.DEEP, ValidationTier.AUDIT,
    )
    password: Optional[str] = None
    if needs_password:
        password = _resolve_password(
            args.password, args.password_env, args.password_file,
        )
        if password is None and sys.stdin.isatty():
            try:
                password = getpass.getpass(
                    "Arq backup encryption password: "
                )
            except (KeyboardInterrupt, EOFError):
                print("\naborted", file=sys.stderr)
                _close_backend(backend)
                return 2
        if not password:
            print(
                "error: encryption password required; pass "
                "--password / --password-file / --password-env, "
                "or run from a TTY.",
                file=sys.stderr,
            )
            _close_backend(backend)
            return 2

    callback = _make_callback(args)

    try:
        if is_drip:
            target = args.target or "default"
            state_file = args.state_file or Path(
                f"./arq_audit_drip_{target}.json"
            )
            state = run_audit_drip(
                backend,
                target=target,
                state_file=state_file,
                encryption_password=password or "",
                root=root,
                max_runtime_sec=args.max_runtime_sec,
                rate_files_per_min=args.rate_files_per_min,
                skip_larger_than=(
                    args.audit_skip_larger_than
                    if args.audit_skip_larger_than > 0 else None
                ),
                openssl_path=args.openssl_path,
                callback=callback,
            )
            print(json.dumps(asdict(state), indent=2,
                              ensure_ascii=False, default=str))
            return _exit_code_for_drip(state)

        if is_record:
            if not args.record_path:
                print(
                    "error: --record-path is required for the "
                    "'record' tier",
                    file=sys.stderr,
                )
                return 2
            from .record_validator import validate_record
            report = validate_record(
                backend, args.record_path,
                encryption_password=password or "",
                openssl_path=args.openssl_path,
                max_blobs=args.record_max_blobs,
                callback=callback,
            )
            print(json.dumps(asdict(report), indent=2,
                             ensure_ascii=False, default=str))
            return 0 if report.ok else 4

        report = validate(
            backend,
            tier=tier,
            root=root,
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
        print(json.dumps(asdict(report), indent=2,
                          ensure_ascii=False, default=str))
        return _exit_code_for(report)
    finally:
        _close_backend(backend)


if __name__ == "__main__":
    sys.exit(main())
