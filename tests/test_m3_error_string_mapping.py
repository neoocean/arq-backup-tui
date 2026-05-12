"""M3 — Arq.app's reader error strings pinned against the
binary + mapped to our reader's equivalent error conditions.

Arq.app's reader uses NSError-style format strings that show up
verbatim in the Mach-O binary. M3 enumerates the ones most
relevant to **format-conformance rejection** and verifies:

1. Each Arq.app error string still appears in the locally
   installed binary (catches Arq.app rename in upgrade).
2. Where our reader has an equivalent rejection path, document
   the mapping; where it doesn't, mark as 'permissive by
   design' (our reader is broader than Arq.app's).

This is a documentation-style test — it doesn't run dynamic
behaviour; it pins the error-string surface so any future
Arq.app upgrade that removes a known error path is flagged.
"""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path


BINARY = Path(
    "/Applications/Arq.app/Contents/Resources/"
    "ArqAgent.app/Contents/MacOS/ArqAgent"
)


# Error string → category mapping.
ERROR_STRING_CATEGORIES = {
    # Arq.app's reader checks for nil arqVersion in a record.
    # Our reader's contract: .get("arqVersion", "") — permissive.
    "nil arqVersion in Commit": (
        "permissive",
        "our reader uses dict.get default '' — accepts nil",
    ),
    # Arq.app's reader aborts when a tree blob doesn't have
    # the right blob identifier. Our reader: parse_tree raises
    # ValueError on malformed bytes.
    "missing blob identifier for tree": (
        "equivalent",
        "our reader's parse_tree raises ValueError on this "
        "shape",
    ),
    # BlobReader gzip / lz4 decompression failures.
    "BlobReader: failed to lz4-inflate buffer": (
        "equivalent",
        "our reader's lz4_unwrap raises ValueError",
    ),
    "BlobReader: failed to gzip-inflate buffer": (
        "equivalent",
        "our reader's _fetch_blob uses gzip.decompress which "
        "raises on bad gzip",
    ),
    # Backup record lookup errors — these are Arq.app's local-
    # cache errors, not on-disk format errors. Not applicable
    # to our reader.
    "no latest backup record found": (
        "not_applicable",
        "Arq.app local-cache error; our reader queries the "
        "destination directly",
    ),
    "no latest complete backup record found": (
        "not_applicable",
        "same; Arq.app local-cache only",
    ),
    "error writing backup record": (
        "writer_side",
        "Arq.app writer error; our writer raises OSError on "
        "the equivalent path",
    ),
    "Backup set encryption data file": (
        "equivalent",
        "our keyset.dat missing → FileNotFoundError on read",
    ),
    # PBKDF2 failure path.
    "PKCS5_PBKDF2_HMAC_SHA1 failed": (
        "not_applicable",
        "we use SHA-256 PBKDF2 (PKCS5_PBKDF2_HMAC_SHA256); "
        "Arq.app v8 also uses SHA-256 — this string is from "
        "the legacy Arq 5 import path",
    ),
}


@unittest.skipUnless(
    BINARY.is_file(),
    f"ArqAgent not installed at {BINARY}",
)
class M3_ErrorStringMappingTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        proc = subprocess.run(
            ["strings", str(BINARY)],
            capture_output=True, text=True, timeout=60, check=True,
        )
        cls.strings = proc.stdout

    def test_each_known_error_string_present(self) -> None:
        """Pin every documented error string against the
        locally-installed ArqAgent. A future upgrade that
        removes one is flagged here."""
        for s in ERROR_STRING_CATEGORIES:
            with self.subTest(error_string=s):
                self.assertIn(
                    s, self.strings,
                    f"Arq.app no longer has the error "
                    f"string {s!r} — RE notes "
                    f"docs/N3-FILECHANGELASTS-RE.md / our "
                    f"reader expectation may be stale",
                )

    def test_mapping_categorisation_is_documented(self) -> None:
        """Every entry has a valid category — refactor sentinel."""
        valid_categories = {
            "equivalent",
            "permissive",
            "not_applicable",
            "writer_side",
        }
        for s, (cat, _explanation) in ERROR_STRING_CATEGORIES.items():
            with self.subTest(error_string=s):
                self.assertIn(cat, valid_categories)

    def test_count_of_mapped_errors(self) -> None:
        """Refactor sentinel: a future addition forces a count
        update + re-categorisation."""
        self.assertEqual(
            len(ERROR_STRING_CATEGORIES), 9,
            "M3 mapping size drifted — re-categorise each "
            "entry in ERROR_STRING_CATEGORIES",
        )


if __name__ == "__main__":
    unittest.main()
