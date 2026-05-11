"""D9 — JSON encoding edge cases.

Arq.app's JSON emit follows Apple's NSJSONSerialization
conventions:

- Compact separators (``,`` and ``:`` with no surrounding space)
- Forward-slash escape (``\\/`` for every literal ``/`` in
  string values — Apple's convention)
- Standard JSON escapes (``\\"``, ``\\\\``, ``\\b``, ``\\f``,
  ``\\n``, ``\\r``, ``\\t``)
- Non-ASCII characters left as UTF-8 (NOT ``\\u`` escaped)

The writer's ``serialize_backuprecord`` already matches this for
the common path. D9 covers the corner cases:

- **Control characters** in strings (\\u0000..\\u001f)
- **Non-BMP Unicode** (emoji + supplementary plane code points)
- **Embedded backslash + slash combos**
- **Empty string** values
- **Nested escapes** (\\\\/ literal)
"""

from __future__ import annotations

import json
import unittest


class JSONEncodingEdgeCasesTests(unittest.TestCase):

    def _serialize(self, rec: dict) -> bytes:
        from arq_writer.backuprecord import serialize_backuprecord
        return serialize_backuprecord(rec, fmt="json")

    def test_forward_slash_in_value_escaped(self) -> None:
        """Pin the Apple ``\\/`` escape on forward slashes."""
        rec = {"path": "/a/b/c"}
        out = self._serialize(rec)
        self.assertIn(rb'"\/a\/b\/c"', out)
        # Round-trips through stdlib json.
        self.assertEqual(
            json.loads(out.decode("utf-8")),
            rec,
        )

    def test_backslash_in_value_double_escaped(self) -> None:
        rec = {"path": "C:\\Users\\test"}
        out = self._serialize(rec)
        # Each backslash escapes as \\ in JSON.
        self.assertIn(b'"C:\\\\Users\\\\test"', out)
        self.assertEqual(
            json.loads(out.decode("utf-8")),
            rec,
        )

    def test_double_quote_in_value_escaped(self) -> None:
        rec = {"note": 'He said "hello"'}
        out = self._serialize(rec)
        self.assertIn(rb'"He said \"hello\""', out)
        self.assertEqual(
            json.loads(out.decode("utf-8")),
            rec,
        )

    def test_control_characters_escaped(self) -> None:
        """Newlines, tabs, etc. in string values must be JSON-
        escaped, not raw."""
        rec = {"text": "line1\nline2\ttab"}
        out = self._serialize(rec)
        self.assertNotIn(b"\nline2", out)   # raw newline absent
        # Standard JSON escapes used.
        self.assertIn(b"\\n", out)
        self.assertIn(b"\\t", out)

    def test_non_ascii_utf8_passthrough(self) -> None:
        """Non-ASCII characters (e.g. Korean) emit as UTF-8
        bytes, NOT as \\u-escape."""
        rec = {"name": "한글 데이터"}
        out = self._serialize(rec)
        # Raw UTF-8 bytes present.
        self.assertIn("한글 데이터".encode("utf-8"), out)
        # No \\u escape.
        self.assertNotIn(b"\\u", out)
        self.assertEqual(
            json.loads(out.decode("utf-8")),
            rec,
        )

    def test_emoji_non_bmp_passthrough(self) -> None:
        """Non-BMP emoji (4-byte UTF-8) survives encoding."""
        rec = {"emoji": "🔐🗝️"}
        out = self._serialize(rec)
        self.assertIn("🔐🗝️".encode("utf-8"), out)
        self.assertEqual(
            json.loads(out.decode("utf-8")),
            rec,
        )

    def test_empty_string_value(self) -> None:
        rec = {"empty": ""}
        out = self._serialize(rec)
        self.assertIn(b'"empty":""', out)
        self.assertEqual(
            json.loads(out.decode("utf-8")),
            rec,
        )

    def test_backslash_slash_combo_disambiguates(self) -> None:
        """``"\\\\/"`` — a backslash followed by a forward slash —
        must serialize unambiguously."""
        rec = {"weird": "\\/"}
        out = self._serialize(rec)
        # Backslash escapes to \\, forward slash to \/, so input
        # "\\/" becomes literal bytes \\\\\\/.
        self.assertIn(b'"\\\\\\/"', out)
        self.assertEqual(
            json.loads(out.decode("utf-8")),
            rec,
        )

    def test_round_trip_byte_identity_for_complex_string(self) -> None:
        """parse → serialize round-trip preserves bytes for a
        string containing every interesting character class."""
        from arq_writer.backuprecord import parse_backuprecord
        rec = {
            "complex": (
                'path /a/b\twith\ttab\n"quote"\\back한국어🔐'
            ),
        }
        first = self._serialize(rec)
        re_parsed = parse_backuprecord(first)
        second = self._serialize(re_parsed)
        self.assertEqual(first, second)

    def test_emit_uses_compact_separators(self) -> None:
        """No whitespace between key/value or between items."""
        rec = {"a": 1, "b": "v"}
        out = self._serialize(rec)
        self.assertNotIn(b": ", out)
        self.assertNotIn(b", ", out)


if __name__ == "__main__":
    unittest.main()
