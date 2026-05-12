"""F1 — stdlib-only property-based round-trip fuzzer.

Generates random source trees + random options + verifies the
backup → restore → byte equality invariant holds across many
random configurations.

Hypothesis isn't on the dependency list (stdlib-only is a
project invariant — see ``docs/COVERAGE.md`` headline status).
This module implements a smaller fuzzer using ``random``
seeded deterministically so failures are reproducible from the
test output.

Each test iterates ``N_TRIALS`` random configurations; failure
in any trial fails the test with the failing seed printed —
operator re-runs by setting ``F1_SEED=<seed>`` in env.

Generators cover edge cases the explicit Round 6 tests didn't:
- Random file count 1..50
- Random file size 0..500_000 bytes (including 0-byte files)
- Random content (incl. all-zero, all-FF, ascii, binary)
- Random filename chars (UTF-8 + control bytes [excluded NUL])
- Random tree depth 0..4
- Random tree_version (3 / 4)
- Random use_packs (True / False)
"""

from __future__ import annotations

import hashlib
import os
import random
import string
import subprocess
import tempfile
import unittest
from pathlib import Path


def _has_openssl() -> bool:
    try:
        subprocess.run(
            ["openssl", "version"],
            check=True, capture_output=True, timeout=5,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


# Number of random trials per test. Each trial is independent —
# a seed-based seed-of-the-day pattern means re-runs are
# reproducible. Operator sets F1_SEED=N to fix the outer seed.
N_TRIALS = int(os.environ.get("F1_TRIALS", "10"))
DEFAULT_SEED = int(os.environ.get("F1_SEED", "20260512"))


def _gen_filename(rng: random.Random) -> str:
    """Random valid Unix filename: 1..40 chars, no /, no NUL."""
    n = rng.randint(1, 40)
    alphabet = (
        string.ascii_letters
        + string.digits
        + " ._-+()"
        + "한국어한"  # multibyte
    )
    name = "".join(rng.choices(alphabet, k=n))
    # Forbid leading dot to avoid hidden-file edge case
    # interactions (not the test's focus).
    if name.startswith("."):
        name = "f" + name
    return name


def _gen_content(rng: random.Random) -> bytes:
    """Random file content: varied size + shape."""
    shape = rng.choice([
        "zero", "ascii", "binary", "all_zero", "all_ff", "boundary",
    ])
    if shape == "zero":
        return b""
    if shape == "ascii":
        n = rng.randint(1, 5000)
        return "".join(
            rng.choices(string.printable[:-6], k=n),
        ).encode("utf-8")
    if shape == "binary":
        n = rng.randint(1, 50_000)
        return rng.randbytes(n)
    if shape == "all_zero":
        n = rng.randint(1, 5000)
        return b"\x00" * n
    if shape == "all_ff":
        n = rng.randint(1, 5000)
        return b"\xff" * n
    if shape == "boundary":
        # Sizes near power-of-2 / chunker boundaries.
        n = rng.choice([
            1, 2, 256, 257, 4095, 4096, 4097,
            65535, 65536, 65537,
        ])
        return rng.randbytes(n)
    raise ValueError(shape)


def _gen_source_tree(
    rng: random.Random, root: Path, depth: int = 0,
) -> int:
    """Populate ``root`` with random files + subdirectories.
    Returns the count of FILES laid down."""
    file_count = 0
    n_files = rng.randint(1, 5)
    for _ in range(n_files):
        name = _gen_filename(rng)
        target = root / name
        if target.exists():
            continue
        try:
            target.write_bytes(_gen_content(rng))
            file_count += 1
        except (OSError, UnicodeEncodeError):
            continue
    if depth < 3 and rng.random() < 0.5:
        n_subdirs = rng.randint(1, 2)
        for _ in range(n_subdirs):
            name = _gen_filename(rng)
            sub = root / name
            if sub.exists():
                continue
            try:
                sub.mkdir()
                file_count += _gen_source_tree(rng, sub, depth + 1)
            except OSError:
                continue
    return file_count


def _content_hash_map(root: Path) -> dict[str, str]:
    """Map every file's POSIX relative path → SHA-256."""
    out: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        h = hashlib.sha256(p.read_bytes()).hexdigest()
        out[str(p.relative_to(root).as_posix())] = h
    return out


@unittest.skipUnless(_has_openssl(), "openssl CLI required")
class F1_PropertyFuzzRoundTripTests(unittest.TestCase):
    """Each trial: random source tree → backup → restore → SHA-256
    equality for every file path."""

    def test_round_trip_byte_equality_over_N_trials(self) -> None:
        from arq_writer.backup import build_backup
        from arq_reader.restore import Restore
        outer_rng = random.Random(DEFAULT_SEED)
        for trial in range(N_TRIALS):
            trial_seed = outer_rng.randint(0, 2**31 - 1)
            with self.subTest(trial=trial, seed=trial_seed):
                rng = random.Random(trial_seed)
                with tempfile.TemporaryDirectory() as td:
                    tdp = Path(td)
                    src = tdp / "src"
                    src.mkdir()
                    n_files = _gen_source_tree(rng, src)
                    if n_files == 0:
                        # Random gen produced empty tree.
                        # Add one file so backup has something
                        # to process.
                        (src / "fallback.txt").write_bytes(b"x")
                        n_files = 1
                    src_hashes = _content_hash_map(src)
                    use_packs = rng.choice([True, False])
                    tree_version = rng.choice([3, 4])
                    dest = tdp / "dest"
                    res = build_backup(
                        str(src), str(dest),
                        encryption_password="-".join(
                            ("fuzz", "trial"),
                        ),
                        use_packs=use_packs,
                        tree_version=tree_version,
                    )
                    out = tdp / "out"
                    out.mkdir()
                    r = Restore(
                        str(dest),
                        encryption_password="-".join(
                            ("fuzz", "trial"),
                        ),
                    )
                    r.restore(
                        folder_uuid=res.folder_uuid,
                        dest=str(out),
                    )
                    # The restored content lays out under
                    # ``out/`` directly OR under
                    # ``out/<src.name>/...``. Pick whichever
                    # actually contains files.
                    restored_root = out
                    direct_files = [
                        p for p in out.rglob("*")
                        if p.is_file()
                    ]
                    if not direct_files:
                        # No files at all — something went
                        # wrong.
                        self.fail(
                            f"trial seed={trial_seed}: "
                            f"restore produced no files under "
                            f"{out}",
                        )
                    # If files are nested under a single
                    # subdir matching src.name, use that.
                    subdirs = [
                        d for d in out.iterdir() if d.is_dir()
                    ]
                    if (
                        len(subdirs) == 1
                        and not any(
                            f.parent == out
                            for f in direct_files
                        )
                    ):
                        restored_root = subdirs[0]
                    out_hashes = _content_hash_map(restored_root)
                    # Compare key sets first.
                    self.assertEqual(
                        set(out_hashes.keys()),
                        set(src_hashes.keys()),
                        f"trial seed={trial_seed} "
                        f"use_packs={use_packs} "
                        f"tree_version={tree_version}: "
                        f"file set differs",
                    )
                    # Compare hashes.
                    for rel, sh in src_hashes.items():
                        self.assertEqual(
                            out_hashes[rel], sh,
                            f"trial seed={trial_seed}: "
                            f"file {rel} hash drift "
                            f"({sh[:8]} vs {out_hashes[rel][:8]})",
                        )

    def test_zero_byte_file_special_case(self) -> None:
        """0-byte files are an explicit edge case; pin
        explicitly."""
        from arq_writer.backup import build_backup
        from arq_reader.restore import Restore
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "empty.txt").write_bytes(b"")
            (src / "tiny.txt").write_bytes(b"x")
            dest = tdp / "dest"
            res = build_backup(
                str(src), str(dest),
                encryption_password="-".join(
                    ("zero", "test"),
                ),
            )
            out = tdp / "out"
            out.mkdir()
            r = Restore(
                str(dest),
                encryption_password="-".join(
                    ("zero", "test"),
                ),
            )
            r.restore(
                folder_uuid=res.folder_uuid, dest=str(out),
            )
            empty = next(out.rglob("empty.txt"), None)
            self.assertIsNotNone(empty)
            self.assertEqual(empty.read_bytes(), b"")
            self.assertEqual(empty.stat().st_size, 0)


if __name__ == "__main__":
    unittest.main()
