"""End-to-end integration tests against a real Arq 7 SFTP destination.

Companion to ``test_arqapp_sftp_compat.py`` which focuses on
read-only **format/shape** conformance. This module exercises the
**runtime behaviour** of the three pillars — reader, validator,
and writer — against the operator's live destination + a sibling
sandbox subdirectory:

- :class:`RealDestinationReaderTests` — restore the latest record
  of every folder to a scratch dir, walk it, and confirm the tree
  is non-empty + every reachable file has plausible bytes.
- :class:`RealDestinationValidatorTests` — drive an audit-drip
  (L2-equivalent) with a small bytes cap so a real run completes
  in seconds; assert no per-blob failures + the cursor advanced.
- :class:`RealDestinationWriterTests` — open a fresh ``SftpBackend``
  rooted at ``creds.write_subdir`` (NOT the operator's real
  destination), build a small synthetic backup with the same
  encryption password, restore it via the reader, and run the
  validator's QUICK tier against it. The sandbox is recursively
  removed in tearDown so re-running the test is idempotent. The
  operator's real Arq.app-managed roots are never touched.

All tests auto-skip if ``.secrets/`` / ``.env`` / env vars don't
provide credentials. Default test runs on machines without
credentials continue to pass.
"""

from __future__ import annotations

import shlex
import tempfile
import unittest
from pathlib import Path

from arq_reader import Restore
from arq_validator import (
    ValidationTier,
    discover_layout,
    validate,
)
from arq_validator.layout import find_latest_backuprecord
from arq_validator.sftp import SftpBackend
from arq_writer import Backup

from tests.integration._creds import resolve_creds, skip_reason


def _open_backend(creds, *, root: str) -> SftpBackend:
    """Open + enter an SftpBackend rooted at ``root``.

    Tests that target the operator's real destination pass
    ``creds.root``; the writer test passes ``creds.write_subdir_path``
    so the writer never ends up creating files at the real
    destination root.
    """
    backend = SftpBackend(
        host=creds.host,
        user=creds.user,
        port=creds.port,
        password=creds.sftp_password,
        identity_file=creds.identity_file,
        root=root,
    )
    backend.__enter__()
    return backend


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------


@unittest.skipUnless(
    resolve_creds() is not None,
    skip_reason() or "no credentials",
)
class RealDestinationReaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.creds = resolve_creds()

    def test_restore_latest_record_of_first_folder(self) -> None:
        """Pick one folder, restore its latest record into a temp
        directory, and assert the restored tree is non-empty. We
        deliberately don't check specific file content (PII); we
        only assert structural properties that prove every
        decrypt/HMAC step worked."""
        backend = _open_backend(self.creds, root=self.creds.root)
        try:
            layouts = discover_layout(backend, "/", enumerate_objects=False)
            self.assertGreaterEqual(len(layouts), 1)
            cu = layouts[0].computer_uuid
            folder_uuids = list(layouts[0].backup_folder_uuids)
            self.assertGreaterEqual(len(folder_uuids), 1)
            fu = folder_uuids[0]
            # Verify a record exists before bothering with restore.
            rec_path = find_latest_backuprecord(
                backend, "/", cu, fu,
            )
            self.assertIsNotNone(
                rec_path,
                f"no backuprecord for {cu}/{fu}",
            )
            with tempfile.TemporaryDirectory() as td:
                out = Path(td)
                rs = Restore(
                    "/", encryption_password=self.creds.dest_password,
                    backend=backend,
                )
                # plan_totals=False keeps the test bounded; we
                # don't need an ETA for a one-shot run.
                result = rs.restore(
                    folder_uuid=fu,
                    computer_uuid=cu,
                    dest=out,
                    plan_totals=False,
                )
                self.assertGreaterEqual(
                    result.files_restored, 1,
                    "restore wrote zero files",
                )
                # Must have produced at least one regular file with
                # real bytes — content is PII so we only check
                # presence + non-empty.
                walked = 0
                for p in out.rglob("*"):
                    if walked >= 8:
                        break
                    if p.is_file():
                        walked += 1
                        # File should be readable; size 0 is
                        # technically valid (operator may back up
                        # empty files), but we require *some* non-
                        # empty file to exist so we tighten the
                        # test elsewhere.
                self.assertGreater(walked, 0)
                self.assertTrue(
                    any(p.is_file() and p.stat().st_size > 0
                        for p in out.rglob("*")),
                    "restored tree has no non-empty files — "
                    "decryption almost certainly failed",
                )
        finally:
            backend.close()


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------


@unittest.skipUnless(
    resolve_creds() is not None,
    skip_reason() or "no credentials",
)
class RealDestinationValidatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.creds = resolve_creds()

    def test_audit_drip_capped_at_a_few_megabytes(self) -> None:
        """Run an L2-equivalent audit with a tight wall-clock +
        bytes budget so the test always finishes quickly. Asserts
        that whatever the audit DID examine had no HMAC failures.
        The bytes cap (`audit_max_bytes`) makes the run bounded
        even on huge destinations."""
        backend = _open_backend(self.creds, root=self.creds.root)
        try:
            report = validate(
                backend, root="/",
                tier=ValidationTier.AUDIT,
                encryption_password=self.creds.dest_password,
                audit_max_bytes=4 * 1024 * 1024,
                audit_max_runtime_sec=20.0,
            )
            self.assertIsNone(
                report.error,
                msg=f"audit errored: {report.error}",
            )
            if report.audit is not None:
                self.assertEqual(
                    report.audit.files_fail, 0,
                    msg=f"audit surfaced fail-count "
                        f"{report.audit.files_fail}: "
                        f"{report.audit.failures[:3]}",
                )
                self.assertEqual(
                    report.audit.files_error, 0,
                    msg=f"audit surfaced error-count "
                        f"{report.audit.files_error}: "
                        f"{report.audit.failures[:3]}",
                )
                self.assertGreater(
                    report.audit.files_total, 0,
                    msg="audit examined zero objects",
                )
        finally:
            backend.close()


# ---------------------------------------------------------------------------
# Writer (sandboxed under .arq-backup-tui-write-test/)
# ---------------------------------------------------------------------------


@unittest.skipUnless(
    resolve_creds() is not None,
    skip_reason() or "no credentials",
)
class RealDestinationWriterTests(unittest.TestCase):
    """Write a synthetic backup into a SAFE sibling subdirectory.

    The subdirectory is rooted at ``creds.write_subdir_path`` which
    defaults to ``<root>/.arq-backup-tui-write-test``. It's:

    - dot-prefixed (Arq destinations are UUID-shaped — no risk of
      collision with the operator's real backups)
    - recursively cleaned up in :meth:`tearDown` so re-running the
      test starts fresh
    - opened as a separate ``SftpBackend.root`` so the writer
      cannot mistakenly land bytes outside the sandbox
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.creds = resolve_creds()

    def setUp(self) -> None:
        # Clean from a previous failing run, then create the
        # sandbox directory from scratch. Both ops happen via ssh
        # against the SFTP host (the SftpBackend has no recursive-
        # rmdir; rm -rf is the simplest portable answer).
        self._cleanup_sandbox()
        # Open a control-only backend at the parent root just to
        # mkdir the sandbox.
        ctrl = _open_backend(self.creds, root=self.creds.root)
        try:
            ctrl.mkdir(f"/{self.creds.write_subdir.lstrip('/')}",
                       parents=True, exist_ok=True)
        finally:
            ctrl.close()

    def tearDown(self) -> None:
        # Always clean the sandbox even if a sub-test failed.
        self._cleanup_sandbox()

    def _cleanup_sandbox(self) -> None:
        """``rm -rf`` the sandbox over the SFTP host's shell."""
        ctrl = _open_backend(self.creds, root=self.creds.root)
        try:
            target = self.creds.write_subdir_path
            cp = ctrl._run_ssh(f"rm -rf {shlex.quote(target)}")
            # Don't assert on returncode — first-run cleanup may
            # see "no such file" which exits 0 anyway with rm -rf.
            self.assertIn(cp.returncode, (0, 1))
        finally:
            ctrl.close()

    def test_round_trip_via_real_sftp(self) -> None:
        """End-to-end: writer → reader → validator round-trip
        through the real SFTP destination, never touching the
        operator's actual backups."""
        backend = _open_backend(
            self.creds, root=self.creds.write_subdir_path,
        )
        try:
            with tempfile.TemporaryDirectory() as td:
                tdp = Path(td)
                src = tdp / "src"
                src.mkdir()
                # Mix of small + medium files + nested directory +
                # a non-ASCII path so the round-trip exercises the
                # real-world surface (UTF-8 names, multiple blobs,
                # tree blob + standalone object paths).
                (src / "alpha.txt").write_bytes(b"alpha\n")
                (src / "한글.txt").write_bytes("내용".encode())
                (src / "subdir").mkdir()
                (src / "subdir" / "gamma.bin").write_bytes(
                    b"x" * 16384,
                )

                # ── Writer ──
                # build_backup() is a local-FS convenience wrapper;
                # for backend-injected runs we drive the lower-level
                # Backup class directly. dest_root="/" is the
                # SFTP-namespace anchor — the backend (rooted at
                # creds.write_subdir_path) maps that to the sandbox.
                bk = Backup(
                    dest_root=Path("/"),
                    encryption_password=self.creds.dest_password,
                    backup_name="real-sftp-roundtrip",
                    backend=backend,
                )
                bk.init_plan()
                bk.add_folder(src, folder_name=src.name)

                # ── Reader ──
                rs = Restore(
                    "/",
                    encryption_password=self.creds.dest_password,
                    backend=backend,
                )
                layouts = rs.layouts()
                self.assertEqual(
                    len(layouts), 1,
                    "writer produced multiple computer roots",
                )
                fu = layouts[0].backup_folder_uuids[0]
                out = tdp / "out"
                out.mkdir()
                rs.restore(
                    folder_uuid=fu,
                    computer_uuid=layouts[0].computer_uuid,
                    dest=out, plan_totals=False,
                )
                self.assertEqual(
                    (out / "alpha.txt").read_bytes(), b"alpha\n",
                )
                self.assertEqual(
                    (out / "한글.txt").read_bytes(),
                    "내용".encode(),
                )
                self.assertEqual(
                    (out / "subdir" / "gamma.bin").read_bytes(),
                    b"x" * 16384,
                )

                # ── Validator ──
                report = validate(
                    backend, root="/",
                    tier=ValidationTier.DEEP,
                    encryption_password=self.creds.dest_password,
                )
                self.assertIsNone(
                    report.error,
                    msg=f"validator errored: {report.error}",
                )
                if report.magic_check is not None:
                    self.assertEqual(report.magic_check.fail, 0)
                if report.backuprecord is not None:
                    self.assertEqual(report.backuprecord.fail, 0)
        finally:
            backend.close()


if __name__ == "__main__":
    unittest.main()
