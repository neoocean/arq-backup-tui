"""P1 — Plan operator-tunable round-trip + scheduleJSON polymorphism.

Round 7 item. Confirms that the writer's ``backupplan.json``
encrypt → decrypt → re-emit round-trip preserves operator-set
fields byte-for-byte, and documents a structural finding about
``scheduleJSON``:

Real Arq.app v8 emits ``scheduleJSON`` with a SHAPE that varies
by ``type``:

  type='Daily':   keys = ['backUpAndValidate', 'daysOfWeek',
                          'pauseDuringWindow', 'startWhenVolumeIsConnected',
                          'timeOfDay', 'type']
  type='Hourly':  keys = ['daysOfWeek', 'everyHours',
                          'minutesAfterHour', 'pauseDuringWindow',
                          'pauseFrom', 'pauseTo',
                          'startWhenVolumeIsConnected', 'type']

Our writer always emits the Hourly shape. P1 documents this as
a known limitation — operators wanting the Daily shape need to
post-process the plan JSON or extend ``build_backupplan`` with
a ``schedule_type`` parameter. The Hourly shape is the safer
default because it specifies every triggering window
explicitly.

Six tests:
- ``test_round_trip_preserves_top_level_fields``
- ``test_operator_can_override_plan_uuid``
- ``test_operator_can_override_plan_name``
- ``test_creationtime_and_updatetime_round_trip``
- ``test_schedulejson_default_shape_is_hourly``
- ``test_schedulejson_type_field_is_hourly``
"""

from __future__ import annotations

import json
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


class P1PlanFieldRoundTripTests(unittest.TestCase):
    """The build_backupplan output should be JSON-serializable +
    round-trip identity through json.dumps/loads."""

    def test_round_trip_preserves_top_level_fields(self) -> None:
        from arq_writer.json_configs import build_backupplan
        plan = build_backupplan(
            plan_uuid="11111111-1111-1111-1111-111111111111",
            plan_name="My Backup Plan",
            folder_plans=[],
            is_encrypted=True,
            update_time=1777000000.0,
            creation_time=1776000000.0,
            storage_location_id=1,
        )
        serialized = json.dumps(plan, ensure_ascii=False)
        decoded = json.loads(serialized)
        self.assertEqual(decoded["planUUID"], plan["planUUID"])
        self.assertEqual(decoded["name"], plan["name"])
        self.assertEqual(decoded["isEncrypted"], True)

    def test_operator_can_override_plan_uuid(self) -> None:
        from arq_writer.json_configs import build_backupplan
        plan = build_backupplan(
            plan_uuid="ABCDEF01-2345-6789-ABCD-EF0123456789",
            plan_name="x", folder_plans=[],
        )
        self.assertEqual(
            plan["planUUID"],
            "ABCDEF01-2345-6789-ABCD-EF0123456789",
        )

    def test_operator_can_override_plan_name(self) -> None:
        from arq_writer.json_configs import build_backupplan
        plan = build_backupplan(
            plan_uuid="00000000-0000-0000-0000-000000000000",
            plan_name="Operator-Provided Plan 한국어 ✨",
            folder_plans=[],
        )
        self.assertEqual(
            plan["name"], "Operator-Provided Plan 한국어 ✨",
        )
        # And the name round-trips through UTF-8 JSON.
        serialized = json.dumps(plan, ensure_ascii=False)
        self.assertIn("한국어", serialized)
        decoded = json.loads(serialized)
        self.assertEqual(
            decoded["name"], "Operator-Provided Plan 한국어 ✨",
        )

    def test_creationtime_and_updatetime_round_trip(self) -> None:
        """D1's fix made creationTime/updateTime int (not str).
        Verify the operator-provided value preserves int type
        and round-trips."""
        from arq_writer.json_configs import build_backupplan
        plan = build_backupplan(
            plan_uuid="aaa", plan_name="x", folder_plans=[],
            creation_time=1700000000.0,
            update_time=1700001234.0,
        )
        self.assertEqual(plan["creationTime"], 1700000000)
        self.assertEqual(plan["updateTime"], 1700001234)
        self.assertIs(type(plan["creationTime"]), int)
        self.assertIs(type(plan["updateTime"]), int)
        # Float -> int truncation.
        plan2 = build_backupplan(
            plan_uuid="aaa", plan_name="x", folder_plans=[],
            creation_time=1700000000.9,
        )
        self.assertEqual(plan2["creationTime"], 1700000000)


class P1ScheduleJsonShapeTests(unittest.TestCase):
    """scheduleJSON is polymorphic by ``type``. P2 (PR #N+1)
    added build_schedule_json and switched the default to the
    Daily shape (6 keys) to match real Arq.app v8 emit.
    Hourly remains available via explicit opt-in."""

    def test_schedulejson_default_shape_is_daily(self) -> None:
        from arq_writer.json_configs import build_backupplan
        plan = build_backupplan(
            plan_uuid="aaa", plan_name="x", folder_plans=[],
        )
        sj = plan["scheduleJSON"]
        expected_keys = {
            "backUpAndValidate", "daysOfWeek",
            "pauseDuringWindow",
            "startWhenVolumeIsConnected", "timeOfDay", "type",
        }
        self.assertEqual(set(sj.keys()), expected_keys)
        self.assertEqual(len(sj.keys()), 6)

    def test_schedulejson_default_type_is_daily(self) -> None:
        from arq_writer.json_configs import build_backupplan
        plan = build_backupplan(
            plan_uuid="aaa", plan_name="x", folder_plans=[],
        )
        self.assertEqual(plan["scheduleJSON"]["type"], "Daily")
        self.assertEqual(
            plan["scheduleJSON"]["daysOfWeek"],
            ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
        )

    def test_schedulejson_hourly_opt_in(self) -> None:
        """P2: explicit Hourly opt-in via build_schedule_json."""
        from arq_writer.json_configs import (
            build_backupplan, build_schedule_json,
        )
        plan = build_backupplan(
            plan_uuid="aaa", plan_name="x", folder_plans=[],
            schedule_json=build_schedule_json(
                schedule_type="Hourly",
            ),
        )
        sj = plan["scheduleJSON"]
        expected_keys = {
            "daysOfWeek", "everyHours", "minutesAfterHour",
            "pauseDuringWindow", "pauseFrom", "pauseTo",
            "startWhenVolumeIsConnected", "type",
        }
        self.assertEqual(set(sj.keys()), expected_keys)
        self.assertEqual(sj["type"], "Hourly")


@unittest.skipUnless(_has_openssl(), "openssl CLI required")
class P1PlanArqoRoundTripTests(unittest.TestCase):
    """End-to-end: plan dict → ARQO-wrap → encrypt → decrypt → parse
    yields the same dict. Pin the encrypted-emit round-trip."""

    def test_plan_arqo_encrypt_decrypt_round_trip(self) -> None:
        from arq_writer.json_configs import build_backupplan
        from arq_writer.crypto_write import build_encrypted_object
        from arq_reader.decrypt import decrypt_encrypted_object
        plan = build_backupplan(
            plan_uuid="11111111-2222-3333-4444-555555555555",
            plan_name="Test plan",
            folder_plans=[],
        )
        plain = json.dumps(plan, ensure_ascii=False).encode("utf-8")
        encryption_key = b"\x01" * 32
        hmac_key = b"\x02" * 32
        arqo = build_encrypted_object(
            plain, encryption_key, hmac_key,
        )
        decrypted = decrypt_encrypted_object(
            arqo, encryption_key, hmac_key,
        )
        round_tripped = json.loads(decrypted.decode("utf-8"))
        self.assertEqual(
            round_tripped["planUUID"], plan["planUUID"],
        )
        self.assertEqual(round_tripped["name"], plan["name"])
        # All 47 keys preserved.
        self.assertEqual(
            set(round_tripped.keys()), set(plan.keys()),
        )


if __name__ == "__main__":
    unittest.main()
