"""N10 — writer emit invariance under locale / timezone changes.

Latent bugs in this class:
- ``de_DE`` locale renders 1.5 as "1,5" (decimal-comma).
- ``tr_TR`` locale lowercases ``I`` → ``ı`` (dotless-i), so
  a Turkish-locale program lowercasing 'IsoVersion' would
  produce 'ısoversion'.
- ``LC_ALL=C`` changes NSDate string formatting.
- Timezone changes shift ``localtime`` but ``time.time()``
  is invariant.

Our writer should be locale + timezone invariant — backups
created on a Korean machine should produce the same bytes as
backups on a US machine. N10 pins this by running the emit
path under multiple locales × timezones and SHA-256-hashing
the emit bytes.

Auto-skips locale-specific assertions when the locale isn't
installed on the system.
"""

from __future__ import annotations

import hashlib
import json
import locale
import os
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


def _locale_available(loc: str) -> bool:
    try:
        locale.setlocale(locale.LC_ALL, loc)
        # Restore default after probe.
        locale.setlocale(locale.LC_ALL, "")
        return True
    except locale.Error:
        return False


class N10_BackupPlanLocaleInvarianceTests(unittest.TestCase):
    """build_backupplan output bytes invariant across locales."""

    LOCALES = ("C", "en_US.UTF-8", "ko_KR.UTF-8", "tr_TR.UTF-8")

    def _capture_emit(self, loc: str) -> bytes:
        from arq_writer.json_configs import build_backupplan
        original = locale.setlocale(locale.LC_ALL, None)
        try:
            try:
                locale.setlocale(locale.LC_ALL, loc)
            except locale.Error:
                self.skipTest(
                    f"locale {loc} not installed on system",
                )
            plan = build_backupplan(
                plan_uuid="00000000-0000-0000-0000-000000000000",
                plan_name="locale-test-plan",
                folder_plans=[],
                creation_time=1700000000.5,
                update_time=1700001234.5,
            )
            return json.dumps(
                plan, ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        finally:
            try:
                locale.setlocale(locale.LC_ALL, original)
            except locale.Error:
                locale.setlocale(locale.LC_ALL, "")

    def test_all_locales_produce_byte_identical_plan(
        self,
    ) -> None:
        hashes: dict[str, str] = {}
        for loc in self.LOCALES:
            if not _locale_available(loc):
                continue
            emit = self._capture_emit(loc)
            hashes[loc] = hashlib.sha256(emit).hexdigest()
        if len(hashes) < 2:
            self.skipTest(
                "fewer than 2 locales installed for comparison",
            )
        # All hashes equal.
        baseline = next(iter(hashes.values()))
        for loc, h in hashes.items():
            self.assertEqual(
                h, baseline,
                f"locale {loc} produced different emit bytes "
                f"({h[:16]}...) vs baseline ({baseline[:16]}...) "
                f"— locale-dependent encoding bug",
            )

    def test_creationtime_int_truncation_locale_invariant(
        self,
    ) -> None:
        """D1's float→int truncation: 1700000000.5 → 1700000000
        regardless of locale. (Catches a hypothetical
        locale-aware int() that uses ',' as decimal separator.)"""
        from arq_writer.json_configs import build_backupplan
        original = locale.setlocale(locale.LC_ALL, None)
        try:
            for loc in self.LOCALES:
                if not _locale_available(loc):
                    continue
                locale.setlocale(locale.LC_ALL, loc)
                plan = build_backupplan(
                    plan_uuid="x", plan_name="y",
                    folder_plans=[],
                    creation_time=1700000000.5,
                )
                self.assertEqual(
                    plan["creationTime"], 1700000000,
                    f"locale {loc}: creationTime truncation "
                    f"broke",
                )
                self.assertIs(type(plan["creationTime"]), int)
        finally:
            try:
                locale.setlocale(locale.LC_ALL, original)
            except locale.Error:
                locale.setlocale(locale.LC_ALL, "")


class N10_BackupRecordTimezoneInvarianceTests(unittest.TestCase):
    """build_backuprecord_dict's creationDate is Unix-epoch
    seconds (UTC). Should be invariant across TZ."""

    TIMEZONES = ("UTC", "America/Los_Angeles", "Asia/Seoul")

    def _capture_record(self, tz: str) -> dict:
        from arq_writer.backuprecord import (
            build_backuprecord_dict,
        )
        from arq_writer.types import TreeNode, BlobLoc
        original_tz = os.environ.get("TZ", "")
        try:
            os.environ["TZ"] = tz
            # macOS / Python's time module reads TZ at module
            # init; force-refresh via tzset().
            import time as _t
            _t.tzset()
            return build_backuprecord_dict(
                backup_folder_uuid="x",
                backup_plan_uuid="y",
                backup_plan_dict={},
                root_node=TreeNode(
                    treeBlobLoc=BlobLoc(
                        blobIdentifier="aa" * 32,
                    ),
                ),
                local_path="/x",
                local_mount_point="/",
                volume_name="V",
                disk_identifier="D",
                creation_date=1700000000,  # explicit UTC seconds
            )
        finally:
            if original_tz:
                os.environ["TZ"] = original_tz
            else:
                os.environ.pop("TZ", None)
            import time as _t
            _t.tzset()

    def test_creation_date_invariant_across_timezones(
        self,
    ) -> None:
        records = {tz: self._capture_record(tz) for tz in self.TIMEZONES}
        base = records["UTC"]
        for tz, rec in records.items():
            self.assertEqual(
                rec["creationDate"], base["creationDate"],
                f"TZ={tz} produced different creationDate "
                f"({rec['creationDate']}) than UTC "
                f"({base['creationDate']}) — TZ-dependent "
                f"timestamp encoding bug",
            )

    def test_record_serialize_bytes_invariant_across_tz(
        self,
    ) -> None:
        from arq_writer.backuprecord import serialize_backuprecord
        hashes = {}
        for tz in self.TIMEZONES:
            rec = self._capture_record(tz)
            ser = serialize_backuprecord(rec, fmt="json")
            hashes[tz] = hashlib.sha256(ser).hexdigest()
        baseline = hashes["UTC"]
        for tz, h in hashes.items():
            self.assertEqual(
                h, baseline,
                f"TZ={tz} produced different serialized bytes "
                f"vs UTC",
            )


@unittest.skipUnless(_has_openssl(), "openssl CLI required")
class N10_FullBackupLocaleInvarianceTests(unittest.TestCase):
    """End-to-end backup → emit → SHA-256 should be identical
    across locale/TZ choices for a deterministic source."""

    def test_two_locale_passes_produce_same_blob_count(
        self,
    ) -> None:
        """Two independent backup runs (different keysets,
        different salts → different blob_ids) under different
        locales should still produce the SAME NUMBER of blobs
        for the same source content. Verifies the walker's
        decisions (chunking, dedup, file-skip) are locale-
        invariant, even though blob_id values themselves
        depend on the random per-keyset salt."""
        from arq_writer.backup import build_backup
        import random
        rng = random.Random(20260512)
        content = rng.randbytes(8192)

        def _count_blobs(loc, tz):
            with tempfile.TemporaryDirectory() as td:
                tdp = Path(td)
                src = tdp / "src"
                src.mkdir()
                (src / "f.bin").write_bytes(content)
                (src / "g.bin").write_bytes(content[:4096])
                dest = tdp / "dest"
                orig_lc = locale.setlocale(locale.LC_ALL, None)
                orig_tz = os.environ.get("TZ", "")
                try:
                    try:
                        locale.setlocale(locale.LC_ALL, loc)
                    except locale.Error:
                        return None
                    os.environ["TZ"] = tz
                    import time as _t
                    _t.tzset()
                    res = build_backup(
                        str(src), str(dest),
                        encryption_password="pw",
                    )
                    so = (
                        dest / res.computer_uuid /
                        "standardobjects"
                    )
                    return sum(
                        1 for _ in so.rglob("*") if _.is_file()
                    )
                finally:
                    try:
                        locale.setlocale(locale.LC_ALL, orig_lc)
                    except locale.Error:
                        locale.setlocale(locale.LC_ALL, "")
                    if orig_tz:
                        os.environ["TZ"] = orig_tz
                    else:
                        os.environ.pop("TZ", None)
                    import time as _t
                    _t.tzset()

        count_a = _count_blobs("C", "UTC")
        count_b = _count_blobs("ko_KR.UTF-8", "Asia/Seoul")
        if count_a is None or count_b is None:
            self.skipTest("required locales not installed")
        self.assertEqual(
            count_a, count_b,
            f"locale/TZ change produced different blob count "
            f"({count_a} vs {count_b}) for identical source — "
            f"locale-dependent walker decision suspected",
        )


if __name__ == "__main__":
    unittest.main()
