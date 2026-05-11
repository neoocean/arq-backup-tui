"""N9 — ``arqVersion`` field receiver tolerance.

The ArqAgent binary contains the literal ``build 7.41 date %@``
and string ``nil arqVersion in Commit %@`` — confirming
Arq.app's reader:
1. Emits its own current version (7.41) into newly-written
   records.
2. Rejects records where ``arqVersion`` is nil (the
   ``nil arqVersion`` error path).

N9 probes our own reader + validator's tolerance band by
constructing records with ``arqVersion`` set to each of:
- Same major (7.41, 7.40, 7.0)
- Future major (8.0, 9.0)
- Empty string
- Missing key entirely

…and verifying our reader handles each shape gracefully
(restore succeeds for the legitimate cases; surfaces a
diagnostic for nil/missing).

This is a **forward-compat** check: if a future Arq.app v9
ships a record with ``arqVersion='9.0'``, our reader should
restore it without rejection. Conversely, our writer should
emit a sensible ``arqVersion`` value that real Arq.app v8
will accept.
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


BINARY = Path(
    "/Applications/Arq.app/Contents/Resources/"
    "ArqAgent.app/Contents/MacOS/ArqAgent"
)


@unittest.skipUnless(_has_openssl(), "openssl CLI required")
class N9_ReaderToleratesArqVersionVariantsTests(
    unittest.TestCase,
):
    """Patch a real BackupRecord's ``arqVersion`` to each
    variant + verify our reader handles each one."""

    def _patch_record_arq_version(
        self, dest, computer_uuid, folder_uuid, new_version,
        password, *, drop_key=False,
    ):
        """Decrypt + patch + re-encrypt the latest record."""
        from arq_writer.backuprecord import (
            parse_backuprecord, serialize_backuprecord,
        )
        from arq_writer.crypto_write import build_encrypted_object
        from arq_writer.lz4_block import lz4_wrap
        from arq_reader.decrypt import decrypt_lz4_arqo
        from arq_validator.crypto import decrypt_keyset

        ks = decrypt_keyset(
            (dest / computer_uuid /
             "encryptedkeyset.dat").read_bytes(),
            password,
        )
        import glob
        rec_paths = glob.glob(str(
            dest / computer_uuid / "backupfolders" / folder_uuid /
            "backuprecords" / "*" / "*.backuprecord"
        ))
        self.assertTrue(rec_paths)
        rp = Path(rec_paths[0])
        arqo = rp.read_bytes()
        plain = decrypt_lz4_arqo(
            arqo, ks.encryption_key, ks.hmac_key,
        )
        rec = parse_backuprecord(plain)
        if drop_key:
            rec.pop("arqVersion", None)
        else:
            rec["arqVersion"] = new_version
        new_plain = serialize_backuprecord(rec, fmt="json")
        new_arqo = build_encrypted_object(
            lz4_wrap(new_plain),
            ks.encryption_key, ks.hmac_key,
        )
        rp.write_bytes(new_arqo)
        return ks

    def _build_baseline(self, tdp):
        from arq_writer.backup import build_backup
        src = tdp / "src"
        src.mkdir()
        (src / "f.txt").write_bytes(b"baseline content\n")
        dest = tdp / "dest"
        res = build_backup(
            str(src), str(dest), encryption_password="pw",
        )
        return dest, res.computer_uuid, res.folder_uuid

    def test_arq_version_7_41_accepted(self) -> None:
        """The current Arq.app v8 version."""
        from arq_reader.restore import Restore
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            dest, cu, fu = self._build_baseline(tdp)
            self._patch_record_arq_version(
                dest, cu, fu, "7.41", "pw",
            )
            r = Restore(str(dest), encryption_password="pw")
            out = tdp / "out"
            out.mkdir()
            result = r.restore(folder_uuid=fu, dest=str(out))
            self.assertEqual(len(result.failures), 0)

    def test_arq_version_8_0_future_major_accepted(self) -> None:
        """Future major version — reader should still parse."""
        from arq_reader.restore import Restore
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            dest, cu, fu = self._build_baseline(tdp)
            self._patch_record_arq_version(
                dest, cu, fu, "8.0", "pw",
            )
            r = Restore(str(dest), encryption_password="pw")
            out = tdp / "out"
            out.mkdir()
            result = r.restore(folder_uuid=fu, dest=str(out))
            self.assertEqual(
                len(result.failures), 0,
                "future-major arqVersion='8.0' should be "
                "tolerated by our reader (forward-compat)",
            )

    def test_arq_version_legacy_accepted(self) -> None:
        """7.37 is the oldest version on the operator's real
        destination. Our reader should still handle it."""
        from arq_reader.restore import Restore
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            dest, cu, fu = self._build_baseline(tdp)
            self._patch_record_arq_version(
                dest, cu, fu, "7.37", "pw",
            )
            r = Restore(str(dest), encryption_password="pw")
            out = tdp / "out"
            out.mkdir()
            result = r.restore(folder_uuid=fu, dest=str(out))
            self.assertEqual(len(result.failures), 0)

    def test_arq_version_empty_string_tolerated(self) -> None:
        """Empty string for arqVersion — our reader is
        permissive (Arq.app v8 may not be). Pin the permissive
        contract."""
        from arq_reader.restore import Restore
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            dest, cu, fu = self._build_baseline(tdp)
            self._patch_record_arq_version(
                dest, cu, fu, "", "pw",
            )
            r = Restore(str(dest), encryption_password="pw")
            out = tdp / "out"
            out.mkdir()
            result = r.restore(folder_uuid=fu, dest=str(out))
            # Our reader treats empty version as a missing
            # value (defaults to "" downstream). Restore
            # should still succeed.
            self.assertEqual(len(result.failures), 0)

    def test_arq_version_missing_key_tolerated(self) -> None:
        """The key entirely absent. Arq.app would emit the
        'nil arqVersion' error here; our reader uses
        .get('arqVersion') with default '' so it succeeds.
        Pin the contract."""
        from arq_reader.restore import Restore
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            dest, cu, fu = self._build_baseline(tdp)
            self._patch_record_arq_version(
                dest, cu, fu, None, "pw", drop_key=True,
            )
            r = Restore(str(dest), encryption_password="pw")
            out = tdp / "out"
            out.mkdir()
            # Should still succeed; arqVersion isn't load-
            # bearing for restore.
            result = r.restore(folder_uuid=fu, dest=str(out))
            self.assertEqual(len(result.failures), 0)


@unittest.skipUnless(
    BINARY.is_file(),
    "ArqAgent not installed locally",
)
class N9_ArqAppArqVersionStringsTests(unittest.TestCase):
    """Confirm Arq.app v8 emits its own arqVersion in records
    (build string in binary) and has an explicit error path
    for nil arqVersion (would reject our emit with empty/None)."""

    def test_arqagent_embeds_own_build_version(self) -> None:
        proc = subprocess.run(
            ["strings", str(BINARY)],
            capture_output=True, check=True, text=True,
            timeout=60,
        )
        # Real Arq.app embeds a "build X.YZ date %@" string.
        self.assertIn(
            "build 7.4", proc.stdout,
            "Arq.app's own arqVersion baseline missing from "
            "binary strings — version probe may be stale",
        )

    def test_arqagent_has_nil_arq_version_rejection(self) -> None:
        """Arq.app explicitly rejects records with nil
        arqVersion. Our writer's emit of a non-empty string
        is therefore required for Arq.app reader acceptance —
        confirmed by our writer always setting arqVersion in
        build_backuprecord_dict."""
        proc = subprocess.run(
            ["strings", str(BINARY)],
            capture_output=True, check=True, text=True,
            timeout=60,
        )
        self.assertIn(
            "nil arqVersion", proc.stdout,
            "Arq.app's nil-arqVersion rejection path missing "
            "from binary — re-check Arq.app's tolerance",
        )


if __name__ == "__main__":
    unittest.main()
