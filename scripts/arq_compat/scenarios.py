"""Scenario corpus generator for the Arq GUI round-trip compatibility suite.

Materialises a fixed-layout tree of "edge case" files under a root so the
same corpus can be (a) backed up by the installed Arq.app GUI and restored
by our reader [Direction B], and (b) backed up by our writer and read back
by Arq.app / patched arq_restore [Direction A]. Each scenario lives in its
own ``<root>/<scenario>/`` subdir so results can be scored per scenario.

Pure stdlib + a few macOS CLI tools (``xattr``, ``ln``); every step that is
platform-specific degrades gracefully (the scenario is marked ``skipped``
with a reason rather than aborting the run) so the corpus is reproducible.

The generator is deterministic given ``seed`` so two runs (e.g. our writer
vs Arq) see byte-identical source content — a prerequisite for byte-level
diffing.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional


@dataclass
class Scenario:
    name: str
    summary: str
    # builds files under ``d`` (the scenario's own dir); returns a note or "".
    build: Callable[[Path], str]
    # platforms the scenario is meaningful on; None = all.
    platforms: Optional[List[str]] = None


def _det_bytes(seed: str, n: int) -> bytes:
    """Deterministic pseudo-random bytes from a seed (SHA-256 stream)."""
    out = bytearray()
    counter = 0
    while len(out) < n:
        out.extend(hashlib.sha256(f"{seed}:{counter}".encode()).digest())
        counter += 1
    return bytes(out[:n])


# --- individual scenario builders ------------------------------------------

def _ascii(d: Path) -> str:
    (d / "hello.txt").write_text("plain ascii content\n")
    (d / "readme.md").write_text("# heading\n\nbody text\n")
    return "2 plain ASCII text files"


def _unicode_nfc(d: Path) -> str:
    import unicodedata
    name = unicodedata.normalize("NFC", "한글-NFC-파일.txt")
    (d / name).write_text("NFC composed name\n")
    (d / "café-ñ-日本語.txt").write_text("mixed unicode content ✅\n")
    return "NFC-composed (precomposed) filenames"


def _unicode_nfd(d: Path) -> str:
    import unicodedata
    name = unicodedata.normalize("NFD", "한글-NFD-파일.txt")
    # write via raw bytes so the on-disk name stays NFD regardless of FS
    p = os.path.join(os.fsencode(str(d)), os.fsencode(name))
    with open(p, "wb") as f:
        f.write(b"NFD decomposed name\n")
    return "NFD-decomposed (jamo) filename"


def _empty(d: Path) -> str:
    (d / "zero-byte.dat").write_bytes(b"")
    (d / "empty.txt").write_bytes(b"")
    return "0-byte files"


def _binary(d: Path) -> str:
    (d / "all-bytes.bin").write_bytes(bytes(range(256)) * 8)
    (d / "rand-4k.bin").write_bytes(_det_bytes("binary", 4096))
    return "binary incl. all 256 byte values + control bytes"


def _large_chunked(d: Path) -> str:
    # > fixed-40m? no — keep it modest but multi-chunk-capable: 12 MB.
    (d / "large-12m.bin").write_bytes(_det_bytes("large", 12 * 1024 * 1024))
    return "12 MB file (exercises packing / multi-blob paths)"


def _large_multichunk(d: Path) -> str:
    # 41,000,000 bytes crosses the fixed-40m boundary (Arq.app useBuzhash=False
    # default): exactly two chunks of 40,000,000 + 1,000,000. Exercises the
    # multi-chunk file path + the GAP-L fixed-chunk boundary end-to-end.
    (d / "large-41mb.bin").write_bytes(_det_bytes("multichunk", 41_000_000))
    return "41 MB file (crosses the 40,000,000-byte fixed-chunk boundary)"


def _nested_deep(d: Path) -> str:
    cur = d
    for i in range(12):
        cur = cur / f"lvl{i:02d}"
        cur.mkdir()
    (cur / "leaf.txt").write_text("deep leaf at depth 12\n")
    return "12-level deep directory nesting"


def _special_names(d: Path) -> str:
    (d / "with spaces.txt").write_text("spaces\n")
    (d / "emoji-😀-name.txt").write_text("emoji\n")
    (d / "dots...and-dashes--.txt").write_text("punct\n")
    (d / ("long-" + "x" * 200 + ".txt")).write_text("long name\n")
    return "spaces / emoji / punctuation / 200-char name"


def _xattr(d: Path) -> str:
    f = d / "with-xattrs.txt"
    f.write_text("file carrying extended attributes\n")
    rc = 0
    for k, v in (("com.example.tag", "alpha"), ("com.example.note", "bravo")):
        rc |= subprocess.run(
            ["xattr", "-w", k, v, str(f)], capture_output=True,
        ).returncode
    if rc:
        return "SKIP: xattr write failed"
    return "file with 2 extended attributes (XAttrSetV002 path)"


def _symlinks(d: Path) -> str:
    (d / "target.txt").write_text("symlink target\n")
    try:
        os.symlink("target.txt", d / "rel-link.txt")
        os.symlink(str((d / "target.txt").resolve()), d / "abs-link.txt")
    except OSError as e:
        return f"SKIP: symlink unsupported ({e})"
    return "relative + absolute symlinks"


def _hardlinks(d: Path) -> str:
    a = d / "hl-a.txt"
    a.write_text("hardlinked content\n")
    try:
        os.link(a, d / "hl-b.txt")
    except OSError as e:
        return f"SKIP: hardlink unsupported ({e})"
    return "two paths sharing one inode (hardlink)"


def _sparse(d: Path) -> str:
    f = d / "sparse.dat"
    try:
        with open(f, "wb") as fh:
            fh.seek(8 * 1024 * 1024)
            fh.write(b"end\n")
    except OSError as e:
        return f"SKIP: sparse create failed ({e})"
    return "sparse file (8 MB hole + tail)"


def _permissions(d: Path) -> str:
    for mode, name in ((0o600, "mode600.txt"), (0o644, "mode644.txt"),
                       (0o755, "mode755.sh"), (0o400, "mode400.txt")):
        p = d / name
        p.write_text(f"mode {oct(mode)}\n")
        os.chmod(p, mode)
    return "varied permission bits (600/644/755/400)"


SCENARIOS: List[Scenario] = [
    Scenario("ascii", "plain ASCII text", _ascii),
    Scenario("unicode_nfc", "NFC filenames", _unicode_nfc),
    Scenario("unicode_nfd", "NFD filenames", _unicode_nfd),
    Scenario("empty", "0-byte files", _empty),
    Scenario("binary", "binary / all byte values", _binary),
    Scenario("large_chunked", "12 MB multi-blob file", _large_chunked),
    Scenario("large_multichunk", "41 MB crosses fixed-40m boundary",
             _large_multichunk),
    Scenario("nested_deep", "deep directory nesting", _nested_deep),
    Scenario("special_names", "spaces/emoji/long names", _special_names),
    Scenario("xattr", "extended attributes", _xattr, platforms=["darwin"]),
    Scenario("symlinks", "relative + absolute symlinks", _symlinks),
    Scenario("hardlinks", "hardlinks", _hardlinks),
    Scenario("sparse", "sparse file", _sparse),
    Scenario("permissions", "varied mode bits", _permissions),
]


def generate(root: Path) -> Dict[str, str]:
    """Build the full corpus under ``root``. Returns {scenario: note}."""
    plat = sys.platform
    notes: Dict[str, str] = {}
    if root.exists():
        import shutil
        shutil.rmtree(root)
    root.mkdir(parents=True)
    for sc in SCENARIOS:
        d = root / sc.name
        d.mkdir()
        if sc.platforms and plat not in sc.platforms:
            notes[sc.name] = f"SKIP: not applicable on {plat}"
            continue
        try:
            notes[sc.name] = sc.build(d) or sc.summary
        except Exception as e:  # noqa: BLE001 - record, don't abort
            notes[sc.name] = f"SKIP: builder error: {e}"
    return notes


if __name__ == "__main__":
    import argparse
    import json
    ap = argparse.ArgumentParser(description="Generate the Arq compat corpus.")
    ap.add_argument("--root", required=True, type=Path)
    args = ap.parse_args()
    n = generate(args.root)
    print(json.dumps(n, ensure_ascii=False, indent=2))
    files = sum(1 for _ in args.root.rglob("*") if _.is_file())
    print(f"\n{len(SCENARIOS)} scenarios, {files} files under {args.root}")
