"""V1 — ArqAgent fingerprint pin (cross-version drift detector).

Cross-version diffing requires having two ArqAgent binaries
simultaneously. In a single-host install only one is present
at a time. V1 records a fingerprint of the CURRENT binary so
the operator notices when an Arq.app upgrade ships changes —
the test fails on the new binary + the operator can run
`scripts/v1_arqagent_fingerprint.py` with both binaries (if
the prior is recoverable from backup / from /private/var) to
compute the actual class-level diff.

Pinned fields:
- build_version (parses from "build X.YZ date" string)
- objc_class_names_sha256 (sha256 of sorted Arq* + core class
  names — a single deletion or rename flips this)
- key_strings_present_count (7 sentinel strings)
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

# Make scripts/ importable.
sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent),
)


BINARY = Path(
    "/Applications/Arq.app/Contents/Resources/"
    "ArqAgent.app/Contents/MacOS/ArqAgent"
)

# Pinned 2026-05-12 against ArqAgent 7.41; build_version re-pinned
# 2026-06-06 to 7.44.1. That version string was the ONLY drift across the
# upgrade — the ObjC class-set sha256, class count (41), and the 7 sentinel
# strings are byte-identical between 7.41 and 7.44.1, so the structural
# fingerprint below is unchanged. Bidirectional byte-perfect interop with
# 7.44.1 was independently verified (see HANDOFF 2026-05-24 / docs/ARQ7-
# GUI-INTEROP-2026-05-24.md), so accepting this drift is safe.
PINNED_BUILD_VERSION = "7.44.1"
PINNED_OBJC_CLASSES_SHA256 = (
    "fdd32aa5982663bbae944c06a9d7699616d871b1fa315d2273d47dfd9eb57e23"
)
PINNED_OBJC_CLASS_COUNT = 41
PINNED_KEY_STRINGS_PRESENT = 7


@unittest.skipUnless(
    BINARY.is_file(),
    f"ArqAgent not installed at {BINARY}",
)
class V1_ArqAgentFingerprintPinTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from scripts.v1_arqagent_fingerprint import fingerprint
        cls.fp = fingerprint(BINARY)

    def test_build_version_pinned(self) -> None:
        """If this fails, Arq.app upgraded. Re-run the script
        with the new binary to see what changed; if the new
        version is intentional, update PINNED_BUILD_VERSION."""
        self.assertEqual(
            self.fp["build_version"], PINNED_BUILD_VERSION,
            f"Arq.app build version drifted from "
            f"{PINNED_BUILD_VERSION} to "
            f"{self.fp['build_version']!r}. Run "
            f"scripts/v1_arqagent_fingerprint.py for full "
            f"diff context.",
        )

    def test_objc_class_set_sha256_pinned(self) -> None:
        """The sorted set of public Arq.app ObjC class names.
        A single rename or addition flips this hash — surfaces
        Arq.app's internal-structure drift."""
        self.assertEqual(
            self.fp["objc_class_names_sha256"],
            PINNED_OBJC_CLASSES_SHA256,
            f"ArqAgent ObjC class set drifted. Run "
            f"scripts/v1_arqagent_fingerprint.py --json to "
            f"see the new class list + diff against this hash. "
            f"Current count: {self.fp['objc_class_count']} "
            f"(was {PINNED_OBJC_CLASS_COUNT}).",
        )

    def test_objc_class_count_pinned(self) -> None:
        self.assertEqual(
            self.fp["objc_class_count"],
            PINNED_OBJC_CLASS_COUNT,
        )

    def test_all_key_strings_present(self) -> None:
        """7 sentinel strings from N1/N3/N9/M3 must remain
        present in any future Arq.app binary that claims to
        be Arq 7-compatible."""
        self.assertEqual(
            self.fp["key_strings_present_count"],
            PINNED_KEY_STRINGS_PRESENT,
            f"Arq.app dropped key sentinel strings: "
            f"{self.fp.get('key_strings_missing', [])}",
        )

    def test_fingerprint_includes_filechangelasts_class(
        self,
    ) -> None:
        """FileChangeLasts class is the source of the v4
        trailing block (N3 RE finding). It MUST stay present
        for our trailing-block invariants to remain valid."""
        self.assertIn(
            "FileChangeLasts", self.fp["objc_class_names"],
        )

    def test_fingerprint_includes_treespackbuilder_class(
        self,
    ) -> None:
        """TreesPackBuilder is the pack-write coordinator. If
        Arq.app renames it, our pack-write semantics may have
        shifted."""
        self.assertIn(
            "TreesPackBuilder", self.fp["objc_class_names"],
        )


if __name__ == "__main__":
    unittest.main()
