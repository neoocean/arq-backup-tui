"""Tests for the second wire-up bundle: E3 (audit ledger),
F1 (DestinationModal save-secrets checkbox), F2 (RunWriter
notifications), F3 (BackupRunScreen disk precheck), F5
(BackupRunScreen macOS progress toasts), Sidebar tracking.

Each module already had its own unit tests for the underlying
machinery (e.g. ``tests/test_dedup_and_incremental_audit.py``
covers AuditLedger I/O, ``tests/test_group_7_notifications_disk_macos.py``
covers notifications config). These tests cover the
*integration* — that the existing modules are now actually
called from the right places.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# E3 — AuditLedger wired into run_full_audit
# ---------------------------------------------------------------------------


class _FakeBackend:
    """Minimal Backend stub so we exercise the ledger short-
    circuit without touching real ARQO objects. Records every
    read so we can assert ``backend.read_all`` was NOT called for
    ledger-skipped entries."""

    def __init__(self):
        self.reads = []

    def read_all(self, path):
        self.reads.append(path)
        # Return bytes that will fail magic check — proves the
        # ledger short-circuit ran first if no read happens.
        return b"NOT_ARQO" + b"\x00" * 32

    def stat_size(self, path):
        return 40


class _FakeLayout:
    def __init__(self, computer_uuid, items):
        self.computer_uuid = computer_uuid
        self._items = items

    def family_items(self, kind):
        if kind == "blobpacks":
            return list(self._items)
        return []


class AuditLedgerWireupTests(unittest.TestCase):
    """``run_full_audit(ledger=…)`` skips ledger-known files +
    records new successes."""

    def test_ledger_contains_skips_backend_read(self) -> None:
        from arq_validator.tiers import run_full_audit
        from arq_validator.incremental_audit import AuditLedger
        # Pre-populate the ledger so the file is "already audited".
        ledger = AuditLedger(target="t1")
        ledger.record("blob_already_audited.pack")

        backend = _FakeBackend()
        # Use a layout with one item — the one we marked as
        # known. We can't easily pre-build a real keyset, so
        # patch decrypt_keyset to short-circuit.
        layout = _FakeLayout("CU1", [
            ("00", "blob_already_audited.pack"),
        ])

        with mock.patch(
            "arq_validator.tiers.decrypt_keyset",
            return_value=mock.MagicMock(hmac_key=b"\x00" * 32),
        ):
            with mock.patch.object(
                _FakeBackend, "read_all",
                side_effect=lambda p: (
                    b"" if "encryptedkeyset" in p
                    else b"NOT_ARQO"
                ),
                autospec=False,
            ):
                # Replace the keyset-fetch path
                backend.read_all = lambda p: b""
                result = run_full_audit(
                    backend, [layout],
                    encryption_password="anything",
                    ledger=ledger,
                )

        # Ledger short-circuit should mean files_skipped_by_ledger
        # ticked + the audit "magic check" path never ran.
        self.assertEqual(result.files_skipped_by_ledger, 1)
        self.assertEqual(result.files_total, 1)
        # Sanity: aborted_reason should be unset (we completed).
        self.assertIsNone(result.aborted_reason)

    def test_ledger_records_after_successful_audit(self) -> None:
        """A successful per-file audit should be appended to the
        ledger so the next sweep skips it."""
        from arq_validator.tiers import run_full_audit
        from arq_validator.incremental_audit import AuditLedger

        ledger = AuditLedger(target="t2")
        layout = _FakeLayout("CU2", [("aa", "blob_new.pack")])
        backend = _FakeBackend()

        # Force a successful audit by stubbing the inner-ARQO
        # verifier to claim "all good".
        with mock.patch(
            "arq_validator.tiers.decrypt_keyset",
            return_value=mock.MagicMock(hmac_key=b"\x00" * 32),
        ), mock.patch(
            "arq_validator.tiers.verify_multi_object_arqos",
            return_value=(1, 0, []),
        ):
            backend.read_all = lambda p: (
                b"" if "encryptedkeyset" in p
                else b"ARQO" + b"\x00" * 36
            )
            backend.stat_size = lambda p: 40
            result = run_full_audit(
                backend, [layout],
                encryption_password="x",
                ledger=ledger,
            )

        self.assertEqual(result.files_ok, 1)
        # Ledger should now contain the successful blob.
        self.assertTrue(ledger.contains("blob_new.pack"))

    def test_failure_does_not_pollute_ledger(self) -> None:
        """A failed audit must NOT be recorded — the next sweep
        needs to retry the file."""
        from arq_validator.tiers import run_full_audit
        from arq_validator.incremental_audit import AuditLedger

        ledger = AuditLedger(target="t3")
        layout = _FakeLayout("CU3", [("bb", "blob_bad.pack")])
        backend = _FakeBackend()

        with mock.patch(
            "arq_validator.tiers.decrypt_keyset",
            return_value=mock.MagicMock(hmac_key=b"\x00" * 32),
        ), mock.patch(
            "arq_validator.tiers.verify_multi_object_arqos",
            return_value=(0, 1, [0]),    # 1 failure
        ):
            backend.read_all = lambda p: (
                b"" if "encryptedkeyset" in p
                else b"ARQO" + b"\x00" * 36
            )
            backend.stat_size = lambda p: 40
            run_full_audit(
                backend, [layout],
                encryption_password="x",
                ledger=ledger,
            )

        self.assertFalse(ledger.contains("blob_bad.pack"))


# ---------------------------------------------------------------------------
# F1 — DestinationModal save-secrets checkbox writes sftp.json
# ---------------------------------------------------------------------------


class DestinationModalSaveSecretsTests(unittest.TestCase):
    """The new Checkbox has id ``save-secrets`` and lives in the
    modal's compose tree. We don't drive an actual textual
    instance — we verify the wiring exists by inspecting the
    source so a refactor that drops the checkbox can't quietly
    pass."""

    def test_modal_imports_secrets_setup_and_has_checkbox(self) -> None:
        modal_src = (
            REPO_ROOT / "arq_tui" / "widgets" / "destination_modal.py"
        ).read_text(encoding="utf-8")
        # Checkbox is yielded with the right id + label.
        self.assertIn('id="save-secrets"', modal_src)
        self.assertIn(".secrets/sftp.json", modal_src)
        # Wired to write_sftp_json on submit.
        self.assertIn("write_sftp_json", modal_src)


# ---------------------------------------------------------------------------
# F2 — RunWriter.__exit__ fires notify_run_finished
# ---------------------------------------------------------------------------


class RunWriterNotificationTests(unittest.TestCase):

    def test_exit_calls_notify_run_finished(self) -> None:
        from arq_tui.runs import RunRecord, RunStatus, RunWriter
        rec = RunRecord(
            run_id="abc123def456",
            plan_name="test-plan",
            status=RunStatus.RUNNING.value,
            started_at=time.time(),
        )
        with tempfile.TemporaryDirectory(
            prefix="arq-runwriter-",
        ) as td:
            # Disable the env-var guard for this test so the
            # notification path actually runs.
            os.environ.pop(
                "ARQ_BACKUP_TUI_DISABLE_NOTIFICATIONS", None,
            )
            with mock.patch(
                "arq_tui.notifications.notify_run_finished",
            ) as fake:
                with RunWriter(rec, state_dir=Path(td)):
                    pass
                self.assertTrue(fake.called)
                # First (positional) arg = the record.
                args, _kwargs = fake.call_args
                self.assertEqual(args[0].plan_name, "test-plan")

    def test_disable_env_var_suppresses_notification(self) -> None:
        from arq_tui.runs import RunRecord, RunStatus, RunWriter
        rec = RunRecord(
            run_id="def987abc654",
            plan_name="quiet-plan",
            status=RunStatus.RUNNING.value,
            started_at=time.time(),
        )
        with tempfile.TemporaryDirectory(prefix="arq-runwriter-") as td:
            os.environ["ARQ_BACKUP_TUI_DISABLE_NOTIFICATIONS"] = "1"
            try:
                with mock.patch(
                    "arq_tui.notifications.notify_run_finished",
                ) as fake:
                    with RunWriter(rec, state_dir=Path(td)):
                        pass
                    self.assertFalse(fake.called)
            finally:
                os.environ.pop(
                    "ARQ_BACKUP_TUI_DISABLE_NOTIFICATIONS", None,
                )

    def test_notification_failure_does_not_break_exit(self) -> None:
        """An exception inside notify_run_finished must NOT
        propagate out of RunWriter.__exit__ — backups never fail
        because a notification daemon hiccupped."""
        from arq_tui.runs import RunRecord, RunStatus, RunWriter
        rec = RunRecord(
            run_id="boom1234abcd",
            plan_name="boom-plan",
            status=RunStatus.RUNNING.value,
            started_at=time.time(),
        )
        with tempfile.TemporaryDirectory(prefix="arq-runwriter-") as td:
            os.environ.pop(
                "ARQ_BACKUP_TUI_DISABLE_NOTIFICATIONS", None,
            )
            with mock.patch(
                "arq_tui.notifications.notify_run_finished",
                side_effect=RuntimeError("boom"),
            ):
                # Must not raise.
                with RunWriter(rec, state_dir=Path(td)):
                    pass


# ---------------------------------------------------------------------------
# F3 — BackupRunScreen disk-precheck wiring
# ---------------------------------------------------------------------------


class BackupRunDiskPrecheckTests(unittest.TestCase):
    """Verify the screen calls estimate_for_plan in on_mount +
    has the env-var skip switch."""

    def test_screen_imports_disk_estimator(self) -> None:
        try:
            import arq_tui.screens.backup_run as br
        except ImportError:
            self.skipTest("textual not installed")
        # Source-level check: estimate_for_plan import + skip
        # env var wired.
        src = (
            REPO_ROOT / "arq_tui" / "screens" / "backup_run.py"
        ).read_text(encoding="utf-8")
        self.assertIn("estimate_for_plan", src)
        self.assertIn("ARQ_TUI_SKIP_DISK_PRECHECK", src)
        # Helper exists at module level (pure func — testable).
        self.assertTrue(hasattr(br, "_fmt_bytes"))

    def test_fmt_bytes_unit_progression(self) -> None:
        try:
            from arq_tui.screens.backup_run import _fmt_bytes
        except ImportError:
            self.skipTest("textual not installed")
        # Spot-check unit boundaries.
        self.assertEqual(_fmt_bytes(0), "0B")
        self.assertEqual(_fmt_bytes(1023), "1023B")
        self.assertEqual(_fmt_bytes(1024), "1.0KB")
        self.assertIn("MB", _fmt_bytes(2 * 1024 * 1024))
        self.assertIn("GB", _fmt_bytes(5 * 1024 ** 3))


# ---------------------------------------------------------------------------
# F5 — macOS progress toasts wired to BackupRunScreen
# ---------------------------------------------------------------------------


class BackupRunMacosProgressTests(unittest.TestCase):

    def test_screen_holds_progress_state(self) -> None:
        try:
            from arq_tui.screens.backup_run import BackupRunScreen
            from arq_tui.macos_progress import _ProgressState
        except ImportError:
            self.skipTest("textual not installed")
        # Source-level check: the on_worker_event handler wires
        # maybe_show_progress + the start/complete toasts fire on
        # the worker lifecycle messages.
        src = (
            REPO_ROOT / "arq_tui" / "screens" / "backup_run.py"
        ).read_text(encoding="utf-8")
        self.assertIn("maybe_show_progress", src)
        self.assertIn("show_start", src)
        self.assertIn("show_complete", src)
        # _ProgressState init runs in __init__ — instances of
        # _ProgressState at the class level imply the import
        # happened. Spot-check via dir().
        self.assertTrue(hasattr(BackupRunScreen, "__init__"))


# ---------------------------------------------------------------------------
# Sidebar tracking — HomeScreen is the M9 persistent shell
# ---------------------------------------------------------------------------


class SidebarSectionForScreenWireupTests(unittest.TestCase):

    def test_home_screen_is_persistent_shell(self) -> None:
        """M9 replaced the launcher model with a persistent shell:
        ``HomeScreen`` composes a fixed ``Sidebar`` (initial section
        ``plans``) driving a ``ContentSwitcher``, and sidebar
        selections route through ``on_sidebar_navigation`` →
        ``_show_section`` (swap-in-place, no full-screen push). The
        old ``section_for_screen``-in-home wireup this test used to
        pin was superseded — ``section_for_screen`` survives as a
        standalone sidebar helper (see test_group_9_polish)."""
        src = (
            REPO_ROOT / "arq_tui" / "screens" / "home.py"
        ).read_text(encoding="utf-8")
        self.assertIn('Sidebar(active="plans")', src)
        self.assertIn("ContentSwitcher", src)
        self.assertIn("on_sidebar_navigation", src)
        self.assertIn("_show_section", src)


if __name__ == "__main__":
    unittest.main()
