"""C-I2 — encryption_password edge cases.

The Backup / Restore APIs accept ``encryption_password`` as a
free-form string. PBKDF2-SHA256 over UTF-8-encoded bytes derives
the AES key + HMAC key. Edge cases:

- **Empty string** — pathological but PBKDF2 accepts; AES key
  is well-defined. Round-trip should work.
- **Whitespace-only** — same as empty (no special handling).
- **Non-ASCII** — UTF-8 encoded; multi-byte chars preserved.
- **Very long** — PBKDF2's input length isn't capped; long
  passwords work but slower derivation (still bounded).
- **Wrong password on restore** — must FAIL with a recognised
  error (HMAC mismatch on keyset), no silent decode.

This module pins each branch.
"""

from __future__ import annotations

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
class EncryptionPasswordEdgeCasesTests(unittest.TestCase):

    def _round_trip(self, password: str) -> bool:
        """Build + restore with the given password. Return True
        iff round-trip succeeded byte-identically."""
        from arq_writer.backup import build_backup
        from arq_reader import Restore
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "f.txt").write_bytes(b"content for password test")
            dest = tdp / "dest"
            build_backup(
                str(src), str(dest), encryption_password=password,
            )
            out = tdp / "out"
            out.mkdir()
            rs = Restore(str(dest), encryption_password=password)
            layouts = rs.layouts()
            rs.restore(
                folder_uuid=layouts[0].backup_folder_uuids[0],
                computer_uuid=layouts[0].computer_uuid, dest=out,
            )
            return (
                (out / "f.txt").read_bytes()
                == b"content for password test"
            )

    def test_empty_password_round_trips(self) -> None:
        """Empty string is a valid password (PBKDF2 doesn't reject)."""
        self.assertTrue(self._round_trip(""))

    def test_whitespace_password_round_trips(self) -> None:
        self.assertTrue(self._round_trip("    "))

    def test_unicode_korean_password_round_trips(self) -> None:
        self.assertTrue(
            self._round_trip("비밀번호한국어테스트"),
        )

    def test_unicode_emoji_password_round_trips(self) -> None:
        self.assertTrue(
            self._round_trip("🔐🗝️password🔓"),
        )

    def test_very_long_password_round_trips(self) -> None:
        """1024-char password — PBKDF2 should accept any length."""
        long_pw = "abc" * 400
        self.assertTrue(self._round_trip(long_pw))

    def test_wrong_password_fails_recognisably(self) -> None:
        """Wrong password on restore raises a recognised crypto
        error — not AttributeError or silent partial decode."""
        from arq_writer.backup import build_backup
        from arq_reader import Restore
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "src"
            src.mkdir()
            (src / "f.txt").write_bytes(b"x")
            dest = tdp / "dest"
            build_backup(
                str(src), str(dest),
                encryption_password="correct-password",
            )
            rs = Restore(
                str(dest),
                encryption_password="wrong-password",
            )
            with self.assertRaises(Exception) as ctx:
                layouts = rs.layouts()
                rs.restore(
                    folder_uuid=layouts[0].backup_folder_uuids[0],
                    computer_uuid=layouts[0].computer_uuid,
                    dest=tdp / "out",
                )
            err = type(ctx.exception).__name__
            self.assertNotIn(
                err, ("AttributeError", "TypeError"),
                f"wrong password produced internal error "
                f"{err}: {ctx.exception}",
            )


if __name__ == "__main__":
    unittest.main()
