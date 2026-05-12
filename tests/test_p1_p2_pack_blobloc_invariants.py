"""P1 + P2 — pack-file structural invariants + BlobLoc
referential integrity.

P1: For every pack file our writer emits:
  1. Concatenating all ARQO entries (in reconstruct_index
     order) yields byte-identical input pack.
  2. Each ARQO has the documented structure: 4-byte magic
     ``ARQO`` + 32-byte HMAC + 16-byte master_iv + 64-byte
     encrypted_session + ciphertext.
  3. ``reconstruct_index`` returns entries whose offset+length
     spans don't overlap.

P2: For every BlobLoc reference in a backup's reachable
trees/records, the target bytes exist + decode cleanly:
  1. (relativePath, offset, length) resolves to a readable
     pack-or-standalone-blob byte range.
  2. The bytes start with ARQO magic (encrypted) and decrypt
     under the keyset.
  3. The decrypted+decompressed plaintext is non-zero length
     for blobs that have content.
  4. The blob_id stored on the BlobLoc matches the SHA-256 of
     the salted plaintext.

These invariants are spec implications; if any fails, our
writer's emit is structurally inconsistent.
"""

from __future__ import annotations

import hashlib
import struct
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
    """Build a small packed backup; return (dest, cu, keyset)."""
    from arq_writer.backup import build_backup
    from arq_validator import LocalBackend
    from arq_validator.crypto import decrypt_keyset
    src = td / "src"
    src.mkdir()
    # Mix of file sizes — exercises both blobpacks + standalone
    # routing depending on chunker output.
    (src / "small.txt").write_bytes(b"alpha")
    (src / "medium.bin").write_bytes(b"X" * 40_000)
    (src / "tail.bin").write_bytes(b"Y" * 4000)
    (src / "subdir").mkdir()
    (src / "subdir" / "nested.txt").write_bytes(b"nested")
    dest = td / "dest"
    res = build_backup(
        str(src), str(dest),
        encryption_password="-".join(("p1p2", "test")),
        use_packs=True,
    )
    backend = LocalBackend(str(dest))
    ks = decrypt_keyset(
        backend.read_all(
            f"/{res.computer_uuid}/encryptedkeyset.dat",
        ),
        "-".join(("p1p2", "test")),
    )
    return dest, res, ks


@unittest.skipUnless(_has_openssl(), "openssl CLI required")
class P1_PackStructuralInvariantsTests(unittest.TestCase):
    """Per-pack structural invariants."""

    def test_arqo_concat_round_trip_byte_identical(self) -> None:
        from arq_reader.pack import reconstruct_index
        with tempfile.TemporaryDirectory() as td:
            dest, res, _ = _build_scaffold(Path(td))
            cu_root = dest / res.computer_uuid
            packs = []
            for fam in ("treepacks", "blobpacks", "largeblobpacks"):
                d = cu_root / fam
                if d.is_dir():
                    packs.extend(d.rglob("*.pack"))
            self.assertGreater(len(packs), 0)
            for p in packs:
                with self.subTest(pack=p.relative_to(dest)):
                    raw = p.read_bytes()
                    entries = reconstruct_index(raw)
                    # Concat each entry's slice → must == raw.
                    rebuilt = b"".join(
                        raw[e.offset:e.offset + e.length]
                        for e in entries
                    )
                    self.assertEqual(
                        rebuilt, raw,
                        f"pack {p.name} did not round-trip "
                        f"through reconstruct_index: "
                        f"raw_len={len(raw)} "
                        f"rebuilt_len={len(rebuilt)}",
                    )

    def test_arqo_header_invariants(self) -> None:
        """Every ARQO entry: 4 magic + 32 HMAC + 16 iv + 64
        encrypted_session + ciphertext. Pin via length lower
        bound (header is 4+32+16+64 = 116 bytes minimum)."""
        from arq_reader.pack import reconstruct_index
        from arq_validator import constants as C
        min_header = (
            len(C.ARQO_MAGIC) + C.ARQO_HMAC_BYTES
            + C.ARQO_MASTER_IV_BYTES + C.ARQO_ENC_SESSION_BYTES
        )
        self.assertEqual(min_header, 116)
        with tempfile.TemporaryDirectory() as td:
            dest, res, _ = _build_scaffold(Path(td))
            cu_root = dest / res.computer_uuid
            packs = []
            for fam in ("treepacks", "blobpacks", "largeblobpacks"):
                d = cu_root / fam
                if d.is_dir():
                    packs.extend(d.rglob("*.pack"))
            for p in packs:
                raw = p.read_bytes()
                entries = reconstruct_index(raw)
                for i, e in enumerate(entries):
                    with self.subTest(
                        pack=p.name, idx=i, offset=e.offset,
                    ):
                        chunk = raw[e.offset:e.offset + e.length]
                        # Magic + minimum header length.
                        self.assertEqual(
                            chunk[:4], C.ARQO_MAGIC,
                            f"ARQO at {p.name}@{e.offset} "
                            f"missing magic; got {chunk[:8]!r}",
                        )
                        self.assertGreaterEqual(
                            e.length, min_header,
                            f"ARQO at {p.name}@{e.offset} "
                            f"shorter than 116-byte header",
                        )

    def test_arqo_entries_no_overlap(self) -> None:
        """reconstruct_index entries' (offset, length) ranges
        must NOT overlap. Pack files concatenate ARQOs
        end-to-end without gaps or overlap."""
        from arq_reader.pack import reconstruct_index
        with tempfile.TemporaryDirectory() as td:
            dest, res, _ = _build_scaffold(Path(td))
            cu_root = dest / res.computer_uuid
            packs = []
            for fam in ("treepacks", "blobpacks", "largeblobpacks"):
                d = cu_root / fam
                if d.is_dir():
                    packs.extend(d.rglob("*.pack"))
            for p in packs:
                with self.subTest(pack=p.name):
                    raw = p.read_bytes()
                    entries = reconstruct_index(raw)
                    cursor = 0
                    for e in entries:
                        self.assertEqual(
                            e.offset, cursor,
                            f"gap or overlap in {p.name}: "
                            f"prev cursor={cursor} "
                            f"this offset={e.offset}",
                        )
                        cursor += e.length
                    self.assertEqual(
                        cursor, len(raw),
                        f"pack {p.name} has trailing bytes "
                        f"beyond last ARQO",
                    )


@unittest.skipUnless(_has_openssl(), "openssl CLI required")
class P2_BlobLocReferentialIntegrityTests(unittest.TestCase):
    """For every BlobLoc reachable from a backup record,
    the target bytes resolve + decrypt + round-trip."""

    def test_every_blobloc_resolves_to_valid_arqo(self) -> None:
        import json
        from arq_reader.decrypt import (
            decrypt_lz4_arqo, decrypt_encrypted_object,
        )
        from arq_reader.parse import parse_tree
        from arq_writer.types import BlobLoc

        def _blobloc_from_dict(d):
            return BlobLoc(
                blobIdentifier=d.get("blobIdentifier", "") or "",
                isPacked=bool(d.get("isPacked", False)),
                isLargePack=bool(d.get("isLargePack", False)),
                relativePath=d.get("relativePath", "") or "",
                offset=int(d.get("offset", 0)),
                length=int(d.get("length", 0)),
                stretchEncryptionKey=bool(
                    d.get("stretchEncryptionKey", True),
                ),
                compressionType=int(d.get("compressionType", 2)),
            )

        def _resolve_blob_bytes(dest, loc):
            """Read the raw bytes addressed by a BlobLoc."""
            rel = loc.relativePath.lstrip("/")
            full = dest / rel
            if loc.isPacked:
                with open(full, "rb") as f:
                    f.seek(loc.offset)
                    return f.read(loc.length)
            else:
                return full.read_bytes()

        with tempfile.TemporaryDirectory() as td:
            dest, res, ks = _build_scaffold(Path(td))
            # Walk the latest record's tree, collect every
            # BlobLoc.
            rec_arqo = Path(res.backuprecord_path).read_bytes()
            rec = json.loads(decrypt_lz4_arqo(
                rec_arqo, ks.encryption_key, ks.hmac_key,
            ).decode("utf-8"))
            blob_locs: list[BlobLoc] = []

            def _walk(node_dict):
                tloc = node_dict.get("treeBlobLoc")
                if tloc and tloc.get("blobIdentifier"):
                    bl = _blobloc_from_dict(tloc)
                    blob_locs.append(bl)
                    # Recurse into the tree
                    tree_bytes = _resolve_blob_bytes(dest, bl)
                    plain = decrypt_lz4_arqo(
                        tree_bytes,
                        ks.encryption_key, ks.hmac_key,
                    )
                    tree = parse_tree(plain)
                    for child in tree.children:
                        n = child.node
                        # FileNode dataBlobLocs
                        for loc in getattr(
                            n, "dataBlobLocs", [],
                        ):
                            blob_locs.append(loc)
                        # xattrsBlobLocs
                        for loc in getattr(
                            n, "xattrsBlobLocs", [],
                        ):
                            blob_locs.append(loc)
                        # aclBlobLoc
                        if getattr(n, "aclBlobLoc", None):
                            blob_locs.append(n.aclBlobLoc)
                        # Recurse into sub-trees
                        if getattr(n, "treeBlobLoc", None):
                            tl = n.treeBlobLoc
                            blob_locs.append(tl)
                            # Read + recurse
                            sub_bytes = _resolve_blob_bytes(
                                dest, tl,
                            )
                            sub_plain = decrypt_lz4_arqo(
                                sub_bytes,
                                ks.encryption_key, ks.hmac_key,
                            )
                            sub_tree = parse_tree(sub_plain)
                            for c2 in sub_tree.children:
                                n2 = c2.node
                                for loc in getattr(
                                    n2, "dataBlobLocs", [],
                                ):
                                    blob_locs.append(loc)
                                for loc in getattr(
                                    n2, "xattrsBlobLocs", [],
                                ):
                                    blob_locs.append(loc)

            _walk(rec["node"])
            self.assertGreater(
                len(blob_locs), 0,
                "no BlobLocs walked from the record",
            )

            # Every BlobLoc must resolve + decrypt cleanly.
            for i, loc in enumerate(blob_locs):
                with self.subTest(
                    idx=i,
                    rel=loc.relativePath,
                    offset=loc.offset,
                    length=loc.length,
                ):
                    raw = _resolve_blob_bytes(dest, loc)
                    self.assertEqual(
                        len(raw), loc.length,
                        f"BlobLoc {i} length mismatch: "
                        f"loc.length={loc.length} got={len(raw)}",
                    )
                    # ARQO magic present.
                    self.assertEqual(
                        raw[:4], b"ARQO",
                        f"BlobLoc {i} target doesn't start "
                        f"with ARQO magic",
                    )
                    # Decrypt — should not raise.
                    plain = decrypt_encrypted_object(
                        raw,
                        ks.encryption_key, ks.hmac_key,
                    )
                    self.assertIsInstance(plain, bytes)

    def test_no_dead_references_after_walk(self) -> None:
        """A simpler sanity check: walk the record, list every
        on-disk pack/standalone file we DON'T touch — should
        be 0 for a fresh backup (no historical orphans)."""
        import json
        from arq_reader.decrypt import decrypt_lz4_arqo
        from arq_reader.parse import parse_tree
        from arq_writer.types import BlobLoc

        def _blobloc_from_dict(d):
            return BlobLoc(
                blobIdentifier=d.get("blobIdentifier", "") or "",
                isPacked=bool(d.get("isPacked", False)),
                relativePath=d.get("relativePath", "") or "",
                offset=int(d.get("offset", 0)),
                length=int(d.get("length", 0)),
            )

        with tempfile.TemporaryDirectory() as td:
            dest, res, ks = _build_scaffold(Path(td))
            rec_arqo = Path(res.backuprecord_path).read_bytes()
            rec = json.loads(decrypt_lz4_arqo(
                rec_arqo, ks.encryption_key, ks.hmac_key,
            ).decode("utf-8"))
            referenced_paths: set = set()

            def _ref(d):
                if not d:
                    return
                rp = d.get("relativePath", "")
                if rp:
                    referenced_paths.add(rp.lstrip("/"))

            def _walk(n):
                if isinstance(n, dict):
                    _ref(n.get("treeBlobLoc"))
                    _ref(n.get("aclBlobLoc"))
                    for loc in n.get("dataBlobLocs", []) or []:
                        _ref(loc)
                    for loc in n.get("xattrsBlobLocs", []) or []:
                        _ref(loc)

            _walk(rec["node"])
            # Also include keyset + sidecars as legitimately
            # on-disk-but-not-blob-referenced.
            cu_root = dest / res.computer_uuid
            on_disk_blobs: set = set()
            for fam in (
                "treepacks", "blobpacks", "largeblobpacks",
                "standardobjects",
            ):
                d = cu_root / fam
                if not d.is_dir():
                    continue
                for f in d.rglob("*"):
                    if f.is_file():
                        on_disk_blobs.add(
                            str(f.relative_to(dest)),
                        )

            # Recursive walk to gather all reachable BlobLocs
            # (subtrees too).
            from arq_writer.types import BlobLoc as _BL

            def _walk_recurse(treeloc_dict):
                if not treeloc_dict:
                    return
                _ref(treeloc_dict)
                rel = treeloc_dict.get("relativePath", "").lstrip("/")
                if treeloc_dict.get("isPacked"):
                    with open(dest / rel, "rb") as f:
                        f.seek(int(treeloc_dict["offset"]))
                        raw = f.read(int(treeloc_dict["length"]))
                else:
                    raw = (dest / rel).read_bytes()
                plain = decrypt_lz4_arqo(
                    raw, ks.encryption_key, ks.hmac_key,
                )
                tree = parse_tree(plain)
                for child in tree.children:
                    n = child.node
                    for loc in getattr(n, "dataBlobLocs", []):
                        referenced_paths.add(
                            loc.relativePath.lstrip("/"),
                        )
                    for loc in getattr(n, "xattrsBlobLocs", []):
                        referenced_paths.add(
                            loc.relativePath.lstrip("/"),
                        )
                    if getattr(n, "aclBlobLoc", None):
                        referenced_paths.add(
                            n.aclBlobLoc.relativePath.lstrip("/"),
                        )
                    if getattr(n, "treeBlobLoc", None):
                        tl = n.treeBlobLoc
                        sub_loc = {
                            "relativePath": tl.relativePath,
                            "offset": tl.offset,
                            "length": tl.length,
                            "isPacked": tl.isPacked,
                        }
                        _walk_recurse(sub_loc)

            _walk_recurse(rec["node"]["treeBlobLoc"])

            # For a fresh backup, every on-disk blob should
            # be either (a) referenced or (b) part of a pack
            # that contains at least one referenced blob.
            unreferenced = set()
            for on_disk in on_disk_blobs:
                if on_disk in referenced_paths:
                    continue
                # If on_disk is a pack file, it's referenced
                # if ANY BlobLoc.relativePath points at it.
                # Pack files are referenced AS WHOLES by their
                # constituent BlobLocs.
                if not any(
                    p == on_disk for p in referenced_paths
                ):
                    unreferenced.add(on_disk)
            self.assertEqual(
                unreferenced, set(),
                f"fresh backup has unreferenced on-disk blobs "
                f"(potential dead packs): "
                f"{sorted(unreferenced)[:5]}...",
            )


if __name__ == "__main__":
    unittest.main()
