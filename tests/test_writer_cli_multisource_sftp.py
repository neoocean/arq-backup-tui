"""Tests for the writer CLI's multi-source + SFTP destination
support added in PR-C3.

The previous behaviour was: ``arq-backup create`` took ONE
positional source and required ``--dest <local-path>``. The TUI's
SubprocessBackupWorker therefore had to fall back to in-process
mode for SFTP destinations and multi-source plans — losing
parity between cron/systemd-driven backups and TUI-driven ones.

This change widens the CLI:

- ``source`` accepts one or more positional args.
- ``--dest`` becomes optional; ``--sftp-host`` / ``--sftp-port`` /
  ``--sftp-user`` / ``--sftp-path`` / ``--sftp-identity-file`` /
  ``--sftp-password-env`` provide the alternative.
- Exactly one of {``--dest``, ``--sftp-host``} must be set.

These tests pin the parser-level contract + the
``subprocess_eligible`` rule's relaxation. The actual SFTP run-
through is exercised by the integration tests against the
operator's real destination (separate file).
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


class CLIArgValidationTests(unittest.TestCase):

    def _parse(self, argv):
        from arq_writer.cli import _build_parser
        return _build_parser().parse_args(argv)

    def test_multi_source_positional_accepted(self) -> None:
        ns = self._parse([
            "create", "/srv/a", "/srv/b", "/srv/c",
            "--dest", "/mnt/dst",
        ])
        self.assertEqual(
            [str(p) for p in ns.source],
            ["/srv/a", "/srv/b", "/srv/c"],
        )

    def test_single_source_still_works(self) -> None:
        ns = self._parse([
            "create", "/srv/a",
            "--dest", "/mnt/dst",
        ])
        self.assertEqual([str(p) for p in ns.source], ["/srv/a"])

    def test_sftp_args_parse(self) -> None:
        ns = self._parse([
            "create", "/srv/a",
            "--sftp-host", "u504460.your-storagebox.de",
            "--sftp-port", "23",
            "--sftp-user", "u504460",
            "--sftp-path", "/home/u504460/arq",
            "--sftp-password-env", "ARQ_SFTP_PW",
        ])
        self.assertEqual(
            ns.sftp_host, "u504460.your-storagebox.de",
        )
        self.assertEqual(ns.sftp_port, 23)
        self.assertEqual(ns.sftp_user, "u504460")
        self.assertEqual(ns.sftp_path, "/home/u504460/arq")
        self.assertIsNone(ns.dest)


class CLIDestinationValidationTests(unittest.TestCase):
    """The CLI must reject {dest+sftp both set, neither set,
    sftp-host without sftp-path}. Each test parses + main()s."""

    def _make_dummy_source(self, td) -> Path:
        s = td / "src"
        s.mkdir()
        (s / "a.txt").write_text("x")
        return s

    def test_dest_and_sftp_host_both_set_rejected(self) -> None:
        from arq_writer.cli import main
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            src = self._make_dummy_source(td)
            rc = main([
                "create", str(src),
                "--dest", str(td / "d"),
                "--sftp-host", "example.invalid",
                "--password", "pw",
            ])
            self.assertEqual(rc, 2)

    def test_neither_dest_nor_sftp_rejected(self) -> None:
        from arq_writer.cli import main
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            src = self._make_dummy_source(td)
            rc = main([
                "create", str(src),
                "--password", "pw",
            ])
            self.assertEqual(rc, 2)

    def test_sftp_host_without_sftp_path_rejected(self) -> None:
        from arq_writer.cli import main
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            src = self._make_dummy_source(td)
            rc = main([
                "create", str(src),
                "--sftp-host", "example.invalid",
                "--sftp-user", "u",
                "--password", "pw",
            ])
            self.assertEqual(rc, 2)


@unittest.skipUnless(_has_openssl(), "openssl CLI required")
class MultiSourceLocalEndToEndTests(unittest.TestCase):

    def test_two_sources_land_as_two_folders(self) -> None:
        """Two positional sources should produce two
        backupfolders/<UUID> subtrees (one per source) under the
        same computer-uuid + plan-uuid."""
        from arq_writer.cli import main
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            srcA = td / "srcA"
            srcA.mkdir()
            (srcA / "a.txt").write_text("alpha")
            srcB = td / "srcB"
            srcB.mkdir()
            (srcB / "b.txt").write_text("beta")
            dst = td / "dst"
            dst.mkdir()
            rc = main([
                "create", str(srcA), str(srcB),
                "--dest", str(dst),
                "--password", "pw",
            ])
            self.assertEqual(rc, 0)
            cu_dirs = [p for p in dst.iterdir() if p.is_dir()]
            self.assertEqual(
                len(cu_dirs), 1,
                f"expected exactly one computer-uuid dir, got "
                f"{cu_dirs!r}",
            )
            folders_dir = cu_dirs[0] / "backupfolders"
            self.assertTrue(folders_dir.is_dir())
            folder_subdirs = [
                p for p in folders_dir.iterdir() if p.is_dir()
            ]
            self.assertEqual(
                len(folder_subdirs), 2,
                f"expected 2 backupfolders (one per source), got "
                f"{[p.name for p in folder_subdirs]}",
            )


class SubprocessEligibilityRelaxationTests(unittest.TestCase):
    """The subprocess worker now allows SFTP + multi-source.
    Previously both forced fallback to the in-process worker."""

    def _plan(self, *, kind="local", sources=None, dest=None):
        from arq_tui.state import Plan
        # ``sources is None`` falls through to the default; an
        # explicit ``sources=[]`` is preserved (lets the empty-
        # source test actually exercise that branch).
        if sources is None:
            sources = ["/srv/a"]
        return Plan(
            plan_id="P", name="t",
            sources=sources,
            destination_kind=kind,
            destination=dest or {"path": "/mnt/dst"},
        )

    def test_sftp_destination_now_eligible(self) -> None:
        from arq_tui.subprocess_workers import subprocess_eligible
        plan = self._plan(
            kind="sftp",
            dest={"host": "h", "user": "u", "path": "/r"},
        )
        self.assertTrue(subprocess_eligible(plan, "sftp"))

    def test_multi_source_now_eligible(self) -> None:
        from arq_tui.subprocess_workers import subprocess_eligible
        plan = self._plan(
            sources=["/srv/a", "/srv/b", "/srv/c"],
        )
        self.assertTrue(subprocess_eligible(plan, "local"))

    def test_no_sources_still_rejected(self) -> None:
        from arq_tui.subprocess_workers import subprocess_eligible
        plan = self._plan(sources=[])
        self.assertFalse(subprocess_eligible(plan, "local"))

    def test_unknown_dest_kind_still_rejected(self) -> None:
        from arq_tui.subprocess_workers import subprocess_eligible
        plan = self._plan(kind="local")
        # An unknown kind (e.g. a future "s3") should still
        # short-circuit so we don't pass garbage to the CLI.
        self.assertFalse(subprocess_eligible(plan, "s3"))


if __name__ == "__main__":
    unittest.main()
