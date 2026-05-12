"""V1 — ArqAgent binary fingerprint (cross-version drift detector).

Cross-version diffing requires having TWO ArqAgent binaries
available. In a single-host install only ONE is present at a
time. V1 records a per-version "fingerprint" so the operator
notices when Arq.app upgrades + can do the side-by-side diff
at that moment.

The fingerprint is a small set of derived stats from the
currently-installed binary:
- Build version (parsed from "build X.YZ date" string)
- Total symbol count
- Total string count
- Presence of key format-identifying strings
- SHA-256 of the (sorted) set of public ObjC class names

The accompanying test (``tests/test_v1_arqagent_fingerprint.py``)
pins the current fingerprint. A future Arq.app upgrade
produces a different fingerprint → test fails → operator runs
this script with both binary paths to compute a structured diff.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path


DEFAULT_BINARY = Path(
    "/Applications/Arq.app/Contents/Resources/"
    "ArqAgent.app/Contents/MacOS/ArqAgent"
)


def _strings(binary: Path) -> list[str]:
    proc = subprocess.run(
        ["strings", str(binary)],
        capture_output=True, text=True, timeout=60, check=True,
    )
    return proc.stdout.splitlines()


def _symbols(binary: Path) -> list[str]:
    proc = subprocess.run(
        ["nm", "-arch", "arm64", str(binary)],
        capture_output=True, text=True, timeout=60, check=True,
    )
    return proc.stdout.splitlines()


def fingerprint(binary: Path) -> dict:
    s = _strings(binary)
    syms = _symbols(binary)
    # Parse "build 7.41 date %@" → version "7.41"
    build_version: str | None = None
    for line in s:
        m = re.match(r"build (\d+\.\d+(?:\.\d+)?) date", line)
        if m:
            build_version = m.group(1)
            break
    # ObjC class names (Arq7-prefix + selected core classes).
    class_names: set[str] = set()
    for sym in syms:
        m = re.search(
            r"_OBJC_CLASS_\$_([A-Za-z0-9_]+)$", sym,
        )
        if m:
            name = m.group(1)
            if (
                name.startswith("Arq")
                or name in {
                    "Node", "Tree", "BlobLoc",
                    "FileChangeLasts", "TreeBackupItem",
                    "NodeBackupItem", "TreesPackBuilder",
                    "TreeDBLSaver", "PackSetWriter",
                }
            ):
                class_names.add(name)
    classes_hash = hashlib.sha256(
        "\n".join(sorted(class_names)).encode("utf-8"),
    ).hexdigest()
    # Key identifying strings present in 7.41 — used as
    # version-drift sentinels.
    key_strings = [
        "ARQO", "ARQ_ENCRYPTED_MASTER_KEYS",
        "XAttrSetV002", "nil arqVersion",
        "missing blob identifier for tree",
        "HardLinkQueue", "FileChangeLasts",
    ]
    found_keys = [k for k in key_strings if k in "\n".join(s)]
    return {
        "binary_path": str(binary),
        "build_version": build_version,
        "string_count": len(s),
        "symbol_count": len(syms),
        "objc_class_count": len(class_names),
        "objc_class_names_sha256": classes_hash,
        "objc_class_names": sorted(class_names),
        "key_strings_present_count": len(found_keys),
        "key_strings_missing": [
            k for k in key_strings if k not in found_keys
        ],
    }


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--binary", default=str(DEFAULT_BINARY), type=Path,
    )
    p.add_argument(
        "--other-binary", type=Path, default=None,
        help="Optional second binary; if provided, prints diff "
             "between the two fingerprints.",
    )
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    if not args.binary.is_file():
        print(
            f"binary not found: {args.binary}", file=sys.stderr,
        )
        return 1
    fp1 = fingerprint(args.binary)
    if args.other_binary:
        if not args.other_binary.is_file():
            print(
                f"second binary not found: {args.other_binary}",
                file=sys.stderr,
            )
            return 1
        fp2 = fingerprint(args.other_binary)
        diff = {
            "binary_a": fp1["binary_path"],
            "binary_b": fp2["binary_path"],
            "version_a": fp1["build_version"],
            "version_b": fp2["build_version"],
            "a_only_classes": sorted(
                set(fp1["objc_class_names"])
                - set(fp2["objc_class_names"]),
            ),
            "b_only_classes": sorted(
                set(fp2["objc_class_names"])
                - set(fp1["objc_class_names"]),
            ),
        }
        if args.json:
            print(json.dumps(diff, indent=2))
        else:
            print(f"A: {fp1['binary_path']} ({fp1['build_version']})")
            print(f"B: {fp2['binary_path']} ({fp2['build_version']})")
            print(f"\nA-only classes ({len(diff['a_only_classes'])}):")
            for c in diff["a_only_classes"]:
                print(f"  - {c}")
            print(f"\nB-only classes ({len(diff['b_only_classes'])}):")
            for c in diff["b_only_classes"]:
                print(f"  + {c}")
    else:
        if args.json:
            print(json.dumps(fp1, indent=2))
        else:
            print(f"binary: {fp1['binary_path']}")
            print(f"build:  {fp1['build_version']}")
            print(f"strings: {fp1['string_count']}")
            print(f"symbols: {fp1['symbol_count']}")
            print(
                f"objc classes (Arq*+core): "
                f"{fp1['objc_class_count']} "
                f"(sha256: {fp1['objc_class_names_sha256'][:16]}...)",
            )
            print(
                f"key-string presence: "
                f"{fp1['key_strings_present_count']}/7",
            )
            if fp1["key_strings_missing"]:
                print(f"  missing: {fp1['key_strings_missing']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
