"""M1 — magic constants cross-check vs ArqAgent binary.

Every magic byte sequence / sentinel string / directory name our
code uses should appear verbatim in the ArqAgent Mach-O binary.
A mismatch means we're using a constant value Arq.app's own
reader/writer doesn't recognise — silent breakage waiting to
happen.

This test extracts the ArqAgent string table once + asserts
each of our 28 cross-checkable constants appears in it. If a
future Arq.app upgrade renames a string or changes a format-
literal, the test fails loudly so we know to revisit.

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
class M1_MagicConstantsCrosscheckTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        proc = subprocess.run(
            ["strings", str(BINARY)],
            capture_output=True, text=True, timeout=60, check=True,
        )
        cls.strings = proc.stdout

    # 1. Crypto + envelope magic bytes
    def test_arqo_magic_present(self) -> None:
        from arq_validator import constants as C
        self.assertEqual(C.ARQO_MAGIC, b"ARQO")
        self.assertIn("ARQO", self.strings)

    def test_keyset_magic_present(self) -> None:
        from arq_validator import constants as C
        self.assertEqual(
            C.KEYSET_MAGIC, b"ARQ_ENCRYPTED_MASTER_KEYS",
        )
        self.assertIn(
            "ARQ_ENCRYPTED_MASTER_KEYS", self.strings,
        )

    def test_xattrset_magic_present(self) -> None:
        from arq_writer.xattrs import _XATTR_MAGIC
        self.assertEqual(_XATTR_MAGIC, b"XAttrSetV002")
        self.assertIn("XAttrSetV002", self.strings)

    # 2. On-disk filenames + directory names
    def test_keyset_filename(self) -> None:
        from arq_validator import constants as C
        self.assertEqual(C.KEYSET_FILE, "encryptedkeyset.dat")
        self.assertIn("encryptedkeyset.dat", self.strings)

    def test_sidecar_filenames(self) -> None:
        for fname in (
            "backupplan.json", "backupconfig.json",
            "backupfolders.json", "backupfolder.json",
        ):
            with self.subTest(filename=fname):
                self.assertIn(fname, self.strings)

    def test_pack_dir_names(self) -> None:
        from arq_validator import constants as C
        for d in (
            C.TREEPACKS_DIR, C.BLOBPACKS_DIR,
            C.LARGEBLOBPACKS_DIR, C.STANDARDOBJECTS_DIR,
        ):
            with self.subTest(dir=d):
                self.assertIn(d, self.strings)

    def test_storage_class_subdir_names(self) -> None:
        """Per backupfolders.json schema — Arq.app emits 6
        ObjectDir families. Each maps to a subdir name."""
        for d in (
            "standardobjects", "standardiaobjects",
            "onezoneiaobjects", "s3glacierobjects",
            "s3glacierirobjects", "s3deeparchiveobjects",
        ):
            with self.subTest(dir=d):
                self.assertIn(d, self.strings)

    def test_backupfolders_subdir(self) -> None:
        from arq_validator import constants as C
        self.assertEqual(
            C.BACKUPFOLDERS_DIR, "backupfolders",
        )
        self.assertIn("backupfolders", self.strings)

    # 3. JSON field names — every key our writer emits should
    # appear as a literal in ArqAgent's parser/emitter.
    def test_node_field_names_present(self) -> None:
        for field in (
            "aclBlobLoc", "dataBlobLocs", "xattrsBlobLocs",
            "treeBlobLoc", "isTree", "containedFilesCount",
            "itemSize", "deleted", "userName", "groupName",
            "computerOSType", "documentID", "hasDocumentID",
            "isSparse", "sparseLogicalSize", "holes",
            "reparseTag", "reparsePointIsDirectory",
            "winAttrs", "addedTime_sec", "addedTime_nsec",
            "modificationTime_sec", "creationTime_sec",
            "changeTime_sec",
        ):
            with self.subTest(field=field):
                self.assertIn(field, self.strings)

    def test_blobloc_field_names_present(self) -> None:
        for field in (
            "blobIdentifier", "isPacked", "isLargePack",
            "relativePath", "offset", "length",
            "stretchEncryptionKey", "compressionType",
        ):
            with self.subTest(field=field):
                self.assertIn(field, self.strings)

    def test_backuprecord_top_level_fields_present(self) -> None:
        for field in (
            "archived", "arqVersion", "backupFolderUUID",
            "backupPlanJSON", "backupPlanUUID",
            "backupRecordErrors", "computerOSType",
            "copiedFromCommit", "copiedFromSnapshot",
            "creationDate", "diskIdentifier", "isComplete",
            "localMountPoint", "localPath", "node",
            "nodeTreeVersion", "relativePath",
            "storageClass", "version", "volumeName",
        ):
            with self.subTest(field=field):
                self.assertIn(field, self.strings)

    def test_backupplan_polymorphic_field_names(self) -> None:
        """P2 / P3 polymorphism keys."""
        for field in (
            "scheduleJSON", "transferRateJSON",
            "emailReportJSON",
            # scheduleJSON shape (Daily / Hourly):
            "backUpAndValidate", "daysOfWeek", "timeOfDay",
            "everyHours", "minutesAfterHour",
            "pauseDuringWindow", "pauseFrom", "pauseTo",
            "startWhenVolumeIsConnected",
            # transferRateJSON shape:
            "scheduleType", "endTimeOfDay", "startTimeOfDay",
            "maxKBPS",
            # emailReportJSON shape:
            "authenticationType", "connectionSecurity",
            "reportHELOUseIP", "port", "fromAddress",
            "hostname", "startTLS", "subject", "toAddress",
            "username", "when",
        ):
            with self.subTest(field=field):
                self.assertIn(field, self.strings)

    # 4. PBKDF2 iteration count cannot be checked via strings
    # (it's an immediate value compiled into ARM64 movz/movk).
    # We pin the value our writer uses + note this is the only
    # constant we can't cross-check directly.
    def test_pbkdf2_iteration_count_documented_not_in_strings(
        self,
    ) -> None:
        from arq_validator import constants as C
        self.assertEqual(C.KEYSET_PBKDF2_ITERATIONS, 200_000)
        # Immediate values don't appear in strings table; this
        # is an expected gap.
        self.assertNotIn("200000", self.strings,
                         "if 200000 ever appears, ArqAgent's "
                         "iteration count may have become "
                         "string-formatted and we should "
                         "re-RE the keyset code path")


if __name__ == "__main__":
    unittest.main()
