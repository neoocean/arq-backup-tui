"""Round-trip + spec-conformance tests for the Arq 5/6 .pack/.index parsers."""

from __future__ import annotations

import hashlib
import secrets
import struct
import unittest

from arq_reader.arq5_pack import (
    PACK_FILE_MAGIC,
    PACK_INDEX_MAGIC,
    build_pack_file,
    build_pack_index,
    parse_pack_file,
    parse_pack_index,
)


class IndexRoundTripTests(unittest.TestCase):
    def test_empty_index(self) -> None:
        body = build_pack_index([])
        # 4 magic + 4 version + 1024 fanout + 0 objects + 20 SHA-1 trailer.
        self.assertEqual(len(body), 4 + 4 + 1024 + 20)
        self.assertEqual(parse_pack_index(body), [])

    def test_round_trip_three_entries(self) -> None:
        entries = [
            ("00" * 20, 0, 100),
            ("ff" * 20, 100, 200),
            ("aa" * 20, 300, 50),
        ]
        body = build_pack_index(entries)
        parsed = parse_pack_index(body)
        # Result should be sorted by sha1.
        self.assertEqual([p.sha1_hex for p in parsed],
                         sorted(e[0] for e in entries))
        sorted_entries = sorted(entries, key=lambda t: t[0])
        for got, exp in zip(parsed, sorted_entries):
            self.assertEqual(got.sha1_hex, exp[0])
            self.assertEqual(got.offset, exp[1])
            self.assertEqual(got.data_length, exp[2])

    def test_fanout_is_correct(self) -> None:
        # Mix of low-byte SHA-1s; fanout[ff] must equal total count.
        entries = [
            ("00aaaa" + "00" * 17, 0, 1),
            ("00bbbb" + "00" * 17, 1, 1),
            ("0fcccc" + "00" * 17, 2, 1),
            ("ffdddd" + "00" * 17, 3, 1),
        ]
        body = build_pack_index(entries)
        parsed = parse_pack_index(body)
        self.assertEqual(len(parsed), len(entries))
        # Fanout extraction sanity: count up to first byte 0x10 must
        # equal 3 (the three entries starting with 00 / 0f).
        fanout = struct.unpack(">256I", body[8:8 + 1024])
        self.assertEqual(fanout[0x0F], 3)
        self.assertEqual(fanout[0xFF], 4)

    def test_corrupted_trailer_rejected(self) -> None:
        body = bytearray(build_pack_index([("aa" * 20, 0, 1)]))
        body[-1] ^= 0xFF
        with self.assertRaises(ValueError) as ctx:
            parse_pack_index(bytes(body))
        self.assertIn("trailer", str(ctx.exception))

    def test_bad_magic_rejected(self) -> None:
        body = bytearray(build_pack_index([]))
        body[0] = 0xAB
        # Recompute trailer so we hit the magic check, not the trailer check.
        body[-20:] = hashlib.sha1(bytes(body[:-20])).digest()
        with self.assertRaises(ValueError) as ctx:
            parse_pack_index(bytes(body))
        self.assertIn("magic", str(ctx.exception))


class PackFileRoundTripTests(unittest.TestCase):
    def test_empty_pack(self) -> None:
        body = build_pack_file([])
        # 4 magic + 4 version + 8 count + 0 entries + 20 trailer.
        self.assertEqual(len(body), 4 + 4 + 8 + 20)
        self.assertEqual(parse_pack_file(body), [])

    def test_round_trip(self) -> None:
        entries = [
            ("aa" * 20, 0, b"hello"),
            ("bb" * 20, 0, b""),
            ("cc" * 20, 0, secrets.token_bytes(1234)),
        ]
        body = build_pack_file(entries)
        parsed = parse_pack_file(body)
        self.assertEqual(len(parsed), 3)
        for got, exp in zip(parsed, entries):
            self.assertEqual(got.data, exp[2])
            self.assertEqual(got.mimetype, "")
            self.assertEqual(got.download_name, "")

    def test_corrupted_data_caught_via_trailer(self) -> None:
        body = bytearray(build_pack_file([("aa" * 20, 0, b"x" * 64)]))
        body[20] ^= 0xFF
        with self.assertRaises(ValueError) as ctx:
            parse_pack_file(bytes(body))
        self.assertIn("trailer", str(ctx.exception))

    def test_bad_signature_rejected(self) -> None:
        body = bytearray(build_pack_file([]))
        body[0:4] = b"NOPE"
        body[-20:] = hashlib.sha1(bytes(body[:-20])).digest()
        with self.assertRaises(ValueError) as ctx:
            parse_pack_file(bytes(body))
        self.assertIn("signature", str(ctx.exception))


class CrossReferenceTests(unittest.TestCase):
    """``.index`` offsets must point at valid entries inside ``.pack``."""

    def test_offsets_resolve_correctly(self) -> None:
        # Build a pack and an index that share the same entries.
        # First, build the pack to learn the actual offsets.
        contents = [
            (hashlib.sha1(b"a").hexdigest(), b"alpha"),
            (hashlib.sha1(b"b").hexdigest(), b"beta-payload" * 20),
            (hashlib.sha1(b"c").hexdigest(), b""),
        ]
        # Compute offsets the way build_pack_file would.
        # 4 magic + 4 version + 8 count = 16 byte header; each entry
        # adds 1 (mime null) + 1 (name null) + 8 (length) before data.
        offsets = []
        cur = 16
        for _, data in contents:
            offsets.append(cur)
            cur += 1 + 1 + 8 + len(data)

        pack_body = build_pack_file([
            (sha1, 0, data) for sha1, data in contents
        ])
        index_body = build_pack_index([
            (sha1, off, len(data))
            for (sha1, data), off in zip(contents, offsets)
        ])

        # Verify each index entry's offset, when used to slice the
        # pack, yields the per-entry header followed by the right
        # data. We re-derive `data_length` from the index and walk
        # past the 10-byte entry header to land on the data.
        entries = parse_pack_index(index_body)
        for entry in entries:
            slice_ = pack_body[entry.offset : entry.offset + 1 + 1 + 8 + entry.data_length]
            self.assertEqual(slice_[0], 0)         # null mime
            self.assertEqual(slice_[1], 0)         # null name
            recorded_len = struct.unpack(">Q", slice_[2:10])[0]
            self.assertEqual(recorded_len, entry.data_length)
            data = slice_[10 : 10 + entry.data_length]
            # Look up the matching content by sha1.
            expected = dict(contents)[entry.sha1_hex]
            self.assertEqual(data, expected)


if __name__ == "__main__":
    unittest.main()
