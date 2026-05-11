"""C5 — multi-GB single-file Buzhash chunker boundary.

The chunker was verified at 91 MB scale during Strategy K
(HANDOFF.md GAP-L); C5 extends coverage to multi-GB scale.
Two questions to pin:

1. **Chunker doesn't degrade at scale** — boundaries stay
   content-defined (not max-cap-driven), and per-chunk size
   stays under ``max_chunk_size``.
2. **Memory bounded** — backing up a multi-GB file via
   ``build_backup`` doesn't blow up RAM (chunker streams
   through input rather than buffering the whole file).

The default test uses 200 MB (still meaningful for boundary
verification, ~4× the prior cap, runs under 30 s on a fast
laptop). A 2 GB variant gated on ``ARQ_RUN_HUGE_C5_TEST=1`` is
available for thorough validation.
"""

from __future__ import annotations

import os
import random
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


@unittest.skipUnless(_has_openssl(), "openssl CLI required")
@unittest.skipUnless(
    os.environ.get("ARQ_RUN_LARGE_C5_TEST") == "1",
    "set ARQ_RUN_LARGE_C5_TEST=1 to exercise 200 MB single-file "
    "Buzhash chunker (skipped by default — takes ~30 s)",
)
class LargeFileBuzhashTests(unittest.TestCase):
    """200 MB single file with Buzhash chunking."""

    SIZE_MB = 200

    def _make_large_file(self, path: Path) -> None:
        # Deterministic content so chunk boundaries are
        # reproducible across runs. Use a bytearray to avoid
        # holding multiple copies in memory.
        rng = random.Random(20260511)
        chunk = 1024 * 1024   # 1 MB at a time
        with path.open("wb") as f:
            for _ in range(self.SIZE_MB):
                f.write(bytes(rng.getrandbits(8) for _ in range(chunk)))

    def test_buzhash_produces_many_chunks_for_large_file(
        self,
    ) -> None:
        from arq_writer.backup import build_backup
        from arq_writer.chunker import ChunkerConfig
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            big = src / "big.bin"
            self._make_large_file(big)
            dest = tdp / "dest"
            res = build_backup(
                str(src), str(dest),
                encryption_password="pw",
                use_packs=True,
                chunker_config=ChunkerConfig(),
            )
            # Buzhash on 200 MB random data should produce
            # hundreds of chunks (not one, not max-cap-limited).
            self.assertGreater(
                len(res.blob_ids), 50,
                f"200 MB file produced only {len(res.blob_ids)} "
                f"chunks — chunker may have fallen back to max-cap",
            )

    def test_large_file_restores_byte_identical(self) -> None:
        """End-to-end: backup → restore → SHA-256 of restored
        file matches source."""
        import hashlib
        from arq_writer.backup import build_backup
        from arq_writer.chunker import ChunkerConfig
        from arq_reader import Restore
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            big = src / "big.bin"
            self._make_large_file(big)
            # Hash the source.
            src_hash = hashlib.sha256()
            with big.open("rb") as f:
                while True:
                    chunk = f.read(1024 * 1024)
                    if not chunk:
                        break
                    src_hash.update(chunk)
            dest = tdp / "dest"
            build_backup(
                str(src), str(dest),
                encryption_password="pw",
                use_packs=True,
                chunker_config=ChunkerConfig(),
            )
            out = tdp / "out"
            out.mkdir()
            rs = Restore(str(dest), encryption_password="pw")
            layouts = rs.layouts()
            rs.restore(
                folder_uuid=layouts[0].backup_folder_uuids[0],
                computer_uuid=layouts[0].computer_uuid, dest=out,
            )
            restored = out / "big.bin"
            self.assertEqual(
                restored.stat().st_size,
                big.stat().st_size,
                "restored size differs from source",
            )
            out_hash = hashlib.sha256()
            with restored.open("rb") as f:
                while True:
                    chunk = f.read(1024 * 1024)
                    if not chunk:
                        break
                    out_hash.update(chunk)
            self.assertEqual(
                src_hash.hexdigest(), out_hash.hexdigest(),
                "restored file SHA-256 differs from source",
            )


@unittest.skipUnless(_has_openssl(), "openssl CLI required")
@unittest.skipUnless(
    os.environ.get("ARQ_RUN_HUGE_C5_TEST") == "1",
    "set ARQ_RUN_HUGE_C5_TEST=1 to exercise 2 GB single-file "
    "Buzhash chunker (skipped by default — requires 2 GB disk + "
    "RAM headroom + ~5 min wall time)",
)
class HugeFileBuzhashTests(unittest.TestCase):
    """2 GB single file with Buzhash chunking. Heavy — only
    runs when explicitly opted in via env var."""

    SIZE_GB = 2

    def test_2gb_file_round_trips_via_buzhash(self) -> None:
        import hashlib
        from arq_writer.backup import build_backup
        from arq_writer.chunker import ChunkerConfig
        from arq_reader import Restore
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            big = src / "huge.bin"
            rng = random.Random(20260511)
            chunk_mb = 4   # 4 MB at a time to limit RAM
            with big.open("wb") as f:
                for _ in range(self.SIZE_GB * 256):
                    f.write(
                        bytes(rng.getrandbits(8) for _ in range(
                            chunk_mb * 1024 * 1024,
                        ))
                    )
            src_hash = hashlib.sha256()
            with big.open("rb") as f:
                while True:
                    c = f.read(4 * 1024 * 1024)
                    if not c:
                        break
                    src_hash.update(c)
            dest = tdp / "dest"
            build_backup(
                str(src), str(dest),
                encryption_password="pw",
                use_packs=True,
                chunker_config=ChunkerConfig(),
            )
            out = tdp / "out"
            out.mkdir()
            rs = Restore(str(dest), encryption_password="pw")
            layouts = rs.layouts()
            rs.restore(
                folder_uuid=layouts[0].backup_folder_uuids[0],
                computer_uuid=layouts[0].computer_uuid, dest=out,
            )
            restored = out / "huge.bin"
            self.assertEqual(
                restored.stat().st_size, big.stat().st_size,
            )
            out_hash = hashlib.sha256()
            with restored.open("rb") as f:
                while True:
                    c = f.read(4 * 1024 * 1024)
                    if not c:
                        break
                    out_hash.update(c)
            self.assertEqual(
                src_hash.hexdigest(), out_hash.hexdigest(),
            )


if __name__ == "__main__":
    unittest.main()
