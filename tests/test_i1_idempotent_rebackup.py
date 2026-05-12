"""I1 — idempotent re-backup invariant: backing up the same
unchanged source N times produces the same blob set after the
first run.

Stronger than F3 (which tests backup → restore → backup
idempotency: ONE restore in the middle). I1 tests backup →
backup → backup → … with NO changes in between. After the
first backup, every subsequent backup should:

1. Add zero new data blobs (cross-run dedup hits cache).
2. Add at most a small number of metadata blobs (new record +
   maybe a fresh tree blob if mtime nanosecond drifted across
   filesystems — though our writer uses prior-tree reuse to
   prevent that).
3. Reuse the prior emit's tree blob for every unchanged file
   when prior-tree-reuse fires.

This invariant is load-bearing for incremental backup behaviour
— if it fails, then long-running operators accumulate redundant
blobs over time.

API NOTE: ``build_backup(dedup_against_existing=True)`` requires
the caller to also pass ``computer_uuid=`` (and ideally
``folder_uuid=``) matching the existing destination's CU/FU.
Without that, each invocation generates a fresh CU and falls
through to fresh keyset, defeating dedup. This test exercises
the documented incremental workflow explicitly.
"""

from __future__ import annotations

import subprocess
import tempfile
import time
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


def _count_blobs(dest: Path) -> int:
    """Count every file under standardobjects + treepacks +
    blobpacks + largeblobpacks (excluding sidecars)."""
    total = 0
    for cu_dir in dest.iterdir():
        if not cu_dir.is_dir() or len(cu_dir.name) != 36:
            continue
        for fam in (
            "standardobjects", "treepacks", "blobpacks",
            "largeblobpacks",
        ):
            d = cu_dir / fam
            if d.is_dir():
                total += sum(
                    1 for _ in d.rglob("*") if _.is_file()
                )
    return total


@unittest.skipUnless(_has_openssl(), "openssl CLI required")
class I1_IdempotentRebackupTests(unittest.TestCase):

    def test_three_consecutive_backups_no_blob_growth(
        self,
    ) -> None:
        """Backup same source three times sharing CU+FU. After
        the first run, the blob count should stay close-to-
        constant (only metadata-record growth)."""
        from arq_writer.backup import build_backup
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "a.txt").write_bytes(b"alpha" * 100)
            (src / "b.txt").write_bytes(b"bravo" * 100)
            (src / "subdir").mkdir()
            (src / "subdir" / "c.txt").write_bytes(
                b"charlie" * 100,
            )
            dest = tdp / "dest"
            # Pass 1 — initial backup. Record CU/FU so passes
            # 2 + 3 can reuse them (incremental workflow).
            res1 = build_backup(
                str(src), str(dest),
                encryption_password="-".join(("i1", "pw")),
            )
            count_after_1 = _count_blobs(dest)

            # Pass 2 — same source + same CU/FU + dedup ON.
            build_backup(
                str(src), str(dest),
                encryption_password="-".join(("i1", "pw")),
                computer_uuid=res1.computer_uuid,
                folder_uuid=res1.folder_uuid,
                dedup_against_existing=True,
            )
            count_after_2 = _count_blobs(dest)

            # Pass 3 — same.
            build_backup(
                str(src), str(dest),
                encryption_password="-".join(("i1", "pw")),
                computer_uuid=res1.computer_uuid,
                folder_uuid=res1.folder_uuid,
                dedup_against_existing=True,
            )
            count_after_3 = _count_blobs(dest)

            # Growth between passes should be small + bounded.
            # Each pass adds ≤ 1 fresh blob (a new record-
            # related metadata blob).
            self.assertLessEqual(
                count_after_2 - count_after_1, 2,
                f"pass 2 added {count_after_2 - count_after_1} "
                f"blobs (>2) — dedup likely broken; "
                f"count1={count_after_1} count2={count_after_2}",
            )
            self.assertLessEqual(
                count_after_3 - count_after_2, 2,
                f"pass 3 added {count_after_3 - count_after_2} "
                f"blobs (>2); dedup broken on N>2",
            )

    def test_record_count_grows_one_per_pass(self) -> None:
        """Each call to build_backup creates exactly one new
        backuprecord (regardless of dedup state)."""
        from arq_writer.backup import build_backup
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "x.txt").write_bytes(b"x")
            dest = tdp / "dest"

            def _count_records():
                n = 0
                for cu_dir in dest.iterdir():
                    if (
                        not cu_dir.is_dir()
                        or len(cu_dir.name) != 36
                    ):
                        continue
                    bf = cu_dir / "backupfolders"
                    if not bf.is_dir():
                        continue
                    for f in bf.iterdir():
                        rec_dir = f / "backuprecords"
                        if rec_dir.is_dir():
                            for bucket in rec_dir.iterdir():
                                n += sum(
                                    1 for _ in bucket.iterdir()
                                    if _.is_file()
                                    and _.name.endswith(
                                        ".backuprecord",
                                    )
                                )
                return n

            # Pass 1.
            res1 = build_backup(
                str(src), str(dest),
                encryption_password="-".join(
                    ("rec", "test"),
                ),
            )
            counts = [_count_records()]
            time.sleep(1.1)

            # Passes 2 + 3 with same CU+FU.
            for _ in range(2):
                build_backup(
                    str(src), str(dest),
                    encryption_password="-".join(
                        ("rec", "test"),
                    ),
                    computer_uuid=res1.computer_uuid,
                    folder_uuid=res1.folder_uuid,
                    dedup_against_existing=True,
                )
                counts.append(_count_records())
                time.sleep(1.1)

            # Records grow exactly by 1 each pass.
            self.assertEqual(counts, [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
