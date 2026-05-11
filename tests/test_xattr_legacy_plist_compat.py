"""A2 — xattr legacy binary plist deserialization compat.

Pre-XAttrSetV002, this writer emitted xattr blobs as Apple
binary plist (``bplist00`` magic). Some destinations on disk
still carry that legacy shape — newer code must keep reading
them so operators with old backups can restore.

The current ``deserialize_xattrs`` accepts both shapes via
magic-byte dispatch:

- ``XAttrSetV002`` (12-byte literal magic) → modern path
- ``bplist00`` (8-byte plist magic) → legacy path

A1 already covered "malformed input fails clean". A2 covers
the **positive** path: every legal legacy shape decodes
correctly. Specifically:

- Empty plist dict
- Single-entry plist
- Multi-entry plist preserving order
- Binary value content
- Str-keyed values with str-typed contents (Python encodes
  on the way in)

If the legacy path is ever removed inadvertently, these tests
flag it before an operator with old data hits the cliff.
"""

from __future__ import annotations

import plistlib
import unittest

from arq_writer.xattrs import (
    _XATTR_MAGIC,
    deserialize_xattrs,
    serialize_xattrs,
)


class XattrLegacyPlistCompatTests(unittest.TestCase):
    """Legacy binary-plist xattr blob shape must keep decoding."""

    def test_empty_dict_legacy_plist_round_trips(self) -> None:
        legacy = plistlib.dumps({}, fmt=plistlib.FMT_BINARY)
        self.assertTrue(legacy.startswith(b"bplist00"))
        decoded = deserialize_xattrs(legacy)
        self.assertEqual(decoded, {})

    def test_single_entry_legacy_plist(self) -> None:
        legacy = plistlib.dumps(
            {"user.simple": b"value-bytes"},
            fmt=plistlib.FMT_BINARY,
        )
        decoded = deserialize_xattrs(legacy)
        self.assertEqual(decoded, {"user.simple": b"value-bytes"})

    def test_multi_entry_legacy_plist_preserves_content(self) -> None:
        original = {
            "user.a": b"alpha",
            "user.b": b"bravo",
            "user.c": b"charlie",
        }
        legacy = plistlib.dumps(original, fmt=plistlib.FMT_BINARY)
        decoded = deserialize_xattrs(legacy)
        # Plist preserves dict equality; ordering depends on
        # plistlib's internal sort but values are intact.
        self.assertEqual(decoded, original)

    def test_legacy_plist_binary_values_round_trip(self) -> None:
        """Real xattrs are arbitrary binary — every byte 0..255
        must survive."""
        binary = bytes(range(256))
        legacy = plistlib.dumps(
            {"user.allbytes": binary},
            fmt=plistlib.FMT_BINARY,
        )
        decoded = deserialize_xattrs(legacy)
        self.assertEqual(decoded["user.allbytes"], binary)

    def test_legacy_plist_str_value_is_utf8_encoded(self) -> None:
        """plistlib may store str values as plist strings. Our
        decoder coerces them to UTF-8 bytes so the contract
        (dict[str, bytes]) holds regardless of how the legacy
        plist stored them."""
        legacy = plistlib.dumps(
            {"user.unicode": "한글 컨텐트"},
            fmt=plistlib.FMT_BINARY,
        )
        decoded = deserialize_xattrs(legacy)
        self.assertEqual(
            decoded["user.unicode"],
            "한글 컨텐트".encode("utf-8"),
        )

    def test_mixed_format_destination_decodes_both_shapes(self) -> None:
        """If a destination has BOTH legacy and modern blobs (a
        plausible migration scenario where some files were re-
        encrypted under the new format), both decode through the
        same call."""
        legacy = plistlib.dumps(
            {"user.legacy": b"old"}, fmt=plistlib.FMT_BINARY,
        )
        modern = serialize_xattrs({"user.modern": b"new"})
        self.assertTrue(legacy.startswith(b"bplist00"))
        self.assertTrue(modern.startswith(_XATTR_MAGIC))
        # Verify both shapes decode cleanly.
        self.assertEqual(
            deserialize_xattrs(legacy),
            {"user.legacy": b"old"},
        )
        self.assertEqual(
            deserialize_xattrs(modern),
            {"user.modern": b"new"},
        )

    def test_unknown_magic_in_xattr_blob_raises_valueerror(
        self,
    ) -> None:
        """Sanity: an unknown magic raises a recognisable error,
        not a silent decode. This is technically A1 territory but
        also pins that the legacy-shape dispatcher doesn't fall
        through to silent acceptance."""
        with self.assertRaises(ValueError):
            deserialize_xattrs(b"XXX_NOT_A_REAL_FORMAT")


if __name__ == "__main__":
    unittest.main()
