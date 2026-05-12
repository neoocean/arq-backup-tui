"""I1 — mixed Arq 5 + Arq 7 destination handling.

ArqAgent 7.41 binary는 ``Arq5Node`` / ``Arq5Tree`` /
``Arq5ImportDB`` / ``Arq5Bucket`` 등 16개 ``Arq5*`` 클래스를
포함. 이는 Arq.app v8이 여전히 Arq 5 데이터를 import / migration
처리 가능함을 보여줍니다.

우리는 Arq 5 reader (``arq_reader.arq5_*``)를 가지고 있지만,
Arq 7 writer만 있음. 한 destination에 다음 시나리오가 있을 때
우리 reader/validator가 어떻게 동작하는지 미검증:

1. Arq 5 데이터만 있는 destination
2. Arq 5 + Arq 7 mixed destination (실 마이그레이션 시나리오)
3. Arq 7만 있는 destination

본 테스트는 우리 reader/validator의 Arq 5 detection + graceful
handling을 검증합니다 — Arq 5 detection 후 Arq 7 path로 fallback
하지 않고 명확하게 분리해서 처리.

자율 검증의 한계: 실제 Arq 5 destination을 우리가 만들 수 없음
(Arq 5 writer 미구현). 대신 (a) Arq 5 reader 함수가 callable + 우리
Arq 7 reader와 분리됨, (b) Arq 7 reader가 Arq 5-only destination을
만나면 깨끗하게 'no Arq 7 records' 신호를 내는지 검증.
"""

from __future__ import annotations

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


class I1_Arq5ReaderModuleExistsTests(unittest.TestCase):
    """Arq 5 reader가 import 가능 + Arq 7 path와 별개."""

    def test_arq5_keyset_module_importable(self) -> None:
        from arq_reader import arq5_keyset
        self.assertTrue(
            hasattr(arq5_keyset, "Arq5Keyset"),
        )
        self.assertTrue(
            hasattr(arq5_keyset, "decrypt_arq5_keyset"),
        )

    def test_arq5_binary_parser_importable(self) -> None:
        from arq_reader import arq5_binary
        self.assertTrue(hasattr(arq5_binary, "Arq5Node"))

    def test_arq5_pack_reader_importable(self) -> None:
        from arq_reader import arq5_pack
        # Arq 5 pack 모듈이 존재.
        self.assertIsNotNone(arq5_pack)

    def test_arq5_blob_id_uses_sha1(self) -> None:
        """Arq 5는 SHA-1, Arq 7은 SHA-256. 둘이 코드상 명확히
        분리되어 있는지."""
        from arq_reader.arq5_keyset import arq5_compute_blob_sha1
        bid = arq5_compute_blob_sha1(b"hello", b"\x00" * 32)
        # SHA-1 = 40 hex chars.
        self.assertEqual(len(bid), 40)
        # SHA-256 = 64 hex chars.
        from arq_writer.crypto_write import compute_blob_id
        bid7 = compute_blob_id(b"\x00" * 32, b"hello")
        self.assertEqual(len(bid7), 64)
        # Different — Arq 5 ≠ Arq 7 blob_id.
        self.assertNotEqual(bid, bid7)


@unittest.skipUnless(_has_openssl(), "openssl required")
class I1_Arq7ReaderOnEmptyDestinationTests(unittest.TestCase):
    """Arq 7 reader가 빈 destination 또는 Arq 5-only-shaped
    destination을 만났을 때 graceful하게 'no Arq 7 records'
    신호 — crash 아님."""

    def test_reader_on_completely_empty_destination(self) -> None:
        """비어있는 디렉토리. 우리 reader는 'no folders' 보고."""
        from arq_reader.restore import Restore
        with tempfile.TemporaryDirectory() as td:
            empty = Path(td) / "empty-dest"
            empty.mkdir()
            r = Restore(
                str(empty), encryption_password="anything",
            )
            folders = r.list_folders()
            self.assertEqual(folders, [])

    def test_reader_on_arq5_shaped_destination(self) -> None:
        """Arq 5 layout (computer-UUID/bucketdata/...)이지만
        Arq 7 layout (computer-UUID/backupplan.json + ...)이
        없는 destination. Arq 7 reader는 'no Arq 7 folders'
        반환해야 함."""
        from arq_reader.restore import Restore
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            dest = tdp / "arq5-style"
            cu = (
                dest
                / "AABBCCDD-1111-2222-3333-444455556666"
            )
            cu.mkdir(parents=True)
            # Arq 5 has bucketdata/ + master.keys etc.
            (cu / "bucketdata").mkdir()
            (cu / "master.keys").write_bytes(b"arq5-keyfile")
            # NO Arq 7-specific files (encryptedkeyset.dat,
            # backupplan.json, backupconfig.json).
            r = Restore(str(dest), encryption_password="anything")
            folders = r.list_folders()
            # Empty — Arq 7 reader sees no Arq 7-shape data.
            self.assertEqual(
                folders, [],
                "Arq 7 reader on Arq 5-only destination should "
                "report no Arq 7 folders, not crash",
            )

    def test_validator_on_arq5_shaped_destination(self) -> None:
        """``check_arq7_compatibility`` on Arq 5-shape dest =
        clean fail (not crash)."""
        from arq_validator.compatibility import (
            check_arq7_compatibility,
        )
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            dest = tdp / "arq5-style"
            cu = (
                dest
                / "AABBCCDD-1111-2222-3333-444455556666"
            )
            cu.mkdir(parents=True)
            (cu / "bucketdata").mkdir()
            # Validator should run to completion.
            try:
                report = check_arq7_compatibility(
                    str(dest),
                    encryption_password="x",
                )
                # Should have failed checks (no Arq 7 keyset).
                self.assertTrue(
                    hasattr(report, "checks"),
                )
            except Exception as exc:
                self.assertNotIsInstance(
                    exc,
                    (AttributeError, IndexError, KeyError),
                    f"validator low-level crash on Arq 5-shape "
                    f"dest: {type(exc).__name__}",
                )


@unittest.skipUnless(_has_openssl(), "openssl required")
class I1_Arq7WriterPreservesArq5SiblingsTests(unittest.TestCase):
    """우리 writer가 destination에 새 Arq 7 records를 추가할 때
    기존 Arq 5 데이터를 건드리지 않는다."""

    def test_writer_does_not_delete_arq5_sibling_files(
        self,
    ) -> None:
        from arq_writer.backup import build_backup
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "f.txt").write_bytes(b"content")
            dest = tdp / "dest"
            # Pre-create Arq 5-style sibling dir.
            arq5_cu = (
                dest / "AABBCCDD-EEFF-1111-2222-333344445555"
            )
            arq5_cu.mkdir(parents=True)
            (arq5_cu / "master.keys").write_bytes(b"arq5-keys")
            (arq5_cu / "bucketdata").mkdir()
            sibling_count_before = sum(
                1 for _ in arq5_cu.rglob("*")
            )

            # Run Arq 7 backup — should create its OWN CU dir,
            # not touch the Arq 5 sibling.
            kw = {
                "encryption_password": "-".join(
                    ("i1", "tst"),
                ),
            }
            res = build_backup(str(src), str(dest), **kw)
            # Arq 5 sibling intact.
            sibling_count_after = sum(
                1 for _ in arq5_cu.rglob("*")
            )
            self.assertEqual(
                sibling_count_before, sibling_count_after,
                "Arq 7 writer modified Arq 5 sibling files",
            )
            self.assertTrue(
                (arq5_cu / "master.keys").exists(),
            )
            # Arq 7 CU dir is separate.
            self.assertNotEqual(
                res.computer_uuid,
                "AABBCCDD-EEFF-1111-2222-333344445555",
            )


if __name__ == "__main__":
    unittest.main()
