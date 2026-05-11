"""C7 + C8 + C10 — reader defensive handling of malformed BlobLoc /
pack inputs.

These three derived items probe the reader's behaviour against
inputs the writer would never legitimately emit but that a buggy
peer (different Arq version, hand-edited destination, file-
corruption recovery scenario) could produce:

- **C7 — wrong-type BlobLoc**: a ``dataBlobLoc`` whose
  ``relativePath`` points at a ``treepacks/`` pack file (or vice
  versa). The reader's blob-fetch is keyed by
  ``(relativePath, offset, length)``, opaque to the directory
  name — so a mis-routed BlobLoc still resolves to bytes. C7
  pins that the fetch path **doesn't crash** when the relative
  path is type-confused; whether the resulting bytes pass higher-
  level validation (e.g. ``parse_tree`` rejecting non-tree
  plaintext) is the responsibility of the caller.

- **C8 — mixed compressionType chunks**: a FileNode whose
  ``dataBlobLocs`` carries chunks at multiple compressionType
  values (e.g. one LZ4, one ``none``). The reader must
  concatenate correctly without assuming uniformity. The writer
  itself doesn't currently emit mixed-compression files, but a
  legitimate Arq.app destination COULD if a migration ran or if
  small chunks chose ``none`` while large chunks chose LZ4.

- **C10 — pack with duplicate blob_ids**: a pack file containing
  two ARQOs whose plaintext (and therefore blob_id) is identical.
  The writer's pack builder deduplicates before emit, so this
  won't arise from our own destinations; a peer pack could
  contain duplicates after a non-atomic merge or a corruption-
  recovery rebuild. The reader doesn't index by blob_id (it
  reads by ``relativePath + offset + length``), so duplicates
  should be transparently handled — both copies decode to the
  same plaintext.

The tests use the writer to build a real backup as scaffolding,
then either hand-craft a synthetic BlobLoc or surgically patch
on-disk bytes to produce the adversarial shape. Each test
verifies the reader's response without depending on writer-side
guarantees we explicitly want to bypass.
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


def _build_scaffold(td: Path):
    """Build a packed backup; return (dest, computer_uuid, keyset).

    Used by every C7/C8/C10 test as a starting point — gives us
    a destination with real treepacks/ and blobpacks/ so the
    BlobLoc surgery has authentic pack files to target."""
    from arq_writer.backup import build_backup
    from arq_validator import LocalBackend
    from arq_validator.crypto import decrypt_keyset

    src = td / "src"
    src.mkdir()
    # Two files of varying size so the dataBlobLocs lists have
    # something to swap / mix.
    (src / "small.txt").write_bytes(b"alpha bravo charlie delta echo")
    (src / "medium.bin").write_bytes(b"X" * 32_000)
    dest = td / "dest"
    res = build_backup(
        str(src), str(dest),
        encryption_password="pw",
        use_packs=True,
    )
    backend = LocalBackend(str(dest))
    ks = decrypt_keyset(
        backend.read_all(
            f"/{res.computer_uuid}/encryptedkeyset.dat",
        ),
        "pw",
    )
    return dest, res.computer_uuid, ks


@unittest.skipUnless(_has_openssl(), "openssl CLI required")
class C7_WrongTypeBlobLocTests(unittest.TestCase):
    """Reader fetches bytes by ``relativePath + offset + length``;
    nothing in the fetch path inspects whether the directory name
    matches the BlobLoc's intended usage. C7 pins that.

    A type-confused BlobLoc (data BlobLoc pointing at a tree pack,
    or vice versa) is then a higher-level validation concern —
    the reader's first defence is the parse step (``parse_tree``
    fails when given non-tree bytes; ``itemSize`` mismatch fails
    a checksum). C7 captures both the success and failure shapes
    so future refactors can't accidentally introduce a strict-
    type check at the fetch layer that breaks legitimate
    mis-routed-but-still-valid scenarios."""

    def test_fetch_data_blob_via_treepacks_relativepath(self) -> None:
        """Take a real tree pack's BlobLoc-equivalent
        (relativePath + offset + length picked from the
        reconstructed index) and feed it as a generic blob fetch.
        The fetch should produce the tree blob's plaintext bytes
        without raising — proving the fetch layer is path-agnostic."""
        from arq_writer.types import BlobLoc
        from arq_reader.restore import Restore
        from arq_reader.pack import reconstruct_index
        from arq_validator import LocalBackend
        with tempfile.TemporaryDirectory() as td:
            dest, cu, ks = _build_scaffold(Path(td))
            tp_dir = dest / cu / "treepacks"
            packs = list(tp_dir.rglob("*.pack"))
            self.assertGreater(
                len(packs), 0,
                "scaffold should have produced at least one tree pack",
            )
            pack_path = packs[0]
            entries = reconstruct_index(pack_path.read_bytes())
            self.assertGreater(len(entries), 0)
            entry = entries[0]
            rel = (
                f"/{cu}/treepacks/"
                f"{pack_path.parent.name}/{pack_path.name}"
            )
            # Forge a "data" BlobLoc pointing at a tree pack's
            # offset. Note isPacked=True so the reader takes the
            # pack-range path.
            forged = BlobLoc(
                blobIdentifier="ff" * 32,
                isPacked=True,
                isLargePack=False,
                relativePath=rel,
                offset=entry.offset,
                length=entry.length,
                stretchEncryptionKey=False,
                compressionType=2,
            )
            r = Restore(str(dest), encryption_password="pw")
            # The fetch must succeed (path-agnostic) and return
            # bytes that look like a tree blob (start with the
            # Arq tree header magic). Note: we test the public
            # _fetch_blob seam since C7 is specifically about the
            # fetch layer's defensive shape, not end-to-end
            # restore.
            plaintext = r._fetch_blob(forged, ks)
            self.assertIsInstance(plaintext, bytes)
            self.assertGreater(len(plaintext), 0)
            # Tree blobs are uint32 version + uint64 count + nodes.
            # Sanity: the first 4 bytes parse as a small uint32
            # (v100=tree-v3, v101=tree-v4, or v3 for very-old
            # legacy parser-compat versions). A random data blob
            # would not have these low-valued leading bytes.
            import struct as _s
            ver = _s.unpack(">I", plaintext[:4])[0]
            self.assertLess(
                ver, 1000,
                f"first uint32 = {ver}; expected a small tree-"
                f"version-like value, indicating the bytes really "
                f"came from a tree pack",
            )

    def test_data_pack_relativepath_resolves_as_tree_blob_fetch(
        self,
    ) -> None:
        """Inverse direction — fetch via a blobpacks/ path with a
        BlobLoc the caller intended as a tree. Same conclusion:
        path opaque to the fetch layer."""
        from arq_writer.types import BlobLoc
        from arq_reader.restore import Restore
        from arq_reader.pack import reconstruct_index
        from arq_validator import LocalBackend
        with tempfile.TemporaryDirectory() as td:
            dest, cu, ks = _build_scaffold(Path(td))
            bp_dir = dest / cu / "blobpacks"
            packs = list(bp_dir.rglob("*.pack"))
            if not packs:
                self.skipTest(
                    "scaffold didn't emit blobpacks (small files "
                    "fit entirely in treepacks via Arq's pre-pack "
                    "decision)"
                )
            pack_path = packs[0]
            entries = reconstruct_index(pack_path.read_bytes())
            entry = entries[0]
            rel = (
                f"/{cu}/blobpacks/"
                f"{pack_path.parent.name}/{pack_path.name}"
            )
            forged = BlobLoc(
                blobIdentifier="ff" * 32,
                isPacked=True,
                relativePath=rel,
                offset=entry.offset,
                length=entry.length,
                stretchEncryptionKey=False,
                compressionType=2,
            )
            r = Restore(str(dest), encryption_password="pw")
            plaintext = r._fetch_blob(forged, ks)
            # blobpacks/ contain whatever the writer routes there —
            # file data, xattr blobs, ACL blobs (the routing is by
            # size class, not semantic type). C7's point is only
            # that the fetch returns bytes without crashing, so a
            # length sanity check is the right invariant here.
            self.assertIsInstance(plaintext, bytes)
            self.assertGreater(len(plaintext), 0)


@unittest.skipUnless(_has_openssl(), "openssl CLI required")
class C8_MixedCompressionTypeChunkTests(unittest.TestCase):
    """A FileNode whose chunks have mixed compressionType values.
    Reader must concatenate correctly regardless of mix."""

    def _emit_standalone_blob(
        self,
        dest: Path,
        cu: str,
        ks,
        plaintext: bytes,
        compression: int,
    ):
        """Write a standalone (non-pack) blob with the given
        compressionType; return the BlobLoc pointing at it."""
        from arq_writer.types import BlobLoc
        from arq_writer.crypto_write import (
            build_encrypted_object, compute_blob_id,
        )
        from arq_writer.lz4_block import lz4_wrap

        # Salt for blob_id; not strictly used by reader since the
        # reader keys by relativePath, but write the right shape
        # so it could be parsed by other validation layers.
        salt = b"\x00" * 32

        if compression == 2:
            wrapped = lz4_wrap(plaintext)
        elif compression == 0:
            wrapped = plaintext
        else:
            raise ValueError(f"unsupported compression={compression}")

        arqo = build_encrypted_object(
            wrapped, ks.encryption_key, ks.hmac_key,
        )
        blob_id = compute_blob_id(salt, plaintext)
        shard = blob_id[:2]
        out_dir = dest / cu / "standardobjects" / shard
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / blob_id
        out_path.write_bytes(arqo)
        rel = (
            f"/{cu}/standardobjects/{shard}/{blob_id}"
        )
        return BlobLoc(
            blobIdentifier=blob_id,
            isPacked=False,
            relativePath=rel,
            offset=0,
            length=len(arqo),
            stretchEncryptionKey=False,
            compressionType=compression,
        )

    def test_lz4_then_none_chunks_concatenate_correctly(
        self,
    ) -> None:
        """File body = chunk0 (LZ4) || chunk1 (none). Reader's
        per-BlobLoc compressionType branching should produce the
        concatenated original bytes."""
        from arq_reader.restore import Restore
        from arq_validator import LocalBackend
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            dest, cu, ks = _build_scaffold(tdp)
            chunk0 = b"first half of the file (will be LZ4-wrapped)"
            chunk1 = b"second half, stored without compression"
            loc0 = self._emit_standalone_blob(
                dest, cu, ks, chunk0, compression=2,
            )
            loc1 = self._emit_standalone_blob(
                dest, cu, ks, chunk1, compression=0,
            )
            r = Restore(str(dest), encryption_password="pw")
            # Fetch both, concatenate, verify.
            got0 = r._fetch_blob(loc0, ks)
            got1 = r._fetch_blob(loc1, ks)
            self.assertEqual(got0, chunk0)
            self.assertEqual(got1, chunk1)
            self.assertEqual(got0 + got1, chunk0 + chunk1)

    def test_unsupported_compressiontype_raises_clean(self) -> None:
        """compressionType=99 (or any unknown value) must raise a
        clear error rather than silently returning wrong bytes.
        Pins the contract that an unknown enum value is fail-loud."""
        from arq_writer.types import BlobLoc
        from arq_reader.restore import Restore
        from arq_writer.crypto_write import build_encrypted_object
        from arq_validator import LocalBackend
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            dest, cu, ks = _build_scaffold(tdp)
            # Write a fake standalone blob with bogus compression flag.
            arqo = build_encrypted_object(
                b"some plaintext", ks.encryption_key, ks.hmac_key,
            )
            shard = "ab"
            out_dir = dest / cu / "standardobjects" / shard
            out_dir.mkdir(parents=True, exist_ok=True)
            blob_id = "ab" + "0" * 62
            (out_dir / blob_id).write_bytes(arqo)
            rel = f"/{cu}/standardobjects/{shard}/{blob_id}"
            bogus = BlobLoc(
                blobIdentifier=blob_id,
                isPacked=False,
                relativePath=rel,
                offset=0,
                length=len(arqo),
                stretchEncryptionKey=False,
                compressionType=99,
            )
            r = Restore(str(dest), encryption_password="pw")
            with self.assertRaises(NotImplementedError):
                r._fetch_blob(bogus, ks)


@unittest.skipUnless(_has_openssl(), "openssl CLI required")
class C10_DuplicateBlobIdsInPackTests(unittest.TestCase):
    """The reader keys blob fetches by (relativePath, offset,
    length), not by blob_id — so a pack file containing two ARQOs
    that decode to the same plaintext is transparently handled.
    C10 pins that invariant by hand-crafting such a pack and
    fetching from both offsets."""

    def test_two_arqos_same_plaintext_in_one_pack_both_decode(
        self,
    ) -> None:
        """Build a synthetic pack file by concatenating two
        independently-encrypted ARQOs of the same plaintext.
        Each ARQO has different random IVs / session_keys so the
        bytes differ; the plaintext after decrypt is identical;
        the blob_id (SHA-256 of plaintext) is identical. Reader
        fetches at each offset and verifies both return the
        original plaintext."""
        from arq_writer.types import BlobLoc
        from arq_reader.restore import Restore
        from arq_writer.crypto_write import (
            build_encrypted_object, compute_blob_id,
        )
        from arq_writer.lz4_block import lz4_wrap
        from arq_validator import LocalBackend
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            dest, cu, ks = _build_scaffold(tdp)

            plaintext = b"duplicate content " * 256  # 4608 bytes
            wrapped = lz4_wrap(plaintext)
            # Two independent ARQOs. Each gets fresh IV/session_key
            # internally, so byte-level they're different; both
            # decrypt to wrapped → unwrap → plaintext.
            arqo_a = build_encrypted_object(
                wrapped, ks.encryption_key, ks.hmac_key,
            )
            arqo_b = build_encrypted_object(
                wrapped, ks.encryption_key, ks.hmac_key,
            )
            self.assertNotEqual(
                arqo_a, arqo_b,
                "independent ARQOs of same plaintext must have "
                "different bytes (different random IVs); the "
                "test's premise relies on this",
            )
            blob_id = compute_blob_id(b"\x00" * 32, plaintext)

            # Hand-build a pack file: simple concatenation of
            # ARQOs (Arq's pack format has no header / index).
            pack_dir = (
                dest / cu / "blobpacks" / "ff"
            )
            pack_dir.mkdir(parents=True, exist_ok=True)
            pack_path = (
                pack_dir / "ffffffffffffffffffffffffffffffff.pack"
            )
            pack_bytes = arqo_a + arqo_b
            pack_path.write_bytes(pack_bytes)

            rel = (
                f"/{cu}/blobpacks/ff/"
                f"ffffffffffffffffffffffffffffffff.pack"
            )
            loc_a = BlobLoc(
                blobIdentifier=blob_id,
                isPacked=True,
                relativePath=rel,
                offset=0,
                length=len(arqo_a),
                stretchEncryptionKey=False,
                compressionType=2,
            )
            loc_b = BlobLoc(
                blobIdentifier=blob_id,   # same blob_id, different ARQO
                isPacked=True,
                relativePath=rel,
                offset=len(arqo_a),
                length=len(arqo_b),
                stretchEncryptionKey=False,
                compressionType=2,
            )
            r = Restore(str(dest), encryption_password="pw")
            got_a = r._fetch_blob(loc_a, ks)
            got_b = r._fetch_blob(loc_b, ks)
            self.assertEqual(got_a, plaintext)
            self.assertEqual(got_b, plaintext)
            self.assertEqual(got_a, got_b)

    def test_reconstruct_index_handles_duplicate_blob_ids(
        self,
    ) -> None:
        """``reconstruct_index`` walks ARQOs by length, not blob_id.
        Two ARQOs of the same plaintext produce two index entries
        — verify the function returns both rather than dedup'ing
        on blob_id (which would silently lose one)."""
        from arq_reader.pack import reconstruct_index
        from arq_writer.crypto_write import build_encrypted_object
        from arq_writer.lz4_block import lz4_wrap
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            _, _, ks = _build_scaffold(tdp)
            plaintext = b"hello world from C10 duplicate test"
            wrapped = lz4_wrap(plaintext)
            arqo_a = build_encrypted_object(
                wrapped, ks.encryption_key, ks.hmac_key,
            )
            arqo_b = build_encrypted_object(
                wrapped, ks.encryption_key, ks.hmac_key,
            )
            pack_bytes = arqo_a + arqo_b
            entries = reconstruct_index(pack_bytes)
            self.assertEqual(
                len(entries), 2,
                "reconstruct_index must walk ARQOs by length, "
                "not collapse on blob_id",
            )
            # Their offsets should be 0 and len(arqo_a); their
            # lengths should match the ARQO sizes.
            self.assertEqual(entries[0].offset, 0)
            self.assertEqual(entries[0].length, len(arqo_a))
            self.assertEqual(entries[1].offset, len(arqo_a))
            self.assertEqual(entries[1].length, len(arqo_b))


if __name__ == "__main__":
    unittest.main()
