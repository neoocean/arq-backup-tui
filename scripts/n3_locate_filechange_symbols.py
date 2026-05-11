"""N3 — locate FileChangeLasts symbols in the current ArqAgent.

The symbol addresses in ``docs/N3-FILECHANGELASTS-RE.md`` are
stable per-binary but shift across Arq.app upgrades. This
helper prints the current addresses by symbol-name lookup —
operator can re-run after each upgrade.

Read-only on the binary; no network, no Arq.app launch.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


DEFAULT_BINARY = Path(
    "/Applications/Arq.app/Contents/Resources/"
    "ArqAgent.app/Contents/MacOS/ArqAgent"
)

INTERESTING = (
    "FileChangeLasts",
    "[Tree writeToData",
    "[Node writeToData",
    "_lastFullScanDatesById",
    "TreeBackupItem makeTreeNode",
    "TreesPackBuilder",
    "TreeDBLSaver",
    "[BlobLoc writeToData",
    "lastFullScanDateForId",
    "setLastFullScanDate",
)


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--binary", default=str(DEFAULT_BINARY), type=Path,
    )
    args = p.parse_args(argv)
    if not args.binary.is_file():
        print(
            f"binary not found: {args.binary}", file=sys.stderr,
        )
        return 1
    proc = subprocess.run(
        ["nm", "-arch", "arm64", str(args.binary)],
        capture_output=True, check=True, text=True, timeout=60,
    )
    by_kw: dict[str, list[str]] = {kw: [] for kw in INTERESTING}
    for line in proc.stdout.splitlines():
        for kw in INTERESTING:
            if kw in line:
                by_kw[kw].append(line.strip())
    for kw in INTERESTING:
        matches = by_kw[kw]
        if not matches:
            print(f"# {kw}: <none>")
        else:
            print(f"\n# {kw}: {len(matches)} match(es)")
            for m in matches[:6]:
                print(f"  {m}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
