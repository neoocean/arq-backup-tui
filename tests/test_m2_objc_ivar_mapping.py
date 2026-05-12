"""M2 — ObjC IVAR / property mapping vs our dataclasses.

Every wire-format field our writer's ``FileNode`` / ``TreeNode``
/ ``BlobLoc`` dataclasses emit should map to a corresponding
identifier (ivar / property / JSON-key string) in the ArqAgent
Mach-O binary. A field that has no Arq.app counterpart means
either:
1. We invented a field Arq.app's parser doesn't recognise
   (silent on-disk drift), OR
2. Field is internal-only parser-state (documented as such).

M2's audit (2026-05-12) found:
- BlobLoc: 8/8 fields verbatim in ArqAgent
- FileNode + TreeNode wire fields: 26/26 each (some via
  documented snake_case ↔ camelCase mapping, e.g.
  ``create_time_sec`` ↔ ``creationTime_sec``,
  ``win_reparse_tag`` ↔ ``reparseTag``)
- 3 fields per Node are parser-state only:
  ``v4_trailing_block``, ``v4_scanned_at_sec``,
  ``v4_scanned_at_nsec`` — internal trailing-block tracking,
  serialized into the BINARY tree blob (not as JSON keys).

The mapping itself is the test invariant: a future refactor that
adds a field MUST either map to an ArqAgent identifier or be
explicitly marked parser-state. Otherwise the test fails loudly.
"""

from __future__ import annotations

import dataclasses as dc
import subprocess
import unittest
from pathlib import Path


BINARY = Path(
    "/Applications/Arq.app/Contents/Resources/"
    "ArqAgent.app/Contents/MacOS/ArqAgent"
)

# Fields that are our writer's parser-state — not on the wire as
# JSON keys, only serialized inside the binary tree blob's
# trailing block. ArqAgent has no JSON identifier for these by
# design (the trailing block is opaque binary).
PARSER_STATE_FIELDS = frozenset({
    "v4_trailing_block",
    "v4_scanned_at_sec",
    "v4_scanned_at_nsec",
})

# Our snake_case field → ArqAgent's camelCase/wire identifier.
# Mappings sampled directly from ArqAgent's string table.
WIRE_MAPPING = {
    "create_time_sec": "creationTime_sec",
    "create_time_nsec": "creationTime_nsec",
    "win_attrs": "winAttrs",
    "win_reparse_tag": "reparseTag",
    "win_reparse_point_is_directory": "reparsePointIsDirectory",
    "username": "userName",
}


@unittest.skipUnless(
    BINARY.is_file(),
    f"ArqAgent not installed at {BINARY}",
)
class M2_ObjCIvarMappingTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        proc = subprocess.run(
            ["strings", str(BINARY)],
            capture_output=True, text=True, timeout=60, check=True,
        )
        cls.strings = set(proc.stdout.splitlines())

    def _check_field(self, field_name: str) -> None:
        """Assert the field name (or its mapped variant) appears
        in ArqAgent's string table."""
        if field_name in PARSER_STATE_FIELDS:
            return
        wire_name = WIRE_MAPPING.get(field_name, field_name)
        candidates = [
            wire_name,
            f"_{wire_name}",
            field_name,
            f"_{field_name}",
        ]
        for c in candidates:
            if c in self.strings:
                return
        self.fail(
            f"field {field_name!r} (wire={wire_name!r}) has no "
            f"identifier in ArqAgent's string table — POTENTIAL "
            f"on-disk drift; either map it via WIRE_MAPPING or "
            f"add it to PARSER_STATE_FIELDS",
        )

    def test_blobloc_all_fields_traced(self) -> None:
        from arq_writer.types import BlobLoc
        for f in dc.fields(BlobLoc):
            with self.subTest(field=f.name):
                self._check_field(f.name)

    def test_filenode_all_fields_traced(self) -> None:
        from arq_writer.types import FileNode
        for f in dc.fields(FileNode):
            with self.subTest(field=f.name):
                self._check_field(f.name)

    def test_treenode_all_fields_traced(self) -> None:
        from arq_writer.types import TreeNode
        for f in dc.fields(TreeNode):
            with self.subTest(field=f.name):
                self._check_field(f.name)

    def test_parser_state_fields_are_v4_trailing_block_only(
        self,
    ) -> None:
        """The 3 parser-state fields are all related to the
        Tree v4 38-byte trailing block — internal to our
        writer's emission. Pin the exact set so a future
        refactor can't sneak in a new internal field without
        explicit re-categorisation."""
        self.assertEqual(
            PARSER_STATE_FIELDS,
            {
                "v4_trailing_block",
                "v4_scanned_at_sec",
                "v4_scanned_at_nsec",
            },
        )

    def test_known_field_count_per_dataclass(self) -> None:
        """Pin the EXACT field count per dataclass so a future
        refactor that adds a field forces a M2 mapping update."""
        from arq_writer.types import FileNode, TreeNode, BlobLoc
        self.assertEqual(
            len(dc.fields(BlobLoc)), 8,
            "BlobLoc field count drifted — update M2 mapping",
        )
        self.assertEqual(
            len(dc.fields(FileNode)), 29,
            "FileNode field count drifted — update M2 mapping",
        )
        self.assertEqual(
            len(dc.fields(TreeNode)), 29,
            "TreeNode field count drifted — update M2 mapping",
        )

    def test_wire_mapping_is_minimal(self) -> None:
        """Each WIRE_MAPPING entry exists only because the
        snake_case ↔ camelCase difference is unavoidable. The
        list shouldn't grow unbounded — if it does, our
        dataclass naming has drifted from Arq.app's emit
        convention more than necessary."""
        self.assertEqual(
            len(WIRE_MAPPING), 6,
            f"WIRE_MAPPING grew to {len(WIRE_MAPPING)} — "
            f"consider standardising dataclass field names on "
            f"Arq.app's camelCase to shrink it",
        )


if __name__ == "__main__":
    unittest.main()
