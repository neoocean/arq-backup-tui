#!/usr/bin/env python3
"""verify.py — byte-level v4 verification using two independent readers.

For a chosen Arq.app v8 BackupRecord on a real destination:
  1. Restore a chosen relative path with the patched arq_restore
     (Strategy I-alt — see ``docs/COMPAT-VERIFICATION.md`` §5.8).
  2. Restore the same path with this project's Python reader.
  3. Diff -r the two restore directories byte-for-byte.

Exit code 0 + empty diff = Tree v4 byte-equivalence proven by two
independent implementations of the spec — i.e. an Arq.app-GUI-free
substitute for Strategy I (full GUI restore + diff). The patched
``arq_restore`` plays the role of "an authoritative independent
reader" the project otherwise lacked for Tree v4.

Usage:
    python3 verify.py \\
        --destination /Volumes/arqbackup1 \\
        --password-file .secrets/dest_password \\
        --arq-restore-bin /private/tmp/strategy-c/arq_restore.bin.v4 \\
        --computer-uuid <CU> \\
        --folder-uuid <FU> \\
        --record-name 7647180.backuprecord \\
        --relative-path /data/assets/.../some-dir \\
        --work-dir /tmp/arq-v4-verify
"""
from __future__ import annotations

import argparse
import hashlib
import os
import pty
import select
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def feed_password(password: str, argv: list[str]) -> int:
    """Run ``argv`` in a pty and feed ``password`` to the
    'enter encryption password:' prompt. Same shape as the
    ``run_with_password.py`` helper in /private/tmp/strategy-c."""
    pid, fd = pty.fork()
    if pid == 0:
        os.execvp(argv[0], argv)
    sent_pw = False
    deadline = time.time() + 900  # 15 min restore cap
    output: bytearray = bytearray()
    while True:
        if time.time() > deadline:
            os.write(fd, b"\x03")
            break
        try:
            rlist, _, _ = select.select([fd], [], [], 0.5)
        except OSError:
            break
        if rlist:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            output.extend(chunk)
            if not sent_pw and b"encryption password" in output:
                os.write(fd, password.encode() + b"\n")
                sent_pw = True
    pid_done, status = os.waitpid(pid, 0)
    sys.stderr.write(output.decode("utf-8", "replace"))
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    return -1


def restore_via_arq_restore(
    arq_restore_bin: Path, dest_nick: str, dest_path: Path,
    cu: str, fu: str, password: str, relative_path: str,
    out_dir: Path,
) -> None:
    """Run arq_restore restore in pty mode. The destination must
    already be registered (we add it here if needed)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    # Register target (idempotent — addtarget fails harmlessly if
    # already registered).
    subprocess.run(
        [str(arq_restore_bin), "addtarget", dest_nick, "local", str(dest_path)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        check=False,
    )
    os.chdir(out_dir)
    rc = feed_password(
        password,
        [str(arq_restore_bin), "-l", "error", "restore",
         dest_nick, cu, fu, relative_path],
    )
    if rc != 0:
        raise SystemExit(f"arq_restore exited with code {rc}")


def restore_via_python(
    dest_path: Path, password: str, cu: str, fu: str,
    record_name: str, relative_path: str, out_dir: Path,
) -> None:
    sys.path.insert(0, str(PROJECT_ROOT))
    from arq_reader import Restore
    from arq_validator.backend import LocalBackend
    backend = LocalBackend(str(dest_path))
    rs = Restore(str(dest_path), encryption_password=password, backend=backend)
    # The bucket dir prefix is 5 digits — derive it from the record
    # name's high digits.
    import glob
    matches = glob.glob(
        f"{dest_path}/{cu}/backupfolders/{fu}/backuprecords/*/{record_name}"
    )
    if not matches:
        raise SystemExit(f"record not found: {record_name}")
    rec_full = matches[0]
    rec_rel = rec_full[len(str(dest_path)):]
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    rs.restore(
        backuprecord_path=rec_rel, computer_uuid=cu, folder_uuid=fu,
        dest=str(out_dir), paths=[relative_path],
    )


def diff_dirs(a: Path, b: Path) -> Tuple[int, int, list[Tuple[Path, Path]]]:
    """Walk a + b, return (files_a, files_b, mismatched_pairs).

    A "matched" pair is two files at the same RELATIVE path with
    identical SHA-256. A mismatch keeps the (a_path, b_path) tuple
    for reporting.
    """
    # Build relative-path → absolute mappings.
    def collect(root: Path) -> dict[str, Path]:
        out: dict[str, Path] = {}
        for r, _, files in os.walk(root):
            for fname in files:
                ap = Path(r) / fname
                rel = str(ap.relative_to(root))
                out[rel] = ap
        return out
    map_a = collect(a)
    map_b = collect(b)
    mismatched: list[Tuple[Path, Path]] = []
    shared_keys = set(map_a.keys()) & set(map_b.keys())
    for rel in sorted(shared_keys):
        if sha256_file(map_a[rel]) != sha256_file(map_b[rel]):
            mismatched.append((map_a[rel], map_b[rel]))
    only_a = set(map_a) - set(map_b)
    only_b = set(map_b) - set(map_a)
    for rel in sorted(only_a):
        mismatched.append((map_a[rel], Path("(missing)")))
    for rel in sorted(only_b):
        mismatched.append((Path("(missing)"), map_b[rel]))
    return len(map_a), len(map_b), mismatched


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--destination", required=True, type=Path)
    ap.add_argument("--password-file", required=True, type=Path)
    ap.add_argument("--arq-restore-bin", required=True, type=Path)
    ap.add_argument("--computer-uuid", required=True)
    ap.add_argument("--folder-uuid", required=True)
    ap.add_argument("--record-name", required=True,
                    help="basename of the .backuprecord file")
    ap.add_argument("--relative-path", required=True,
                    help="path inside the backup to restore + verify")
    ap.add_argument("--work-dir", required=True, type=Path)
    ap.add_argument("--dest-nickname", default="arqbackup_v4_verify")
    args = ap.parse_args()

    password = args.password_file.read_text().strip()
    args.work_dir.mkdir(parents=True, exist_ok=True)
    out_arqr = args.work_dir / "arq_restore"
    out_python = args.work_dir / "python"
    if out_arqr.exists():
        shutil.rmtree(out_arqr)
    if out_python.exists():
        shutil.rmtree(out_python)

    print("==> Restoring via patched arq_restore...")
    restore_via_arq_restore(
        args.arq_restore_bin, args.dest_nickname, args.destination,
        args.computer_uuid, args.folder_uuid, password,
        args.relative_path, out_arqr,
    )
    print("==> Restoring via Python reader...")
    restore_via_python(
        args.destination, password, args.computer_uuid, args.folder_uuid,
        args.record_name, args.relative_path, out_python,
    )

    print("==> Diffing...")
    n_a, n_b, mismatches = diff_dirs(out_arqr, out_python)
    print(f"  arq_restore restored: {n_a} files")
    print(f"  python reader: {n_b} files")
    if not mismatches and n_a > 0 and n_a == n_b:
        print(f"\nByte-identical: {n_a}/{n_a} files match SHA-256 between")
        print(f"the patched arq_restore (BSD reference + 3-line v4 patch)")
        print(f"and this project's Python reader.")
        sys.exit(0)
    print(f"\n{len(mismatches)} mismatched files:")
    for a_path, b_path in mismatches[:20]:
        print(f"  - {a_path} vs {b_path}")
    sys.exit(1)


if __name__ == "__main__":
    main()
