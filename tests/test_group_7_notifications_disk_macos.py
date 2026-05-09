"""Tests for group 7: F2 notifications + F3 disk estimator +
F5 macOS progress.

All three modules are platform-aware — tests use mocking to
avoid actually firing osascript / notify-send / shell hooks.
"""

from __future__ import annotations

import platform
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


# ---------------------------------------------------------------------------
# F2 — notifications
# ---------------------------------------------------------------------------


class NotificationConfigTests(unittest.TestCase):

    def test_default_config_disables_on_unknown_platform(self) -> None:
        from arq_tui.notifications import NotificationConfig
        cfg = NotificationConfig()
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.on_status, ["failed", "cancelled"])

    def test_load_config_returns_defaults_when_missing(self) -> None:
        from arq_tui.notifications import load_config
        with tempfile.TemporaryDirectory() as td:
            cfg = load_config(Path(td) / "missing.json")
            # Auto-detect kicked in; default still has on_status set.
            self.assertEqual(
                cfg.on_status, ["failed", "cancelled"],
            )

    def test_load_config_reads_operator_overrides(self) -> None:
        import json
        from arq_tui.notifications import load_config
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            cfg_path = tdp / "notifications.json"
            cfg_path.write_text(json.dumps({
                "enabled": False,
                "on_status": ["failed"],
                "shell_command": "/usr/bin/curl example.com",
            }), encoding="utf-8")
            cfg = load_config(cfg_path)
            self.assertFalse(cfg.enabled)
            self.assertEqual(cfg.on_status, ["failed"])
            self.assertEqual(
                cfg.shell_command,
                "/usr/bin/curl example.com",
            )


class NotifyDispatchTests(unittest.TestCase):

    def _record(self, status="failed", error="something broke"):
        # Minimal duck-typed stand-in — the dispatcher only
        # accesses .status / .plan_name / .error.
        from arq_tui.runs import RunRecord
        rec = RunRecord(
            status=status, plan_name="test-plan",
        )
        rec.error = error
        return rec

    def test_disabled_config_yields_no_handlers(self) -> None:
        from arq_tui.notifications import (
            NotificationConfig, notify_run_finished,
        )
        result = notify_run_finished(
            self._record(),
            config=NotificationConfig(enabled=False),
        )
        self.assertFalse(result["desktop"])
        self.assertFalse(result["shell"])

    def test_status_filter_skips_completed(self) -> None:
        """Default on_status=['failed','cancelled'] — a
        'completed' record fires nothing."""
        from arq_tui.notifications import (
            NotificationConfig, notify_run_finished,
        )
        cfg = NotificationConfig(
            enabled=True,
            on_status=["failed"],
            desktop_kind="osascript",
            shell_command="/bin/true",
        )
        result = notify_run_finished(
            self._record(status="completed"), config=cfg,
        )
        self.assertFalse(result["desktop"])
        self.assertFalse(result["shell"])

    def test_shell_command_runs_on_failure(self) -> None:
        """Operator-supplied shell command must fire when
        status matches on_status."""
        from arq_tui.notifications import (
            NotificationConfig, notify_run_finished,
        )
        cfg = NotificationConfig(
            enabled=True,
            on_status=["failed"],
            desktop_kind=None,    # no desktop call to mock
            shell_command="/bin/true",   # always succeeds
        )
        result = notify_run_finished(
            self._record(), config=cfg,
        )
        self.assertTrue(result["shell"])


# ---------------------------------------------------------------------------
# F3 — disk estimator
# ---------------------------------------------------------------------------


class DiskEstimatorTests(unittest.TestCase):

    def test_estimate_handles_empty_source(self) -> None:
        from arq_writer.disk_estimator import (
            estimate_destination_size,
        )
        with tempfile.TemporaryDirectory() as td:
            est = estimate_destination_size(
                source_bytes=0,
                dest_path=Path(td),
            )
            self.assertEqual(est.estimated_dest_bytes, 0)
            self.assertTrue(est.will_fit)

    def test_compression_ratio_shrinks_estimate(self) -> None:
        from arq_writer.disk_estimator import (
            estimate_destination_size,
        )
        with tempfile.TemporaryDirectory() as td:
            est_no = estimate_destination_size(
                source_bytes=10_000_000,
                dest_path=Path(td),
                compression_ratio=1.0,
            )
            est_yes = estimate_destination_size(
                source_bytes=10_000_000,
                dest_path=Path(td),
                compression_ratio=2.0,
            )
            self.assertLess(
                est_yes.estimated_dest_bytes,
                est_no.estimated_dest_bytes,
            )
            # 2x ratio → half the estimated bytes.
            self.assertEqual(
                est_yes.estimated_dest_bytes,
                est_no.estimated_dest_bytes // 2,
            )

    def test_will_fit_false_when_dest_too_small(self) -> None:
        from arq_writer.disk_estimator import (
            estimate_destination_size, DiskEstimate,
        )
        with patch(
            "arq_writer.disk_estimator.shutil.disk_usage",
            return_value=type("U", (), {"free": 1_000})(),
        ):
            est = estimate_destination_size(
                source_bytes=100_000_000,
                dest_path=Path("/tmp"),
                compression_ratio=1.0,
            )
            self.assertFalse(est.will_fit)
            self.assertGreater(est.shortfall_bytes, 0)

    def test_estimate_for_plan_walks_sources(self) -> None:
        from arq_tui.state import Plan
        from arq_writer.disk_estimator import estimate_for_plan
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "a.txt").write_bytes(b"x" * 1024)
            plan = Plan(
                plan_id="p", name="t",
                sources=[str(src)],
                destination_kind="local",
                destination={"path": str(tdp / "dst")},
            )
            est = estimate_for_plan(plan)
            self.assertEqual(est.source_bytes, 1024)


# ---------------------------------------------------------------------------
# F5 — macOS progress
# ---------------------------------------------------------------------------


class MacOSProgressTests(unittest.TestCase):

    def test_is_supported_matches_platform(self) -> None:
        from arq_tui.macos_progress import is_supported
        # On macOS with osascript installed, supported = True.
        # On Linux / Windows / stripped builds, False.
        if platform.system() == "Darwin":
            import shutil as _sh
            expected = bool(_sh.which("osascript"))
            self.assertEqual(is_supported(), expected)
        else:
            self.assertFalse(is_supported())

    def test_show_start_runs_osascript_on_mac(self) -> None:
        from arq_tui.macos_progress import show_start
        with patch(
            "arq_tui.macos_progress.is_supported",
            return_value=True,
        ), patch(
            "arq_tui.macos_progress.subprocess.run",
        ) as mock_run:
            show_start("plan-x")
            self.assertEqual(mock_run.call_count, 1)
            args = mock_run.call_args[0][0]
            self.assertEqual(args[0], "osascript")
            # Title + body text appear in the script.
            script = args[2]
            self.assertIn("plan-x", script)
            self.assertIn("started", script.lower())

    def test_show_start_noop_off_mac(self) -> None:
        from arq_tui.macos_progress import show_start
        with patch(
            "arq_tui.macos_progress.is_supported",
            return_value=False,
        ), patch(
            "arq_tui.macos_progress.subprocess.run",
        ) as mock_run:
            show_start("plan-x")
            mock_run.assert_not_called()

    def test_progress_milestones_fire_at_step(self) -> None:
        from arq_tui.macos_progress import (
            _ProgressState, maybe_show_progress,
        )
        state = _ProgressState(
            plan_name="p", milestone_step=10,
        )
        calls = []
        with patch(
            "arq_tui.macos_progress.is_supported",
            return_value=True,
        ), patch(
            "arq_tui.macos_progress._osascript_notification",
            side_effect=lambda **kw: calls.append(kw),
        ):
            # Below first milestone — no fire.
            maybe_show_progress(
                state, bytes_done=5, bytes_total=100,
            )
            self.assertEqual(len(calls), 0)
            # Cross 10% — fire.
            maybe_show_progress(
                state, bytes_done=15, bytes_total=100,
            )
            self.assertEqual(len(calls), 1)
            # Same milestone — no double-fire.
            maybe_show_progress(
                state, bytes_done=18, bytes_total=100,
            )
            self.assertEqual(len(calls), 1)
            # Cross 20% — fire again.
            maybe_show_progress(
                state, bytes_done=25, bytes_total=100,
            )
            self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
