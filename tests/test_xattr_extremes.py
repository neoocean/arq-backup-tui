"""E3 — extreme xattr cases.

The ``XAttrSetV002`` format has no inherent count or per-value
size cap — it's a flat sequence of length-prefixed records — but
the format-handler code still needs to handle the volume without
quadratic-cost or unbounded-memory regressions. These tests pin:

- **Many xattrs on one inode** (200 attributes) — round-trips
  byte-identical, no field count overflow.
- **One large xattr value** (4 MiB binary) — round-trips byte-
  identical, no integer overflow on the per-value length prefix.
- **Both at once** (50 entries each ≈ 100 KiB) — combined volume
  ≈ 5 MiB through serialise → deserialise → serialise; second
  emit matches the first.

The XAttrSetV002 length prefix is ``uint64 BE``, so the format
itself supports values up to 2⁶⁴-1 bytes; the tests use values
small enough to keep CI fast but large enough to exercise the
multi-MiB code paths.

Tests use the pure serialise/deserialise API rather than the
filesystem-side ``capture_xattrs`` / ``apply_xattrs`` pair
because:

- macOS imposes a 128 KiB per-xattr cap (HFS+) or 2 KiB on some
  filesystems; the FS APIs would reject our synthetic extremes.
- Linux tmpfs imposes its own caps (typically 64 KiB).
- The on-disk format is what compat with Arq.app v8 requires;
  the FS round-trip is OS-bounded, not format-bounded.

What's pinned here is **the format handler's behaviour**. The
existing :class:`tests.test_xattrs.FilesystemRoundTripTests`
covers the FS path under its tighter limits.
"""

from __future__ import annotations

import unittest

from arq_writer.xattrs import deserialize_xattrs, serialize_xattrs


class ManyXattrsOnOneNodeTests(unittest.TestCase):
    def test_two_hundred_xattrs_round_trip(self) -> None:
        # 200 entries, unique names, unique values.
        xattrs = {
            f"user.test.attr{i:03d}": f"value-{i}".encode("utf-8")
            for i in range(200)
        }
        blob = serialize_xattrs(xattrs)
        decoded = deserialize_xattrs(blob)
        self.assertEqual(decoded, xattrs)
        # And the round-trip is byte-identical (re-serialise =
        # original). Pins the listxattr-order convention end-to-
        # end at large field count.
        self.assertEqual(serialize_xattrs(decoded), blob)

    def test_keys_preserve_input_order_at_scale(self) -> None:
        # Non-alphabetic insertion order; deserialize must give
        # back the same iteration order.
        names = [
            f"user.{chr(0x7A - (i % 26))}_{i:03d}"
            for i in range(150)
        ]
        xattrs = {name: f"v{i}".encode() for i, name in enumerate(names)}
        blob = serialize_xattrs(xattrs)
        decoded = deserialize_xattrs(blob)
        self.assertEqual(list(decoded.keys()), names)


class LargeXattrValueTests(unittest.TestCase):
    def test_four_mib_single_value_round_trips(self) -> None:
        # Four MiB binary value with a recognisable pattern, so a
        # silent truncation regression would surface as a content
        # diff (not just a size diff).
        value = (b"PATTERN" * 600_000)[: 4 * 1024 * 1024]
        self.assertEqual(len(value), 4 * 1024 * 1024)
        xattrs = {"user.bigblob": value}
        blob = serialize_xattrs(xattrs)
        # Sanity: blob is at least value-sized.
        self.assertGreater(len(blob), len(value))
        decoded = deserialize_xattrs(blob)
        self.assertEqual(set(decoded.keys()), {"user.bigblob"})
        self.assertEqual(decoded["user.bigblob"], value)
        self.assertEqual(serialize_xattrs(decoded), blob)

    def test_one_mib_with_arbitrary_binary_bytes(self) -> None:
        # Arbitrary bytes (every value byte 0..255 repeated). Tests
        # we don't accidentally UTF-8-decode or otherwise mangle
        # binary content.
        value = bytes(range(256)) * 4096   # 1 MiB
        self.assertEqual(len(value), 256 * 4096)
        blob = serialize_xattrs({"user.allbytes": value})
        decoded = deserialize_xattrs(blob)
        self.assertEqual(decoded["user.allbytes"], value)


class CombinedExtremeTests(unittest.TestCase):
    def test_fifty_entries_100kib_each_round_trips(self) -> None:
        # 50 entries × ~100 KiB → ~5 MiB total. Exercises both
        # the per-entry overhead and the cumulative buffer growth.
        xattrs = {
            f"user.entry{i:02d}": (
                f"prefix-{i:02d}-".encode() + b"x" * (100 * 1024)
            )
            for i in range(50)
        }
        blob = serialize_xattrs(xattrs)
        # 50 × 100 KiB plus per-entry overhead ≈ 5 MB. Pin a lower
        # bound that catches "the writer silently truncated".
        self.assertGreater(len(blob), 50 * 100 * 1024)
        decoded = deserialize_xattrs(blob)
        self.assertEqual(decoded, xattrs)
        # Second emit byte-identical.
        self.assertEqual(serialize_xattrs(decoded), blob)


if __name__ == "__main__":
    unittest.main()
