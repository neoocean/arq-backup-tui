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
        "backupFolderPlansByUUID": folder_plans_dict,
        "cpuUsage": 25,
        "creationTime": f"{creation_time:.3f}" if creation_time else "0",
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
        "name": plan_name,
        "needsArq5Buckets": False,
        "noBackupsAlertDays": 5,
        "notifyOnError": True,
        "notifyOnSuccess": False,
        "pauseOnBattery": False,
        "planUUID": plan_uuid,
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
        "updateTime": f"{update_time:.3f}" if update_time else "0",
        "useAPFSSnapshots": True,
        "useBuzhash": False,
        "version": 2,
        "wakeForBackup": False,
    }
