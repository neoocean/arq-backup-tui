"""N3 — verify FileChangeLasts symbols are present in the
locally-installed ArqAgent binary.

A defensive test: if Arq.app's authors rename
``FileChangeLasts`` in a future version, our RE notes (in
``docs/N3-FILECHANGELASTS-RE.md``) would silently go stale.
This test catches that — operator runs the suite after each
Arq.app upgrade and gets a clear "RE notes need refreshing"
signal if the symbol moves.

Auto-skips when ArqAgent isn't installed locally.
"""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path


BINARY = Path(
    "/Applications/Arq.app/Contents/Resources/"
    "ArqAgent.app/Contents/MacOS/ArqAgent"
)


@unittest.skipUnless(
    BINARY.is_file(),
    f"ArqAgent not installed at {BINARY}",
)
class N3FileChangeLastsSymbolsPresentTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        proc = subprocess.run(
            ["nm", "-arch", "arm64", str(BINARY)],
            capture_output=True, check=True, text=True, timeout=60,
        )
        cls.symbols = proc.stdout

    def test_filechangelasts_class_present(self) -> None:
        self.assertIn("FileChangeLasts", self.symbols)

    def test_filechangelasts_setter_present(self) -> None:
        self.assertIn(
            "setLastFullScanDate:forId:", self.symbols,
            "FileChangeLasts setter symbol missing — RE notes "
            "in docs/N3-FILECHANGELASTS-RE.md may be stale",
        )

    def test_filechangelasts_getter_present(self) -> None:
        self.assertIn(
            "lastFullScanDateForId:", self.symbols,
        )

    def test_filechangelasts_save_present(self) -> None:
        """Persistence method — confirms the per-Node date dict
        is saved across runs (K2 Finding 1 explanation)."""
        self.assertIn(
            "-[FileChangeLasts save:]", self.symbols,
        )

    def test_lastfullscandatesbyid_ivar_present(self) -> None:
        """The NSDictionary IVAR holding NodeID → NSDate."""
        self.assertIn(
            "_lastFullScanDatesById", self.symbols,
        )

    def test_node_writetodata_present(self) -> None:
        """Node binary serializer — the path that emits the
        Node fields."""
        self.assertIn("[Node writeToData:]", self.symbols)

    def test_tree_writetodata_present(self) -> None:
        """Tree binary serializer."""
        self.assertIn("[Tree writeToData:]", self.symbols)


if __name__ == "__main__":
    unittest.main()
