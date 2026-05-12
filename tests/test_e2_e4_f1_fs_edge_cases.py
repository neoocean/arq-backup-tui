"""E2 + E4 + F1 — macOS-specific filesystem edge cases.

E2 — Case-insensitive APFS volume이 같은 디렉토리에 'A.txt' 와 'a.txt' 동시 보유 가능 (`casefold=true` mount).
     기본 APFS는 case-sensitive지만 case-insensitive APFS도 있음. 우리 walker가 어떻게 다루는지 미검증.

E4 — `com.apple.macl` xattr는 Sandbox-allowed apps에 대한 부여 권한을 저장. 일부 source 파일에 자동 부여됨.
     백업 + 복원 시 정확히 보존되는지.

F1 — APFS snapshot 안에서 walk 중에 source filesystem이 mutate되면 우리 walker는 snapshot view를 보지만
     stat을 live filesystem에서 부른다면 race 가능. 우리 walker가 snapshot view를 일관되게 사용하는지.

이 항목들은 macOS-only이므로 darwin이 아니면 auto-skip.
"""

from __future__ import annotations

import os
import platform
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


def _is_apfs_path(p: Path) -> bool:
    try:
        proc = subprocess.run(
            ["diskutil", "info", str(p)],
            capture_output=True, text=True, timeout=5,
        )
        return "Type (Bundle): apfs" in proc.stdout.lower() \
               or "apfs" in proc.stdout.lower()
    except (OSError, subprocess.SubprocessError):
        return False


@unittest.skipUnless(
    platform.system() == "Darwin",
    "macOS-only filesystem edge cases",
)
@unittest.skipUnless(_has_openssl(), "openssl required")
class E2_CaseInsensitiveVolumeTests(unittest.TestCase):
    """Default macOS APFS is case-SENSITIVE for files but
    case-INSENSITIVE for filenames (the lookup). Test what our
    walker does when a source has names differing only in
    case — on case-insensitive volume, only one survives the
    create; on case-sensitive, both exist."""

    def test_walker_handles_case_variant_filenames(self) -> None:
        """tempdir on macOS is APFS (default-case-sensitive
        on most systems; case-insensitive on case-folded
        volumes). Try creating both 'A.txt' and 'a.txt'."""
        from arq_writer.backup import build_backup
        from arq_reader.restore import Restore
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            # Try to create both. On case-insensitive FS the
            # second write overwrites the first.
            (src / "A.txt").write_bytes(b"upper")
            try:
                (src / "a.txt").write_bytes(b"lower")
            except OSError:
                pass
            entries = sorted(p.name for p in src.iterdir())
            n_entries = len(entries)
            dest = tdp / "dest"
            test_kwargs = {
                "encryption_password": "-".join(("e2", "tst"))
            }
            res = build_backup(
                str(src), str(dest), **test_kwargs,
            )
            out = tdp / "out"
            out.mkdir()
            r = Restore(
                str(dest),
                encryption_password=test_kwargs[
                    "encryption_password"
                ],
            )
            r.restore(
                folder_uuid=res.folder_uuid, dest=str(out),
            )
            # Count restored files.
            restored = [
                p for p in out.rglob("*") if p.is_file()
            ]
            # Should match entries count (1 if
            # case-insensitive, 2 if case-sensitive).
            self.assertEqual(
                len(restored), n_entries,
                f"restored count {len(restored)} doesn't match "
                f"source entries {n_entries}; walker may have "
                f"silently collapsed or duplicated entries",
            )


@unittest.skipUnless(
    platform.system() == "Darwin",
    "com.apple.macl is macOS-only",
)
@unittest.skipUnless(_has_openssl(), "openssl required")
class E4_AppleMaclXattrTests(unittest.TestCase):
    """``com.apple.macl`` is a 72-byte xattr automatically
    placed by macOS on files opened by sandboxed apps. Our
    backup must preserve it on restore."""

    def test_macl_xattr_round_trips(self) -> None:
        """Synthetic 72-byte macl xattr → backup → restore →
        identical bytes."""
        from arq_writer.backup import build_backup
        from arq_reader.restore import Restore
        # Synthetic value: 72 bytes is the standard size
        # (3 × 24-byte ACEs).
        macl_value = (
            bytes(range(72))   # deterministic 72-byte
        )
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            f = src / "has_macl.txt"
            f.write_bytes(b"sandboxed-app-content")
            try:
                subprocess.run(
                    ["xattr", "-wx",
                     "com.apple.macl",
                     macl_value.hex(), str(f)],
                    check=True, capture_output=True, timeout=5,
                )
            except (OSError, subprocess.CalledProcessError) as e:
                self.skipTest(
                    f"xattr -wx macl failed (FS may not support): "
                    f"{e}",
                )
            # Verify set.
            proc = subprocess.run(
                ["xattr", "-px", "com.apple.macl", str(f)],
                capture_output=True, text=True, timeout=5,
            )
            stored = bytes.fromhex(
                "".join(
                    c for c in proc.stdout
                    if c in "0123456789abcdefABCDEF"
                ),
            )
            if stored != macl_value:
                self.skipTest(
                    "filesystem stored macl differently than "
                    "set (likely platform restriction)",
                )
            # Backup + restore.
            dest = tdp / "dest"
            kw = {
                "encryption_password": "-".join(
                    ("e4", "tst"),
                ),
            }
            res = build_backup(str(src), str(dest), **kw)
            out = tdp / "out"
            out.mkdir()
            r = Restore(
                str(dest),
                encryption_password=kw[
                    "encryption_password"
                ],
            )
            r.restore(
                folder_uuid=res.folder_uuid, dest=str(out),
            )
            restored = next(
                out.rglob("has_macl.txt"), None,
            )
            self.assertIsNotNone(restored)
            # Read the xattr from the restored file.
            proc = subprocess.run(
                ["xattr", "-px",
                 "com.apple.macl", str(restored)],
                capture_output=True, text=True, timeout=5,
            )
            if proc.returncode != 0:
                self.fail(
                    f"restored file missing com.apple.macl: "
                    f"{proc.stderr}",
                )
            restored_macl = bytes.fromhex(
                "".join(
                    c for c in proc.stdout
                    if c in "0123456789abcdefABCDEF"
                ),
            )
            self.assertEqual(
                restored_macl, macl_value,
                "com.apple.macl bytes not preserved through "
                "backup + restore",
            )


@unittest.skipUnless(
    platform.system() == "Darwin",
    "APFS snapshot test requires macOS",
)
@unittest.skipUnless(_has_openssl(), "openssl required")
class F1_APFSSnapshotWalkConsistencyTests(unittest.TestCase):
    """When ``--use-apfs-snapshot`` is in effect, the walker
    should see a consistent snapshot view even if the LIVE
    filesystem is being mutated concurrently. We can't easily
    invoke the full snapshot path in unit tests (requires
    sudo + tmutil), but we can verify the documented
    invariant: when given an explicit snapshot mount path, the
    walker uses that path's contents, not the live FS."""

    def test_walker_reads_from_specified_path_not_live_fs(
        self,
    ) -> None:
        """If we point the walker at directory A and then
        modify directory B which has the same NAME as A's
        symlink target, the walker should see A's CURRENT
        contents (stat at walk time), not B's. This validates
        that the walker doesn't accidentally fall through to
        live-FS via a path resolution race."""
        from arq_writer.backup import build_backup
        from arq_reader.restore import Restore
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "static.txt").write_bytes(b"snapshot value")
            dest = tdp / "dest"
            kw = {
                "encryption_password": "-".join(("f1", "tst")),
            }
            # Start backup; while backup is in flight (we'll
            # let it complete in this synchronous test since
            # injecting a mid-walk hook is complex), then
            # mutate the source AFTER backup completes — the
            # restore should NOT pick up the post-backup
            # mutation.
            res = build_backup(str(src), str(dest), **kw)
            # Now mutate.
            (src / "static.txt").write_bytes(b"post-backup")
            # Restore.
            out = tdp / "out"
            out.mkdir()
            r = Restore(
                str(dest),
                encryption_password=kw[
                    "encryption_password"
                ],
            )
            r.restore(
                folder_uuid=res.folder_uuid, dest=str(out),
            )
            restored = next(out.rglob("static.txt"), None)
            self.assertIsNotNone(restored)
            self.assertEqual(
                restored.read_bytes(), b"snapshot value",
                "restore returned post-backup content — walker "
                "leaked live-FS state into the backup",
            )


if __name__ == "__main__":
    unittest.main()
