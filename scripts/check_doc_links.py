#!/usr/bin/env python3
"""Detect stale references in documentation.

Walks every Markdown file under the repo and pulls out:

- Module / file path references like ``arq_writer/backup.py``,
  ``arq_tui/screens/home.py`` — checks each path actually
  exists on disk.
- Function / class refs like ``arq_writer.backup.Backup``,
  ``arq_validator.crypto.decrypt_keyset`` — verifies the
  module exists + the symbol is defined inside it.

Output: a list of stale references with the file + line where
they appear. Exits non-zero when any stale reference is found,
so a CI step can gate on it.

Usage::

    python3 scripts/check_doc_links.py [--quiet]

The check is intentionally conservative: it only flags
references that look unambiguously code-shaped (matches the
regexes below). Free-form prose mentions of "the writer" or
"PR #21" are ignored.
"""

from __future__ import annotations

import argparse
import importlib
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Set


# Inline-code-fenced refs we treat as path/symbol candidates.
# The regex is conservative — it only matches things that look
# like module paths or file paths. URLs, command flags, and
# free prose stay untouched.
_PATH_RE = re.compile(
    r"`((?:arq_writer|arq_reader|arq_validator|arq_tui|tests|"
    r"scripts|docs)(?:/[\w._-]+)+\.[a-zA-Z]+)`"
)
_SYMBOL_RE = re.compile(
    r"`((?:arq_writer|arq_reader|arq_validator|arq_tui)"
    r"(?:\.[A-Za-z_][\w]*)+)`"
)


@dataclass
class StaleRef:
    file: str
    line: int
    ref: str
    kind: str           # "path" | "symbol"
    reason: str


@dataclass
class CheckResult:
    files_scanned: int = 0
    refs_checked: int = 0
    stale: List[StaleRef] = field(default_factory=list)


def _scan(repo_root: Path) -> CheckResult:
    """Walk every markdown file under ``repo_root`` + return any
    stale references."""
    out = CheckResult()
    for md in sorted(repo_root.rglob("*.md")):
        if any(part.startswith(".") for part in md.parts):
            # Skip dotted dirs like .git, .claude, .venv.
            continue
        if "node_modules" in md.parts:
            continue
        out.files_scanned += 1
        try:
            text = md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for ref in _PATH_RE.findall(line):
                out.refs_checked += 1
                target = repo_root / ref
                if not target.exists():
                    out.stale.append(StaleRef(
                        file=str(md.relative_to(repo_root)),
                        line=lineno, ref=ref, kind="path",
                        reason="path does not exist",
                    ))
            for ref in _SYMBOL_RE.findall(line):
                out.refs_checked += 1
                problem = _check_symbol(ref)
                if problem is not None:
                    out.stale.append(StaleRef(
                        file=str(md.relative_to(repo_root)),
                        line=lineno, ref=ref, kind="symbol",
                        reason=problem,
                    ))
    return out


# Module-level cache so the same module isn't imported once per
# reference (each check_symbol call would otherwise hammer the
# package's own import side effects).
_module_cache: dict = {}


def _check_symbol(symbol_path: str) -> str:
    """Verify that ``symbol_path`` (e.g. ``arq_writer.backup.Backup``)
    resolves to a module + attribute. Returns None when valid,
    else the reason the lookup failed."""
    parts = symbol_path.split(".")
    # Try progressively shorter import paths until one works,
    # then look up the remaining attrs.
    for cut in range(len(parts), 0, -1):
        mod_path = ".".join(parts[:cut])
        try:
            if mod_path in _module_cache:
                mod = _module_cache[mod_path]
            else:
                mod = importlib.import_module(mod_path)
                _module_cache[mod_path] = mod
        except ImportError:
            continue
        # Found the longest importable prefix; resolve the
        # remaining parts as attributes.
        obj = mod
        for attr in parts[cut:]:
            if not hasattr(obj, attr):
                return (
                    f"module {mod_path!r} has no attribute "
                    f"{attr!r}"
                )
            obj = getattr(obj, attr)
        return None      # success
    return f"no importable prefix found for {symbol_path!r}"


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--quiet", action="store_true")
    p.add_argument(
        "--repo-root", type=Path, default=None,
        help="Repository root (default: parent of this script).",
    )
    args = p.parse_args(argv)
    repo_root = (
        args.repo_root
        or Path(__file__).resolve().parent.parent
    )
    sys.path.insert(0, str(repo_root))
    result = _scan(repo_root)
    if not args.quiet:
        print(
            f"scanned {result.files_scanned} markdown files "
            f"with {result.refs_checked} code-shaped references",
            file=sys.stderr,
        )
    if not result.stale:
        if not args.quiet:
            print("no stale references found", file=sys.stderr)
        return 0
    print(f"\n{len(result.stale)} stale reference(s):\n",
          file=sys.stderr)
    for s in result.stale:
        print(
            f"  {s.file}:{s.line}  [{s.kind}]  {s.ref}\n"
            f"    {s.reason}",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    sys.exit(main())
