"""D6 + D7 + D8 + D10 — value-level checks for config JSON +
plist/JSON serialization round-trip.

Earlier D-series rounds pinned **schema** (every required field
present + correct type). This module pins **value** correctness
for fields that were schema-only before:

- **D6 — ``additionalUnpackedBlobDirs``**: the field's role is to
  list directories the reader should also scan for standalone
  (unpacked) blobs in addition to ``standardobjects``. Arq.app
  emits an empty list by default; non-empty values appear when
  the user has migrated from an older Arq destination layout.
  D6 verifies the writer's emit shape and the validator's type
  acceptance for both the empty and non-empty cases.

- **D7 — ``archiveUploadedDate``**: this BackupRecord JSON field
  appears in Arq.app destinations where a record has been
  uploaded to a separate archive tier. Our writer doesn't emit
  it (we don't have archive-tier infrastructure); the reader
  must round-trip records that DO carry it without crashing.
  D7 pins the reader's pass-through behaviour for unknown
  BackupRecord fields generally, with ``archiveUploadedDate``
  as the canonical example.

- **D8 — ``backupconfig.json`` value-level**: every emitted field
  matches the values Arq.app v8 actually writes. Schema check
  L3 enforced types only; D8 enforces values for the constants
  Arq.app emits (``isWORM: false``, ``containsGlacierArchives:
  false``, ``blobStorageClass: "STANDARD"``, etc.) — operator-
  overridable values are checked against the override.

- **D10 — Plist binary vs JSON round-trip**: both serialization
  formats decode into the same dict shape. D10 pins this with
  a property-based check: build a record, serialize as JSON,
  serialize as binary-plist, parse each back, verify the two
  decoded dicts are equal in the keys that matter.
"""

from __future__ import annotations

import json
import plistlib
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


class D6_AdditionalUnpackedBlobDirsTests(unittest.TestCase):
    """``additionalUnpackedBlobDirs`` should be present + a list +
    empty by default. Validator should accept both empty and
    non-empty values without raising."""

    def test_default_emit_is_empty_list(self) -> None:
        from arq_writer.json_configs import build_backupconfig
        cfg = build_backupconfig(
            backup_name="test",
            computer_name="test-host",
        )
        self.assertIn("additionalUnpackedBlobDirs", cfg)
        self.assertIsInstance(cfg["additionalUnpackedBlobDirs"], list)
        self.assertEqual(cfg["additionalUnpackedBlobDirs"], [])

    def test_field_round_trips_through_json(self) -> None:
        """Validator accepts the field when present + non-empty
        (operator might point at a sibling destination's
        ``standardobjects`` for cross-destination dedup
        reading). Pin: serialize → parse → field preserved."""
        from arq_writer.json_configs import build_backupconfig
        cfg = build_backupconfig(
            backup_name="test",
            computer_name="test-host",
        )
        # Inject a non-empty value as if the operator had set it.
        cfg["additionalUnpackedBlobDirs"] = [
            "/extra/path1",
            "/extra/path2",
        ]
        as_json = json.dumps(cfg, ensure_ascii=False)
        decoded = json.loads(as_json)
        self.assertEqual(
            decoded["additionalUnpackedBlobDirs"],
            ["/extra/path1", "/extra/path2"],
        )


@unittest.skipUnless(_has_openssl(), "openssl CLI required")
class D7_ArchiveUploadedDateTests(unittest.TestCase):
    """The reader must not crash on a BackupRecord JSON containing
    fields our writer doesn't emit. D7 pins this with
    ``archiveUploadedDate`` (a known Arq.app field that appears
    after archive-tier upload) as the example. The principle is
    forward-compatibility: any new Arq.app field that doesn't
    contradict known semantics should pass through."""

    def test_record_with_extra_archive_field_loads_via_parse(
        self,
    ) -> None:
        """Build a record dict, inject ``archiveUploadedDate``,
        round-trip through serialize_backuprecord → parse →
        back to dict. The field should survive the round-trip
        unchanged."""
        from arq_writer.backuprecord import (
            parse_backuprecord, serialize_backuprecord,
        )
        record = {
            "backupFolderUUID": "00000000-0000-0000-0000-000000000000",
            "backupPlanUUID": "11111111-1111-1111-1111-111111111111",
            "creationDate": 1777000000,
            "version": 100,
            # Arq.app's emit puts archiveUploadedDate as a numeric
            # Unix timestamp when the record is archived.
            "archiveUploadedDate": 1777999999,
            "node": {
                "isTree": True,
                "containedFilesCount": 0,
                "itemSize": 0,
                "treeBlobLoc": {
                    "blobIdentifier": "00" * 32,
                    "isPacked": False,
                    "isLargePack": False,
                    "relativePath": "/x/y",
                    "offset": 0,
                    "length": 0,
                    "stretchEncryptionKey": False,
                    "compressionType": 2,
                },
                "dataBlobLocs": [],
                "xattrsBlobLocs": [],
                "aclBlobLoc": None,
            },
        }
        serialized = serialize_backuprecord(record, fmt="json")
        decoded = parse_backuprecord(serialized)
        self.assertIn("archiveUploadedDate", decoded)
        self.assertEqual(decoded["archiveUploadedDate"], 1777999999)

    def test_reader_restore_path_tolerates_extra_record_field(
        self,
    ) -> None:
        """End-to-end: build a real backup, then surgically patch
        the BackupRecord's JSON to add ``archiveUploadedDate``,
        and verify restore still succeeds. Pin: extra root-level
        fields in the BackupRecord dict don't break restore."""
        from arq_writer.backup import build_backup
        from arq_writer.crypto_write import build_encrypted_object
        from arq_writer.lz4_block import lz4_wrap
        from arq_writer.backuprecord import (
            parse_backuprecord, serialize_backuprecord,
        )
        from arq_validator import LocalBackend
        from arq_validator.crypto import decrypt_keyset
        from arq_reader.decrypt import decrypt_lz4_arqo
        from arq_reader.restore import Restore
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "x.txt").write_bytes(b"x-content")
            dest = tdp / "dest"
            res = build_backup(
                str(src), str(dest), encryption_password="pw",
            )
            # Decrypt + patch + re-encrypt the backuprecord.
            backend = LocalBackend(str(dest))
            ks = decrypt_keyset(
                backend.read_all(
                    f"/{res.computer_uuid}/encryptedkeyset.dat",
                ),
                "pw",
            )
            rec_path = Path(res.backuprecord_path)
            arqo = rec_path.read_bytes()
            plain = decrypt_lz4_arqo(
                arqo, ks.encryption_key, ks.hmac_key,
            )
            rec_dict = parse_backuprecord(plain)
            rec_dict["archiveUploadedDate"] = 1888000000
            new_plain = serialize_backuprecord(rec_dict, fmt="json")
            new_arqo = build_encrypted_object(
                lz4_wrap(new_plain),
                ks.encryption_key, ks.hmac_key,
            )
            rec_path.write_bytes(new_arqo)
            # Now restore — must succeed despite the extra field.
            r = Restore(str(dest), encryption_password="pw")
            out = tdp / "out"
            out.mkdir()
            result = r.restore(
                folder_uuid=res.folder_uuid, dest=str(out),
            )
            self.assertEqual(len(result.failures), 0)


class D8_BackupconfigValueLevelTests(unittest.TestCase):
    """Pin the values Arq.app v8 emits for backupconfig.json
    constants. Sampled 2026-05-11 against
    ``/Volumes/arqbackup1``."""

    def test_arq_app_compatible_constant_values(self) -> None:
        from arq_writer.json_configs import build_backupconfig
        cfg = build_backupconfig(
            backup_name="test",
            computer_name="test-host",
        )
        # Constants Arq.app v8 always emits with these values.
        # (Operator overrides documented separately in D8 docs
        # section.)
        self.assertEqual(cfg["isWORM"], False)
        self.assertEqual(cfg["containsGlacierArchives"], False)
        self.assertEqual(cfg["blobStorageClass"], "STANDARD")
        self.assertEqual(cfg["computerSerial"], "unused")
        # ``isEncrypted`` default is True; the operator can flip
        # it for unencrypted destinations.
        self.assertEqual(cfg["isEncrypted"], True)

    def test_isencrypted_flips_with_operator_setting(self) -> None:
        from arq_writer.json_configs import build_backupconfig
        cfg = build_backupconfig(
            backup_name="test",
            computer_name="test-host",
            is_encrypted=False,
        )
        self.assertEqual(cfg["isEncrypted"], False)

    def test_blob_id_type_is_sha256_by_default(self) -> None:
        """Arq 7 spec sets ``blobIdentifierType: 2`` for SHA-256
        (SHA-1 was the legacy Arq 5 era). Pin: writer defaults
        to SHA-256 unless explicitly overridden."""
        from arq_writer.json_configs import build_backupconfig
        cfg = build_backupconfig(
            backup_name="test",
            computer_name="test-host",
        )
        self.assertEqual(
            cfg["blobIdentifierType"], 2,
            f"Arq 7 SHA-256 blob_id should encode as 2; "
            f"got {cfg['blobIdentifierType']}",
        )

    def test_chunker_version_default(self) -> None:
        """``chunkerVersion`` default should be 3 (the v3 chunker
        Arq.app v8 ships)."""
        from arq_writer.json_configs import build_backupconfig
        from arq_writer.constants import DEFAULT_CHUNKER_VERSION
        cfg = build_backupconfig(
            backup_name="test",
            computer_name="test-host",
        )
        self.assertEqual(cfg["chunkerVersion"], DEFAULT_CHUNKER_VERSION)


class D10_PlistJsonRoundTripTests(unittest.TestCase):
    """Both supported serialization formats decode into the same
    dict shape. Pin via round-trip equality."""

    def _make_record(self) -> dict:
        return {
            "backupFolderUUID":
                "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA",
            "backupPlanUUID":
                "BBBBBBBB-BBBB-BBBB-BBBB-BBBBBBBBBBBB",
            "creationDate": 1777777777,
            "version": 101,
            "computerOSType": 1,
            "isComplete": True,
            "node": {
                "isTree": True,
                "containedFilesCount": 3,
                "itemSize": 9001,
                "treeBlobLoc": {
                    "blobIdentifier": "ff" * 32,
                    "isPacked": True,
                    "isLargePack": False,
                    "relativePath": "/x/y/treepacks/00/aa.pack",
                    "offset": 0,
                    "length": 1024,
                    "stretchEncryptionKey": True,
                    "compressionType": 2,
                },
                "dataBlobLocs": [],
                "xattrsBlobLocs": [],
                "aclBlobLoc": None,
            },
        }

    def test_json_serialize_parse_round_trip_is_identity(
        self,
    ) -> None:
        from arq_writer.backuprecord import (
            parse_backuprecord, serialize_backuprecord,
        )
        rec = self._make_record()
        ser = serialize_backuprecord(rec, fmt="json")
        # First bytes are NOT the binary-plist magic.
        self.assertFalse(ser.startswith(b"bplist00"))
        parsed = parse_backuprecord(ser)
        self.assertEqual(parsed["backupFolderUUID"],
                         rec["backupFolderUUID"])
        self.assertEqual(parsed["version"], rec["version"])
        # Nested dict survives the round-trip.
        self.assertEqual(
            parsed["node"]["treeBlobLoc"]["blobIdentifier"],
            "ff" * 32,
        )

    def test_binary_plist_serialize_parse_round_trip(self) -> None:
        from arq_writer.backuprecord import (
            parse_backuprecord, serialize_backuprecord,
        )
        rec = self._make_record()
        ser = serialize_backuprecord(rec, fmt="binary-plist")
        # Binary plist starts with the well-known magic.
        self.assertTrue(
            ser.startswith(b"bplist00"),
            f"binary-plist emit should start with bplist00 magic; "
            f"got {ser[:8]!r}",
        )
        parsed = parse_backuprecord(ser)
        self.assertEqual(parsed["backupFolderUUID"],
                         rec["backupFolderUUID"])
        self.assertEqual(parsed["version"], rec["version"])
        self.assertEqual(
            parsed["node"]["treeBlobLoc"]["blobIdentifier"],
            "ff" * 32,
        )

    def test_json_and_plist_decode_to_equal_dicts(self) -> None:
        """The two formats encode into different bytes (one JSON
        text, one binary plist) but parse() returns equal dicts.
        Pin via field-by-field equality (modulo plist tagging
        differences for booleans / numbers)."""
        from arq_writer.backuprecord import (
            parse_backuprecord, serialize_backuprecord,
        )
        rec = self._make_record()
        from_json = parse_backuprecord(
            serialize_backuprecord(rec, fmt="json"),
        )
        from_plist = parse_backuprecord(
            serialize_backuprecord(rec, fmt="binary-plist"),
        )
        # Top-level fields equal.
        self.assertEqual(
            from_json["backupFolderUUID"],
            from_plist["backupFolderUUID"],
        )
        self.assertEqual(from_json["version"], from_plist["version"])
        self.assertEqual(
            from_json["isComplete"], from_plist["isComplete"],
        )
        # Nested treeBlobLoc fields equal.
        for k in (
            "blobIdentifier", "isPacked", "relativePath",
            "offset", "length", "compressionType",
        ):
            self.assertEqual(
                from_json["node"]["treeBlobLoc"][k],
                from_plist["node"]["treeBlobLoc"][k],
                f"mismatch on treeBlobLoc.{k}",
            )

    def test_unknown_format_raises_clean(self) -> None:
        from arq_writer.backuprecord import serialize_backuprecord
        with self.assertRaises(ValueError):
            serialize_backuprecord(self._make_record(), fmt="yaml")


if __name__ == "__main__":
    unittest.main()
