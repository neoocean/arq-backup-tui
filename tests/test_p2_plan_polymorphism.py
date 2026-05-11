"""P2 — backupplan.json scheduleJSON + transferRateJSON
polymorphism.

Real Arq.app v8 emits two polymorphic sub-objects in
``backupplan.json``:

- **scheduleJSON** varies by ``type`` discriminator:
  - ``type='Daily'``: 6 keys (backUpAndValidate, daysOfWeek,
    pauseDuringWindow, startWhenVolumeIsConnected,
    timeOfDay, type)
  - ``type='Hourly'``: 8 keys (daysOfWeek, everyHours,
    minutesAfterHour, pauseDuringWindow, pauseFrom, pauseTo,
    startWhenVolumeIsConnected, type)

- **transferRateJSON** varies by ``scheduleType``:
  - ``scheduleType='Always'``: 5 keys (no maxKBPS)
  - ``scheduleType='Scheduled'``: 6 keys (includes maxKBPS)

P2 (this PR) adds ``build_schedule_json`` /
``build_transfer_rate_json`` factories and switches
``build_backupplan``'s defaults to ``Daily`` / ``Always`` —
the shapes Arq.app v8 emits on a freshly-provisioned plan.

12 tests pin each polymorphism shape + invalid-type behaviour
+ opt-in overrides + nested-record round-trip.
"""

from __future__ import annotations

import json
import unittest


class P2ScheduleJsonPolymorphismTests(unittest.TestCase):
    """build_schedule_json emits Daily or Hourly shape based on
    ``schedule_type`` parameter."""

    def test_daily_emits_six_keys(self) -> None:
        from arq_writer.json_configs import build_schedule_json
        sj = build_schedule_json(schedule_type="Daily")
        self.assertEqual(
            set(sj.keys()),
            {"backUpAndValidate", "daysOfWeek",
             "pauseDuringWindow", "startWhenVolumeIsConnected",
             "timeOfDay", "type"},
        )
        self.assertEqual(sj["type"], "Daily")

    def test_hourly_emits_eight_keys(self) -> None:
        from arq_writer.json_configs import build_schedule_json
        sj = build_schedule_json(schedule_type="Hourly")
        self.assertEqual(
            set(sj.keys()),
            {"daysOfWeek", "everyHours", "minutesAfterHour",
             "pauseDuringWindow", "pauseFrom", "pauseTo",
             "startWhenVolumeIsConnected", "type"},
        )
        self.assertEqual(sj["type"], "Hourly")

    def test_daily_default_time_of_day(self) -> None:
        from arq_writer.json_configs import build_schedule_json
        sj = build_schedule_json(schedule_type="Daily")
        self.assertEqual(sj["timeOfDay"], "12:00")
        self.assertEqual(sj["backUpAndValidate"], True)

    def test_hourly_override_every_hours(self) -> None:
        from arq_writer.json_configs import build_schedule_json
        sj = build_schedule_json(
            schedule_type="Hourly", every_hours=6,
            minutes_after_hour=15,
        )
        self.assertEqual(sj["everyHours"], 6)
        self.assertEqual(sj["minutesAfterHour"], 15)

    def test_unknown_schedule_type_raises(self) -> None:
        from arq_writer.json_configs import build_schedule_json
        with self.assertRaises(ValueError):
            build_schedule_json(schedule_type="Weekly")

    def test_pause_window_round_trip(self) -> None:
        """Daily ignores pauseFrom/pauseTo — those live on
        Hourly only. Pin that the operator-provided values
        survive on Hourly and don't leak into Daily."""
        from arq_writer.json_configs import build_schedule_json
        daily = build_schedule_json(
            schedule_type="Daily",
            pause_from="10:00", pause_to="18:00",
        )
        self.assertNotIn("pauseFrom", daily)
        self.assertNotIn("pauseTo", daily)
        hourly = build_schedule_json(
            schedule_type="Hourly",
            pause_from="10:00", pause_to="18:00",
        )
        self.assertEqual(hourly["pauseFrom"], "10:00")
        self.assertEqual(hourly["pauseTo"], "18:00")


class P2TransferRateJsonPolymorphismTests(unittest.TestCase):
    """build_transfer_rate_json emits Always or Scheduled
    shape based on ``schedule_type`` parameter."""

    def test_always_emits_five_keys_no_maxkbps(self) -> None:
        from arq_writer.json_configs import build_transfer_rate_json
        tr = build_transfer_rate_json(schedule_type="Always")
        self.assertEqual(
            set(tr.keys()),
            {"daysOfWeek", "enabled", "endTimeOfDay",
             "scheduleType", "startTimeOfDay"},
        )
        self.assertNotIn("maxKBPS", tr)
        self.assertEqual(tr["scheduleType"], "Always")

    def test_scheduled_emits_six_keys_with_maxkbps(self) -> None:
        from arq_writer.json_configs import build_transfer_rate_json
        tr = build_transfer_rate_json(
            schedule_type="Scheduled", max_kbps=500,
        )
        self.assertEqual(
            set(tr.keys()),
            {"daysOfWeek", "enabled", "endTimeOfDay",
             "maxKBPS", "scheduleType", "startTimeOfDay"},
        )
        self.assertEqual(tr["maxKBPS"], 500)
        self.assertEqual(tr["scheduleType"], "Scheduled")

    def test_unknown_schedule_type_raises(self) -> None:
        from arq_writer.json_configs import build_transfer_rate_json
        with self.assertRaises(ValueError):
            build_transfer_rate_json(schedule_type="Adaptive")


class P2BackupplanDefaultsTests(unittest.TestCase):
    """build_backupplan defaults to Daily + Always shapes (the
    real Arq.app v8 defaults sampled 2026-05-11)."""

    def test_default_schedulejson_is_daily(self) -> None:
        from arq_writer.json_configs import build_backupplan
        plan = build_backupplan(
            plan_uuid="x", plan_name="y", folder_plans=[],
        )
        self.assertEqual(plan["scheduleJSON"]["type"], "Daily")
        self.assertEqual(len(plan["scheduleJSON"]), 6)

    def test_default_transferratejson_is_always(self) -> None:
        from arq_writer.json_configs import build_backupplan
        plan = build_backupplan(
            plan_uuid="x", plan_name="y", folder_plans=[],
        )
        self.assertEqual(
            plan["transferRateJSON"]["scheduleType"], "Always",
        )
        self.assertEqual(len(plan["transferRateJSON"]), 5)
        self.assertNotIn(
            "maxKBPS", plan["transferRateJSON"],
        )

    def test_operator_can_override_to_hourly(self) -> None:
        from arq_writer.json_configs import (
            build_backupplan, build_schedule_json,
        )
        plan = build_backupplan(
            plan_uuid="x", plan_name="y", folder_plans=[],
            schedule_json=build_schedule_json(
                schedule_type="Hourly", every_hours=4,
            ),
        )
        self.assertEqual(plan["scheduleJSON"]["type"], "Hourly")
        self.assertEqual(plan["scheduleJSON"]["everyHours"], 4)

    def test_operator_can_override_to_scheduled_with_limit(
        self,
    ) -> None:
        from arq_writer.json_configs import (
            build_backupplan, build_transfer_rate_json,
        )
        plan = build_backupplan(
            plan_uuid="x", plan_name="y", folder_plans=[],
            transfer_rate_json=build_transfer_rate_json(
                schedule_type="Scheduled",
                enabled=True, max_kbps=1000,
            ),
        )
        self.assertEqual(
            plan["transferRateJSON"]["scheduleType"], "Scheduled",
        )
        self.assertEqual(plan["transferRateJSON"]["maxKBPS"], 1000)
        self.assertEqual(plan["transferRateJSON"]["enabled"], True)


if __name__ == "__main__":
    unittest.main()
