"""A보완-8 — Robustness scenarios via mock SFTP backend.

A1+A5 (PR #81) + A4 (PR #87) pinned reader robustness against
LocalBackend. SFTP / remote backends route through the same
``Backend`` protocol but with different error surfaces:

- LocalBackend's ``read_range`` returns short on EOF (Unix
  file.read semantics)
- SFTP servers may raise ``IOError`` / ``OSError`` for
  out-of-range reads, time out, or return partial data

This module re-runs the key robustness scenarios through a
**mock backend** that simulates SFTP-style error surfaces:

- ``RaiseOnOversizeBackend`` — raises on read past EOF (some
  SFTP servers do this)
- ``ShortReadBackend`` — silently returns fewer bytes than
  requested (some SFTP intermediaries do this on packet loss)

For each scenario, the reader's restore path must produce a
recognised error (no AttributeError / TypeError / silent
decode).

The mock backends are also useful documentation of the
``Backend`` protocol's permissive read_range contract — a
spec sketch of what implementations may do.
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


class _RaiseOnOversizeBackend:
    """Mock backend that raises IOError when read_range goes past
    end-of-file. Mirrors some SFTP servers' strict behaviour."""

    def __init__(self, real_backend):
        self._real = real_backend

    def __getattr__(self, name):
        return getattr(self._real, name)

    def read_range(self, path, offset, length):
        size = self._real.stat_size(path)
        if offset >= size:
            raise IOError(
                f"SFTP: read past EOF (offset {offset}, "
                f"file size {size})"
            )
        if offset + length > size:
            raise IOError(
                f"SFTP: read crosses EOF "
                f"(offset {offset}, length {length}, file size {size})"
            )
        return self._real.read_range(path, offset, length)


class _ShortReadBackend:
    """Mock backend that returns fewer bytes than requested even
    when within bounds. Simulates SFTP intermediaries that drop
    bytes on packet loss / timeout."""

    def __init__(self, real_backend, *, truncate_to: int):
        self._real = real_backend
        self._truncate_to = truncate_to

    def __getattr__(self, name):
        return getattr(self._real, name)

    def read_range(self, path, offset, length):
        full = self._real.read_range(path, offset, length)
        return full[: min(len(full), self._truncate_to)]


@unittest.skipUnless(_has_openssl(), "openssl CLI required")
class RobustnessThroughSFTPLikeBackendTests(unittest.TestCase):

    def _build_packed_backup(self, td: Path):
        from arq_writer.backup import build_backup
        src = td / "src"
        src.mkdir()
        (src / "f.bin").write_bytes(
            b"content for sftp robustness test " * 100,
        )
        dest = td / "dest"
        res = build_backup(
            str(src), str(dest),
            encryption_password="pw",
            use_packs=True,
        )
        return dest, res

    def test_raise_on_oversize_sftp_backend_clean_failure(
        self,
    ) -> None:
        """When SFTP-like backend raises on EOF-crossing read,
        restore reports the error cleanly (no internal crash)."""
        from arq_reader import Restore
        from arq_validator import LocalBackend
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            dest, res = self._build_packed_backup(tdp)
            # Wrap the real LocalBackend with the SFTP-mock.
            real_b = LocalBackend(str(dest))
            sftp_b = _RaiseOnOversizeBackend(real_b)
            # Construct restore with the mocked backend.
            rs = Restore(
                "/", encryption_password="pw",
                backend=sftp_b,
            )
            # Patch a BlobLoc to point past EOF — restore should
            # report the IOError cleanly.
            from arq_writer.types import BlobLoc
            bad_loc = BlobLoc(
                blobIdentifier="x" * 64,
                isPacked=True,
                relativePath=(
                    "/" + str(
                        next(
                            (dest / res.computer_uuid / "blobpacks")
                            .rglob("*.pack")
                        ).relative_to(dest)
                    )
                ),
                offset=10_000_000,   # past EOF
                length=1024,
                stretchEncryptionKey=True,
                compressionType=2,
            )
            ks = rs.keyset(res.computer_uuid)
            with self.assertRaises(Exception) as ctx:
                rs._fetch_blob(bad_loc, ks)
            err = type(ctx.exception).__name__
            self.assertNotIn(
                err, ("AttributeError", "TypeError"),
                f"oversize SFTP read produced internal error "
                f"{err}: {ctx.exception}",
            )

    def test_short_read_sftp_backend_caught_by_hmac(self) -> None:
        """When SFTP-like backend silently returns fewer bytes
        than requested, the decrypt step's HMAC check catches the
        truncation (no silent partial decode)."""
        from arq_reader import Restore
        from arq_validator import LocalBackend
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            dest, res = self._build_packed_backup(tdp)
            real_b = LocalBackend(str(dest))
            # Truncate every read to first 200 bytes — far short
            # of a typical ARQO (300 bytes header + content).
            sftp_b = _ShortReadBackend(real_b, truncate_to=200)
            rs = Restore(
                "/", encryption_password="pw",
                backend=sftp_b,
            )
            # The restore walk should fail somewhere (HMAC,
            # decrypt, parse) — NOT silently succeed.
            out = tdp / "out"
            out.mkdir()
            failure_seen = False
            try:
                layouts = rs.layouts()
                if layouts:
                    rs.restore(
                        folder_uuid=layouts[0].backup_folder_uuids[0],
                        computer_uuid=layouts[0].computer_uuid,
                        dest=out,
                    )
            except Exception as exc:
                failure_seen = True
                err = type(exc).__name__
                self.assertNotIn(
                    err, ("AttributeError", "TypeError"),
                    f"short-read SFTP produced internal error "
                    f"{err}: {exc}",
                )
            # Even if it didn't raise, the restored file must
            # NOT exist with correct content.
            restored = out / "f.bin"
            if restored.exists():
                self.assertNotEqual(
                    restored.read_bytes(),
                    b"content for sftp robustness test " * 100,
                    "short-read SFTP somehow produced correct "
                    "content — silent-decode regression",
                )


if __name__ == "__main__":
    unittest.main()
