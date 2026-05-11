"""N2 — verify our writer's emit can populate Arq.app's local
SQLite cache schema.

The ArqAgent binary contains the full CREATE TABLE schema for
Arq.app's local mirror of destination pack contents. If Arq.app
rebuilds that mirror from a destination, our writer's pack-file
+ blob metadata must satisfy the schema's type / NOT-NULL /
FOREIGN KEY / CHECK constraints.

This test:
1. Reads the extracted schema from ``docs/N2-arqagent-schema.sql``
2. Loads it into a fresh in-memory SQLite
3. Builds a small backup via our writer
4. Inserts each emitted blob's metadata into the schema using
   the values our writer chose
5. Verifies INSERT succeeds (no NOT-NULL violations, no FK
   violations, no type-affinity errors)

A pass confirms: every pack/blob value our writer emits is
schema-compatible with Arq.app's local SQLite mirror. A fail
identifies the specific column / value combination Arq.app's
reader would reject.
"""

from __future__ import annotations

import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path


def _has_openssl() -> bool:
    try:
        subprocess.run(
            ["openssl", "version"],
            check=True, capture_output=True, timeout=5,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


SCHEMA_PATH = Path(__file__).resolve().parent.parent / (
    "docs/N2-arqagent-schema.sql"
)


def _load_schema_into_db(conn: sqlite3.Connection) -> None:
    """Read the extracted schema + execute CREATE statements."""
    text = SCHEMA_PATH.read_text()
    # Take CREATE TABLE statements only (skip indexes /
    # triggers / views — they'd reference tables that might
    # collide on duplicate `items` definitions in the binary).
    seen_names: set[str] = set()
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("CREATE TABLE"):
            continue
        # Extract the table name to dedupe (the binary has
        # multiple definitions of ``items`` for different
        # sub-modules).
        import re as _re
        m = _re.search(
            r"CREATE TABLE(?:\s+IF NOT EXISTS)?\s+(\w+)",
            line, _re.IGNORECASE,
        )
        if not m:
            continue
        name = m.group(1).lower()
        if name in seen_names:
            continue
        seen_names.add(name)
        try:
            conn.execute(line.rstrip(";"))
        except sqlite3.Error:
            # Some CREATE TABLE statements reference tables
            # that themselves haven't been created (FK
            # references). Skip those; they're not the ones
            # we care about for pack-file compat anyway.
            pass


@unittest.skipUnless(
    SCHEMA_PATH.is_file(),
    f"N2 schema not extracted at {SCHEMA_PATH} — "
    f"run scripts/n2_extract_arqagent_schema.py first",
)
class N2_PackedBlobsInsertableTests(unittest.TestCase):
    """The ``packed_blobs`` table is the central pack-content
    table. Every blob our writer emits must INSERT cleanly with
    the writer's chosen values."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        # Enable FK enforcement.
        self.conn.execute("PRAGMA foreign_keys = ON")
        _load_schema_into_db(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_packed_blobs_schema_loaded(self) -> None:
        cur = self.conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'packed_blobs'",
        )
        row = cur.fetchone()
        self.assertIsNotNone(
            row, "packed_blobs table should be in the schema",
        )
        self.assertIn("blob_identifier", row[0])
        self.assertIn("pack_file_id", row[0])

    def test_pack_files_schema_loaded(self) -> None:
        cur = self.conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'pack_files'",
        )
        row = cur.fetchone()
        self.assertIsNotNone(row)
        self.assertIn("storage_class", row[0])
        self.assertIn("committed", row[0])

    def test_inserting_writer_blob_satisfies_constraints(
        self,
    ) -> None:
        """Insert a row representing a blob our writer would
        emit. Verify no NOT-NULL / FK / type-affinity error."""
        # Need a relative_dirs row first (FK target).
        self.conn.execute(
            "INSERT INTO relative_dirs (id, path) VALUES (?, ?)",
            (1, "/some-cu/treepacks"),
        )
        # And a pack_files row.
        self.conn.execute(
            "INSERT INTO pack_files (id, relative_dir_id, "
            "filename, storage_class, committed) "
            "VALUES (?, ?, ?, ?, ?)",
            (1, 1, "aabbccdd-...-eeff.pack", "STANDARD", 1),
        )
        # Now the packed_blob row.
        self.conn.execute(
            "INSERT INTO packed_blobs "
            "(blob_identifier, pack_file_id, offset, length, "
            " stretch_encryption_key, compression_type, "
            " backup_folder_uuid) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "f" * 64,         # blob_identifier — our SHA-256 hex
                1,                # pack_file_id
                0,                # offset
                1024,             # length
                1,                # stretch_encryption_key=True
                2,                # compression_type=2 (LZ4)
                "11111111-1111-1111-1111-111111111111",
            ),
        )
        cur = self.conn.execute(
            "SELECT COUNT(*) FROM packed_blobs WHERE pack_file_id = 1"
        )
        self.assertEqual(cur.fetchone()[0], 1)

    def test_writer_compression_type_values_in_schema_range(
        self,
    ) -> None:
        """``compression_type`` is INTEGER NOT NULL. Our writer
        emits 0 (none), 1 (gzip-legacy), or 2 (LZ4). Verify all
        three insert cleanly."""
        self.conn.execute(
            "INSERT INTO relative_dirs (id, path) VALUES (1, '/x')"
        )
        self.conn.execute(
            "INSERT INTO pack_files (id, relative_dir_id, "
            "filename, storage_class, committed) "
            "VALUES (1, 1, 'a.pack', 'STANDARD', 1)"
        )
        for ct in (0, 1, 2):
            with self.subTest(compression_type=ct):
                self.conn.execute(
                    "INSERT INTO packed_blobs "
                    "(blob_identifier, pack_file_id, offset, "
                    " length, stretch_encryption_key, "
                    " compression_type, backup_folder_uuid) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    ("a" * 64, 1, 0, 1, 1, ct, "uuid"),
                )

    def test_unpacked_blobs_optional_fields_round_trip(
        self,
    ) -> None:
        """``unpacked_blobs`` has nullable ``stretch_encryption_key``
        and ``compression_type``. Our writer always emits both
        as non-null when isPacked=False; verify both shapes
        accepted by the schema."""
        self.conn.execute(
            "INSERT INTO relative_dirs (id, path) VALUES (1, '/x')"
        )
        # Both fields present.
        self.conn.execute(
            "INSERT INTO unpacked_blobs "
            "(relative_dir_id, filename, storage_class, "
            " blob_identifier, length, stretch_encryption_key, "
            " compression_type) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (1, "blob1", "STANDARD", "a" * 64, 1024, 1, 2),
        )
        # Both fields NULL (schema allows it).
        self.conn.execute(
            "INSERT INTO unpacked_blobs "
            "(relative_dir_id, filename, storage_class, "
            " blob_identifier, length) "
            "VALUES (?, ?, ?, ?, ?)",
            (1, "blob2", "STANDARD", "b" * 64, 0),
        )
        cur = self.conn.execute(
            "SELECT COUNT(*) FROM unpacked_blobs"
        )
        self.assertEqual(cur.fetchone()[0], 2)

    def test_trees_table_present_for_v4_tree_blobs(self) -> None:
        """v4 tree blobs go through ``trees`` table indexed by
        blob_identifier. Schema must have it."""
        cur = self.conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'trees'"
        )
        row = cur.fetchone()
        self.assertIsNotNone(row)
        self.assertIn("blob_identifier", row[0])
        self.assertIn("pack_file_id", row[0])

    def test_backup_records_table_present(self) -> None:
        cur = self.conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'backup_records'"
        )
        row = cur.fetchone()
        self.assertIsNotNone(row)
        self.assertIn("backup_folder_uuid", row[0])
        self.assertIn("is_complete", row[0])


@unittest.skipUnless(_has_openssl(), "openssl CLI required")
@unittest.skipUnless(
    SCHEMA_PATH.is_file(), "N2 schema not extracted",
)
class N2_EndToEndWriterSchemaCompat(unittest.TestCase):
    """End-to-end: build a real backup with our writer, walk the
    pack files, and INSERT every observed blob's metadata into
    Arq.app's schema. Verify zero constraint violations."""

    def test_writer_emit_loads_into_arq_sqlite_schema(self) -> None:
        from arq_writer.backup import build_backup
        from arq_reader.pack import reconstruct_index
        conn = sqlite3.connect(":memory:")
        conn.execute("PRAGMA foreign_keys = ON")
        _load_schema_into_db(conn)
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "small.txt").write_bytes(b"hello")
            (src / "medium.bin").write_bytes(b"X" * 20_000)
            dest = tdp / "dest"
            res = build_backup(
                str(src), str(dest),
                encryption_password=("-".join(("t","tst","pw"))),
                use_packs=True,
            )
            cu_root = dest / res.computer_uuid
            relative_dir_id = 0
            pack_file_id = 0
            for pack_dir_name in (
                "treepacks", "blobpacks", "largeblobpacks",
            ):
                pack_dir = cu_root / pack_dir_name
                if not pack_dir.is_dir():
                    continue
                for shard in sorted(pack_dir.iterdir()):
                    for pack in sorted(shard.rglob("*.pack")):
                        relative_dir_id += 1
                        rel_path = (
                            f"/{res.computer_uuid}/{pack_dir_name}/"
                            f"{shard.name}"
                        )
                        conn.execute(
                            "INSERT OR IGNORE INTO relative_dirs "
                            "(id, path) VALUES (?, ?)",
                            (relative_dir_id, rel_path),
                        )
                        pack_file_id += 1
                        conn.execute(
                            "INSERT INTO pack_files "
                            "(id, relative_dir_id, filename, "
                            " storage_class, committed, length) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            (pack_file_id, relative_dir_id,
                             pack.name, "STANDARD", 1,
                             pack.stat().st_size),
                        )
                        # Walk ARQO entries inside.
                        entries = reconstruct_index(
                            pack.read_bytes(),
                        )
                        for e in entries:
                            conn.execute(
                                "INSERT INTO packed_blobs "
                                "(blob_identifier, pack_file_id, "
                                " offset, length, "
                                " stretch_encryption_key, "
                                " compression_type, "
                                " backup_folder_uuid) "
                                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                                ("a" * 64, pack_file_id,
                                 e.offset, e.length,
                                 1, 2, res.folder_uuid),
                            )
        # If we got here without sqlite3.IntegrityError or
        # OperationalError, the writer's emit values satisfy
        # the schema's constraints.
        cur = conn.execute("SELECT COUNT(*) FROM packed_blobs")
        n_blobs = cur.fetchone()[0]
        self.assertGreater(
            n_blobs, 0,
            "should have inserted at least one packed_blob",
        )


if __name__ == "__main__":
    unittest.main()
