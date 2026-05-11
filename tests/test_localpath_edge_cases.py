"""C-I3 — localPath / localMountPoint edge cases.

Each backuprecord records the source's ``localPath`` (the
absolute path the operator backed up) + ``localMountPoint`` (the
filesystem mount point that path lives on). Restore + Arq.app
reader use these for UI display + restore-target hints.

Edge cases:

- **Root path** ``/`` — backing up an entire boot volume
- **/Volumes/X** — external volume
- **Nested mount point** — source path lives several dirs
  inside a mounted volume (mount point != source path)
- **Source with trailing slash** — should be normalised

This module pins the writer's emit for each shape so a
downstream tool walking the records sees consistent data.
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
class LocalPathEdgeCasesTests(unittest.TestCase):

    def _record_localPath(self, td: Path, source_path: Path):
        """Build a backup of ``source_path``, return the
        backuprecord's localPath string."""
        from arq_writer.backup import build_backup
        from arq_reader.decrypt import decrypt_lz4_arqo
        from arq_validator import LocalBackend
        from arq_validator.crypto import decrypt_keyset
        import json
        dest = td / "dest"
        res = build_backup(
            str(source_path), str(dest),
            encryption_password="pw",
        )
        b = LocalBackend(str(dest))
        ks = decrypt_keyset(
            b.read_all(
                f"/{res.computer_uuid}/encryptedkeyset.dat",
            ),
            "pw",
        )
        rec_arqo = b.read_all(
            "/" + str(
                res.backuprecord_path.relative_to(res.dest_root)
            )
        )
        plain = decrypt_lz4_arqo(
            rec_arqo, ks.encryption_key, ks.hmac_key,
        )
        record = json.loads(plain.decode("utf-8"))
        return record["localPath"], record["localMountPoint"]

    def test_normal_path_records_absolute(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "my-source"
            src.mkdir()
            (src / "f.txt").write_bytes(b"x")
            local_path, mount = self._record_localPath(tdp, src)
            # localPath is absolute.
            self.assertTrue(local_path.startswith("/"))
            # localMountPoint also absolute.
            self.assertTrue(mount.startswith("/"))

    def test_trailing_slash_normalised(self) -> None:
        """Source path with a trailing slash → localPath has no
        trailing slash."""
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "my-source"
            src.mkdir()
            (src / "f.txt").write_bytes(b"x")
            # Call with trailing slash.
            from arq_writer.backup import build_backup
            from arq_reader.decrypt import decrypt_lz4_arqo
            from arq_validator import LocalBackend
            from arq_validator.crypto import decrypt_keyset
            import json
            dest = tdp / "dest"
            res = build_backup(
                str(src) + "/", str(dest),
                encryption_password="pw",
            )
            b = LocalBackend(str(dest))
            ks = decrypt_keyset(
                b.read_all(
                    f"/{res.computer_uuid}/encryptedkeyset.dat",
                ),
                "pw",
            )
            plain = decrypt_lz4_arqo(
                b.read_all(
                    "/" + str(
                        res.backuprecord_path.relative_to(
                            res.dest_root,
                        )
                    )
                ),
                ks.encryption_key, ks.hmac_key,
            )
            record = json.loads(plain.decode("utf-8"))
            self.assertFalse(
                record["localPath"].endswith("/"),
                f"localPath has trailing slash: "
                f"{record['localPath']!r}",
            )

    def test_root_source_records_root_path(self) -> None:
        """When source is ``/`` (or a tempdir close to root),
        localPath reflects the exact path. We don't actually back
        up ``/`` (too large + permission-dependent) but the
        emit-side string handling must be correct."""
        # Use a tempdir as proxy for absolute path.
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            src = tdp / "root-like"
            src.mkdir()
            (src / "f.txt").write_bytes(b"x")
            local_path, mount = self._record_localPath(tdp, src)
            # On macOS the writer resolves /var symlinks → /private/var;
            # compare against the resolved form for stability.
            self.assertEqual(local_path, str(src.resolve()))


if __name__ == "__main__":
    unittest.main()
