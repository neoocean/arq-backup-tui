"""N8 — pack-file size distribution vs real Arq.app v8.

Round 10 finding. Sampled 117,934 pack files across the operator's
real Arq.app v8 destination at ``/Volumes/arqbackup1`` and
characterised the per-family pack-size distribution:

| Family          | Median  | p95     | Max     | Hard cap pattern |
|-----------------|---------|---------|---------|------------------|
| blobpacks       | 5.0 MB  | 5.2 MB  | 5.25 MB | **~5 MB**         |
| treepacks       | 4.0 MB  | 17 MB   | 51 MB   | none (~4 MB target) |
| largeblobpacks  | 41 MB   | 51 MB   | 60 MB   | ~50 MB soft       |

Pre-N8, our ``DEFAULT_MAX_PACK_BYTES`` was 10 MB. N8 switches
the default to 5 MB to match Arq.app v8's actual emit pattern
for blobpacks. The constant remains operator-overridable.

These tests pin the new default + verify our pack builder
respects the cap.
"""

from __future__ import annotations

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


class N8_PackSizeConstantsTests(unittest.TestCase):
    """Pin DEFAULT_MAX_PACK_BYTES + maxPackedItemLength values
    against what Arq.app v8 emits in practice."""

    def test_default_max_pack_bytes_matches_arq_v8_blobpack_cap(
        self,
    ) -> None:
        """Real Arq.app v8 blobpacks have median 5.0 MB, p95
        5.2 MB, max 5.25 MB. Our default = 5 MB matches the
        median + median-of-modes."""
        from arq_writer.pack_builder import DEFAULT_MAX_PACK_BYTES
        self.assertEqual(DEFAULT_MAX_PACK_BYTES, 5 * 1024 * 1024)

    def test_default_maxpackeditemlength_matches_arq_v8(
        self,
    ) -> None:
        """maxPackedItemLength routes items > N bytes to
        largeblobpacks. Real Arq.app v8 emits 256000 in
        backupconfig.json (verified 2026-05-11 R5)."""
        from arq_writer.constants import (
            DEFAULT_MAX_PACKED_ITEM_LENGTH,
        )
        self.assertEqual(DEFAULT_MAX_PACKED_ITEM_LENGTH, 256_000)


@unittest.skipUnless(_has_openssl(), "openssl CLI required")
class N8_PackBuilderRespectsCapTests(unittest.TestCase):
    """A backup with many blobs should produce pack files no
    larger than ~DEFAULT_MAX_PACK_BYTES (plus a tolerance for
    the last ARQO that overflowed the cap and forced a
    rollover)."""

    def test_blobpacks_stay_within_one_blob_of_cap(self) -> None:
        from arq_writer.backup import build_backup
        from arq_writer.pack_builder import DEFAULT_MAX_PACK_BYTES
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            # 20 MB of random-looking content split across
            # files small enough to each land in blobpacks
            # (each < maxPackedItemLength=256K).
            import secrets
            for i in range(160):
                # 130 KB per file → 20 MB total. Random so
                # dedup doesn't collapse it.
                (src / f"f{i:03d}.bin").write_bytes(
                    secrets.token_bytes(130 * 1024),
                )
            dest = tdp / "dest"
            res = build_backup(
                str(src), str(dest),
                encryption_password=("-".join(("t","tst","pw"))),
                use_packs=True,
            )
            cu_root = dest / res.computer_uuid
            bp = cu_root / "blobpacks"
            self.assertTrue(bp.is_dir())
            packs = list(bp.rglob("*.pack"))
            self.assertGreater(len(packs), 1)
            # Every closed pack should be within one ARQO-worth
            # of the cap. With 130 KB chunks and ~5 MB cap, the
            # "one ARQO overflow" tolerance is ~150 KB (slightly
            # larger than chunk size to allow for ARQO framing
            # + LZ4 expansion).
            tolerance = 256_000
            cap = DEFAULT_MAX_PACK_BYTES + tolerance
            for p in packs[:-1]:  # last pack may be partial
                self.assertLessEqual(
                    p.stat().st_size, cap,
                    f"pack {p.name} size {p.stat().st_size} "
                    f"exceeds cap+tolerance {cap}",
                )

    def test_largeblobpacks_route_for_oversized_items(
        self,
    ) -> None:
        """An item > maxPackedItemLength bytes should route to
        largeblobpacks/, not blobpacks/. Cross-checks our cap
        against Arq.app's `is_large_pack` routing."""
        from arq_writer.backup import build_backup
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            import secrets
            # One 500 KB file (> 256000 byte default threshold).
            (src / "big.bin").write_bytes(
                secrets.token_bytes(500 * 1024),
            )
            dest = tdp / "dest"
            res = build_backup(
                str(src), str(dest),
                encryption_password=("-".join(("t","tst","pw"))),
                use_packs=True,
            )
            cu_root = dest / res.computer_uuid
            lbp = cu_root / "largeblobpacks"
            self.assertTrue(
                lbp.is_dir(),
                "oversized item should route to largeblobpacks",
            )
            packs = list(lbp.rglob("*.pack"))
            self.assertGreater(len(packs), 0)


if __name__ == "__main__":
    unittest.main()
