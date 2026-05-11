"""A보완-3 — file_skipped event reason taxonomy.

The walker emits ``file_skipped`` events with a ``reason``
field whenever an entry is omitted from the backup. Pinning the
full taxonomy here so subscribers (TUI activity log, run-summary
formatter, audit tooling) know exactly which reasons to handle.

Current taxonomy (sampled 2026-05-11 from
``arq_writer/backup.py``):

| reason | Trigger | Additional payload keys |
|---|---|---|
| ``tm_excluded`` | Source carries ``com.apple.metadata:com_apple_backup_excludeItem`` xattr + ``skip_tm_excludes=False`` | (none) |
| ``special_file`` | Entry is FIFO / socket / char-device / block-device | ``special_kind``, ``mode`` |
| ``size_limit`` | File size > ``max_file_bytes`` | ``size``, ``limit`` |

All ``file_skipped`` events carry the common ``path`` +
``rel_path`` payload regardless of reason. New reasons must be
documented here AND in ``docs/PLAN-cli-tui-split.md`` event
taxonomy (the canonical reference for callback subscribers).
"""

from __future__ import annotations

import os
import platform
import socket
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


KNOWN_REASONS = frozenset({
    "tm_excluded",
    "special_file",
    "size_limit",
})


@unittest.skipUnless(_has_openssl(), "openssl CLI required")
class FileSkippedTaxonomyTests(unittest.TestCase):

    def _collect_skip_events(self, source_setup, **backup_kwargs):
        """Build a backup with the given source setup; return all
        emitted file_skipped events."""
        from arq_writer.backup import build_backup
        events = []
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            source_setup(src)
            build_backup(
                str(src), str(tdp / "dest"),
                encryption_password="pw",
                callback=lambda k, p: events.append((k, p)),
                **backup_kwargs,
            )
        return [
            p for (k, p) in events if k == "file_skipped"
        ]

    def test_size_limit_reason_emits_with_size_and_limit(self) -> None:
        def setup(src):
            (src / "big.bin").write_bytes(b"X" * 2000)
            (src / "small.bin").write_bytes(b"x")
        skips = self._collect_skip_events(
            setup, max_file_bytes=100,
        )
        size_limit = [p for p in skips if p["reason"] == "size_limit"]
        self.assertEqual(len(size_limit), 1)
        self.assertEqual(size_limit[0]["size"], 2000)
        self.assertEqual(size_limit[0]["limit"], 100)
        # rel_path + path always present.
        self.assertIn("path", size_limit[0])
        self.assertIn("rel_path", size_limit[0])

    @unittest.skipUnless(
        not platform.system().startswith("Win"),
        "POSIX file types only",
    )
    def test_special_file_reason_emits_kind_and_mode(self) -> None:
        def setup(src):
            os.mkfifo(str(src / "myfifo"))
            (src / "normal.txt").write_bytes(b"normal")
        skips = self._collect_skip_events(setup)
        special = [
            p for p in skips if p["reason"] == "special_file"
        ]
        self.assertEqual(len(special), 1)
        self.assertEqual(special[0]["special_kind"], "fifo")
        self.assertIn("mode", special[0])
        # Mode in octal string form for readability.
        self.assertTrue(special[0]["mode"].startswith("0o"))

    def test_tm_excluded_reason_emits_minimal_payload(self) -> None:
        def setup(src):
            normal = src / "normal.txt"
            normal.write_bytes(b"normal")
            tm = src / "tm.txt"
            tm.write_bytes(b"y")
            if platform.system() == "Darwin":
                subprocess.run(
                    ["xattr", "-wx",
                     "com.apple.metadata:com_apple_backup_excludeItem",
                     "62706c697374303001",
                     str(tm)],
                    capture_output=True,
                )
            else:
                try:
                    os.setxattr(
                        str(tm),
                        "com.apple.metadata:com_apple_backup_excludeItem",
                        b"\x00",
                    )
                except OSError:
                    pass
        skips = self._collect_skip_events(setup)
        tm = [p for p in skips if p["reason"] == "tm_excluded"]
        if not tm:
            self.skipTest(
                "this filesystem doesn't support the TM-exclude xattr",
            )
        self.assertEqual(len(tm), 1)
        # No 'special_kind' / 'size' / 'limit' — tm_excluded keeps
        # only path + rel_path + reason.
        self.assertNotIn("special_kind", tm[0])
        self.assertNotIn("size", tm[0])
        self.assertNotIn("limit", tm[0])

    def test_all_reasons_are_in_known_taxonomy(self) -> None:
        """Belt-and-braces: any reason value emitted by the
        walker must appear in KNOWN_REASONS. If a future change
        adds a new reason, this test flags the need to update
        both this taxonomy + ``docs/PLAN-cli-tui-split.md``."""
        def setup(src):
            # Trigger every reason we can in one run.
            (src / "big.bin").write_bytes(b"X" * 5000)
            if not platform.system().startswith("Win"):
                os.mkfifo(str(src / "fifo"))
            (src / "normal.txt").write_bytes(b"x")
        skips = self._collect_skip_events(
            setup, max_file_bytes=100,
        )
        seen_reasons = {p["reason"] for p in skips}
        unknown = seen_reasons - KNOWN_REASONS
        self.assertEqual(
            unknown, set(),
            f"unknown skip reasons emitted: {unknown}. Update "
            f"KNOWN_REASONS + docs/PLAN-cli-tui-split.md",
        )


if __name__ == "__main__":
    unittest.main()
