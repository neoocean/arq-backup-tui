"""A보완-4 — Plan.skip_tm_excludes wiring through TUI → Backup.

E2-new added ``skip_tm_excludes`` to ``Backup`` + ``build_backup``.
A보완-4 connects it to the TUI plan layer:

1. ``Plan`` dataclass gains a ``skip_tm_excludes: bool = False``
   field (matching the Arq.app v8 default).
2. ``PlanRegistry`` JSON round-trip preserves the field.
3. ``BackupWorker`` accepts the kwarg + threads it to the
   underlying ``Backup`` instance.
4. ``BackupRunScreen`` reads ``plan.skip_tm_excludes`` and passes
   it to ``BackupWorker``.

This module pins (1) + (2) + (3) directly. (4) is integration-
tested via the existing TUI smoke tests.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path


try:
    import textual  # noqa: F401
    HAS_TEXTUAL = True
except ImportError:  # pragma: no cover
    HAS_TEXTUAL = False


@unittest.skipUnless(HAS_TEXTUAL, "textual not installed")
class PlanSkipTMExcludesFieldTests(unittest.TestCase):
    """Dataclass field + JSON round-trip."""

    def _new_plan(self, skip_tm_excludes: bool = False):
        from arq_tui.state import Plan
        return Plan(
            plan_id="p1",
            name="tm-test",
            sources=["/data"],
            destination_kind="local",
            destination={"path": "/Volumes/arq"},
            skip_tm_excludes=skip_tm_excludes,
        )

    def test_default_is_false(self) -> None:
        p = self._new_plan()
        self.assertFalse(p.skip_tm_excludes)

    def test_field_round_trips_through_planregistry(self) -> None:
        from arq_tui.state import PlanRegistry
        with tempfile.TemporaryDirectory() as td:
            reg = PlanRegistry(config_dir=Path(td))
            reg.save(self._new_plan(skip_tm_excludes=True))
            loaded = reg.list_plans()
            self.assertEqual(len(loaded), 1)
            self.assertTrue(loaded[0].skip_tm_excludes)

    def test_legacy_plan_json_loads_with_default_false(self) -> None:
        """A pre-A보완-4 plan JSON (no skip_tm_excludes key) loads
        with the field defaulted to False."""
        import json
        from arq_tui.state import PlanRegistry
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td)
            (cfg / "plans").mkdir(parents=True)
            legacy = {
                "plan_id": "legacy",
                "name": "old",
                "sources": ["/x"],
                "destination_kind": "local",
                "destination": {"path": "/y"},
            }
            (cfg / "plans" / "legacy.json").write_text(
                json.dumps(legacy),
            )
            reg = PlanRegistry(config_dir=cfg)
            loaded = reg.list_plans()
            self.assertEqual(len(loaded), 1)
            self.assertFalse(loaded[0].skip_tm_excludes)


class BackupWorkerSkipTMExcludesWiringTests(unittest.TestCase):
    """``BackupWorker`` accepts skip_tm_excludes + stores it."""

    @unittest.skipUnless(HAS_TEXTUAL, "textual not installed")
    def test_worker_stores_skip_tm_excludes(self) -> None:
        from arq_tui.workers import BackupWorker
        # Stub target — BackupWorker stores target.app for
        # call_from_thread + post_message for events.
        class _StubApp:
            def call_from_thread(self, fn, *a, **kw):
                fn(*a, **kw)
        class _StubTarget:
            app = _StubApp()
            def post_message(self, m):
                pass
        worker = BackupWorker(
            _StubTarget(),
            sources=["/x"],
            dest_root="/y",
            encryption_password="pw",
            skip_tm_excludes=True,
        )
        self.assertTrue(worker.skip_tm_excludes)

    @unittest.skipUnless(HAS_TEXTUAL, "textual not installed")
    def test_worker_default_skip_tm_excludes_is_false(self) -> None:
        from arq_tui.workers import BackupWorker
        class _StubApp:
            def call_from_thread(self, fn, *a, **kw):
                fn(*a, **kw)
        class _StubTarget:
            app = _StubApp()
            def post_message(self, m):
                pass
        worker = BackupWorker(
            _StubTarget(),
            sources=["/x"],
            dest_root="/y",
            encryption_password="pw",
        )
        self.assertFalse(worker.skip_tm_excludes)


if __name__ == "__main__":
    unittest.main()
