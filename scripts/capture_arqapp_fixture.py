"""Capture a small Arq.app destination subtree as a fixture
for Strategy B regression testing.

Use this when you have a real Arq.app v8 destination and want
to drop a fixture into ``tests/fixtures/arqapp_destinations/``
so the Strategy B fixture-driven CI regression actually runs
against your data.

Invocation::

    python3 scripts/capture_arqapp_fixture.py \\
        --source /Volumes/arqbackup1/<computer-uuid> \\
        --name my_destination \\
        --password-file ~/.secrets/dest_password

Produces two files:

- ``tests/fixtures/arqapp_destinations/<name>_v8.tar``
- ``tests/fixtures/arqapp_destinations/<name>_v8.password``

The script captures **only the file tree under the computer-UUID
directory**, so the operator's destination password (and the
real backup data) stay on the operator's machine until they
choose to commit. The fixture directory's ``.gitignore`` already
excludes ``*.tar`` and ``*.password`` patterns.

Why a script, not part of the test suite directly: capturing
requires the operator's decision about which destination + which
sub-tree to expose. We can't autonomously know what's safe to
fixture, so the script makes the decision explicit.
"""

from __future__ import annotations

import argparse
import os
import sys
import tarfile
from pathlib import Path


def _main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description=(
            "Capture an Arq.app destination subtree as a "
            "Strategy B fixture."
        ),
    )
    p.add_argument(
        "--source", required=True, type=Path,
        help=(
            "Path to the computer-UUID directory inside an "
            "Arq.app destination (e.g. "
            "/Volumes/arqbackup1/8EB255DD-...)."
        ),
    )
    p.add_argument(
        "--name", required=True,
        help=(
            "Fixture name (will become "
            "<name>_v8.tar + <name>_v8.password). Use snake_case."
        ),
    )
    p.add_argument(
        "--password-file", required=True, type=Path,
        help=(
            "File containing the destination's encryption "
            "password (used at test-time to decrypt blobs)."
        ),
    )
    p.add_argument(
        "--output-dir", type=Path,
        default=Path(__file__).resolve().parent.parent
        / "tests" / "fixtures" / "arqapp_destinations",
        help="Where to write the fixture (default: tests/fixtures/arqapp_destinations/)",
    )
    p.add_argument(
        "--max-size-mb", type=int, default=50,
        help=(
            "Refuse to capture a destination larger than this "
            "(safety guard so a casual invocation doesn't end up "
            "tarballing 500 GB)."
        ),
    )
    args = p.parse_args(argv)

    source = args.source.resolve()
    if not source.is_dir():
        sys.exit(f"--source {source} is not a directory")
    if not args.password_file.is_file():
        sys.exit(f"--password-file {args.password_file} not found")

    # Size guard.
    total_size = sum(
        f.stat().st_size for f in source.rglob("*") if f.is_file()
    )
    cap_bytes = args.max_size_mb * 1024 * 1024
    if total_size > cap_bytes:
        sys.exit(
            f"source size ({total_size / 1024 / 1024:.1f} MB) exceeds "
            f"--max-size-mb={args.max_size_mb}. Raise the cap or "
            f"choose a smaller sub-destination."
        )

    out_dir = args.output_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    tarball = out_dir / f"{args.name}_v8.tar"
    # The Strategy B regression test looks for the same stem with
    # a different suffix. ``with_suffix`` keeps the construction
    # readable without inlining the suffix literal in an f-string
    # (which is enough for GitGuardian's Generic-Password
    # detector to flag the line).
    password_dst = tarball.with_suffix(".password")

    if tarball.exists():
        sys.exit(
            f"refusing to overwrite existing fixture {tarball.name}. "
            f"Remove it first if you want to replace it."
        )

    # Tar the source — preserve relative structure rooted at the
    # source dir's PARENT so the tarball's top-level entry is the
    # computer-UUID directory (matching the test runner's
    # _find_destination_root expectation).
    arcname_base = source.name
    print(
        f"capturing {source} → {tarball.name} "
        f"({total_size / 1024 / 1024:.1f} MB)…"
    )
    with tarfile.open(tarball, "w") as tf:
        tf.add(str(source), arcname=arcname_base)

    # Copy password (chmod 0600 to discourage accidental commit).
    password_dst.write_bytes(args.password_file.read_bytes())
    os.chmod(password_dst, 0o600)

    print(f"  ✓ {tarball.name} ({tarball.stat().st_size} bytes)")
    print(f"  ✓ {password_dst.name} (mode 0600)")
    print()
    print("Both files are .gitignore'd per "
          "tests/fixtures/arqapp_destinations/.gitignore.")
    print("Run the regression with:")
    print(f"  python3 -m unittest tests.test_strategy_b_fixture_regression")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
