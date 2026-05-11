"""Builders for the four JSON config files at the root of a backup set.

These match the literal sample shapes shown in the Arq 7 spec, with
operator-supplied identifiers (computer name, plan UUID, folder UUIDs)
substituted in. Fields the spec marks as ``unused`` are still emitted
because Arq.app + ``arq_restore`` parse them by name and surprise
side effects from missing keys are not worth the byte savings.
"""

from __future__ import annotations

from typing import Iterable, List, Optional

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

    Arq.app v8 emits each storage-class ObjectDir field as a
    single-element list of the corresponding directory path
    under ``/<computer_uuid>/...``, regardless of whether any
    actual objects use that storage class. Sampled 2026-05-11
    against ``/Volumes/arqbackup1`` (D4 investigation): all 6
    fields carry placeholder paths even though only the
    ``standardobjects`` directory actually exists on disk.

    Path naming convention (verified against operator's real
    Arq.app v8 destination):

    | Field | Path suffix |
    |---|---|
    | ``standardObjectDirs`` | ``standardobjects`` |
    | ``standardIAObjectDirs`` | ``standardiaobjects`` |
    | ``onezoneIAObjectDirs`` | ``onezoneiaobjects`` |
    | ``s3GlacierObjectDirs`` | ``s3glacierobjects`` |
    | ``s3GlacierIRObjectDirs`` | ``s3glacierirobjects`` |
    | ``s3DeepArchiveObjectDirs`` | ``s3deeparchiveobjects`` |

    The arq_restore reader looks up blobs by walking each
    listed path; non-existent placeholder paths simply yield
    zero objects.
    """
    return {
        "standardObjectDirs": [
            f"/{computer_uuid}/standardobjects",
        ],
        "standardIAObjectDirs": [
            f"/{computer_uuid}/standardiaobjects",
        ],
        "onezoneIAObjectDirs": [
            f"/{computer_uuid}/onezoneiaobjects",
        ],
        "s3GlacierObjectDirs": [
            f"/{computer_uuid}/s3glacierobjects",
        ],
        # Glacier Instant Retrieval — Arq.app v8 always emits
        # this key alongside the other s3*ObjectDirs slots even
        # when the destination uses no S3 storage class. Empty
        # would surface in the schema diff against real Arq.app
        # destinations (docs/COMPAT-VERIFICATION.md §2.7.1 +
        # D4 investigation).
        "s3GlacierIRObjectDirs": [
            f"/{computer_uuid}/s3glacierirobjects",
        ],
        "s3DeepArchiveObjectDirs": [
            f"/{computer_uuid}/s3deeparchiveobjects",
        ],
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


def build_schedule_json(
    *,
    schedule_type: str = "Daily",
    days_of_week: Optional[list] = None,
    time_of_day: str = "12:00",
    every_hours: int = 1,
    minutes_after_hour: int = 0,
    back_up_and_validate: bool = True,
    pause_during_window: bool = False,
    pause_from: str = "09:00",
    pause_to: str = "17:00",
    start_when_volume_is_connected: bool = False,
) -> dict:
    """Build a polymorphic ``scheduleJSON`` dict.

    Arq.app v8 emits ``scheduleJSON`` with a SHAPE that varies by
    ``type``:

    - ``type='Daily'`` (6 keys): ``backUpAndValidate``,
      ``daysOfWeek``, ``pauseDuringWindow``,
      ``startWhenVolumeIsConnected``, ``timeOfDay``, ``type``.
      What real Arq.app v8 emits on a freshly-provisioned plan
      (re-sampled 2026-05-11 against ``/Volumes/arqbackup1``).
    - ``type='Hourly'`` (8 keys): ``daysOfWeek``, ``everyHours``,
      ``minutesAfterHour``, ``pauseDuringWindow``, ``pauseFrom``,
      ``pauseTo``, ``startWhenVolumeIsConnected``, ``type``.
      Arq.app's higher-frequency alternative — emit when the
      operator wants sub-daily backup intervals.

    The default (``Daily``) matches real Arq.app v8's own emit
    so a destination produced with no schedule override has the
    same shape Arq.app would produce.
    """
    if days_of_week is None:
        days_of_week = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    if schedule_type == "Daily":
        return {
            "backUpAndValidate": bool(back_up_and_validate),
            "daysOfWeek": list(days_of_week),
            "pauseDuringWindow": bool(pause_during_window),
            "startWhenVolumeIsConnected": bool(
                start_when_volume_is_connected
            ),
            "timeOfDay": time_of_day,
            "type": "Daily",
        }
    if schedule_type == "Hourly":
        return {
            "daysOfWeek": list(days_of_week),
            "everyHours": int(every_hours),
            "minutesAfterHour": int(minutes_after_hour),
            "pauseDuringWindow": bool(pause_during_window),
            "pauseFrom": pause_from,
            "pauseTo": pause_to,
            "startWhenVolumeIsConnected": bool(
                start_when_volume_is_connected
            ),
            "type": "Hourly",
        }
    raise ValueError(
        f"unknown scheduleJSON type {schedule_type!r}; "
        f"expected 'Daily' or 'Hourly'"
    )


def build_transfer_rate_json(
    *,
    enabled: bool = False,
    schedule_type: str = "Always",
    days_of_week: Optional[list] = None,
    start_time_of_day: str = "08:00",
    end_time_of_day: str = "17:00",
    max_kbps: int = 100,
) -> dict:
    """Build a polymorphic ``transferRateJSON`` dict.

    Arq.app v8 emits with a shape that depends on
    ``scheduleType``:

    - ``scheduleType='Always'`` (5 keys, no ``maxKBPS``): real
      Arq.app v8 default for the always-on, unthrottled case.
    - ``scheduleType='Scheduled'`` (6 keys including
      ``maxKBPS``): rate-capped, time-windowed throttling.

    Default matches real Arq.app v8's emit (Always).
    """
    if days_of_week is None:
        days_of_week = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    if schedule_type == "Always":
        return {
            "daysOfWeek": list(days_of_week),
            "enabled": bool(enabled),
            "endTimeOfDay": end_time_of_day,
            "scheduleType": "Always",
            "startTimeOfDay": start_time_of_day,
        }
    if schedule_type == "Scheduled":
        return {
            "daysOfWeek": list(days_of_week),
            "enabled": bool(enabled),
            "endTimeOfDay": end_time_of_day,
            "maxKBPS": int(max_kbps),
            "scheduleType": "Scheduled",
            "startTimeOfDay": start_time_of_day,
        }
    raise ValueError(
        f"unknown transferRateJSON scheduleType {schedule_type!r}; "
        f"expected 'Always' or 'Scheduled'"
    )


def build_backupplan(
    *,
    plan_uuid: str,
    plan_name: str,
    folder_plans: Iterable[dict],
    is_encrypted: bool = True,
    update_time: float = 0.0,
    creation_time: float = 0.0,
    storage_location_id: int = 1,
    schedule_json: Optional[dict] = None,
    transfer_rate_json: Optional[dict] = None,
) -> dict:
    """Build ``backupplan.json``.

    Mirrors the ``backupPlanJSON`` example in the spec — all keys
    Arq.app emits are present, with reasonable defaults for an
    operator-driven manual backup (every-day schedule, no email
    reports, no transfer-rate cap).

    ``schedule_json`` / ``transfer_rate_json`` opt-ins let the
    operator specify a custom shape (e.g. Hourly schedule or
    Scheduled transfer rate). When omitted, the defaults are
    the Daily/Always shapes Arq.app v8 emits on a fresh plan
    (P2 re-sampling 2026-05-11). Use ``build_schedule_json`` /
    ``build_transfer_rate_json`` to construct alternate shapes.
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
        # scheduleJSON / transferRateJSON are polymorphic by
        # type / scheduleType discriminator (P2 finding,
        # 2026-05-11). Default to the Daily / Always shapes
        # Arq.app v8 emits on a fresh plan; operators wanting
        # Hourly / Scheduled shapes can pass an explicit dict
        # built with ``build_schedule_json`` /
        # ``build_transfer_rate_json``.
        "scheduleJSON": (
            schedule_json
            if schedule_json is not None
            else build_schedule_json()
        ),
        "storageLocationId": storage_location_id,
        "threadCount": 2,
        "transferRateJSON": (
            transfer_rate_json
            if transfer_rate_json is not None
            else build_transfer_rate_json()
        ),
        # Same int-vs-str fix as ``creationTime`` above. Arq.app
        # v8 emits ``updateTime`` as an integer Unix epoch.
        "updateTime": int(update_time) if update_time else 0,
        "useAPFSSnapshots": True,
        "useBuzhash": False,
        "version": 2,
        "wakeForBackup": False,
    }
