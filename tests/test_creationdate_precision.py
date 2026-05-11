"""D5 — creationDate field precision (int vs float).

The BackupRecord's ``creationDate`` field carries the wall-
clock moment the record was emitted. Two valid precisions:

- **int** (Unix epoch seconds) — Arq.app v8's convention.
  Sampled 2026-05-11 against ``/Volumes/arqbackup1`` (real v4
  record 7877564): ``creationDate`` is int.
- **float** (Unix epoch seconds + fractional) — Arq 5/6 era
  + some derived emit code paths.

Our writer's ``build_backuprecord_dict`` coerces via ``int()``,
matching Arq.app v8. This module pins:

- Default emit is int (no fractional component)
- Float input is coerced to int (no schema drift on caller bug)
- Reader's RecordInfo.creation_date is int (whether source was
  int or float)

If a future refactor introduces precision drift (e.g.
``json.dumps(..., default=str)`` formatting that turns int
seconds into "1777877564.0" strings), these tests flag it.
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


class CreationDateTypeTests(unittest.TestCase):

    def test_default_creationdate_is_int(self) -> None:
        from arq_writer.backuprecord import build_backuprecord_dict
        from arq_writer.types import FileNode
        rec = build_backuprecord_dict(
            backup_folder_uuid="F",
            backup_plan_uuid="P",
            backup_plan_dict={},
            root_node=FileNode(itemSize=0),
            local_path="/x",
        )
        self.assertIsInstance(
            rec["creationDate"], int,
            f"creationDate should be int, got "
            f"{type(rec['creationDate']).__name__}",
        )

    def test_float_input_is_coerced_to_int(self) -> None:
        """Caller passing a float (timestamp with fractional
        seconds) must produce an int in the record dict — matches
        Arq.app v8's emit precision."""
        from arq_writer.backuprecord import build_backuprecord_dict
        from arq_writer.types import FileNode
        rec = build_backuprecord_dict(
            backup_folder_uuid="F",
            backup_plan_uuid="P",
            backup_plan_dict={},
            root_node=FileNode(itemSize=0),
            local_path="/x",
            creation_date=1777877564.123456,
        )
        self.assertIsInstance(rec["creationDate"], int)
        self.assertEqual(rec["creationDate"], 1777877564)

    @unittest.skipUnless(_has_openssl(), "openssl CLI required")
    def test_emitted_record_creationDate_is_int_after_decrypt(
        self,
    ) -> None:
        """End-to-end: a full build_backup → decrypt → JSON parse
        yields an int creationDate."""
        from arq_writer.backup import build_backup
        from arq_validator import LocalBackend
        from arq_validator.crypto import decrypt_keyset
        from arq_reader.decrypt import decrypt_lz4_arqo
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
            rec_rel = str(
                res.backuprecord_path.relative_to(res.dest_root)
            )
            plain = decrypt_lz4_arqo(
                b.read_all("/" + rec_rel),
                ks.encryption_key, ks.hmac_key,
            )
            rec = json.loads(plain.decode("utf-8"))
            self.assertIsInstance(rec["creationDate"], int)
            # And it's a plausible Unix epoch (not 0 or negative).
            self.assertGreater(rec["creationDate"], 1_700_000_000)

    @unittest.skipUnless(_has_openssl(), "openssl CLI required")
    def test_reader_record_info_creation_date_is_int(self) -> None:
        """The reader's RecordInfo.creation_date type matches the
        source field — int after our writer's emit."""
        from arq_writer.backup import build_backup
        from arq_reader import Restore
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "a.txt").write_bytes(b"x")
            res = build_backup(
                str(src), str(tdp / "dest"),
                encryption_password="pw",
            )
            rs = Restore(
                str(tdp / "dest"), encryption_password="pw",
            )
            recs = rs.list_records(
                computer_uuid=res.computer_uuid,
                folder_uuid=res.folder_uuid,
            )
            self.assertGreater(len(recs), 0)
            for r in recs:
                self.assertIsInstance(r.creation_date, int)


if __name__ == "__main__":
    unittest.main()
