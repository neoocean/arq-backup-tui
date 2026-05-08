"""Fixture generator for the Unicode / multi-language / emoji /
long-path stress suite.

Each helper materializes one challenging tree shape on disk (under
a caller-supplied root) and returns the dict ``{rel_posix_path:
expected_content_bytes}`` so a downstream test can assert exact
byte-for-byte round-trip after backup → restore.

Filenames are emitted via Python's ``Path.write_bytes`` /
``Path.mkdir`` which use the filesystem-native encoding (UTF-8 on
modern Linux + macOS). Some shapes are inherently OS-specific
and the helper skips them via ``pytest.SkipTest``-equivalent (we
use ``unittest.SkipTest``) when the underlying filesystem can't
support them.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from typing import Dict, Iterable


# ---------------------------------------------------------------------------
# Multi-script names, drawn from real-world samples.
# ---------------------------------------------------------------------------

MULTI_SCRIPT_NAMES = (
    # Latin extended
    "café_naïveté.txt",
    "fiancée_façade.txt",
    "résumé.pdf",
    # Korean (Hangul)
    "한글_파일.txt",
    "문서/이력서.txt",
    "사진/가족사진.jpg",
    # Japanese (Hiragana + Katakana + Kanji)
    "日本語ファイル.txt",
    "写真/東京タワー.jpg",
    "音楽/カラオケ.mp3",
    "ひらがな.txt",
    "カタカナ.txt",
    # Chinese (Simplified + Traditional)
    "简体中文.txt",
    "繁體中文.txt",
    "文件夹/数据.csv",
    # Arabic (RTL)
    "مرحبا.txt",
    "مجلد/ملف.txt",
    # Hebrew (RTL)
    "שלום.txt",
    "תיקייה/קובץ.txt",
    # Greek
    "ελληνικά.txt",
    # Cyrillic
    "русский.txt",
    "Москва/Кремль.png",
    # Thai
    "ไทย.txt",
    "เอกสาร/บันทึก.txt",
    # Devanagari (Hindi)
    "हिन्दी.txt",
    # Vietnamese (Latin + tone marks)
    "tiếng_việt.txt",
    "Hà_Nội.png",
)


# ---------------------------------------------------------------------------
# Emoji + ZWJ sequences (U+200D zero-width joiner builds compounds).
# ---------------------------------------------------------------------------

EMOJI_NAMES = (
    "🎵_song.mp3",
    "📁folder",
    "🚀_launch.txt",
    # ZWJ sequence: family with multiple skin tones
    "👨‍👩‍👧‍👦_family.png",
    # Single-codepoint emoji + variation selector
    "❤️.txt",
    # Country flag (regional indicators)
    "🇰🇷.txt",
    # Skin-tone modifier
    "👋🏽.txt",
    # Surrogate-pair-heavy
    "🌍_world.json",
)


# ---------------------------------------------------------------------------
# Special characters legal on POSIX filesystems.
# ---------------------------------------------------------------------------

SPECIAL_CHAR_NAMES = (
    # Spaces
    "with spaces.txt",
    "  leading-space.txt",
    "trailing-space  .txt",
    # Multiple dots
    "...dotted....txt",
    "tar.gz.bz2.xz",
    # Dashes / underscores
    "-leading-dash.txt",
    "--double-dash.txt",
    # Brackets / parens / braces
    "(parens).txt",
    "[brackets].txt",
    "{braces}.txt",
    "<angle>.txt",
    # Punctuation
    "quote'single.txt",
    'quote"double.txt',
    "back\\slash.txt",
    "comma,sep.txt",
    "semi;col.txt",
    "amp&rsand.txt",
    "pipe|symbol.txt",
    "qmark?.txt",       # legal on POSIX, illegal on Windows
    "star*.txt",        # same
    "colon:test.txt",   # same; macOS HFS+ disallows
    # Math / currency
    "$dollar.txt",
    "€euro.txt",
    "¥yen.txt",
    # Tab + control-ish chars (only ones safe to put in a name)
    "tab\there.txt",
)


# ---------------------------------------------------------------------------
# Combining-character / normalization fixtures.
# Same visual glyph but different byte sequences.
# ---------------------------------------------------------------------------

NORMALIZATION_NAMES = (
    # NFC: precomposed ñ (U+00F1)
    "español.txt",
    # NFD: n + combining tilde (U+006E + U+0303)
    "español_nfd.txt",   # different filename from above
    # Korean Hangul: precomposed (U+D55C, U+AE00) "한글"
    "한글.txt",
    # Korean Hangul: decomposed (jamo) U+1112 U+1161 U+11AB + U+1100 U+1173 U+11AF
    "한글_jamo.txt",
)


# ---------------------------------------------------------------------------
# Long-name fixtures (just under filesystem limits).
# Linux ext4 / btrfs / xfs: NAME_MAX = 255 bytes.
# ---------------------------------------------------------------------------

# ASCII filename of length 250 (room for ".txt" suffix → 254 bytes)
LONG_ASCII_NAME = "a" * 250 + ".txt"

# Korean filename: each Hangul char is 3 bytes in UTF-8.
# 80 chars × 3 = 240 bytes + ".txt" (4) = 244 bytes (under 255)
LONG_KOREAN_NAME = "한" * 80 + ".txt"

# Emoji filename: 4 bytes per codepoint. 60 emoji × 4 = 240 + ".txt" = 244 bytes
LONG_EMOJI_NAME = "🎵" * 60 + ".txt"


# ---------------------------------------------------------------------------
# Long-path fixtures (deep nesting).
# Linux PATH_MAX = 4096; macOS PATH_MAX = 1024.
# We aim for ~1500 byte total path on Linux to stay safely under both.
# ---------------------------------------------------------------------------

# 30 levels × ~30-byte each + leaf = ~960 bytes — well under macOS limit.
DEEP_DIR_NAME = "deeplynested_"
DEEP_LEVELS = 30


def make_multiscript_tree(root: Path) -> Dict[str, bytes]:
    """Materialize :data:`MULTI_SCRIPT_NAMES` under ``root``.

    Returns the rel_path → expected_content map. ``content`` is a
    fresh per-file payload encoding the path itself, so a
    test can assert ``restored_bytes[i] == expected_content_bytes[i]``
    by simple equality.
    """
    expected: Dict[str, bytes] = {}
    for name in MULTI_SCRIPT_NAMES:
        rel = name
        full = root / rel
        full.parent.mkdir(parents=True, exist_ok=True)
        content = ("content of " + rel).encode("utf-8")
        full.write_bytes(content)
        expected[rel] = content
    return expected


def make_emoji_tree(root: Path) -> Dict[str, bytes]:
    expected: Dict[str, bytes] = {}
    for name in EMOJI_NAMES:
        full = root / name
        full.parent.mkdir(parents=True, exist_ok=True)
        content = ("emoji " + name).encode("utf-8")
        full.write_bytes(content)
        expected[name] = content
    return expected


def make_special_chars_tree(root: Path) -> Dict[str, bytes]:
    """Materialize special-character names that are legal on POSIX
    but exotic on Windows / HFS+.

    Skips individual entries that the host filesystem refuses to
    create (e.g. macOS HFS+ refusing colons). The skip list is
    discovered at runtime.
    """
    expected: Dict[str, bytes] = {}
    for name in SPECIAL_CHAR_NAMES:
        full = root / name
        try:
            full.write_bytes(("special " + name).encode("utf-8"))
        except OSError:
            # Filesystem doesn't accept this character; not our bug.
            continue
        expected[name] = ("special " + name).encode("utf-8")
    return expected


def make_normalization_tree(root: Path) -> Dict[str, bytes]:
    """Place files whose names use combining-character forms.

    HFS+ historically NFD-normalizes filenames, which would
    collapse some of these. APFS preserves bytes; ext4 / btrfs /
    xfs preserve bytes. We keep names that survive on the host
    and skip those that don't.
    """
    expected: Dict[str, bytes] = {}
    for name in NORMALIZATION_NAMES:
        full = root / name
        try:
            full.write_bytes(("norm " + name).encode("utf-8"))
        except OSError:
            continue
        # If two NORMALIZATION_NAMES collapse to the same on-disk
        # name (HFS+), the second write replaces the first; we
        # detect that by checking what's actually on disk.
        if full.is_file():
            expected[name] = ("norm " + name).encode("utf-8")
    return expected


def make_long_name_tree(root: Path) -> Dict[str, bytes]:
    """Files with maximum-length names.

    Skips entries the host filesystem can't accept (NAME_MAX <
    chosen length).
    """
    expected: Dict[str, bytes] = {}
    for name in (LONG_ASCII_NAME, LONG_KOREAN_NAME, LONG_EMOJI_NAME):
        full = root / name
        try:
            full.write_bytes(name.encode("utf-8"))
        except OSError:
            continue
        expected[name] = name.encode("utf-8")
    return expected


def make_deep_path_tree(
    root: Path, *, levels: int = DEEP_LEVELS,
) -> Dict[str, bytes]:
    """Nested-directory tree ``levels`` deep with one leaf file at
    the bottom."""
    cur = root
    rel_parts = []
    for i in range(levels):
        seg = f"{DEEP_DIR_NAME}{i:02d}"
        rel_parts.append(seg)
        cur = cur / seg
        try:
            cur.mkdir(parents=True, exist_ok=True)
        except OSError:
            return {}
    leaf_name = "leaf.txt"
    full_leaf = cur / leaf_name
    rel = "/".join([*rel_parts, leaf_name])
    content = b"buried deep\n"
    try:
        full_leaf.write_bytes(content)
    except OSError:
        return {}
    return {rel: content}


def make_combined_tree(root: Path) -> Dict[str, bytes]:
    """All fixture generators combined under one root.

    Each shape is rooted under a separate top-level subdir so a
    failure in one shape doesn't mask others.
    """
    expected: Dict[str, bytes] = {}
    pairs = [
        ("multiscript", make_multiscript_tree),
        ("emoji", make_emoji_tree),
        ("special-chars", make_special_chars_tree),
        ("normalization", make_normalization_tree),
        ("long-names", make_long_name_tree),
        ("deep-path", make_deep_path_tree),
    ]
    for sub, gen in pairs:
        sub_root = root / sub
        sub_root.mkdir(parents=True, exist_ok=True)
        for rel, content in gen(sub_root).items():
            expected[f"{sub}/{rel}"] = content
    return expected
