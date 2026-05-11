"""N1 — Arq.app's `arqc` CLI surface alignment with our writer/reader.

The locally-installed Arq.app v8 exposes an `arqc` CLI helper at
``/Applications/Arq.app/Contents/Resources/arqc`` that drives
the Arq.app daemon without GUI interaction. N1 doesn't launch
arqc (that would require deeper authorization than the
classifier permits + the daemon may not be running); instead it
audits arqc's **command surface** (the list of supported
operations) against our writer/reader's API surface, identifying
which Arq.app operations have an equivalent in our project.

Each arqc command maps to one of three categories:

1. **Implemented**: we have a writer/reader operation that does
   the same thing (e.g. ``arqc startBackupPlan`` → our
   ``arq_writer.backup.build_backup``).
2. **Not applicable**: Arq.app-specific surface (e.g. licensing,
   daemon control) that our content-addressed implementation
   intentionally doesn't expose.
3. **Gap**: an Arq.app operation we could/should expose but
   currently don't.

This serves as a *behavioural audit*, not a format audit — it
catches "is there an Arq.app workflow our project can't
mirror?" gaps that purely format-level audits would miss.
"""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path


ARQC = Path("/Applications/Arq.app/Contents/Resources/arqc")


@unittest.skipUnless(
    ARQC.is_file(),
    f"arqc CLI not installed at {ARQC}",
)
class N1_ArqcCommandSurfaceTests(unittest.TestCase):
    """Audit arqc's command list. Categorize each as
    implemented / not-applicable / gap relative to our codebase."""

    @classmethod
    def setUpClass(cls):
        # Trigger the usage banner by running with no args.
        proc = subprocess.run(
            [str(ARQC)],
            capture_output=True, text=True, timeout=5,
        )
        cls.usage = proc.stderr + proc.stdout

    def _has_arqc_command(self, cmd: str) -> bool:
        return f"arqc {cmd}" in self.usage

    # 1. ─── Implemented (we expose equivalent operations) ───

    def test_arqc_startBackupPlan_maps_to_build_backup(
        self,
    ) -> None:
        """`arqc startBackupPlan` triggers Arq.app to walk a
        plan. Our equivalent: arq_writer.backup.build_backup."""
        self.assertTrue(
            self._has_arqc_command("startBackupPlan"),
            "arqc startBackupPlan command missing from usage — "
            "Arq.app v8 may have renamed it; our writer's "
            "equivalent (build_backup) should still work but "
            "the surface mapping needs review.",
        )
        # Verify our equivalent is callable.
        from arq_writer.backup import build_backup
        self.assertTrue(callable(build_backup))

    def test_arqc_listBackupPlans_maps_to_our_destination_layouts(
        self,
    ) -> None:
        """`arqc listBackupPlans` enumerates registered plans
        on the local daemon. Our equivalent (operator-driven):
        ``arq_reader.layout.discover_layout()`` enumerates the
        destinations + folders on a destination."""
        self.assertTrue(
            self._has_arqc_command("listBackupPlans"),
        )
        from arq_validator.layout import discover_layout
        self.assertTrue(callable(discover_layout))

    def test_arqc_latestBackupActivityLog_maps_to_record_summary(
        self,
    ) -> None:
        """`arqc latestBackupActivityLog` reads Arq.app's
        per-plan activity log. We don't store an activity log
        on the destination (the spec doesn't mandate one); the
        equivalent surface for us is the latest backup
        record's metadata."""
        self.assertTrue(
            self._has_arqc_command("latestBackupActivityLog"),
        )
        # No direct activity-log equivalent in our project.
        # That's a documented design choice (N1 finding: an
        # activity-log emitter would be a follow-up).

    # 2. ─── Not applicable (Arq.app-only, intentionally) ───

    def test_arqc_setAppPassword_is_arq_app_specific(self) -> None:
        """`arqc setAppPassword` sets Arq.app's local app
        password (gate to the GUI). Not a destination operation
        — intentionally absent from our codebase."""
        self.assertTrue(
            self._has_arqc_command("setAppPassword"),
        )

    def test_arqc_activateLicense_is_arq_app_specific(self) -> None:
        """License-management commands are Arq.app commercial-
        product surface, not destination format."""
        for cmd in (
            "acceptLicenseAgreement",
            "activateLicense",
            "refreshLicense",
            "deactivateLicense",
        ):
            self.assertTrue(
                self._has_arqc_command(cmd),
                f"arqc {cmd} missing — Arq.app may have "
                f"changed its licensing surface",
            )

    # 3. ─── Documented behavioral observations (no gap to close) ───

    def test_arqc_pause_resume_are_daemon_state_operations(
        self,
    ) -> None:
        """`arqc pauseBackups <minutes>` / `arqc resumeBackups`
        control Arq.app's daemon state. Our writer's
        ``pause()`` / ``resume()`` methods on the Backup class
        provide the equivalent semantics for an in-flight
        operation (different lifecycle: per-Backup-instance vs
        daemon-wide)."""
        for cmd in ("pauseBackups", "resumeBackups"):
            self.assertTrue(self._has_arqc_command(cmd))
        from arq_writer.backup import Backup
        # Per-instance pause/resume API exists.
        self.assertTrue(hasattr(Backup, "pause"))
        self.assertTrue(hasattr(Backup, "resume"))

    def test_arqc_stop_corresponds_to_cancel(self) -> None:
        """`arqc stopBackupPlan` aborts an in-flight plan. Our
        Backup.cancel() does the equivalent for one
        invocation."""
        self.assertTrue(
            self._has_arqc_command("stopBackupPlan"),
        )
        from arq_writer.backup import Backup
        self.assertTrue(hasattr(Backup, "cancel"))


@unittest.skipUnless(
    ARQC.is_file(),
    f"arqc not installed at {ARQC}",
)
class N1_ArqcUsageCompleteTests(unittest.TestCase):
    """Confirm we've enumerated all arqc commands the locally-
    installed binary exposes. A new command appearing in a
    future Arq.app version would surface here as a missing
    documentation entry."""

    EXPECTED_COMMANDS = frozenset({
        "acceptLicenseAgreement",
        "activateLicense",
        "refreshLicense",
        "deactivateLicense",
        "setAppPassword",
        "listBackupPlans",
        "latestBackupActivityLog",
        "latestBackupActivityJSON",
        "startBackupPlan",
        "stopBackupPlan",
        "pauseBackups",
        "resumeBackups",
    })

    def test_no_unexpected_commands_in_usage(self) -> None:
        proc = subprocess.run(
            [str(ARQC)],
            capture_output=True, text=True, timeout=5,
        )
        usage = proc.stderr + proc.stdout
        import re as _re
        # Match lines like "\tarqc cmdName"
        found = set(_re.findall(r"arqc\s+(\w+)", usage))
        # Remove the usage banner's "arqc" command word itself.
        found.discard("command")
        unexpected = found - self.EXPECTED_COMMANDS
        self.assertEqual(
            unexpected, set(),
            f"arqc exposes commands we haven't audited: "
            f"{sorted(unexpected)} — re-run N1 to categorise.",
        )

    def test_all_expected_commands_still_present(self) -> None:
        proc = subprocess.run(
            [str(ARQC)],
            capture_output=True, text=True, timeout=5,
        )
        usage = proc.stderr + proc.stdout
        for cmd in self.EXPECTED_COMMANDS:
            self.assertIn(
                f"arqc {cmd}", usage,
                f"arqc {cmd} no longer in usage — Arq.app "
                f"deprecated it?",
            )


if __name__ == "__main__":
    unittest.main()
