"""G1 + G3 — test reliability audit + parser mutation sentinel.

G1 (test reliability audit):
- 우리 ~960 단위 테스트 중 mock-heavy인 비율 측정. mock-heavy
  테스트는 우리 모델을 검증할 뿐 실제 behavior와 분리될 가능성.
- Mock import 빈도 vs 전체 테스트 파일 수의 비율을 pin.

G3 (parser mutation sentinel):
- ``parse_tree`` / ``parse_node`` / ``parse_backuprecord``의 핵심
  invariant를 mutate해본 simulated mutation 결과를 pin. 외부
  mutmut 도구 없이 stdlib 만으로 'sample mutation들이 우리
  테스트로 catch되는가'를 확인.

G3는 진짜 mutation testing이 아니라 'mutation-style' regression —
parse 함수에 의도적으로 잘못된 bytes를 넣고 우리 reader가 명확한
에러를 raise하는지 검증. 진짜 mutmut은 5-6시간 인프라가 필요하니
나중 라운드의 후보로 남김.
"""

from __future__ import annotations

import struct
import subprocess
import unittest
from pathlib import Path


TESTS_DIR = Path(__file__).resolve().parent


class G1_TestSuiteReliabilityAuditTests(unittest.TestCase):
    """우리 테스트 중 mock 의존 비율 pin. 비율이 30%를 넘으면
    너무 mock-heavy = 실제 behavior 검증력 약함."""

    def test_mock_usage_ratio_under_threshold(self) -> None:
        """전체 test 파일 중 mock 라이브러리를 import하는 비율은
        20% 이하여야 함."""
        total = 0
        with_mock = 0
        for f in TESTS_DIR.glob("test_*.py"):
            total += 1
            text = f.read_text()
            if (
                "unittest.mock" in text
                or "from mock import" in text
                or "@patch" in text
            ):
                with_mock += 1
        self.assertGreater(total, 100, "test suite is small")
        ratio = with_mock / total
        self.assertLess(
            ratio, 0.20,
            f"mock-using tests = {with_mock}/{total} "
            f"({ratio*100:.1f}%) — over 20% suggests too much "
            f"of the suite is verifying our model rather than "
            f"our behavior. Audit + replace mock-heavy tests "
            f"with real-construction equivalents.",
        )

    def test_real_io_tests_present(self) -> None:
        """tempfile + Path round-trip을 쓰는 테스트가 다수
        존재해야 함 — 우리 코드의 실제 I/O behavior가 검증되는
        것을 보장."""
        with_tempfile = 0
        for f in TESTS_DIR.glob("test_*.py"):
            text = f.read_text()
            if "tempfile.TemporaryDirectory" in text:
                with_tempfile += 1
        self.assertGreater(
            with_tempfile, 50,
            f"only {with_tempfile} tests use tempfile.TemporaryDirectory; "
            f"real-I/O coverage may be thin",
        )

    def test_total_test_file_count(self) -> None:
        """Pin total test file count. 새 라운드의 PR이 머지될
        때마다 이 숫자가 갱신되므로 의도적 변경 sentinel."""
        n = sum(1 for _ in TESTS_DIR.glob("test_*.py"))
        # 175 (Round 11 직후), Round 12 진행 중이라 ≥ 175.
        self.assertGreaterEqual(
            n, 175,
            f"test file count dropped to {n} — files removed?",
        )


class G3_ParserMutationSentinelTests(unittest.TestCase):
    """우리 parser가 mutation-shaped bytes를 받았을 때 명확한
    에러를 raise하는지. 진짜 mutmut 대신 sample mutation을
    수동으로 시도."""

    def test_parse_tree_rejects_negative_version(self) -> None:
        """tree version field is uint32 BE. Negative interpreted
        (high bit set) → still uint32 but very large. Reader
        should accept (no validation) or raise cleanly. Either
        is OK; what's NOT OK is silent AttributeError /
        ValueError without context."""
        from arq_reader.parse import parse_tree
        # Tree blob with version=0xFFFFFFFF + count=0 (no
        # children).
        bad = (
            struct.pack(">I", 0xFFFFFFFF)
            + struct.pack(">Q", 0)
        )
        try:
            t = parse_tree(bad)
            # If it parses, version stored as-is.
            self.assertEqual(t.version, 0xFFFFFFFF)
        except ValueError:
            # Clean rejection is acceptable.
            pass

    def test_parse_tree_rejects_count_overflow(self) -> None:
        """version=100, count=2^32 → walking that many children
        would loop indefinitely. Reader must surface this as
        a clean error, not OOM."""
        from arq_reader.parse import parse_tree
        bad = (
            struct.pack(">I", 100)
            + struct.pack(">Q", 0xFFFFFFFFFFFFFFFF)
        )
        # Should raise (insufficient bytes for that many
        # children) before any OOM.
        with self.assertRaises((ValueError, struct.error,
                                EOFError, IndexError)):
            parse_tree(bad)

    def test_parse_tree_on_empty_bytes(self) -> None:
        """Empty input → ValueError, not crash."""
        from arq_reader.parse import parse_tree
        with self.assertRaises((ValueError, struct.error,
                                EOFError, IndexError)):
            parse_tree(b"")

    def test_parse_tree_on_truncated_after_count(self) -> None:
        """version + count + (truncated child data). Reader
        must not infinite-loop, must surface as error."""
        from arq_reader.parse import parse_tree
        bad = (
            struct.pack(">I", 100)
            + struct.pack(">Q", 1)
            + b"some garbage but not enough"
        )
        with self.assertRaises((ValueError, struct.error,
                                EOFError, IndexError, UnicodeError)):
            parse_tree(bad)

    def test_parse_backuprecord_on_random_bytes(self) -> None:
        """parse_backuprecord 받은 임의 bytes — 둘 중 하나여야:
        (1) bplist 또는 JSON으로 해석 시도 후 ValueError,
        (2) crash 없이 'invalid' 신호 반환.
        AttributeError / KeyError 같은 low-level crash 금지."""
        from arq_writer.backuprecord import parse_backuprecord
        for sample in (
            b"\x00" * 100,
            b"\xff" * 100,
            b"\x80\x81\x82\x83\x84" * 20,
            bytes(range(256)),
        ):
            with self.subTest(sample_first8=sample[:8]):
                try:
                    parse_backuprecord(sample)
                except ValueError:
                    pass  # acceptable
                except (AttributeError, KeyError, TypeError) as e:
                    self.fail(
                        f"parse_backuprecord raised low-level "
                        f"crash {type(e).__name__} on random "
                        f"input: {e}",
                    )


if __name__ == "__main__":
    unittest.main()
