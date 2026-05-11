"""V6 — validator accepts polymorphic nested-dict shapes.

Round 9 item. P1/P2/P3 surfaced three nested dicts in
``backupplan.json`` whose shapes vary by a discriminator key:

- ``scheduleJSON`` by ``type`` (Daily vs Hourly)
- ``transferRateJSON`` by ``scheduleType`` (Always vs Scheduled)
- ``emailReportJSON`` by SMTP-configured vs not

V6 extends the L4 validator with polymorphism-aware checks so a
destination conforming to any of the legal shapes passes, and a
destination with a NEW field set surfaces a clear diagnostic.

This is purely a validator-side change; the writer's behaviour
is unchanged. The point is to catch future drift early — if
Arq.app v8.5 adds a new schedule type "Weekly", the L4 report
will flag the unknown type rather than silently accepting it.
"""

from __future__ import annotations

import unittest


class V6PolymorphicShapesAcceptedTests(unittest.TestCase):
    """Real Arq.app v8's emit + our writer's emit + the
    operator's explicit overrides all produce shapes the L4
    validator should accept."""

    def _build_report(self):
        from arq_validator.compatibility import ComplianceReport
        return ComplianceReport(destination_root="/", computer_uuid="x")

    def test_scheduledjson_daily_accepted(self) -> None:
        from arq_validator.compatibility import (
            _check_polymorphic_schedule_json,
        )
        report = self._build_report()
        _check_polymorphic_schedule_json({
            "backUpAndValidate": True,
            "daysOfWeek": ["Mon", "Tue"],
            "pauseDuringWindow": False,
            "startWhenVolumeIsConnected": False,
            "timeOfDay": "12:00",
            "type": "Daily",
        }, report)
        failures = [i for i in report.checks if not i.passed]
        self.assertEqual(
            failures, [],
            f"Daily shape should pass; got failures: "
            f"{[(f.name, f.message) for f in failures]}",
        )

    def test_scheduledjson_hourly_accepted(self) -> None:
        from arq_validator.compatibility import (
            _check_polymorphic_schedule_json,
        )
        report = self._build_report()
        _check_polymorphic_schedule_json({
            "daysOfWeek": ["Mon"],
            "everyHours": 6,
            "minutesAfterHour": 0,
            "pauseDuringWindow": False,
            "pauseFrom": "09:00",
            "pauseTo": "17:00",
            "startWhenVolumeIsConnected": False,
            "type": "Hourly",
        }, report)
        failures = [i for i in report.checks if not i.passed]
        self.assertEqual(failures, [])

    def test_unknown_schedule_type_flagged(self) -> None:
        from arq_validator.compatibility import (
            _check_polymorphic_schedule_json,
        )
        report = self._build_report()
        _check_polymorphic_schedule_json({
            "type": "Weekly",
            "daysOfWeek": ["Mon"],
        }, report)
        failures = [i for i in report.checks if not i.passed]
        self.assertEqual(len(failures), 1)
        # The check's name embeds the offending type value; the
        # message lists the expected types.
        self.assertIn("Weekly", failures[0].name)
        self.assertIn("Daily", failures[0].message)

    def test_extra_key_in_daily_shape_flagged(self) -> None:
        from arq_validator.compatibility import (
            _check_polymorphic_schedule_json,
        )
        report = self._build_report()
        _check_polymorphic_schedule_json({
            "backUpAndValidate": True,
            "daysOfWeek": ["Mon"],
            "pauseDuringWindow": False,
            "startWhenVolumeIsConnected": False,
            "timeOfDay": "12:00",
            "type": "Daily",
            "everyHours": 6,  # extra: belongs to Hourly only
        }, report)
        failures = [i for i in report.checks if not i.passed]
        self.assertGreater(
            len(failures), 0,
            "extra Hourly-only key in Daily shape should be flagged",
        )
        self.assertIn("everyHours", failures[0].message)

    def test_transfer_rate_always_accepted(self) -> None:
        from arq_validator.compatibility import (
            _check_polymorphic_transfer_rate_json,
        )
        report = self._build_report()
        _check_polymorphic_transfer_rate_json({
            "daysOfWeek": ["Mon"],
            "enabled": False,
            "endTimeOfDay": "17:00",
            "scheduleType": "Always",
            "startTimeOfDay": "08:00",
        }, report)
        failures = [i for i in report.checks if not i.passed]
        self.assertEqual(failures, [])

    def test_transfer_rate_scheduled_accepted(self) -> None:
        from arq_validator.compatibility import (
            _check_polymorphic_transfer_rate_json,
        )
        report = self._build_report()
        _check_polymorphic_transfer_rate_json({
            "daysOfWeek": ["Mon"],
            "enabled": True,
            "endTimeOfDay": "17:00",
            "maxKBPS": 500,
            "scheduleType": "Scheduled",
            "startTimeOfDay": "08:00",
        }, report)
        failures = [i for i in report.checks if not i.passed]
        self.assertEqual(failures, [])

    def test_transfer_rate_maxkbps_in_always_flagged(self) -> None:
        """Always shape must NOT have maxKBPS — flag it."""
        from arq_validator.compatibility import (
            _check_polymorphic_transfer_rate_json,
        )
        report = self._build_report()
        _check_polymorphic_transfer_rate_json({
            "daysOfWeek": ["Mon"],
            "enabled": False,
            "endTimeOfDay": "17:00",
            "maxKBPS": 100,
            "scheduleType": "Always",
            "startTimeOfDay": "08:00",
        }, report)
        failures = [i for i in report.checks if not i.passed]
        self.assertGreater(len(failures), 0)
        self.assertIn("maxKBPS", failures[0].message)

    def test_email_report_not_configured_accepted(self) -> None:
        from arq_validator.compatibility import (
            _check_polymorphic_email_report_json,
        )
        report = self._build_report()
        _check_polymorphic_email_report_json({
            "authenticationType": "none",
            "connectionSecurity": "none",
            "port": 587,
            "reportHELOUseIP": True,
            "type": "custom",
            "when": "never",
        }, report)
        failures = [i for i in report.checks if not i.passed]
        self.assertEqual(failures, [])

    def test_email_report_smtp_configured_accepted(self) -> None:
        from arq_validator.compatibility import (
            _check_polymorphic_email_report_json,
        )
        report = self._build_report()
        _check_polymorphic_email_report_json({
            "authenticationType": "plain",
            "connectionSecurity": "STARTTLS",
            "fromAddress": "a@b",
            "hostname": "smtp.example.com",
            "port": 587,
            "reportHELOUseIP": True,
            "startTLS": True,
            "subject": "Report",
            "toAddress": "c@d",
            "type": "custom",
            "username": "u",
            "when": "onError",
        }, report)
        failures = [i for i in report.checks if not i.passed]
        self.assertEqual(failures, [])

    def test_email_report_partial_smtp_flagged(self) -> None:
        """A 10-key shape with only SOME SMTP fields is neither
        the not-configured nor fully-configured shape — flag it.
        This catches the pre-P3 writer emit (10 keys missing 2
        and adding 6 different ones)."""
        from arq_validator.compatibility import (
            _check_polymorphic_email_report_json,
        )
        report = self._build_report()
        _check_polymorphic_email_report_json({
            # Pre-P3 our writer's 10-key shape.
            "authenticationType": "none",
            "fromAddress": "",
            "hostname": "",
            "port": 587,
            "startTLS": False,
            "subject": "",
            "toAddress": "",
            "type": "custom",
            "username": "",
            "when": "never",
        }, report)
        failures = [i for i in report.checks if not i.passed]
        self.assertEqual(len(failures), 1)
        self.assertIn("connectionSecurity", failures[0].message)


if __name__ == "__main__":
    unittest.main()
