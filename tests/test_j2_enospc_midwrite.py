"""J2 — ENOSPC (No space left on device) during pack write.

Disk full 시나리오. 우리 writer의 atomic temp+rename (N7,
PR #163)은 SIGKILL은 처리하지만 ENOSPC은 별개 — write가 명시적
에러로 surface돼야 함.

자율 모드 시뮬레이션: monkey-patch ``Path.write_bytes`` 또는
backend's ``write_all``로 ENOSPC을 raise. 우리 writer가 이를
어떻게 처리하는지 검증.

- 깔끔한 OSError 전파 (EBADF/ENOSPC errno로 식별 가능)
- destination에 truncated pack 안 남김 (N7 atomic 보장)
- 다음 backup attempt가 깨끗하게 동작 가능

진짜 disk-full 시뮬레이션(loop device + size limit)은 macOS에서
직접 불가능; tmpfs도 macOS 미지원. 그래서 monkey-patch가 가장
가까운 검증.
"""

from __future__ import annotations

import errno
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


def _has_openssl() -> bool:
    try:
        subprocess.run(
            ["openssl", "version"],
            check=True, capture_output=True, timeout=5,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


@unittest.skipUnless(_has_openssl(), "openssl required")
class J2_ENOSPC_During_Pack_Write_Tests(unittest.TestCase):

    def test_enospc_during_write_all_propagates_cleanly(
        self,
    ) -> None:
        """LocalBackend.write_all이 ENOSPC을 만나면 우리 writer는
        OSError(errno=ENOSPC)을 전파해야 함 (silent 무시 금지)."""
        from arq_writer.backup import build_backup
        from arq_validator.backend import LocalBackend

        original_write_all = LocalBackend.write_all
        nth_call = {"n": 0}

        def _failing_write_all(self, path, data):
            nth_call["n"] += 1
            # Let keyset write succeed; fail on 3rd call
            # (deep in pack-write or backuprecord-write).
            if nth_call["n"] == 3:
                raise OSError(
                    errno.ENOSPC,
                    os.strerror(errno.ENOSPC),
                    str(self.root) + path,
                )
            return original_write_all(self, path, data)

        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "a.bin").write_bytes(b"X" * 10_000)
            (src / "b.bin").write_bytes(b"Y" * 10_000)
            dest = tdp / "dest"
            kw = {
                "encryption_password": "-".join(
                    ("j2", "tst"),
                ),
            }
            with mock.patch.object(
                LocalBackend, "write_all", _failing_write_all,
            ):
                with self.assertRaises(OSError) as ctx:
                    build_backup(str(src), str(dest), **kw)
                # Specific errno: ENOSPC.
                self.assertEqual(
                    ctx.exception.errno, errno.ENOSPC,
                    f"expected ENOSPC propagated, got "
                    f"{ctx.exception.errno}",
                )

    def test_partial_pack_does_not_leak_at_final_path(
        self,
    ) -> None:
        """ENOSPC 발생 후 destination에 .tmp.* 파일은 있을 수
        있지만 *.pack final-path에 truncated 파일은 없어야 함
        (N7 atomic 보장)."""
        from arq_writer.backup import build_backup
        from arq_validator.backend import LocalBackend

        original_write_all = LocalBackend.write_all
        nth_call = {"n": 0}

        def _enospc_after_n(self, path, data):
            nth_call["n"] += 1
            if nth_call["n"] >= 5:
                raise OSError(
                    errno.ENOSPC, "no space", path,
                )
            return original_write_all(self, path, data)

        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            for i in range(10):
                (src / f"f{i:02d}.bin").write_bytes(
                    b"X" * 5_000,
                )
            dest = tdp / "dest"
            kw = {
                "encryption_password": "-".join(
                    ("j2", "tst"),
                ),
            }
            with mock.patch.object(
                LocalBackend, "write_all", _enospc_after_n,
            ):
                try:
                    build_backup(str(src), str(dest), **kw)
                except OSError:
                    pass
            # No .pack files with truncated ARQO at final
            # paths. (N7 atomic guarantees this.)
            partial_packs = []
            for p in dest.rglob("*.pack"):
                # Each surviving .pack file should be readable
                # via reconstruct_index without crash.
                from arq_reader.pack import reconstruct_index
                try:
                    raw = p.read_bytes()
                    entries = reconstruct_index(raw)
                    if not entries and raw:
                        partial_packs.append(p)
                except Exception:
                    partial_packs.append(p)
            self.assertEqual(
                partial_packs, [],
                f"ENOSPC left {len(partial_packs)} partial pack(s) "
                f"at final paths: "
                f"{[str(p.relative_to(dest)) for p in partial_packs[:3]]}",
            )


if __name__ == "__main__":
    unittest.main()
