"""C3 — maxPackedItemLength boundary precision (strict ``>``).

The writer's routing decision at
``arq_writer/backup.py::_write_blob`` line ~856 uses strict
``>`` (not ``>=``):

    elif self.large_blob_threshold > 0 and len(arqo) > threshold:
        # → largeblobpacks
    else:
        # → blobpacks

This means a blob whose ARQO length equals the threshold EXACTLY
lands in ``blobpacks/``, not ``largeblobpacks/``. C1 already
covered the bulk routing direction with 16-KB-clear buffers; C3
narrows in on the 1-byte boundary.

Since we can't easily produce an ARQO of exactly N bytes (AES-
CBC padding adjusts in 1..16-byte steps, LZ4 frame overhead is
content-dependent), C3 instead varies the **threshold** itself
to land a known ARQO either side of the boundary:

- Backup one file → measure its actual ARQO size on disk
- Build a fresh backup with threshold = ARQO_size - 1 →
  routes to largeblobpacks (ARQO > threshold)
- Build a fresh backup with threshold = ARQO_size →
  routes to blobpacks (ARQO == threshold, strict >)
- Build a fresh backup with threshold = ARQO_size + 1 →
  routes to blobpacks (ARQO < threshold)

Three writer runs verify the comparison's strictness with
single-byte precision.
"""

from __future__ import annotations

import secrets
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
class MaxPackedItemLengthBoundaryTests(unittest.TestCase):

    def _build_and_get_arqo_size(self, td: Path, threshold: int):
        """Build a packed backup with the given large_blob_threshold;
        return (bp_files, lbp_files, largest_individual_arqo_size).
        The third value comes from scanning ARQO boundaries inside
        the pack files — pack files concatenate multiple ARQOs so
        ``stat().st_size`` overcounts."""
        from arq_writer.backup import build_backup
        from arq_reader.pack import reconstruct_index
        src = td / "src"
        src.mkdir()
        # Use deterministic random bytes so LZ4 doesn't compress
        # (predictable ARQO size across runs of this test).
        import random as _rand
        (src / "f.bin").write_bytes(
            _rand.Random(20260511).randbytes(8192),
        )
        dest = td / "dest"
        res = build_backup(
            str(src), str(dest),
            encryption_password="pw",
            use_packs=True,
            large_blob_threshold=threshold,
        )
        cu = res.computer_uuid
        bp_dir = dest / cu / "blobpacks"
        lbp_dir = dest / cu / "largeblobpacks"
        bp_files = (
            list(bp_dir.rglob("*.pack")) if bp_dir.is_dir() else []
        )
        lbp_files = (
            list(lbp_dir.rglob("*.pack"))
            if lbp_dir.is_dir() else []
        )
        # Find the largest individual ARQO across all packs.
        largest_arqo = 0
        for pack_path in bp_files + lbp_files:
            entries = reconstruct_index(pack_path.read_bytes())
            for e in entries:
                if e.length > largest_arqo:
                    largest_arqo = e.length
        return bp_files, lbp_files, largest_arqo

    def test_arqo_size_measurement_is_deterministic(self) -> None:
        """A보완-5: the random.Random(seed).randbytes() + AES-CBC
        padding + ARQO header sizing chain is fully deterministic.
        Two independent measurements with the same threshold MUST
        produce identical arqo_size — otherwise the threshold-
        based tests below are unreliable.

        Pre-A보완-5 these tests assumed stability without pinning
        it; if a future refactor introduced non-determinism (e.g.
        randomised tree timestamps), the boundary tests would
        flake. This test makes the determinism explicit."""
        with tempfile.TemporaryDirectory() as td:
            _, _, size_a = self._build_and_get_arqo_size(
                Path(td), 10_000_000,
            )
        with tempfile.TemporaryDirectory() as td:
            _, _, size_b = self._build_and_get_arqo_size(
                Path(td), 10_000_000,
            )
        self.assertEqual(
            size_a, size_b,
            f"arqo size measurement drifted: run-1={size_a} "
            f"vs run-2={size_b}. The seed/padding/header chain "
            f"must be deterministic for the boundary tests to "
            f"be reliable.",
        )

    def test_threshold_equals_arqo_size_routes_to_blobpacks(
        self,
    ) -> None:
        """When ARQO size == threshold, ``len(arqo) > threshold``
        is False → blobpacks. Pin the strict-greater behaviour."""
        with tempfile.TemporaryDirectory() as td:
            # First run with very high threshold to measure ARQO size.
            _, _, arqo_size = self._build_and_get_arqo_size(
                Path(td), 10_000_000,
            )
            self.assertGreater(arqo_size, 0)

        # Now run with threshold = arqo_size exactly. Expected:
        # data blob's ARQO is == threshold → strict-greater False
        # → routes to blobpacks.
        with tempfile.TemporaryDirectory() as td:
            _, lbp2, _ = self._build_and_get_arqo_size(
                Path(td), arqo_size,
            )
            self.assertEqual(
                len(lbp2), 0,
                f"largest ARQO == threshold {arqo_size}: "
                f"strict-greater should route to blobpacks "
                f"(got {len(lbp2)} largeblobpacks)",
            )

    def test_threshold_one_below_arqo_size_routes_to_largeblobpacks(
        self,
    ) -> None:
        """When ARQO size > threshold (by 1 byte), routes to
        largeblobpacks."""
        with tempfile.TemporaryDirectory() as td:
            _, _, arqo_size = self._build_and_get_arqo_size(
                Path(td), 10_000_000,
            )

        with tempfile.TemporaryDirectory() as td:
            _, lbp2, _ = self._build_and_get_arqo_size(
                Path(td), arqo_size - 1,
            )
            self.assertGreater(
                len(lbp2), 0,
                f"largest ARQO = threshold+1: expected "
                f"largeblobpacks routing, got 0 largeblobpacks",
            )

    def test_zero_threshold_disables_largeblobpacks_routing(
        self,
    ) -> None:
        """``large_blob_threshold=0`` disables the largeblobpacks
        path entirely — everything goes to blobpacks regardless
        of size."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            from arq_writer.backup import build_backup
            src = tdp / "src"
            src.mkdir()
            (src / "big.bin").write_bytes(secrets.token_bytes(300_000))
            dest = tdp / "dest"
            res = build_backup(
                str(src), str(dest),
                encryption_password="pw",
                use_packs=True,
                large_blob_threshold=0,
            )
            cu = res.computer_uuid
            lbp_dir = dest / cu / "largeblobpacks"
            # Either the dir doesn't exist OR it's empty — both
            # are acceptable evidence that no blob got routed
            # there.
            if lbp_dir.is_dir():
                self.assertEqual(
                    list(lbp_dir.rglob("*.pack")), [],
                    "large_blob_threshold=0 should disable "
                    "largeblobpacks routing entirely",
                )


if __name__ == "__main__":
    unittest.main()
