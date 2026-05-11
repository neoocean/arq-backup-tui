"""Builders for the four JSON config files at the root of a backup set.

These match the literal sample shapes shown in the Arq 7 spec, with
operator-supplied identifiers (computer name, plan UUID, folder UUIDs)
substituted in. Fields the spec marks as ``unused`` are still emitted
because Arq.app + ``arq_restore`` parse them by name and surprise
side effects from missing keys are not worth the byte savings.
"""

from __future__ import annotations

from typing import Iterable, List

from .constants import (
    BLOB_ID_SHA256,
    DEFAULT_CHUNKER_VERSION,
    DEFAULT_MAX_PACKED_ITEM_LENGTH,
)


def build_backupconfig(
    *,
    backup_name: str,
    computer_name: str,
    is_encrypted: bool = True,
    blob_id_type: int = BLOB_ID_SHA256,
    max_packed_item_length: int = DEFAULT_MAX_PACKED_ITEM_LENGTH,
    chunker_version: int = DEFAULT_CHUNKER_VERSION,
) -> dict:
    """Build ``backupconfig.json``.

    ``chunker_version`` is recorded so existing readers see the
    expected schema, even though this writer doesn't actually run the
    chunker (every file becomes a single blob).
    """
    return {
        "blobIdentifierType": blob_id_type,
        "maxPackedItemLength": max_packed_item_length,
        "backupName": backup_name,
        "isWORM": False,
        "containsGlacierArchives": False,
        "additionalUnpackedBlobDirs": [],
        "chunkerVersion": chunker_version,
        "computerName": computer_name,
        "computerSerial": "unused",
        "blobStorageClass": "STANDARD",
        "isEncrypted": is_encrypted,
    }


def build_backupfolders_json(computer_uuid: str) -> dict:
    """Build the top-level ``backupfolders.json``.

    Only the ``standardObjectDirs`` field is populated since the v0
    writer stores everything under ``standardobjects/``. The other
    storage-class fields stay empty — matches what ``arq_restore`` /
    Arq.app expect for a non-S3 destination.
    """
    return {
        "standardObjectDirs": [
            f"/{computer_uuid}/standardobjects",
        ],
        "standardIAObjectDirs": [],
        "onezoneIAObjectDirs": [],
        "s3GlacierObjectDirs": [],
        # Glacier Instant Retrieval — Arq.app v8 always emits this
        # key alongside the other s3*ObjectDirs slots even when the
        # destination uses no S3 storage class. Omitting it surfaces
        # in the schema diff against real Arq.app destinations
        # (docs/COMPAT-VERIFICATION.md §2.7.1).
        "s3GlacierIRObjectDirs": [],
        "s3DeepArchiveObjectDirs": [],
    }


def build_backupfolder_json(
    *,
    folder_uuid: str,
    name: str,
    local_path: str,
    local_mount_point: str = "/",
    storage_class: str = "STANDARD",
    disk_identifier: str = "ROOT",
) -> dict:
    """Build per-folder ``backupfolder.json``."""
    return {
        "localPath": local_path,
        "migratedFromArq60": False,
        "storageClass": storage_class,
        "diskIdentifier": disk_identifier,
        "uuid": folder_uuid,
        "migratedFromArq5": False,
        "localMountPoint": local_mount_point,
        "name": name,
    }


def build_folder_plan(
    *,
    folder_uuid: str,
    local_path: str,
    name: str,
    local_mount_point: str = "/",
    relative_path: str = "/",
    disk_identifier: str = "ROOT",
) -> dict:
    """Per-folder entry inside ``backupPlanJSON.backupFolderPlansByUUID``."""
    return {
        "allDrives": False,
        "backupFolderUUID": folder_uuid,
        "blobStorageClass": "STANDARD",
        "diskIdentifier": disk_identifier,
        "excludedDrives": [],
        "ignoredRelativePaths": [],
        "localMountPoint": local_mount_point,
        "localPath": local_path,
        "name": name,
        "regexExcludes": [],
        "relativePath": relative_path,
        "skipDuringBackup": False,
        "skipIfNotMounted": False,
        # Time Machine excludes — Arq.app v8 honours
        # ``com.apple.metadata:com_apple_backup_excludeItem`` xattr
        # by default; set ``skipTMExcludes=True`` to override and
        # back up TM-excluded paths anyway. Default mirrors Arq.app
        # v8 (False = obey TM excludes). Sampled 2026-05-10 against
        # ``/Volumes/arqbackup1`` (HANDOFF.md GAP-A).
        "skipTMExcludes": False,
        "useDiskIdentifier": False,
        "wildcardExcludes": [],
    }


def build_backupplan(
    *,
    plan_uuid: str,
    plan_name: str,
    folder_plans: Iterable[dict],
    is_encrypted: bool = True,
    update_time: float = 0.0,
    creation_time: float = 0.0,
    storage_location_id: int = 1,
) -> dict:
    """Build ``backupplan.json``.

    Mirrors the ``backupPlanJSON`` example in the spec — all keys
    Arq.app emits are present, with reasonable defaults for an
    operator-driven manual backup (every-day schedule, no email
    reports, no transfer-rate cap).
    """
    folder_plans_dict = {
        fp["backupFolderUUID"]: fp for fp in folder_plans
    }
    return {
        "active": True,
        "arq5UseS3IA": False,
        # Defaults below mirror what Arq.app v8 emits on a freshly
        # provisioned plan (sampled 2026-05-10 against the operator's
        # destination — see docs/COMPAT-VERIFICATION.md §2.7.1).
        # `backupFolderPlanMountPointsAreInitialized` /
        # `backupSetIsInitialized` are True because we *do* finalize
        # the folder + set during build_backup before emitting plan.
        "backupFolderPlanMountPointsAreInitialized": True,
        "backupFolderPlansByUUID": folder_plans_dict,
        "backupSetIsInitialized": True,
        # `budgetGB`: 0 = unbounded; operators with a managed quota
        # set this externally via Arq.app GUI.
        "budgetGB": 0,
        "cpuUsage": 25,
        # Pro Console is Arq.app's enterprise management feature;
        # standalone backups never originate there.
        "createdAtProConsole": False,
        # Arq.app v8 emits ``creationTime`` as an integer Unix
        # epoch (seconds), NOT a stringified decimal. Verified
        # 2026-05-11 against the operator's real
        # ``/Volumes/arqbackup1`` destination — D1 type-level
        # check found ``real=int, ours=str`` here. The fingerprint
        # module's value-level diff would have flagged this on
        # any future comparison; emit as int to match.
        "creationTime": int(creation_time) if creation_time else 0,
        # `datalessFilesOption`: 1 = "materialize then back up" (the
        # safe default Arq.app v8 ships with for iCloud / Dropbox
        # placeholders).
        "datalessFilesOption": 1,
        "emailReportJSON": {
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
        },
        "excludedNetworkInterfaces": [],
        "excludedWiFiNetworkNames": [],
        "id": 1,
        "includeFileListInActivityLog": False,
        "includeNetworkInterfaces": False,
        "includeNewVolumes": False,
        "includeWiFiNetworks": False,
        "isEncrypted": is_encrypted,
        "keepDeletedFiles": False,
        # `managed`: True iff the plan is centrally administered via
        # Arq.app's Pro Console; False for ordinary user-driven plans.
        "managed": False,
        "name": plan_name,
        "needsArq5Buckets": False,
        "noBackupsAlertDays": 5,
        "notifyOnError": True,
        "notifyOnSuccess": False,
        # S3 Object Lock is unrelated to local destinations but Arq.app
        # always emits the slot; default it off + use Arq.app's 30-day
        # update interval so the schema agrees.
        "objectLockAvailable": False,
        "objectLockUpdateIntervalDays": 30,
        "pauseOnBattery": False,
        "planUUID": plan_uuid,
        # `preventBackupOnConstrainedNetworks` /
        # `preventBackupOnExpensiveNetworks`: macOS network-quality
        # gates Arq.app respects; default off so the writer doesn't
        # silently inherit a stricter policy than the user requested.
        "preventBackupOnConstrainedNetworks": False,
        "preventBackupOnExpensiveNetworks": False,
        "preventSleep": False,
        "retainAll": True,
        "retainDays": 30,
        "retainHours": 24,
        "retainMonths": 60,
        "retainWeeks": 52,
        "scheduleJSON": {
            "daysOfWeek": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
            "everyHours": 1,
            "minutesAfterHour": 0,
            "pauseDuringWindow": False,
            "pauseFrom": "09:00",
            "pauseTo": "17:00",
            "startWhenVolumeIsConnected": False,
            "type": "Hourly",
        },
        "storageLocationId": storage_location_id,
        "threadCount": 2,
        "transferRateJSON": {
            "daysOfWeek": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
            "enabled": False,
            "endTimeOfDay": "18:00",
            "maxKBPS": 100,
            "scheduleType": "Scheduled",
            "startTimeOfDay": "18:00",
        },
        # Same int-vs-str fix as ``creationTime`` above. Arq.app
        # v8 emits ``updateTime`` as an integer Unix epoch.
        "updateTime": int(update_time) if update_time else 0,
        "useAPFSSnapshots": True,
        "useBuzhash": False,
        "version": 2,
        "wakeForBackup": False,
    }
