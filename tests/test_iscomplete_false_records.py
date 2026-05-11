"""E5(new) — isComplete=false record handling.

Arq.app marks a backuprecord with ``isComplete: false`` when the
backup was interrupted (Cmd-Q during scan, network drop, power
loss). The destination still has whatever blobs+trees were
emitted before the interruption — the half-finished state is
useful for two purposes:

1. **Audit visibility** — operators see which records are
   complete vs partial, surfacing failed runs.
2. **Best-effort restore** — a partial record may still allow
   restoring the files that DID finish before the interruption.

Pins three properties:

- **Reader marks records correctly** — ``RecordInfo.is_complete``
  reflects the JSON field's value.
- **The validator accepts both** ``isComplete: true`` and
  ``isComplete: false`` records — the field's value isn't a
  format violation in either direction.
- **Best-effort restore of a partial record** — given a record
  whose tree references blobs that DO exist on the destination,
  restore should succeed for those blobs (treating missing
  blobs the same way it treats any data-corruption: graceful
  failure with a clear message, not crash).
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


@unittest.skipUnless(_has_openssl(), "openssl CLI required")
class IsCompleteFalseRecordTests(unittest.TestCase):

    def _build_and_patch_iscomplete(
        self, td: Path, is_complete: bool,
    ):
        """Build a backup, then re-encrypt the backuprecord JSON
        with the desired ``isComplete`` value."""
        from arq_writer.backup import build_backup
        from arq_writer.backuprecord import serialize_backuprecord
        from arq_validator import LocalBackend
        from arq_validator.crypto import decrypt_keyset
        from arq_reader.decrypt import decrypt_lz4_arqo
        from arq_writer.lz4_block import lz4_wrap

        src = td / "src"
        src.mkdir()
        (src / "a.txt").write_bytes(b"alpha")
        (src / "b.txt").write_bytes(b"bravo")
        dest = td / "dest"
        res = build_backup(
            str(src), str(dest), encryption_password="pw",
        )
        backend = LocalBackend(str(dest))
        ks = decrypt_keyset(
            backend.read_all(
                f"/{res.computer_uuid}/encryptedkeyset.dat",
            ),
            "pw",
        )
        # Decrypt the record, mutate isComplete, re-encrypt +
        # write back.
        rec_rel = str(
            res.backuprecord_path.relative_to(res.dest_root)
        )
        rec_full_path = (
            dest / rec_rel
        )
        arqo = rec_full_path.read_bytes()
        plain = decrypt_lz4_arqo(
            arqo, ks.encryption_key, ks.hmac_key,
        )
        rec = json.loads(plain.decode("utf-8"))
        rec["isComplete"] = is_complete
        new_plain = serialize_backuprecord(rec, fmt="json")
        # Re-emit with LZ4 + ARQO. Match the writer's pipeline.
        from arq_writer.crypto_write import build_encrypted_object
        # arq_writer's emit pipeline: serialize → lz4_wrap →
        # build_encrypted_object. Use the same calls in reverse.
        wrapped = lz4_wrap(new_plain)
        new_arqo = build_encrypted_object(
            wrapped, ks.encryption_key, ks.hmac_key,
        )
        rec_full_path.write_bytes(new_arqo)
        return dest, res.computer_uuid, res.folder_uuid

    def test_reader_reports_iscomplete_false(self) -> None:
        """RecordInfo.is_complete must reflect the JSON field
        when set to False."""
        from arq_reader import Restore
        with tempfile.TemporaryDirectory() as td:
            dest, cu, fu = self._build_and_patch_iscomplete(
                Path(td), is_complete=False,
            )
            rs = Restore(str(dest), encryption_password="pw")
            recs = rs.list_records(
                computer_uuid=cu, folder_uuid=fu,
            )
            self.assertGreater(len(recs), 0)
            for r in recs:
                self.assertFalse(
                    r.is_complete,
                    f"reader did not pick up isComplete=False: {r}",
                )

    def test_reader_reports_iscomplete_true_baseline(self) -> None:
        """Baseline: an unmodified record reads back as
        ``is_complete=True``. Pins the "no false-negative"
        direction."""
        from arq_reader import Restore
        with tempfile.TemporaryDirectory() as td:
            dest, cu, fu = self._build_and_patch_iscomplete(
                Path(td), is_complete=True,
            )
            rs = Restore(str(dest), encryption_password="pw")
            recs = rs.list_records(
                computer_uuid=cu, folder_uuid=fu,
            )
            self.assertGreater(len(recs), 0)
            for r in recs:
                self.assertTrue(r.is_complete)

    def test_validator_accepts_iscomplete_false(self) -> None:
        """A record with isComplete=False is a valid Arq 7
        artefact. Validator's B2 check (required keys) passes
        regardless of the value."""
        from arq_validator import (
            LocalBackend, check_arq7_compatibility,
        )
        with tempfile.TemporaryDirectory() as td:
            dest, cu, fu = self._build_and_patch_iscomplete(
                Path(td), is_complete=False,
            )
            report = check_arq7_compatibility(
                LocalBackend(str(dest)),
                "/", encryption_password="pw",
            )
            # No B2 failures on the isComplete field.
            b2_fails = [
                c for c in report.checks
                if c.id == "B2" and "isComplete" in c.name
                and not c.passed
            ]
            self.assertEqual(
                b2_fails, [],
                f"validator flagged isComplete=False as invalid: "
                f"{b2_fails}",
            )

    def test_partial_record_restore_still_works_when_blobs_present(
        self,
    ) -> None:
        """A record marked isComplete=False whose blob references
        ARE all present on disk still restores correctly. The
        completeness flag is metadata, not a gate on read."""
        from arq_reader import Restore
        with tempfile.TemporaryDirectory() as td:
            dest, cu, fu = self._build_and_patch_iscomplete(
                Path(td), is_complete=False,
            )
            out = Path(td) / "out"
            out.mkdir()
            rs = Restore(str(dest), encryption_password="pw")
            rs.restore(
                folder_uuid=fu, computer_uuid=cu, dest=out,
            )
            self.assertEqual(
                (out / "a.txt").read_bytes(), b"alpha",
            )
            self.assertEqual(
                (out / "b.txt").read_bytes(), b"bravo",
            )


if __name__ == "__main__":
    unittest.main()
