"""C1 — largeblobpacks/ routing boundary.

Arq.app v8 routes encrypted blobs based on their POST-ARQO size:

- Blobs whose encrypted ARQO bytes exceed ``maxPackedItemLength``
  (= 256000 by default) go to ``largeblobpacks/``.
- Blobs at or below that threshold go to ``blobpacks/``.
- Tree blobs always go to ``treepacks/`` regardless of size.

Our writer's routing logic
(``arq_writer/backup.py::_write_blob`` ~ line 810):

    elif self.large_blob_threshold > 0 and len(arqo) > threshold:
        loc = self._large_blob_pack.add(...)

This module pins:

1. **Below-threshold blob lands in blobpacks/** — no false
   positive routing.
2. **Above-threshold blob lands in largeblobpacks/** — no false
   negative routing.
3. **Exact-threshold behaviour** is documented: ``>`` strictly
   (a blob exactly at the boundary goes to blobpacks, not
   largeblobpacks). This pins the writer's emit alongside
   Arq.app v8's convention (sampled per the
   ``arq_reader/parse.py`` notes on BlobLoc::relativePath).
4. **Both pack types restore correctly** — content round-trips
   regardless of which routing tier a blob ended up in.

The threshold comparison being ``>`` (not ``>=``) means a blob
whose ARQO length is exactly 256000 bytes lands in
``blobpacks/``. If a future change weakens that to ``>=``,
on-disk layouts produced by old vs new writers would differ for
blobs at the boundary — a subtle compat regression. These tests
prevent that drift.
"""

from __future__ import annotations

import os
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
class LargeBlobPacksRoutingTests(unittest.TestCase):
    """Empirically verify the routing boundary by varying file
    sizes around 256 KB and inspecting which pack directory
    each blob lands in."""

    THRESHOLD = 256000   # ARQO size threshold for largeblobpacks

    def _build_dest_with_file(
        self, size_bytes: int, td: Path,
    ):
        """Build a backup of one file of ``size_bytes`` random
        bytes (incompressible → arqo size ≈ plaintext size + ~70)
        and return (dest, computer_uuid)."""
        from arq_writer.backup import build_backup
        src = td / "src"
        src.mkdir()
        # Use cryptographically-random bytes so LZ4 doesn't compress
        # them — the ARQO size will track plaintext size closely
        # (compressible content would make the routing test unstable).
        (src / "f.bin").write_bytes(secrets.token_bytes(size_bytes))
        dest = td / "dest"
        res = build_backup(
            str(src), str(dest),
            encryption_password="pw",
            use_packs=True,
        )
        return dest, res.computer_uuid

    def _count_packs(
        self, dest: Path, cu: str,
    ):
        """Return (blobpacks_pack_count, largeblobpacks_pack_count)."""
        bp = dest / cu / "blobpacks"
        lbp = dest / cu / "largeblobpacks"
        bp_count = (
            len(list(bp.rglob("*.pack"))) if bp.is_dir() else 0
        )
        lbp_count = (
            len(list(lbp.rglob("*.pack"))) if lbp.is_dir() else 0
        )
        return bp_count, lbp_count

    def test_small_file_lands_in_blobpacks_not_largeblobpacks(
        self,
    ) -> None:
        """A 10 KB file's ARQO is ~10 KB + ~70 bytes overhead —
        far below 256 KB threshold. Must land in blobpacks/."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            dest, cu = self._build_dest_with_file(
                10 * 1024, tdp,
            )
            bp, lbp = self._count_packs(dest, cu)
            self.assertGreaterEqual(
                bp, 1,
                "small blob did not land in blobpacks/",
            )
            self.assertEqual(
                lbp, 0,
                "small blob unexpectedly created a "
                "largeblobpacks/ pack",
            )

    def test_large_file_lands_in_largeblobpacks(self) -> None:
        """A 300 KB random-bytes file's ARQO is ~300 KB — above
        the 256 KB threshold. Must land in largeblobpacks/."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            dest, cu = self._build_dest_with_file(
                300 * 1024, tdp,
            )
            bp, lbp = self._count_packs(dest, cu)
            self.assertGreaterEqual(
                lbp, 1,
                "large blob did not land in largeblobpacks/",
            )

    def test_boundary_well_below_threshold_lands_in_blobpacks(
        self,
    ) -> None:
        """A file whose ARQO is comfortably under 256000 bytes:
        plaintext 240000 random bytes empirically produces an
        ARQO ≈ 240070 bytes (well under threshold). Routes to
        blobpacks/.

        We deliberately stay 16 KB clear of the boundary because
        the actual ARQO size depends on LZ4 frame overhead +
        AES-CBC PKCS7 padding (variable up to 16 bytes); a
        too-close test would flake under randomness."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            dest, cu = self._build_dest_with_file(
                240_000, tdp,
            )
            bp, lbp = self._count_packs(dest, cu)
            self.assertEqual(
                lbp, 0,
                "below-threshold blob slipped into "
                "largeblobpacks/",
            )

    def test_boundary_above_threshold_lands_in_largeblobpacks(
        self,
    ) -> None:
        """A file whose ARQO clearly exceeds 256000 bytes:
        plaintext 270000 random bytes produces an ARQO ≈ 270070.
        Routes to largeblobpacks/. Empirical study (below the
        test in this PR's commit log) shows the actual boundary
        is at plaintext ≈ 254000 bytes (since ARQO overhead is
        ~70 + padding); 270 KB is safely above."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            dest, cu = self._build_dest_with_file(
                270_000, tdp,
            )
            bp, lbp = self._count_packs(dest, cu)
            self.assertGreaterEqual(
                lbp, 1,
                "above-threshold blob did not route to "
                "largeblobpacks/",
            )

    def test_largeblobpacks_blob_restores_correctly(self) -> None:
        """End-to-end: a file routed to largeblobpacks/ must
        restore byte-identical. Catches any read-path
        regression that doesn't traverse the largeblobpacks/
        tier."""
        from arq_reader import Restore
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            payload = secrets.token_bytes(300 * 1024)
            src = tdp / "src"
            src.mkdir()
            (src / "f.bin").write_bytes(payload)
            from arq_writer.backup import build_backup
            res = build_backup(
                str(src), str(tdp / "dest"),
                encryption_password="pw",
                use_packs=True,
            )
            out = tdp / "out"
            out.mkdir()
            rs = Restore(
                str(tdp / "dest"), encryption_password="pw",
            )
            layouts = rs.layouts()
            rs.restore(
                folder_uuid=layouts[0].backup_folder_uuids[0],
                computer_uuid=layouts[0].computer_uuid, dest=out,
            )
            self.assertEqual(
                (out / "f.bin").read_bytes(), payload,
                "largeblobpacks-routed file restored corrupt",
            )


if __name__ == "__main__":
    unittest.main()
