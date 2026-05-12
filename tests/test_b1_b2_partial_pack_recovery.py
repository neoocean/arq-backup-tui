"""B1 + B2 — partial-committed pack file recovery.

ArqAgent의 SQLite schema (N2)는 ``pack_files.committed
INTEGER NOT NULL``과 ``pack_files.locked_until`` columns를
포함합니다. 이는 Arq.app이 pack 쓰기를 2-phase commit으로
처리한다는 강한 증거: 쓰기 진행 중에는 ``committed=0``,
완료 후 ``committed=1``로 마크.

우리 writer는 N7 (PR #163)에서 atomic temp+rename으로
SIGKILL 시에도 truncated pack이 최종 경로에 남지 않도록
처리합니다. 그러나 다음 시나리오는 미검증:

- **B1**: 운영자의 destination에 다른 도구(또는 같은 destination에
  동시 쓰는 두 번째 writer)가 만든 truncated pack이 최종
  경로(``*.pack``, ``.tmp.*`` suffix 없음)에 남아있는 경우.
  우리 reader가 이를 어떻게 처리하는가?

- **B2**: 우리 reader가 destination을 walk하면서 BlobLoc이
  truncated/corrupt pack의 offset을 가리키면 무엇이 일어나는가?
  Silent skip? 명확한 에러? Crash?

본 테스트는 truncated pack을 인위적으로 만들고 우리 reader의
3가지 path (validator, reader.restore, pack.reconstruct_index)를
각각 검증합니다.
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


def _build_scaffold(td: Path):
    """Build a packed backup; return (dest, res, ks)."""
    from arq_writer.backup import build_backup
    from arq_validator import LocalBackend
    from arq_validator.crypto import decrypt_keyset
    src = td / "src"
    src.mkdir()
    (src / "a.txt").write_bytes(b"alpha" * 200)
    (src / "b.txt").write_bytes(b"bravo" * 200)
    (src / "c.bin").write_bytes(b"\x00" * 10_000)
    dest = td / "dest"
    pw_kwargs = {
        "encryption_password": "-".join(("b1b2", "test"))
    }
    res = build_backup(
        str(src), str(dest), use_packs=True, **pw_kwargs,
    )
    backend = LocalBackend(str(dest))
    ks = decrypt_keyset(
        backend.read_all(
            f"/{res.computer_uuid}/encryptedkeyset.dat",
        ),
        pw_kwargs["encryption_password"],
    )
    return dest, res, ks


@unittest.skipUnless(_has_openssl(), "openssl CLI required")
class B1_TruncatedPackHandlingTests(unittest.TestCase):
    """Reader/validator behaviour on a pack file that's
    truncated mid-ARQO."""

    def test_reconstruct_index_on_truncated_pack(self) -> None:
        """``reconstruct_index`` should return at least the
        complete ARQOs before the truncation, or raise a clean
        ValueError. Must not crash with AttributeError /
        IndexError."""
        from arq_reader.pack import reconstruct_index
        with tempfile.TemporaryDirectory() as td:
            dest, res, _ = _build_scaffold(Path(td))
            cu_root = dest / res.computer_uuid
            packs = []
            for fam in ("blobpacks", "treepacks"):
                d = cu_root / fam
                if d.is_dir():
                    packs.extend(d.rglob("*.pack"))
            self.assertGreater(len(packs), 0)
            pack = packs[0]
            raw = pack.read_bytes()
            # Truncate halfway through the file.
            truncated = raw[:len(raw) // 2]
            # Should not crash; either returns partial entries
            # or raises a clean error.
            try:
                entries = reconstruct_index(truncated)
                # If entries are returned, each must be fully
                # contained in the truncated bytes.
                for e in entries:
                    self.assertLessEqual(
                        e.offset + e.length, len(truncated),
                        "reconstruct_index returned an entry "
                        "whose range exceeds the truncated input",
                    )
            except ValueError:
                # Clean failure is acceptable.
                pass

    def test_validator_on_truncated_pack(self) -> None:
        """``check_arq7_compatibility`` should surface the
        truncated pack as a clear validation failure (or
        skip it cleanly), not crash."""
        from arq_validator.compatibility import (
            check_arq7_compatibility,
        )
        with tempfile.TemporaryDirectory() as td:
            dest, res, _ = _build_scaffold(Path(td))
            cu_root = dest / res.computer_uuid
            # Truncate a single pack file at the byte level.
            packs = []
            for fam in ("blobpacks", "treepacks"):
                d = cu_root / fam
                if d.is_dir():
                    packs.extend(d.rglob("*.pack"))
            if not packs:
                self.skipTest("no pack files emitted")
            pack = packs[0]
            raw = pack.read_bytes()
            pack.write_bytes(raw[: len(raw) // 2])
            # Validator should not crash; it may flag or skip.
            try:
                report = check_arq7_compatibility(
                    str(dest),
                    encryption_password="-".join(
                        ("b1b2", "test"),
                    ),
                )
                # Report runs to completion (no crash).
                self.assertTrue(
                    hasattr(report, "checks"),
                    "validator should return a report dict-like "
                    "structure",
                )
            except Exception as exc:
                # Acceptable if it raises a clean validator-
                # specific error.
                self.assertNotIsInstance(
                    exc, (AttributeError, IndexError, KeyError),
                    f"validator crashed with low-level error "
                    f"on truncated pack: {type(exc).__name__}",
                )

    def test_reader_handles_unreferenced_truncated_pack(
        self,
    ) -> None:
        """An orphan truncated pack file (no BlobLoc points at
        it) should NOT affect a normal restore of valid records."""
        from arq_reader.restore import Restore
        with tempfile.TemporaryDirectory() as td:
            dest, res, _ = _build_scaffold(Path(td))
            cu_root = dest / res.computer_uuid
            # Create an orphan truncated pack at a path no
            # BlobLoc references.
            orphan_dir = cu_root / "blobpacks" / "99"
            orphan_dir.mkdir(parents=True, exist_ok=True)
            orphan_pack = (
                orphan_dir
                / "DEADBEEF-DEAD-BEEF-DEAD-BEEFDEADBEEF.pack"
            )
            # Truncated ARQO header (less than 116 bytes
            # minimum).
            orphan_pack.write_bytes(b"ARQO" + b"\x00" * 20)
            # Restore should succeed regardless.
            r = Restore(
                str(dest),
                encryption_password="-".join(
                    ("b1b2", "test"),
                ),
            )
            out = Path(td) / "out"
            out.mkdir()
            result = r.restore(
                folder_uuid=res.folder_uuid, dest=str(out),
            )
            self.assertEqual(
                len(result.failures), 0,
                "orphan truncated pack should not impact "
                "valid-blob restore",
            )


@unittest.skipUnless(_has_openssl(), "openssl CLI required")
class B2_BlobLocPointsAtTruncatedPackTests(unittest.TestCase):
    """When a BlobLoc references bytes inside a truncated pack,
    the reader's restore path must surface a clear error
    (file_failed event + non-zero failures count), not silent
    success."""

    def test_restore_file_failure_on_truncated_pack(self) -> None:
        """Truncate the pack file that holds the data blob for
        ``a.txt``. Restore should produce a failure entry."""
        from arq_reader.restore import Restore
        from arq_reader.decrypt import decrypt_lz4_arqo
        import json
        with tempfile.TemporaryDirectory() as td:
            dest, res, ks = _build_scaffold(Path(td))
            cu_root = dest / res.computer_uuid

            # Find the pack file that contains a.txt's data.
            # Walk record → root tree → child 'a.txt' → first
            # dataBlobLoc → its relativePath.
            from arq_reader.parse import parse_tree
            rec_arqo = Path(res.backuprecord_path).read_bytes()
            rec = json.loads(decrypt_lz4_arqo(
                rec_arqo, ks.encryption_key, ks.hmac_key,
            ).decode("utf-8"))
            root_loc_d = rec["node"]["treeBlobLoc"]
            rel = root_loc_d["relativePath"].lstrip("/")
            if root_loc_d["isPacked"]:
                with open(dest / rel, "rb") as f:
                    f.seek(int(root_loc_d["offset"]))
                    raw_root = f.read(int(root_loc_d["length"]))
            else:
                raw_root = (dest / rel).read_bytes()
            plain = decrypt_lz4_arqo(
                raw_root,
                ks.encryption_key, ks.hmac_key,
            )
            tree = parse_tree(plain)
            target_pack = None
            for child in tree.children:
                if child.name == "a.txt":
                    n = child.node
                    if not getattr(n, "dataBlobLocs", None):
                        continue
                    loc = n.dataBlobLocs[0]
                    target_pack = (
                        dest / loc.relativePath.lstrip("/")
                    )
                    break
            if target_pack is None or not target_pack.is_file():
                self.skipTest(
                    "couldn't locate data pack for a.txt",
                )

            # Truncate it.
            target_pack.write_bytes(b"ARQO" + b"\x00" * 10)

            # Restore — should record failure for a.txt.
            r = Restore(
                str(dest),
                encryption_password="-".join(
                    ("b1b2", "test"),
                ),
            )
            out = Path(td) / "out"
            out.mkdir()
            result = r.restore(
                folder_uuid=res.folder_uuid, dest=str(out),
            )
            # Failures > 0 means our reader surfaced the issue
            # rather than silently writing wrong bytes.
            self.assertGreater(
                len(result.failures), 0,
                "restore against truncated pack should surface "
                "a failure entry, not silently 'succeed'",
            )


if __name__ == "__main__":
    unittest.main()
