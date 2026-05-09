"""Tests for the cron + launchd schedule emitters.

The pure emitters (:func:`generate_crontab_entry` /
:func:`generate_launchd_plist`) are exercised byte-by-byte; the
install / list / remove side is exercised against a temp HOME so
nothing touches the operator's real schedule store.

Both code paths emit the same argv shape — a fork of the writer
CLI with `--state-file` so a scheduler-launched run looks
identical to a TUI-launched one.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


def _make_plan(**overrides):
    """Build a minimal Plan with sensible defaults so each test
    only has to override the field it actually cares about."""
    from arq_tui.state import Plan
    base = dict(
        plan_id="P-test",
        name="schedule-test",
        sources=["/srv/data"],
        destination_kind="local",
        destination={"path": "/Volumes/dst"},
        chunker="default",
        use_packs=True,
        dedup_against_existing=True,
    )
    base.update(overrides)
    return Plan(**base)


class ScheduleSpecValidationTests(unittest.TestCase):

    def test_cron_expr_only_is_valid(self) -> None:
        from arq_tui.scheduling import ScheduleSpec
        s = ScheduleSpec(cron_expr="0 3 * * *")
        self.assertEqual(s.cron_fields(), (0, 3, -1, -1, -1))

    def test_interval_only_is_valid(self) -> None:
        from arq_tui.scheduling import ScheduleSpec
        s = ScheduleSpec(interval_sec=3600)
        self.assertEqual(s.interval_sec, 3600)

    def test_both_set_rejected(self) -> None:
        from arq_tui.scheduling import ScheduleSpec
        with self.assertRaises(ValueError):
            ScheduleSpec(cron_expr="0 3 * * *", interval_sec=3600)

    def test_neither_set_rejected(self) -> None:
        from arq_tui.scheduling import ScheduleSpec
        with self.assertRaises(ValueError):
            ScheduleSpec()

    def test_cron_expr_with_step_syntax_rejected(self) -> None:
        from arq_tui.scheduling import ScheduleSpec
        with self.assertRaises(ValueError):
            ScheduleSpec(cron_expr="*/15 * * * *").cron_fields()


class CrontabEntryEmitterTests(unittest.TestCase):

    def test_entry_carries_marker_with_plan_id(self) -> None:
        from arq_tui.scheduling import generate_crontab_entry
        plan = _make_plan(schedule={"cron_expr": "30 4 * * *"})
        entry = generate_crontab_entry(
            plan, executable="/usr/bin/python3",
            state_dir=Path("/var/state"),
        )
        # First line is the marker comment so the parser can
        # later identify our entries; second line is the cron + cmd.
        first_line, second_line = entry.split("\n", 1)
        self.assertEqual(
            first_line, "# arq-backup-tui:plan=P-test",
        )
        self.assertTrue(second_line.startswith("30 4 * * * "))

    def test_entry_includes_state_file_and_password_env(self) -> None:
        from arq_tui.scheduling import generate_crontab_entry
        plan = _make_plan(schedule={"cron_expr": "0 1 * * *"})
        entry = generate_crontab_entry(
            plan, executable="/usr/bin/python3",
            state_dir=Path("/state"),
            password_env_var="MY_PW",
        )
        self.assertIn("--state-file", entry)
        self.assertIn("plan-P-test.json", entry)
        self.assertIn("--password-env", entry)
        self.assertIn("MY_PW", entry)

    def test_sftp_destination_emits_sftp_args(self) -> None:
        from arq_tui.scheduling import generate_crontab_entry
        plan = _make_plan(
            destination_kind="sftp",
            destination={
                "host": "u504460.your-storagebox.de",
                "user": "u504460", "port": 23,
                "path": "/home/u504460/arq",
            },
            schedule={"cron_expr": "0 5 * * *"},
        )
        entry = generate_crontab_entry(
            plan, executable="/usr/bin/python3",
        )
        self.assertIn("--sftp-host", entry)
        self.assertIn("u504460.your-storagebox.de", entry)
        self.assertIn("--sftp-port", entry)
        self.assertIn("23", entry)
        self.assertIn("/home/u504460/arq", entry)
        # Should NOT have --dest in SFTP mode.
        self.assertNotIn("--dest", entry.split("\n")[1])

    def test_default_schedule_is_daily_at_3am(self) -> None:
        # When the plan has no schedule field set, the helper
        # falls back to daily 03:00 — least-bad sensible default.
        from arq_tui.scheduling import generate_crontab_entry
        plan = _make_plan()  # no schedule
        entry = generate_crontab_entry(
            plan, executable="/usr/bin/python3",
        )
        self.assertTrue(entry.split("\n")[1].startswith("0 3 * * * "))


class CrontabRoundTripTests(unittest.TestCase):

    def test_parse_extracts_managed_entries(self) -> None:
        from arq_tui.scheduling import (
            generate_crontab_entry, parse_crontab_entries,
        )
        plan_a = _make_plan(plan_id="A", name="alpha",
                            schedule={"cron_expr": "0 4 * * *"})
        plan_b = _make_plan(plan_id="B", name="beta",
                            schedule={"cron_expr": "0 5 * * *"})
        # Operator has a pre-existing crontab line we must not
        # disturb when parsing.
        existing = "0 0 * * * /usr/bin/operator-job\n"
        merged = (
            existing
            + generate_crontab_entry(plan_a) + "\n"
            + generate_crontab_entry(plan_b) + "\n"
        )
        out = parse_crontab_entries(merged)
        plan_ids = [pid for pid, _ in out]
        self.assertEqual(set(plan_ids), {"A", "B"})

    def test_strip_preserves_unrelated_lines(self) -> None:
        from arq_tui.scheduling import (
            _strip_plan_from_crontab, generate_crontab_entry,
        )
        plan_a = _make_plan(plan_id="A",
                            schedule={"cron_expr": "0 4 * * *"})
        plan_b = _make_plan(plan_id="B",
                            schedule={"cron_expr": "0 5 * * *"})
        unrelated = "0 0 * * * /usr/bin/operator-job"
        merged = (
            unrelated + "\n"
            + generate_crontab_entry(plan_a) + "\n"
            + generate_crontab_entry(plan_b) + "\n"
        )
        result = _strip_plan_from_crontab(merged, "A")
        self.assertIn(unrelated, result)
        # B should still be present.
        self.assertIn(
            "# arq-backup-tui:plan=B", result,
        )
        # A is gone.
        self.assertNotIn(
            "# arq-backup-tui:plan=A", result,
        )


class LaunchdPlistEmitterTests(unittest.TestCase):

    def test_plist_label_is_reverse_dns_with_plan_id(self) -> None:
        from arq_tui.scheduling import generate_launchd_plist
        plan = _make_plan(plan_id="ABC",
                          schedule={"cron_expr": "0 3 * * *"})
        plist = generate_launchd_plist(
            plan, executable="/usr/bin/python3",
        )
        self.assertIn(
            "<string>com.arq-backup-tui.plan-ABC</string>", plist,
        )

    def test_plist_includes_schedule_block(self) -> None:
        from arq_tui.scheduling import generate_launchd_plist
        plan = _make_plan(schedule={"cron_expr": "30 4 * * *"})
        plist = generate_launchd_plist(plan)
        self.assertIn("<key>StartCalendarInterval</key>", plist)
        self.assertIn(
            "<key>Hour</key><integer>4</integer>", plist,
        )
        self.assertIn(
            "<key>Minute</key><integer>30</integer>", plist,
        )

    def test_interval_schedule_uses_start_interval(self) -> None:
        from arq_tui.scheduling import generate_launchd_plist
        plan = _make_plan(schedule={"interval_sec": 7200})
        plist = generate_launchd_plist(plan)
        self.assertIn("<key>StartInterval</key>", plist)
        self.assertIn("<integer>7200</integer>", plist)
        self.assertNotIn("StartCalendarInterval", plist)

    def test_plist_xml_has_well_formed_root(self) -> None:
        # Sanity-check the emit is at least parseable as XML by
        # plistlib (the OS's launchd would reject anything else).
        import plistlib
        from arq_tui.scheduling import generate_launchd_plist
        plan = _make_plan(schedule={"cron_expr": "15 2 * * *"})
        plist = generate_launchd_plist(plan)
        parsed = plistlib.loads(plist.encode("utf-8"))
        self.assertEqual(parsed["Label"], "com.arq-backup-tui.plan-P-test")
        self.assertIsInstance(parsed["ProgramArguments"], list)


class InstallListRemoveTests(unittest.TestCase):
    """Integration test exercising the install/list/remove cycle
    against a temp dir + a stub crontab command."""

    def setUp(self) -> None:
        # Create a fake `crontab` shim — a tiny shell script that
        # reads/writes a single file in a temp dir, mirroring the
        # real `crontab -l` / `crontab <file>` semantics.
        self.tmp = Path(tempfile.mkdtemp(prefix="arq-sched-test-"))
        self.tab_file = self.tmp / "fake.crontab"
        self.tab_file.write_text("")
        self.fake_cmd = self.tmp / "fake-crontab"
        self.fake_cmd.write_text(
            "#!/bin/sh\n"
            "set -e\n"
            f'TAB="{self.tab_file}"\n'
            'if [ "$1" = "-l" ]; then\n'
            "  cat \"$TAB\"\n"
            "  exit 0\n"
            "fi\n"
            'cp "$1" "$TAB"\n'
        )
        self.fake_cmd.chmod(0o755)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_install_creates_crontab_entry(self) -> None:
        from arq_tui.scheduling import (
            install_schedule, list_schedules,
        )
        plan = _make_plan(plan_id="X",
                          schedule={"cron_expr": "0 4 * * *"})
        install_schedule(
            plan, kind="cron",
            crontab_cmd=str(self.fake_cmd),
            executable="/usr/bin/python3",
        )
        # The fake crontab now holds our entry.
        contents = self.tab_file.read_text()
        self.assertIn("# arq-backup-tui:plan=X", contents)
        # list_schedules should find it.
        found = list_schedules(
            kind="cron", crontab_cmd=str(self.fake_cmd),
        )
        plan_ids = [pid for pid, _, _ in found]
        self.assertIn("X", plan_ids)

    def test_install_then_remove_round_trip(self) -> None:
        from arq_tui.scheduling import (
            install_schedule, remove_schedule,
        )
        plan = _make_plan(plan_id="Y",
                          schedule={"cron_expr": "0 5 * * *"})
        install_schedule(
            plan, kind="cron",
            crontab_cmd=str(self.fake_cmd),
        )
        n = remove_schedule(
            "Y", kind="cron", crontab_cmd=str(self.fake_cmd),
        )
        self.assertEqual(n, 1)
        self.assertNotIn(
            "# arq-backup-tui:plan=Y",
            self.tab_file.read_text(),
        )

    def test_re_install_replaces_rather_than_duplicates(self) -> None:
        from arq_tui.scheduling import install_schedule
        plan = _make_plan(plan_id="Z",
                          schedule={"cron_expr": "0 6 * * *"})
        install_schedule(plan, kind="cron",
                         crontab_cmd=str(self.fake_cmd))
        plan.schedule = {"cron_expr": "30 6 * * *"}
        install_schedule(plan, kind="cron",
                         crontab_cmd=str(self.fake_cmd))
        contents = self.tab_file.read_text()
        # Exactly one marker line for plan Z.
        self.assertEqual(
            contents.count("# arq-backup-tui:plan=Z"), 1,
        )
        # The cron expression reflects the second install.
        self.assertIn("30 6 * * *", contents)

    def test_launchd_install_writes_plist_to_dir(self) -> None:
        from arq_tui.scheduling import install_schedule
        plan = _make_plan(plan_id="L",
                          schedule={"cron_expr": "0 7 * * *"})
        agents = self.tmp / "LaunchAgents"
        install_schedule(
            plan, kind="launchd",
            launch_agents_dir=agents,
        )
        plist_files = list(agents.glob(
            "com.arq-backup-tui.plan-*.plist",
        ))
        self.assertEqual(len(plist_files), 1)
        body = plist_files[0].read_text()
        self.assertIn(
            "com.arq-backup-tui.plan-L", body,
        )


if __name__ == "__main__":
    unittest.main()
