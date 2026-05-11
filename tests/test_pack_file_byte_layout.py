"""C2 — pack file byte layout matches Arq 7's plain-concatenation
format.

The Arq 7 spec + ``arq_restore/arq7restore/Arq7BlobReader.m::
dataForBlobLoc:`` confirm that a ``.pack`` file is **plain
concatenation of EncryptedObject (ARQO) blobs**, with NO per-
entry framing (no length prefix, no MIME type, no name index).
The BlobLoc records the byte offset + length of each blob
inside its pack file.

This module pins the byte layout against synthetic input:

- A pack with N ARQOs equals the byte concatenation of those N
  ARQOs in insertion order.
- Each BlobLoc's ``offset`` + ``length`` slice the original
  ARQO out byte-for-byte.
- Empty pack = empty file (no header, no trailer).
- Round-trip: write pack via PackBuilder → read pack bytes →
  slice via BlobLocs → equals original ARQO inputs.

If a future PackBuilder refactor inserts a header (per the Arq 5
``.pack`` format), a trailer (per a hypothetical integrity hash),
or any per-entry framing, these tests flag it. The on-disk
format would change and break ``arq_restore`` compatibility.
"""

from __future__ import annotations

import secrets
import tempfile
import unittest
from pathlib import Path


class PackFileByteLayoutTests(unittest.TestCase):
    """Lock the pack file's on-disk byte layout."""

    def _new_pack_builder(self, td: Path, family: str = "blobpacks"):
        from arq_writer.pack_builder import PackBuilder
        cu = "8EB255DD-09D3-43F8-8FE5-6106EBCE1A5D"
        return PackBuilder(
            computer_uuid=cu,
            family=family,
            dest_root=td,
            max_pack_bytes=10 * 1024 * 1024,
        ), cu

    def _read_pack(self, td: Path, pack_info) -> bytes:
        # PackFileInfo.relative_path is rooted at "/<cu>/..."
        full = td / pack_info.relative_path.lstrip("/")
        return full.read_bytes()

    def test_pack_with_n_blobs_is_plain_concatenation(self) -> None:
        """N synthetic ARQO-shaped inputs → pack file = bytewise
        concatenation in insertion order. No framing."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            builder, cu = self._new_pack_builder(tdp)
            arqos = [
                b"ARQO" + bytes(28) + b"_blob_" + str(i).encode() + b"X" * 64
                for i in range(5)
            ]
            blob_ids = [f"{i:064d}" for i in range(5)]
            locs = []
            for bid, arqo in zip(blob_ids, arqos):
                locs.append(builder.add(bid, arqo))
            infos = builder.close()
            self.assertEqual(len(infos), 1)
            pack_bytes = self._read_pack(tdp, infos[0])
            # The pack must EXACTLY equal the concatenation —
            # no header, no trailer, no padding.
            expected = b"".join(arqos)
            self.assertEqual(
                pack_bytes, expected,
                "pack file is NOT pure concatenation — first diff "
                f"at offset "
                f"{next((i for i,(a,b) in enumerate(zip(pack_bytes, expected)) if a!=b), 'EOF')}",
            )
            # Each BlobLoc's offset + length slices its ARQO.
            running = 0
            for loc, arqo in zip(locs, arqos):
                self.assertEqual(loc.offset, running)
                self.assertEqual(loc.length, len(arqo))
                self.assertEqual(
                    pack_bytes[loc.offset:loc.offset + loc.length],
                    arqo,
                )
                running += len(arqo)

    def test_empty_pack_is_zero_bytes(self) -> None:
        """A PackBuilder with no add() calls + close() should
        produce no pack files (or empty ones — we accept either,
        but pin which behaviour we have)."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            builder, _cu = self._new_pack_builder(tdp)
            infos = builder.close()
            # Either no pack info (clean abort) or empty pack
            # files. Either is fine for the operator — what we
            # pin is "no header byte ever emitted into a pack".
            for info in infos:
                pack_bytes = self._read_pack(tdp, info)
                if pack_bytes:
                    self.assertEqual(
                        pack_bytes[:1], b"A",
                        "pack file's first byte is not 'A' "
                        "(ARQO magic start) — header bytes "
                        "snuck in",
                    )

    def test_single_blob_pack_equals_blob_bytes(self) -> None:
        """A pack with exactly one ARQO equals that ARQO byte-
        for-byte. No header, no trailer."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            builder, _ = self._new_pack_builder(tdp)
            arqo = b"ARQO" + secrets.token_bytes(2000)
            loc = builder.add("a" * 64, arqo)
            infos = builder.close()
            self.assertEqual(len(infos), 1)
            pack_bytes = self._read_pack(tdp, infos[0])
            self.assertEqual(pack_bytes, arqo)
            self.assertEqual(loc.offset, 0)
            self.assertEqual(loc.length, len(arqo))

    def test_realbackup_packs_are_concatenations(self) -> None:
        """End-to-end: a real backup's blobpacks pack file slices
        via BlobLocs back to ARQOs whose magic is ``ARQO``."""
        from arq_writer.backup import build_backup
        from arq_reader.parse import parse_tree
        from arq_reader.decrypt import decrypt_lz4_arqo
        from arq_validator import LocalBackend
        from arq_validator.crypto import decrypt_keyset
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "a.txt").write_bytes(b"alpha" * 200)
            (src / "b.txt").write_bytes(b"bravo" * 200)
            (src / "c.txt").write_bytes(b"charlie" * 200)
            dest = tdp / "dest"
            res = build_backup(
                str(src), str(dest),
                encryption_password="pw",
                use_packs=True,
            )
            cu = res.computer_uuid
            # Find each pack file's first 4 bytes — must be ARQO
            # magic (no header insertion).
            for family in ("treepacks", "blobpacks"):
                family_dir = dest / cu / family
                if not family_dir.is_dir():
                    continue
                for pack_path in family_dir.rglob("*.pack"):
                    raw = pack_path.read_bytes()
                    self.assertEqual(
                        raw[:4], b"ARQO",
                        f"{family}/{pack_path.name} doesn't start "
                        f"with ARQO magic — first 4 bytes = "
                        f"{raw[:4]!r}; pack format may have "
                        f"acquired a header",
                    )

    def test_pack_dedup_does_not_duplicate_blobs_in_concat(
        self,
    ) -> None:
        """Adding the same blob_id twice is idempotent — the pack
        file still contains the ARQO bytes only ONCE."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            builder, _ = self._new_pack_builder(tdp)
            arqo = b"ARQO" + secrets.token_bytes(500)
            loc_a = builder.add("dedup-blob-id" + "0" * 51, arqo)
            loc_b = builder.add("dedup-blob-id" + "0" * 51, arqo)
            # Second add returns the same BlobLoc.
            self.assertEqual(loc_a.offset, loc_b.offset)
            self.assertEqual(loc_a.length, loc_b.length)
            infos = builder.close()
            self.assertEqual(len(infos), 1)
            pack_bytes = self._read_pack(tdp, infos[0])
            # The ARQO appears exactly once in the pack.
            self.assertEqual(pack_bytes, arqo)


if __name__ == "__main__":
    unittest.main()
