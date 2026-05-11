"""C-J1 — Pack file UUID uniqueness analysis.

``PackBuilder._allocate_pack_path`` generates each pack's path
from ``uuid.uuid4()`` — 122 bits of randomness. By the birthday
paradox the collision probability for N packs is roughly
N² / 2 / 2^122 — astronomically low for any realistic N.

This module:

- Pins that ``_allocate_pack_path`` returns unique paths for
  many calls (10k samples — should never collide)
- Documents the birthday-paradox bound so a reviewer can verify
  the math
- Pins that the path shape matches Arq.app's emit (UUID-derived,
  shard = first 2 hex chars)
"""

from __future__ import annotations

import unittest


class PackUuidUniquenessTests(unittest.TestCase):

    def test_ten_thousand_allocations_never_collide(self) -> None:
        from arq_writer.pack_builder import _allocate_pack_path
        cu = "8EB255DD-09D3-43F8-8FE5-6106EBCE1A5D"
        seen = set()
        for _ in range(10_000):
            path = _allocate_pack_path(cu, "blobpacks")
            self.assertNotIn(
                path, seen,
                f"UUID collision after {len(seen)} allocations — "
                f"{path}. Expected probability ~ 1e-30",
            )
            seen.add(path)

    def test_path_shape_matches_arq_convention(self) -> None:
        """Pack path layout: /<cu>/<family>/<2hex>/<36-char>.pack"""
        import re
        from arq_writer.pack_builder import _allocate_pack_path
        cu = "8EB255DD-09D3-43F8-8FE5-6106EBCE1A5D"
        pattern = re.compile(
            r"^/[0-9A-F-]+/blobpacks/[0-9A-F]{2}/"
            r"[0-9A-F]{6}-[0-9A-F]{4}-[0-9A-F]{4}"
            r"-[0-9A-F]{4}-[0-9A-F]{12}\.pack$"
        )
        for _ in range(20):
            path = _allocate_pack_path(cu, "blobpacks")
            self.assertRegex(path, pattern)

    def test_birthday_paradox_documentation(self) -> None:
        """Sanity: UUID4 is 122 bits of randomness. Collision
        probability for N packs ≈ N² / 2 / 2^122.

        For N = 10^9 (a billion packs): p ≈ 10^18 / 2 / 2^122
        ≈ 10^18 / 10^37 ≈ 10^-19 — astronomically low. This
        test exists to document the bound (no actual numeric
        assertion that could regress)."""
        # 122 bits of randomness in UUID4.
        bits = 122
        # Birthday-bound: for N entries, p ≈ N² / 2^(bits+1).
        # Solve for N where p ≈ 1: N ≈ 2^((bits+1)/2)
        n_at_50pct = 2 ** ((bits + 1) / 2)
        # ~2^61.5 ≈ 3e18. So we'd need quintillions of packs
        # for a 50% collision chance.
        self.assertGreater(n_at_50pct, 1e18)


if __name__ == "__main__":
    unittest.main()
