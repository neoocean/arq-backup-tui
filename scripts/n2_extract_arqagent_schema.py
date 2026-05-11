"""N2 — extract Arq.app's local-cache SQLite schema from ArqAgent.

The ArqAgent binary contains literal CREATE TABLE / SELECT
statements that describe Arq.app's **local SQLite mirror** of
destination pack contents. Arq.app's reader rebuilds this
mirror from the destination on first walk — so a destination
whose pack-file metadata can't round-trip through that schema
would silently break Arq.app's reader.

This script extracts the schema strings from the binary and
emits them to ``docs/N2-arqagent-schema.sql`` as a reference
artifact. Operator can re-run after each Arq.app upgrade to
detect schema changes.

Read-only on the binary; no network, no Arq.app launch, no
operator state touched.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path


DEFAULT_BINARY = Path(
    "/Applications/Arq.app/Contents/Resources/"
    "ArqAgent.app/Contents/MacOS/ArqAgent"
)


def extract_create_statements(binary: Path) -> list[str]:
    """Return every CREATE TABLE / CREATE INDEX statement
    visible as a raw string in the Mach-O binary."""
    proc = subprocess.run(
        ["strings", str(binary)],
        capture_output=True, check=True, text=True, timeout=60,
    )
    stmts: list[str] = []
    for line in proc.stdout.splitlines():
        if re.match(
            r"^CREATE (TABLE|INDEX|TRIGGER|VIEW)\b",
            line, re.IGNORECASE,
        ):
            # Normalize repeated whitespace; the binary often
            # has many spaces between fields.
            normalized = re.sub(r"\s+", " ", line).strip()
            stmts.append(normalized)
    return stmts


def extract_select_statements(binary: Path) -> list[str]:
    proc = subprocess.run(
        ["strings", str(binary)],
        capture_output=True, check=True, text=True, timeout=60,
    )
    stmts: list[str] = []
    for line in proc.stdout.splitlines():
        if re.match(r"^SELECT\b", line, re.IGNORECASE):
            if "packed_blobs" in line or "pack_files" in line:
                stmts.append(re.sub(r"\s+", " ", line).strip())
    return stmts


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--binary", default=str(DEFAULT_BINARY), type=Path,
    )
    p.add_argument("--out", type=Path)
    args = p.parse_args(argv)

    if not args.binary.is_file():
        print(
            f"ArqAgent binary not found at {args.binary}",
            file=sys.stderr,
        )
        return 1

    creates = extract_create_statements(args.binary)
    selects = extract_select_statements(args.binary)

    out_lines: list[str] = [
        "-- N2: Arq.app local-cache SQLite schema (extracted)",
        "-- Source: /Applications/Arq.app/Contents/Resources/"
        "ArqAgent.app/Contents/MacOS/ArqAgent",
        "-- Each statement is a raw string visible in the Mach-O",
        "-- binary. Extracted via ``strings`` + regex; do not",
        "-- run this as a live schema (column orderings may",
        "-- differ from Arq.app's runtime CREATE because of",
        "-- whitespace normalisation).",
        "",
        f"-- {len(creates)} CREATE statements found",
        "",
    ]
    for s in creates:
        out_lines.append(s + ";")
    out_lines.append("")
    out_lines.append(
        f"-- {len(selects)} SELECT/UPDATE statements touching "
        "pack_files / packed_blobs"
    )
    out_lines.append("")
    for s in selects:
        out_lines.append(f"-- {s}")

    output = "\n".join(out_lines) + "\n"
    if args.out:
        args.out.write_text(output)
        print(
            f"wrote {len(creates)} CREATEs + {len(selects)} "
            f"SELECTs to {args.out}"
        )
    else:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
