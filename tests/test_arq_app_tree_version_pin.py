"""Pin Arq.app's current default nodeTreeVersion = 4.

This test compares our writer's TREE_VERSION_V4_TRAILING_BLOCK
constant against what Arq.app v8's BackupRecord ctor stamps as
its default nodeTreeVersion (RE'd in docs/C1-MACHO-RE-PLAN.md
2026-05-10 session: -[BackupRecord init] writes $0x4 to
ivar offset 0x1c).

If a future Arq.app release bumps the default to 5, this test
serves as the trigger: re-run the C1 RE procedure on the new
ArqAgent binary to confirm the v5 format, then update both
sides (our writer's emit + reader's parse + this constant).

The test is OS-agnostic — we don't actually inspect Arq.app
here (binary may not be present in CI). We just pin the
constant value our writer emits + cross-reference the doc.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


class ArqAppTreeVersionPinTests(unittest.TestCase):
    """RE'd 2026-05-10 from
    /Applications/Arq.app/Contents/Resources/ArqAgent.app:
    ``movl $0x4, 0x1c(%rax)`` in -[BackupRecord init]."""

    def test_writer_default_matches_arq_app_v8(self) -> None:
        from arq_writer.constants import TREE_VERSION_V4_TRAILING_BLOCK
        # If you bumped this, you also bumped Arq.app compat —
        # re-do the C1 RE procedure on the new binary first.
        self.assertEqual(
            TREE_VERSION_V4_TRAILING_BLOCK, 4,
            "Arq.app v8 emits nodeTreeVersion = 4. If you "
            "intentionally moved on to v5+, update "
            "docs/C1-MACHO-RE-PLAN.md alongside this test.",
        )

    def test_doc_records_arq_app_findings(self) -> None:
        """The C1 doc must record that we resumed the RE
        session — pin this so a future revert can't quietly
        drop the findings."""
        doc = REPO_ROOT / "docs" / "C1-MACHO-RE-PLAN.md"
        text = doc.read_text(encoding="utf-8")
        for needle in (
            "-[BackupRecord init]",
            "movl   $0x4, 0x1c(%rax)",
            "Findings (2026-05-10 RE session)",
            "ArqAgent",
        ):
            self.assertIn(
                needle, text,
                f"C1 doc lost the {needle!r} reference — "
                f"the RE session findings should stay pinned",
            )


if __name__ == "__main__":
    unittest.main()
