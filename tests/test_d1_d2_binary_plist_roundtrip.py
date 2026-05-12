"""D1 + D2 — binary-plist BackupRecord emit byte-level
verification.

D10 (PR #140)는 plist↔JSON 라운드트립을 검증했지만, 우리 binary-
plist 출력의 byte-level 정확성은 미검증. macOS의 ``plutil``로
검증할 수 있는 항목:

- D1: 우리 plistlib.dumps(FMT_BINARY) 출력이 valid bplist00 인지
- D2: ``plutil -lint`` 통과 + ``plutil -convert xml1``로 다시
  decode 가능 (양방향 round-trip)

자율 모드의 한계: Arq.app GUI가 실제로 우리 plist를 받는지는
운영자 검증 필요 (P5 in WEEKEND-HUMAN-INTERVENTION-PLAN.md).
하지만 plutil이 받아들이면 macOS의 NSPropertyList parser가
받아들인다는 강한 신호.
"""

from __future__ import annotations

import json
import plistlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


def _has_plutil() -> bool:
    return shutil.which("plutil") is not None


class D1_BinaryPlistByteLevelTests(unittest.TestCase):
    """우리 binary-plist 출력의 정확성."""

    def _build_record_dict(self):
        return {
            "backupFolderUUID":
                "11111111-1111-1111-1111-111111111111",
            "backupPlanUUID":
                "22222222-2222-2222-2222-222222222222",
            "creationDate": 1777777777,
            "version": 101,
            "computerOSType": 1,
            "isComplete": True,
            "archived": False,
            "node": {
                "isTree": True,
                "containedFilesCount": 3,
                "itemSize": 9001,
                "treeBlobLoc": {
                    "blobIdentifier": "ff" * 32,
                    "isPacked": True,
                    "isLargePack": False,
                    "relativePath": "/x/y/treepacks/00/aa.pack",
                    "offset": 0,
                    "length": 1024,
                    "stretchEncryptionKey": True,
                    "compressionType": 2,
                },
                "dataBlobLocs": [],
            },
        }

    def test_emit_starts_with_bplist00_magic(self) -> None:
        from arq_writer.backuprecord import serialize_backuprecord
        rec = self._build_record_dict()
        out = serialize_backuprecord(rec, fmt="binary-plist")
        self.assertEqual(
            out[:8], b"bplist00",
            "binary-plist emit must start with bplist00 magic",
        )

    def test_emit_decodes_with_stdlib_plistlib(self) -> None:
        from arq_writer.backuprecord import serialize_backuprecord
        rec = self._build_record_dict()
        out = serialize_backuprecord(rec, fmt="binary-plist")
        decoded = plistlib.loads(out)
        self.assertEqual(
            decoded["backupFolderUUID"], rec["backupFolderUUID"],
        )
        self.assertEqual(
            decoded["node"]["treeBlobLoc"]["blobIdentifier"],
            "ff" * 32,
        )

    @unittest.skipUnless(_has_plutil(), "plutil not in PATH")
    def test_emit_passes_plutil_lint(self) -> None:
        """plutil -lint은 Apple의 NSPropertyList parser. 통과
        하면 Arq.app GUI의 plist reader도 받아들일 가능성 높음."""
        from arq_writer.backuprecord import serialize_backuprecord
        rec = self._build_record_dict()
        out = serialize_backuprecord(rec, fmt="binary-plist")
        with tempfile.NamedTemporaryFile(
            suffix=".plist", delete=False,
        ) as f:
            f.write(out)
            tmp_path = f.name
        try:
            result = subprocess.run(
                ["plutil", "-lint", tmp_path],
                capture_output=True, text=True, timeout=5,
            )
            self.assertEqual(
                result.returncode, 0,
                f"plutil -lint failed: stdout={result.stdout!r} "
                f"stderr={result.stderr!r}",
            )
            self.assertIn(
                "OK", result.stdout,
                f"plutil didn't say OK: {result.stdout!r}",
            )
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    @unittest.skipUnless(_has_plutil(), "plutil not in PATH")
    def test_emit_round_trips_through_plutil_xml(self) -> None:
        """binary-plist → plutil convert to xml → parse xml →
        same dict shape."""
        from arq_writer.backuprecord import serialize_backuprecord
        rec = self._build_record_dict()
        out = serialize_backuprecord(rec, fmt="binary-plist")
        with tempfile.NamedTemporaryFile(
            suffix=".plist", delete=False,
        ) as f:
            f.write(out)
            bin_path = f.name
        xml_path = bin_path + ".xml"
        try:
            subprocess.run(
                [
                    "plutil", "-convert", "xml1",
                    "-o", xml_path, bin_path,
                ],
                check=True, capture_output=True, timeout=5,
            )
            xml_bytes = Path(xml_path).read_bytes()
            xml_decoded = plistlib.loads(xml_bytes)
            # Same dict.
            self.assertEqual(
                xml_decoded["backupFolderUUID"],
                rec["backupFolderUUID"],
            )
            self.assertEqual(
                xml_decoded["version"], 101,
            )
            self.assertEqual(
                xml_decoded["isComplete"], True,
            )
        finally:
            Path(bin_path).unlink(missing_ok=True)
            Path(xml_path).unlink(missing_ok=True)


class D2_BinaryPlistKeyOrderConcernTests(unittest.TestCase):
    """binary-plist의 NSDictionary key 순서는 우리가 Python dict
    순서로 emit. Apple plistlib도 dict 순서를 그대로 옮김. 그러나
    Arq.app의 self-cache가 그 순서대로 hydrate하면 SQLite의
    UNIQUE constraint 위반 가능성이 있는지 검토."""

    def test_binary_plist_sorts_keys_alphabetically(
        self,
    ) -> None:
        """**Finding**: Python plistlib.FMT_BINARY emits dict
        keys in **alphabetic order**, NOT insertion order.
        This differs from our JSON emit (insertion order via
        json.dumps).

        Compat implication: a BackupRecord emitted as JSON has
        keys in insertion order; the same record emitted as
        binary-plist has keys alphabetically sorted. Both are
        well-formed as PropertyList / JSON respectively, but
        the byte sequence + dict iteration order differ.

        For Arq.app compat:
        - Reader path is plistlib.loads or json.loads — both
          produce a Python dict that consumers iterate.
        - If Arq.app's reader iterates the parsed dict in a
          specific order, JSON and binary-plist emits would
          produce different iteration order.
        - In practice this matters only when downstream code
          hashes the iteration sequence — unlikely.

        This test pins the behaviour so future plistlib changes
        surface here."""
        d = {"z_last": 1, "a_first": 2, "m_middle": 3}
        out1 = plistlib.dumps(d, fmt=plistlib.FMT_BINARY)
        out2 = plistlib.dumps(d, fmt=plistlib.FMT_BINARY)
        # Deterministic across calls.
        self.assertEqual(out1, out2)
        # Keys are alphabetically sorted in the decoded result.
        decoded = plistlib.loads(out1)
        self.assertEqual(
            list(decoded.keys()),
            ["a_first", "m_middle", "z_last"],
            "plistlib FMT_BINARY behaviour changed — keys are "
            "no longer alphabetically sorted. Re-evaluate "
            "Arq.app binary-plist compat assumptions.",
        )

    def test_two_constructions_same_bytes(self) -> None:
        """Same construction → same bytes. Catches a future
        regression where plistlib gets non-deterministic."""
        from arq_writer.backuprecord import serialize_backuprecord
        rec = {
            "backupFolderUUID": "fu",
            "backupPlanUUID": "pu",
            "creationDate": 100,
            "version": 100,
            "node": {"isTree": True},
        }
        out_a = serialize_backuprecord(rec, fmt="binary-plist")
        out_b = serialize_backuprecord(rec, fmt="binary-plist")
        self.assertEqual(
            out_a, out_b,
            "binary-plist emit is non-deterministic across "
            "calls — UNIQUE constraint risk",
        )


if __name__ == "__main__":
    unittest.main()
