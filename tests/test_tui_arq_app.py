"""Tests for the read-only Arq.app (ArqAgent server.db) adapter.

Most tests run against a synthetic fixture DB that mirrors the subset
of Arq's schema the adapter reads, so they're hermetic (no Arq install
required). One smoke test runs against the operator's real
``server.db`` when present + readable, and is skipped otherwise.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from arq_tui.arq_app import (
    DEFAULT_SERVER_DB,
    ArqActivity,
    ArqAppSource,
    ArqStorageLocation,
    _folder_local_root,
    detect_arq_app,
)


def _build_fixture_db(path: Path) -> None:
    """Create a server.db-shaped fixture with the columns the adapter
    queries, populated with folder + sftp + cloud locations, three
    plans (buzhash + fixed + retain-all), and a handful of activities."""
    con = sqlite3.connect(str(path))
    con.executescript(
        """
        CREATE TABLE storage_locations (
            id INTEGER PRIMARY KEY, active INTEGER NOT NULL,
            password TEXT, json TEXT);
        CREATE TABLE backup_plans (
            id INTEGER PRIMARY KEY, active INTEGER NOT NULL,
            plan_uuid TEXT NOT NULL, storage_location_id INTEGER NOT NULL,
            last_backed_up REAL, encryption_password TEXT, json TEXT);
        CREATE TABLE activities (
            id INTEGER PRIMARY KEY, uuid TEXT NOT NULL, type TEXT NOT NULL,
            sub_type TEXT NOT NULL, message TEXT NOT NULL,
            plan_uuid TEXT, processed_files INTEGER NOT NULL,
            total_files INTEGER, processed_bytes INTEGER NOT NULL,
            total_bytes INTEGER, created_time REAL NOT NULL,
            finished_time REAL, aborted INTEGER NOT NULL,
            abort_reason TEXT, error_count INTEGER,
            activity_log_path TEXT);
        """
    )
    # Storage locations: folder (1), sftp (2), arqpremium cloud (3).
    sls = [
        (1, 1, json.dumps({
            "id": 1, "uuid": "SL-FOLDER", "name": "arqbackup1",
            "providerType": "folder", "planType": "backup",
            "path": "/Volumes/arqbackup1:/", "useSSL": False,
            "active": True, "hasPassword": True,
            "displayDescription": "Folder:/Volumes/arqbackup1:/",
        })),
        (2, 1, json.dumps({
            "id": 2, "uuid": "SL-SFTP", "name": "storagebox",
            "providerType": "sftp", "planType": "backup",
            "path": "/home", "hostname": "u1.example.de", "port": 23,
            "username": "u1", "useSSL": True, "active": True,
            "hasPassword": True,
        })),
        (3, 0, json.dumps({
            "id": 3, "uuid": "SL-CLOUD", "name": "Arq Cloud Storage",
            "providerType": "arqpremium", "planType": "backup",
            "active": False, "hasPassword": False,
        })),
    ]
    con.executemany(
        "INSERT INTO storage_locations(id,active,json) VALUES (?,?,?)", sls,
    )
    # Plans: buzhash (folder), fixed (sftp), retain-all (folder).
    plan_buzhash = {
        "planUUID": "PLAN-BUZHASH", "name": "Buzhash plan",
        "storageLocationId": 1, "useBuzhash": True,
        "useAPFSSnapshots": True, "isEncrypted": True,
        "retainAll": False, "retainHours": 24, "retainDays": 30,
        "retainWeeks": 0, "retainMonths": 12,
        "backupFolderPlansByUUID": {
            "F2": {"name": "docs", "localPath": "/Users/me/docs",
                   "wildcardExcludes": ["*.log"], "regexExcludes": [],
                   "skipTMExcludes": False},
            "F1": {"name": "code", "localPath": "/Users/me/code",
                   "wildcardExcludes": ["*.tmp"],
                   "regexExcludes": ["^build/"], "skipTMExcludes": True},
        },
    }
    plan_fixed = {
        "planUUID": "PLAN-FIXED", "name": "Fixed plan",
        "storageLocationId": 2, "useBuzhash": False,
        "useAPFSSnapshots": False, "isEncrypted": True,
        "retainAll": False, "retainMonths": 6,
        "backupFolderPlansByUUID": {
            "G1": {"name": "vol", "localPath": "/Volumes/data"},
        },
    }
    plan_retainall = {
        "planUUID": "PLAN-RETAINALL", "name": "Archive plan",
        "storageLocationId": 1, "useBuzhash": False,
        "retainAll": True,
        "backupFolderPlansByUUID": {},
    }
    con.executemany(
        "INSERT INTO backup_plans"
        "(id,active,plan_uuid,storage_location_id,last_backed_up,json)"
        " VALUES (?,?,?,?,?,?)",
        [
            (1, 1, "PLAN-BUZHASH", 1, 1_700_000_000.0,
             json.dumps(plan_buzhash)),
            (2, 1, "PLAN-FIXED", 2, None, json.dumps(plan_fixed)),
            (3, 0, "PLAN-RETAINALL", 1, None,
             json.dumps(plan_retainall)),
        ],
    )
    # Activities: an in-flight backup, a finished backup, an aborted
    # one, and a validate (sub_type differs from type).
    acts = [
        ("A-RUN", "backup", "backup", "Backing up", "PLAN-BUZHASH",
         10, 100, 500, 1000, 3000.0, None, 0, None, 0, "/logs/run"),
        ("A-DONE", "backup", "backup", "Idle", "PLAN-BUZHASH",
         100, 100, 1000, 1000, 2000.0, 2100.0, 0, None, 0, "/logs/done"),
        ("A-ABORT", "backup", "backup", "Idle", "PLAN-FIXED",
         0, None, 0, None, 1000.0, None, 1, "user cancelled", 2, None),
        ("A-VALIDATE", "backup", "validate", "Validating", "PLAN-BUZHASH",
         5, 5, 0, None, 500.0, 600.0, 0, None, 0, None),
    ]
    con.executemany(
        "INSERT INTO activities(uuid,type,sub_type,message,plan_uuid,"
        "processed_files,total_files,processed_bytes,total_bytes,"
        "created_time,finished_time,aborted,abort_reason,error_count,"
        "activity_log_path) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        acts,
    )
    con.commit()
    con.close()


class FolderPathTests(unittest.TestCase):
    def test_mount_with_empty_relative(self) -> None:
        self.assertEqual(
            _folder_local_root("/Volumes/arqbackup1:/"),
            "/Volumes/arqbackup1",
        )

    def test_root_mount_with_relative(self) -> None:
        self.assertEqual(
            _folder_local_root("/:Users/me/dest"), "/Users/me/dest",
        )

    def test_no_colon_passthrough(self) -> None:
        self.assertEqual(_folder_local_root("/Volumes/x"), "/Volumes/x")

    def test_empty(self) -> None:
        self.assertEqual(_folder_local_root(""), "")


class _FixtureMixin(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "server.db"
        _build_fixture_db(self.db)
        self.src = ArqAppSource(self.db)

    def tearDown(self) -> None:
        self._tmp.cleanup()


class DetectTests(_FixtureMixin):
    def test_detect_with_fixture(self) -> None:
        src = detect_arq_app(server_db=self.db, require_app=False)
        self.assertIsNotNone(src)

    def test_detect_missing_db_returns_none(self) -> None:
        missing = self.db.parent / "nope.db"
        self.assertIsNone(
            detect_arq_app(server_db=missing, require_app=False),
        )

    def test_detect_requires_app_by_default(self) -> None:
        # require_app=True + a bogus app path → None even if DB exists.
        self.assertIsNone(detect_arq_app(
            server_db=self.db,
            app_path=self.db.parent / "NoSuch.app",
            require_app=True,
        ))


class StorageLocationTests(_FixtureMixin):
    def test_lists_all_including_cloud(self) -> None:
        sls = self.src.storage_locations()
        kinds = {sl.provider_type for sl in sls}
        self.assertEqual(kinds, {"folder", "sftp", "arqpremium"})

    def test_active_only_filter(self) -> None:
        active = self.src.storage_locations(active_only=True)
        self.assertEqual({sl.provider_type for sl in active},
                         {"folder", "sftp"})

    def test_folder_to_destination(self) -> None:
        sl = next(s for s in self.src.storage_locations()
                  if s.provider_type == "folder")
        d = sl.to_destination()
        self.assertIsNotNone(d)
        self.assertEqual(d.kind, "local")
        self.assertEqual(d.path, "/Volumes/arqbackup1")
        self.assertEqual(d.origin, "arq")

    def test_sftp_to_destination(self) -> None:
        sl = next(s for s in self.src.storage_locations()
                  if s.provider_type == "sftp")
        d = sl.to_destination()
        self.assertEqual(d.kind, "sftp")
        self.assertEqual((d.host, d.port, d.user, d.path),
                         ("u1.example.de", 23, "u1", "/home"))
        self.assertEqual(d.origin, "arq")

    def test_cloud_not_openable(self) -> None:
        sl = next(s for s in self.src.storage_locations()
                  if s.provider_type == "arqpremium")
        self.assertFalse(sl.is_openable)
        self.assertIsNone(sl.to_destination())


class PlanTests(_FixtureMixin):
    def _plan(self, uuid: str):
        return next(p for p in self.src.plans() if p.plan_uuid == uuid)

    def test_sorted_by_name(self) -> None:
        names = [p.name for p in self.src.plans()]
        self.assertEqual(names, sorted(names, key=str.lower))

    def test_buzhash_plan_maps_to_arq_v7_41(self) -> None:
        pl = self._plan("PLAN-BUZHASH").to_plan()
        self.assertEqual(pl.chunker, "arq_v7_41")
        self.assertEqual(pl.origin, "arq")
        self.assertEqual(pl.plan_id, "PLAN-BUZHASH")

    def test_fixed_plan_maps_to_fixed_40m(self) -> None:
        pl = self._plan("PLAN-FIXED").to_plan()
        self.assertEqual(pl.chunker, "fixed-40m")

    def test_sources_from_folder_plans(self) -> None:
        pl = self._plan("PLAN-BUZHASH").to_plan()
        # sorted by folder name: code, docs
        self.assertEqual(pl.sources,
                         ["/Users/me/code", "/Users/me/docs"])

    def test_excludes_aggregated(self) -> None:
        pl = self._plan("PLAN-BUZHASH").to_plan()
        self.assertIn("*.log", pl.exclude_globs)
        self.assertIn("*.tmp", pl.exclude_globs)
        self.assertIn("^build/", pl.exclude_regexes)
        self.assertTrue(pl.skip_tm_excludes)  # one folder sets it

    def test_apfs_snapshot_flag(self) -> None:
        self.assertTrue(self._plan("PLAN-BUZHASH").to_plan().use_apfs_snapshot)
        self.assertFalse(self._plan("PLAN-FIXED").to_plan().use_apfs_snapshot)

    def test_retention_mapping(self) -> None:
        pl = self._plan("PLAN-BUZHASH").to_plan()
        self.assertEqual(pl.retention, {
            "keep_hourly": 24, "keep_daily": 30, "keep_monthly": 12,
        })  # retainWeeks=0 dropped

    def test_retain_all_means_empty_policy(self) -> None:
        self.assertEqual(self._plan("PLAN-RETAINALL").to_plan().retention, {})

    def test_sftp_plan_destination_wiring(self) -> None:
        pl = self._plan("PLAN-FIXED").to_plan()
        self.assertEqual(pl.destination_kind, "sftp")
        self.assertEqual(pl.destination["host"], "u1.example.de")
        self.assertEqual(pl.destination["port"], 23)

    def test_folder_plan_destination_wiring(self) -> None:
        pl = self._plan("PLAN-BUZHASH").to_plan()
        self.assertEqual(pl.destination_kind, "local")
        self.assertEqual(pl.destination["path"], "/Volumes/arqbackup1")

    def test_last_run_iso_when_present(self) -> None:
        self.assertTrue(self._plan("PLAN-BUZHASH").to_plan().last_run_iso)
        self.assertEqual(self._plan("PLAN-FIXED").to_plan().last_run_iso, "")


class ActivityTests(_FixtureMixin):
    def test_most_recent_first(self) -> None:
        acts = self.src.activities()
        times = [a.created_time for a in acts]
        self.assertEqual(times, sorted(times, reverse=True))

    def test_running_only_filter(self) -> None:
        running = self.src.activities(running_only=True)
        self.assertEqual([a.uuid for a in running], ["A-RUN"])

    def test_plan_uuid_filter(self) -> None:
        acts = self.src.activities(plan_uuid="PLAN-FIXED")
        self.assertEqual([a.uuid for a in acts], ["A-ABORT"])

    def test_limit(self) -> None:
        self.assertEqual(len(self.src.activities(limit=2)), 2)

    def test_snapshot_fallback_reads_and_cleans_up(self) -> None:
        # The snapshot path (used when the live DB is write-locked)
        # must read identically and remove its temp copy on close.
        from pathlib import Path as _P
        con = self.src._connect_snapshot()
        tmp = getattr(con, "_arq_tui_tmp_dir", None)
        self.assertTrue(tmp and _P(tmp).is_dir())
        n = con.execute("SELECT count(*) FROM activities").fetchone()[0]
        self.assertEqual(n, 4)
        self.src._close(con)
        self.assertFalse(_P(tmp).exists())  # temp copy cleaned

    def test_is_running(self) -> None:
        by_uuid = {a.uuid: a for a in self.src.activities()}
        self.assertTrue(by_uuid["A-RUN"].is_running)
        self.assertFalse(by_uuid["A-DONE"].is_running)
        self.assertFalse(by_uuid["A-ABORT"].is_running)  # aborted

    def test_aborted_fields(self) -> None:
        by_uuid = {a.uuid: a for a in self.src.activities()}
        self.assertTrue(by_uuid["A-ABORT"].aborted)
        self.assertEqual(by_uuid["A-ABORT"].abort_reason, "user cancelled")
        self.assertEqual(by_uuid["A-ABORT"].error_count, 2)

    def test_progress_fraction(self) -> None:
        by_uuid = {a.uuid: a for a in self.src.activities()}
        self.assertAlmostEqual(by_uuid["A-RUN"].progress_fraction(), 0.5)
        self.assertIsNone(by_uuid["A-ABORT"].progress_fraction())  # no total

    def test_validate_kind_label(self) -> None:
        by_uuid = {a.uuid: a for a in self.src.activities()}
        self.assertEqual(by_uuid["A-VALIDATE"].kind_label, "validate")
        self.assertEqual(by_uuid["A-RUN"].kind_label, "backup")


class GracefulDegradationTests(unittest.TestCase):
    def test_missing_json_column_yields_empty(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "server.db"
            con = sqlite3.connect(str(db))
            # storage_locations without a json column (very old Arq).
            con.execute(
                "CREATE TABLE storage_locations(id INTEGER, active INTEGER)")
            con.execute(
                "CREATE TABLE backup_plans(plan_uuid TEXT, "
                "storage_location_id INTEGER, active INTEGER, "
                "last_backed_up REAL)")
            con.commit()
            con.close()
            src = ArqAppSource(db)
            self.assertEqual(src.storage_locations(), [])
            self.assertEqual(src.plans(), [])

    def test_malformed_json_row_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "server.db"
            con = sqlite3.connect(str(db))
            con.execute(
                "CREATE TABLE storage_locations(id INTEGER, "
                "active INTEGER, json TEXT)")
            con.execute(
                "INSERT INTO storage_locations VALUES (1,1,'{not json')")
            con.execute(
                "INSERT INTO storage_locations VALUES (2,1,?)",
                (json.dumps({"id": 2, "uuid": "U", "name": "ok",
                             "providerType": "folder",
                             "path": "/x:/"}),))
            con.commit()
            con.close()
            sls = ArqAppSource(db).storage_locations()
            self.assertEqual([sl.sl_id for sl in sls], [2])


@unittest.skipUnless(
    DEFAULT_SERVER_DB.is_file() and os.access(DEFAULT_SERVER_DB, os.R_OK),
    "real Arq ArqAgent server.db not present/readable",
)
class RealServerDbSmokeTests(unittest.TestCase):
    """Runs only on the operator's machine with Arq installed. Confirms
    the adapter reads the live DB without error and produces coherent
    objects — never asserts on specific operator data."""

    def setUp(self) -> None:
        self.src = detect_arq_app()
        self.assertIsNotNone(self.src, "Arq.app present but detect failed")

    def test_storage_locations_decode(self) -> None:
        for sl in self.src.storage_locations():
            self.assertIsInstance(sl, ArqStorageLocation)
            self.assertTrue(sl.provider_type)

    def test_plans_resolve_and_map(self) -> None:
        for p in self.src.plans():
            pl = p.to_plan()
            self.assertEqual(pl.origin, "arq")
            self.assertIn(pl.chunker, ("arq_v7_41", "fixed-40m"))
            # planUUID drives plan_id (folder-name == planUUID layout).
            self.assertEqual(pl.plan_id, p.plan_uuid)

    def test_activities_read(self) -> None:
        acts = self.src.activities(limit=20)
        for a in acts:
            self.assertIsInstance(a, ArqActivity)


if __name__ == "__main__":
    unittest.main()
