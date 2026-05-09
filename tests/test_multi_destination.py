"""Tests for the multi-destination plan support (PR-B3).

Two layers:

- Plan model: ``additional_destinations`` field round-trips through
  PlanRegistry, ``iter_destinations()`` yields primary + extras
  in the right order.
- Runner: ``run_plan_multi`` runs Backup once per destination,
  returns per-destination outcomes, isolates failures so a single
  bad destination doesn't drop the others.
"""

from __future__ import annotations

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


class PlanIterDestinationsTests(unittest.TestCase):

    def test_primary_only_yields_one(self) -> None:
        from arq_tui.state import Plan
        plan = Plan(
            plan_id="P", name="t",
            destination_kind="local",
            destination={"path": "/Volumes/dst1"},
        )
        dests = plan.iter_destinations()
        self.assertEqual(len(dests), 1)
        self.assertEqual(dests[0]["kind"], "local")
        self.assertEqual(dests[0]["path"], "/Volumes/dst1")

    def test_primary_plus_additional_in_order(self) -> None:
        from arq_tui.state import Plan
        plan = Plan(
            plan_id="P", name="t",
            destination_kind="local",
            destination={"path": "/Volumes/main"},
            additional_destinations=[
                {"kind": "sftp", "host": "h1", "user": "u",
                 "path": "/r"},
                {"kind": "local", "path": "/Volumes/mirror"},
            ],
        )
        dests = plan.iter_destinations()
        self.assertEqual(len(dests), 3)
        # Primary first.
        self.assertEqual(dests[0]["path"], "/Volumes/main")
        # Then SFTP, then local mirror — preserving operator-
        # supplied order so cron-vs-tui executions never reorder.
        self.assertEqual(dests[1]["kind"], "sftp")
        self.assertEqual(dests[1]["host"], "h1")
        self.assertEqual(dests[2]["path"], "/Volumes/mirror")

    def test_empty_primary_skipped(self) -> None:
        # Operators migrating to additional_destinations sometimes
        # leave the legacy primary blank. iter_destinations() must
        # NOT yield a phantom empty primary.
        from arq_tui.state import Plan
        plan = Plan(
            plan_id="P", name="t",
            destination_kind="local",
            destination={},
            additional_destinations=[
                {"kind": "local", "path": "/Volumes/only"},
            ],
        )
        dests = plan.iter_destinations()
        self.assertEqual(len(dests), 1)
        self.assertEqual(dests[0]["path"], "/Volumes/only")

    def test_kind_default_when_extra_omits_it(self) -> None:
        from arq_tui.state import Plan
        plan = Plan(
            plan_id="P", name="t",
            destination_kind="local",
            destination={"path": "/Volumes/a"},
            additional_destinations=[
                # Operator forgot 'kind'; default to local.
                {"path": "/Volumes/b"},
            ],
        )
        dests = plan.iter_destinations()
        self.assertEqual(dests[1]["kind"], "local")


class PlanRegistryRoundTripTests(unittest.TestCase):

    def test_additional_destinations_round_trip(self) -> None:
        from arq_tui.state import Plan, PlanRegistry
        with tempfile.TemporaryDirectory() as td:
            reg = PlanRegistry(config_dir=Path(td))
            extras = [
                {"kind": "local", "path": "/Volumes/mirror"},
                {"kind": "sftp", "host": "h", "user": "u",
                 "path": "/r"},
            ]
            reg.save(Plan(
                plan_id="P", name="t", sources=["/srv"],
                destination_kind="local",
                destination={"path": "/Volumes/main"},
                additional_destinations=extras,
            ))
            loaded = reg.list_plans()
            self.assertEqual(len(loaded), 1)
            self.assertEqual(
                loaded[0].additional_destinations, extras,
            )


@unittest.skipUnless(_has_openssl(), "openssl CLI required")
class RunPlanMultiTests(unittest.TestCase):

    def test_two_local_destinations_both_get_backup(self) -> None:
        from arq_tui.multi_destination import run_plan_multi
        from arq_tui.state import Plan
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            src = td / "src"
            src.mkdir()
            (src / "a.txt").write_text("alpha")
            d1 = td / "d1"
            d1.mkdir()
            d2 = td / "d2"
            d2.mkdir()
            plan = Plan(
                plan_id="P", name="multi-test",
                sources=[str(src)],
                destination_kind="local",
                destination={"path": str(d1)},
                additional_destinations=[
                    {"kind": "local", "path": str(d2)},
                ],
            )
            r = run_plan_multi(
                plan, encryption_password="pw",
            )
            self.assertTrue(r.all_ok)
            self.assertEqual(len(r.destinations), 2)
            # Each destination got its own computer-uuid subtree.
            cu_d1 = list(d1.iterdir())
            cu_d2 = list(d2.iterdir())
            self.assertEqual(len(cu_d1), 1)
            self.assertEqual(len(cu_d2), 1)
            # Per-destination metrics carried.
            for d in r.destinations:
                self.assertGreater(d.files_written, 0)
                self.assertGreater(d.bytes_plaintext, 0)
                self.assertIsNone(d.error)

    def test_one_failed_destination_does_not_drop_others(self) -> None:
        """Pointing one destination at a non-existent path should
        fail just THAT destination; the second destination still
        runs to completion."""
        from arq_tui.multi_destination import run_plan_multi
        from arq_tui.state import Plan
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            src = td / "src"
            src.mkdir()
            (src / "a.txt").write_text("alpha")
            ok_dst = td / "ok"
            ok_dst.mkdir()
            # The bad destination's path goes through a regular
            # file pretending to be a directory — Backup will
            # error out trying to mkdir under it.
            bad_path = td / "bad-not-a-dir"
            bad_path.write_bytes(b"this is a file, not a dir")
            plan = Plan(
                plan_id="P", name="partial",
                sources=[str(src)],
                destination_kind="local",
                destination={"path": str(bad_path)},
                additional_destinations=[
                    {"kind": "local", "path": str(ok_dst)},
                ],
            )
            r = run_plan_multi(
                plan, encryption_password="pw",
            )
            self.assertFalse(r.all_ok)
            self.assertTrue(r.any_failed)
            # The OK one still landed bytes on disk.
            cu_ok = list(ok_dst.iterdir())
            self.assertEqual(len(cu_ok), 1)
            ok_outcome = r.destinations[1]
            self.assertTrue(ok_outcome.ok)
            self.assertGreater(ok_outcome.files_written, 0)
            # The failed one carries an error string.
            failed_outcome = r.destinations[0]
            self.assertFalse(failed_outcome.ok)
            self.assertIsNotNone(failed_outcome.error)


if __name__ == "__main__":
    unittest.main()
