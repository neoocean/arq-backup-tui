"""Tests for ``arq_writer.json_configs`` template emit.

These pin the JSON sidecar shape against the schema diff observed
on the operator's real Arq.app v8 destination
(``docs/COMPAT-VERIFICATION.md`` §2.7.1). Each missing or
divergent key surfaced there gets a focused test here so we don't
silently regress.
"""

from __future__ import annotations

import unittest

from arq_writer.json_configs import (
    build_backupfolders_json,
    build_backupplan,
    build_folder_plan,
)


class BackupFoldersIndexTests(unittest.TestCase):
    """``backupfolders.json`` keys must match the Arq.app v8 set."""

    EXPECTED_KEYS = frozenset({
        "standardObjectDirs",
        "standardIAObjectDirs",
        "onezoneIAObjectDirs",
        "s3GlacierObjectDirs",
        "s3GlacierIRObjectDirs",
        "s3DeepArchiveObjectDirs",
    })

    def test_emits_all_six_storage_class_slots(self) -> None:
        out = build_backupfolders_json("CU-1234")
        self.assertEqual(set(out.keys()), self.EXPECTED_KEYS)

    def test_includes_s3GlacierIRObjectDirs(self) -> None:
        # Regression for the 2026-05-10 schema diff: this key was
        # missing from our writer's output but present (as []) in
        # Arq.app v8's emit. Pin it as an empty list — same shape
        # Arq.app uses for unused storage-class slots.
        out = build_backupfolders_json("CU-1234")
        self.assertIn("s3GlacierIRObjectDirs", out)
        self.assertEqual(out["s3GlacierIRObjectDirs"], [])

    def test_storage_class_slots_are_lists(self) -> None:
        out = build_backupfolders_json("CU-1234")
        for k in self.EXPECTED_KEYS:
            self.assertIsInstance(
                out[k], list, msg=f"{k} should be a list",
            )

    def test_standard_dir_threads_through_computer_uuid(self) -> None:
        # Sanity: the only slot writes use is standardObjectDirs.
        # Make sure the UUID is wired in and the others stay empty.
        out = build_backupfolders_json("CU-WIRED-IN")
        self.assertEqual(
            out["standardObjectDirs"],
            ["/CU-WIRED-IN/standardobjects"],
        )
        for k in self.EXPECTED_KEYS - {"standardObjectDirs"}:
            self.assertEqual(out[k], [], msg=k)


class FolderPlanArqAppV8KeysTests(unittest.TestCase):
    """``build_folder_plan`` emits every key Arq.app v8 emits in
    ``backupFolderPlansByUUID``'s value dict.

    Sampled 2026-05-10 against ``/Volumes/arqbackup1`` (HANDOFF.md
    GAP-A): real folder plans carry a 16-key set; pre-fix our
    writer's emit was 15. The missing key — ``skipTMExcludes`` —
    is the macOS Time Machine exclude-xattr override toggle.
    """

    EXPECTED_KEYS = frozenset({
        "allDrives",
        "backupFolderUUID",
        "blobStorageClass",
        "diskIdentifier",
        "excludedDrives",
        "ignoredRelativePaths",
        "localMountPoint",
        "localPath",
        "name",
        "regexExcludes",
        "relativePath",
        "skipDuringBackup",
        "skipIfNotMounted",
        "skipTMExcludes",
        "useDiskIdentifier",
        "wildcardExcludes",
    })

    def _build(self) -> dict:
        return build_folder_plan(
            folder_uuid="FOLDER-UUID",
            local_path="/some/path",
            name="some-folder",
        )

    def test_emits_all_sixteen_arq_app_v8_keys(self) -> None:
        plan = self._build()
        self.assertEqual(set(plan.keys()), self.EXPECTED_KEYS)

    def test_includes_skipTMExcludes(self) -> None:
        # Regression for the 2026-05-10 schema diff: Arq.app v8
        # emits this as ``False`` by default (obey TM excludes).
        plan = self._build()
        self.assertIn("skipTMExcludes", plan)
        self.assertEqual(plan["skipTMExcludes"], False)
        self.assertIs(type(plan["skipTMExcludes"]), bool)


class BackupPlanArqAppV8KeysTests(unittest.TestCase):
    """``build_backupplan`` emits every key Arq.app v8 emits.

    Pre-fix the writer's plan was missing these 10 keys (sampled
    2026-05-10 against the operator's destination — see
    ``docs/COMPAT-VERIFICATION.md`` §2.7.1). Defaults below match
    Arq.app v8's freshly-provisioned-plan emit so a structural
    schema diff against a real destination shows zero
    ``a_only`` / ``b_only`` keys.
    """

    EXPECTED_DEFAULTS = {
        "backupFolderPlanMountPointsAreInitialized": True,
        "backupSetIsInitialized": True,
        "budgetGB": 0,
        "createdAtProConsole": False,
        "datalessFilesOption": 1,
        "managed": False,
        "objectLockAvailable": False,
        "objectLockUpdateIntervalDays": 30,
        "preventBackupOnConstrainedNetworks": False,
        "preventBackupOnExpensiveNetworks": False,
    }

    def _build(self) -> dict:
        fp = build_folder_plan(
            folder_uuid="FOLDER-UUID",
            local_path="/some/path",
            name="some-folder",
        )
        return build_backupplan(
            plan_uuid="PLAN-UUID",
            plan_name="some-plan",
            folder_plans=[fp],
        )

    def test_all_ten_arq_app_keys_present(self) -> None:
        plan = self._build()
        for key in self.EXPECTED_DEFAULTS:
            self.assertIn(
                key, plan, msg=f"missing key {key!r}",
            )

    def test_default_values_match_arq_app_v8(self) -> None:
        # If Arq.app drifts the default for one of these keys in a
        # future version, the schema diff will catch it long before
        # this test fails — but pinning the value matters because
        # an int vs bool mismatch (e.g. budgetGB=False) would also
        # surface as a type-level diff against a real destination.
        plan = self._build()
        for key, expected in self.EXPECTED_DEFAULTS.items():
            self.assertEqual(
                plan[key], expected,
                msg=f"{key} default drifted",
            )

    def test_default_value_types_match(self) -> None:
        plan = self._build()
        for key, expected in self.EXPECTED_DEFAULTS.items():
            self.assertIs(
                type(plan[key]), type(expected),
                msg=(
                    f"{key} type drift: "
                    f"got {type(plan[key]).__name__}, "
                    f"expected {type(expected).__name__}"
                ),
            )

    def test_plan_keeps_pre_existing_keys(self) -> None:
        # Sanity that the new keys haven't displaced any structural
        # keys the L4 audit already requires.
        plan = self._build()
        for k in (
            "active", "isEncrypted", "name", "planUUID",
            "scheduleJSON", "transferRateJSON", "emailReportJSON",
            "version", "useBuzhash",
        ):
            self.assertIn(k, plan)


if __name__ == "__main__":
    unittest.main()
