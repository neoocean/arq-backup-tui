"""P3 — emailReportJSON shape polymorphism.

Real Arq.app v8 emits ``emailReportJSON`` as 6 keys when SMTP
is NOT configured (the fresh-plan default), and adds 6 more
when SMTP is configured. Our writer's pre-P3 emit was always
the 10-key (without 2 real ones) shape, which produced a
schema-level drift at the nested-dict layer.

Sampled 2026-05-11 against ``/Volumes/arqbackup1``:

Real Arq.app v8 default (6 keys):
  authenticationType, connectionSecurity, port,
  reportHELOUseIP, type, when

Real Arq.app v8 with SMTP (12 keys):
  + fromAddress, hostname, startTLS, subject, toAddress, username

P3's fix adds ``build_email_report_json`` factory + switches
``build_backupplan``'s default to the 6-key shape. The
``smtp-configured`` shape (12 keys) is opt-in via any non-None
SMTP kwarg.
"""

from __future__ import annotations

import json
import unittest


class P3EmailReportShapeTests(unittest.TestCase):

    def test_default_is_six_key_not_configured_shape(self) -> None:
        from arq_writer.json_configs import build_email_report_json
        er = build_email_report_json()
        self.assertEqual(
            set(er.keys()),
            {"authenticationType", "connectionSecurity", "port",
             "reportHELOUseIP", "type", "when"},
        )
        self.assertEqual(len(er), 6)

    def test_default_values_match_real_arq_v8(self) -> None:
        """Pin the exact default values that match what real
        Arq.app v8 emits on a freshly-provisioned plan."""
        from arq_writer.json_configs import build_email_report_json
        er = build_email_report_json()
        self.assertEqual(er["authenticationType"], "none")
        self.assertEqual(er["connectionSecurity"], "none")
        self.assertEqual(er["port"], 587)
        self.assertEqual(er["reportHELOUseIP"], True)
        self.assertEqual(er["type"], "custom")
        self.assertEqual(er["when"], "never")

    def test_smtp_configured_shape_has_twelve_keys(self) -> None:
        from arq_writer.json_configs import build_email_report_json
        er = build_email_report_json(
            hostname="smtp.example.com",
            from_address="backup@example.com",
            to_address="ops@example.com",
            username="backup",
            subject="Arq backup report",
            start_tls=True,
        )
        self.assertEqual(
            set(er.keys()),
            {"authenticationType", "connectionSecurity",
             "fromAddress", "hostname", "port",
             "reportHELOUseIP", "startTLS", "subject",
             "toAddress", "type", "username", "when"},
        )
        self.assertEqual(len(er), 12)
        self.assertEqual(er["hostname"], "smtp.example.com")
        self.assertEqual(er["startTLS"], True)

    def test_one_smtp_kwarg_promotes_to_twelve_key_shape(
        self,
    ) -> None:
        """Providing ANY single SMTP slot switches the shape.
        Pin this so future callers don't get a half-shape they
        weren't expecting."""
        from arq_writer.json_configs import build_email_report_json
        er = build_email_report_json(hostname="smtp.x.com")
        self.assertIn("hostname", er)
        self.assertIn("fromAddress", er)
        self.assertEqual(er["fromAddress"], "")
        self.assertEqual(len(er), 12)

    def test_backupplan_default_emits_six_key_email_shape(
        self,
    ) -> None:
        """End-to-end: build_backupplan with no email override
        emits the 6-key shape."""
        from arq_writer.json_configs import build_backupplan
        plan = build_backupplan(
            plan_uuid="x", plan_name="y", folder_plans=[],
        )
        er = plan["emailReportJSON"]
        self.assertEqual(len(er), 6)
        self.assertNotIn("fromAddress", er)
        self.assertNotIn("hostname", er)
        self.assertIn("connectionSecurity", er)
        self.assertIn("reportHELOUseIP", er)

    def test_backupplan_smtp_override_round_trips(self) -> None:
        from arq_writer.json_configs import (
            build_backupplan, build_email_report_json,
        )
        custom = build_email_report_json(
            hostname="mail.example.com",
            from_address="from@example.com",
            to_address="to@example.com",
            when="onError",
        )
        plan = build_backupplan(
            plan_uuid="x", plan_name="y", folder_plans=[],
            email_report_json=custom,
        )
        er = plan["emailReportJSON"]
        self.assertEqual(len(er), 12)
        self.assertEqual(er["hostname"], "mail.example.com")
        self.assertEqual(er["when"], "onError")
        # JSON round-trip integrity.
        decoded = json.loads(json.dumps(plan, ensure_ascii=False))
        self.assertEqual(decoded["emailReportJSON"], er)


if __name__ == "__main__":
    unittest.main()
