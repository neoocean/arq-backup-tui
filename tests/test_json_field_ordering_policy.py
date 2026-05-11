"""A11 — JSON sidecar field ordering policy.

Arq.app v8 emits JSON sidecars (``backupconfig.json``,
``backupplan.json``, ``backupfolder.json``, ``backupfolders.json``,
plus BackupRecord JSON for Tree v4 records) with key ordering
that reflects Objective-C ``NSDictionary``'s internal hash-bucket
layout. From a Python perspective the ordering is "random" — same
keys, different sequence each time NSDictionary's hash table
reshuffles.

Sampled 2026-05-11 against ``/Volumes/arqbackup1`` (operator's
real Arq.app v8 destination):

    real backupplan.json key order:
      transferRateJSON, cpuUsage, id, storageLocationId,
      budgetGB, excludedNetworkInterfaces, needsArq5Buckets,
      useBuzhash, arq5UseS3IA, objectLockUpdateIntervalDays,
      planUUID, scheduleJSON, …

There is **no portable way to reproduce this ordering** from a
Python writer — NSDictionary's hash bucket positions depend on
the Objective-C runtime + the specific NSString hash function +
the table's internal load factor at creation time.

## Practical consequences

| Property | Arq.app v8 vs our writer |
|---|---|
| Schema (keys + types) | ✅ 100% match |
| Values (per field) | ✅ Match where operator config matches |
| JSON byte ordering | ❌ Different (NSDictionary vs Python dict) |
| Functional restoration | ✅ JSON spec defines dicts as unordered |
| Schema fingerprint diff | ✅ Zero diffs (validator normalises) |

The byte-level diff is a **documented** compat gap that does not
affect any consumer of the JSON sidecar — every Arq 7 reader
(including our own) parses JSON into a dict that's order-
agnostic. Operators who want byte-level diff against Arq.app
must canonicalise the JSON (sorted keys + same separators)
before comparing.

This module pins our writer's ordering convention (Python dict
insertion order, which equals the order in
``arq_writer/json_configs.py::build_backupplan``) so a future
refactor that reorders the source dict catches a deliberate
break.
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


# The order our writer emits keys, sampled from
# ``arq_writer/json_configs.py::build_backupplan``.
EXPECTED_BACKUPPLAN_KEY_ORDER = [
    "active",
    "arq5UseS3IA",
    "backupFolderPlanMountPointsAreInitialized",
    "backupFolderPlansByUUID",
    "backupSetIsInitialized",
    "budgetGB",
    "cpuUsage",
    "createdAtProConsole",
    "creationTime",
    "datalessFilesOption",
    "emailReportJSON",
    "excludedNetworkInterfaces",
    "excludedWiFiNetworkNames",
    "id",
    "includeFileListInActivityLog",
    "includeNetworkInterfaces",
    "includeNewVolumes",
    "includeWiFiNetworks",
    "isEncrypted",
    "keepDeletedFiles",
    "managed",
    "name",
    "needsArq5Buckets",
    "noBackupsAlertDays",
    "notifyOnError",
    "notifyOnSuccess",
    "objectLockAvailable",
    "objectLockUpdateIntervalDays",
    "pauseOnBattery",
    "planUUID",
    "preventBackupOnConstrainedNetworks",
    "preventBackupOnExpensiveNetworks",
    "preventSleep",
    "retainAll",
    "retainDays",
    "retainHours",
    "retainMonths",
    "retainWeeks",
    "scheduleJSON",
    "storageLocationId",
    "threadCount",
    "transferRateJSON",
    "updateTime",
    "useAPFSSnapshots",
    "useBuzhash",
    "version",
    "wakeForBackup",
]


@unittest.skipUnless(_has_openssl(), "openssl CLI required")
class WriterEmitsStableKeyOrderTests(unittest.TestCase):
    """Our writer's JSON emit order is determined by the dict
    literal in ``arq_writer.json_configs.build_backupplan``.
    Python preserves dict insertion order (3.7+), so once the
    dict literal is fixed, the emit order is stable across runs.
    """

    def test_backupplan_keys_emit_in_expected_order(self) -> None:
        """The on-disk JSON key order matches the source dict
        literal's order. If a future refactor reorders the dict
        in ``build_backupplan``, this test flags the change."""
        from arq_writer.backup import build_backup
        from arq_reader.decrypt import decrypt_encrypted_object
        from arq_validator import LocalBackend
        from arq_validator.crypto import decrypt_keyset
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "a.txt").write_bytes(b"x")
            dest = tdp / "dest"
            res = build_backup(
                str(src), str(dest), encryption_password="pw",
            )
            b = LocalBackend(str(dest))
            ks = decrypt_keyset(
                b.read_all(
                    f"/{res.computer_uuid}/encryptedkeyset.dat",
                ),
                "pw",
            )
            arqo = b.read_all(
                f"/{res.computer_uuid}/backupplan.json",
            )
            plain = decrypt_encrypted_object(
                arqo, ks.encryption_key, ks.hmac_key,
            )
            # Parse preserving insertion order.
            ordered = json.loads(
                plain.decode("utf-8"), object_pairs_hook=list,
            )
            actual_keys = [k for k, _ in ordered]
            self.assertEqual(
                actual_keys, EXPECTED_BACKUPPLAN_KEY_ORDER,
                "writer's key order drifted — update either the "
                "source dict in build_backupplan or "
                "EXPECTED_BACKUPPLAN_KEY_ORDER above (and "
                "document the drift in docs/COMPATIBILITY.md)",
            )

    def test_writer_emit_stable_across_runs(self) -> None:
        """Two builds with the same input produce the same key
        order. Pins determinism."""
        from arq_writer.backup import build_backup
        from arq_reader.decrypt import decrypt_encrypted_object
        from arq_validator import LocalBackend
        from arq_validator.crypto import decrypt_keyset
        orders = []
        for _ in range(2):
            with tempfile.TemporaryDirectory() as td:
                tdp = Path(td)
                src = tdp / "src"
                src.mkdir()
                (src / "a.txt").write_bytes(b"x")
                dest = tdp / "dest"
                res = build_backup(
                    str(src), str(dest), encryption_password="pw",
                )
                b = LocalBackend(str(dest))
                ks = decrypt_keyset(
                    b.read_all(
                        f"/{res.computer_uuid}/encryptedkeyset.dat",
                    ),
                    "pw",
                )
                plain = decrypt_encrypted_object(
                    b.read_all(
                        f"/{res.computer_uuid}/backupplan.json",
                    ),
                    ks.encryption_key, ks.hmac_key,
                )
                ordered = json.loads(
                    plain.decode("utf-8"), object_pairs_hook=list,
                )
                orders.append([k for k, _ in ordered])
        self.assertEqual(
            orders[0], orders[1],
            "key order drifted across two builds — Python dict "
            "insertion order should be stable",
        )


class JSONByteEquivalenceWithArqAppDocumentedTests(unittest.TestCase):
    """Documents the byte-equivalence policy: our JSON emit is
    NOT byte-identical to Arq.app's because NSDictionary's
    internal hash-bucket layout differs from Python dict
    insertion order. Schema, types, and values all match;
    only the byte sequence differs.

    This test class exists primarily to document the policy via
    its docstrings; the single test pins the cross-format diff
    technique operators can use to compare JSON sidecars across
    writers in a key-order-independent way."""

    def test_canonical_normalisation_yields_byte_equivalence(
        self,
    ) -> None:
        """``json.dumps(json.loads(x), sort_keys=True)`` produces
        a canonical form that's byte-identical regardless of the
        original key order. Operators comparing our emit to
        Arq.app's must normalise this way first."""
        import json
        arq_app_emit = (
            '{"transferRateJSON":{},"cpuUsage":25,"id":1,'
            '"budgetGB":0}'
        )
        our_emit = (
            '{"budgetGB":0,"cpuUsage":25,"id":1,'
            '"transferRateJSON":{}}'
        )
        canonical_arq = json.dumps(
            json.loads(arq_app_emit), sort_keys=True,
        )
        canonical_ours = json.dumps(
            json.loads(our_emit), sort_keys=True,
        )
        self.assertEqual(canonical_arq, canonical_ours)


if __name__ == "__main__":
    unittest.main()
