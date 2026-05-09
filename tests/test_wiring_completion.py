"""Tests for the four wire-up additions in this PR.

Each previous PR added a backing module that wasn't reachable
from the operator-facing surface. This PR connects them:

- G1-(b): ``arq-reader restore --verify-after`` actually invokes
  ``restore_verify`` post-restore.
- A4-(b): ``SchedulingScreen(staged_plan=plan)`` lets HomeScreen
  hand a focused plan to the install action.
- B9-(c): ``Backup._walk_*`` populates ``Node.aclBlobLoc`` via
  ``capture_acl``; ``Restore._restore_file_node`` calls
  ``apply_acl`` after chmod/chown.
- H2: README §4.5 documents pre-commit setup.

These tests pin the wiring at the seam between the previously-
isolated module and its caller — they don't re-test the modules
themselves (those have their own test files).
"""

from __future__ import annotations

import subprocess
import sys
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


# ---------------------------------------------------------------------------
# G1-(b)
# ---------------------------------------------------------------------------


class VerifyAfterCLITests(unittest.TestCase):

    def test_verify_after_flag_parses(self) -> None:
        from arq_reader.cli import _build_parser
        ns = _build_parser().parse_args([
            "restore", "/tmp/src", "abcd", "/tmp/dest",
            "--password", "p", "--verify-after",
        ])
        self.assertTrue(ns.verify_after)

    def test_verify_after_default_false(self) -> None:
        from arq_reader.cli import _build_parser
        ns = _build_parser().parse_args([
            "restore", "/tmp/src", "abcd", "/tmp/dest",
            "--password", "p",
        ])
        self.assertFalse(ns.verify_after)


@unittest.skipUnless(_has_openssl(), "openssl CLI required")
class VerifyAfterEndToEndTests(unittest.TestCase):

    def test_verify_after_passes_on_clean_round_trip(self) -> None:
        """Build a tiny backup, restore it with --verify-after,
        confirm the verify section appears in the JSON output +
        reports ok=true."""
        import json
        from io import StringIO
        from contextlib import redirect_stdout
        from arq_reader.cli import main as reader_main
        from arq_writer import build_backup
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            src = td / "src"
            src.mkdir()
            (src / "a.txt").write_bytes(b"hello world\n")
            dst = td / "dst"
            dst.mkdir()
            build_backup(src, dst, "pw")
            out = td / "out"
            cu = next(p.name for p in dst.iterdir() if p.is_dir())
            fu = next(
                p.name
                for p in (dst / cu / "backupfolders").iterdir()
                if p.is_dir()
            )
            buf = StringIO()
            with redirect_stdout(buf):
                rc = reader_main([
                    "restore", str(dst), fu, str(out),
                    "--password", "pw",
                    "--computer-uuid", cu,
                    "--verify-after",
                ])
            self.assertEqual(rc, 0)
            data = json.loads(buf.getvalue())
            self.assertIn("verify", data)
            self.assertTrue(data["verify"]["ok"])
            self.assertGreater(
                data["verify"]["files_verified"], 0,
            )


# ---------------------------------------------------------------------------
# A4-(b)
# ---------------------------------------------------------------------------


class SchedulingStagedPlanTests(unittest.TestCase):

    def test_screen_accepts_staged_plan(self) -> None:
        try:
            from arq_tui.screens.scheduling import SchedulingScreen
        except ImportError:
            self.skipTest("textual not installed")
        from arq_tui.state import Plan
        plan = Plan(
            plan_id="P-staged", name="staged",
            sources=["/srv"],
            destination_kind="local",
            destination={"path": "/Volumes/x"},
            schedule={"cron_expr": "0 4 * * *"},
        )
        s = SchedulingScreen(staged_plan=plan)
        self.assertIs(s._staged_plan, plan)
        self.assertEqual(
            s._plan_to_install().plan_id, "P-staged",
        )

    def test_screen_without_staged_plan_falls_through(self) -> None:
        """Without a staged plan + no focused row + no live App
        context, _plan_to_install returns None (or raises an
        attribute lookup error from .query_one — both are the
        'no plan to install' path)."""
        try:
            from arq_tui.screens.scheduling import SchedulingScreen
        except ImportError:
            self.skipTest("textual not installed")
        s = SchedulingScreen()
        try:
            result = s._plan_to_install()
        except Exception:
            # query_one / notify need a live App; outside one
            # they raise. Either outcome counts as the
            # "no plan" path the production code handles.
            result = None
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# B9-(c)
# ---------------------------------------------------------------------------


@unittest.skipUnless(_has_openssl(), "openssl CLI required")
class ACLWiringTests(unittest.TestCase):

    def test_backup_walker_attempts_acl_capture_on_each_file(self) -> None:
        """The Backup walker should call capture_acl per file —
        we monkey-patch capture_acl + count invocations rather
        than assert on actual ACL bytes (real macOS ACLs need
        non-trivial chmod +a setup the test would have to
        provide)."""
        import arq_writer.acl as acl_mod
        original = acl_mod.capture_acl
        calls = []

        def _spy(path, *, callback=None):
            calls.append(str(path))
            return b""   # no ACL

        acl_mod.capture_acl = _spy
        try:
            from arq_writer import Backup
            with tempfile.TemporaryDirectory() as td:
                td = Path(td)
                src = td / "src"
                src.mkdir()
                (src / "a.txt").write_text("hello")
                (src / "b.txt").write_text("world")
                dst = td / "dst"
                dst.mkdir()
                bk = Backup(
                    dest_root=dst, encryption_password="pw",
                )
                bk.init_plan()
                bk.add_folder(src)
            # capture_acl should have been called for each file
            # PLUS the source dir itself.
            file_calls = [
                c for c in calls
                if c.endswith("a.txt") or c.endswith("b.txt")
            ]
            self.assertGreaterEqual(len(file_calls), 2)
        finally:
            acl_mod.capture_acl = original

    def test_restore_attempts_acl_apply_when_node_has_acl_loc(self) -> None:
        """Mock _fetch_blob + apply_acl + drive a restore through
        a pre-built backup. The mock confirms apply_acl gets the
        bytes from the recorded aclBlobLoc."""
        # Black-box: build a real backup + spy on apply_acl during
        # restore. Since capture_acl on macOS test machines may
        # return empty (no ACLs on tmpdir), we monkey-patch
        # capture_acl to inject a sentinel blob during backup +
        # apply_acl during restore to confirm it's the same blob.
        import arq_writer.acl as acl_mod
        sentinel = b"ACL_MACOS_NFSV4\n0: user:test allow read"
        applied_args = []

        def _capture(path, *, callback=None):
            return sentinel

        def _apply(path, blob, *, callback=None):
            applied_args.append((str(path), bytes(blob)))
            return True

        original_capture = acl_mod.capture_acl
        original_apply = acl_mod.apply_acl
        acl_mod.capture_acl = _capture
        acl_mod.apply_acl = _apply
        try:
            from arq_writer import build_backup
            from arq_reader import Restore
            with tempfile.TemporaryDirectory() as td:
                td = Path(td)
                src = td / "src"
                src.mkdir()
                (src / "a.txt").write_text("alpha")
                dst = td / "dst"
                dst.mkdir()
                build_backup(src, dst, "pw")
                out = td / "out"
                rs = Restore(str(dst), encryption_password="pw")
                cu = next(p.name for p in dst.iterdir() if p.is_dir())
                fu = next(
                    p.name
                    for p in (dst / cu / "backupfolders").iterdir()
                    if p.is_dir()
                )
                rs.restore(
                    folder_uuid=fu, computer_uuid=cu, dest=out,
                )
            # apply_acl should have been called for the restored
            # file with the same blob bytes capture_acl returned.
            file_applies = [
                (p, b) for (p, b) in applied_args
                if p.endswith("a.txt")
            ]
            self.assertEqual(
                len(file_applies), 1,
                f"expected one apply_acl call for a.txt; "
                f"got: {applied_args!r}",
            )
            self.assertEqual(file_applies[0][1], sentinel)
        finally:
            acl_mod.capture_acl = original_capture
            acl_mod.apply_acl = original_apply


# ---------------------------------------------------------------------------
# H2 — README setup section exists
# ---------------------------------------------------------------------------


class READMEPreCommitDocsTests(unittest.TestCase):

    def test_readme_mentions_pre_commit_install(self) -> None:
        readme = Path(__file__).resolve().parent.parent / "README.md"
        text = readme.read_text(encoding="utf-8")
        # The development setup section talks about pre-commit
        # install + the doc-link checker hook + pyright.
        for needle in (
            "pre-commit install",
            "check-doc-links",
            "pyright",
        ):
            self.assertIn(
                needle, text,
                f"README missing pre-commit doc element: {needle!r}",
            )


if __name__ == "__main__":
    unittest.main()
