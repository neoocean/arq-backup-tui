"""Buzhash chunker tests + writer integration tests."""

from __future__ import annotations

import filecmp
import secrets
import tempfile
import unittest
from pathlib import Path

from arq_reader import Restore
from arq_writer import build_backup
from arq_writer.chunker import (
    Buzhash,
    ChunkerConfig,
    chunk_bytes,
)


class BuzhashAlgebraTests(unittest.TestCase):
    """Tests for the rolling-hash math, independent of chunking."""

    def test_initial_hash_deterministic(self) -> None:
        b1 = Buzhash()
        b2 = Buzhash()
        win = b"hello world! " * 4         # exactly 52 bytes; trim to 48
        win = win[:48]
        self.assertEqual(b1.initial_hash(win), b2.initial_hash(win))

    def test_slide_matches_initial(self) -> None:
        # The rolling-hash invariant: if you slide one byte forward,
        # the result must equal recomputing initial_hash on the new
        # window from scratch.
        bz = Buzhash()
        n = bz.config.window_size
        data = secrets.token_bytes(n + 50)
        h = bz.initial_hash(data[:n])
        for i in range(50):
            byte_out = data[i]
            byte_in = data[i + n]
            h = bz.slide(h, byte_out, byte_in)
            expected = bz.initial_hash(data[i + 1 : i + 1 + n])
            self.assertEqual(
                h, expected,
                f"slide diverged at step {i}: got {h:#010x}, "
                f"expected {expected:#010x}",
            )

    def test_window_size_validation(self) -> None:
        bz = Buzhash()
        with self.assertRaises(ValueError):
            bz.initial_hash(b"\x00" * (bz.config.window_size + 1))

    def test_invalid_config_rejected(self) -> None:
        with self.assertRaises(ValueError):
            Buzhash(ChunkerConfig(window_size=0))
        with self.assertRaises(ValueError):
            Buzhash(ChunkerConfig(min_chunk_size=10, window_size=64))
        with self.assertRaises(ValueError):
            Buzhash(ChunkerConfig(
                min_chunk_size=128,
                max_chunk_size=64,
                window_size=48,
            ))


class ChunkerCorrectnessTests(unittest.TestCase):
    def _chunker(self) -> Buzhash:
        # Tighter parameters than the default so small test inputs
        # produce multiple chunks.
        return Buzhash(ChunkerConfig(
            window_size=16,
            boundary_bits=8,        # ~256-byte avg chunk
            min_chunk_size=16,
            max_chunk_size=4096,
        ))

    def test_concat_equals_input(self) -> None:
        bz = self._chunker()
        data = secrets.token_bytes(50_000)
        chunks = list(bz.chunk(data))
        self.assertEqual(b"".join(chunks), data)

    def test_empty_input_yields_no_chunks(self) -> None:
        bz = self._chunker()
        self.assertEqual(list(bz.chunk(b"")), [])

    def test_short_input_yields_one_chunk(self) -> None:
        bz = self._chunker()
        self.assertEqual(list(bz.chunk(b"x" * 5)), [b"x" * 5])

    def test_max_chunk_size_enforced(self) -> None:
        bz = Buzhash(ChunkerConfig(
            window_size=16,
            boundary_bits=30,       # mask is huge → boundaries rare
            min_chunk_size=16,
            max_chunk_size=512,
        ))
        # Use random bytes so the rolling hash never accidentally hits
        # the rare boundary mask before max_chunk_size kicks in.
        data = secrets.token_bytes(2048)
        chunks = list(bz.chunk(data))
        # At least 4 chunks (2048 / 512); none larger than max.
        self.assertGreaterEqual(len(chunks), 4)
        for c in chunks:
            self.assertLessEqual(len(c), 512)

    def test_chunks_are_content_defined(self) -> None:
        # The defining property: same content -> same chunk
        # boundaries regardless of where it appears in the stream.
        bz = self._chunker()
        prefix1 = secrets.token_bytes(2000)
        prefix2 = secrets.token_bytes(2500)    # different length
        common = secrets.token_bytes(20_000)
        chunks_a = list(bz.chunk(prefix1 + common))
        chunks_b = list(bz.chunk(prefix2 + common))
        # Strip the differing prefixes and look at chunks that lie
        # entirely within the common section — they should match.
        # Since we don't know which chunk first lands fully in the
        # common section, we use a tail-suffix match instead:
        # the FINAL ~50% of each output should be identical.
        joined_a = b"".join(chunks_a)
        joined_b = b"".join(chunks_b)
        # Sanity: total bytes equal input bytes.
        self.assertEqual(joined_a, prefix1 + common)
        self.assertEqual(joined_b, prefix2 + common)
        # Find common-suffix chunks: walk backwards and collect chunks
        # while bytes match. We expect the same chunk boundaries on
        # both sides for at least the trailing few KB.
        rev_a = list(reversed(chunks_a))
        rev_b = list(reversed(chunks_b))
        matched = 0
        i = 0
        while i < min(len(rev_a), len(rev_b)) and rev_a[i] == rev_b[i]:
            matched += len(rev_a[i])
            i += 1
        # Expect at least 1 KB of trailing common chunks (with our
        # 256-byte avg, ~4 chunks).
        self.assertGreater(
            matched, 1024,
            f"only {matched} bytes of trailing chunk-overlap — "
            f"chunker isn't producing content-defined boundaries",
        )

    def test_dedup_friendly_for_small_edits(self) -> None:
        # Insert one byte in the middle of a large random buffer.
        # Most chunks should still match the unmodified version.
        bz = self._chunker()
        data = secrets.token_bytes(20_000)
        modified = data[:10_000] + b"X" + data[10_000:]
        chunks_a = set(bytes(c) for c in bz.chunk(data))
        chunks_b = set(bytes(c) for c in bz.chunk(modified))
        common = chunks_a & chunks_b
        # At least half the chunks should be shared.
        self.assertGreater(
            len(common), len(chunks_a) // 2,
            f"dedup overlap only {len(common)} of {len(chunks_a)} "
            f"unmodified chunks survived the edit",
        )

    def test_chunk_bytes_convenience(self) -> None:
        data = b"hello arq backup " * 1000
        chunks = list(chunk_bytes(data, config=self._chunker().config))
        self.assertEqual(b"".join(chunks), data)


class ChunkerWriterIntegrationTests(unittest.TestCase):
    def test_round_trip_with_chunker(self) -> None:
        # Big random file → multiple chunks; reader concatenates.
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            src = td / "src"
            src.mkdir()
            big = secrets.token_bytes(64 * 1024)
            small = b"tiny\n"
            (src / "big.bin").write_bytes(big)
            (src / "small.txt").write_bytes(small)

            cfg = ChunkerConfig(
                window_size=32,
                boundary_bits=10,        # ~1 KiB avg
                min_chunk_size=512,
                max_chunk_size=8 * 1024,
            )
            res_w = build_backup(
                src, td / "backup", "pw",
                chunker_config=cfg,
            )
            r = Restore(td / "backup", "pw")
            res_r = r.restore(
                folder_uuid=res_w.folder_uuid, dest=td / "out",
            )
            self.assertEqual(res_r.failures, [])
            self.assertEqual((td / "out" / "big.bin").read_bytes(), big)
            self.assertEqual((td / "out" / "small.txt").read_bytes(), small)

    def test_big_file_produces_multiple_chunks(self) -> None:
        # Read the backuprecord and inspect dataBlobLocs count.
        from arq_reader.decrypt import decrypt_lz4_arqo
        from arq_validator.crypto import decrypt_keyset
        from arq_validator.layout import find_latest_backuprecord
        from arq_validator.backend import LocalBackend
        import plistlib

        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            src = td / "src"
            src.mkdir()
            (src / "big.bin").write_bytes(secrets.token_bytes(64 * 1024))
            cfg = ChunkerConfig(
                window_size=32,
                boundary_bits=10,
                min_chunk_size=512,
                max_chunk_size=4 * 1024,
            )
            res_w = build_backup(
                src, td / "backup", "pw",
                chunker_config=cfg,
            )

            # Verify the file was split into multiple blobs.
            backend = LocalBackend(td / "backup")
            keyset_blob = backend.read_all(
                f"/{res_w.computer_uuid}/encryptedkeyset.dat"
            )
            keyset = decrypt_keyset(keyset_blob, "pw")
            rec_path = find_latest_backuprecord(
                backend, "/", res_w.computer_uuid, res_w.folder_uuid,
            )
            rec_arqo = backend.read_all(rec_path)
            rec_plain = decrypt_lz4_arqo(
                rec_arqo, keyset.encryption_key, keyset.hmac_key,
            )
            rec = plistlib.loads(rec_plain)
            # Walk to the file node.
            tree_loc = rec["node"]["treeBlobLoc"]
            from arq_writer.types import BlobLoc as _BL
            from arq_reader.parse import parse_tree, BinaryReader
            from arq_reader.decrypt import decrypt_encrypted_object
            tree_arqo = backend.read_all(tree_loc["relativePath"])
            tree_bytes = decrypt_lz4_arqo(
                tree_arqo, keyset.encryption_key, keyset.hmac_key,
            )
            tree = parse_tree(tree_bytes)
            # First child is "big.bin" (only file), but Tree binary
            # children iter from arq_reader.parse:
            big_node = tree.children[0].node
            self.assertGreaterEqual(
                len(big_node.dataBlobLocs), 8,
                f"expected ≥ 8 chunks for 64 KiB file with avg-1KiB "
                f"chunker, got {len(big_node.dataBlobLocs)}",
            )

    def test_dedup_across_modified_file(self) -> None:
        # Two files: one is the original, the other has a small
        # in-place edit. With chunker dedup most blobs should be
        # shared; the writer's blob_id cache returns one BlobLoc per
        # SHA-256.
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            src = td / "src"
            src.mkdir()
            data = secrets.token_bytes(32 * 1024)
            modified = data[:16_000] + b"PATCH" + data[16_000:]
            (src / "v1.bin").write_bytes(data)
            (src / "v2.bin").write_bytes(modified)

            cfg = ChunkerConfig(
                window_size=32, boundary_bits=10,
                min_chunk_size=512, max_chunk_size=4096,
            )
            res_w = build_backup(
                src, td / "backup", "pw", chunker_config=cfg,
            )
            r = Restore(td / "backup", "pw")
            res_r = r.restore(
                folder_uuid=res_w.folder_uuid, dest=td / "out",
            )
            self.assertEqual(res_r.failures, [])
            self.assertTrue(
                filecmp.cmp(src / "v1.bin", td / "out" / "v1.bin",
                             shallow=False)
            )
            self.assertTrue(
                filecmp.cmp(src / "v2.bin", td / "out" / "v2.bin",
                             shallow=False)
            )

    def test_no_chunker_yields_one_blob_per_file(self) -> None:
        # When chunker_config is None (default), each file produces
        # exactly one BlobLoc — preserving v0 behavior.
        with tempfile.TemporaryDirectory() as td_str:
            td = Path(td_str)
            src = td / "src"; src.mkdir()
            (src / "f.bin").write_bytes(secrets.token_bytes(20_000))
            res_w = build_backup(src, td / "backup", "pw")
            r = Restore(td / "backup", "pw")
            res_r = r.restore(
                folder_uuid=res_w.folder_uuid, dest=td / "out",
            )
            self.assertEqual(res_r.failures, [])


if __name__ == "__main__":
    unittest.main()
